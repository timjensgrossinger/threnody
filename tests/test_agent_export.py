from __future__ import annotations

"""Tests for shared.agent_export."""

import sys
import time
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.db import Database
from shared.agent_export import (
    ExportTarget,
    _resolve_export_path,
    _slugify,
    export_agent_skill,
    export_all_active,
    check_and_promote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_agent(db: Database, pattern_hash: str, state: str, match_count: int = 5) -> None:
    definition = "---\nname: test-agent\ndescription: does things\n---\n\nDo things well."
    with db.conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO agent_definitions "
            "(pattern_hash, pattern_desc, definition, match_count, ts, promotion_state, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pattern_hash, "test pattern", definition, match_count, time.time(), state, state),
        )


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------

def test_slugify_basic() -> None:
    assert _slugify("JWT Auth Handler") == "jwt-auth-handler"


def test_slugify_empty() -> None:
    assert _slugify("") == "learned-agent"


def test_slugify_special_chars() -> None:
    slug = _slugify("  foo / bar :: baz  ")
    assert "/" not in slug
    assert " " not in slug
    assert slug.startswith("foo")


# ---------------------------------------------------------------------------
# _resolve_export_path
# ---------------------------------------------------------------------------

def test_resolve_export_path_skill_dir(tmp_path: Path) -> None:
    out = _resolve_export_path(tmp_path, ".claude/skills", "my-agent", "skill_dir")
    assert out == tmp_path / ".claude" / "skills" / "my-agent" / "SKILL.md"


def test_resolve_export_path_flat_md(tmp_path: Path) -> None:
    out = _resolve_export_path(tmp_path, ".github/agents", "my-agent", "flat_md")
    assert out == tmp_path / ".github" / "agents" / "my-agent.md"


def test_resolve_export_path_escape_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escaped root"):
        _resolve_export_path(tmp_path, "../../../etc", "passwd", "flat_md")


# ---------------------------------------------------------------------------
# export_agent_skill — approval gate
# ---------------------------------------------------------------------------

def test_export_non_active_raises(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "draft-hash", "draft")
    with pytest.raises(ValueError, match="only 'active'"):
        export_agent_skill(temp_db_fixture, "draft-hash", project_path=str(tmp_path))


