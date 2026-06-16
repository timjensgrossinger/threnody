#!/usr/bin/env python3
"""
Tests for shared/db.py — SQLite database layer.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.db import Database
from shared.memory import (
    MemoryNotFoundError,
    MemoryRequestError,
    memory_delete,
    memory_get,
    memory_list,
    memory_set,
)


def test_cache_put_and_get() -> None:
    """Basic cache round-trip."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        db.cache_put("test task", "test result", "gpt-5-mini")
        hit = db.cache_get("test task")
        assert hit is not None
        result, model = hit
        assert result == "test result"
        assert model == "gpt-5-mini"
        db.close()


def test_cache_miss() -> None:
    """Non-existent key returns None."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        assert db.cache_get("nonexistent") is None
        db.close()


def test_plan_cache() -> None:
    """Plan cache round-trip."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        plan = {"subtasks": [{"id": 1, "description": "test", "tier": "low"}]}
        db.plan_put("write tests for auth", plan, "sonnet")
        cached = db.plan_get("write tests for auth")
        assert cached is not None
        assert cached["subtasks"][0]["tier"] == "low"
        db.close()


def test_plan_cache_structural_hash() -> None:
    """Similar tasks should match via structural hash."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        plan = {"subtasks": [{"id": 1, "description": "test", "tier": "low"}]}
        db.plan_put("write tests for auth.py", plan, "sonnet")
        # Different file name but same pattern
        cached = db.plan_get("write tests for users.py")
        # Structural hash strips file names, so these should match
        assert cached is not None
        db.close()


def test_plan_cache_rejects_non_serializable_payloads() -> None:
    """Non-JSON plan payloads should fail with a clear error."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        try:
            db.plan_put(
                "write tests for auth",
                {"subtasks": [{"id": 1, "description": object(), "tier": "low"}]},
                "sonnet",
            )
        except TypeError as exc:
            assert str(exc) == "plan must be JSON-serializable"
        else:
            raise AssertionError("Expected TypeError for non-serializable plan payload")
        finally:
            db.close()


def test_plan_lookup_reports_miss() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        lookup = db.plan_lookup("missing task")
        assert lookup.status == "miss"
        assert lookup.plan is None
        db.close()


def test_plan_lookup_expired_entry() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name), plan_ttl_hours=0)
        plan = {"subtasks": [{"id": 1, "description": "test", "tier": "low"}]}
        db.plan_put("expired task", plan, "sonnet")
        lookup = db.plan_lookup("expired task")
        assert lookup.status == "expired"
        assert db.plan_get("expired task") is None
        db.close()


def test_plan_lookup_invalidates_stale_schema_version(monkeypatch) -> None:
    import json
    import time

    monkeypatch.setattr("shared.db.CURRENT_PLAN_SCHEMA_VERSION", 2)
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        plan = {"subtasks": [{"id": 1, "description": "test", "tier": "low"}]}
        key = db._plan_key("stale schema task")
        with db.conn() as conn:
            conn.execute(
                "INSERT INTO plan_cache "
                "(key, task_hash, plan_json, model, plan_schema_version, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, db._key("stale schema task"), json.dumps(plan), "sonnet", 1, time.time()),
            )
        lookup = db.plan_lookup("stale schema task")
        assert lookup.status == "schema_invalid"
        assert lookup.plan_schema_version == 1
        assert db.plan_get("stale schema task") is None
        db.close()


def test_plan_put_stores_current_schema_version() -> None:
    from shared.config import CURRENT_PLAN_SCHEMA_VERSION

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        plan = {"subtasks": [{"id": 1, "description": "test", "tier": "low"}]}
        db.plan_put("schema task", plan, "sonnet")
        key = db._plan_key("schema task")
        with db.conn() as conn:
            row = conn.execute(
                "SELECT plan_schema_version FROM plan_cache WHERE key = ?",
                (key,),
            ).fetchone()
        assert row is not None
        assert int(row[0]) == CURRENT_PLAN_SCHEMA_VERSION
        db.close()


