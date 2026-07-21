#!/usr/bin/env python3
"""
Tests for shared/router.py — complexity classifier with intent modifier.
"""
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure shared/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import re

import shared.adaptive as adaptive_module
import shared.router as router_module
from shared.config import TGsConfig
from shared.db import Database
from shared.router import TaskRouter, RoutingDecision
from shared.routing_report import build_routing_report, render_routing_accuracy_markdown
from shared.routing_hook import parse_hook_payload, validate_routing_guard


def _make_router() -> TaskRouter:
    return TaskRouter(TGsConfig())


def test_base_score_low_tier() -> None:
    """Simple task with no signals should be low tier."""
    router = _make_router()
    decision = router.classify("hello world")
    assert decision.tier == "low", f"Expected low, got {decision.tier}"
    assert decision.score <= 0.55


def test_override_low() -> None:
    """A dominating low keyword nudges the score toward low (no longer a hard set).

    The low override is now an additive score nudge, not a hard tier override, so
    downstream floors (security/reasoning) still apply. A single-concern docstring
    task still lands low, but ``override`` is False (only high overrides are hard).
    """
    router = _make_router()
    decision = router.classify("add a docstring to this function")
    assert decision.tier == "low"
    assert decision.override is False
    assert "low_override" in decision.reason


def test_low_override_suppressed_by_risk_cooccurrence() -> None:
    """A security-sensitive multi-concern task is not dragged to low by a keyword."""
    router = _make_router()
    decision = router.classify(
        "add a docstring and refactor credential keychain handling in auth.py"
    )
    assert decision.tier in {"medium", "high"}
    assert decision.override is False


def test_deep_security_review_override_high() -> None:
    """Explicit deep security review should force high tier."""
    router = _make_router()
    decision = router.classify("do a deep security review of this module")
    assert decision.tier == "high"
    assert decision.override is True


def test_generic_security_review_is_not_hard_high_override() -> None:
    router = _make_router()
    decision = router.classify("do a security review of this module")

    assert decision.tier in {"low", "medium"}
    assert decision.override is False


def test_routine_authentication_implementation_is_medium() -> None:
    router = _make_router()
    decision = router.classify(
        "Implement authentication middleware across auth.py service.py cli.py"
    )

    assert decision.tier == "medium"
    assert decision.override is False


def test_oauth_architecture_remains_high() -> None:
    router = _make_router()
    decision = router.classify("Architect an OAuth authentication migration")

    assert decision.tier == "high"
    assert decision.override is True


def test_intent_modifier_speed() -> None:
    """Speed signals should lower the effective score."""
    router = _make_router()
    # "implement" normally adds medium signal, but "quick" should lower it
    decision_normal = router.classify("implement message filtering logic")
    decision_quick = router.classify("quick implement message filtering logic")
    assert decision_quick.score < decision_normal.score
    assert decision_quick.intent_modifier < 0


def test_intent_modifier_quality() -> None:
    """Quality signals should raise the effective score."""
    router = _make_router()
    decision_normal = router.classify("add a helper function")
    decision_thorough = router.classify("thorough add a helper function")
    assert decision_thorough.score > decision_normal.score
    assert decision_thorough.intent_modifier > 0


def test_multi_file_bonus() -> None:
    """Multiple file references should increase score."""
    router = _make_router()
    decision = router.classify("update foo.py bar.js baz.ts to use new API")
    assert "multi_file" in decision.reason


def test_long_prompt_bonus() -> None:
    """Long prompts should get a score bump."""
    router = _make_router()
    long_task = " ".join(["word"] * 35)
    decision = router.classify(long_task)
    assert "long_prompt" in decision.reason


def test_tier_returns_labels_not_models() -> None:
    """Tier should be low/medium/high, never a model name."""
    router = _make_router()
    for task in ["simple fix", "implement auth", "architect system"]:
        decision = router.classify(task)
        assert decision.tier in ("low", "medium", "high"), (
            f"Got tier '{decision.tier}' for '{task}'"
        )


def test_hard_bounds_respected() -> None:
    """Thresholds should be within hard bounds."""
    config = TGsConfig()
    config.thresholds.low_max = 0.20  # below floor
    config.thresholds.medium_max = 0.99  # above ceiling
    config.thresholds.clamp()
    assert config.thresholds.low_max >= 0.50
    assert config.thresholds.medium_max <= 0.95


def test_project_local_optin_gate() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "router.db")
        router = TaskRouter(TGsConfig(), db=db)
        project_id = str(Path(td) / "project")

        assert router.is_learning_enabled(project_id) is False
        router.enable_learning(project_id)
        assert router.is_learning_enabled(project_id) is True
        db.close()