def test_export_unknown_agent_raises(temp_db_fixture: Database, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        export_agent_skill(temp_db_fixture, "nonexistent-hash", project_path=str(tmp_path))


# ---------------------------------------------------------------------------
# export_agent_skill — happy path (claude-code, project scope)
# ---------------------------------------------------------------------------

def test_export_writes_skill_md(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "active-hash", "active")
    result = export_agent_skill(
        temp_db_fixture, "active-hash",
        providers=["claude-code"],
        scope="project",
        project_path=str(tmp_path),
    )
    assert len(result["written"]) == 1
    assert result["errors"] == []
    written_path = Path(result["written"][0]["path"])
    assert written_path.exists()
    assert written_path.name == "SKILL.md"
    assert "does things" in written_path.read_text()


@pytest.mark.parametrize(
    ("provider", "expected_path"),
    [
        ("codex", ".codex/skills/test-pattern/SKILL.md"),
        ("cursor", ".cursor/skills/test-pattern/SKILL.md"),
    ],
)
def test_export_writes_provider_skill_dirs(
    temp_db_fixture: Database,
    tmp_path: Path,
    provider: str,
    expected_path: str,
) -> None:
    _insert_agent(temp_db_fixture, f"active-{provider}", "active")
    result = export_agent_skill(
        temp_db_fixture,
        f"active-{provider}",
        providers=[provider],
        scope="project",
        project_path=str(tmp_path),
    )
    assert result["errors"] == []
    assert Path(result["written"][0]["path"]) == tmp_path / expected_path
    assert (tmp_path / expected_path).is_file()


def test_export_dry_run_no_write(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "active-hash2", "active")
    result = export_agent_skill(
        temp_db_fixture, "active-hash2",
        providers=["claude-code"],
        scope="project",
        project_path=str(tmp_path),
        dry_run=True,
    )
    assert result["written"][0]["dry_run"] is True
    written_path = Path(result["written"][0]["path"])
    assert not written_path.exists()


def test_export_unknown_provider_skipped(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "active-hash3", "active")
    result = export_agent_skill(
        temp_db_fixture, "active-hash3",
        providers=["no-such-provider"],
        project_path=str(tmp_path),
    )
    assert result["written"] == []
    assert result["skipped"][0]["reason"] == "unknown provider"


# ---------------------------------------------------------------------------
# export_agent_skill — copilot flat_md layout
# ---------------------------------------------------------------------------

def test_export_copilot_flat_md(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "copilot-hash", "active")
    result = export_agent_skill(
        temp_db_fixture, "copilot-hash",
        providers=["github-copilot-cli"],
        scope="project",
        project_path=str(tmp_path),
    )
    assert len(result["written"]) == 1
    written_path = Path(result["written"][0]["path"])
    assert written_path.suffix == ".md"
    assert written_path.parent.name == "agents"


# ---------------------------------------------------------------------------
# export_agent_skill — audit rows created
# ---------------------------------------------------------------------------

def test_export_creates_audit_row(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "audit-hash", "active")
    export_agent_skill(
        temp_db_fixture, "audit-hash",
        providers=["claude-code"],
        scope="project",
        project_path=str(tmp_path),
    )
    events = temp_db_fixture.list_agent_audit_events(agent_id="audit-hash") or []
    assert any(e.get("event_type") == "skill_exported" for e in events)


# ---------------------------------------------------------------------------
# export_agent_skill — no project_path for project scope skips
# ---------------------------------------------------------------------------

def test_export_missing_project_path_skips(temp_db_fixture: Database) -> None:
    _insert_agent(temp_db_fixture, "no-proj-hash", "active")
    result = export_agent_skill(
        temp_db_fixture, "no-proj-hash",
        providers=["claude-code"],
        scope="project",
        project_path=None,
    )
    assert result["written"] == []
    assert result["skipped"][0]["reason"] == "project_path required for scope=project"


# ---------------------------------------------------------------------------
# export_all_active
# ---------------------------------------------------------------------------

def test_export_all_active_skips_non_active(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "act1", "active")
    _insert_agent(temp_db_fixture, "dft1", "draft")
    results = export_all_active(
        temp_db_fixture,
        providers=["claude-code"],
        scope="project",
        project_path=str(tmp_path),
    )
    written_counts = [len(r["written"]) for r in results if r.get("written")]
    assert sum(written_counts) >= 1


# ---------------------------------------------------------------------------
# check_and_promote
# ---------------------------------------------------------------------------

def test_check_and_promote_auto_disabled(temp_db_fixture: Database) -> None:
    _insert_agent(temp_db_fixture, "promo-hash", "active", match_count=50)
    from shared.config import TGsConfig
    cfg = TGsConfig()
    cfg.skill_auto_promote = False
    result = check_and_promote(temp_db_fixture, "promo-hash", cfg)
    assert result["promoted"] is False
    assert result["reason"] == "auto_promote disabled"


def test_check_and_promote_below_threshold(temp_db_fixture: Database) -> None:
    _insert_agent(temp_db_fixture, "below-thresh", "active", match_count=2)
    from shared.config import TGsConfig
    cfg = TGsConfig()
    cfg.skill_auto_promote = True
    cfg.skill_promotion_threshold = 10
    result = check_and_promote(temp_db_fixture, "below-thresh", cfg)
    assert result["promoted"] is False
    assert "threshold" in result["reason"]


def test_check_and_promote_at_threshold(temp_db_fixture: Database, tmp_path: Path) -> None:
    _insert_agent(temp_db_fixture, "at-thresh", "active", match_count=10)
    from shared.config import TGsConfig
    cfg = TGsConfig()
    cfg.skill_auto_promote = True
    cfg.skill_promotion_threshold = 10
    cfg.skill_export_providers = ["claude-code"]

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    with unittest.mock.patch("shared.agent_export.Path") as mock_path_cls:
        # Replace Path.home() to return our fake home
        real_path = Path
        def patched_path(*args, **kwargs):
            return real_path(*args, **kwargs)
        patched_path.home = lambda: fake_home
        patched_path.cwd = real_path.cwd
        mock_path_cls.side_effect = patched_path
        mock_path_cls.home = lambda: fake_home

        # Easier: patch at ExportTarget level
        pass

    # Direct approach: patch _BUILTIN_TARGETS to use tmp_path
    from shared import agent_export as ae_mod
    original = ae_mod._BUILTIN_TARGETS[:]
    ae_mod._BUILTIN_TARGETS = [
        ExportTarget(
            provider_id="claude-code",
            project_subdir=".claude/skills",
            global_dir=tmp_path / "global-skills",
            layout="skill_dir",
        ),
    ]
    ae_mod._TARGET_BY_PROVIDER = {t.provider_id: t for t in ae_mod._BUILTIN_TARGETS}
    try:
        result = check_and_promote(temp_db_fixture, "at-thresh", cfg)
        assert result["promoted"] is True
        assert len(result["written"]) >= 1
        # exported_global_ts should be set
        row = temp_db_fixture.get_agent_definition("at-thresh")
        assert row is not None
        assert row.get("exported_global_ts") is not None
    finally:
        ae_mod._BUILTIN_TARGETS = original
        ae_mod._TARGET_BY_PROVIDER = {t.provider_id: t for t in original}


def test_check_and_promote_idempotent(temp_db_fixture: Database, tmp_path: Path) -> None:
    """Already-promoted agents are not promoted twice."""
    _insert_agent(temp_db_fixture, "already-promoted", "active", match_count=20)
    with temp_db_fixture.conn() as conn:
        conn.execute(
            "UPDATE agent_definitions SET exported_global_ts = ? WHERE pattern_hash = ?",
            (time.time(), "already-promoted"),
        )
    from shared.config import TGsConfig
    cfg = TGsConfig()
    cfg.skill_auto_promote = True
    cfg.skill_promotion_threshold = 10
    result = check_and_promote(temp_db_fixture, "already-promoted", cfg)
    assert result["promoted"] is False
    assert result["reason"] == "already promoted"


def test_export_tier_effort_variants_generates_nine(tmp_path: Path) -> None:
    from shared.agent_export import export_tier_effort_variants

    written = export_tier_effort_variants(ROOT / "shell" / "agents", tmp_path)
    assert len(written) == 9
    text = (tmp_path / "threnody-medium-high.md").read_text(encoding="utf-8")
    lines = text.split("\n")
    assert "name: threnody-medium-high" in lines
    model_idx = lines.index("model: sonnet")
    assert lines[model_idx + 1] == "effort: high"
    assert "(effort high)" in next(l for l in lines if l.startswith("description:"))
    assert "Threnody host subagent" in text


def test_export_tier_effort_variants_skips_missing_frontmatter(tmp_path: Path) -> None:
    from shared.agent_export import export_tier_effort_variants

    src = tmp_path / "src"
    src.mkdir()
    (src / "threnody-low.md").write_text("no frontmatter\n", encoding="utf-8")
    assert export_tier_effort_variants(src, tmp_path / "out") == []


# ---------------------------------------------------------------------------
# Generic effort variants + review definitions
# ---------------------------------------------------------------------------


def _base(tmp_path: Path, name: str, extra: str = "") -> Path:
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    path = src / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: A reviewer\ntools: Read, Grep, Glob\n{extra}---\n\nBody text.\n",
        encoding="utf-8",
    )
    return path