def test_artifact_persistence() -> None:
    """Artifacts should round-trip with scoped lookup and compact envelopes."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        first_ref = db.save_artifact(
            execution_id="exec-1",
            plan_revision=1,
            wave=1,
            subtask_id="12-01",
            artifact_type="summary",
            full_payload="raw artifact payload",
            compact_summary="compact summary",
        )
        db.save_artifact(
            execution_id="exec-1",
            plan_revision=2,
            wave=2,
            subtask_id="12-01",
            artifact_type="summary",
            full_payload="newer payload",
            compact_summary={"summary_text": "new summary", "length_chars": 11},
        )

        scoped = db.query_artifacts("exec-1", 1)
        assert len(scoped) == 1
        assert scoped[0]["stable_ref"] == first_ref
        assert scoped[0]["artifact_type"] == "summary"
        assert scoped[0]["compact_summary"] == {
            "summary_text": "compact summary",
            "length_chars": len("compact summary"),
            "artifact_ref": first_ref,
        }
        assert db._get_full_payload(first_ref) == "raw artifact payload"

        other_scope = db.query_artifacts("exec-1", 2, wave=2, artifact_types=["summary"])
        assert len(other_scope) == 1
        assert other_scope[0]["compact_summary"] == {
            "summary_text": "new summary",
            "length_chars": 11,
            "artifact_ref": other_scope[0]["stable_ref"],
        }

        consumes = db.get_artifacts_for_consumes(
            "exec-1",
            2,
            ["summary"],
            upto_wave=2,
        )
        assert consumes == [
            {
                "artifact_type": "summary",
                "summary_text": "new summary",
                "length_chars": 11,
                "artifact_ref": other_scope[0]["stable_ref"],
            }
        ]
        db.close()


def test_routing_outcomes_schema() -> None:
    """Outcome persistence tables should be created during schema initialization."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        with db.conn() as conn:
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        assert "routing_outcomes" in table_names
        assert "routing_outcome_audit" in table_names
        db.close()


def test_cache_stats() -> None:
    """Stats should count entries."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        db.cache_put("task1", "r1", "gpt-5-mini")
        db.cache_put("task2", "r2", "sonnet")
        stats = db.cache_stats()
        assert stats["total_cached"] == 2
        assert "gpt-5-mini" in stats["by_model"]
        db.close()


def test_escalation_logging() -> None:
    """Escalation events should be recorded."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        db.log_escalation("hash1", 1, "low", "medium", 2000, 1500)
        with db.conn() as conn:
            rows = conn.execute("SELECT * FROM escalations").fetchall()
        assert len(rows) == 1
        db.close()


