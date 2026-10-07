"""Claude Code Agent-tool hook (shared/agent_hook.py) and its ledger.

Hermetic: a tmp HOME, a tmp user agents dir, a tmp logs dir, default config. No
network, no real ``~/.claude``, no ``cache.db``.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from shared import agent_hook, agent_ledger, host_spawn
from shared.config import TGsConfig

ROOT = Path(__file__).resolve().parent.parent
HIGH_PROMPT = "Design and implement a secure OAuth token refresh architecture across services"
MEDIUM_PROMPT = "Refactor the parser module and update its unit tests to cover the new branches"
LOW_PROMPT = "review code"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    user_agents = home / ".claude" / "agents"
    logs = tmp_path / "logs"
    ws = tmp_path / "ws"
    for d in (user_agents, logs, ws):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("THRENODY_LOG_DIR", str(logs))
    monkeypatch.setenv("THRENODY_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.delenv("THRENODY_AGENT_HOOK", raising=False)
    monkeypatch.setattr(host_spawn, "claude_agents_dir", lambda: user_agents)
    monkeypatch.setattr(agent_hook, "_load_config", lambda: TGsConfig())
    host_spawn._FRONTMATTER_CACHE.clear()
    return {"home": home, "agents": user_agents, "logs": logs, "ws": ws, "tmp": tmp_path}


def _definition(directory: Path, name: str, *, model: str | None = None,
                effort: str | None = None, marked_base: str | None = None,
                mtime: float | None = None) -> Path:
    lines = ["---", f"name: {name}", f"description: {name} agent", "tools: Read"]
    if model:
        lines.append(f"model: {model}")
    if effort:
        lines.append(f"effort: {effort}")
    lines.append("---")
    if marked_base:
        lines.append(f"<!-- threnody:effort-variant base={marked_base} effort={effort} -->")
    lines.append(f"Body of {name}.")
    path = directory / f"{name}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _payload(env: dict[str, Path], session_start: float | None = None, **tool_input) -> dict:
    session_id = "sess-1"
    if session_start is not None:
        agent_ledger.record_session_start(session_id, session_start)
    tool_input.setdefault("description", "do a thing")
    tool_input.setdefault("prompt", LOW_PROMPT)
    return {
        "session_id": session_id,
        "transcript_path": "",
        "cwd": str(env["ws"]),
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        "tool_use_id": "toolu_1",
    }


def _ledger() -> list[dict]:
    return agent_ledger.read_events()


def _pre_events() -> list[dict]:
    return [e for e in _ledger() if e.get("event") == "pre"]


# ---------------------------------------------------------------------------
# pre — decision table
# ---------------------------------------------------------------------------

def test_builtin_gets_routed_model_only_when_missing(env):
    out = agent_hook.handle_pre(_payload(env, subagent_type="Explore", prompt=HIGH_PROMPT))
    assert out is not None
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["subagent_type"] == "Explore"
    assert updated["model"] == "opus"
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"

    assert agent_hook.handle_pre(
        _payload(env, subagent_type="Explore", prompt=HIGH_PROMPT, model="haiku")
    ) is None
    events = _pre_events()
    assert [e["decision"] for e in events] == ["rewrite", "pass"]
    assert {e["effort_source"] for e in events} == {"not_applicable"}


def test_threnody_planned_spawn_is_not_rerouted(env):
    start = time.time()
    _definition(env["agents"], "threnody-medium", model="sonnet", mtime=start - 100)
    _definition(env["agents"], "threnody-medium-high", model="sonnet", effort="high",
                marked_base="threnody-medium", mtime=start - 100)
    from shared.run_log import runs_root

    runs = runs_root()
    prompt = f"{HIGH_PROMPT}\nWrite it to {runs}/run-42/artifacts/agent-3.md as well."
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="threnody-medium-high", prompt=prompt)
    )
    assert out is None
    (event,) = _pre_events()
    assert event["decision"] == "pass"
    assert event["effort_source"] == "planned"
    assert event["run_id"] == "run-42"
    assert event["spawn_id"] == "agent-3"
    assert event["applied_effort"] == "high"
    assert event["tier"] is None  # never routed


def test_planned_variant_newer_than_session_falls_back_to_base(env):
    start = time.time() - 50
    _definition(env["agents"], "threnody-low", model="haiku", mtime=start - 100)
    _definition(env["agents"], "threnody-low-high", model="haiku", effort="high",
                marked_base="threnody-low", mtime=start + 10)
    out = agent_hook.handle_pre(_payload(env, start, subagent_type="threnody-low-high"))
    assert out is not None
    assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] == "threnody-low"
    (event,) = _pre_events()
    assert event["effort_source"] == "pending_restart"
    assert event["effort_unapplied_reason"] == "variant_created_after_session_start"


def test_bare_tier_type_gets_its_tier_variant(env):
    start = time.time()
    _definition(env["agents"], "threnody-medium", model="sonnet", mtime=start - 100)
    for effort in ("low", "medium", "high"):
        _definition(env["agents"], f"threnody-medium-{effort}", model="sonnet", effort=effort,
                    marked_base="threnody-medium", mtime=start - 100)
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="threnody-medium", prompt=HIGH_PROMPT)
    )
    updated = out["hookSpecificOutput"]["updatedInput"]
    (event,) = _pre_events()
    assert event["tier"] == "medium"  # the type's tier wins over the prompt's
    assert event["effort_source"] == "tier_variant"
    assert updated["subagent_type"] == f"threnody-medium-{event['applied_effort']}"
    assert "model" not in updated  # the tier definition pins sonnet


def test_skill_spawn_uses_visible_variant_and_routed_model(env):
    start = time.time()
    _definition(env["agents"], "gsd-planner", mtime=start - 100)
    _definition(env["agents"], "gsd-planner-high", effort="high", marked_base="gsd-planner",
                mtime=start - 100)
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="gsd-planner", prompt=HIGH_PROMPT)
    )
    assert out is not None
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["subagent_type"] == "gsd-planner-high"
    assert updated["model"] == "opus"
    (event,) = _pre_events()
    assert event["effort_source"] == "base_variant"
    assert event["applied_effort"] == "high"
    assert event["resolved_model_alias"] == "opus"
    assert event["tier"] == "high"


def test_variant_newer_than_session_keeps_base_type(env):
    start = time.time() - 50
    _definition(env["agents"], "gsd-planner", mtime=start - 100)
    _definition(env["agents"], "gsd-planner-high", effort="high", marked_base="gsd-planner",
                mtime=start + 10)
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="gsd-planner", prompt=HIGH_PROMPT)
    )
    assert out is not None
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["subagent_type"] == "gsd-planner"  # type untouched
    assert updated["model"] == "opus"
    (event,) = _pre_events()
    assert event["effort_source"] == "pending_restart"
    assert event["effort_unapplied_reason"] == "variant_created_after_session_start"
    assert event.get("applied_effort") is None


def test_definition_declared_model_and_effort_win(env):
    _definition(env["agents"], "auditor", model="sonnet", effort="medium")
    assert agent_hook.handle_pre(
        _payload(env, subagent_type="auditor", prompt=HIGH_PROMPT)
    ) is None
    (event,) = _pre_events()
    assert event["effort_source"] == "definition"
    assert event["applied_effort"] == "medium"
    assert event["model_source"] == "definition"


def test_definition_declared_effort_outside_resolver_vocabulary(env):
    _definition(env["agents"], "deep", model="opus", effort="xhigh")
    assert agent_hook.handle_pre(_payload(env, subagent_type="deep", prompt=HIGH_PROMPT)) is None
    (event,) = _pre_events()
    assert event["effort_source"] == "definition"
    assert event["applied_effort"] == "xhigh"
    assert not list(env["agents"].glob("deep-*.md"))


def test_explicit_model_is_never_changed(env):
    start = time.time()
    _definition(env["agents"], "gsd-planner", mtime=start - 100)
    _definition(env["agents"], "gsd-planner-high", effort="high", marked_base="gsd-planner",
                mtime=start - 100)
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="gsd-planner", prompt=HIGH_PROMPT, model="sonnet")
    )
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["model"] == "sonnet"
    assert updated["subagent_type"] == "gsd-planner-high"


def test_plugin_agent_creates_no_variant(env):
    assert agent_hook.handle_pre(
        _payload(env, subagent_type="auto-time:atlassian-fetch", prompt=HIGH_PROMPT)
    ) is None
    (event,) = _pre_events()
    assert event["effort_unapplied_reason"] == "plugin_definition_unresolvable"
    assert event["variant_created"] is None
    assert not list(env["agents"].iterdir())


def test_haiku_skips_effort_variants(env):
    _definition(env["agents"], "helper")
    out = agent_hook.handle_pre(_payload(env, subagent_type="helper", prompt=LOW_PROMPT))
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["model"] == "haiku"
    assert updated["subagent_type"] == "helper"
    (event,) = _pre_events()
    assert event["effort_source"] == "model_unsupported"
    assert not list(env["agents"].glob("helper-*.md"))


def test_unknown_custom_type_is_kept(env):
    out = agent_hook.handle_pre(_payload(env, subagent_type="from-cli-flag", prompt=MEDIUM_PROMPT))
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["subagent_type"] == "from-cli-flag"
    (event,) = _pre_events()
    assert event["effort_source"] == "unknown_base"


def test_missing_variant_is_rewritten_to_its_base(env):
    _definition(env["agents"], "helper", model="sonnet")
    out = agent_hook.handle_pre(_payload(env, subagent_type="helper-high", prompt=HIGH_PROMPT))
    assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] == "helper"
    (event,) = _pre_events()
    assert event["effort_unapplied_reason"] == "variant_missing"


def test_project_agent_variant_is_used_but_never_generated(env):
    start = time.time()
    project = env["ws"] / ".claude" / "agents"
    project.mkdir(parents=True)
    _definition(project, "proj-agent", mtime=start - 100)
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="proj-agent", prompt=HIGH_PROMPT)
    )
    assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] == "proj-agent"
    assert not list(project.glob("proj-agent-*.md"))
    assert not list(env["agents"].glob("proj-agent-*.md"))

    _definition(project, "proj-agent-high", effort="high", marked_base="proj-agent",
                mtime=start - 100)
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="proj-agent", prompt=HIGH_PROMPT)
    )
    assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] == "proj-agent-high"


def test_lazy_variant_creation_writes_marked_file_once(env):
    start = time.time() - 50
    _definition(env["agents"], "gsd-executor", mtime=start - 100)
    out = agent_hook.handle_pre(
        _payload(env, start, subagent_type="gsd-executor", prompt=MEDIUM_PROMPT)
    )
    assert out["hookSpecificOutput"]["updatedInput"]["subagent_type"] == "gsd-executor"
    variant = env["agents"] / "gsd-executor-medium.md"
    text = variant.read_text(encoding="utf-8")
    assert "<!-- threnody:effort-variant base=gsd-executor effort=medium -->" in text
    assert "effort: medium" in text
    (event,) = _pre_events()
    assert event["variant_created"] == str(variant)
    assert event["effort_source"] == "pending_restart"

    variant.unlink()
    agent_hook.handle_pre(_payload(env, start, subagent_type="gsd-executor", prompt=MEDIUM_PROMPT))
    assert not variant.exists()  # attempted once per (base, effort)


def test_lazy_variant_creation_never_overwrites_unmarked_file(env):
    _definition(env["agents"], "gsd-executor")
    own = env["agents"] / "gsd-executor-medium.md"
    own.write_text("mine\n", encoding="utf-8")
    future = time.time() + 1000
    os.utime(own, (future, future))
    agent_hook.handle_pre(
        _payload(env, time.time(), subagent_type="gsd-executor", prompt=MEDIUM_PROMPT)
    )
    assert own.read_text(encoding="utf-8") == "mine\n"


def test_updated_input_preserves_every_original_key(env):
    out = agent_hook.handle_pre(
        _payload(
            env,
            subagent_type="general-purpose",
            prompt=HIGH_PROMPT,
            run_in_background=True,
            isolation="worktree",
            name="worker-1",
        )
    )
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["run_in_background"] is True
    assert updated["isolation"] == "worktree"
    assert updated["name"] == "worker-1"
    assert updated["prompt"] == HIGH_PROMPT
    assert updated["description"] == "do a thing"
    assert set(updated) == {
        "subagent_type", "prompt", "description", "run_in_background", "isolation",
        "name", "model",
    }


def test_kill_switch_env_and_config(env, monkeypatch):
    monkeypatch.setenv("THRENODY_AGENT_HOOK", "off")
    assert agent_hook.handle_pre(_payload(env, subagent_type="Explore", prompt=HIGH_PROMPT)) is None
    monkeypatch.delenv("THRENODY_AGENT_HOOK")
    config = TGsConfig()
    config.agent_hook.enabled = False
    monkeypatch.setattr(agent_hook, "_load_config", lambda: config)
    assert agent_hook.handle_pre(_payload(env, subagent_type="Explore", prompt=HIGH_PROMPT)) is None
    assert _ledger() == []


def test_routing_exception_is_a_noop_with_error_line(env, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("router exploded")

    monkeypatch.setattr(agent_hook, "route", boom)
    assert agent_hook.handle_pre(_payload(env, subagent_type="Explore")) is None
    (event,) = _pre_events()
    assert event["decision"] == "error"
    assert "router exploded" in event["error"]


def test_prompt_body_never_reaches_the_ledger(env):
    secret = "TOP-SECRET-PROMPT-BODY"
    agent_hook.handle_pre(
        _payload(env, subagent_type="Explore", prompt=secret, description="x" * 300)
    )
    raw = agent_ledger.ledger_path().read_text(encoding="utf-8")
    assert secret not in raw
    (event,) = _pre_events()
    assert len(event["description"]) == agent_ledger.DESCRIPTION_MAX


def test_pre_row_carries_every_schema_field(env):
    agent_hook.handle_pre(_payload(env, subagent_type="Explore", prompt=HIGH_PROMPT))
    (event,) = _pre_events()
    assert set(agent_ledger.EVENT_FIELDS["pre"]) <= set(event)
    assert event["caller"] == "claude-code"
    assert event["variant_created"] is None and event["run_id"] is None


def test_default_session_start_feeds_resolver(env):
    start = time.time() - 50
    _definition(env["agents"], "threnody-high", model="opus", mtime=start - 100)
    _definition(env["agents"], "threnody-high-high", model="opus", effort="high",
                marked_base="threnody-high", mtime=start + 10)
    try:
        host_spawn.set_default_session_start(start)
        late = host_spawn.resolve_spawn_type(caller="claude-code", base=None, tier="high", effort="high")
        assert late.subagent_type == "threnody-high"
        assert late.effort_source == "pending_restart"
        host_spawn.set_default_session_start(None)
        free = host_spawn.resolve_spawn_type(caller="claude-code", base=None, tier="high", effort="high")
        assert free.subagent_type == "threnody-high-high"
    finally:
        host_spawn.set_default_session_start(None)


def test_non_agent_tool_and_malformed_input_are_noops(env):
    assert agent_hook.run("pre", "{not json") == ""
    task = _payload(env, subagent_type="Explore", prompt=HIGH_PROMPT)
    task["tool_name"] = "Task"  # only the Agent tool is ever answered
    assert agent_hook.run("pre", json.dumps(task)) == ""
    assert agent_hook.run("pre", "") == ""
    assert agent_hook.run("pre", json.dumps({"tool_name": "Bash", "tool_input": {}})) == ""
    assert agent_hook.run("pre", json.dumps({"tool_name": "Agent", "tool_input": "x"})) == ""
    assert agent_hook.run("bogus", "{}") == ""


def test_wrapper_always_exits_zero(tmp_path):
    script = ROOT / "shell" / "threnody-agent-hook.sh"
    env = {**os.environ, "THRENODY_LOG_DIR": str(tmp_path), "HOME": str(tmp_path)}
    for sub, stdin in (("pre", "garbage"), ("post", "{}"), ("nope", ""), ("pre", "")):
        result = subprocess.run(
            ["bash", str(script), sub], input=stdin, capture_output=True, text=True,
            env=env, timeout=30, check=False,
        )
        assert result.returncode == 0
        assert result.stdout == ""


# ---------------------------------------------------------------------------
# ledger-only events, join, rotation, session file
# ---------------------------------------------------------------------------

def test_post_and_subagent_events_join_with_pre(env):
    agent_hook.handle_pre(_payload(env, subagent_type="Explore", prompt=HIGH_PROMPT))
    agent_hook.run("subagent-start", json.dumps(
        {"session_id": "sess-1", "agent_id": "a1", "agent_type": "Explore"}
    ))
    agent_hook.run("subagent-stop", json.dumps(
        {"session_id": "sess-1", "agent_id": "a1", "agent_type": "Explore",
         "last_assistant_message": "secret answer"}
    ))
    agent_hook.run("post", json.dumps({
        "session_id": "sess-1", "tool_name": "Agent", "tool_use_id": "toolu_1",
        "duration_ms": 1234,
        "tool_response": {"status": "completed", "agentId": "a1", "agentType": "Explore",
                          "resolvedModel": "claude-opus-4-5-20251101"},
    }))
    (spawn,) = agent_ledger.join_spawns(_ledger())
    assert spawn["pre"]["resolved_model_alias"] == "opus"
    assert spawn["post"]["resolved_model"] == "claude-opus-4-5-20251101"
    assert spawn["post"]["duration_ms"] == 1234
    assert spawn["start"]["agent_id"] == spawn["stop"]["agent_id"] == "a1"
    assert "secret answer" not in agent_ledger.ledger_path().read_text(encoding="utf-8")

    summary = agent_ledger.summarize()
    assert summary["total"] == 1
    assert summary["by_resolved_model"] == {"claude-opus-4-5-20251101": 1}
    assert summary["by_effort_source"] == {"not_applicable": 1}
    assert summary["effort_unapplied"] == 1  # routed high, builtin takes none


def test_ledger_rotates_and_keeps_three_files(env):
    for i in range(40):
        agent_ledger.append_event({"event": "pre", "n": i, "pad": "x" * 200}, max_bytes=1000)
    files = sorted(p.name for p in env["logs"].glob("agent_spawns.jsonl*") if not p.name.endswith(".lock"))
    assert files == ["agent_spawns.jsonl", "agent_spawns.jsonl.1", "agent_spawns.jsonl.2"]
    numbers = [e["n"] for e in agent_ledger.read_events()]
    assert numbers == sorted(numbers) and numbers[-1] == 39
    assert len(agent_ledger.tail(3)) == 3


def test_session_start_file_is_pruned_and_ignores_compact(env, monkeypatch):
    monkeypatch.setattr(agent_ledger, "MAX_SESSIONS", 3)
    for i in range(5):
        agent_ledger.record_session_start(f"s{i}", 1000.0 + i)
    sessions = agent_ledger.read_json_state(agent_ledger.SESSIONS_NAME)
    assert set(sessions) == {"s2", "s3", "s4"}

    agent_hook.run("session-start", json.dumps({"session_id": "s4", "source": "compact"}))
    assert agent_ledger.session_start_for("s4") == 1004.0
    agent_hook.run("session-start", json.dumps({"session_id": "s4", "source": "resume"}))
    assert agent_ledger.session_start_for("s4") > 1004.0


def test_session_start_falls_back_to_transcript(env):
    transcript = env["tmp"] / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    payload = {"session_id": "unknown", "transcript_path": str(transcript)}
    assert agent_hook.session_start_ts(payload) == pytest.approx(
        host_spawn.session_start_from_transcript(str(transcript))
    )
    assert agent_hook.session_start_ts({"session_id": "unknown"}) is None


def test_status_agent_spawns_section(env):
    from shared.status import _load_agent_spawn_summary

    agent_hook.handle_pre(_payload(env, subagent_type="Explore", prompt=HIGH_PROMPT))
    section = _load_agent_spawn_summary()
    assert section["window_hours"] == 24
    assert section["total"] == 1
    assert section["effort_unapplied"] == 1
    assert section["recent_unapplied"][0]["reason"] == "builtin_type"


def test_pre_hook_cold_start_latency(tmp_path):
    """Cold ``python3 -m shared.agent_hook pre`` stays well inside the hook timeout."""
    payload = json.dumps({
        "session_id": "s", "cwd": str(tmp_path), "tool_name": "Agent", "tool_use_id": "t",
        "tool_input": {"subagent_type": "Explore", "prompt": HIGH_PROMPT, "description": "d"},
    })
    env = {**os.environ, "THRENODY_LOG_DIR": str(tmp_path), "HOME": str(tmp_path),
           "THRENODY_INSTALL_DIR": str(tmp_path), "PYTHONPATH": str(ROOT)}
    started = time.perf_counter()
    result = subprocess.run(
        ["python3", "-m", "shared.agent_hook", "pre"], input=payload, capture_output=True,
        text=True, env=env, cwd=str(ROOT), timeout=30, check=False,
    )
    elapsed = time.perf_counter() - started
    assert result.returncode == 0
    assert json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "opus"
    assert elapsed < 3.0  # generous for loaded CI; typical is far below
