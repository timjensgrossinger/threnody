#!/usr/bin/env python3
"""Tests for Phase 4 — Emergent Agent System."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import shared.agents as agents_module
ROOT = Path(__file__).resolve().parent.parent
from shared.agents import (
    AgentDefinition, AgentRegistry, PatternMatch,
    pattern_hash, normalize_pattern, extract_keywords,
    build_creation_prompt, build_dedup_prompt, parse_dedup_response,
    merge_definitions, match_agent, _parse_sections,
    MAX_DEFINITION_LENGTH, build_learned_agent_runtime_context,
    derive_learning_quality, evaluate_pattern_readiness,
    structured_pattern_example,
    find_similar_agents, merge_agent_definitions,
    _similarity_score, _extract_specialist_aspects,
    generate_agent_draft,
)
from shared.config import TGsConfig
from shared.context import enrich_subtask
from shared.db import Database
from shared.planner import Subtask



# ---------------------------------------------------------------------------
# Pattern normalization + hashing
# ---------------------------------------------------------------------------

def test_normalize_strips_file_paths():
    result = normalize_pattern("Add tests for auth.py")
    assert "<file>" in result
    assert "auth.py" not in result


def test_normalize_strips_quoted_strings():
    result = normalize_pattern('Fix the "login" endpoint')
    assert '""' in result
    assert '"login"' not in result


def test_normalize_strips_class_names():
    result = normalize_pattern("Refactor UserController to use dependency injection")
    assert "<class>" in result


def test_pattern_hash_stable_across_files():
    h1 = pattern_hash("Add tests for auth.py")
    h2 = pattern_hash("add tests for users.py")
    assert h1 == h2, f"Hashes should match: {h1} != {h2}"


def test_pattern_hash_differs_for_different_patterns():
    h1 = pattern_hash("Add tests for the module")
    h2 = pattern_hash("Refactor error handling")
    assert h1 != h2


def test_pattern_hash_length():
    h = pattern_hash("some task")
    assert len(h) == 24


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def test_extract_keywords_filters_stopwords():
    kw = extract_keywords("implement the error handling for database connections")
    assert "implement" in kw
    assert "error" in kw
    assert "database" in kw
    assert "the" not in kw
    assert "for" not in kw


def test_extract_keywords_ignores_short_words():
    kw = extract_keywords("a b cd abc def")
    assert "cd" not in kw  # too short (len <= 2)
    assert "abc" in kw
    assert "def" in kw


# ---------------------------------------------------------------------------
# Dedup prompt + parsing
# ---------------------------------------------------------------------------

def test_build_dedup_prompt_contains_both():
    prompt = build_dedup_prompt("Agent A definition", "Agent B definition")
    assert "Agent A definition" in prompt
    assert "Agent B definition" in prompt
    assert "SAME or DIFFERENT" in prompt


def test_build_dedup_prompt_truncates_long_defs():
    long_def = "x" * 2000
    prompt = build_dedup_prompt(long_def, "short")
    assert len(prompt) < 3000


def test_parse_dedup_same():
    assert parse_dedup_response("SAME") is True
    assert parse_dedup_response("same") is True
    assert parse_dedup_response("SAME.") is True
    assert parse_dedup_response("SAME - they are identical") is True


def test_parse_dedup_different():
    assert parse_dedup_response("DIFFERENT") is False
    assert parse_dedup_response("different") is False
    assert parse_dedup_response("DIFFERENT.") is False


def test_parse_dedup_ambiguous_defaults_different():
    assert parse_dedup_response("I'm not sure") is False
    assert parse_dedup_response("") is False


# ---------------------------------------------------------------------------
# Merge definitions
# ---------------------------------------------------------------------------

def test_merge_prefers_quality_backed_sections_and_unions_lists():
    existing = "## Context\nShort but reliable. Quality score: 0.95\n\n## Preferred Checks\n- Check A"
    new_def = "## Context\nMuch longer and more verbose context. Quality score: 0.40\n\n## Preferred Checks\n- Check A\n- Check B"
    merged = merge_definitions(existing, new_def)
    assert "Short but reliable" in merged
    assert "Much longer" not in merged
    assert "Check B" in merged


def test_merge_preserves_claude_frontmatter_and_representative_tasks():
    existing = "---\nname: \"canonical\"\ndescription: \"Good\"\ntools: \"Read\"\nmodel: \"sonnet\"\n---\n\n## Representative tasks\n- Fix auth.py\n"
    new_def = "---\nname: \"duplicate\"\ndescription: \"Noisier\"\ntools: \"Read, Edit\"\nmodel: \"opus\"\n---\n\n## Representative tasks\n- Fix billing.py\n"
    merged = merge_definitions(existing, new_def)
    assert merged.startswith("---\nname: \"canonical\"")
    assert "Fix auth.py" in merged
    assert "Fix billing.py" in merged


def test_merge_preserves_unique_sections():
    existing = "## Context\nContext text.\n\n## Style Notes\nStyle info."
    new_def = "## Context\nContext text.\n\n## Preferred Checks\n- Check A"
    merged = merge_definitions(existing, new_def)
    assert "Style Notes" in merged or "Style info" in merged
    assert "Check A" in merged


def test_merge_truncates_if_too_long():
    existing = "## Context\nQuality score: 0.20\n" + "x" * (MAX_DEFINITION_LENGTH + 100)
    new_def = "## Context\nQuality score: 0.10\n" + "y" * (MAX_DEFINITION_LENGTH + 100)
    merged = merge_definitions(existing, new_def)
    assert len(merged) <= MAX_DEFINITION_LENGTH


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------

def test_parse_sections_basic():
    md = "## Context\nSome context.\n\n## Checks\n- A\n- B"
    sections = _parse_sections(md)
    assert "Context" in sections
    assert "Checks" in sections
    assert "Some context." in sections["Context"]


def test_parse_sections_empty():
    sections = _parse_sections("")
    assert len(sections) <= 1  # May have empty string key


# ---------------------------------------------------------------------------
# AgentDefinition.context_preamble
# ---------------------------------------------------------------------------

def test_context_preamble_extracts_context_section():
    ad = AgentDefinition(
        pattern_hash="abc",
        pattern_desc="test pattern",
        definition="## Context\nThis agent handles testing.\n\n## Checks\n- Verify output",
    )
    preamble = ad.context_preamble
    assert "This agent handles testing" in preamble
    assert "Verify output" not in preamble


def test_context_preamble_fallback_first_paragraph():
    ad = AgentDefinition(
        pattern_hash="abc",
        pattern_desc="test",
        definition="This is a plain text definition without headers.",
    )
    preamble = ad.context_preamble
    assert "plain text definition" in preamble


# ---------------------------------------------------------------------------
# Agent matching
# ---------------------------------------------------------------------------

def test_match_agent_finds_best():
    agents = [
        AgentDefinition("h1", "add error handling database", "## Context\nerror handling"),
        AgentDefinition("h2", "write unit tests module", "## Context\ntesting agent"),
    ]
    result = match_agent("add error handling for the database layer", agents)
    assert result is not None
    assert result.agent.pattern_hash == "h1"
    assert result.score > 0.0


def test_match_agent_respects_min_score():
    agents = [
        AgentDefinition("h1", "completely unrelated topic xyz", "## Context\nxyz"),
    ]
    result = match_agent("add error handling", agents, min_score=0.5)
    assert result is None


def test_match_agent_empty_agents():
    result = match_agent("some task", [])
    assert result is None


def test_match_agent_no_keywords():
    result = match_agent("a b c", [AgentDefinition("h1", "test", "def")])
    assert result is None


# ---------------------------------------------------------------------------
# DB pattern tracking
# ---------------------------------------------------------------------------

def test_db_track_pattern_increments():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        c1 = db.track_pattern("hash1", "pattern desc", "low", "example 1")
        assert c1 == 1
        c2 = db.track_pattern("hash1", "pattern desc", "low", "example 2")
        assert c2 == 2
        pat = db.get_pattern("hash1")
        assert pat is not None
        assert pat["occurrence_count"] == 2
        assert len(pat["examples"]) == 2
        db.close()


def test_db_track_pattern_stores_structured_examples_and_quality():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        example = structured_pattern_example(
            task="Fix auth retry logic",
            tier="medium",
            model="sonnet",
            provider="claude-code",
            touched_files=["shared/auth.py"],
            outcome_summary="completed",
            quality_score=0.9,
        )
        db.track_pattern("hash-structured", "fix auth retry", "medium", example, quality_score=0.9)
        pat = db.get_pattern("hash-structured")
        assert pat is not None
        assert pat["eval_quality"] == 0.9
        assert pat["examples"][0]["task"] == "Fix auth retry logic"
        assert pat["examples"][0]["touched_files"] == ["shared/auth.py"]
        db.close()


def test_derive_learning_quality_penalizes_real_rework_signals():
    clean = derive_learning_quality(success=True, output="done")
    reworked = derive_learning_quality(
        success=True,
        escalated=True,
        rework_count=2,
        used_fallback=True,
        output="done",
    )
    failed = derive_learning_quality(success=False, output="")
    assert clean == 1.0
    assert 0.0 < reworked < clean
    assert failed < reworked


def test_db_get_mature_patterns():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        for i in range(6):
            db.track_pattern("mature", "mature pattern", "low", f"ex {i}")
        for i in range(2):
            db.track_pattern("immature", "young pattern", "low")
        mature = db.get_mature_patterns(min_occurrences=5)
        assert len(mature) == 1
        assert mature[0]["pattern_hash"] == "mature"
        db.close()


def test_db_agent_definition_crud():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        db.save_agent_definition("h1", "desc1", "## Context\nTest agent")
        agent = db.get_agent_definition("h1")
        assert agent is not None
        assert agent["definition"] == "## Context\nTest agent"
        assert agent["match_count"] == 1

        # Update increments match_count
        db.save_agent_definition("h1", "desc1", "## Context\nUpdated agent")
        agent = db.get_agent_definition("h1")
        assert agent["match_count"] == 2
        assert "Updated" in agent["definition"]

        # List all
        all_agents = db.get_all_agent_definitions()
        assert len(all_agents) == 1

        # Delete
        assert db.delete_agent_definition("h1") is True
        assert db.get_agent_definition("h1") is None
        db.close()


def test_db_increment_match_count():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        db.save_agent_definition("h1", "desc1", "def1")
        db.increment_agent_match_count("h1")
        db.increment_agent_match_count("h1")
        agent = db.get_agent_definition("h1")
        assert agent["match_count"] == 3  # 1 from save + 2 increments
        db.close()


# ---------------------------------------------------------------------------
# AgentRegistry (integration)
# ---------------------------------------------------------------------------

def test_registry_track_subtask_counts():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        registry = AgentRegistry(db, emergence_threshold=3)
        c1 = registry.track_subtask("add tests for auth.py", "low")
        assert c1 == 1
        c2 = registry.track_subtask("add tests for users.py", "low")
        # Same normalized pattern, so count should be 2
        assert c2 == 2
        db.close()


def test_registry_creates_agent_at_threshold():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        registry = AgentRegistry(db, emergence_threshold=3)
        # Track same pattern 3 times (no LLM provider → template creation)
        registry.track_subtask("add error handling for auth module", "low")
        registry.track_subtask("add error handling for users module", "low")
        registry.track_subtask("add error handling for payment module", "low")
        # Should have created an agent
        agents = registry.get_agents()
        assert len(agents) == 1
        assert "error" in agents[0].definition.lower() or "handling" in agents[0].definition.lower()
        db.close()


def test_registry_no_duplicate_agent_creation():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        registry = AgentRegistry(db, emergence_threshold=2)
        registry.track_subtask("write unit tests for auth.py", "low")
        registry.track_subtask("write unit tests for users.py", "low")
        # Agent created at threshold=2
        assert len(registry.get_agents()) == 1
        # Track more — should NOT create another
        registry.track_subtask("write unit tests for orders.py", "low")
        assert len(registry.get_agents()) == 1
        db.close()


def test_registry_find_match():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        db.save_agent_definition(
            "test-agent", "add error handling database",
            "## Context\nSpecializes in error handling for database operations."
        )
        registry = AgentRegistry(db)
        result = registry.find_match("add error handling for the database layer")
        assert result is not None
        assert result.agent.pattern_hash == "test-agent"
        db.close()


def test_registry_find_match_no_match():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        registry = AgentRegistry(db)
        result = registry.find_match("completely unrelated task")
        assert result is None
        db.close()


def test_registry_assign_agents_to_plan():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        db.save_agent_definition(
            "err-agent", "add error handling database",
            "## Context\nSpecializes in error handling for database operations.\n\n## Checks\n- Verify retries"
        )
        registry = AgentRegistry(db)
        subtasks = [
            {"id": 1, "description": "add error handling for the database layer", "tier": "low"},
            {"id": 2, "description": "write documentation for the API", "tier": "low"},
        ]
        result = registry.assign_agents_to_plan(subtasks)
        # First subtask should have agent assigned
        assert "Agent:" in result[0]["description"]
        assert "agent_assigned" in result[0]
        # Second subtask should NOT be modified (no matching agent)
        assert "Agent:" not in result[1]["description"]
        db.close()


# ---------------------------------------------------------------------------
# Creation prompt
# ---------------------------------------------------------------------------

def test_creation_prompt_includes_pattern_info():
    pattern = {
        "pattern_desc": "add error handling for <file>",
        "tier": "low",
        "occurrence_count": 7,
        "examples": ["add error handling for auth.py", "add error handling for db.py"],
    }
    prompt = build_creation_prompt(pattern)
    assert "add error handling" in prompt
    assert "low" in prompt
    assert "7" in prompt
    assert "auth.py" in prompt


def test_phase3_draft_lifecycle_scaffold():
    pattern = {
        "pattern_desc": "draft-first agent lifecycle scaffold",
        "tier": "low",
        "occurrence_count": 3,
        "examples": ["draft agent for auth.py"],
    }
    prompt = build_creation_prompt(pattern)
    assert "draft-first agent lifecycle scaffold" in prompt
    assert "draft agent for auth.py" in prompt


def test_generate_agent_draft_creates_draft_record():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            draft = agents_module.generate_agent_draft(
                "project-a",
                {
                    "pattern_hash": "pattern-auth-rollbacks",
                    "pattern_desc": "Handle auth workflow rollback and remediation",
                    "occurrence_count": 7,
                    "eval_quality": 0.93,
                    "tier": "medium",
                    "examples": [
                        "Fix auth rollback wiring after failed login flow refactor",
                        "Repair remediation path for auth rollback when tokens are stale",
                    ],
                },
            )
            assert draft["status"] == "draft"
            assert draft["fingerprint"] == "pattern-auth-rollbacks"
            assert draft["pattern_hash"] == "pattern-auth-rollbacks"
            assert isinstance(draft["id"], str) and draft["id"]
            assert isinstance(draft["fingerprint"], str) and draft["fingerprint"]
            assert draft["export_format"] == "claude-code"
            assert draft["model"] == "sonnet"
            assert "Read" in draft["tools"]
            assert draft["instructions"].startswith("---\n")
            assert 'name: "handle-auth-workflow"' in draft["instructions"]
            assert 'model: "sonnet"' in draft["instructions"]
            assert "## Workflow" in draft["instructions"]
            stored = db.get_agent_definition(draft["fingerprint"])
            assert stored is not None
            stored_payload = json.loads(stored["definition"])
            assert stored_payload["revision"] == 1
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_generate_agent_draft_refreshes_existing_pattern_definition():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            base_candidate = {
                "pattern_hash": "pattern-router-agent-export",
                "pattern_desc": "Export learned Claude agents into the target project",
                "occurrence_count": 11,
                "eval_quality": 0.97,
                "tier": "high",
                "examples": ["Write the approved agent to .claude/agents/exporter.md"],
            }
            first = agents_module.generate_agent_draft("project-a", base_candidate)
            second = agents_module.generate_agent_draft(
                "project-a",
                {
                    **base_candidate,
                    "examples": [
                        *base_candidate["examples"],
                        "Refresh the exporter agent when new successful patterns are observed",
                    ],
                },
            )

            assert second["fingerprint"] == first["fingerprint"]
            assert second["id"] == first["id"]
            assert second["revision"] == 2
            assert len(second["examples"]) == 2

            stored = db.get_agent_definition(first["fingerprint"])
            assert stored is not None
            stored_payload = json.loads(stored["definition"])
            assert stored_payload["revision"] == 2
            assert "Refresh the exporter agent" in stored_payload["instructions"]
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_record_agent_audit_inserts_row():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            audit_id = agents_module.record_agent_audit(
                "agent-1",
                "draft_created",
                {"project_id": "project-a", "status": "draft"},
            )
            assert audit_id > 0
            with db.conn() as conn:
                row = conn.execute(
                    "SELECT agent_id, event_type, details_json FROM agent_audit WHERE id = ?",
                    (audit_id,),
                ).fetchone()
            assert row is not None
            assert row[0] == "agent-1"
            assert row[1] == "draft_created"
            assert "project-a" in row[2]
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_merge_near_duplicate_records_audit():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            db.save_agent_definition("canon", "canonical", "## Context\nCanonical agent")
            db.save_agent_definition("dupe", "duplicate", "## Context\nDuplicate agent")

            result = agents_module.merge_near_duplicate("canon", "dupe", "near-duplicate")
            assert result["canonical_agent"]["pattern_hash"] == "canon"
            assert db.get_agent_definition("dupe") is None
            with db.conn() as conn:
                row = conn.execute(
                    """
                    SELECT canonical_id, merged_from, event_type
                    FROM agent_audit
                    WHERE agent_id = ?
                    ORDER BY id DESC
                    """,
                    ("canon",),
                ).fetchone()
            assert row is not None
            assert row[0] == "canon"
            assert row[1] == "dupe"
            assert row[2] == "merged"
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_draft_agents_are_not_auto_assigned():
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            draft = agents_module.generate_agent_draft(
                "project-a",
                {
                    "name": "draft-agent",
                    "instructions": "## Context\nHandle auth workflow rollback.",
                },
            )
            stored = db.get_agent_definition(draft["fingerprint"])
            assert stored is not None
            assert stored["promotion_state"] == "draft"

            registry = AgentRegistry(db)
            assert registry.find_match("handle auth workflow rollback task") is None
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_test_approval_queue_approve():
    """D-05..D-08: queued drafts must support explicit approval and audit."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            draft = agents_module.generate_agent_draft(
                "project-a",
                {"name": "approve-agent", "instructions": "## Context\nApprove me."},
            )
            queued = agents_module.approval_queue_enqueue("project-a", draft)
            pending = agents_module.approval_queue_list("project-a")
            assert len(pending) == 1

            result = agents_module.approval_queue_approve(
                "project-a",
                queued["id"],
                operator_id="operator-1",
            )
            assert result["approved"] is True
            assert result["operator_id"] == "operator-1"
            stored = db.get_agent_definition(draft["fingerprint"])
            assert stored is not None
            assert stored["promotion_state"] == "active"
            assert stored["pattern_hash"] == draft["fingerprint"]

            with db.conn() as conn:
                queue_row = conn.execute(
                    "SELECT status FROM approval_queue WHERE id = ?",
                    (queued["id"],),
                ).fetchone()
                audit_row = conn.execute(
                    """
                    SELECT event_type, details_json
                    FROM agent_audit
                    WHERE agent_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (draft["fingerprint"],),
                ).fetchone()
            assert queue_row == ("approved",)
            assert audit_row is not None
            assert audit_row[0] == "approval_approved"
            assert '"operator_id": "operator-1"' in audit_row[1]
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_evaluate_pattern_readiness_project_and_shared_rules():
    project_ready = evaluate_pattern_readiness(
        {
            "pattern_hash": "project-ready",
            "pattern_desc": "Fix this repo auth workflow",
            "occurrence_count": 5,
            "eval_quality": 0.70,
            "rework_detected": False,
        },
        "project-a",
    )
    shared_not_ready = evaluate_pattern_readiness(
        {
            "pattern_hash": "shared-low",
            "pattern_desc": "Write API tests",
            "occurrence_count": 5,
            "eval_quality": 0.84,
            "rework_detected": False,
        },
        "project-a",
    )
    assert project_ready["ready"] is True
    assert project_ready["lane"] == "project"
    assert shared_not_ready["ready"] is False
    assert shared_not_ready["recurrence_threshold"] == 10


def test_non_native_provider_receives_learned_agent_runtime_context():
    context = build_learned_agent_runtime_context(
        {
            "pattern_hash": "runtime-pattern",
            "pattern_desc": "Fix retry handling",
            "lane": "shared",
            "definition": "## Context\nUse the retry helper and preserve explicit errors.",
            "examples": [
                {
                    "task": "Fix retry handling in shared/client.py",
                    "touched_files": ["shared/client.py"],
                    "outcome_summary": "completed",
                }
            ],
        }
    )
    subtask = Subtask(
        id=1,
        description="Fix retry handling in shared/client.py",
        tier="low",
        provider_id="github-copilot",
        agent_context=context,
    )
    enriched = enrich_subtask(subtask, project_root=str(ROOT))
    assert "Learned Agent Context" in enriched.description
    assert "runtime-pattern" in enriched.description
    assert "Use the retry helper" in enriched.description


def test_test_approval_queue_reject():
    """D-05..D-08: queued drafts must support reject/defer without auto-promotion."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            draft = agents_module.generate_agent_draft(
                "project-a",
                {"name": "reject-agent", "instructions": "## Context\nReject me."},
            )
            queued = agents_module.approval_queue_enqueue("project-a", draft)

            result = agents_module.approval_queue_reject(
                "project-a",
                queued["id"],
                operator_id="operator-2",
                reason="needs-more-evidence",
            )
            assert result["rejected"] is True
            assert result["operator_id"] == "operator-2"

            with db.conn() as conn:
                queue_row = conn.execute(
                    "SELECT status, review_note FROM approval_queue WHERE id = ?",
                    (queued["id"],),
                ).fetchone()
                audit_row = conn.execute(
                    """
                    SELECT event_type, details_json
                    FROM agent_audit
                    WHERE agent_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (draft["fingerprint"],),
                ).fetchone()
            assert queue_row == ("rejected", "needs-more-evidence")
            assert audit_row is not None
            assert audit_row[0] == "approval_rejected"
            assert '"operator_id": "operator-2"' in audit_row[1]
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_test_approval_queue_merge():
    """D-06..D-08: near-duplicate drafts must merge into the canonical chain."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            db.save_agent_definition("canon", "canonical", "## Context\nCanonical agent")
            draft = agents_module.generate_agent_draft(
                "project-a",
                {"name": "merge-agent", "instructions": "## Context\nMerge me."},
            )
            queued = agents_module.approval_queue_enqueue("project-a", draft)

            result = agents_module.approval_queue_merge(
                "project-a",
                queued["id"],
                "canon",
                operator_id="operator-3",
                reason="near-duplicate",
            )
            assert result["merged"] is True
            assert result["operator_id"] == "operator-3"
            assert db.get_agent_definition(draft["fingerprint"]) is None

            with db.conn() as conn:
                queue_row = conn.execute(
                    "SELECT status, canonical_id FROM approval_queue WHERE id = ?",
                    (queued["id"],),
                ).fetchone()
                audit_row = conn.execute(
                    """
                    SELECT event_type, details_json
                    FROM agent_audit
                    WHERE agent_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (draft["fingerprint"],),
                ).fetchone()
                merge_row = conn.execute(
                    """
                    SELECT event_type, canonical_id, merged_from
                    FROM agent_audit
                    WHERE agent_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    ("canon",),
                ).fetchone()
            assert queue_row == ("merged", "canon")
            assert audit_row is not None
            assert audit_row[0] == "approval_merged"
            assert '"operator_id": "operator-3"' in audit_row[1]
            assert merge_row is not None
            assert merge_row[0] == "merged"
            assert merge_row[1] == "canon"
            assert merge_row[2] == draft["fingerprint"]
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


def test_test_approval_queue_rate_limit():
    """D-06: each project enforces a pending-approval cap before more queueing."""
    with tempfile.TemporaryDirectory() as td:
        db = Database(db_path=Path(td) / "test.db")
        agents_module._DEFAULT_DB = db
        try:
            first = agents_module.generate_agent_draft(
                "project-a",
                {"name": "limit-a", "instructions": "## Context\nOne."},
            )
            second = agents_module.generate_agent_draft(
                "project-a",
                {"name": "limit-b", "instructions": "## Context\nTwo."},
            )
            third = agents_module.generate_agent_draft(
                "project-a",
                {"name": "limit-c", "instructions": "## Context\nThree."},
            )

            with db.conn() as conn:
                conn.execute(
                    """
                    INSERT INTO project_settings
                        (project_path, concurrency_limit, budget_hard_cap_tokens,
                         fanout_cap, pending_approval_limit, ts)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    ("project-a", 8, 1000, 3, 2),
                )

            agents_module.approval_queue_enqueue("project-a", first)
            agents_module.approval_queue_enqueue("project-a", second)

            try:
                agents_module.approval_queue_enqueue("project-a", third)
            except ValueError as exc:
                assert "pending approval limit exceeded" in str(exc)
            else:
                raise AssertionError("expected approval queue limit enforcement")
        finally:
            agents_module._DEFAULT_DB = None
            db.close()


# --- dedup / similarity ---


def test_find_similar_agents_high_threshold():
    """similar A and B returned, C not returned"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        db.agent_definition_insert(
            project_id=None,
            lane="shared",
            pattern_hash="hash_a",
            pattern_desc="Test writer for async code",
            description="Test writer for async code",
            agent_id="agent_a",
            status="approved",
        )
        db.agent_definition_insert(
            project_id=None,
            lane="shared",
            pattern_hash="hash_b",
            pattern_desc="Test writer for async error handling",
            description="Test writer for async error handling",
            agent_id="agent_b",
            status="approved",
        )
        db.agent_definition_insert(
            project_id=None,
            lane="shared",
            pattern_hash="hash_c",
            pattern_desc="Documentation generator",
            description="Documentation generator",
            agent_id="agent_c",
            status="approved",
        )

        result = find_similar_agents(
            "Test writer for async code",
            lane="shared",
            db=db,
            project_id=None,
        )

        assert len(result) >= 1, f"Expected >= 1 result, got {len(result)}: {result}"
        agent_ids = [r["agent_id"] for r in result]
        assert "agent_a" in agent_ids, f"Expected agent_a in results, got {agent_ids}"
        assert "agent_c" not in agent_ids, f"Expected agent_c NOT in results, got {agent_ids}"


def test_conservative_merge_keeps_specialization():
    """merge preserves both aspects"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        db.agent_definition_insert(
            project_id=None,
            lane="shared",
            pattern_hash="hash_a",
            pattern_desc="Test writer for async code patterns",
            description="Test writer for async code patterns",
            agent_id="agent_a",
            status="approved",
        )
        db.agent_definition_insert(
            project_id=None,
            lane="shared",
            pattern_hash="hash_b",
            pattern_desc="Test writer for exception handling",
            description="Test writer for exception handling",
            agent_id="agent_b",
            status="pending",
        )

        result = merge_agent_definitions("agent_a", "agent_b", db)
        assert result is True, f"Expected merge to succeed, got {result}"

        canonical = db.agent_definition_get("agent_a")
        assert canonical is not None, "Canonical agent not found after merge"
        assert "async" in canonical["description"].lower(), \
            f"Async specialization not preserved: {canonical['description']}"
        assert "exception" in canonical["description"].lower(), \
            f"Exception handling specialization not preserved: {canonical['description']}"
        assert canonical["status"] == "approved", \
            f"Expected canonical status 'approved', got '{canonical['status']}'"

        merge_from = db.agent_definition_get("agent_b")
        assert merge_from is not None, "Merge-from agent not found after merge"
        assert merge_from["status"] == "merged_into", \
            f"Expected merge_from status 'merged_into', got '{merge_from['status']}'"

        description = canonical["description"].lower()
        assert "also handles" in description or "exception" in description, \
            f"Specializations appear to be flattened: {canonical['description']}"

        db.close()


def test_low_similarity_does_not_suggest_merge():
    """returns empty list when similarity is below threshold"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db = Database(db_path)

        db.agent_definition_insert(
            project_id=None,
            lane="shared",
            pattern_hash="hash_a",
            pattern_desc="Test writer for async patterns",
            description="Test writer for async patterns",
            agent_id="agent_a",
            status="approved",
        )

        result = find_similar_agents(
            "Documentation generator",
            lane="shared",
            db=db,
            project_id=None,
        )

        assert len(result) == 0, f"Expected no results for low similarity, got {len(result)}"
        db.close()


def test_similarity_score_computation():
    """_similarity_score() computes Jaccard similarity correctly"""
    # Identical text
    score = _similarity_score("test writer", "test writer")
    assert score == 1.0, f"Expected 1.0 for identical text, got {score}"

    # 2-token intersection out of 4-token union → Jaccard ~0.5
    score = _similarity_score("test writer async", "test writer exception")
    assert 0.4 < score <= 0.6, f"Expected 0.4 < score <= 0.6 for 2/4 overlap, got {score}"

    # No overlap
    score = _similarity_score("test", "documentation")
    assert score == 0.0 or score < 0.2, f"Expected ~0 for no overlap, got {score}"

    # High overlap — 3 common out of 5 union → Jaccard ~0.6
    score = _similarity_score("test writer async code", "test writer async patterns")
    assert 0.5 < score < 0.8, f"Expected 0.5 < score < 0.8 for high overlap, got {score}"


def test_extract_specialist_aspects():
    """_extract_specialist_aspects() extracts keywords correctly"""
    # Single aspect
    aspects = _extract_specialist_aspects("Test writer for async patterns")
    assert "async patterns" in aspects or "async" in aspects[0].lower(), \
        f"Expected 'async patterns' or 'async' in {aspects}"

    # Multiple aspects
    aspects = _extract_specialist_aspects("Test writer for async patterns and exception handling")
    assert len(aspects) >= 2, f"Expected >= 2 aspects, got {len(aspects)}"
    assert any("async" in a.lower() for a in aspects), f"Expected 'async' in {aspects}"
    assert any("exception" in a.lower() for a in aspects), f"Expected 'exception' in {aspects}"

    # No "for" pattern — generic description yields no aspects
    aspects = _extract_specialist_aspects("Generic test writer")
    assert len(aspects) == 0, f"Expected no aspects for generic pattern, got {aspects}"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cost lane agent drafting (from test_cost_lane_agents.py)
# ---------------------------------------------------------------------------

def test_cost_lane_readiness_for_low_tier_pattern() -> None:
    pattern = {
        "pattern_desc": "Write tests for async helper",
        "occurrence_count": 6,
        "tier": "low",
        "rework_detected": False,
        "eval_quality": 0.82,
    }
    readiness = evaluate_pattern_readiness(pattern, "demo-project")
    assert readiness["lane"] == "cost_lane"
    assert readiness["ready"] is True


def test_generate_agent_draft_cost_lane_metadata(tmp_path) -> None:
    db = Database(tmp_path / "cost-lane.db")
    candidate = {
        "pattern_hash": "abc123",
        "description": "Refactor small utility with low-tier routing",
        "tier": "low",
        "occurrence_count": 7,
        "rework_detected": False,
        "eval_quality": 0.9,
    }
    draft = generate_agent_draft("demo-project", candidate, db=db)
    assert draft["lane"] == "cost_lane"
    assert draft["cost_lane"] is True
    assert draft["preferred_tier"] == "low"
    assert draft["prefer_free"] is True
    assert draft["model"] == "haiku"