def test_export_effort_variant_marks_and_inserts_effort(tmp_path: Path) -> None:
    from shared.agent_export import export_effort_variant

    base = _base(tmp_path, "threnody-review-logic")
    dest = export_effort_variant(base, "high", tmp_path / "out")
    assert dest == tmp_path / "out" / "threnody-review-logic-high.md"
    lines = dest.read_text(encoding="utf-8").split("\n")
    assert "name: threnody-review-logic-high" in lines
    desc_idx = next(i for i, l in enumerate(lines) if l.startswith("description:"))
    assert lines[desc_idx] == "description: A reviewer (effort high)"
    # No model: line, so effort goes right after the description.
    assert lines[desc_idx + 1] == "effort: high"
    assert "tools: Read, Grep, Glob" in lines
    assert "<!-- threnody:effort-variant base=threnody-review-logic effort=high -->" in lines
    assert "Body text." in lines


def test_export_effort_variant_never_overwrites_foreign_file(tmp_path: Path) -> None:
    from shared.agent_export import export_effort_variant

    base = _base(tmp_path, "mine")
    out = tmp_path / "out"
    out.mkdir()
    foreign = out / "mine-low.md"
    foreign.write_text("hand written\n", encoding="utf-8")
    assert export_effort_variant(base, "low", out) is None
    assert foreign.read_text(encoding="utf-8") == "hand written\n"
    # A file this generator wrote is regenerated freely.
    generated = export_effort_variant(base, "medium", out)
    assert generated is not None
    assert export_effort_variant(base, "medium", out) == generated


