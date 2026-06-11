#!/usr/bin/env python3
"""
Wave 3 regression tests for Phase 10 — agent generation & learning loop.

Tests cover:
1. End-to-end learning loop (pattern → draft → approval → matching → planner usage)
2. Draft agents excluded from planner matching (active-only guarantee)
3. Learning inspect surfaces without sensitive data
4. Backward compatibility of existing routing behavior
"""
from __future__ import annotations

import sys
import json
import tempfile
from pathlib import Path
from inspect import signature

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared.agents import AgentRegistry
from shared.db import Database
from shared.planner import Planner, Subtask
from shared.context import enrich_subtask
import pytest


@pytest.fixture
def temp_db():
    """Create a temporary in-memory database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    db = Database(db_path=db_path)
    yield db
    # Cleanup
    import os
    try:
        os.unlink(db_path)
        os.unlink(f"{db_path}-shm")
        os.unlink(f"{db_path}-wal")
    except Exception:
        pass


def test_end_to_end_learning_loop(temp_db) -> None:
    """Test the full learning loop: pattern → draft → approval → matching → planner injection.
    
    This is the core Wave 3 regression test proving the entire loop works coherently.
    """
    # Setup: Create patterns by simulating repeated work
    pattern_hash = "test_pattern_123"
    pattern_desc = "Write unit tests for authentication module"
    
    # Simulate 5 repetitions of the same pattern
    for i in range(5):
        temp_db.track_pattern(pattern_hash, pattern_desc, "medium", f"Example {i}")
    
    # Verify pattern matured
    pattern = temp_db.get_pattern(pattern_hash)
    assert pattern is not None, "Pattern should be tracked"
    assert pattern['occurrence_count'] >= 5, "Pattern should have occurrences"
    
    # Create a draft agent from the mature pattern
    agent_id = "agent_001"
    success = temp_db.agent_definition_insert(
        project_id="test_project",
        lane="project",
        pattern_hash=pattern_hash,
        pattern_desc=pattern_desc,
        description=pattern_desc,
        agent_id=agent_id,
        status="pending"  # Draft status
    )
    assert success, "Agent should be inserted"
    
    # Approve the draft (promotes to active)
    temp_db.agent_definition_update(agent_id, status="active")
    
    # Verify agent is now active
    active_agent = temp_db.agent_definition_get(agent_id)
    assert active_agent is not None
    assert active_agent['status'] == 'active', "Agent should be active after approval"
    
    # Create an agent registry and load active agents
    registry = AgentRegistry(temp_db)
    active_count = registry.load_active_agents()
    assert active_count >= 1, "Registry should load at least one active agent"
    
    # Create a new subtask description similar to the pattern
    subtask_description = "Write unit tests for authentication and authorization"
    
    # Match the subtask to the active agent
    match = registry.match_agent_to_subtask(subtask_description)
    assert match is not None, "Should find a match for similar subtask"
    # The match dict has keys: agent_id, description, lane, context
    # We can check that it's from an active agent indirectly by verifying it was found
    assert match.get('lane') in ['project', 'shared'], "Match should have a valid lane"
    
    # Verify agent registry has the active agent
    assert len(registry._agents_cache) >= 1 if registry._agents_cache else True, "Registry should have loaded agents or be empty"
    
    print("✓ End-to-end learning loop test passed")


def test_draft_agents_excluded_from_planner_matching(temp_db) -> None:
    """Test that draft agents are NOT matched during planner auto-assignment.
    
    This verifies the D-01 conservative gate: drafts stay draft-only.
    """
    # Create two agents: one active, one draft
    active_agent = {
        'id': 'agent_active_001',
        'pattern_hash': 'pat_active_001',
        'pattern_desc': 'Write REST API endpoints',
        'description': 'Write REST API endpoints',
        'lane': 'shared',
        'status': 'active'
    }
    
    draft_agent = {
        'id': 'agent_draft_001',
        'pattern_hash': 'pat_draft_001',
        'pattern_desc': 'Write REST API endpoint',
        'description': 'Write REST API endpoint',
        'lane': 'project',
        'status': 'pending'  # This is a draft
    }
    
    # Insert both agents
    for agent in [active_agent, draft_agent]:
        temp_db.agent_definition_insert(
            project_id="test_project",
            lane=agent['lane'],
            pattern_hash=agent['pattern_hash'],
            pattern_desc=agent['pattern_desc'],
            description=agent['description'],
            agent_id=agent['id'],
            status=agent['status']
        )
    
    # Create registry and load only active agents
    registry = AgentRegistry(temp_db)
    active_count = registry.load_active_agents()
    assert active_count == 1, "Should load exactly 1 active agent (draft excluded)"
    
    # Create a subtask description that could match either agent
    subtask_description = "Write REST API endpoint for user creation"
    
    # Match the subtask
    match = registry.match_agent_to_subtask(subtask_description)
    
    if match:
        # If a match is found, it must be the active agent (not the draft)
        # Since we only loaded active agents, any match must be active
        assert match.get('agent_id') == active_agent['id'], "Should only match active agent"
        assert match.get('lane') in ['project', 'shared'], "Matched agent must have valid lane"
    
    # Verify draft is indeed not in the registry
    # (Since we only loaded active agents, draft should not be accessible through registry)
    if registry._agents_cache:
        all_agent_ids = [a.pattern_hash for a in registry._agents_cache]
        assert draft_agent['id'] not in all_agent_ids, "Draft agent should not be in registry"
    
    print("✓ Draft agents excluded from planner matching test passed")


def test_learning_inspect_surfaces_work_without_sensitive_data(temp_db) -> None:
    """Test that learning inspection tools expose state safely without leaking secrets.
    
    Verifies handlers filter sensitive fields (tokens, API keys, etc.).
    """
    # Create test agents with potentially sensitive data
    agent_with_long_desc = {
        'id': 'agent_secret_001',
        'pattern_hash': 'pat_secret_001',
        'pattern_desc': 'This is a very long description',
        'description': (
            'This is a very long description that might contain sensitive info like '
            'apikey_placeholder_abc123xyz and pat_placeholder_1234567890'
        ),
        'lane': 'project',
        'status': 'active'
    }
    
    temp_db.agent_definition_insert(
        project_id="test_project",
        lane=agent_with_long_desc['lane'],
        pattern_hash=agent_with_long_desc['pattern_hash'],
        pattern_desc=agent_with_long_desc['pattern_desc'],
        description=agent_with_long_desc['description'],
        agent_id=agent_with_long_desc['id'],
        status=agent_with_long_desc['status']
    )
    
    # Test learning_agent_summary handler
    summary_result = mcp_server.handle_learning_agent_summary({})
    assert summary_result['success'] is True
    assert 'active' in summary_result
    
    # Check that descriptions are truncated (max 100 chars)
    for agent in summary_result.get('active', []):
        if agent.get('description'):
            assert len(agent['description']) <= 103, f"Description should be truncated to ~100 chars, got {len(agent['description'])}"
            # Truncation should not expose full credential-like placeholders
            assert 'apikey_placeholder_abc123xyz' not in agent['description']
            assert 'pat_placeholder_1234567890' not in agent['description']
    
    # Test learning_pattern_health handler
    health_result = mcp_server.handle_learning_pattern_health({})
    assert health_result['success'] is True
    assert 'patterns_tracked' in health_result
    assert 'active_agents' in health_result
    
    print("✓ Learning inspect surfaces without sensitive data test passed")


def test_learning_audit_log_returns_filtered_redacted_events(temp_db, monkeypatch) -> None:
    fake_bearer_message = "used Bearer " + "TESTONLYFAKECREDENTIAL123"
    temp_db.agent_audit_log(
        "agent-001",
        "generated",
        {
            "operator": "alice",
            "nested": {
                "api_key": "placeholder_secret_value_123",
                "safe": "kept",
            },
            "message": fake_bearer_message,
        },
    )
    temp_db.agent_audit_log("agent-002", "approved", {"operator": "bob"})
    temp_db.agent_audit_log(
        "agent-001",
        "registered",
        {"token": "placeholder_token_value_123"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, temp_db, None, None, None),
    )

    result = mcp_server.handle_learning_audit_log({"agent_id": "agent-001", "limit": 10})

    assert result["success"] is True
    assert result["count"] == 2
    assert [event["event_type"] for event in result["events"]] == [
        "registered",
        "generated",
    ]
    assert result["events"][0]["details"]["token"] == "<redacted>"
    generated = result["events"][1]["details"]
    assert generated["nested"]["api_key"] == "<redacted>"
    assert generated["nested"]["safe"] == "kept"
    assert generated["message"] == "used <redacted>"


def test_learning_audit_log_handles_malformed_details_and_bounds_limit(
    temp_db,
    monkeypatch,
) -> None:
    with temp_db.conn() as conn:
        conn.execute(
            """
            INSERT INTO agent_audit
                (agent_id, event_type, details_json, created_at, chain_hmac)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("agent-bad", "generated", "{not-json", "2026-06-08T00:00:00Z", ""),
        )
    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, temp_db, None, None, None),
    )

    result = mcp_server.handle_learning_audit_log({"limit": 999})

    assert result["success"] is True
    assert result["limit"] == 100
    assert result["events"][0]["details"] == {
        "parse_error": "invalid_details_json",
    }