def test_concurrent_db_writes_via_threads() -> None:
    """
    Validate concurrent write behavior with thread-local connections.
    
    This test validates FNDX-01 requirement: "Each worker thread receives an
    isolated SQLite connection" and "Concurrent DB writes from multiple threads
    never raise ProgrammingError".
    
    Test steps:
    1. Creates 5 concurrent write tasks using threading
    2. Each task calls `with db.conn() as conn:` and executes INSERT into telemetry
    3. Each task writes a unique record (different session_id)
    4. Verifies no ProgrammingError is raised during concurrent execution
    5. Verifies all 5 records are inserted (SELECT count(*) from telemetry returns 5)
    6. Verifies each record is readable from the main thread afterward
    
    Expected: PASS — thread-local connections prevent cross-thread access errors
    """
    import threading
    import time
    
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        
        results: dict[int, str] = {}
        errors: list[str] = []
        
        def worker_insert_task(task_id: int) -> None:
            """Worker task: insert telemetry record from thread."""
            try:
                # Simulate some work before insert
                time.sleep(0.01)
                
                # This should use thread-local connection automatically
                with db.conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO telemetry (session_id, tier, model, provider_name, ts)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (f"concurrent-session-{task_id}", "low", "test-model", "test-provider", time.time()),
                    )
                results[task_id] = "success"
            except Exception as e:
                errors.append(f"Task {task_id} error: {e}")
                results[task_id] = "failed"
        
        # Spawn 5 threads concurrently
        threads = []
        for i in range(5):
            t = threading.Thread(target=worker_insert_task, args=(i,))
            threads.append(t)
            t.start()
        
        # Wait for all threads to complete
        for t in threads:
            t.join(timeout=10)
        
        # Verify no errors occurred during concurrent execution
        assert not errors, f"Concurrent write errors: {errors}"
        
        # Verify all 5 threads succeeded
        assert len(results) == 5, f"Expected 5 thread results, got {len(results)}"
        assert all(v == "success" for v in results.values()), \
            f"Not all threads succeeded: {results}"
        
        # Verify main thread can read all 5 inserted rows
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM telemetry WHERE session_id LIKE 'concurrent-session-%'"
            ).fetchone()
            count = rows[0] if rows else 0
        
        assert count == 5, f"Expected 5 telemetry records, found {count}"
        
        # Verify records are readable with correct data
        with db.conn() as conn:
            records = conn.execute(
                "SELECT session_id, model FROM telemetry WHERE session_id LIKE 'concurrent-session-%' ORDER BY session_id"
            ).fetchall()
        
        assert len(records) == 5, f"Expected 5 readable records, got {len(records)}"
        for i, (session_id, model) in enumerate(records):
            assert session_id == f"concurrent-session-{i}", \
                f"Record {i}: expected session_id 'concurrent-session-{i}', got '{session_id}'"
            assert model == "test-model", \
                f"Record {i}: expected model 'test-model', got '{model}'"
        
        db.close()


def test_memory_set_get_list_delete() -> None:
    """Memory helpers should round-trip values per explicit scope."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))

        global_item = memory_set("global", "release-note", "hello", db=db)
        project_item = memory_set(
            "project",
            "release-note",
            7,
            project_id="project-a",
            db=db,
        )
        task_item = memory_set(
            "task",
            "release-note",
            True,
            project_id="project-a",
            task_id="task-1",
            db=db,
        )

        assert global_item["scope"] == "global"
        assert global_item["project_id"] is None
        assert global_item["task_id"] is None
        assert global_item["value"] == "hello"
        assert global_item["value_type"] == "string"
        assert isinstance(global_item["updated_at"], float)

        assert project_item["scope"] == "project"
        assert project_item["project_id"] == "project-a"
        assert project_item["task_id"] is None
        assert project_item["value"] == 7
        assert project_item["value_type"] == "number"

        assert task_item["scope"] == "task"
        assert task_item["project_id"] == "project-a"
        assert task_item["task_id"] == "task-1"
        assert task_item["value"] is True
        assert task_item["value_type"] == "bool"

        assert memory_get("global", "release-note", db=db)["value"] == "hello"
        assert memory_get(
            "project",
            "release-note",
            project_id="project-a",
            db=db,
        )["value"] == 7
        assert memory_get(
            "task",
            "release-note",
            project_id="project-a",
            task_id="task-1",
            db=db,
        )["value"] is True

        global_list = memory_list("global", db=db)
        assert global_list == [{
            "key": "release-note",
            "scope": "global",
            "updated_at": global_item["updated_at"],
            "value_type": "string",
            "value_size": len('"hello"'.encode("utf-8")),
        }]

        project_list = memory_list("project", project_id="project-a", db=db)
        assert [item["key"] for item in project_list] == ["release-note"]
        assert all("value" not in item for item in project_list)

        task_list = memory_list(
            "task",
            project_id="project-a",
            task_id="task-1",
            db=db,
        )
        assert [item["key"] for item in task_list] == ["release-note"]

        deleted = memory_delete(
            "task",
            "release-note",
            project_id="project-a",
            task_id="task-1",
            db=db,
        )
        assert deleted == {"deleted": True}
        assert memory_list("task", project_id="project-a", task_id="task-1", db=db) == []

        try:
            memory_get(
                "task",
                "release-note",
                project_id="project-a",
                task_id="task-1",
                db=db,
            )
        except MemoryNotFoundError as exc:
            assert "not found" in str(exc)
        else:
            raise AssertionError("Expected task memory get to raise MemoryNotFoundError")

        db.close()


def test_memory_overwrite_json() -> None:
    """Memory writes should overwrite in-place and preserve structured JSON."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))

        first = memory_set(
            "project",
            "settings",
            {"enabled": True, "retries": 1},
            project_id="project-b",
            db=db,
        )
        time.sleep(0.01)
        second = memory_set(
            "project",
            "settings",
            {"enabled": False, "retries": 2},
            project_id="project-b",
            db=db,
        )
        fetched = memory_get("project", "settings", project_id="project-b", db=db)

        assert second["updated_at"] > first["updated_at"]
        assert fetched["value"] == {"enabled": False, "retries": 2}
        assert fetched["value_type"] == "object"

        listed = memory_list("project", project_id="project-b", db=db)
        assert listed == [{
            "key": "settings",
            "scope": "project",
            "updated_at": fetched["updated_at"],
            "value_type": "object",
            "value_size": len('{"enabled":false,"retries":2}'.encode("utf-8")),
        }]

        db.close()