def test_export_effort_variant_refuses_effort_declaring_base(tmp_path: Path) -> None:
    from shared.agent_export import export_effort_variant

    base = _base(tmp_path, "pinned", extra="effort: low\n")
    assert export_effort_variant(base, "high", tmp_path / "out") is None
    assert not (tmp_path / "out" / "pinned-high.md").exists()
    assert export_effort_variant(_base(tmp_path, "ok"), "extreme", tmp_path / "out") is None


def test_tier_variants_overwrite_unmarked_legacy_files(tmp_path: Path) -> None:
    """Pre-marker installs wrote these without the marker; upgrades must replace them."""
    from shared.agent_export import export_tier_effort_variants

    (tmp_path / "threnody-medium-high.md").write_text("old\n", encoding="utf-8")
    export_tier_effort_variants(ROOT / "shell" / "agents", tmp_path)
    text = (tmp_path / "threnody-medium-high.md").read_text(encoding="utf-8")
    assert "effort: high" in text
    assert "threnody:effort-variant base=threnody-medium effort=high" in text


def _review_target(monkeypatch, tmp_path: Path) -> Path:
    import shared.agent_export as ae

    target = tmp_path / "claude-agents"
    monkeypatch.setitem(
        ae._REVIEW_TARGET_OVERRIDES,
        "claude-code",
        ae.ExportTarget(
            provider_id="claude-code",
            project_subdir=".claude/agents",
            global_dir=target,
            layout="flat_md",
        ),
    )
    return target


def test_export_review_definitions_writes_namespace_fast_and_variants(monkeypatch, tmp_path: Path) -> None:
    from shared.agent_export import export_review_definitions
    from shared.review_fanout import REVIEW_DIMENSIONS

    target = _review_target(monkeypatch, tmp_path)
    result = export_review_definitions(providers=["claude-code"], scope="user")
    assert not result["errors"]
    names = {p.name for p in target.iterdir()}
    for dim in REVIEW_DIMENSIONS:
        assert dim.subagent_type.startswith("threnody-review-")
        assert f"{dim.subagent_type}.md" in names
        for effort in ("low", "medium", "high"):
            assert f"{dim.subagent_type}-{effort}.md" in names
    assert "threnody-review-fast.md" in names
    assert "threnody-review-fast-high.md" in names
    fast = (target / "threnody-review-fast.md").read_text(encoding="utf-8")
    assert "tools: Read, Grep, Glob" in fast
    assert "where dimension is one of security, logic, edge, types, performance" in fast
    variant = (target / "threnody-review-security-medium.md").read_text(encoding="utf-8")
    assert "effort: medium" in variant and "model:" not in variant


def test_agent_md_does_not_count_as_installed(monkeypatch, tmp_path: Path) -> None:
    """A ``.agent.md`` body never loads, so it must not suppress the export."""
    from shared.agent_export import export_review_definitions

    target = _review_target(monkeypatch, tmp_path)
    target.mkdir()
    (target / "threnody-review-logic.agent.md").write_text("mine\n", encoding="utf-8")
    (target / "threnody-review-types.md").write_text(
        "---\nname: threnody-review-types\ndescription: mine\n---\nmy body\n", encoding="utf-8"
    )
    result = export_review_definitions(providers=["claude-code"], scope="user")
    assert (target / "threnody-review-logic.md").is_file()
    # A user's own .md is kept, and its variants mirror *its* body.
    assert (target / "threnody-review-types.md").read_text(encoding="utf-8").endswith("my body\n")
    assert "my body" in (target / "threnody-review-types-high.md").read_text(encoding="utf-8")
    assert any(s.get("dimension") == "types" and "variant" not in s for s in result["skipped"])