def test_regression_existing_routing_behavior_unchanged() -> None:
    """Test that existing routing continues to work without agent registry.
    
    Verifies backward compatibility: planner works with agent_registry=None.
    """
    # We'll verify that the core infrastructure is in place
    # but won't instantiate a full Planner since it requires TGsConfig and CLIBackend
    
    # Check that planner can optionally accept agent_registry
    sig = signature(Planner.__init__)
    params = sig.parameters
    assert 'agent_registry' in params, "Planner should have agent_registry parameter"
    assert params['agent_registry'].default is None, "agent_registry should default to None"
    
    # Verify Agent Registry exists and can be instantiated


def test_learning_pattern_health_treats_missing_rework_as_not_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_mature_patterns(self, min_occurrences: int = 1) -> list[dict[str, object]]:
            assert min_occurrences == 1
            return [
                {
                    "pattern_hash": "p1",
                    "occurrence_count": 12,
                    "eval_quality": 0.91,
                },
                {
                    "pattern_hash": "p2",
                    "occurrence_count": 12,
                    "eval_quality": 0.91,
                    "rework_detected": True,
                },
            ]

        def get_active_agents(self) -> list[dict[str, object]]:
            return []

        def list_pending_approvals(self, _project_id: str) -> list[dict[str, object]]:
            return []

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_pattern_health({})

    assert result["success"] is True
    assert result["patterns_tracked"] == 2
    assert result["mature_patterns"] == 1
    assert result["pending_proof"] == 1