def test_memory_validation_and_corrupted_value() -> None:
    """Memory helpers should reject oversized task identifiers and corrupted rows."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))

        try:
            memory_set(
                "task",
                "settings",
                "hello",
                project_id="project-c",
                task_id="t" * 257,
                db=db,
            )
        except MemoryRequestError as exc:
            assert str(exc) == "task_id must be <= 256 characters"
        else:
            raise AssertionError("Expected oversized task_id to raise MemoryRequestError")

        try:
            memory_set("global", "nan", float("nan"), db=db)
        except MemoryRequestError as exc:
            assert str(exc) == "number values must be finite"
        else:
            raise AssertionError("Expected non-finite value to raise MemoryRequestError")

        with db.conn() as conn:
            conn.execute(
                """
                INSERT INTO memory (
                    scope, project_id, task_id, key, value_type, value_json, value_size, updated_at
                )
                VALUES ('global', '', '', 'corrupt', 'object', '{', 1, ?)
                """,
                (time.time(),),
            )

        try:
            memory_get("global", "corrupt", db=db)
        except MemoryRequestError as exc:
            assert str(exc) == "stored memory value is corrupted"
        else:
            raise AssertionError("Expected corrupted payload to raise MemoryRequestError")

        db.close()



# ---------------------------------------------------------------------------
# Coordinator audit persistence (from test_coordinator_audit_persistence.py)
# ---------------------------------------------------------------------------

import json


def test_accepted_amendment_persists_revision_and_audit() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
        db = Database(Path(db_file.name))
        revision_id = db.insert_plan_revision(
            plan_id="13-03",
            revision_number=2,
            diff_blob={"updated_subtasks": ["13-03-02"]},
            proposer_id="coordinator-1",
            reason="tighten future validation",
        )
        audit_id = db.insert_coordinator_audit(revision_id, outcome="accepted")

        with db.conn() as conn:
            revision = conn.execute(
                """
                SELECT plan_id, revision_number, diff_blob, proposer_id, reason
                FROM plan_revisions
                WHERE id = ?
                """,
                (revision_id,),
            ).fetchone()
            audit = conn.execute(
                """
                SELECT plan_id, revision_id, proposer_id, diff_blob, reason, outcome, rejection_reason
                FROM coordinator_amendments
                WHERE id = ?
                """,
                (audit_id,),
            ).fetchone()

        assert revision is not None
        assert revision[0] == "13-03"
        assert revision[1] == 2
        assert json.loads(revision[2]) == {"updated_subtasks": ["13-03-02"]}
        assert revision[3] == "coordinator-1"
        assert revision[4] == "tighten future validation"

        assert audit is not None
        assert audit[0] == "13-03"
        assert audit[1] == revision_id
        assert audit[2] == "coordinator-1"
        assert json.loads(audit[3]) == {"updated_subtasks": ["13-03-02"]}
        assert audit[4] == "tighten future validation"
        assert audit[5] == "accepted"
        assert audit[6] is None

        db.close()


def test_rejected_amendment_persists_audit_error() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as db_file:
        db = Database(Path(db_file.name))
        audit_id = db.insert_coordinator_audit_rejection(
            plan_id="13-03",
            proposer_id="coordinator-2",
            reason="attempted duplicate coordinator in wave 2",
            diff_blob={"wave": 2, "coordinator_ids": [4, 5]},
        )

        with db.conn() as conn:
            audit = conn.execute(
                """
                SELECT plan_id, revision_id, proposer_id, diff_blob, reason, outcome, rejection_reason
                FROM coordinator_amendments
                WHERE id = ?
                """,
                (audit_id,),
            ).fetchone()

        assert audit is not None
        assert audit[0] == "13-03"
        assert audit[1] is None
        assert audit[2] == "coordinator-2"
        assert json.loads(audit[3]) == {"wave": 2, "coordinator_ids": [4, 5]}
        assert audit[4] == "attempted duplicate coordinator in wave 2"
        assert audit[5] == "rejected"
        assert audit[6] == "attempted duplicate coordinator in wave 2"

        db.close()


# ---------------------------------------------------------------------------
# File permission hardening (from test_db_permissions.py)
# ---------------------------------------------------------------------------


def test_database_restricts_permissions_to_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "private-router.db"
    db = Database(db_path=db_path)
    try:
        assert db_path.parent.stat().st_mode & 0o777 == 0o700
        conn = db._connect()
        conn.execute("CREATE TABLE IF NOT EXISTS perms_probe (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        for candidate in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        ):
            if candidate.exists():
                assert candidate.stat().st_mode & 0o777 == 0o600
    finally:
        db.close()


def test_database_restricts_custom_owned_parent_directory(tmp_path: Path) -> None:
    custom_parent = tmp_path / "custom-db-dir"
    custom_parent.mkdir(mode=0o755)
    db_path = custom_parent / "router.db"
    db = Database(db_path=db_path)
    try:
        assert custom_parent.stat().st_mode & 0o777 == 0o700
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Artifact visibility scoping (from test_db_concurrency.py — unique test)
# ---------------------------------------------------------------------------

import shared.db as _shared_db


def test_artifact_visibility_scoping() -> None:
    """Artifacts should be visible across DB instances and scoped by revision and wave."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "artifacts.db"
        first = _shared_db.Database(db_path)
        second = _shared_db.Database(db_path)
        try:
            first_ref = first.save_artifact(
                execution_id="exec-1",
                plan_revision=1,
                wave=1,
                subtask_id="12-01",
                artifact_type="summary",
                full_payload="payload-one",
                compact_summary="summary-one",
            )
            second_ref = first.save_artifact(
                execution_id="exec-1",
                plan_revision=2,
                wave=2,
                subtask_id="12-01",
                artifact_type="summary",
                full_payload="payload-two",
                compact_summary="summary-two",
            )

            visible = second.query_artifacts("exec-1", 1, wave=1, artifact_types=["summary"])
            assert len(visible) == 1
            assert visible[0]["stable_ref"] == first_ref
            assert visible[0]["compact_summary"]["artifact_ref"] == first_ref

            hidden = second.query_artifacts("exec-1", 1, wave=2, artifact_types=["summary"])
            assert hidden == []

            other_revision = second.query_artifacts("exec-1", 2, wave=2, artifact_types=["summary"])
            assert len(other_revision) == 1
            assert other_revision[0]["stable_ref"] == second_ref
            assert first._get_full_payload(second_ref) == "payload-two"
        finally:
            first.close()
            second.close()