def test_project_learning_setting_round_trip_through_db_helper() -> None:
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "router.db")
        router = TaskRouter(TGsConfig(), db=db)
        project_id = str((Path(td) / "project").resolve())

        assert router.is_learning_enabled(project_id) is False
        db.set_project_setting(project_id, "learning_enabled", True)
        assert router.is_learning_enabled(project_id) is True
        db.reset_project_setting(project_id, "learning_enabled")
        assert router.is_learning_enabled(project_id) is False
        db.close()


def test_project_sample_min_gate() -> None:
    assert hasattr(router_module, "ACTIVATION_MIN_SAMPLES")
    assert router_module.ACTIVATION_MIN_SAMPLES == 5
    assert adaptive_module.PROJECT_SAMPLE_MIN == 3


# --- urgency ---


def test_classify_includes_urgency_fields():
    """D-07/D-08: RoutingDecision must expose urgency fields without breaking existing fields."""
    cfg = TGsConfig()
    r = TaskRouter(cfg)
    decision = r.classify("just a small change")

    # existing fields still present
    assert hasattr(decision, "score")
    assert hasattr(decision, "reason")

    # new urgency explainability surface
    assert hasattr(decision, "urgency_score")
    assert isinstance(decision.urgency_score, float)
    assert hasattr(decision, "matched_urgency_signals")
    assert isinstance(decision.matched_urgency_signals, list)
    # default should be 0.0 for non-urgent prompts
    assert decision.urgency_score == 0.0


def test_soft_implied_urgency_detected():
    """D-01/D-02: Softer implied urgency like "by EOD" and "ASAP" raises urgency_score."""
    cfg = TGsConfig()
    r = TaskRouter(cfg)
    prompt = "Please finish this by EOD — we need it ASAP."
    decision = r.classify(prompt)

    assert decision.urgency_score > 0.0
    # matched signals should mention at least eod or asap
    matched = " ".join(decision.matched_urgency_signals).lower()
    assert re.search(r"eod|asap|by eod", matched)


def test_excluded_phrases_do_not_raise_urgency():
    """D-03: Phrases like 'quick question', 'review', 'refactor' do not trigger urgency."""
    cfg = TGsConfig()
    r = TaskRouter(cfg)
    prompt = "Quick question: could you review this refactor?"
    decision = r.classify(prompt)

    assert decision.urgency_score == 0.0
    assert decision.matched_urgency_signals == []


# --- routing report ---


def test_build_routing_report_in_test_mode(monkeypatch) -> None:
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    report = build_routing_report(filter_categories=["low"])
    assert "summary" in report
    assert "config_hash" in report
    assert report["summary"]["fixture_count"] >= 1
    markdown = render_routing_accuracy_markdown(report)
    assert "Routing accuracy" in markdown
    assert "Executed accuracy" in markdown


# --- routing hook ---


def test_parse_hook_payload_extracts_edit_target() -> None:
    payload = {
        "tool_name": "Edit",
        "cwd": "/tmp/project",
        "tool_input": {"file_path": "src/main.py"},
    }
    fields = parse_hook_payload(payload)
    assert fields["tool_name"] == "Edit"
    assert fields["cwd"] == "/tmp/project"
    assert fields["target_file"] == "src/main.py"
    assert fields["caller"] == "claude-code"


def test_validate_routing_guard_blocks_without_guard(monkeypatch, tmp_path) -> None:
    import mcp_server
    from shared.config import TGsConfig
    from shared.db import Database

    db_path = tmp_path / "hook.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (cfg, db, None, None, None),
    )
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

    result = validate_routing_guard(
        caller="claude-code",
        cwd=str(tmp_path),
        target_file="foo.py",
        tool_name="Edit",
    )
    assert result["valid"] is False
    assert "route_task" in str(result.get("reason", "")).lower()


def test_routing_hook_cli_blocks_without_guard(monkeypatch, tmp_path, capsys) -> None:
    import mcp_server
    from shared.config import TGsConfig
    from shared.db import Database

    import shared.routing_hook as routing_hook

    db_path = tmp_path / "hook-cli.db"
    cfg = TGsConfig(db_path=db_path)
    db = Database(db_path=db_path)
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (cfg, db, None, None, None),
    )
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "claude-code")

    payload = json.dumps({
        "tool_name": "Write",
        "cwd": str(tmp_path),
        "tool_input": {"file_path": "bar.py"},
    })
    exit_code = routing_hook.main(["validate", "--json", payload])
    captured = capsys.readouterr()
    body = json.loads(captured.out)
    assert exit_code == 2
    assert body["valid"] is False


if __name__ == "__main__":
    tests = [
        test_base_score_low_tier,
        test_override_low,
        test_override_high,
        test_intent_modifier_speed,
        test_intent_modifier_quality,
        test_multi_file_bonus,
        test_long_prompt_bonus,
        test_tier_returns_labels_not_models,
        test_hard_bounds_respected,
        test_project_local_optin_gate,
        test_project_learning_setting_round_trip_through_db_helper,
        test_project_sample_min_gate,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ❌ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