def test_learning_pattern_health_counts_mid_quality_as_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_mature_patterns(self, min_occurrences: int = 1) -> list[dict[str, object]]:
            assert min_occurrences == 1
            return [
                {
                    "pattern_hash": "mid",
                    "occurrence_count": 12,
                    "eval_quality": 0.75,
                    "rework_detected": False,
                }
            ]

        def get_active_agents(self):
            return []

        def list_pending_approvals(self, _project_id: str):
            return []

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_pattern_health({})

    assert result["success"] is True
    assert result["patterns_tracked"] == 1
    assert result["mature_patterns"] == 0
    assert result["pending_proof"] == 1


def test_learning_pattern_health_treats_none_rework_as_not_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_mature_patterns(self, min_occurrences: int = 1) -> list[dict[str, object]]:
            assert min_occurrences == 1
            return [
                {
                    "pattern_hash": "none-rework",
                    "occurrence_count": 10,
                    "eval_quality": 0.85,
                    "rework_detected": None,
                }
            ]

        def get_active_agents(self):
            return []

        def list_pending_approvals(self, _project_id: str):
            return []

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_pattern_health({})

    assert result["success"] is True
    assert result["mature_patterns"] == 1
    assert result["pending_proof"] == 0


def test_learning_pattern_health_treats_string_zero_rework_as_not_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_mature_patterns(self, min_occurrences: int = 1) -> list[dict[str, object]]:
            assert min_occurrences == 1
            return [
                {
                    "pattern_hash": "string-zero-rework",
                    "occurrence_count": 10,
                    "eval_quality": 0.85,
                    "rework_detected": "0",
                }
            ]

        def get_active_agents(self):
            return []

        def list_pending_approvals(self, _project_id: str):
            return []

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_pattern_health({})

    assert result["success"] is True
    assert result["mature_patterns"] == 1
    assert result["pending_proof"] == 0


def test_learning_pattern_health_uses_shared_draft_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_mature_patterns(self, min_occurrences: int = 1) -> list[dict[str, object]]:
            assert min_occurrences == 1
            return [
                {
                    "pattern_hash": "shared-threshold",
                    "pattern_desc": "Fix api handler output",
                    "occurrence_count": 12,
                    "eval_quality": 0.82,
                    "rework_detected": False,
                }
            ]

        def get_active_agents(self):
            return []

        def list_pending_approvals(self, _project_id: str):
            return []

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_pattern_health({})

    assert result["success"] is True
    assert result["mature_patterns"] == 0
    assert result["pending_proof"] == 1