def _count(db, table: str) -> int:
    with db.conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_flush_host_wave_records_single_transaction_all_tables() -> None:
    """flush_host_wave_records writes patterns, telemetry, and routing guards."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        counts = db.flush_host_wave_records(
            patterns=[
                {"pattern_hash": "h1", "pattern_desc": "task one", "tier": "low"},
                {"pattern_hash": "h2", "pattern_desc": "task two", "tier": "medium"},
            ],
            telemetry=[
                {"session_id": "run", "task_hash": "t1", "agent_id": 1, "tier": "low", "model": "haiku"},
                {"session_id": "run", "task_hash": "t2", "agent_id": 2, "tier": "medium", "model": "sonnet"},
            ],
            routing_guards=[
                {"caller": "claude-code", "cwd": "/tmp/x", "task_id": "t1", "file_written": "a.py"},
            ],
        )
        assert counts == [1, 1]
        assert _count(db, "subtask_patterns") == 2
        assert _count(db, "telemetry") == 2
        assert _count(db, "routing_guard_executions") == 1


def test_flush_accumulates_repeated_pattern_hash() -> None:
    """Repeated hashes in one flush accumulate occurrence_count (sees in-txn writes)."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        counts = db.flush_host_wave_records(
            patterns=[
                {"pattern_hash": "dup", "pattern_desc": "same", "tier": "low"},
                {"pattern_hash": "dup", "pattern_desc": "same", "tier": "low"},
                {"pattern_hash": "dup", "pattern_desc": "same", "tier": "low"},
            ],
        )
        assert counts == [1, 2, 3]
        with db.conn() as conn:
            row = conn.execute(
                "SELECT occurrence_count FROM subtask_patterns WHERE pattern_hash = ?", ("dup",)
            ).fetchone()
        assert row[0] == 3


