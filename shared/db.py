#!/usr/bin/env python3
"""
Threnody SQLite database layer.

WAL mode — concurrent reads from hot/warm/cold paths.
Stores: result cache, plan cache, telemetry, adaptive thresholds,
        agent definitions, code style profiles, project routing profiles.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import hashlib
import hmac as _hmac_mod
import json
import logging
import math
import os
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path

from .config import (
    DB_PATH,
    PLAN_CACHE_TTL_HOURS,
    RESULT_CACHE_TTL_HOURS,
    TGsConfig,
    UNLIMITED_PARALLELISM,
)
from .context import make_artifact_envelope

log = logging.getLogger(__name__)
SWARM_SCHEMA_VERSION = "phase-36"
_PREVIEW_TOKEN_PRUNE_INTERVAL_SECONDS = 60.0

DEFAULT_PROJECT_FANOUT_CAP = UNLIMITED_PARALLELISM
DEFAULT_PROJECT_PENDING_APPROVAL_LIMIT = 3
PROJECT_SETTING_KEYS = frozenset({
    "learning_enabled",
    "concurrency_limit",
    "budget_hard_cap_tokens",
    "fanout_cap",
    "pending_approval_limit",
    "allow_out_of_workspace_writes",
})
PROJECT_SETTING_COLUMNS = {
    "concurrency_limit": "concurrency_limit",
    "budget_hard_cap_tokens": "budget_hard_cap_tokens",
    "fanout_cap": "fanout_cap",
    "pending_approval_limit": "pending_approval_limit",
    "allow_out_of_workspace_writes": "allow_out_of_workspace_writes",
}
_PROJECT_SETTING_DEFAULTS_CACHE: dict[str, int | bool] | None = None
_SCHEMA_LOCKS_GUARD = threading.Lock()
_SCHEMA_LOCKS: dict[str, threading.Lock] = {}

# Routing guard mode constants (Phase 37+)
ROUTING_GUARD_MODE_DIRECT: str = "direct"
ROUTING_GUARD_MODE_EXECUTE_SUBTASK: str = "execute_subtask"
ROUTING_GUARD_MODE_ROUTED_PLAN: str = "routed_plan"
ROUTING_GUARD_TTL_SECONDS: int = 3600


def _coerce_db_bool(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _schema_lock_for_path(path: Path) -> threading.Lock:
    key = str(path.resolve(strict=False))
    with _SCHEMA_LOCKS_GUARD:
        lock = _SCHEMA_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _SCHEMA_LOCKS[key] = lock
        return lock


class Database:
    """Unified SQLite database for all Threnody state."""

    def __init__(
        self,
        db_path: Path | None = None,
        result_ttl_hours: int = RESULT_CACHE_TTL_HOURS,
        plan_ttl_hours: int = PLAN_CACHE_TTL_HOURS,
        backup_keep: int = 3,
    ) -> None:
        self._db_path = (db_path or DB_PATH).expanduser() if db_path else DB_PATH
        self._result_ttl = result_ttl_hours * 3600
        self._plan_ttl = plan_ttl_hours * 3600
        self._backup_keep = backup_keep
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_private_db_directory()
        self._schema_lock = _schema_lock_for_path(self._db_path)
        self._legacy_conn_lock = threading.Lock()
        self._schema_ready = False
        self._legacy_conns: dict[int, sqlite3.Connection] = {}
        self._preview_token_last_prune_ts = 0.0
        self._last_backup_ts: float | None = None
        self._last_integrity_ok: bool | None = None
        # Per-thread connection storage for wave parallelism (FNDX-01)
        # Each thread gets its own SQLite connection to avoid ProgrammingError
        # when multiple threads access the shared database.
        self._thread_local = threading.local()
        self._ensure_private_db_file(self._db_path)
        self._restrict_db_permissions()
        self._check_integrity_and_recover()

    def _ensure_private_db_directory(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("secure database directory handling requires O_NOFOLLOW")
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(self._db_path.parent, flags)
        except OSError as exc:
            raise RuntimeError(
                f"failed to secure database directory: {self._db_path.parent}"
            ) from exc
        try:
            stat_result = os.fstat(fd)
            if not stat.S_ISDIR(stat_result.st_mode):
                raise RuntimeError(
                    f"refusing to secure non-directory database path: {self._db_path.parent}"
                )
            mode = stat.S_IMODE(stat_result.st_mode)
            if stat_result.st_uid == os.getuid():
                if mode != 0o700:
                    os.fchmod(fd, 0o700)
            elif stat_result.st_mode & stat.S_ISVTX:
                return
            else:
                raise RuntimeError(
                    f"database directory is not owned by current user: {self._db_path.parent}"
                )
        except OSError as exc:
            raise RuntimeError(
                f"failed to secure database directory: {self._db_path.parent}"
            ) from exc
        finally:
            os.close(fd)

    def _ensure_private_db_file(self, path: Path) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            raise RuntimeError("secure database file handling requires O_NOFOLLOW")
        flags = os.O_RDWR | os.O_CREAT
        flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise RuntimeError(f"failed to open private database file: {path}") from exc
        try:
            stat_result = os.fstat(fd)
            if not stat.S_ISREG(stat_result.st_mode):
                raise RuntimeError(f"refusing to secure non-regular database file: {path}")
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise RuntimeError(f"failed to secure database file: {path}") from exc
        finally:
            os.close(fd)

    def _restrict_db_permissions(self) -> None:
        """Keep the router DB and WAL sidecars private to the current user."""
        self._ensure_private_db_directory()
        for candidate in (
            self._db_path,
            self._db_path.with_name(f"{self._db_path.name}-wal"),
            self._db_path.with_name(f"{self._db_path.name}-shm"),
        ):
            try:
                fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            except OSError:
                raise RuntimeError(f"failed to open database file securely: {candidate}")
            try:
                stat_result = os.fstat(fd)
                if not stat.S_ISREG(stat_result.st_mode):
                    raise RuntimeError(f"refusing to secure non-regular database path: {candidate}")
                os.fchmod(fd, 0o600)
            except OSError as exc:
                raise RuntimeError(f"failed to secure database file: {candidate}") from exc
            finally:
                os.close(fd)

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        """Create all tables if they don't exist."""
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            -- Result cache (existing functionality)
            CREATE TABLE IF NOT EXISTS cache (
                key     TEXT PRIMARY KEY,
                task    TEXT NOT NULL,
                result  TEXT NOT NULL,
                model   TEXT NOT NULL,
                ts      REAL NOT NULL
            );

            -- Plan cache (structural hash → decomposition)
            CREATE TABLE IF NOT EXISTS plan_cache (
                key         TEXT PRIMARY KEY,
                task_hash   TEXT NOT NULL,
                plan_json   TEXT NOT NULL,
                model       TEXT NOT NULL,
                ts          REAL NOT NULL
            );

            -- Phase 12 artifact bus persistence
            CREATE TABLE IF NOT EXISTS artifacts (
                id            TEXT PRIMARY KEY,
                execution_id  TEXT NOT NULL,
                plan_revision INTEGER NOT NULL,
                wave          INTEGER NOT NULL,
                subtask_id    TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                full_payload  TEXT NOT NULL,
                compact_summary TEXT NOT NULL,
                stable_ref    TEXT NOT NULL,
                size          INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                parent_execution_id TEXT,
                producer_subtask_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_scope
                ON artifacts (execution_id, plan_revision, wave, artifact_type);
            CREATE INDEX IF NOT EXISTS idx_artifacts_ref
                ON artifacts (stable_ref);
            CREATE INDEX IF NOT EXISTS idx_artifacts_latest_by_producer
                ON artifacts (
                    execution_id,
                    artifact_type,
                    COALESCE(NULLIF(producer_subtask_id, ''), subtask_id),
                    plan_revision DESC,
                    wave DESC,
                    created_at DESC,
                    stable_ref ASC
                );

            -- Phase 31 swarm persistence scaffolding
            CREATE TABLE IF NOT EXISTS swarm_schema (
                schema_version TEXT PRIMARY KEY,
                applied_ts REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS swarm_runs (
                swarm_id TEXT PRIMARY KEY,
                task_hash TEXT NOT NULL DEFAULT '',
                created_ts REAL NOT NULL,
                status TEXT NOT NULL,
                requested_agents INTEGER NOT NULL,
                effective_agents INTEGER NOT NULL,
                progress_counters TEXT NOT NULL DEFAULT '{}',
                cost_summary_ref TEXT,
                topology TEXT,
                round INTEGER NOT NULL DEFAULT 0,
                resumable INTEGER NOT NULL DEFAULT 0,
                resume_status TEXT NOT NULL DEFAULT 'not_resumable',
                parent_swarm_id TEXT,
                chosen_checkpoint_index INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_swarm_runs_swarm_id
                ON swarm_runs (swarm_id);

            CREATE TABLE IF NOT EXISTS swarm_workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                swarm_id TEXT NOT NULL,
                worker_index INTEGER NOT NULL,
                worker_snapshot_ref TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_swarm_workers_swarm_worker
                ON swarm_workers (swarm_id, worker_index, ts);

            -- Provider-reported subscription quota observations.
            CREATE TABLE IF NOT EXISTS provider_quota_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_ts REAL NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_provider_quota_provider_ts
                ON provider_quota_observations (provider, observed_ts DESC);

             CREATE TABLE IF NOT EXISTS swarm_events (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 swarm_id TEXT NOT NULL,
                 event_type TEXT NOT NULL,
                 payload TEXT NOT NULL,
                 ts REAL NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_swarm_events_swarm_type
                 ON swarm_events (swarm_id, event_type, ts);

             -- Phase 36 budget-preview admission tokens for execute_swarm.
             CREATE TABLE IF NOT EXISTS preview_tokens (
                 token_hmac TEXT PRIMARY KEY,
                 swarm_id TEXT NOT NULL,
                 expires_ts REAL NOT NULL,
                 used INTEGER NOT NULL DEFAULT 0
             );
             CREATE INDEX IF NOT EXISTS idx_preview_tokens_swarm_expires
                 ON preview_tokens (swarm_id, expires_ts);
             CREATE INDEX IF NOT EXISTS idx_preview_tokens_expires
                 ON preview_tokens (expires_ts);

             -- Remote server: async job tracking
             CREATE TABLE IF NOT EXISTS remote_jobs (
                 job_id     TEXT PRIMARY KEY,
                 status     TEXT NOT NULL DEFAULT 'pending',
                 task       TEXT NOT NULL,
                 result     TEXT,
                 error      TEXT,
                 created_ts REAL NOT NULL,
                 updated_ts REAL NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_remote_jobs_status
                 ON remote_jobs (status);

             -- Phase 37+ routing guard records (deduplicate write-file guard decisions).
             CREATE TABLE IF NOT EXISTS routing_guards (
                 guard_key TEXT PRIMARY KEY,
                 caller TEXT NOT NULL,
                 cwd TEXT NOT NULL DEFAULT '',
                 mode TEXT NOT NULL,
                 tier TEXT,
                 provider TEXT,
                 model TEXT,
                 source_tool TEXT NOT NULL DEFAULT '',
                 task_text TEXT NOT NULL DEFAULT '',
                 file_hints_json TEXT NOT NULL DEFAULT '[]',
                 created_ts REAL NOT NULL,
                 expires_ts REAL NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_routing_guards_caller_cwd
                 ON routing_guards (caller, cwd, expires_ts);

             -- Phase 38+ execution tracking: satisfies routing guard after execute_subtask runs.
             CREATE TABLE IF NOT EXISTS routing_guard_executions (
                 id           INTEGER PRIMARY KEY AUTOINCREMENT,
                 caller       TEXT NOT NULL,
                 cwd          TEXT NOT NULL DEFAULT '',
                 task_id      TEXT NOT NULL DEFAULT '',
                 file_written TEXT,
                 executed_ts  REAL NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_rge_caller_cwd
                 ON routing_guard_executions (caller, cwd, executed_ts);

             -- Routing exceptions: user-defined bypass rules for validate_routing_guard.
             CREATE TABLE IF NOT EXISTS routing_exceptions (
                 id             INTEGER PRIMARY KEY AUTOINCREMENT,
                 exception_type TEXT NOT NULL,
                 pattern        TEXT NOT NULL,
                 note           TEXT,
                 created_at     REAL NOT NULL,
                 UNIQUE(exception_type, pattern)
             );
             CREATE INDEX IF NOT EXISTS idx_routing_exceptions_type
                 ON routing_exceptions (exception_type);

            CREATE TABLE IF NOT EXISTS coordinator_round_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                swarm_id TEXT NOT NULL,
                plan_revision INTEGER NOT NULL,
                round_index INTEGER NOT NULL,
                coordinator_subtask_id TEXT NOT NULL,
                verdict TEXT,
                amendment_json TEXT,
                next_work_json TEXT,
                synthesis_summary_json TEXT,
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                artifact_summaries_json TEXT NOT NULL DEFAULT '[]',
                round_counters_json TEXT NOT NULL DEFAULT '{}',
                fallback_reason TEXT,
                created_ts REAL NOT NULL,
                UNIQUE(swarm_id, plan_revision, round_index)
            );
            CREATE INDEX IF NOT EXISTS idx_coord_round_ckpt_swarm_revision
                ON coordinator_round_checkpoints (swarm_id, plan_revision, round_index);
            CREATE INDEX IF NOT EXISTS idx_coord_round_ckpt_created
                ON coordinator_round_checkpoints (swarm_id, created_ts);

            -- Phase 13 coordinator revision and audit persistence
            CREATE TABLE IF NOT EXISTS plan_revisions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id         TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                diff_blob       TEXT NOT NULL,
                proposer_id     TEXT NOT NULL,
                reason          TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                UNIQUE(plan_id, revision_number)
            );
            CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan_revision
                ON plan_revisions (plan_id, revision_number);

            CREATE TABLE IF NOT EXISTS coordinator_amendments (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id          TEXT NOT NULL,
                revision_id      INTEGER,
                proposer_id      TEXT NOT NULL,
                diff_blob        TEXT NOT NULL,
                reason           TEXT NOT NULL,
                outcome          TEXT NOT NULL,
                rejection_reason TEXT,
                created_at       INTEGER NOT NULL,
                FOREIGN KEY(revision_id) REFERENCES plan_revisions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_coordinator_amendments_plan_created
                ON coordinator_amendments (plan_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_coordinator_amendments_revision
                ON coordinator_amendments (revision_id);

            -- Telemetry (per-agent results, rework signals)
            CREATE TABLE IF NOT EXISTS telemetry (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT,
                task_hash   TEXT,
                agent_id    INTEGER,
                tier        TEXT,
                model       TEXT,
                provider_name TEXT,
                success     INTEGER DEFAULT 1,
                rework      INTEGER DEFAULT 0,
                tokens_used INTEGER DEFAULT 0,
                escalated   INTEGER DEFAULT 0,
                used_fallback INTEGER DEFAULT 0,
                used_speculation INTEGER DEFAULT 0,
                provenance_trace_id TEXT,
                provenance_depth INTEGER DEFAULT 0,
                provenance_caller_id TEXT,
                provider_opt_out_reason TEXT,
                estimated_tokens INTEGER,
                actual_tokens INTEGER,
                timing_ms INTEGER,
                rework_count INTEGER DEFAULT 0,
                parse_diagnostics TEXT,
                reason      TEXT,
                version     TEXT,
                ts          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry (ts);
            CREATE INDEX IF NOT EXISTS idx_telemetry_task_hash_ts
                ON telemetry (task_hash, ts DESC, id DESC);

            -- Adaptive thresholds (EMA per complexity band per version)
            CREATE TABLE IF NOT EXISTS adaptive_thresholds (
                band        TEXT NOT NULL,
                version     TEXT NOT NULL,
                tier        TEXT NOT NULL,
                success_ema REAL DEFAULT 0.90,
                sample_count INTEGER DEFAULT 0,
                ts          REAL NOT NULL,
                PRIMARY KEY (band, version, tier)
            );

            -- Learned agent definitions
            CREATE TABLE IF NOT EXISTS agent_definitions (
                pattern_hash TEXT PRIMARY KEY,
                pattern_desc TEXT NOT NULL,
                definition   TEXT NOT NULL,
                match_count  INTEGER DEFAULT 1,
                ts           REAL NOT NULL
            );

            -- Agent audit trail for draft/merge/promotion lifecycle
            CREATE TABLE IF NOT EXISTS agent_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id     TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                details_json TEXT NOT NULL,
                canonical_id TEXT,
                merged_from  TEXT,
                created_at   TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_agent_audit_agent_created
                ON agent_audit (agent_id, created_at);

            -- Code style profiles (per project)
            CREATE TABLE IF NOT EXISTS style_profiles (
                project_path TEXT PRIMARY KEY,
                profile_json TEXT NOT NULL,
                ts           REAL NOT NULL
            );

            -- Project routing profiles
            CREATE TABLE IF NOT EXISTS project_routing (
                project_path TEXT PRIMARY KEY,
                overrides_json TEXT NOT NULL,
                ts             REAL NOT NULL
            );

            -- Fan-out telemetry for per-domain budgeting and reconciliation
            CREATE TABLE IF NOT EXISTS fanout_telemetry (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id           TEXT NOT NULL,
                selected_routers  TEXT NOT NULL,
                budget_accounting TEXT NOT NULL,
                created_at        TEXT NOT NULL
            );
             CREATE INDEX IF NOT EXISTS idx_fanout_telemetry_task_created
                 ON fanout_telemetry (task_id, created_at);

             -- Phase 4 operator settings and approval queue
             CREATE TABLE IF NOT EXISTS project_settings (
                 project_path           TEXT PRIMARY KEY,
                 concurrency_limit      INTEGER NOT NULL,
                 budget_hard_cap_tokens INTEGER NOT NULL,
                 fanout_cap             INTEGER NOT NULL,
                 pending_approval_limit INTEGER NOT NULL,
                 allow_out_of_workspace_writes INTEGER NOT NULL DEFAULT 0,
                 ts                     REAL NOT NULL
            );

             CREATE TABLE IF NOT EXISTS approval_queue (
                  id                INTEGER PRIMARY KEY AUTOINCREMENT,
                  project_path      TEXT NOT NULL,
                  draft_fingerprint TEXT NOT NULL,
                  draft_name        TEXT NOT NULL,
                 draft_json        TEXT NOT NULL,
                 status            TEXT NOT NULL DEFAULT 'pending',
                 review_note       TEXT,
                 canonical_id      TEXT,
                 created_at        TEXT NOT NULL,
                 updated_at        TEXT NOT NULL
             );
             CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_queue_project_fingerprint_status
                 ON approval_queue (project_path, draft_fingerprint, status);
              CREATE INDEX IF NOT EXISTS idx_approval_queue_project_status_created
                  ON approval_queue (project_path, status, created_at);

             CREATE TABLE IF NOT EXISTS model_catalog (
                 model_id    TEXT NOT NULL,
                 provider    TEXT NOT NULL,
                 tier        TEXT NOT NULL,
                 cost        REAL,
                 last_seen   INTEGER NOT NULL,
                 source      TEXT NOT NULL,
                 stale_until INTEGER,
                 metadata_json TEXT,
                 UNIQUE(provider, model_id)
             );
             CREATE INDEX IF NOT EXISTS idx_model_catalog_provider
                 ON model_catalog (provider);

             -- Rework tracking (file overlap between waves)
             CREATE TABLE IF NOT EXISTS rework_events (
                 id          INTEGER PRIMARY KEY AUTOINCREMENT,
                 session_id  TEXT,
                wave_n      INTEGER,
                wave_n1     INTEGER,
                file_path   TEXT,
                scope_match INTEGER DEFAULT 0,
                ts          REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rework_events_ts ON rework_events (ts);

            -- Subtask pattern tracking (pre-agent emergence)
            CREATE TABLE IF NOT EXISTS subtask_patterns (
                pattern_hash TEXT PRIMARY KEY,
                pattern_desc TEXT NOT NULL,
                occurrence_count INTEGER DEFAULT 1,
                tier         TEXT,
                last_seen    REAL NOT NULL,
                examples     TEXT DEFAULT '[]',
                rework_detected INTEGER DEFAULT 0,
                eval_quality REAL DEFAULT 0.0
            );

            -- Kill switch escalations
            CREATE TABLE IF NOT EXISTS escalations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                task_hash   TEXT,
                agent_id    INTEGER,
                from_tier   TEXT,
                to_tier     TEXT,
                token_count INTEGER,
                ceiling     INTEGER,
                ts          REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS preview_records (
                preview_token TEXT PRIMARY KEY,
                requested_path TEXT NOT NULL,
                content TEXT NOT NULL,
                caller TEXT,
                ts REAL NOT NULL
            );

             CREATE TABLE IF NOT EXISTS write_audit (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 preview_token TEXT,
                 requested_path TEXT NOT NULL,
                 caller TEXT,
                 outcome TEXT NOT NULL,
                 details TEXT,
                 ts REAL NOT NULL
              );

             CREATE TABLE IF NOT EXISTS routing_outcomes (
                 task_id TEXT PRIMARY KEY,
                 current_outcome TEXT NOT NULL,
                 previous_outcome TEXT,
                 recorded_at REAL NOT NULL,
                 tier TEXT,
                 model TEXT,
                 provider_name TEXT,
                 complexity_score REAL,
                 telemetry_id INTEGER,
                 last_modified_by TEXT,
                 created_at REAL NOT NULL
             );

             CREATE TABLE IF NOT EXISTS routing_outcome_audit (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 task_id TEXT NOT NULL,
                 outcome TEXT NOT NULL,
                 operator_id TEXT,
                 note TEXT,
                 recorded_at REAL NOT NULL,
                 previous_outcome TEXT
             );

             CREATE TABLE IF NOT EXISTS learning_queue (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 task_id TEXT NOT NULL,
                 tier TEXT NOT NULL,
                 complexity_score REAL NOT NULL,
                 success BOOLEAN NOT NULL,
                 status TEXT DEFAULT 'pending',
                 enqueued_at REAL NOT NULL,
                 processed_at REAL,
                 UNIQUE(task_id)
             );
             CREATE INDEX IF NOT EXISTS idx_learning_queue_status ON learning_queue(status);

             -- Multi-user server support: one row per registered remote-server user.
             CREATE TABLE IF NOT EXISTS users (
                 user_id        TEXT PRIMARY KEY,
                 username       TEXT UNIQUE NOT NULL,
                 token_hmac     TEXT UNIQUE NOT NULL,
                 providers_json TEXT NOT NULL DEFAULT '{}',
                 enabled        INTEGER NOT NULL DEFAULT 1,
                 created_ts     REAL NOT NULL,
                 updated_ts     REAL NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_users_username    ON users (username);
             CREATE INDEX IF NOT EXISTS idx_users_token_hmac  ON users (token_hmac);
        """)
        self._ensure_parent_scoped_schema(conn)
        self._ensure_telemetry_columns(conn)
        self._ensure_phase3_columns(conn)
        self._ensure_phase10_columns(conn)
        self._ensure_phase11_columns(conn)
        self._ensure_phase18_memory_table(conn)
        self._ensure_phase31_swarm_columns(conn)
        self._ensure_project_settings_oow_column(conn)
        self._ensure_users_columns(conn)
        self._ensure_resilience_schema(conn)
        self._ensure_preview_records_mode_column(conn)
        self._ensure_model_catalog_url_source(conn)
        self._ensure_idempotency_schema(conn)
        self._ensure_audit_chain_schema(conn)
        self._ensure_worker_lease_schema(conn)
        self._ensure_cost_telemetry_schema(conn)
        self._ensure_foreach_schema(conn)
        self._ensure_gate_verdict_schema(conn)
        self._ensure_worker_sessions_schema(conn)
        self._ensure_bandit_schema(conn)
        self._ensure_convergence_schema(conn)
        self._ensure_compression_schema(conn)
        self._record_swarm_schema_version(conn)
        conn.commit()

    @staticmethod
    def _ensure_telemetry_columns(conn: sqlite3.Connection) -> None:
        """Add newer telemetry columns to older databases.

        Phase 15 additions (D-01 / D-02 / D-03): add queryable, first-class
        telemetry columns for core explainability fields while preserving the
        existing `parse_diagnostics` / `extras` JSON payload.
        """
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(telemetry)").fetchall()
        }
        migrations = {
            "provider_name": "ALTER TABLE telemetry ADD COLUMN provider_name TEXT",
            "used_fallback": (
                "ALTER TABLE telemetry ADD COLUMN used_fallback INTEGER DEFAULT 0"
            ),
            "used_speculation": (
                "ALTER TABLE telemetry ADD COLUMN used_speculation INTEGER DEFAULT 0"
            ),
            "provenance_trace_id": (
                "ALTER TABLE telemetry ADD COLUMN provenance_trace_id TEXT"
            ),
            "provenance_depth": (
                "ALTER TABLE telemetry ADD COLUMN provenance_depth INTEGER DEFAULT 0"
            ),
            "provenance_caller_id": (
                "ALTER TABLE telemetry ADD COLUMN provenance_caller_id TEXT"
            ),
            "provider_opt_out_reason": (
                "ALTER TABLE telemetry ADD COLUMN provider_opt_out_reason TEXT"
            ),
            "estimated_tokens": (
                "ALTER TABLE telemetry ADD COLUMN estimated_tokens INTEGER"
            ),
            "actual_tokens": (
                "ALTER TABLE telemetry ADD COLUMN actual_tokens INTEGER"
            ),
            "timing_ms": "ALTER TABLE telemetry ADD COLUMN timing_ms INTEGER",
            "rework_count": (
                "ALTER TABLE telemetry ADD COLUMN rework_count INTEGER DEFAULT 0"
            ),
            "parse_diagnostics": (
                "ALTER TABLE telemetry ADD COLUMN parse_diagnostics TEXT"
            ),
            "reason": "ALTER TABLE telemetry ADD COLUMN reason TEXT",
            # Phase 15 core explainability fields (additive)
            "urgency_score": (
                "ALTER TABLE telemetry ADD COLUMN urgency_score REAL"
            ),
            "selected_topology": (
                "ALTER TABLE telemetry ADD COLUMN selected_topology TEXT"
            ),
            "fanout_final_action": (
                "ALTER TABLE telemetry ADD COLUMN fanout_final_action TEXT"
            ),
            "artifact_publish_count": (
                "ALTER TABLE telemetry ADD COLUMN artifact_publish_count INTEGER DEFAULT 0"
            ),
            "artifact_consume_count": (
                "ALTER TABLE telemetry ADD COLUMN artifact_consume_count INTEGER DEFAULT 0"
            ),
            "coordinator_round_count": (
                "ALTER TABLE telemetry ADD COLUMN coordinator_round_count INTEGER DEFAULT 0"
            ),
            "coordinator_amendment_count": (
                "ALTER TABLE telemetry ADD COLUMN coordinator_amendment_count INTEGER DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)

    @staticmethod
    def _ensure_phase3_columns(conn: sqlite3.Connection) -> None:
        """Add Phase 3 lifecycle and learning columns to existing tables."""
        project_routing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(project_routing)").fetchall()
        }
        if "learning_enabled" not in project_routing_columns:
            conn.execute(
                "ALTER TABLE project_routing ADD COLUMN learning_enabled INTEGER DEFAULT 0"
            )

        agent_definition_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_definitions)").fetchall()
        }
        if "promotion_state" not in agent_definition_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN promotion_state TEXT DEFAULT 'draft'"
            )

    @staticmethod
    def _ensure_phase10_columns(conn: sqlite3.Connection) -> None:
        """Add Phase 10 pattern tracking columns and agent definition columns."""
        pattern_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(subtask_patterns)").fetchall()
        }
        if "rework_detected" not in pattern_columns:
            conn.execute(
                "ALTER TABLE subtask_patterns ADD COLUMN rework_detected INTEGER DEFAULT 0"
            )
        if "eval_quality" not in pattern_columns:
            conn.execute(
                "ALTER TABLE subtask_patterns ADD COLUMN eval_quality REAL DEFAULT 0.0"
            )
        
        # Add Phase 10 agent_definitions columns for Wave 1b: conservative dedup and merge
        agent_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_definitions)").fetchall()
        }
        if "id" not in agent_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN id TEXT"
            )
        if "project_id" not in agent_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN project_id TEXT"
            )
        if "lane" not in agent_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN lane TEXT DEFAULT 'shared'"
            )
        if "description" not in agent_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN description TEXT"
            )
        if "status" not in agent_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN status TEXT DEFAULT 'pending'"
            )
        if "merged_into_id" not in agent_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN merged_into_id TEXT"
            )
        if "activated_at" not in agent_columns:
            conn.execute(
                "ALTER TABLE agent_definitions ADD COLUMN activated_at REAL"
            )

    @staticmethod
    def _ensure_phase11_columns(conn: sqlite3.Connection) -> None:
        """Add Phase 11 plan cache metadata columns."""
        plan_cache_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(plan_cache)").fetchall()
        }
        if "topology" not in plan_cache_columns:
            conn.execute(
                "ALTER TABLE plan_cache ADD COLUMN topology TEXT DEFAULT NULL"
            )
        if "plan_schema_version" not in plan_cache_columns:
            conn.execute(
                "ALTER TABLE plan_cache ADD COLUMN plan_schema_version INTEGER DEFAULT 1"
            )

    @staticmethod
    def _ensure_phase18_memory_table(conn: sqlite3.Connection) -> None:
        """Add Phase 18 namespaced memory storage."""
        memory_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory'"
        ).fetchone()
        if memory_exists is None:
            conn.execute("""
                CREATE TABLE memory (
                    scope TEXT NOT NULL CHECK(scope IN ('global', 'project', 'task')),
                    project_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL DEFAULT '',
                    key TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    value_size INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
        memory_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(memory)").fetchall()
        }
        if "value_size" not in memory_columns:
            conn.execute(
                "ALTER TABLE memory ADD COLUMN value_size INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "UPDATE memory SET value_size = length(CAST(value_json AS BLOB)) WHERE value_size = 0"
            )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_scope_key
            ON memory (scope, project_id, task_id, key)
            """
        )

    @staticmethod
    def _ensure_phase31_swarm_columns(conn: sqlite3.Connection) -> None:
        """Add Phase 31 swarm persistence tables and columns."""
        swarm_runs_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'swarm_runs'"
        ).fetchone()
        if swarm_runs_exists is None:
            conn.execute("""
                CREATE TABLE swarm_runs (
                    swarm_id TEXT PRIMARY KEY,
                    task_hash TEXT NOT NULL DEFAULT '',
                    created_ts REAL NOT NULL,
                    status TEXT NOT NULL,
                    requested_agents INTEGER NOT NULL,
                    effective_agents INTEGER NOT NULL,
                    progress_counters TEXT NOT NULL DEFAULT '{}',
                    cost_summary_ref TEXT,
                    topology TEXT,
                    round INTEGER NOT NULL DEFAULT 0,
                    resumable INTEGER NOT NULL DEFAULT 0,
                    resume_status TEXT NOT NULL DEFAULT 'not_resumable',
                    parent_swarm_id TEXT,
                    chosen_checkpoint_index INTEGER
                )
            """)
        swarm_runs_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(swarm_runs)").fetchall()
        }
        swarm_run_migrations = {
            "task_hash": "ALTER TABLE swarm_runs ADD COLUMN task_hash TEXT NOT NULL DEFAULT ''",
            "requested_agents": (
                "ALTER TABLE swarm_runs ADD COLUMN requested_agents INTEGER NOT NULL DEFAULT 0"
            ),
            "effective_agents": (
                "ALTER TABLE swarm_runs ADD COLUMN effective_agents INTEGER NOT NULL DEFAULT 0"
            ),
            "progress_counters": (
                "ALTER TABLE swarm_runs ADD COLUMN progress_counters TEXT NOT NULL DEFAULT '{}'"
            ),
            "cost_summary_ref": (
                "ALTER TABLE swarm_runs ADD COLUMN cost_summary_ref TEXT"
            ),
            "topology": "ALTER TABLE swarm_runs ADD COLUMN topology TEXT",
            "round": "ALTER TABLE swarm_runs ADD COLUMN round INTEGER NOT NULL DEFAULT 0",
            "resumable": (
                "ALTER TABLE swarm_runs ADD COLUMN resumable INTEGER NOT NULL DEFAULT 0"
            ),
            "resume_status": (
                "ALTER TABLE swarm_runs ADD COLUMN resume_status TEXT NOT NULL DEFAULT 'not_resumable'"
            ),
            "parent_swarm_id": (
                "ALTER TABLE swarm_runs ADD COLUMN parent_swarm_id TEXT"
            ),
            "chosen_checkpoint_index": (
                "ALTER TABLE swarm_runs ADD COLUMN chosen_checkpoint_index INTEGER"
            ),
        }
        for column, statement in swarm_run_migrations.items():
            if column not in swarm_runs_columns:
                conn.execute(statement)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_swarm_runs_swarm_id ON swarm_runs (swarm_id)"
        )

        swarm_workers_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'swarm_workers'"
        ).fetchone()
        if swarm_workers_exists is None:
            conn.execute("""
                CREATE TABLE swarm_workers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    swarm_id TEXT NOT NULL,
                    worker_index INTEGER NOT NULL,
                    worker_snapshot_ref TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    ts REAL NOT NULL
                )
            """)
        swarm_worker_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(swarm_workers)").fetchall()
        }
        swarm_worker_migrations = {
            "worker_snapshot_ref": (
                "ALTER TABLE swarm_workers ADD COLUMN worker_snapshot_ref TEXT NOT NULL DEFAULT ''"
            ),
            "snapshot_json": (
                "ALTER TABLE swarm_workers ADD COLUMN snapshot_json TEXT NOT NULL DEFAULT '{}'"
            ),
            "ts": "ALTER TABLE swarm_workers ADD COLUMN ts REAL NOT NULL DEFAULT 0",
        }
        for column, statement in swarm_worker_migrations.items():
            if column not in swarm_worker_columns:
                conn.execute(statement)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_swarm_workers_swarm_worker "
            "ON swarm_workers (swarm_id, worker_index, ts)"
        )

        swarm_events_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'swarm_events'"
        ).fetchone()
        if swarm_events_exists is None:
            conn.execute("""
                CREATE TABLE swarm_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    swarm_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    ts REAL NOT NULL
                )
            """)
        swarm_event_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(swarm_events)").fetchall()
        }
        swarm_event_migrations = {
            "payload": "ALTER TABLE swarm_events ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'",
            "ts": "ALTER TABLE swarm_events ADD COLUMN ts REAL NOT NULL DEFAULT 0",
        }
        for column, statement in swarm_event_migrations.items():
            if column not in swarm_event_columns:
                conn.execute(statement)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_swarm_events_swarm_type "
            "ON swarm_events (swarm_id, event_type, ts)"
        )

        routing_guards_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'routing_guards'"
        ).fetchone()
        if routing_guards_exists is None:
            conn.execute("""
                CREATE TABLE routing_guards (
                    guard_key TEXT PRIMARY KEY,
                    caller TEXT NOT NULL,
                    cwd TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL,
                    tier TEXT,
                    provider TEXT,
                    model TEXT,
                    source_tool TEXT NOT NULL DEFAULT '',
                    task_text TEXT NOT NULL DEFAULT '',
                    file_hints_json TEXT NOT NULL DEFAULT '[]',
                    created_ts REAL NOT NULL,
                    expires_ts REAL NOT NULL
                )
            """)
        else:
            # Add guard_key column if missing (older schema used id as primary key).
            existing_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(routing_guards)").fetchall()
            }
            if "guard_key" not in existing_cols:
                conn.execute(
                    "ALTER TABLE routing_guards ADD COLUMN guard_key TEXT"
                )
            missing_guard_rows = conn.execute(
                """
                SELECT rowid, caller, cwd
                FROM routing_guards
                WHERE guard_key IS NULL OR trim(guard_key) = ''
                """
            ).fetchall()
            for rowid, caller, cwd in missing_guard_rows:
                conn.execute(
                    "UPDATE routing_guards SET guard_key = ? WHERE rowid = ?",
                    (
                        Database._routing_guard_key(
                            str(caller or "").strip() or "mcp",
                            Database._normalize_routing_guard_cwd(cwd),
                        ),
                        rowid,
                    ),
                )
            conn.execute(
                """
                DELETE FROM routing_guards
                WHERE rowid NOT IN (
                    SELECT MAX(rowid)
                    FROM routing_guards
                    GROUP BY guard_key
                )
                """
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_guards_guard_key "
            "ON routing_guards (guard_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_routing_guards_caller_cwd "
            "ON routing_guards (caller, cwd, expires_ts)"
        )

        swarm_schema_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'swarm_schema'"
        ).fetchone()
        if swarm_schema_exists is None:
            conn.execute("""
                CREATE TABLE swarm_schema (
                    schema_version TEXT PRIMARY KEY,
                    applied_ts REAL NOT NULL
                )
            """)

        routing_exceptions_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'routing_exceptions'"
        ).fetchone()
        if routing_exceptions_exists is None:
            conn.execute("""
                CREATE TABLE routing_exceptions (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    exception_type TEXT NOT NULL,
                    pattern        TEXT NOT NULL,
                    note           TEXT,
                    created_at     REAL NOT NULL,
                    UNIQUE(exception_type, pattern)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_routing_exceptions_type "
                "ON routing_exceptions (exception_type)"
            )

    @staticmethod
    def _record_swarm_schema_version(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT applied_ts FROM swarm_schema WHERE schema_version = ?",
            (SWARM_SCHEMA_VERSION,),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO swarm_schema (schema_version, applied_ts)
                VALUES (?, ?)
                """,
                (SWARM_SCHEMA_VERSION, time.time()),
            )

    @staticmethod
    def _serialize_json_field(value: object, *, default: object | None = None) -> str | None:
        if value is None:
            value = default
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except TypeError as exc:
            raise TypeError("value must be JSON-serializable") from exc

    @staticmethod
    def _parse_json_field(value: object, *, default: object) -> object:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list, bool, int, float)):
            return value
        if not isinstance(value, str):
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    @staticmethod
    def _swarm_snapshot_ref(snapshot_json: str) -> str:
        digest = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        return f"swarm-snapshot:{digest}"

    @staticmethod
    def _coerce_swarm_int(value: object, *, field_name: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc

    @staticmethod
    def _coerce_swarm_float(value: object, *, field_name: str) -> float:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be a number") from exc

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            self._init_schema(conn)
            conn.commit()
            self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        """Open a fresh WAL connection for one logical DB operation."""
        conn = sqlite3.connect(str(self._db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        self._ensure_schema(conn)
        self._restrict_db_permissions()
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get or create a thread-local SQLite connection.
        
        This method implements the per-thread connection pattern required by FNDX-01.
        Each thread gets its own Connection object stored in thread-local storage.
        The connection is created on first use and reused for subsequent calls within
        the same thread. This eliminates ProgrammingError when concurrent workers
        (e.g., ThreadPoolExecutor wave execution) write to the database.
        
        WAL mode is set on all new connections to allow concurrent reads while
        writes serialize safely.
        
        Returns:
            sqlite3.Connection: Thread-local connection for this thread
        """
        # Check if this thread already has a connection
        if not hasattr(self._thread_local, 'conn'):
            # Create a new connection for this thread
            conn = sqlite3.connect(str(self._db_path), timeout=30.0)
            # Do not attempt to change journal_mode here; it can conflict when multiple threads initialize.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            # Ensure schema is initialized (first thread will create tables)
            self._ensure_schema(conn)
            self._restrict_db_permissions()
            # Store in thread-local storage for reuse
            self._thread_local.conn = conn
            log.debug(f"Created thread-local connection for thread {threading.get_ident()}")
        return self._thread_local.conn

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        """
        Yield a thread-safe SQLite connection with automatic commit/rollback.
        
        This context manager uses thread-local connections from _get_connection()
        to safely support concurrent access from multiple threads. Each thread
        gets its own connection, eliminating cross-thread SQLite errors.
        
        The connection is kept in thread-local storage after use (not closed)
        so it can be reused for subsequent operations in the same thread.
        This improves performance in worker threads that make multiple DB calls.
        
        Usage:
            with db.conn() as conn:
                conn.execute("INSERT INTO telemetry ...")
        """
        conn = self._get_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        # Note: Connection is NOT closed here — it persists in thread-local storage
        # for reuse by subsequent calls in the same thread (FNDX-01)

    # ------------------------------------------------------------------
    # Phase 4 operator settings (D-09..D-12)
    # ------------------------------------------------------------------

    @staticmethod
    def _default_project_routing_overrides() -> str:
        return json.dumps({
            "tier_bias": 0.0,
            "sample_count": 0,
            "learning_sample_count": 0,
        })


    @staticmethod
    def _ensure_project_settings_oow_column(conn: sqlite3.Connection) -> None:
        """Add allow_out_of_workspace_writes column to project_settings if missing."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(project_settings)").fetchall()}
        if "allow_out_of_workspace_writes" not in cols:
            conn.execute(
                "ALTER TABLE project_settings ADD COLUMN "
                "allow_out_of_workspace_writes INTEGER NOT NULL DEFAULT 0"
            )

    @staticmethod
    def _ensure_users_columns(conn: sqlite3.Connection) -> None:
        """Add user_id column to remote_jobs for multi-user server support."""
        remote_job_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(remote_jobs)").fetchall()
        }
        if "user_id" not in remote_job_cols:
            conn.execute("ALTER TABLE remote_jobs ADD COLUMN user_id TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_remote_jobs_user_id ON remote_jobs (user_id)"
            )

    @staticmethod
    def _ensure_preview_records_mode_column(conn: sqlite3.Connection) -> None:
        """Add mode column to preview_records for surgical-edit mode tracking."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(preview_records)").fetchall()}
        if "mode" not in existing:
            conn.execute(
                "ALTER TABLE preview_records ADD COLUMN mode TEXT NOT NULL DEFAULT 'write'"
            )

    @staticmethod
    def _ensure_model_catalog_url_source(conn: sqlite3.Connection) -> None:
        """Add normalized catalog metadata columns to older databases."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(model_catalog)").fetchall()}
        if "url_source" not in cols:
            conn.execute("ALTER TABLE model_catalog ADD COLUMN url_source TEXT")
        if "metadata_json" not in cols:
            conn.execute("ALTER TABLE model_catalog ADD COLUMN metadata_json TEXT")

    @staticmethod
    def _ensure_idempotency_schema(conn: sqlite3.Connection) -> None:
        """Add idempotency columns to side-effecting tables; create file_writes + idempotency_attempts tables."""
        # Hardcoded ALTER statements per table — no f-strings in SQL.
        _swarm_alters: list[tuple[str, str]] = [
            ("idempotency_key", "ALTER TABLE swarm_events ADD COLUMN idempotency_key TEXT"),
            ("attempt", "ALTER TABLE swarm_events ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"),
            ("first_seen_at", "ALTER TABLE swarm_events ADD COLUMN first_seen_at REAL"),
            ("last_attempt_at", "ALTER TABLE swarm_events ADD COLUMN last_attempt_at REAL"),
            ("op_class", "ALTER TABLE swarm_events ADD COLUMN op_class TEXT NOT NULL DEFAULT 'side_effecting'"),
            ("chain_hmac", "ALTER TABLE swarm_events ADD COLUMN chain_hmac TEXT NOT NULL DEFAULT ''"),
        ]
        _remote_jobs_alters: list[tuple[str, str]] = [
            ("idempotency_key", "ALTER TABLE remote_jobs ADD COLUMN idempotency_key TEXT"),
            ("attempt", "ALTER TABLE remote_jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"),
            ("first_seen_at", "ALTER TABLE remote_jobs ADD COLUMN first_seen_at REAL"),
            ("last_attempt_at", "ALTER TABLE remote_jobs ADD COLUMN last_attempt_at REAL"),
        ]
        _approval_queue_alters: list[tuple[str, str]] = [
            ("idempotency_key", "ALTER TABLE approval_queue ADD COLUMN idempotency_key TEXT"),
            ("attempt", "ALTER TABLE approval_queue ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"),
            ("first_seen_at", "ALTER TABLE approval_queue ADD COLUMN first_seen_at REAL"),
            ("last_attempt_at", "ALTER TABLE approval_queue ADD COLUMN last_attempt_at REAL"),
        ]
        _ckpt_alters: list[tuple[str, str]] = [
            ("idempotency_key", "ALTER TABLE coordinator_round_checkpoints ADD COLUMN idempotency_key TEXT"),
            ("attempt", "ALTER TABLE coordinator_round_checkpoints ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"),
            ("first_seen_at", "ALTER TABLE coordinator_round_checkpoints ADD COLUMN first_seen_at REAL"),
            ("last_attempt_at", "ALTER TABLE coordinator_round_checkpoints ADD COLUMN last_attempt_at REAL"),
        ]
        for pragma_sql, alters in (
            ("PRAGMA table_info(swarm_events)", _swarm_alters),
            ("PRAGMA table_info(remote_jobs)", _remote_jobs_alters),
            ("PRAGMA table_info(approval_queue)", _approval_queue_alters),
            ("PRAGMA table_info(coordinator_round_checkpoints)", _ckpt_alters),
        ):
            existing = {row[1] for row in conn.execute(pragma_sql).fetchall()}
            for col_name, alter_sql in alters:
                if col_name not in existing:
                    conn.execute(alter_sql)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_writes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                scope           TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                target_path     TEXT NOT NULL,
                lines_written   INTEGER,
                completed_at    REAL NOT NULL,
                UNIQUE(scope, idempotency_key)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_writes_scope_key "
            "ON file_writes (scope, idempotency_key)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS idempotency_attempts (
                scope           TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                attempt         INTEGER NOT NULL DEFAULT 0,
                first_seen_at   REAL NOT NULL,
                last_attempt_at REAL NOT NULL,
                PRIMARY KEY (scope, idempotency_key)
            )
        """)

    @staticmethod
    def _ensure_audit_chain_schema(conn: sqlite3.Connection) -> None:
        """Add chain_hmac column to agent_audit and file_writes (swarm_events handled in idempotency schema)."""
        agent_audit_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_audit)").fetchall()
        }
        if "chain_hmac" not in agent_audit_cols:
            conn.execute(
                "ALTER TABLE agent_audit ADD COLUMN chain_hmac TEXT NOT NULL DEFAULT ''"
            )
        file_writes_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(file_writes)").fetchall()
        }
        if "chain_hmac" not in file_writes_cols:
            conn.execute(
                "ALTER TABLE file_writes ADD COLUMN chain_hmac TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _ensure_worker_lease_schema(conn: sqlite3.Connection) -> None:
        """Create worker_leases and dead_letters tables (plan 09)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_leases (
                task_id        TEXT NOT NULL,
                worker_id      TEXT NOT NULL,
                acquired_at    REAL NOT NULL,
                expires_at     REAL NOT NULL,
                last_heartbeat REAL NOT NULL,
                attempt        INTEGER NOT NULL DEFAULT 0,
                status         TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY (task_id, worker_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worker_leases_status_expires"
            " ON worker_leases (status, expires_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dead_letters (
                task_id       TEXT PRIMARY KEY,
                last_error    TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                first_failed_at REAL NOT NULL,
                last_failed_at  REAL NOT NULL,
                payload       TEXT
            )
        """)

    @staticmethod
    def _ensure_cost_telemetry_schema(conn: sqlite3.Connection) -> None:
        """Create cost_telemetry table for savings tracking (plan 07)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cost_telemetry (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id             TEXT NOT NULL,
                tier                TEXT NOT NULL,
                provider_id         TEXT NOT NULL,
                model               TEXT NOT NULL,
                input_tokens        INTEGER NOT NULL DEFAULT 0,
                output_tokens       INTEGER NOT NULL DEFAULT 0,
                est_cost_usd        REAL NOT NULL DEFAULT 0.0,
                counterfactual_tier TEXT NOT NULL DEFAULT 'high',
                counterfactual_cost_usd REAL NOT NULL DEFAULT 0.0,
                ts                  REAL NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cost_telemetry_ts"
            " ON cost_telemetry (ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cost_telemetry_tier"
            " ON cost_telemetry (tier, ts)"
        )

    @staticmethod
    def _ensure_foreach_schema(conn: sqlite3.Connection) -> None:
        """Add expanded_items column to plan_revisions for for_each node persistence."""
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(plan_revisions)").fetchall()
        }
        if "expanded_items" not in cols:
            conn.execute(
                "ALTER TABLE plan_revisions ADD COLUMN expanded_items TEXT"
            )

    @staticmethod
    def _ensure_gate_verdict_schema(conn: sqlite3.Connection) -> None:
        """Add gate_verdict column to routing_outcomes (plan 04 verify gate)."""
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(routing_outcomes)").fetchall()
        }
        if "gate_verdict" not in cols:
            conn.execute(
                "ALTER TABLE routing_outcomes ADD COLUMN gate_verdict TEXT"
            )

    @staticmethod
    def _ensure_convergence_schema(conn: sqlite3.Connection) -> None:
        """Add convergence_rounds column to routing_outcomes (plan 14)."""
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(routing_outcomes)").fetchall()
        }
        if "convergence_rounds" not in cols:
            conn.execute(
                "ALTER TABLE routing_outcomes ADD COLUMN convergence_rounds TEXT"
            )

    @staticmethod
    def _ensure_compression_schema(conn: sqlite3.Connection) -> None:
        """Add context_compression_ratio column to cost_telemetry (plan 15)."""
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(cost_telemetry)").fetchall()
        }
        if "context_compression_ratio" not in cols:
            conn.execute(
                "ALTER TABLE cost_telemetry"
                " ADD COLUMN context_compression_ratio REAL DEFAULT NULL"
            )

    @staticmethod
    def _ensure_worker_sessions_schema(conn: sqlite3.Connection) -> None:
        """Create worker_sessions table (plan 10 persistent sessions)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_sessions (
                session_id   TEXT PRIMARY KEY,
                provider     TEXT NOT NULL,
                model        TEXT NOT NULL,
                pid          INTEGER,
                started_at   REAL NOT NULL,
                last_used_at REAL NOT NULL,
                status       TEXT NOT NULL DEFAULT 'active',
                token_count  INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_worker_sessions_status"
            " ON worker_sessions (status, last_used_at)"
        )

    @staticmethod
    def _ensure_bandit_schema(conn: sqlite3.Connection) -> None:
        """Create routing_decisions table for contextual bandit (plan 11)."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS routing_decisions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id        TEXT NOT NULL,
                features       TEXT NOT NULL,
                heuristic_pick TEXT NOT NULL,
                bandit_pick    TEXT NOT NULL,
                chosen         TEXT NOT NULL,
                outcome_score  REAL,
                regret         REAL,
                ts             REAL NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_routing_decisions_ts"
            " ON routing_decisions (ts)"
        )

    @staticmethod
    def _ensure_resilience_schema(conn: sqlite3.Connection) -> None:
        """Add provider_health table and resilience telemetry columns."""
        # provider_health: per-provider circuit-breaker and health state
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provider_health (
                provider_id           TEXT PRIMARY KEY,
                state                 TEXT NOT NULL DEFAULT 'HEALTHY',
                consecutive_failures  INTEGER NOT NULL DEFAULT 0,
                last_failure_ts       REAL,
                last_failure_category TEXT,
                last_failure_stderr   TEXT,
                quarantine_until_ts   REAL,
                last_probe_ts         REAL,
                last_probe_ok         INTEGER,
                updated_ts            REAL NOT NULL
            )
        """)

        # Resilience telemetry columns (additive — safe on old DBs)
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(telemetry)").fetchall()
        }
        resilience_migrations = {
            "retry_count": (
                "ALTER TABLE telemetry ADD COLUMN retry_count INTEGER DEFAULT 0"
            ),
            "error_category": (
                "ALTER TABLE telemetry ADD COLUMN error_category TEXT"
            ),
            "exit_code": (
                "ALTER TABLE telemetry ADD COLUMN exit_code INTEGER"
            ),
            "stderr_snippet": (
                "ALTER TABLE telemetry ADD COLUMN stderr_snippet TEXT"
            ),
            "fallback_chain_depth": (
                "ALTER TABLE telemetry ADD COLUMN fallback_chain_depth INTEGER DEFAULT 0"
            ),
            "circuit_state_at_call": (
                "ALTER TABLE telemetry ADD COLUMN circuit_state_at_call TEXT"
            ),
        }
        for column, statement in resilience_migrations.items():
            if column not in existing:
                conn.execute(statement)

    def _project_setting_defaults(self) -> dict[str, int | bool]:
        global _PROJECT_SETTING_DEFAULTS_CACHE
        if _PROJECT_SETTING_DEFAULTS_CACHE is None:
            cfg = TGsConfig()
            _PROJECT_SETTING_DEFAULTS_CACHE = {
                "learning_enabled": False,
                "concurrency_limit": int(cfg.parallelism.max_workers),
                "budget_hard_cap_tokens": int(cfg.budgets.default_hard_cap_tokens),
                "fanout_cap": int(DEFAULT_PROJECT_FANOUT_CAP),
                "pending_approval_limit": DEFAULT_PROJECT_PENDING_APPROVAL_LIMIT,
            }
        return dict(_PROJECT_SETTING_DEFAULTS_CACHE)

    def _ensure_project_settings_row(
        self,
        conn: sqlite3.Connection,
        project_path: str,
    ) -> None:
        defaults = self._project_setting_defaults()
        conn.execute(
            """
            INSERT INTO project_settings
                (project_path, concurrency_limit, budget_hard_cap_tokens,
                 fanout_cap, pending_approval_limit, allow_out_of_workspace_writes, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_path) DO NOTHING
            """,
            (
                project_path,
                int(defaults.get("concurrency_limit", 0)),
                int(defaults.get("budget_hard_cap_tokens", 0)),
                int(defaults.get("fanout_cap", 0)),
                int(defaults.get("pending_approval_limit", 0)),
                0,  # allow_out_of_workspace_writes default
                time.time(),
            ),
        )

    def get_project_settings(self, project_path: str) -> dict[str, object]:
        """Return persisted guardrail-level operator settings for one project."""
        settings = self._project_setting_defaults()
        if not project_path:
            return settings

        with self.conn() as conn:
            row = conn.execute(
                """
                WITH project(project_path) AS (VALUES (?))
                SELECT s.concurrency_limit,
                       s.budget_hard_cap_tokens,
                       s.fanout_cap,
                       s.pending_approval_limit,
                       r.learning_enabled,
                       s.allow_out_of_workspace_writes
                FROM project
                LEFT JOIN project_settings AS s
                    ON s.project_path = project.project_path
                LEFT JOIN project_routing AS r
                    ON r.project_path = project.project_path
                """,
                (project_path,),
            ).fetchone()

        if row is not None and row[0] is not None:
            settings.update({
                "concurrency_limit": int(row[0]),
                "budget_hard_cap_tokens": int(row[1]),
                "fanout_cap": int(row[2]),
                "pending_approval_limit": int(row[3]),
            })
        if row is not None and row[4] is not None:
            settings["learning_enabled"] = bool(row[4])
        if row is not None and len(row) > 5 and row[5] is not None:
            settings["allow_out_of_workspace_writes"] = bool(row[5])
        settings["project_path"] = project_path
        return settings

    def set_project_setting(
        self,
        project_path: str,
        key: str,
        value: int | bool,
    ) -> dict[str, object]:
        """Persist one SQLite-backed operator control for a project."""
        if not project_path:
            raise ValueError("project_path is required")
        if key not in PROJECT_SETTING_KEYS:
            raise ValueError(f"unknown project setting: {key}")

        with self.conn() as conn:
            if key == "learning_enabled":
                row = conn.execute(
                    "SELECT overrides_json FROM project_routing WHERE project_path = ?",
                    (project_path,),
                ).fetchone()
                overrides_json = (
                    row[0]
                    if row and row[0]
                    else self._default_project_routing_overrides()
                )
                conn.execute(
                    """
                    INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project_path) DO UPDATE SET
                        overrides_json = excluded.overrides_json,
                        learning_enabled = excluded.learning_enabled,
                        ts = excluded.ts
                    """,
                    (project_path, overrides_json, int(bool(value)), time.time()),
                )
            else:
                self._ensure_project_settings_row(conn, project_path)
                column = PROJECT_SETTING_COLUMNS[key]
                conn.execute(
                    f"UPDATE project_settings SET {column} = ?, ts = ? WHERE project_path = ?",
                    (int(value), time.time(), project_path),
                )

        return self.get_project_settings(project_path)

    def reset_project_setting(
        self,
        project_path: str,
        key: str | None = None,
    ) -> dict[str, object]:
        """Reset one or all operator-facing project settings to defaults."""
        if not project_path:
            raise ValueError("project_path is required")
        if key is not None and key not in PROJECT_SETTING_KEYS:
            raise ValueError(f"unknown project setting: {key}")

        defaults = self._project_setting_defaults()
        with self.conn() as conn:
            if key in (None, "learning_enabled"):
                row = conn.execute(
                    "SELECT overrides_json FROM project_routing WHERE project_path = ?",
                    (project_path,),
                ).fetchone()
                overrides_json = (
                    row[0]
                    if row and row[0]
                    else self._default_project_routing_overrides()
                )
                conn.execute(
                    """
                    INSERT INTO project_routing (project_path, overrides_json, learning_enabled, ts)
                    VALUES (?, ?, 0, ?)
                    ON CONFLICT(project_path) DO UPDATE SET
                        overrides_json = excluded.overrides_json,
                        learning_enabled = excluded.learning_enabled,
                        ts = excluded.ts
                    """,
                    (project_path, overrides_json, time.time()),
                )

            if key is None:
                conn.execute(
                    "DELETE FROM project_settings WHERE project_path = ?",
                    (project_path,),
                )
            elif key != "learning_enabled":
                self._ensure_project_settings_row(conn, project_path)
                column = PROJECT_SETTING_COLUMNS[key]
                conn.execute(
                    f"UPDATE project_settings SET {column} = ?, ts = ? WHERE project_path = ?",
                    (int(defaults[key]), time.time(), project_path),
                )

        return self.get_project_settings(project_path)

    def list_pending_approvals(
        self,
        project_path: str,
        limit: int = 25,
    ) -> list[dict[str, object]]:
        """Return a compact pending-approvals list for one project."""
        if not project_path:
            return []

        bounded_limit = max(1, min(int(limit), 100))
        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT id, draft_fingerprint, draft_name, status, review_note,
                       canonical_id, created_at, updated_at
                FROM approval_queue
                WHERE project_path = ? AND status = 'pending'
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (project_path, bounded_limit),
            ).fetchall()

        return [
            {
                "id": int(row[0]),
                "fingerprint": row[1],
                "name": row[2],
                "status": row[3],
                "review_note": row[4],
                "canonical_id": row[5],
                "created_at": row[6],
                "updated_at": row[7],
            }
            for row in rows
        ]

    def _get_legacy_conn(self) -> sqlite3.Connection:
        """Compatibility escape hatch using one cached connection per thread."""
        thread_id = threading.get_ident()
        conn = self._legacy_conns.get(thread_id)
        if conn is not None:
            return conn
        with self._legacy_conn_lock:
            conn = self._legacy_conns.get(thread_id)
            if conn is None:
                conn = self._connect()
                self._legacy_conns[thread_id] = conn
            return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._get_legacy_conn()

    # ------------------------------------------------------------------
    # SQLite hardening: integrity check, recovery, backup, close
    # ------------------------------------------------------------------

    @property
    def last_backup_ts(self) -> float | None:
        return self._last_backup_ts

    @property
    def last_integrity_ok(self) -> bool | None:
        return self._last_integrity_ok

    def _check_integrity_and_recover(self) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=5)
            try:
                row = conn.execute("PRAGMA integrity_check(1)").fetchone()
                self._last_integrity_ok = row is not None and row[0] == "ok"
            finally:
                conn.close()
            if self._last_integrity_ok:
                return
        except Exception:
            self._last_integrity_ok = False
            log.debug("integrity pre-check error", exc_info=True)
        log.warning("DB integrity check failed at %s — attempting auto-recovery", self._db_path)
        self._recover_db()

    def _recover_db(self) -> None:
        import glob as _glob
        pattern = str(self._db_path) + ".bak.*"
        candidates = sorted(
            _glob.glob(pattern),
            key=lambda p: (p.rsplit(".", 1)[-1].isdigit(), p),
            reverse=True,
        )
        for candidate in candidates:
            try:
                conn = sqlite3.connect(candidate, timeout=5)
                try:
                    row = conn.execute("PRAGMA integrity_check(1)").fetchone()
                    valid = row is not None and row[0] == "ok"
                finally:
                    conn.close()
                if valid:
                    os.replace(candidate, self._db_path)
                    self._last_integrity_ok = True
                    log.warning("DB recovered from backup %s", candidate)
                    return
                else:
                    os.unlink(candidate)
                    log.warning("Discarded invalid backup %s", candidate)
            except Exception:
                log.debug("Recovery candidate %s failed", candidate, exc_info=True)
        try:
            os.unlink(self._db_path)
            log.warning("No valid backup found — deleted corrupt DB, will recreate on next connect")
        except Exception:
            log.debug("Could not delete corrupt DB", exc_info=True)
        self._last_integrity_ok = None

    def backup_db(self) -> Path | None:
        backup_path = self._db_path.with_name(
            self._db_path.name + f".bak.{int(time.time())}"
        )
        try:
            src = sqlite3.connect(str(self._db_path), timeout=10)
            try:
                dst = sqlite3.connect(str(backup_path), timeout=10)
                try:
                    src.backup(dst, pages=100)
                    self._last_backup_ts = time.time()
                    self._ensure_private_db_file(backup_path)
                finally:
                    dst.close()
            finally:
                src.close()
            self._prune_old_backups(keep=self._backup_keep)
            log.debug("DB backup written to %s", backup_path)
            return backup_path
        except Exception:
            log.warning("DB backup failed", exc_info=True)
            return None

    def _prune_old_backups(self, keep: int = 3) -> None:
        import glob as _glob
        pattern = str(self._db_path) + ".bak.*"
        candidates = sorted(
            _glob.glob(pattern),
            key=lambda p: (p.rsplit(".", 1)[-1].isdigit(), p),
            reverse=True,
        )
        for old in candidates[keep:]:
            try:
                os.unlink(old)
                log.debug("Pruned old backup %s", old)
            except Exception:
                log.debug("Could not prune %s", old, exc_info=True)

    def close(self) -> None:
        for conn in list(self._legacy_conns.values()):
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
            except Exception:
                log.debug("close: legacy conn cleanup failed", exc_info=True)
        self._legacy_conns.clear()
        tl_conn = getattr(self._thread_local, "conn", None)
        if tl_conn is not None:
            try:
                tl_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                tl_conn.close()
            except Exception:
                log.debug("close: thread-local conn cleanup failed", exc_info=True)
            try:
                del self._thread_local.conn
            except AttributeError:
                pass

    # ------------------------------------------------------------------
    # Result cache (preserves original Cache interface)
    # ------------------------------------------------------------------

    @staticmethod
    def _key(task: str) -> str:
        """Normalise and hash a task string."""
        normalised = " ".join(task.lower().split())
        return hashlib.sha256(normalised.encode()).hexdigest()[:32]

    def cache_get(self, task: str) -> tuple[str, str] | None:
        """Return (result, model) if cached and not expired."""
        key = self._key(task)
        with self.conn() as conn:
            row = conn.execute(
                "SELECT result, model, ts FROM cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            result, model, ts = row
            if time.time() - ts > self._result_ttl:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
                return None
            return result, model

    def cache_put(self, task: str, result: str, model: str) -> None:
        """Store a task result."""
        key = self._key(task)
        with self.conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO cache (key, task, result, model, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, task, result, model, time.time()),
            )

    def cache_stats(self) -> dict:
        """Return cache statistics."""
        now = time.time()
        with self.conn() as conn:
            active_rows = conn.execute(
                "SELECT model, COUNT(*) FROM cache "
                "WHERE ? - ts <= ? "
                "GROUP BY model",
                (now, self._result_ttl),
            ).fetchall()
            expired_row = conn.execute(
                "SELECT COUNT(*) FROM cache WHERE ? - ts > ?",
                (now, self._result_ttl),
            ).fetchone()
        by_model = {model: count for model, count in active_rows}
        active = sum(by_model.values())
        expired = expired_row[0] if expired_row else 0
        return {"total_cached": active, "expired": expired, "by_model": by_model}

    def cache_clear(self) -> int:
        with self.conn() as conn:
            cursor = conn.execute("DELETE FROM cache")
            return cursor.rowcount

    def cache_prune(self) -> int:
        with self.conn() as conn:
            cursor = conn.execute(
                "DELETE FROM cache WHERE ? - ts > ?",
                (time.time(), self._result_ttl),
            )
            return cursor.rowcount

    @staticmethod
    def _routing_guard_key(caller: str, cwd: str) -> str:
        return hashlib.sha256(f"{caller}\0{cwd}".encode()).hexdigest()[:32]

    @staticmethod
    def _normalize_routing_guard_cwd(cwd: str | None) -> str:
        return str(cwd or "").strip()

    def routing_guard_purge_expired(self) -> int:
        with self.conn() as conn:
            cursor = conn.execute(
                "DELETE FROM routing_guards WHERE expires_ts <= ?",
                (time.time(),),
            )
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Plan cache
    # ------------------------------------------------------------------

    @staticmethod
    def _plan_key(task: str) -> str:
        """Structural hash — normalise whitespace, lowercase, strip variable names."""
        import re

        normalised = " ".join(task.lower().split())
        # Strip quoted strings (variable names, file paths)
        normalised = re.sub(r'"[^"]*"', '""', normalised)
        normalised = re.sub(r"'[^']*'", "''", normalised)
        # Strip specific file paths but keep the pattern
        normalised = re.sub(r"\b[\w./]+\.\w{1,4}\b", "<file>", normalised)
        return hashlib.sha256(normalised.encode()).hexdigest()[:32]

    def plan_get(self, task: str) -> dict | None:
        """Return cached plan if found and not expired."""
        key = self._plan_key(task)
        with self.conn() as conn:
            row = conn.execute(
                "SELECT plan_json, topology, plan_schema_version, ts FROM plan_cache WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            plan_json, topology, _plan_schema_version, ts = row
            if time.time() - ts > self._plan_ttl:
                conn.execute("DELETE FROM plan_cache WHERE key = ?", (key,))
                return None
        try:
            plan = json.loads(plan_json)
        except json.JSONDecodeError:
            return None
        if isinstance(plan, dict) and topology is not None and "topology" not in plan:
            plan["topology"] = topology
        return plan

    def plan_put(self, task: str, plan: dict, model: str) -> None:
        """Cache a plan decomposition."""
        key = self._plan_key(task)
        topology = None
        plan_schema_version = 1
        if isinstance(plan, dict):
            raw_topology = plan.get("topology")
            if isinstance(raw_topology, str) and raw_topology.strip():
                topology = raw_topology.strip()
            raw_schema_version = plan.get("plan_schema_version", 1)
            try:
                plan_schema_version = int(raw_schema_version)
            except (TypeError, ValueError):
                plan_schema_version = 1
        try:
            plan_json = json.dumps(plan)
        except TypeError as exc:
            raise TypeError("plan must be JSON-serializable") from exc
        with self.conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO plan_cache "
                "(key, task_hash, plan_json, model, topology, plan_schema_version, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    self._key(task),
                    plan_json,
                    model,
                    topology,
                    plan_schema_version,
                    time.time(),
                ),
            )

    # ------------------------------------------------------------------
    # Artifact bus
    # ------------------------------------------------------------------

    @staticmethod
    def _artifact_key(
        execution_id: str,
        plan_revision: int,
        wave: int,
        subtask_id: str,
        artifact_type: str,
    ) -> str:
        raw = "::".join(
            (
                execution_id,
                str(plan_revision),
                str(wave),
                subtask_id,
                artifact_type,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
        # Use the pragma_table_info table-valued function (SQLite 3.16+) which
        # accepts a bound parameter — avoids any string formatting in SQL.
        rows = conn.execute(
            "SELECT * FROM pragma_table_info(?)", (table_name,)
        ).fetchall()
        return {str(row[1]) for row in rows}

    def _ensure_parent_scoped_schema(self, conn: sqlite3.Connection) -> None:
        """Create additive hierarchical artifact schema seams for D-01..D-08."""
        artifact_columns = self._column_names(conn, "artifacts")
        if "parent_execution_id" not in artifact_columns:
            conn.execute("ALTER TABLE artifacts ADD COLUMN parent_execution_id TEXT")
        if "producer_subtask_id" not in artifact_columns:
            conn.execute("ALTER TABLE artifacts ADD COLUMN producer_subtask_id TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_artifacts_parent_scope
                ON artifacts (
                    execution_id,
                    plan_revision,
                    parent_execution_id,
                    artifact_type,
                    wave,
                    created_at,
                    stable_ref
                )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_artifacts_parent_latest
                ON artifacts (execution_id, parent_execution_id, plan_revision DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_artifacts_latest_by_producer
                ON artifacts (
                    execution_id,
                    artifact_type,
                    COALESCE(NULLIF(producer_subtask_id, ''), subtask_id),
                    plan_revision DESC,
                    wave DESC,
                    created_at DESC,
                    stable_ref ASC
                )
            """
        )

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS degradation_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL,
                parent_subtask_id TEXT NOT NULL,
                missing_artifact_type TEXT NOT NULL,
                affected_child_subtask_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_degradation_events_execution
                ON degradation_events (execution_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_degradation_events_unique
                ON degradation_events (
                    execution_id,
                    parent_subtask_id,
                    missing_artifact_type,
                    affected_child_subtask_id,
                    reason
                );

            CREATE TABLE IF NOT EXISTS artifact_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id TEXT NOT NULL,
                child_subtask_id TEXT NOT NULL,
                artifact_ref TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_artifact_bindings_child
                ON artifact_bindings (execution_id, child_subtask_id, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_bindings_unique
                ON artifact_bindings (execution_id, child_subtask_id, artifact_ref);
        """)
        artifact_binding_columns = self._column_names(conn, "artifact_bindings")
        if "plan_revision" not in artifact_binding_columns:
            conn.execute("ALTER TABLE artifact_bindings ADD COLUMN plan_revision INTEGER")
        if "parent_execution_id" not in artifact_binding_columns:
            conn.execute("ALTER TABLE artifact_bindings ADD COLUMN parent_execution_id TEXT")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_artifact_bindings_scope
                ON artifact_bindings (
                    execution_id,
                    child_subtask_id,
                    plan_revision,
                    parent_execution_id,
                    created_at
                )
            """
        )

    @staticmethod
    def _coerce_length_chars(value: object, default: int) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return int(value)
            except ValueError:
                return default
        return default

    @staticmethod
    def _coerce_compact_summary(
        compact_summary: str | dict[str, object],
        stable_ref: str,
    ) -> str:
        if isinstance(compact_summary, str):
            summary_text = compact_summary
            payload = {
                "summary_text": summary_text,
                "length_chars": len(summary_text),
                "artifact_ref": stable_ref,
            }
            return json.dumps(payload)

        if not isinstance(compact_summary, dict):
            raise TypeError("compact_summary must be a string or dict")

        summary_text = str(compact_summary.get("summary_text", ""))
        raw_length = compact_summary.get("length_chars", len(summary_text))
        payload = {
            "summary_text": summary_text,
            "length_chars": Database._coerce_length_chars(raw_length, len(summary_text)),
            "artifact_ref": str(compact_summary.get("artifact_ref", stable_ref)),
        }
        return json.dumps(payload)

    @staticmethod
    def _parse_compact_summary(
        compact_summary: str | None,
        stable_ref: str,
    ) -> dict[str, object]:
        if compact_summary is None:
            return {
                "summary_text": "",
                "length_chars": 0,
                "artifact_ref": stable_ref,
            }
        try:
            parsed = json.loads(compact_summary)
        except json.JSONDecodeError:
            summary_text = compact_summary
            return {
                "summary_text": summary_text,
                "length_chars": len(summary_text),
                "artifact_ref": stable_ref,
            }

        if isinstance(parsed, dict):
            summary_text = str(parsed.get("summary_text", ""))
            raw_length = parsed.get("length_chars", len(summary_text))
            return {
                "summary_text": summary_text,
                "length_chars": Database._coerce_length_chars(raw_length, len(summary_text)),
                "artifact_ref": str(parsed.get("artifact_ref", stable_ref)),
            }

        summary_text = str(parsed)
        return {
            "summary_text": summary_text,
            "length_chars": len(summary_text),
            "artifact_ref": stable_ref,
        }

    def save_artifact(
        self,
        execution_id: str,
        plan_revision: int,
        wave: int,
        subtask_id: str,
        artifact_type: str,
        full_payload: str | dict[str, object],
        compact_summary: str | dict[str, object],
        parent_execution_id: str | None = None,
        producer_subtask_id: str | None = None,
        stable_ref: str | None = None,
    ) -> str:
        """Persist one artifact and optional parent-scoped metadata additively."""
        artifact_id = self._artifact_key(
            execution_id,
            plan_revision,
            wave,
            subtask_id,
            artifact_type,
        )
        stable_ref = stable_ref or f"artifact:{artifact_id}"
        if isinstance(full_payload, str):
            payload_text = full_payload
        else:
            try:
                payload_text = json.dumps(full_payload)
            except TypeError as exc:
                raise TypeError("artifact full_payload must be JSON-serializable") from exc

        summary_text = self._coerce_compact_summary(compact_summary, stable_ref)
        created_at = int(time.time())
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO artifacts
                    (id, execution_id, plan_revision, wave, subtask_id, artifact_type,
                     full_payload, compact_summary, stable_ref, size, created_at,
                     parent_execution_id, producer_subtask_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    execution_id,
                    int(plan_revision),
                    int(wave),
                    subtask_id,
                    artifact_type,
                    payload_text,
                    summary_text,
                    stable_ref,
                    len(payload_text.encode("utf-8")),
                    created_at,
                    parent_execution_id,
                    producer_subtask_id or subtask_id,
                ),
            )
        return stable_ref

    def query_artifacts(
        self,
        execution_id: str,
        plan_revision: int,
        wave: int | None = None,
        max_wave: int | None = None,
        artifact_types: list[str] | None = None,
    ) -> list[dict[str, object]]:
        clauses = ["execution_id = ?", "plan_revision = ?"]
        params: list[object] = [execution_id, int(plan_revision)]
        if wave is not None:
            clauses.append("wave = ?")
            params.append(int(wave))
        if max_wave is not None:
            clauses.append("wave <= ?")
            params.append(int(max_wave))
        if artifact_types:
            placeholders = ", ".join("?" for _ in artifact_types)
            clauses.append(f"artifact_type IN ({placeholders})")
            params.extend(artifact_types)

        query = (
            "SELECT execution_id, plan_revision, wave, subtask_id, artifact_type, "
            "compact_summary, stable_ref, size, created_at, "
            "parent_execution_id, producer_subtask_id "
            "FROM artifacts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY wave, subtask_id, artifact_type"
        )
        with self.conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        artifacts: list[dict[str, object]] = []
        for row in rows:
            (
                row_execution_id,
                row_plan_revision,
                row_wave,
                row_subtask_id,
                row_artifact_type,
                row_compact_summary,
                row_stable_ref,
                row_size,
                row_created_at,
                row_parent_execution_id,
                row_producer_subtask_id,
            ) = row
            artifacts.append(
                {
                    "execution_id": row_execution_id,
                    "plan_revision": row_plan_revision,
                    "wave": row_wave,
                    "subtask_id": row_subtask_id,
                    "artifact_type": row_artifact_type,
                    "compact_summary": self._parse_compact_summary(
                        row_compact_summary,
                        row_stable_ref,
                    ),
                    "stable_ref": row_stable_ref,
                    "size": row_size,
                    "created_at": row_created_at,
                    "parent_execution_id": row_parent_execution_id,
                    "producer_subtask_id": row_producer_subtask_id,
                }
            )
        return artifacts

    def get_parent_scoped_artifacts(
        self,
        execution_id: str,
        plan_revision: int,
        parent_execution_id: str,
        consumes: list[str],
    ) -> list[dict[str, object]]:
        """Return authoritative direct-parent artifact envelopes for D-01..D-08."""
        if not consumes:
            return []
        normalized_parent_execution_id = (
            parent_execution_id.strip() if isinstance(parent_execution_id, str) else ""
        )
        if not normalized_parent_execution_id:
            return []

        selected: list[dict[str, object]] = []
        with self.conn() as conn:
            for artifact_type in consumes:
                row = conn.execute(
                    """
                    SELECT compact_summary, stable_ref, producer_subtask_id, parent_execution_id
                    FROM artifacts
                    WHERE execution_id = ?
                      AND plan_revision = ?
                      AND parent_execution_id = ?
                      AND artifact_type = ?
                    ORDER BY wave DESC, created_at DESC, stable_ref ASC
                    LIMIT 1
                    """,
                    (
                        execution_id,
                        int(plan_revision),
                        normalized_parent_execution_id,
                        artifact_type,
                    ),
                ).fetchone()
                if row is None:
                    continue
                compact_summary, stable_ref, producer_subtask_id, stored_parent_execution_id = row
                compact_summary_dict = self._parse_compact_summary(compact_summary, str(stable_ref))
                selected.append(
                    make_artifact_envelope(
                        artifact_type,
                        compact_summary_dict,
                        producer_subtask_id=str(producer_subtask_id or ""),
                        parent_execution_id=str(stored_parent_execution_id or ""),
                    )
                )
        return selected

    def latest_artifact_plan_revision(
        self,
        execution_id: str,
        *,
        parent_execution_id: str | None = None,
    ) -> int | None:
        """Return the latest artifact plan revision for one execution scope."""
        clauses = ["execution_id = ?"]
        params: list[object] = [execution_id]
        normalized_parent_execution_id = (
            parent_execution_id.strip() if isinstance(parent_execution_id, str) else None
        )
        if normalized_parent_execution_id:
            clauses.append("parent_execution_id = ?")
            params.append(normalized_parent_execution_id)
        query = "SELECT MAX(plan_revision) FROM artifacts WHERE " + " AND ".join(clauses)
        with self.conn() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def log_degradation_event(
        self,
        execution_id: str,
        parent_subtask_id: str,
        missing_artifact_type: str,
        affected_child_subtask_id: str,
        reason: str,
    ) -> None:
        """Persist one hierarchical degradation event without full artifact payloads."""
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO degradation_events
                    (execution_id, parent_subtask_id, missing_artifact_type,
                     affected_child_subtask_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    parent_subtask_id,
                    missing_artifact_type,
                    affected_child_subtask_id,
                    reason,
                    int(time.time()),
                ),
            )

    def query_degradation_events(self, execution_id: str) -> list[dict[str, object]]:
        """Return persisted degradation events for one execution in stable order."""
        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT parent_subtask_id, missing_artifact_type,
                       affected_child_subtask_id, reason, created_at
                FROM degradation_events
                WHERE execution_id = ?
                ORDER BY created_at, id
                """,
                (execution_id,),
            ).fetchall()
        return [
            {
                "parent_subtask_id": row[0],
                "missing_artifact_type": row[1],
                "affected_child_subtask_id": row[2],
                "reason": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]

    def get_artifact_bindings(
        self,
        execution_id: str,
        child_subtask_id: str,
        *,
        plan_revision: int | None = None,
        parent_execution_id: str | None = None,
    ) -> list[str]:
        """Return existing snapshot bindings for one child subtask execution."""
        clauses = ["execution_id = ?", "child_subtask_id = ?"]
        params: list[object] = [execution_id, child_subtask_id]
        if plan_revision is not None:
            clauses.append("plan_revision = ?")
            params.append(int(plan_revision))
        normalized_parent_execution_id = (
            parent_execution_id.strip() if isinstance(parent_execution_id, str) else None
        )
        if normalized_parent_execution_id:
            clauses.append("parent_execution_id = ?")
            params.append(normalized_parent_execution_id)
        with self.conn() as conn:
            rows = conn.execute(
                (
                    """
                SELECT artifact_ref
                FROM artifact_bindings
                WHERE """
                    + " AND ".join(clauses)
                    + """
                ORDER BY created_at, id
                """
                ),
                tuple(params),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def save_artifact_bindings(
        self,
        execution_id: str,
        child_subtask_id: str,
        artifact_refs: list[str],
        *,
        plan_revision: int,
        parent_execution_id: str,
    ) -> None:
        """Persist snapshot bindings for one child subtask execution."""
        if not artifact_refs:
            return
        created_at = int(time.time())
        normalized_parent_execution_id = (
            parent_execution_id.strip() if isinstance(parent_execution_id, str) else ""
        )
        with self.conn() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO artifact_bindings
                    (execution_id, child_subtask_id, artifact_ref, created_at, plan_revision, parent_execution_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        execution_id,
                        child_subtask_id,
                        artifact_ref,
                        created_at,
                        int(plan_revision),
                        normalized_parent_execution_id,
                    )
                    for artifact_ref in artifact_refs
                ],
            )

    def get_artifacts_for_consumes(
        self,
        execution_id: str,
        plan_revision: int,
        consumes: list[str],
        upto_wave: int | None = None,
    ) -> list[dict[str, object]]:
        if not consumes:
            return []

        consume_order = {artifact_type: index for index, artifact_type in enumerate(consumes)}
        artifacts = self.query_artifacts(
            execution_id,
            plan_revision,
            max_wave=upto_wave,
            artifact_types=consumes,
        )
        artifacts.sort(
            key=lambda artifact: (
                consume_order.get(str(artifact.get("artifact_type", "")), len(consumes)),
                int(artifact.get("wave", 0)),
                str(artifact.get("subtask_id", "")),
                str(artifact.get("artifact_type", "")),
            )
        )

        envelopes: list[dict[str, object]] = []
        for artifact in artifacts:
            compact_summary = artifact.get("compact_summary")
            if not isinstance(compact_summary, dict):
                continue
            summary_text = str(compact_summary.get("summary_text", ""))
            raw_length = compact_summary.get("length_chars")
            envelopes.append(
                {
                    "artifact_type": artifact.get("artifact_type"),
                    "summary_text": summary_text,
                    "length_chars": self._coerce_length_chars(raw_length, len(summary_text)),
                    "artifact_ref": str(compact_summary.get("artifact_ref", "")),
                }
            )
        return envelopes

    def persist_swarm_run(self, swarm_run: Mapping[str, object]) -> None:
        """Insert or update one authoritative swarm run record."""
        swarm_id = str(swarm_run.get("swarm_id") or "").strip()
        if not swarm_id:
            raise ValueError("swarm_id is required")

        created_ts_raw = swarm_run.get("created_ts", time.time())
        created_ts = (
            time.time()
            if created_ts_raw is None
            else self._coerce_swarm_float(created_ts_raw, field_name="created_ts")
        )
        progress_counters = self._serialize_json_field(
            swarm_run.get("progress_counters"),
            default="{}",
        )
        resumable = 1 if _coerce_db_bool(swarm_run.get("resumable")) else 0
        resume_status = str(
            swarm_run.get("resume_status")
            or ("resumable" if resumable else "not_resumable")
        )
        parent_swarm_id = str(swarm_run.get("parent_swarm_id") or "").strip() or None
        chosen_checkpoint_index_raw = swarm_run.get("chosen_checkpoint_index")
        chosen_checkpoint_index = None
        if chosen_checkpoint_index_raw is not None:
            chosen_checkpoint_index = self._coerce_swarm_int(
                chosen_checkpoint_index_raw,
                field_name="chosen_checkpoint_index",
            )
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO swarm_runs (
                    swarm_id,
                    task_hash,
                    created_ts,
                    status,
                    requested_agents,
                    effective_agents,
                    progress_counters,
                    cost_summary_ref,
                    topology,
                    round,
                    resumable,
                    resume_status,
                    parent_swarm_id,
                    chosen_checkpoint_index
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(swarm_id) DO UPDATE SET
                    task_hash = excluded.task_hash,
                    status = excluded.status,
                    requested_agents = excluded.requested_agents,
                    effective_agents = excluded.effective_agents,
                    progress_counters = excluded.progress_counters,
                    cost_summary_ref = excluded.cost_summary_ref,
                    topology = excluded.topology,
                    round = excluded.round,
                    resumable = excluded.resumable,
                    resume_status = excluded.resume_status,
                    parent_swarm_id = excluded.parent_swarm_id,
                    chosen_checkpoint_index = excluded.chosen_checkpoint_index
                """,
                (
                    swarm_id,
                    str(swarm_run.get("task_hash") or ""),
                    created_ts,
                    str(swarm_run.get("status") or "planned"),
                    self._coerce_swarm_int(
                        swarm_run.get("requested_agents", 0),
                        field_name="requested_agents",
                    ),
                    self._coerce_swarm_int(
                        swarm_run.get("effective_agents", 0),
                        field_name="effective_agents",
                    ),
                    progress_counters,
                    swarm_run.get("cost_summary_ref"),
                    swarm_run.get("topology"),
                    self._coerce_swarm_int(
                        swarm_run.get("round", 0),
                        field_name="round",
                    ),
                    resumable,
                    resume_status,
                    parent_swarm_id,
                    chosen_checkpoint_index,
                ),
            )

    @staticmethod
    def _coerce_coordinator_verdict(value: object) -> str:
        verdict = str(value or "").strip().lower()
        if verdict not in {"complete", "another-pass", "fallback"}:
            raise ValueError(
                "verdict must be one of: complete, another-pass, fallback"
            )
        return verdict

    @staticmethod
    def _compact_checkpoint_artifact_summary(summary: object) -> dict[str, object]:
        if not isinstance(summary, Mapping):
            return {}
        compact: dict[str, object] = {}
        allowed_keys = {
            "artifact_type",
            "summary_text",
            "length_chars",
            "artifact_ref",
            "producer_subtask_id",
        }
        blocked_keys = {"payload", "content", "full_payload", "artifact_payload"}
        for key in allowed_keys:
            if key not in summary or key in blocked_keys:
                continue
            value = summary[key]
            if key == "length_chars":
                compact[key] = Database._coerce_swarm_int(
                    value,
                    field_name="length_chars",
                )
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact[key] = value
        if "artifact_ref" not in compact and "artifact_ref" in summary:
            compact["artifact_ref"] = str(summary.get("artifact_ref", ""))
        if "producer_subtask_id" in compact:
            compact["producer_subtask_id"] = str(compact.get("producer_subtask_id", ""))
        if "artifact_type" in compact:
            compact["artifact_type"] = str(compact.get("artifact_type", ""))
        if "summary_text" in compact:
            compact["summary_text"] = str(compact.get("summary_text", ""))
        return compact

    def _coordinator_checkpoint_from_row(
        self,
        row: sqlite3.Row | tuple[object, ...],
    ) -> dict[str, object]:
        return {
            "swarm_id": row[0],
            "plan_revision": int(row[1]),
            "round_index": int(row[2]),
            "coordinator_subtask_id": row[3],
            "verdict": row[4],
            "amendment": self._parse_json_field(row[5], default={}),
            "next_work": self._parse_json_field(row[6], default={}),
            "synthesis_summary": self._parse_json_field(row[7], default={}),
            "artifact_refs": self._parse_json_field(row[8], default=[]),
            "artifact_summaries": self._parse_json_field(row[9], default=[]),
            "round_counters": self._parse_json_field(row[10], default={}),
            "fallback_reason": row[11],
            "created_ts": float(row[12]),
        }

    def persist_coordinator_round_checkpoint(
        self,
        checkpoint: Mapping[str, object],
    ) -> None:
        swarm_id = str(checkpoint.get("swarm_id") or "").strip()
        if not swarm_id:
            raise ValueError("swarm_id is required")
        plan_revision = self._coerce_swarm_int(
            checkpoint.get("plan_revision", 1),
            field_name="plan_revision",
        )
        round_index = self._coerce_swarm_int(
            checkpoint.get("round_index"),
            field_name="round_index",
        )
        if round_index < 1:
            raise ValueError("round_index must be >= 1")
        coordinator_subtask_id = str(
            checkpoint.get("coordinator_subtask_id") or ""
        ).strip()
        if not coordinator_subtask_id:
            raise ValueError("coordinator_subtask_id is required")
        verdict = self._coerce_coordinator_verdict(checkpoint.get("verdict"))
        artifact_refs_value = checkpoint.get("artifact_refs") or []
        if not isinstance(artifact_refs_value, list):
            raise TypeError("artifact_refs must be a list")
        artifact_refs = [
            str(ref).strip()
            for ref in artifact_refs_value
            if str(ref).strip()
        ]
        artifact_summaries_value = checkpoint.get("artifact_summaries") or []
        if not isinstance(artifact_summaries_value, list):
            raise TypeError("artifact_summaries must be a list")
        artifact_summaries = [
            self._compact_checkpoint_artifact_summary(summary)
            for summary in artifact_summaries_value
            if isinstance(summary, Mapping)
        ]
        round_counters_value = checkpoint.get("round_counters") or {}
        if not isinstance(round_counters_value, Mapping):
            raise TypeError("round_counters must be a mapping")
        created_ts = (
            time.time()
            if checkpoint.get("created_ts") is None
            else self._coerce_swarm_float(
                checkpoint.get("created_ts"),
                field_name="created_ts",
            )
        )
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO coordinator_round_checkpoints (
                    swarm_id,
                    plan_revision,
                    round_index,
                    coordinator_subtask_id,
                    verdict,
                    amendment_json,
                    next_work_json,
                    synthesis_summary_json,
                    artifact_refs_json,
                    artifact_summaries_json,
                    round_counters_json,
                    fallback_reason,
                    created_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(swarm_id, plan_revision, round_index) DO UPDATE SET
                    coordinator_subtask_id = excluded.coordinator_subtask_id,
                    verdict = excluded.verdict,
                    amendment_json = excluded.amendment_json,
                    next_work_json = excluded.next_work_json,
                    synthesis_summary_json = excluded.synthesis_summary_json,
                    artifact_refs_json = excluded.artifact_refs_json,
                    artifact_summaries_json = excluded.artifact_summaries_json,
                    round_counters_json = excluded.round_counters_json,
                    fallback_reason = excluded.fallback_reason
                """,
                (
                    swarm_id,
                    plan_revision,
                    round_index,
                    coordinator_subtask_id,
                    verdict,
                    self._serialize_json_field(checkpoint.get("amendment"), default=None),
                    self._serialize_json_field(checkpoint.get("next_work"), default=None),
                    self._serialize_json_field(
                        checkpoint.get("synthesis_summary"),
                        default={},
                    ),
                    self._serialize_json_field(artifact_refs, default=[]),
                    self._serialize_json_field(artifact_summaries, default=[]),
                    self._serialize_json_field(round_counters_value, default={}),
                    str(checkpoint.get("fallback_reason") or "").strip() or None,
                    created_ts,
                ),
            )

    def list_coordinator_round_checkpoints(
        self,
        swarm_id: str,
        *,
        plan_revision: int | None = None,
    ) -> list[dict[str, object]]:
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        query = """
            SELECT swarm_id, plan_revision, round_index, coordinator_subtask_id,
                   verdict, amendment_json, next_work_json, synthesis_summary_json,
                   artifact_refs_json, artifact_summaries_json, round_counters_json,
                   fallback_reason, created_ts
            FROM coordinator_round_checkpoints
            WHERE swarm_id = ?
        """
        params: list[object] = [normalized_swarm_id]
        if plan_revision is not None:
            query += " AND plan_revision = ?"
            params.append(
                self._coerce_swarm_int(plan_revision, field_name="plan_revision")
            )
        query += " ORDER BY plan_revision, round_index"
        with self.conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        return [self._coordinator_checkpoint_from_row(row) for row in rows]

    def summarize_coordinator_round_metrics(
        self,
        swarm_id: str,
        *,
        plan_revision: int | None = None,
    ) -> list[dict[str, object]]:
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        query = """
            SELECT verdict, artifact_refs_json, round_counters_json, fallback_reason
            FROM coordinator_round_checkpoints
            WHERE swarm_id = ?
        """
        params: list[object] = [normalized_swarm_id]
        if plan_revision is not None:
            query += " AND plan_revision = ?"
            params.append(
                self._coerce_swarm_int(plan_revision, field_name="plan_revision")
            )
        query += " ORDER BY plan_revision, round_index"
        with self.conn() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        summaries: list[dict[str, object]] = []
        for verdict, artifact_refs_json, round_counters_json, fallback_reason in rows:
            artifact_refs = self._parse_json_field(artifact_refs_json, default=[])
            artifact_count = (
                sum(1 for ref in artifact_refs if str(ref).strip())
                if isinstance(artifact_refs, list)
                else 0
            )
            if artifact_count == 0:
                round_counters = self._parse_json_field(round_counters_json, default={})
                if isinstance(round_counters, dict):
                    try:
                        artifact_count = max(
                            int(round_counters.get("artifacts_consumed", 0)),
                            0,
                        )
                    except (TypeError, ValueError):
                        artifact_count = 0
            summaries.append(
                {
                    "verdict": str(verdict or ""),
                    "fallback_reason": str(fallback_reason or ""),
                    "artifact_count": artifact_count,
                }
            )
        return summaries

    def get_latest_completed_coordinator_checkpoint(
        self,
        swarm_id: str,
        *,
        plan_revision: int | None = None,
    ) -> dict[str, object] | None:
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        query = """
            SELECT swarm_id, plan_revision, round_index, coordinator_subtask_id,
                   verdict, amendment_json, next_work_json, synthesis_summary_json,
                   artifact_refs_json, artifact_summaries_json, round_counters_json,
                   fallback_reason, created_ts
            FROM coordinator_round_checkpoints
            WHERE swarm_id = ? AND COALESCE(verdict, '') != ''
        """
        params: list[object] = [normalized_swarm_id]
        if plan_revision is not None:
            query += " AND plan_revision = ?"
            params.append(
                self._coerce_swarm_int(plan_revision, field_name="plan_revision")
            )
        query += " ORDER BY plan_revision DESC, round_index DESC, id DESC LIMIT 1"
        with self.conn() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        if row is None:
            return None
        return self._coordinator_checkpoint_from_row(row)

    def get_latest_fallback_ready_coordinator_checkpoint(
        self,
        swarm_id: str,
        *,
        plan_revision: int | None = None,
    ) -> dict[str, object] | None:
        return self.get_latest_completed_coordinator_checkpoint(
            swarm_id,
            plan_revision=plan_revision,
        )

    def persist_worker_snapshot(
        self,
        swarm_id: str,
        worker_index: int,
        snapshot_json: str | Mapping[str, object],
        snapshot_ref: str | None = None,
        *,
        ts: float | None = None,
    ) -> str:
        """Persist one per-worker snapshot for inspect-only recovery scaffolding."""
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        payload_text = self._serialize_json_field(snapshot_json, default="{}")
        if payload_text is None:
            payload_text = "{}"
        stable_ref = snapshot_ref or self._swarm_snapshot_ref(payload_text)
        timestamp = (
            time.time()
            if ts is None
            else self._coerce_swarm_float(ts, field_name="ts")
        )
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO swarm_workers (
                    swarm_id,
                    worker_index,
                    worker_snapshot_ref,
                    snapshot_json,
                    ts
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_swarm_id,
                    self._coerce_swarm_int(worker_index, field_name="worker_index"),
                    stable_ref,
                    payload_text,
                    timestamp,
                ),
            )
        return stable_ref

    def log_swarm_event(
        self,
        swarm_id: str,
        event_type: str,
        payload: Mapping[str, object] | str | None,
        *,
        ts: float | None = None,
    ) -> None:
        """Persist one swarm audit/telemetry event."""
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        payload_text = self._serialize_json_field(payload, default="{}")
        if payload_text is None:
            payload_text = "{}"
        timestamp = (
            time.time()
            if ts is None
            else self._coerce_swarm_float(ts, field_name="ts")
        )
        try:
            _secret = self._get_audit_secret()
        except Exception:
            _secret = b""
        with self.conn() as conn:
            _prev = self.get_prev_chain_hmac(conn, "swarm_events")
            _chain = self._compute_chain_hmac(_secret, _prev, payload_text) if _secret else ""
            conn.execute(
                "INSERT INTO swarm_events (swarm_id, event_type, payload, ts, chain_hmac)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    normalized_swarm_id,
                    str(event_type or "event"),
                    payload_text,
                    timestamp,
                    _chain,
                ),
            )

    def _maybe_prune_preview_tokens(
        self,
        *,
        now: float,
        force: bool = False,
    ) -> None:
        if not force and now - self._preview_token_last_prune_ts < _PREVIEW_TOKEN_PRUNE_INTERVAL_SECONDS:
            return
        with self.conn() as conn:
            conn.execute(
                """
                DELETE FROM preview_tokens
                WHERE expires_ts <= ?
                """,
                (now,),
            )
        self._preview_token_last_prune_ts = now

    def get_latest_swarm_event_payload(
        self,
        swarm_id: str,
        event_type: str,
    ) -> dict[str, object] | None:
        """Return the latest decoded payload for one swarm event type."""
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        normalized_event_type = str(event_type or "").strip()
        if not normalized_event_type:
            raise ValueError("event_type is required")
        with self.conn() as conn:
            row = conn.execute(
                """
                SELECT payload
                FROM swarm_events
                WHERE swarm_id = ?
                  AND event_type = ?
                ORDER BY ts DESC, id DESC
                LIMIT 1
                """,
                (
                    normalized_swarm_id,
                    normalized_event_type,
                ),
            ).fetchone()
        if row is None:
            return None
        payload = self._parse_json_field(row[0], default={})
        if not isinstance(payload, Mapping):
            return None
        return {str(key): value for key, value in payload.items()}

    def persist_preview_token(self, token_hmac: str, swarm_id: str, expires_ts: float) -> bool:
        """Persist one single-use budget-preview token by HMAC only."""
        normalized_token_hmac = str(token_hmac or "").strip()
        if not normalized_token_hmac:
            raise ValueError("token_hmac is required")
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        expiry = self._coerce_swarm_float(expires_ts, field_name="expires_ts")
        if not math.isfinite(expiry):
            raise ValueError("expires_ts must be finite")
        now = time.time()
        try:
            with self.conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO preview_tokens (
                        token_hmac,
                        swarm_id,
                        expires_ts,
                        used
                    )
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(token_hmac) DO UPDATE SET
                        swarm_id = excluded.swarm_id,
                        expires_ts = excluded.expires_ts
                    WHERE preview_tokens.used = 0
                    """,
                    (
                        normalized_token_hmac,
                        normalized_swarm_id,
                        expiry,
                    ),
                )
                changed = conn.execute("SELECT changes()").fetchone()
                if not changed or int(changed[0] or 0) == 0:
                    return False
        except sqlite3.Error:
            log.warning("preview token persist failed", exc_info=True)
            return False
        self._maybe_prune_preview_tokens(now=now)
        return True

    def persist_preview_token_with_event(
        self,
        token_hmac: str,
        swarm_id: str,
        expires_ts: float,
        *,
        event_type: str,
        payload: object,
        ts: float | None = None,
    ) -> bool:
        """Atomically persist a preview token together with its matching swarm event."""
        normalized_token_hmac = str(token_hmac or "").strip()
        if not normalized_token_hmac:
            raise ValueError("token_hmac is required")
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        normalized_event_type = str(event_type or "").strip()
        if not normalized_event_type:
            raise ValueError("event_type is required")
        expiry = self._coerce_swarm_float(expires_ts, field_name="expires_ts")
        if not math.isfinite(expiry):
            raise ValueError("expires_ts must be finite")
        payload_text = self._serialize_json_field(payload, default="{}")
        if payload_text is None:
            payload_text = "{}"
        timestamp = (
            time.time()
            if ts is None
            else self._coerce_swarm_float(ts, field_name="ts")
        )
        now = time.time()
        try:
            with self.conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO preview_tokens (
                        token_hmac,
                        swarm_id,
                        expires_ts,
                        used
                    )
                    VALUES (?, ?, ?, 0)
                    ON CONFLICT(token_hmac) DO UPDATE SET
                        swarm_id = excluded.swarm_id,
                        expires_ts = excluded.expires_ts
                    WHERE preview_tokens.used = 0
                    """,
                    (
                        normalized_token_hmac,
                        normalized_swarm_id,
                        expiry,
                    ),
                )
                changed = conn.execute("SELECT changes()").fetchone()
                if not changed or int(changed[0] or 0) == 0:
                    return False
                conn.execute(
                    """
                    INSERT INTO swarm_events (swarm_id, event_type, payload, ts)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        normalized_swarm_id,
                        normalized_event_type,
                        payload_text,
                        timestamp,
                    ),
                )
        except sqlite3.Error:
            log.warning("preview token + event persist failed", exc_info=True)
            return False
        self._maybe_prune_preview_tokens(now=now)
        return True

    def lookup_preview_token_swarm_id(self, token_hmac: str) -> str | None:
        """Return the pending swarm_id for a preview token, if still valid."""
        normalized_token_hmac = str(token_hmac or "").strip()
        if not normalized_token_hmac:
            return None
        now = time.time()
        try:
            with self.conn() as conn:
                row = conn.execute(
                    """
                    SELECT swarm_id
                    FROM preview_tokens
                    WHERE token_hmac = ?
                      AND used = 0
                      AND expires_ts > ?
                    """,
                    (
                        normalized_token_hmac,
                        now,
                    ),
                ).fetchone()
        except sqlite3.Error:
            log.warning("preview token lookup failed", exc_info=True)
            return None
        if row is None:
            return None
        return str(row[0] or "").strip() or None

    def consume_preview_token(self, token_hmac: str) -> bool:
        """Atomically consume a pending preview token by deleting it if still valid."""
        normalized_token_hmac = str(token_hmac or "").strip()
        if not normalized_token_hmac:
            return False
        now = time.time()
        try:
            with self.conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    DELETE FROM preview_tokens
                    WHERE token_hmac = ?
                      AND used = 0
                      AND expires_ts > ?
                    """,
                    (
                        normalized_token_hmac,
                        now,
                    ),
                )
                changes_row = conn.execute("SELECT changes()").fetchone()
                return bool(changes_row and int(changes_row[0] or 0) == 1)
        except sqlite3.Error:
            log.warning("preview token consume failed", exc_info=True)
            return False

    @staticmethod
    def _compact_worker_snapshot(summary: object) -> dict[str, object]:
        if not isinstance(summary, Mapping):
            return {"summary": str(summary)}
        compact: dict[str, object] = {}
        blocked_tokens = {"payload", "full_payload", "content", "artifact_payload"}
        for key, value in summary.items():
            if key in blocked_tokens:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                compact[str(key)] = value
            elif key in {"progress", "counters"} and isinstance(value, Mapping):
                compact[str(key)] = dict(value)
        if not compact:
            compact["summary"] = "snapshot recorded"
        return compact

    def get_swarm_summary(self, swarm_id: str) -> dict[str, object] | None:
        """Return a compact top-level swarm summary suitable for inspect surfaces."""
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        with self.conn() as conn:
            row = conn.execute(
                """
                SELECT swarm_id, task_hash, created_ts, status, requested_agents,
                       effective_agents, progress_counters, cost_summary_ref,
                       topology, round, resumable, resume_status,
                       parent_swarm_id, chosen_checkpoint_index
                FROM swarm_runs
                WHERE swarm_id = ?
                """,
                (normalized_swarm_id,),
            ).fetchone()
            worker_counts = conn.execute(
                """
                SELECT COUNT(DISTINCT worker_index), COALESCE(MAX(ts), 0)
                FROM swarm_workers
                WHERE swarm_id = ?
                """,
                (normalized_swarm_id,),
            ).fetchone()
            event_counts = conn.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(ts), 0)
                FROM swarm_events
                WHERE swarm_id = ?
                """,
                (normalized_swarm_id,),
            ).fetchone()

        if row is None:
            return None

        progress_counters = self._parse_json_field(row[6], default={})
        worker_count = int(worker_counts[0]) if worker_counts else 0
        event_count = int(event_counts[0]) if event_counts else 0
        last_worker_ts = float(worker_counts[1]) if worker_counts else 0.0
        last_event_ts = float(event_counts[1]) if event_counts else 0.0
        return {
            "swarm_id": row[0],
            "task_hash": row[1],
            "created_ts": float(row[2]),
            "status": row[3],
            "requested_agents": int(row[4]),
            "effective_agents": int(row[5]),
            "progress_counters": progress_counters if isinstance(progress_counters, Mapping) else {},
            "cost_summary_ref": row[7],
            "topology": row[8],
            "round": int(row[9] or 0),
            "resumable": _coerce_db_bool(row[10]),
            "resume_status": row[11] or "not_resumable",
            "parent_swarm_id": row[12] or None,
            "chosen_checkpoint_index": (
                self._coerce_swarm_int(row[13], field_name="chosen_checkpoint_index")
                if row[13] is not None
                else None
            ),
            "worker_snapshot_count": worker_count,
            "event_count": event_count,
            "last_worker_ts": last_worker_ts,
            "last_event_ts": last_event_ts,
            "last_updated_ts": max(last_worker_ts, last_event_ts, float(row[2])),
        }

    def get_coordinator_round_checkpoint_by_index(
        self,
        swarm_id: str,
        checkpoint_index: int,
        *,
        plan_revision: int | None = None,
    ) -> dict[str, object] | None:
        """Return one coordinator checkpoint by its round_index (1-based)."""
        normalized_swarm_id = str(swarm_id or "").strip()
        if not normalized_swarm_id:
            raise ValueError("swarm_id is required")
        normalized_checkpoint_index = self._coerce_swarm_int(
            checkpoint_index,
            field_name="checkpoint_index",
        )
        if normalized_checkpoint_index < 1:
            raise ValueError("checkpoint_index must be >= 1")
        with self.conn() as conn:
            query = """
                SELECT swarm_id, plan_revision, round_index, coordinator_subtask_id,
                       verdict, amendment_json, next_work_json, synthesis_summary_json,
                       artifact_refs_json, artifact_summaries_json, round_counters_json,
                       fallback_reason, created_ts
                FROM coordinator_round_checkpoints
                WHERE swarm_id = ? AND round_index = ?
            """
            params: list[object] = [normalized_swarm_id, normalized_checkpoint_index]
            if plan_revision is not None:
                query += " AND plan_revision = ?"
                params.append(
                    self._coerce_swarm_int(plan_revision, field_name="plan_revision")
                )
            query += " ORDER BY plan_revision DESC, id DESC LIMIT 1"
            row = conn.execute(query, tuple(params)).fetchone()
        if row is None:
            return None
        return self._coordinator_checkpoint_from_row(row)

    def rebuild_swarm_state_from_db(self, swarm_id: str) -> dict[str, object]:
        """Rebuild the compact operator-facing swarm_state projection from SQLite."""
        summary = self.get_swarm_summary(swarm_id)
        if summary is None:
            raise LookupError(f"swarm_id {swarm_id!r} was not found")

        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT w.worker_index, w.worker_snapshot_ref, w.snapshot_json, w.ts
                FROM swarm_workers AS w
                INNER JOIN (
                    SELECT worker_index, MAX(id) AS max_id
                    FROM swarm_workers
                    WHERE swarm_id = ?
                    GROUP BY worker_index
                ) AS latest
                    ON latest.max_id = w.id
                WHERE w.swarm_id = ?
                ORDER BY w.worker_index
                """,
                (str(swarm_id), str(swarm_id)),
            ).fetchall()

        workers: list[dict[str, object]] = []
        for worker_index, worker_snapshot_ref, snapshot_json, ts in rows:
            parsed = self._parse_json_field(snapshot_json, default={})
            workers.append(
                {
                    "worker_index": int(worker_index),
                    "snapshot_ref": worker_snapshot_ref,
                    "snapshot_summary": self._compact_worker_snapshot(parsed),
                    "ts": float(ts),
                }
            )

        state = dict(summary)
        state["workers"] = workers
        return state

    def _get_full_payload(self, stable_ref: str) -> str | None:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT full_payload FROM artifacts WHERE stable_ref = ?",
                (stable_ref,),
            ).fetchone()
        if row is None:
            return None
        return row[0]

    @staticmethod
    def _coordinator_diff_blob(
        diff_blob: str | dict[str, object] | list[object] | None,
    ) -> str:
        if diff_blob is None:
            return "{}"
        if isinstance(diff_blob, str):
            return diff_blob
        try:
            return json.dumps(diff_blob)
        except TypeError as exc:
            raise TypeError("coordinator diff_blob must be JSON-serializable") from exc

    def insert_plan_revision(
        self,
        plan_id: str,
        revision_number: int,
        diff_blob: str | dict[str, object] | list[object],
        proposer_id: str,
        reason: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Persist one accepted coordinator revision and return its row ID."""
        created_at = int(time.time())
        payload = self._coordinator_diff_blob(diff_blob)
        if conn is None:
            with self.conn() as inner_conn:
                cursor = inner_conn.execute(
                    """
                    INSERT INTO plan_revisions
                        (plan_id, revision_number, diff_blob, proposer_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plan_id,
                        int(revision_number),
                        payload,
                        proposer_id,
                        reason,
                        created_at,
                    ),
                )
                return int(cursor.lastrowid)
        cursor = conn.execute(
            """
            INSERT INTO plan_revisions
                (plan_id, revision_number, diff_blob, proposer_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                int(revision_number),
                payload,
                proposer_id,
                reason,
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    def insert_coordinator_audit(
        self,
        revision_id: int,
        outcome: str,
        rejection_reason: str | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Persist one coordinator audit row for an accepted or rejected revision."""
        if outcome not in {"accepted", "rejected"}:
            raise ValueError("outcome must be 'accepted' or 'rejected'")

        def _insert(active_conn: sqlite3.Connection) -> int:
            row = active_conn.execute(
                """
                SELECT plan_id, proposer_id, diff_blob, reason
                FROM plan_revisions
                WHERE id = ?
                """,
                (int(revision_id),),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown revision_id: {revision_id}")

            created_at = int(time.time())
            cursor = active_conn.execute(
                """
                INSERT INTO coordinator_amendments
                    (plan_id, revision_id, proposer_id, diff_blob, reason,
                     outcome, rejection_reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    int(revision_id),
                    row[1],
                    row[2],
                    row[3],
                    outcome,
                    rejection_reason,
                    created_at,
                ),
            )
            return int(cursor.lastrowid)
        if conn is None:
            with self.conn() as inner_conn:
                return _insert(inner_conn)
        return _insert(conn)

    def insert_coordinator_audit_rejection(
        self,
        plan_id: str,
        proposer_id: str,
        reason: str,
        diff_blob: str | dict[str, object] | list[object] | None = None,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Persist a rejected coordinator amendment without creating a revision."""
        created_at = int(time.time())
        payload = self._coordinator_diff_blob(diff_blob)
        if conn is None:
            with self.conn() as inner_conn:
                cursor = inner_conn.execute(
                    """
                    INSERT INTO coordinator_amendments
                        (plan_id, revision_id, proposer_id, diff_blob, reason,
                         outcome, rejection_reason, created_at)
                    VALUES (?, NULL, ?, ?, ?, 'rejected', ?, ?)
                    """,
                    (
                        plan_id,
                        proposer_id,
                        payload,
                        reason,
                        reason,
                        created_at,
                    ),
                )
                return int(cursor.lastrowid)
        cursor = conn.execute(
            """
            INSERT INTO coordinator_amendments
                (plan_id, revision_id, proposer_id, diff_blob, reason,
                 outcome, rejection_reason, created_at)
            VALUES (?, NULL, ?, ?, ?, 'rejected', ?, ?)
            """,
            (
                plan_id,
                proposer_id,
                payload,
                reason,
                reason,
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def log_agent_result(
        self,
        session_id: str,
        task_hash: str,
        agent_id: int,
        tier: str,
        model: str,
        success: bool = True,
        rework: bool = False,
        tokens_used: int = 0,
        escalated: bool = False,
        provider_name: str | None = None,
        used_fallback: bool = False,
        used_speculation: bool = False,
        provenance_trace_id: str | None = None,
        provenance_depth: int | None = 0,
        provenance_caller_id: str | None = None,
        provider_opt_out_reason: str | None = None,
        estimated_tokens: int | None = None,
        actual_tokens: int | None = None,
        timing_ms: int | None = None,
        rework_count: int = 0,
        parse_diagnostics: str | None = None,
        reason: str | None = None,
        version: str = "copilot",
        # Phase 15 explainability fields (additive, optional)
        urgency_score: float | None = None,
        selected_topology: str | None = None,
        fanout_final_action: str | None = None,
        artifact_publish_count: int = 0,
        artifact_consume_count: int = 0,
        coordinator_round_count: int = 0,
        coordinator_amendment_count: int = 0,
    ) -> int:
        """Persist one telemetry row.

        This method was extended in Phase 15 to include first-class explainability
        columns (urgency_score, selected_topology, fanout_final_action and
        various counts). The additions are additive and optional to preserve
        backward compatibility with older rows and readers per D-01/D-02/D-03.
        """
        with self.conn() as conn:
            # Build a stable ordered list of columns and corresponding values.
            # Using a dynamically generated placeholder string avoids mismatches
            # between the SQL and Python tuple length that caused earlier
            # OperationalError in some environments.
            columns = [
                "session_id",
                "task_hash",
                "agent_id",
                "tier",
                "model",
                "provider_name",
                "success",
                "rework",
                "tokens_used",
                "escalated",
                "used_fallback",
                "used_speculation",
                "provenance_trace_id",
                "provenance_depth",
                "provenance_caller_id",
                "provider_opt_out_reason",
                "estimated_tokens",
                "actual_tokens",
                "timing_ms",
                "rework_count",
                "parse_diagnostics",
                "reason",
                "version",
                "urgency_score",
                "selected_topology",
                "fanout_final_action",
                "artifact_publish_count",
                "artifact_consume_count",
                "coordinator_round_count",
                "coordinator_amendment_count",
                "ts",
            ]
            values = [
                session_id,
                task_hash,
                agent_id,
                tier,
                model,
                provider_name,
                int(success),
                int(rework),
                tokens_used,
                int(escalated),
                int(used_fallback),
                int(used_speculation),
                provenance_trace_id,
                provenance_depth,
                provenance_caller_id,
                provider_opt_out_reason,
                estimated_tokens,
                actual_tokens,
                timing_ms,
                rework_count,
                parse_diagnostics,
                reason,
                version,
                urgency_score,
                selected_topology,
                fanout_final_action,
                int(artifact_publish_count),
                int(artifact_consume_count),
                int(coordinator_round_count),
                int(coordinator_amendment_count),
                time.time(),
            ]
            placeholders = ", ".join(["?"] * len(values))
            sql = f"INSERT INTO telemetry ({', '.join(columns)}) VALUES ({placeholders})"
            cursor = conn.execute(sql, tuple(values))
            return int(cursor.lastrowid or 0)

    def write_telemetry_row(
        self,
        *,
        session_id: str,
        task_hash: str,
        agent_id: int,
        tier: str,
        model: str,
        urgency_score: float | None = None,
        selected_topology: str | None = None,
        fanout_final_action: str | None = None,
        artifact_publish_count: int = 0,
        artifact_consume_count: int = 0,
        coordinator_round_count: int = 0,
        coordinator_amendment_count: int = 0,
        parse_diagnostics: str | None = None,
        reason: str | None = None,
        version: str = "copilot",
    ) -> int:
        """Convenience wrapper for backwards-compatible telemetry writes.

        Provides a focused signature for Phase 15 callers that only need to set
        core explainability fields while leaving other telemetry columns at
        their defaults. Internally delegates to :meth:`log_agent_result`.
        """
        return self.log_agent_result(
            session_id=session_id,
            task_hash=task_hash,
            agent_id=agent_id,
            tier=tier,
            model=model,
            parse_diagnostics=parse_diagnostics,
            reason=reason,
            version=version,
            urgency_score=urgency_score,
            selected_topology=selected_topology,
            fanout_final_action=fanout_final_action,
            artifact_publish_count=artifact_publish_count,
            artifact_consume_count=artifact_consume_count,
            coordinator_round_count=coordinator_round_count,
            coordinator_amendment_count=coordinator_amendment_count,
        )


    def get_provider_token_usage(self, provider_name: str, since_ts: float) -> int:
        """Return total tokens_used for a provider since since_ts (unix timestamp).

        Uses the telemetry table. Returns 0 if no rows match or tokens_used is NULL.
        """
        with self.conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens_used), 0) FROM telemetry WHERE provider_name = ? AND ts > ?",
                (provider_name, since_ts),
            ).fetchone()
        return int(row[0]) if row else 0

    def record_provider_quota_observation(self, observation: Mapping[str, object]) -> int:
        """Persist one secret-safe normalized provider quota observation."""
        provider = str(observation.get("provider") or "").strip().lower()
        if not provider:
            raise ValueError("quota observation requires provider")
        status = str(observation.get("status") or "unavailable")
        source = str(observation.get("source") or "unsupported")
        observed_ts_raw = observation.get("observed_timestamp")
        observed_ts = (
            float(observed_ts_raw)
            if isinstance(observed_ts_raw, (int, float))
            else time.time()
        )
        payload = json.dumps(dict(observation), sort_keys=True, separators=(",", ":"))
        with self.conn() as conn:
            cursor = conn.execute(
                "INSERT INTO provider_quota_observations "
                "(provider, status, source, observed_ts, payload) VALUES (?, ?, ?, ?, ?)",
                (provider, status, source, observed_ts, payload),
            )
            return int(cursor.lastrowid)

    def get_latest_provider_quota_observation(
        self, provider: str
    ) -> dict[str, object] | None:
        """Return the latest normalized quota observation for diagnostics."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT payload FROM provider_quota_observations "
                "WHERE provider = ? ORDER BY observed_ts DESC, id DESC LIMIT 1",
                (provider.strip().lower(),),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def log_urgency_fanout_event(
        self,
        task_id: str,
        selected_routers: list[str],
        budget_accounting: dict,
        urgency_metadata: dict | None = None,
    ) -> int:
        """Insert a fanout_telemetry row augmented with urgency explainability.

        The existing fanout_telemetry schema stores selected_routers and a
        budget_accounting JSON blob. We embed urgency metadata under the
        'urgency' key inside the budget_accounting blob to keep schema changes
        minimal and additive for older deployments.
        """
        try:
            payload = dict(budget_accounting)
        except Exception:
            payload = {"total_budget": 0, "used": 0, "remaining": 0}
        if urgency_metadata:
            payload["urgency"] = urgency_metadata
        with self.conn() as conn:
            cursor = conn.execute(
                "INSERT INTO fanout_telemetry "
                "(task_id, selected_routers, budget_accounting, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    json.dumps(selected_routers),
                    json.dumps(payload),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
            return int(cursor.lastrowid or 0)

    # ------------------------------------------------------------------
    # Escalation logging (kill switch)
    # ------------------------------------------------------------------

    def log_escalation(
        self,
        task_hash: str,
        agent_id: int,
        from_tier: str,
        to_tier: str,
        token_count: int,
        ceiling: int,
    ) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO escalations "
                "(task_hash, agent_id, from_tier, to_tier, token_count, ceiling, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_hash,
                    agent_id,
                    from_tier,
                    to_tier,
                    token_count,
                    ceiling,
                    time.time(),
                ),
            )

    # ------------------------------------------------------------------
    # Rework events
    # ------------------------------------------------------------------

    def log_rework(
        self,
        session_id: str,
        wave_n: int,
        wave_n1: int,
        file_path: str,
        scope_match: bool = False,
    ) -> None:
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO rework_events "
                "(session_id, wave_n, wave_n1, file_path, scope_match, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    wave_n,
                    wave_n1,
                    file_path,
                    int(scope_match),
                    time.time(),
                ),
            )

    # ------------------------------------------------------------------
    # Subtask pattern tracking
    # ------------------------------------------------------------------


    def log_out_of_workspace_write(
        self,
        target_path: str,
        provider: str | None,
        tier: str | None,
        grant_reason: str,
    ) -> None:
        """Record an out-of-workspace write grant. Prunes to 500 entries rolling."""
        import json as _json
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO write_audit (preview_token, requested_path, caller, outcome, details, ts)
                VALUES (NULL, ?, ?, 'allowed_out_of_workspace', ?, ?)
                """,
                (
                    target_path,
                    provider or "unknown",
                    _json.dumps({"provider": provider, "tier": tier, "grant_reason": grant_reason}),
                    time.time(),
                ),
            )
            # Rolling prune — keep only 500 most recent
            conn.execute(
                """
                DELETE FROM write_audit
                WHERE id NOT IN (
                    SELECT id FROM write_audit
                    WHERE outcome = 'allowed_out_of_workspace'
                    ORDER BY ts DESC
                    LIMIT 500
                )
                AND outcome = 'allowed_out_of_workspace'
                """
            )

    def get_write_audit(self, limit: int = 500) -> list[dict]:
        """Return recent out-of-workspace write audit entries, newest first."""
        import json as _json
        with self.conn() as conn:
            rows = conn.execute(
                """
                SELECT requested_path, caller, details, ts
                FROM write_audit
                WHERE outcome = 'allowed_out_of_workspace'
                ORDER BY ts DESC
                LIMIT ?
                """,
                (min(limit, 500),),
            ).fetchall()
        result = []
        for row in rows:
            entry: dict = {
                "path": row[0],
                "provider": row[1],
                "ts": row[3],
            }
            try:
                details = _json.loads(row[2] or "{}")
                entry.update(details)
            except Exception:
                pass
            result.append(entry)
        return result

    def track_pattern(
        self,
        pattern_hash: str,
        pattern_desc: str,
        tier: str,
        example: str | dict = "",
        quality_score: float | None = None,
        rework_detected: bool | None = None,
    ) -> int:
        """Increment occurrence count for a subtask pattern. Returns new count."""
        def _bounded_examples(raw_value: object, new_example: str | dict) -> str:
            try:
                existing = json.loads(raw_value) if raw_value else []
            except (TypeError, json.JSONDecodeError):
                existing = []
            if not isinstance(existing, list):
                existing = []
            existing = existing[-9:]
            if new_example:
                existing.append(new_example)

            unique: list[object] = []
            seen: set[str] = set()
            for item in existing:
                if isinstance(item, dict):
                    key = json.dumps(item, sort_keys=True, separators=(",", ":"))
                    normalized = item
                elif isinstance(item, str):
                    normalized = " ".join(item.strip().split())
                    if not normalized:
                        continue
                    key = normalized
                else:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                unique.append(normalized)
            return json.dumps(unique[-10:], sort_keys=True)

        bounded_quality: float | None = None
        if quality_score is not None:
            try:
                candidate_quality = float(quality_score)
            except (TypeError, ValueError):
                candidate_quality = 0.0
            if math.isfinite(candidate_quality):
                bounded_quality = max(0.0, min(1.0, candidate_quality))

        with self.conn() as conn:
            row = conn.execute(
                "SELECT occurrence_count, examples, eval_quality, rework_detected FROM subtask_patterns "
                "WHERE pattern_hash = ?",
                (pattern_hash,),
            ).fetchone()
            if row is None:
                examples = _bounded_examples(None, example)
                initial_quality = bounded_quality if bounded_quality is not None else 0.0
                initial_rework = 1 if rework_detected else 0
                conn.execute(
                    "INSERT INTO subtask_patterns "
                    "(pattern_hash, pattern_desc, occurrence_count, tier, last_seen, examples, rework_detected, eval_quality) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                    (pattern_hash, pattern_desc, tier, time.time(), examples, initial_rework, initial_quality),
                )
                return 1
            count = row[0] + 1
            examples = _bounded_examples(row[1], example)
            prior_quality = float(row[2]) if row[2] is not None else 0.0
            if bounded_quality is None:
                next_quality = prior_quality
            else:
                next_quality = ((prior_quality * row[0]) + bounded_quality) / count if count != 0 else prior_quality
            prior_rework = _coerce_db_bool(row[3])
            next_rework = prior_rework if rework_detected is None else (prior_rework or bool(rework_detected))
            conn.execute(
                "UPDATE subtask_patterns "
                "SET occurrence_count = ?, tier = ?, last_seen = ?, examples = ?, "
                "rework_detected = ?, eval_quality = ? "
                "WHERE pattern_hash = ?",
                (count, tier, time.time(), examples, 1 if next_rework else 0, next_quality, pattern_hash),
            )
            return count

    def update_pattern_quality(
        self,
        pattern_hash: str,
        quality_score: float,
        *,
        rework_detected: bool | None = None,
    ) -> bool:
        """Fold an explicit outcome signal into an existing subtask pattern."""
        try:
            candidate_quality = float(quality_score)
        except (TypeError, ValueError):
            candidate_quality = 0.0
        if not math.isfinite(candidate_quality):
            candidate_quality = 0.0
        bounded_quality = max(0.0, min(1.0, candidate_quality))

        with self.conn() as conn:
            row = conn.execute(
                "SELECT occurrence_count, eval_quality, rework_detected FROM subtask_patterns WHERE pattern_hash = ?",
                (pattern_hash,),
            ).fetchone()
            if row is None:
                return False
            occurrence_count = max(1, int(row[0] or 1))
            prior_quality = float(row[1]) if row[1] is not None else 0.0
            next_quality = ((prior_quality * occurrence_count) + bounded_quality) / (occurrence_count + 1)
            prior_rework = _coerce_db_bool(row[2])
            next_rework = prior_rework if rework_detected is None else (prior_rework or bool(rework_detected))
            conn.execute(
                """
                UPDATE subtask_patterns
                SET eval_quality = ?, rework_detected = ?, last_seen = ?
                WHERE pattern_hash = ?
                """,
                (next_quality, 1 if next_rework else 0, time.time(), pattern_hash),
            )
            return True

    def get_mature_patterns(self, min_occurrences: int = 5) -> list[dict]:
        """Return patterns that have hit the emergence threshold."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT pattern_hash, pattern_desc, occurrence_count, tier, examples, "
                "rework_detected, eval_quality "
                "FROM subtask_patterns WHERE occurrence_count >= ? "
                "ORDER BY occurrence_count DESC",
                (min_occurrences,),
            ).fetchall()
        results = []
        for ph, pd, oc, tier, ex, rw, eq in rows:
            try:
                examples = json.loads(ex) if ex else []
            except json.JSONDecodeError:
                examples = []
            results.append(
                {
                    "pattern_hash": ph,
                    "pattern_desc": pd,
                    "occurrence_count": oc,
                    "tier": tier,
                    "examples": examples,
                    "rework_detected": _coerce_db_bool(rw),
                    "eval_quality": float(eq) if eq is not None else 0.0,
                }
            )
        return results

    def get_pattern(self, pattern_hash: str) -> dict | None:
        """Return a single pattern by hash."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT pattern_hash, pattern_desc, occurrence_count, tier, examples, "
                "rework_detected, eval_quality "
                "FROM subtask_patterns WHERE pattern_hash = ?",
                (pattern_hash,),
            ).fetchone()
        if not row:
            return None
        try:
            examples = json.loads(row[4]) if row[4] else []
        except json.JSONDecodeError:
            examples = []
        return {
            "pattern_hash": row[0],
            "pattern_desc": row[1],
            "occurrence_count": row[2],
            "tier": row[3],
            "examples": examples,
            "rework_detected": _coerce_db_bool(row[5]),
            "eval_quality": float(row[6]) if row[6] is not None else 0.0,
        }

    # ------------------------------------------------------------------
    # Agent definitions
    # ------------------------------------------------------------------

    def save_agent_definition(
        self,
        pattern_hash: str,
        pattern_desc: str,
        definition: str,
        *,
        promotion_state: str = "active",
        match_count: int | None = None,
    ) -> None:
        """Save or update a learned agent definition."""
        with self.conn() as conn:
            if match_count is None:
                conn.execute(
                    """
                    INSERT INTO agent_definitions
                        (pattern_hash, pattern_desc, definition, match_count, ts,
                         promotion_state, status, description)
                    VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                    ON CONFLICT(pattern_hash) DO UPDATE SET
                        pattern_desc = excluded.pattern_desc,
                        definition = excluded.definition,
                        match_count = agent_definitions.match_count + 1,
                        ts = excluded.ts,
                        promotion_state = excluded.promotion_state,
                        status = CASE
                            WHEN excluded.promotion_state = 'active' THEN 'active'
                            ELSE COALESCE(agent_definitions.status, excluded.status)
                        END,
                        description = COALESCE(agent_definitions.description, excluded.description)
                    """,
                    (
                        pattern_hash,
                        pattern_desc,
                        definition,
                        time.time(),
                        promotion_state,
                        "active" if promotion_state == "active" else promotion_state,
                        pattern_desc,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO agent_definitions
                        (pattern_hash, pattern_desc, definition, match_count, ts,
                         promotion_state, status, description)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pattern_hash) DO UPDATE SET
                        pattern_desc = excluded.pattern_desc,
                        definition = excluded.definition,
                        match_count = excluded.match_count,
                        ts = excluded.ts,
                        promotion_state = excluded.promotion_state,
                        status = CASE
                            WHEN excluded.promotion_state = 'active' THEN 'active'
                            ELSE COALESCE(agent_definitions.status, excluded.status)
                        END,
                        description = COALESCE(agent_definitions.description, excluded.description)
                    """,
                    (
                        pattern_hash,
                        pattern_desc,
                        definition,
                        match_count,
                        time.time(),
                        promotion_state,
                        "active" if promotion_state == "active" else promotion_state,
                        pattern_desc,
                    ),
                )

    def get_agent_definition(self, pattern_hash: str) -> dict | None:
        """Return a single agent definition by pattern hash."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT id, pattern_hash, pattern_desc, definition, match_count, ts, promotion_state "
                "FROM agent_definitions WHERE pattern_hash = ?",
                (pattern_hash,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "pattern_hash": row[1],
            "pattern_desc": row[2],
            "definition": row[3],
            "match_count": row[4],
            "ts": row[5],
            "promotion_state": row[6],
        }

    def get_all_agent_definitions(self) -> list[dict]:
        """Return all agent definitions, ordered by most-used first."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT id, pattern_hash, pattern_desc, definition, match_count, ts, promotion_state "
                "FROM agent_definitions ORDER BY match_count DESC"
            ).fetchall()
        return [
            {
                "id": row[0],
                "pattern_hash": row[1],
                "pattern_desc": row[2],
                "definition": row[3],
                "match_count": row[4],
                "ts": row[5],
                "promotion_state": row[6],
            }
            for row in rows
        ]

    def delete_agent_definition(self, pattern_hash: str) -> bool:
        """Remove an agent definition (used during dedup merges)."""
        with self.conn() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_definitions WHERE pattern_hash = ?",
                (pattern_hash,),
            )
            return cursor.rowcount > 0

    def increment_agent_match_count(self, pattern_hash: str) -> None:
        """Bump match_count when an agent is auto-assigned to a subtask."""
        with self.conn() as conn:
            conn.execute(
                "UPDATE agent_definitions SET match_count = match_count + 1, ts = ? "
                "WHERE pattern_hash = ?",
                (time.time(), pattern_hash),
            )

    # ========================================================================
    # Wave 1b: Conservative Dedup and Merge Methods
    # ========================================================================

    def agent_definition_insert(
        self,
        project_id: str | None,
        lane: str,
        pattern_hash: str,
        pattern_desc: str,
        description: str,
        agent_id: str,
        status: str = "pending"
    ) -> bool:
        """Insert a new agent definition with Wave 1b fields."""
        try:
            with self.conn() as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO agent_definitions
                    (id, project_id, lane, pattern_hash, pattern_desc, description, 
                     status, definition, match_count, ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        agent_id,
                        project_id,
                        lane,
                        pattern_hash,
                        pattern_desc,
                        description,
                        status,
                        "",  # Empty definition placeholder
                        1,
                        time.time()
                    )
                )
            return True
        except Exception as e:
            log.warning(f"Failed to insert agent definition: {e}")
            return False

    def agent_definition_get(self, agent_id: str) -> dict | None:
        """Get an agent definition by ID or pattern_hash (canonical identity).

        Queries by ``id`` first; if no row has that id, falls back to
        ``pattern_hash``.  This lets callers use either the UUID-style ``id``
        from :meth:`agent_definition_insert` **or** the ``pattern_hash`` that
        :meth:`save_agent_definition` uses as its primary key — both resolve to
        the same row so ``pattern_hash`` is the authoritative identity.
        """
        try:
            with self.conn() as conn:
                row = conn.execute(
                    """SELECT id, project_id, lane, pattern_hash, pattern_desc, description,
                     status, definition, match_count, ts, merged_into_id, activated_at,
                     promotion_state
                    FROM agent_definitions WHERE id = ?""",
                    (agent_id,)
                ).fetchone()
                if row is None:
                    row = conn.execute(
                       """SELECT id, project_id, lane, pattern_hash, pattern_desc, description,
                        status, definition, match_count, ts, merged_into_id, activated_at,
                        promotion_state
                       FROM agent_definitions WHERE pattern_hash = ?""",
                       (agent_id,),
                    ).fetchone()
            if not row:
                return None
            return {
                "id": row[0] or row[3],
                "project_id": row[1],
                "lane": row[2],
                "pattern_hash": row[3],
                "pattern_desc": row[4],
                "description": row[5],
                "status": row[6],
                "definition": row[7],
                "match_count": row[8],
                "ts": row[9],
                "merged_into_id": row[10],
                "activated_at": row[11],
                "promotion_state": row[12],
            }
        except Exception as e:
            log.warning(f"Failed to get agent definition: {e}")
            return None

    def agent_definitions_list(
        self,
        lane: str,
        project_id: str | None = None
    ) -> list[dict]:
        """Get all agent definitions for a lane (Wave 1b)."""
        try:
            with self.conn() as conn:
                if lane == "project" and project_id:
                    rows = conn.execute(
                        """SELECT id, project_id, lane, pattern_hash, pattern_desc, description,
                         status, definition, match_count, ts, merged_into_id, activated_at
                        FROM agent_definitions WHERE lane = ? AND project_id = ?""",
                        (lane, project_id)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, project_id, lane, pattern_hash, pattern_desc, description,
                         status, definition, match_count, ts, merged_into_id, activated_at
                        FROM agent_definitions WHERE lane = ?""",
                        (lane,)
                    ).fetchall()
            
            result = []
            for row in rows:
                result.append({
                    "id": row[0] or row[3],
                    "project_id": row[1],
                    "lane": row[2],
                    "pattern_hash": row[3],
                    "pattern_desc": row[4],
                    "description": row[5],
                    "status": row[6],
                    "definition": row[7],
                    "match_count": row[8],
                    "ts": row[9],
                    "merged_into_id": row[10],
                    "activated_at": row[11]
                })
            return result
        except Exception as e:
            log.warning(f"Failed to list agent definitions: {e}")
            return []

    def agent_definition_update(
        self,
        agent_id: str,
        description: str | None = None,
        status: str | None = None,
        merged_into_id: str | None = None,
        activated_at: float | None = None,
        promotion_state: str | None = None,
    ) -> bool:
        """Update an agent definition (Wave 1b)."""
        try:
            updates = []
            params = []
            
            if description is not None:
                updates.append("description = ?")
                params.append(description)
            if status is not None:
                updates.append("status = ?")
                params.append(status)
            if merged_into_id is not None:
                updates.append("merged_into_id = ?")
                params.append(merged_into_id)
            if activated_at is not None:
                updates.append("activated_at = ?")
                params.append(activated_at)
            if promotion_state is not None:
                updates.append("promotion_state = ?")
                params.append(promotion_state)
            
            if not updates:
                return True
            
            updates.append("ts = ?")
            params.append(time.time())
            params.extend([agent_id, agent_id])

            with self.conn() as conn:
                cursor = conn.execute(
                    f"""UPDATE agent_definitions SET {', '.join(updates)}
                        WHERE pattern_hash = COALESCE(
                            (SELECT pattern_hash FROM agent_definitions WHERE id = ? LIMIT 1),
                            ?
                        )""",
                    params
                )
                return cursor.rowcount > 0
        except Exception as e:
            log.warning(f"Failed to update agent definition: {e}")
            return False

    def agent_audit_log(
        self,
        agent_id: str,
        event_type: str,
        details: dict | None = None,
        target: str | None = None
    ) -> int | None:
        """Log an agent audit event for tracking lifecycle changes."""
        try:
            details = details or {}
            if target:
                details['target'] = target
            
            import json
            import time
            
            details_json = json.dumps(details, sort_keys=True)
            created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            
            try:
                _secret = self._get_audit_secret()
            except Exception:
                _secret = b""
            with self.conn() as conn:
                _prev = self.get_prev_chain_hmac(conn, "agent_audit")
                _chain = self._compute_chain_hmac(_secret, _prev, details_json) if _secret else ""
                cursor = conn.execute(
                    "INSERT INTO agent_audit"
                    " (agent_id, event_type, details_json, created_at, chain_hmac)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (agent_id, event_type, details_json, created_at, _chain),
                )
                return int(cursor.lastrowid)
        except Exception as e:
            log.warning(f"Failed to log agent audit: {e}")
            return None

    def list_agent_audit_events(
        self,
        *,
        agent_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return newest-first agent lifecycle audit events."""
        bounded_limit = max(1, min(int(limit), 100))
        params: list[object] = []
        where = ""
        if agent_id:
            where = "WHERE agent_id = ?"
            params.append(agent_id)
        params.append(bounded_limit)
        with self.conn() as conn:
            rows = conn.execute(
                f"""
                SELECT id, agent_id, event_type, details_json, canonical_id,
                       merged_from, created_at
                FROM agent_audit
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "id": int(row[0]),
                "agent_id": row[1],
                "event_type": row[2],
                "details_json": row[3],
                "canonical_id": row[4],
                "merged_from": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]

    def get_active_agents(self) -> list[dict]:
        """Get all agents with status='active'.
        
        Per D-01: Only return ACTIVE agents, excluding drafts, pending, and rejected agents.
        
        Returns:
            List of agent dicts with id, description, lane, status, definition, etc.
        """
        try:
            with self.conn() as conn:
                rows = conn.execute(
                    """SELECT id, project_id, lane, pattern_hash, pattern_desc, description,
                              status, definition, match_count, ts, merged_into_id, activated_at
                       FROM agent_definitions
                       WHERE status = ? OR promotion_state = ?""",
                    ('active', 'active')
                ).fetchall()
            
            result = []
            for row in rows:
                result.append({
                    "id": row[0] or row[3],
                    "project_id": row[1],
                    "lane": row[2],
                    "pattern_hash": row[3],
                    "pattern_desc": row[4],
                    "description": row[5],
                    "status": row[6],
                    "definition": row[7],
                    "match_count": row[8],
                    "ts": row[9],
                    "merged_into_id": row[10],
                    "activated_at": row[11]
                })
            return result
        except Exception as e:
            log.warning(f"Failed to get active agents: {e}")
            return []

    # ------------------------------------------------------------------
    # Routing guard methods (Phase 37+)
    # ------------------------------------------------------------------

    def routing_guard_put(
        self,
        *,
        caller: str,
        cwd: str | None,
        mode: str,
        tier: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        source_tool: str = "",
        task_text: str = "",
        file_hints: list[str] | None = None,
        ttl_seconds: int = 3600,
    ) -> dict[str, object]:
        """Persist a routing guard record and return it."""
        normalized_caller = str(caller or "").strip() or "mcp"
        normalized_cwd = self._normalize_routing_guard_cwd(cwd)
        normalized_mode = str(mode or "").strip()
        if not normalized_mode:
            raise ValueError("mode is required")
        now = time.time()
        expires_ts = now + max(int(ttl_seconds), 0)
        file_hints_json = self._serialize_json_field(file_hints or [], default="[]") or "[]"
        guard_key = self._routing_guard_key(normalized_caller, normalized_cwd)
        # Don't downgrade an existing higher-tier guard (direct > execute_subtask).
        _mode_rank: dict[str, int] = {"execute_subtask": 0, "direct": 1}
        existing = self.routing_guard_get(caller=normalized_caller, cwd=normalized_cwd)
        if existing and _mode_rank.get(str(existing.get("mode", "")), 0) > _mode_rank.get(normalized_mode, 0):
            return {**existing, "skipped": True}
        with self.conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO routing_guards (
                    guard_key, caller, cwd, mode, tier, provider, model,
                    source_tool, task_text, file_hints_json, created_ts, expires_ts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guard_key,
                    normalized_caller,
                    normalized_cwd,
                    normalized_mode,
                    tier,
                    provider,
                    model,
                    str(source_tool or ""),
                    str(task_text or ""),
                    file_hints_json,
                    now,
                    expires_ts,
                ),
            )
        return {
            "caller": normalized_caller,
            "cwd": normalized_cwd,
            "mode": normalized_mode,
            "tier": tier,
            "provider": provider,
            "model": model,
            "source_tool": source_tool,
            "task_text": task_text,
            "file_hints": file_hints or [],
            "expires_ts": expires_ts,
            "created_ts": now,
        }

    def routing_guard_get(
        self,
        *,
        caller: str,
        cwd: str | None,
    ) -> dict[str, object] | None:
        """Return the most recent non-expired routing guard for caller/cwd, or None."""
        normalized_caller = str(caller or "").strip() or "mcp"
        normalized_cwd = self._normalize_routing_guard_cwd(cwd)
        now = time.time()
        with self.conn() as conn:
            row = conn.execute(
                """
                SELECT caller, cwd, mode, tier, provider, model,
                       source_tool, task_text, file_hints_json, expires_ts, created_ts
                FROM routing_guards
                WHERE caller = ? AND cwd = ? AND expires_ts > ?
                ORDER BY created_ts DESC, guard_key DESC
                LIMIT 1
                """,
                (normalized_caller, normalized_cwd, now),
            ).fetchone()
        if row is None:
            return None
        file_hints = self._parse_json_field(row[8], default=[])
        return {
            "caller": row[0],
            "cwd": row[1],
            "mode": row[2],
            "tier": row[3],
            "provider": row[4],
            "model": row[5],
            "source_tool": row[6],
            "task_text": row[7],
            "file_hints": file_hints if isinstance(file_hints, list) else [],
            "expires_ts": float(row[9]),
            "created_ts": float(row[10]),
        }

    def routing_guard_clear(
        self,
        *,
        caller: str,
        cwd: str | None,
    ) -> int:
        """Delete all routing guards for the given caller/cwd. Returns rows deleted."""
        normalized_caller = str(caller or "").strip() or "mcp"
        normalized_cwd = self._normalize_routing_guard_cwd(cwd)
        with self.conn() as conn:
            result = conn.execute(
                "DELETE FROM routing_guards WHERE caller = ? AND cwd = ?",
                (normalized_caller, normalized_cwd),
            )
            return result.rowcount

    def routing_guard_record_execution(
        self,
        *,
        caller: str,
        cwd: str | None,
        task_id: str = "",
        file_written: str | None = None,
    ) -> None:
        """Record that a subtask was executed, satisfying the routing guard."""
        caller_norm = str(caller or "").strip().lower() or "mcp"
        cwd_norm = self._normalize_routing_guard_cwd(cwd)
        executed_ts = time.time()
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO routing_guard_executions (caller, cwd, task_id, file_written, executed_ts) VALUES (?, ?, ?, ?, ?)",
                (caller_norm, cwd_norm, task_id, file_written, executed_ts),
            )

    def routing_guard_has_executions(
        self,
        *,
        caller: str,
        cwd: str | None,
    ) -> bool:
        """Return True if at least one subtask execution was recorded in the last hour."""
        caller_norm = str(caller or "").strip().lower() or "mcp"
        cwd_norm = self._normalize_routing_guard_cwd(cwd)
        cutoff = time.time() - 3600
        with self.conn() as conn:
            exists = conn.execute(
                "SELECT EXISTS(SELECT 1 FROM routing_guard_executions WHERE caller=? AND cwd=? AND executed_ts > ?)",
                (caller_norm, cwd_norm, cutoff),
            ).fetchone()[0]
        return bool(exists)

    # ------------------------------------------------------------------
    # Audit chain helpers (plan 03)
    # ------------------------------------------------------------------

    _AUDIT_TABLES: frozenset[str] = frozenset({"swarm_events", "agent_audit", "file_writes"})

    _PREV_HMAC_QUERIES: dict[str, str] = {
        "swarm_events": "SELECT chain_hmac FROM swarm_events ORDER BY id DESC LIMIT 1",
        "agent_audit": "SELECT chain_hmac FROM agent_audit ORDER BY id DESC LIMIT 1",
        "file_writes": "SELECT chain_hmac FROM file_writes ORDER BY id DESC LIMIT 1",
    }

    def _get_audit_secret(self) -> bytes:
        """Load or generate the 32-byte audit HMAC secret (perms 0600)."""
        secret_path = self._db_path.parent / "audit_secret"
        if secret_path.exists():
            with open(str(secret_path), "rb") as fh:
                data = fh.read(32)
            if len(data) == 32:
                return data
        new_secret = secrets.token_bytes(32)
        tmp_path = secret_path.with_suffix(".tmp")
        try:
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(new_secret)
            os.rename(str(tmp_path), str(secret_path))
        except OSError:
            log.debug("Could not persist audit_secret", exc_info=True)
        return new_secret

    @staticmethod
    def _compute_chain_hmac(secret: bytes, prev_hmac: str, payload: str) -> str:
        """Compute HMAC-SHA256 for a new chain entry: hmac(secret, prev_hmac || payload)."""
        msg = (prev_hmac + payload).encode("utf-8")
        return _hmac_mod.new(secret, msg, hashlib.sha256).hexdigest()

    def get_prev_chain_hmac(self, conn: sqlite3.Connection, table: str) -> str:
        """Return the most recent chain_hmac for *table*, or '' if none."""
        if table not in self._AUDIT_TABLES:
            return ""
        query = self._PREV_HMAC_QUERIES.get(table, "")
        if not query:
            return ""
        row = conn.execute(query).fetchone()
        return (row[0] or "") if row else ""

    def verify_audit_chain(self, table: str, *, from_ts: float | None = None) -> list[dict]:
        """Walk the audit chain for *table* and return a list of broken entries.

        Each broken entry is a dict with keys: id, expected_hmac, stored_hmac.
        Empty list means the chain is intact.
        """
        if table not in self._AUDIT_TABLES:
            raise ValueError(f"Unknown audit table: {table}")
        secret = self._get_audit_secret()
        select_map = {
            "swarm_events": "SELECT id, chain_hmac, payload FROM swarm_events ORDER BY id",
            "agent_audit": "SELECT id, chain_hmac, details_json FROM agent_audit ORDER BY id",
            "file_writes": "SELECT id, chain_hmac, target_path FROM file_writes ORDER BY id",
        }
        query = select_map[table]
        with self.conn() as conn:
            rows = conn.execute(query).fetchall()
        breaks: list[dict] = []
        prev_hmac = ""
        for row in rows:
            row_id, stored_hmac, payload = row
            if not stored_hmac:
                prev_hmac = ""
                continue  # pre-audit row; skip
            expected = self._compute_chain_hmac(secret, prev_hmac, payload or "")
            if expected != stored_hmac:
                breaks.append({
                    "id": row_id,
                    "expected_hmac": expected,
                    "stored_hmac": stored_hmac,
                })
            prev_hmac = stored_hmac
        return breaks

    def claim_attempt(self, scope: str, key: str) -> tuple[int, bool]:
        """Claim an attempt for (scope, key). Returns (attempt_n, already_completed).

        already_completed=True when file_writes holds a completed record for this key.
        attempt_n is 0-based and incremented on each call via upsert.
        """
        import time as _time
        now = _time.time()
        with self.conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM file_writes WHERE scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
            if row is not None:
                return (0, True)
            conn.execute(
                "INSERT INTO idempotency_attempts"
                " (scope, idempotency_key, attempt, first_seen_at, last_attempt_at)"
                " VALUES (?, ?, 0, ?, ?)"
                " ON CONFLICT(scope, idempotency_key) DO UPDATE SET"
                "   attempt = attempt + 1,"
                "   last_attempt_at = excluded.last_attempt_at",
                (scope, key, now, now),
            )
            row2 = conn.execute(
                "SELECT attempt FROM idempotency_attempts WHERE scope=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
        attempt_n = int(row2[0]) if row2 else 0
        return (attempt_n, False)

    def record_file_write(
        self,
        scope: str,
        idempotency_key: str,
        target_path: str,
        lines_written: int | None = None,
    ) -> None:
        """Record a completed file write for idempotency tracking."""
        import time as _time
        try:
            _secret = self._get_audit_secret()
        except Exception:
            _secret = b""
        payload_text = f"{scope}:{idempotency_key}:{target_path}"
        with self.conn() as conn:
            _prev = self.get_prev_chain_hmac(conn, "file_writes")
            _chain = self._compute_chain_hmac(_secret, _prev, payload_text) if _secret else ""
            conn.execute(
                "INSERT OR IGNORE INTO file_writes"
                " (scope, idempotency_key, target_path, lines_written, completed_at, chain_hmac)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (scope, idempotency_key, target_path, lines_written, _time.time(), _chain),
            )

    def get_file_write(self, scope: str, idempotency_key: str) -> dict | None:
        """Return a previously completed file_write record, or None if not found."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT target_path, lines_written, completed_at"
                " FROM file_writes WHERE scope=? AND idempotency_key=?",
                (scope, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "target_path": row[0],
            "lines_written": row[1],
            "completed_at": row[2],
        }

    def acquire_lease(self, task_id: str, worker_id: str, ttl_seconds: float = 60.0) -> bool:
        """Claim a worker lease for task_id. Returns True if acquired, False if already held."""
        import time as _time
        now = _time.time()
        expires_at = now + ttl_seconds
        with self.conn() as conn:
            row = conn.execute(
                "SELECT worker_id, status, expires_at FROM worker_leases WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if row is None:
                # No existing lease — insert fresh
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO worker_leases"
                        " (task_id, worker_id, acquired_at, expires_at, last_heartbeat, attempt, status)"
                        " VALUES (?, ?, ?, ?, ?, 0, 'active')",
                        (task_id, worker_id, now, expires_at, now),
                    )
                    return True
                except Exception:
                    return False
            existing_worker, status, existing_expires = row
            if existing_worker == worker_id and status == "active":
                return True  # same worker re-acquires
            if existing_expires < now:
                # Expired — delete and re-insert
                conn.execute("DELETE FROM worker_leases WHERE task_id=?", (task_id,))
                try:
                    conn.execute(
                        "INSERT INTO worker_leases"
                        " (task_id, worker_id, acquired_at, expires_at, last_heartbeat, attempt, status)"
                        " VALUES (?, ?, ?, ?, ?, 0, 'active')",
                        (task_id, worker_id, now, expires_at, now),
                    )
                    return True
                except Exception:
                    return False
            return False  # held by another worker

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        """Extend the lease for (task_id, worker_id). Returns True if lease still active."""
        import time as _time
        now = _time.time()
        with self.conn() as conn:
            row = conn.execute(
                "SELECT expires_at FROM worker_leases"
                " WHERE task_id=? AND worker_id=? AND status='active'",
                (task_id, worker_id),
            ).fetchone()
            if row is None or row[0] < now:
                return False
            conn.execute(
                "UPDATE worker_leases SET last_heartbeat=? WHERE task_id=? AND worker_id=?",
                (now, task_id, worker_id),
            )
        return True

    def release_lease(self, task_id: str, worker_id: str) -> None:
        """Release (complete) a worker lease."""
        with self.conn() as conn:
            conn.execute(
                "UPDATE worker_leases SET status='released'"
                " WHERE task_id=? AND worker_id=?",
                (task_id, worker_id),
            )

    def expire_stale_leases(self) -> list[str]:
        """Mark expired active leases as expired. Returns list of expired task_ids."""
        now = __import__("time").time()
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT task_id FROM worker_leases WHERE status='active' AND expires_at<?",
                (now,),
            ).fetchall()
            expired = [r[0] for r in rows]
            if expired:
                conn.execute(
                    "UPDATE worker_leases SET status='expired'"
                    " WHERE status='active' AND expires_at<?",
                    (now,),
                )
        return expired

    def dead_letter(self, task_id: str, error: str, payload: str | None = None) -> None:
        """Move task_id to dead_letters after max retry failures."""
        import time as _time
        now = _time.time()
        with self.conn() as conn:
            existing = conn.execute(
                "SELECT attempt_count, first_failed_at FROM dead_letters WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE dead_letters SET last_error=?, attempt_count=attempt_count+1,"
                    " last_failed_at=? WHERE task_id=?",
                    (error, now, task_id),
                )
            else:
                conn.execute(
                    "INSERT INTO dead_letters"
                    " (task_id, last_error, attempt_count, first_failed_at, last_failed_at, payload)"
                    " VALUES (?, ?, 1, ?, ?, ?)",
                    (task_id, error, now, now, payload),
                )

    def get_dead_letters(self, limit: int = 50) -> list[dict]:
        """Return dead letter queue entries."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT task_id, last_error, attempt_count, first_failed_at, last_failed_at"
                " FROM dead_letters ORDER BY last_failed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "task_id": r[0], "last_error": r[1], "attempt_count": r[2],
                "first_failed_at": r[3], "last_failed_at": r[4],
            }
            for r in rows
        ]

    def replay_dead_letter(self, task_id: str) -> bool:
        """Reset a dead letter for re-attempt (removes from dead_letters). Returns True if found."""
        with self.conn() as conn:
            row = conn.execute(
                "DELETE FROM dead_letters WHERE task_id=? RETURNING task_id",
                (task_id,),
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Worker sessions (plan 10)
    # ------------------------------------------------------------------

    def create_worker_session(
        self,
        session_id: str,
        provider: str,
        model: str,
        pid: int | None = None,
    ) -> None:
        """Register a new active worker session."""
        import time as _time
        now = _time.time()
        with self.conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO worker_sessions"
                " (session_id, provider, model, pid, started_at, last_used_at, status, token_count)"
                " VALUES (?, ?, ?, ?, ?, ?, 'active', 0)",
                (session_id, provider, model, pid, now, now),
            )

    def update_worker_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        pid: int | None = None,
        token_count_delta: int = 0,
        touch: bool = True,
    ) -> None:
        """Update mutable fields on a worker session."""
        import time as _time
        now = _time.time()
        with self.conn() as conn:
            if status is not None:
                conn.execute(
                    "UPDATE worker_sessions SET status=? WHERE session_id=?",
                    (status, session_id),
                )
            if pid is not None:
                conn.execute(
                    "UPDATE worker_sessions SET pid=? WHERE session_id=?",
                    (pid, session_id),
                )
            if token_count_delta:
                conn.execute(
                    "UPDATE worker_sessions"
                    " SET token_count = token_count + ? WHERE session_id=?",
                    (token_count_delta, session_id),
                )
            if touch:
                conn.execute(
                    "UPDATE worker_sessions SET last_used_at=? WHERE session_id=?",
                    (now, session_id),
                )

    def get_worker_session(self, session_id: str) -> dict | None:
        """Return session row as dict or None."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT session_id, provider, model, pid, started_at,"
                " last_used_at, status, token_count"
                " FROM worker_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row[0], "provider": row[1], "model": row[2],
            "pid": row[3], "started_at": row[4], "last_used_at": row[5],
            "status": row[6], "token_count": row[7],
        }

    def list_worker_sessions(
        self,
        status: str | None = None,
        provider: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """List sessions filtered by optional status/provider."""
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if provider is not None:
            clauses.append("provider=?")
            params.append(provider)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self.conn() as conn:
            rows = conn.execute(
                f"SELECT session_id, provider, model, pid, started_at,"
                f" last_used_at, status, token_count"
                f" FROM worker_sessions {where} ORDER BY last_used_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "session_id": r[0], "provider": r[1], "model": r[2],
                "pid": r[3], "started_at": r[4], "last_used_at": r[5],
                "status": r[6], "token_count": r[7],
            }
            for r in rows
        ]

    def reap_idle_sessions(self, idle_ttl_seconds: float) -> list[str]:
        """Mark sessions idle longer than ttl as 'reaped'. Returns reaped session_ids."""
        import time as _time
        cutoff = _time.time() - idle_ttl_seconds
        with self.conn() as conn:
            rows = conn.execute(
                "UPDATE worker_sessions SET status='reaped'"
                " WHERE status='idle' AND last_used_at < ?"
                " RETURNING session_id",
                (cutoff,),
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Contextual bandit routing decisions (plan 11)
    # ------------------------------------------------------------------

    def log_routing_decision(
        self,
        task_id: str,
        features: list[float],
        heuristic_pick: str,
        bandit_pick: str,
        chosen: str,
    ) -> int:
        """Record a shadow-mode routing decision. Returns row id."""
        import time as _time
        with self.conn() as conn:
            cursor = conn.execute(
                "INSERT INTO routing_decisions"
                " (task_id, features, heuristic_pick, bandit_pick, chosen, ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    __import__("json").dumps(features),
                    heuristic_pick,
                    bandit_pick,
                    chosen,
                    _time.time(),
                ),
            )
            return cursor.lastrowid or 0

    def update_routing_decision_outcome(
        self,
        task_id: str,
        outcome_score: float,
        regret: float | None = None,
    ) -> None:
        """Attach outcome to the most recent decision for task_id."""
        with self.conn() as conn:
            conn.execute(
                "UPDATE routing_decisions"
                " SET outcome_score=?, regret=?"
                " WHERE task_id=? AND id=("
                "   SELECT MAX(id) FROM routing_decisions WHERE task_id=?"
                " )",
                (outcome_score, regret, task_id, task_id),
            )

    def get_bandit_summary(
        self,
        limit: int = 500,
        since_ts: float = 0.0,
    ) -> list[dict]:
        """Return recent routing decisions for win-rate analysis."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT task_id, heuristic_pick, bandit_pick, chosen,"
                " outcome_score, regret, ts"
                " FROM routing_decisions"
                " WHERE ts >= ?"
                " ORDER BY ts DESC LIMIT ?",
                (since_ts, limit),
            ).fetchall()
        return [
            {
                "task_id": r[0],
                "heuristic_pick": r[1],
                "bandit_pick": r[2],
                "chosen": r[3],
                "outcome_score": r[4],
                "regret": r[5],
                "ts": r[6],
            }
            for r in rows
        ]

    def record_cost_telemetry(
        self,
        task_id: str,
        tier: str,
        provider_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        est_cost_usd: float,
        counterfactual_tier: str = "high",
        counterfactual_cost_usd: float = 0.0,
    ) -> None:
        """Record per-subtask cost telemetry for savings tracking."""
        import time as _time
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO cost_telemetry"
                " (task_id, tier, provider_id, model, input_tokens, output_tokens,"
                "  est_cost_usd, counterfactual_tier, counterfactual_cost_usd, ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id, tier, provider_id, model,
                    input_tokens, output_tokens, est_cost_usd,
                    counterfactual_tier, counterfactual_cost_usd,
                    _time.time(),
                ),
            )

    def get_cost_summary(
        self,
        since_ts: float = 0.0,
        group_by: str = "tier",
    ) -> list[dict]:
        """Return aggregated cost summary since since_ts, grouped by group_by.

        group_by: "tier" | "provider" | "model"
        """
        allowed_groups = {"tier", "provider_id", "model"}
        col = group_by if group_by in allowed_groups else "tier"
        with self.conn() as conn:
            rows = conn.execute(
                f"SELECT {col}, SUM(input_tokens), SUM(output_tokens),"
                f"  SUM(est_cost_usd), SUM(counterfactual_cost_usd), COUNT(*)"
                f" FROM cost_telemetry WHERE ts >= ? GROUP BY {col}",
                (since_ts,),
            ).fetchall()
        result = []
        for row in rows:
            savings = (row[4] or 0.0) - (row[3] or 0.0)
            result.append({
                col: row[0],
                "input_tokens": int(row[1] or 0),
                "output_tokens": int(row[2] or 0),
                "est_cost_usd": round(float(row[3] or 0.0), 6),
                "counterfactual_cost_usd": round(float(row[4] or 0.0), 6),
                "savings_usd": round(savings, 6),
                "subtask_count": int(row[5] or 0),
            })
        return result

    def create_remote_job(
        self,
        job_id: str,
        task: str,
        user_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        """Create a new remote_jobs record with status='pending'.

        *user_id* associates the job with a registered server user.  Pass
        ``None`` for admin-submitted jobs (backward-compatible default).
        *idempotency_key* deduplicates concurrent submits of the same job.
        """
        now = __import__("time").time()
        with self.conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO remote_jobs"
                " (job_id, status, task, user_id, idempotency_key, created_ts, updated_ts)"
                " VALUES (?, 'pending', ?, ?, ?, ?, ?)",
                (job_id, task, user_id, idempotency_key, now, now),
            )

    def update_remote_job(
        self,
        job_id: str,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        """Update status, result, and/or error for a remote_jobs record."""
        now = __import__("time").time()
        with self.conn() as conn:
            conn.execute(
                "UPDATE remote_jobs SET status=?, result=?, error=?, updated_ts=? WHERE job_id=?",
                (status, result, error, now, job_id),
            )

    def get_remote_job(self, job_id: str, user_id: str | None = None) -> dict | None:
        """Return the remote_jobs row for job_id, or None if not found.

        If *user_id* is provided the row is only returned when it belongs to
        that user (admin callers pass ``user_id=None`` to bypass the filter).
        """
        with self.conn() as conn:
            if user_id is not None:
                # Strict ownership: only return if user_id matches exactly
                row = conn.execute(
                    "SELECT job_id, status, task, result, error, created_ts, updated_ts, user_id"
                    " FROM remote_jobs WHERE job_id=? AND user_id=?",
                    (job_id, user_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT job_id, status, task, result, error, created_ts, updated_ts, user_id"
                    " FROM remote_jobs WHERE job_id=?",
                    (job_id,),
                ).fetchone()
        if row is None:
            return None
        return {
            "job_id": row[0],
            "status": row[1],
            "task": row[2],
            "result": row[3],
            "error": row[4],
            "created_ts": row[5],
            "updated_ts": row[6],
            "user_id": row[7],
        }

    def list_user_jobs(self, user_id: str) -> list[dict]:
        """Return all remote_jobs rows owned by *user_id*, newest first."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT job_id, status, task, result, error, created_ts, updated_ts, user_id"
                " FROM remote_jobs WHERE user_id=? ORDER BY created_ts DESC",
                (user_id,),
            ).fetchall()
        return [
            {
                "job_id": r[0], "status": r[1], "task": r[2],
                "result": r[3], "error": r[4],
                "created_ts": r[5], "updated_ts": r[6], "user_id": r[7],
            }
            for r in rows
        ]

    def list_all_jobs(self) -> list[dict]:
        """Return all remote_jobs rows, newest first (admin view)."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT job_id, status, task, result, error, created_ts, updated_ts, user_id"
                " FROM remote_jobs ORDER BY created_ts DESC"
            ).fetchall()
        return [
            {
                "job_id": r[0], "status": r[1], "task": r[2],
                "result": r[3], "error": r[4],
                "created_ts": r[5], "updated_ts": r[6], "user_id": r[7],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # User management (multi-user remote server)
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        raw_token: str,
        providers_json: str = "{}",
        *,
        secret: str = "",
    ) -> str:
        """Register a new user.  Returns the generated user_id.

        Args:
            username:      Unique display name for the user.
            raw_token:     The bearer token the user will authenticate with.
            providers_json: JSON string of per-provider credentials.
            secret:        If provided, the token is stored as
                           ``hmac(secret, raw_token)`` rather than in plain
                           text.  Pass the server's admin token here.
        """
        import uuid as _uuid, hashlib as _hashlib, hmac as _hmac_mod
        user_id = str(_uuid.uuid4())
        token_hmac = (
            _hmac_mod.new(secret.encode(), raw_token.encode(), _hashlib.sha256).hexdigest()
            if secret else raw_token
        )
        now = __import__("time").time()
        try:
            with self.conn() as conn:
                conn.execute(
                    "INSERT INTO users"
                    " (user_id, username, token_hmac, providers_json, enabled, created_ts, updated_ts)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (user_id, username, token_hmac, providers_json, now, now),
                )
        except Exception as exc:
            raise ValueError(f"create_user failed: {exc}") from exc
        return user_id

    def get_user_by_token_hmac(self, token_hmac: str) -> dict | None:
        """Return the users row whose token_hmac matches, or None."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT user_id, username, providers_json, enabled, created_ts, updated_ts"
                " FROM users WHERE token_hmac=?",
                (token_hmac,),
            ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row[0], "username": row[1],
            "providers_json": row[2], "enabled": bool(row[3]),
            "created_ts": row[4], "updated_ts": row[5],
        }

    def get_user_by_id(self, user_id: str) -> dict | None:
        """Return the users row for user_id, or None."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT user_id, username, providers_json, enabled, created_ts, updated_ts"
                " FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row[0], "username": row[1],
            "providers_json": row[2], "enabled": bool(row[3]),
            "created_ts": row[4], "updated_ts": row[5],
        }

    def get_user_by_username(self, username: str) -> dict | None:
        """Return the users row for username, or None."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT user_id, username, providers_json, enabled, created_ts, updated_ts"
                " FROM users WHERE username=?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row[0], "username": row[1],
            "providers_json": row[2], "enabled": bool(row[3]),
            "created_ts": row[4], "updated_ts": row[5],
        }

    def list_users(self) -> list[dict]:
        """Return all user rows ordered by username."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT user_id, username, providers_json, enabled, created_ts, updated_ts"
                " FROM users ORDER BY username"
            ).fetchall()
        return [
            {
                "user_id": r[0], "username": r[1],
                "providers_json": r[2], "enabled": bool(r[3]),
                "created_ts": r[4], "updated_ts": r[5],
            }
            for r in rows
        ]

    def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        """Enable or disable a user account."""
        now = __import__("time").time()
        with self.conn() as conn:
            conn.execute(
                "UPDATE users SET enabled=?, updated_ts=? WHERE user_id=?",
                (1 if enabled else 0, now, user_id),
            )

    def update_user_token_hmac(self, user_id: str, raw_token: str, *, secret: str = "") -> None:
        """Replace the stored token HMAC for a user (token rotation).

        Args:
            user_id:   The target user's UUID.
            raw_token: The new bearer token (plain text).
            secret:    If provided, the token is stored as
                       ``hmac(secret, raw_token)``.  Pass the server's admin
                       token here to match the authentication logic.
        """
        import hashlib as _hashlib, hmac as _hmac_mod
        token_hmac = (
            _hmac_mod.new(secret.encode(), raw_token.encode(), _hashlib.sha256).hexdigest()
            if secret else raw_token
        )
        now = __import__("time").time()
        try:
            with self.conn() as conn:
                conn.execute(
                    "UPDATE users SET token_hmac=?, updated_ts=? WHERE user_id=?",
                    (token_hmac, now, user_id),
                )
        except Exception as exc:
            raise ValueError(f"update_user_token_hmac failed: {exc}") from exc

    def delete_user(self, user_id: str) -> None:
        """Permanently remove a user and their jobs."""
        with self.conn() as conn:
            conn.execute("DELETE FROM remote_jobs WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))

    def close(self) -> None:
        """Close all database connections
 and clean up resources."""
        # Close legacy thread-pool connections
        with self._legacy_conn_lock:
            for conn in self._legacy_conns.values():
                conn.close()
            self._legacy_conns.clear()
        
        # Close thread-local connections (FNDX-01)
        # Note: We can only close the current thread's connection from this thread.
        # Other threads' connections remain until those threads exit.
        if hasattr(self._thread_local, 'conn'):
            try:
                self._thread_local.conn.close()
            except Exception as e:
                log.debug(f"Error closing thread-local connection: {e}", exc_info=True)
            finally:
                # Clean up the thread-local attribute
                if hasattr(self._thread_local, 'conn'):
                    delattr(self._thread_local, 'conn')

    # ------------------------------------------------------------------
    # Routing exceptions CRUD
    # ------------------------------------------------------------------

    _VALID_EXCEPTION_TYPES: frozenset[str] = frozenset({
        "skill", "filetype", "project", "command", "caller", "path",
    })

    def routing_exception_add(
        self,
        exception_type: str,
        pattern: str,
        note: str | None = None,
    ) -> dict[str, object]:
        """Persist a routing bypass rule. Returns the stored row as a dict."""
        exc_type = (exception_type or "").strip().lower()
        if exc_type not in self._VALID_EXCEPTION_TYPES:
            raise ValueError(
                f"Invalid exception_type '{exception_type}'. "
                f"Must be one of: {', '.join(sorted(self._VALID_EXCEPTION_TYPES))}"
            )
        pat = (pattern or "").strip()
        if not pat:
            raise ValueError("pattern must not be empty")
        with self.conn() as conn:
            conn.execute(
                """
                INSERT INTO routing_exceptions (exception_type, pattern, note, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(exception_type, pattern) DO UPDATE SET
                    note = excluded.note,
                    created_at = excluded.created_at
                """,
                (exc_type, pat, note or None, time.time()),
            )
        return {"exception_type": exc_type, "pattern": pat, "note": note}

    def routing_exception_remove(
        self,
        exception_type: str,
        pattern: str,
    ) -> bool:
        """Delete a routing bypass rule. Returns True if a row was removed."""
        exc_type = (exception_type or "").strip().lower()
        pat = (pattern or "").strip()
        with self.conn() as conn:
            cursor = conn.execute(
                "DELETE FROM routing_exceptions WHERE exception_type = ? AND pattern = ?",
                (exc_type, pat),
            )
            return cursor.rowcount > 0

    def routing_exception_list(self) -> list[dict[str, object]]:
        """Return all routing bypass rules ordered by type then pattern."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT id, exception_type, pattern, note, created_at "
                "FROM routing_exceptions ORDER BY exception_type, pattern"
            ).fetchall()
        return [
            {
                "id": row[0],
                "exception_type": row[1],
                "pattern": row[2],
                "note": row[3],
                "created_at": row[4],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Provider health — circuit-breaker state                             #
    # ------------------------------------------------------------------ #

    def get_provider_health(self, provider_id: str) -> dict[str, object] | None:
        """Return the current health row for one provider, or None if unseen."""
        with self.conn() as conn:
            row = conn.execute(
                "SELECT provider_id, state, consecutive_failures, "
                "last_failure_ts, last_failure_category, last_failure_stderr, "
                "quarantine_until_ts, last_probe_ts, last_probe_ok, updated_ts "
                "FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "provider_id": row[0],
            "state": row[1],
            "consecutive_failures": row[2],
            "last_failure_ts": row[3],
            "last_failure_category": row[4],
            "last_failure_stderr": row[5],
            "quarantine_until_ts": row[6],
            "last_probe_ts": row[7],
            "last_probe_ok": bool(row[8]) if row[8] is not None else None,
            "updated_ts": row[9],
        }

    def iter_provider_health(self) -> list[dict[str, object]]:
        """Return health rows for all known providers, ordered by provider_id."""
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT provider_id, state, consecutive_failures, "
                "last_failure_ts, last_failure_category, last_failure_stderr, "
                "quarantine_until_ts, last_probe_ts, last_probe_ok, updated_ts "
                "FROM provider_health ORDER BY provider_id"
            ).fetchall()
        return [
            {
                "provider_id": row[0],
                "state": row[1],
                "consecutive_failures": row[2],
                "last_failure_ts": row[3],
                "last_failure_category": row[4],
                "last_failure_stderr": row[5],
                "quarantine_until_ts": row[6],
                "last_probe_ts": row[7],
                "last_probe_ok": bool(row[8]) if row[8] is not None else None,
                "updated_ts": row[9],
            }
            for row in rows
        ]

    def update_provider_health_state(
        self,
        provider_id: str,
        state: str,
        *,
        consecutive_failures: int | None = None,
        last_failure_ts: float | None = None,
        last_failure_category: str | None = None,
        last_failure_stderr: str | None = None,
        quarantine_until_ts: float | None = None,
        last_probe_ts: float | None = None,
        last_probe_ok: bool | None = None,
    ) -> None:
        """Upsert the health state for a provider."""
        now = time.time()
        with self.conn() as conn:
            existing = conn.execute(
                "SELECT consecutive_failures FROM provider_health WHERE provider_id = ?",
                (provider_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO provider_health
                        (provider_id, state, consecutive_failures,
                         last_failure_ts, last_failure_category, last_failure_stderr,
                         quarantine_until_ts, last_probe_ts, last_probe_ok, updated_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        provider_id, state,
                        consecutive_failures if consecutive_failures is not None else 0,
                        last_failure_ts, last_failure_category,
                        last_failure_stderr, quarantine_until_ts,
                        last_probe_ts,
                        int(last_probe_ok) if last_probe_ok is not None else None,
                        now,
                    ),
                )
            else:
                current_failures = existing[0]
                conn.execute(
                    """
                    UPDATE provider_health SET
                        state = ?,
                        consecutive_failures = ?,
                        last_failure_ts = COALESCE(?, last_failure_ts),
                        last_failure_category = COALESCE(?, last_failure_category),
                        last_failure_stderr = COALESCE(?, last_failure_stderr),
                        quarantine_until_ts = COALESCE(?, quarantine_until_ts),
                        last_probe_ts = COALESCE(?, last_probe_ts),
                        last_probe_ok = COALESCE(?, last_probe_ok),
                        updated_ts = ?
                    WHERE provider_id = ?
                    """,
                    (
                        state,
                        consecutive_failures if consecutive_failures is not None
                        else current_failures,
                        last_failure_ts, last_failure_category,
                        last_failure_stderr, quarantine_until_ts,
                        last_probe_ts,
                        int(last_probe_ok) if last_probe_ok is not None else None,
                        now, provider_id,
                    ),
                )