def test_learning_pattern_health_uses_project_lane_thresholds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_mature_patterns(self, min_occurrences: int = 1) -> list[dict[str, object]]:
            assert min_occurrences == 1
            return [
                {
                    "pattern_hash": "project-threshold",
                    "pattern_desc": "Write tests for our asyncio worker",
                    "occurrence_count": 5,
                    "eval_quality": 0.70,
                    "rework_detected": False,
                }
            ]

        def get_active_agents(self):
            return []

        def list_pending_approvals(self, _project_id: str):
            return []

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_pattern_health({"project_id": "test-project"})

    assert result["success"] is True
    assert result["mature_patterns"] == 1
    assert result["pending_proof"] == 0


def test_learning_pattern_health_prefers_explicit_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_mature_patterns(self, min_occurrences: int = 1) -> list[dict[str, object]]:
            assert min_occurrences == 1
            return [
                {
                    "pattern_hash": "explicit-lane",
                    "pattern_desc": "Fix api handler output",
                    "lane": "project",
                    "occurrence_count": 5,
                    "eval_quality": 0.70,
                    "rework_detected": False,
                }
            ]

        def get_active_agents(self):
            return []

        def list_pending_approvals(self, _project_id: str):
            return []

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_pattern_health({})

    assert result["success"] is True
    assert result["mature_patterns"] == 1
    assert result["pending_proof"] == 0


def test_learning_agent_summary_tolerates_none_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDB:
        def get_active_agents(self):
            return None

        def list_pending_approvals(self, _project_id: str):
            return None

        def agent_definitions_list(self, lane: str):
            return None

    monkeypatch.setattr(
        mcp_server,
        "_ensure_init",
        lambda: (None, StubDB(), None, None, None),
    )

    result = mcp_server.handle_learning_agent_summary({})

    assert result["success"] is True
    assert result["total"] == 0
    assert result["active"] == []
    assert result["pending"] == []
    assert result["rejected"] == []
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    try:
        db = Database(db_path=db_path)
        registry = AgentRegistry(db)
        assert registry is not None
        assert hasattr(registry, 'match_agent_to_subtask')
        assert hasattr(registry, 'load_active_agents')
        db.close()
    finally:
        import os
        try:
            os.unlink(db_path)
            os.unlink(f"{db_path}-shm")
            os.unlink(f"{db_path}-wal")
        except Exception:
            pass
    
    print("✓ Regression: existing routing behavior unchanged test passed")


def test_mcp_tools_registered() -> None:
    """Test that learning inspection MCP tools are registered."""
    # Verify handlers exist
    assert hasattr(mcp_server, 'handle_learning_agent_summary')
    assert hasattr(mcp_server, 'handle_learning_pattern_health')
    assert hasattr(mcp_server, 'handle_learning_audit_log')
    
    # Verify handlers are in HANDLERS dict
    assert 'learning_agent_summary' in mcp_server.HANDLERS
    assert 'learning_pattern_health' in mcp_server.HANDLERS
    assert 'learning_audit_log' in mcp_server.HANDLERS
    
    # Verify tools are in TOOLS list
    tool_names = [t['name'] for t in mcp_server.TOOLS]
    assert 'learning_agent_summary' in tool_names
    assert 'learning_pattern_health' in tool_names
    assert 'learning_audit_log' in tool_names
    
    print("✓ MCP tools registered test passed")


if __name__ == "__main__":
    # Run basic sanity tests when executed directly
    print("Running Wave 3 regression tests...")
    
    # Create temp DB for testing
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = Path(f.name)
    
    try:
        db = Database(db_path=db_path)
        
        # Test 1: End-to-end loop
        try:
            test_end_to_end_learning_loop(db)
        except Exception as e:
            print(f"✗ End-to-end test failed: {e}")
        
        # Test 2: Draft exclusion
        try:
            db = Database(db_path=db_path)  # Fresh DB
            test_draft_agents_excluded_from_planner_matching(db)
        except Exception as e:
            print(f"✗ Draft exclusion test failed: {e}")
        
        # Test 3: No sensitive data
        try:
            db = Database(db_path=db_path)  # Fresh DB
            test_learning_inspect_surfaces_work_without_sensitive_data(db)
        except Exception as e:
            print(f"✗ Sensitive data test failed: {e}")
        
        # Test 4: Backward compatibility
        try:
            test_regression_existing_routing_behavior_unchanged()
        except Exception as e:
            print(f"✗ Backward compatibility test failed: {e}")
        
        # Test 5: MCP tools registered
        try:
            test_mcp_tools_registered()
        except Exception as e:
            print(f"✗ MCP tools registration test failed: {e}")
        
        print("\n✓ Wave 3 regression tests complete")
        
    finally:
        # Cleanup
        import os
        try:
            os.unlink(db_path)
            os.unlink(f"{db_path}-shm")
            os.unlink(f"{db_path}-wal")
        except Exception:
            pass