def test_apply_pattern_row_parity_with_track_pattern() -> None:
    """Batched _apply_pattern_row yields the same occurrence_count as track_pattern."""
    with tempfile.NamedTemporaryFile(suffix=".db") as fa, \
         tempfile.NamedTemporaryFile(suffix=".db") as fb:
        single = Database(Path(fa.name))
        batched = Database(Path(fb.name))
        for _ in range(3):
            single.track_pattern(pattern_hash="p", pattern_desc="d", tier="low")
        batched.flush_host_wave_records(
            patterns=[{"pattern_hash": "p", "pattern_desc": "d", "tier": "low"}] * 3,
        )
        with single.conn() as ca, batched.conn() as cb:
            a = ca.execute("SELECT occurrence_count FROM subtask_patterns WHERE pattern_hash='p'").fetchone()[0]
            b = cb.execute("SELECT occurrence_count FROM subtask_patterns WHERE pattern_hash='p'").fetchone()[0]
        assert a == b == 3


def test_flush_empty_is_noop() -> None:
    """Empty buffers write nothing and return no counts."""
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        db = Database(Path(f.name))
        assert db.flush_host_wave_records() == []
        assert _count(db, "telemetry") == 0


if __name__ == "__main__":
    tests = [
        test_cache_put_and_get,
        test_cache_miss,
        test_plan_cache,
        test_plan_cache_structural_hash,
        test_plan_cache_rejects_non_serializable_payloads,
        test_artifact_persistence,
        test_routing_outcomes_schema,
        test_cache_stats,
        test_escalation_logging,
        test_concurrent_db_writes_via_threads,
        test_memory_set_get_list_delete,
        test_memory_overwrite_json,
        test_memory_validation_and_corrupted_value,
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
