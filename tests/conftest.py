#!/usr/bin/env python3
"""
Shared pytest fixtures for Phase 5 test infrastructure.

Provides hermetic test isolation for concurrent DB access, parallel wave
execution, path validation, and provider discovery testing. All fixtures
are function-scoped and clean up after themselves.

Used by:
  - tests/test_concurrency.py (concurrent DB and wave parallelism)
  - tests/test_mcp_security.py (path validation and traversal guards)
  - tests/test_eval.py (warm-path background evaluation)
  - Phase 5 implementation tests (FNDX-01, FNDX-02, FNDX-03, FNDX-04, TEST-01)
"""
from __future__ import annotations

import sys
import shutil
import tempfile
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Iterator, Any

import pytest

# Ensure the project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.db import Database
from shared.discovery import CLIProvider, ProviderReadiness, DetectReason
from shared.config import TGsConfig


# ============================================================================
# FIXTURE 1: temp_db_fixture
# ============================================================================


@pytest.fixture(scope="function")
def temp_db_fixture() -> Iterator[Database]:
    """
    Provide a temporary SQLite Database instance isolated from production.
    
    Returns:
        A Database object backed by a temporary database file with WAL mode
        enabled and all tables initialized (schema ready for inserts).
    
    Cleanup:
        Temporary database file is deleted after test completes.
    
    Usage in Phase 5 tests:
        - Concurrent DB writes via ThreadPoolExecutor workers
        - Wave execution with parallel subtask persists
        - Warm-path background evaluation writes telemetry
        - Thread-local connection verification
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    
    db = Database(db_path=db_path)
    
    # Initialize schema (all tables created)
    with db.conn() as conn:
        # Schema initialization is done in Database._init_schema called on first conn()
        pass
    
    yield db
    
    # Cleanup: close and delete temp database
    try:
        db.close()
    except Exception:
        pass
    
    try:
        db_path.unlink(missing_ok=True)
        # Also clean up WAL-related files
        (db_path.parent / f"{db_path.name}-wal").unlink(missing_ok=True)
        (db_path.parent / f"{db_path.name}-shm").unlink(missing_ok=True)
    except Exception:
        pass


# ============================================================================
# FIXTURE 2: mock_provider_fixture
# ============================================================================


@pytest.fixture(scope="function")
def mock_provider_fixture() -> CLIProvider:
    """
    Provide a mock CLIProvider for test isolation.
    
    Returns:
        A CLIProvider configured for testing with:
        - name: "test-provider"
        - binary: "test-binary" (not on PATH)
        - tier_models: low/medium/high mapped to test models
        - cost_rank: low=1, medium=2, high=3
        - detect_cmd: ["true"] (always succeeds without real subprocess)
    
    Notes:
        - detect() returns True without requiring real CLI binary
        - Used in discovery tests under THRENODY_TEST_MODE
        - Cost rank values are relative; matches BUILTIN_PROVIDERS pattern
    
    Usage in Phase 5 tests:
        - Provider registry discovery tests
        - Wave execution with mock provider
        - Agent result persistence verification
    """
    provider = CLIProvider(
        name="test-provider",
        binary="test-binary",
        display_name="Test Provider",
        tier_models={
            "low": "test-low-model",
            "medium": "test-med-model",
            "high": "test-high-model",
        },
        cost_rank={"low": 1, "medium": 2, "high": 3},
        detect_cmd=["true"],  # Always succeeds
    )
    return provider


# ============================================================================
# FIXTURE 3: test_config_fixture
# ============================================================================


@pytest.fixture(scope="function")
def test_config_fixture() -> Iterator[TGsConfig]:
    """
    Provide a TGsConfig instance configured for Phase 5 tests.
    
    Returns:
        A TGsConfig with:
        - parallelism.enabled = True
        - parallelism.max_workers = 4 (reduced for test speed)
        - Other fields use TGsConfig defaults
    
    Side Effects:
        - Creates /tmp/test-project directory if it doesn't exist
    
    Cleanup:
        - Removes /tmp/test-project directory after test
    
    Usage in Phase 5 tests:
        - Path validation tests (trusted vs untrusted paths)
        - Parallel wave execution config
        - Warm-path scheduling behavior
        - Test isolation via THRENODY_TEST_MODE checks
    
    Note:
        - write_safety config will be added in Phase 5 Wave 2
        - For now, tests use explicit allowed_bases parameters
    """
    from shared.config import ParallelismConfig
    
    test_project_root = Path("/tmp/test-project")
    test_project_root.mkdir(parents=True, exist_ok=True)
    
    # Create config with Phase 5 test defaults
    parallelism = ParallelismConfig(enabled=True, max_workers=4)
    config = TGsConfig(parallelism=parallelism)
    
    # Store test project root as an attribute for test access
    config.write_safety_trusted_bases = [test_project_root]
    
    yield config
    
    # Cleanup: remove test project directory
    try:
        import shutil
        shutil.rmtree(test_project_root, ignore_errors=True)
    except Exception:
        pass


# ============================================================================
# FIXTURE: reset_registry (autouse for discovery tests)
# ============================================================================


@pytest.fixture(autouse=True)
def _auto_reset_singletons():
    """
    Reset module-level singletons before and after each test.

    Ensures that provider registry and mcp_server module-level globals
    don't carry state between tests. Critical for hermetic test isolation
    in THRENODY_TEST_MODE.

    Also saves/restores:
    - mcp_server._ensure_init (test_inspect_extensions replaces it with a lambda)
    - Provider-specific env markers that opencode/entry.py sets at module-import time
      (OPENCODE_HOST, OPENCODE_SESSION) — these cause detect_caller() to return "opencode"
      instead of "github-copilot" for all tests that run after test_entrypoints imports
      opencode.entry.
    """
    import os
    import shared.discovery as mod
    import mcp_server as mcp

    # Save state that may be mutated by tests
    saved_ensure_init = mcp._ensure_init
    saved_opencode_host = os.environ.get("OPENCODE_HOST")
    saved_opencode_session = os.environ.get("OPENCODE_SESSION")

    # Reset singletons and strip provider env markers for a clean slate
    mod._registry = None
    mcp._config = None
    mcp._db = None
    mcp._router = None
    mcp._planner = None
    mcp._orchestrator = None
    mcp._client_name = None
    os.environ.pop("OPENCODE_HOST", None)
    os.environ.pop("OPENCODE_SESSION", None)

    yield

    # Restore exactly what was there before this test ran
    mcp._ensure_init = saved_ensure_init
    if saved_opencode_host is None:
        os.environ.pop("OPENCODE_HOST", None)
    else:
        os.environ["OPENCODE_HOST"] = saved_opencode_host
    if saved_opencode_session is None:
        os.environ.pop("OPENCODE_SESSION", None)
    else:
        os.environ["OPENCODE_SESSION"] = saved_opencode_session

    mod._registry = None
    mcp._config = None
    mcp._db = None
    mcp._router = None
    mcp._planner = None
    mcp._orchestrator = None
    mcp._client_name = None


# ============================================================================
# WAVE 0 FIXTURES: Provider CLI mocking for Phase 7 smoke tests
# ============================================================================

# These fixtures support TEST-03 requirements: hermetic smoke tests that verify
# provider detection, auth, and result extraction without requiring real CLIs.


@pytest.fixture(scope="function")
def mock_codex_cli():
    """
    Mock Codex CLI subprocess.run for hermetic smoke testing.
    
    Returns a context manager that patches subprocess.run to simulate
    `codex exec -m MODEL -a never -o FILE PROMPT` behavior.
    
    Mock success response:
    - Returns CompletedProcess(returncode=0, stdout="generated code snippet")
    
    Mock failure cases (configurable via test):
    - Auth error: OPENAI_API_KEY missing triggers auth failure detection
    - Binary missing: simulates codex executable not found
    - Timeout: simulates execution timeout
    
    Usage in tests:
        with mock_codex_cli() as mock:
            mock.return_value = CompletedProcess(...)
            # test execution code
    """
    from subprocess import CompletedProcess
    
    def _mock_codex(cmd, *args, **kwargs):
        # Simulate success for standard Codex command
        if cmd[0] == "codex" and "exec" in cmd:
            return CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="def hello():\n    return 42\n",
                stderr=""
            )
        # Simulate version check success
        if cmd == ["codex", "--version"]:
            return CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="codex version 1.0.0\n",
                stderr=""
            )
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="Unknown command")
    
    with patch("subprocess.run", side_effect=_mock_codex) as mock:
        yield mock


@pytest.fixture(scope="function")
def mock_cursor_cli():
    """
    Mock Cursor CLI subprocess.run for headless binary detection and execution.
    
    Simulates `cursor-agent --model MODEL --code-only PROMPT` behavior.
    
    Mock success response:
    - Returns CompletedProcess(returncode=0, stdout="generated async code")
    
    Mock behaviors:
    - --version probe returns version string
    - --code-only flag is recognized
    - Failure when cursor-agent binary not found
    
    Usage in tests:
        with mock_cursor_cli() as mock:
            # test cursor headless detection and execution
    """
    from subprocess import CompletedProcess
    
    def _mock_cursor(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 0:
            # Version check
            if cmd == ["cursor-agent", "--version"]:
                return CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="cursor-agent 0.1.0\n",
                    stderr=""
                )
            # Code generation
            if cmd[0] == "cursor-agent" and "--model" in cmd:
                return CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="async function fetch() {\n  return data;\n}\n",
                    stderr=""
                )
        return CompletedProcess(args=cmd, returncode=127, stdout="", stderr="Not found")
    
    with patch("subprocess.run", side_effect=_mock_cursor) as mock:
        yield mock


@pytest.fixture(scope="function")
def mock_junie_cli():
    """
    Mock Junie CLI subprocess.run with realistic JSON output and telemetry.
    
    Simulates `junie PROMPT --output-format=json` behavior.
    
    Mock success response includes:
    - result: generated code string
    - sessionId: unique session identifier
    - llmUsage: array with token counts, model, and cost telemetry
    
    The llmUsage fixture contains all required fields per D-10:
    {
      "model": "claude-opus-4-1",
      "inputTokens": 42,
      "outputTokens": 156,
      "cost": 0.00234
    }
    
    Mock failure cases:
    - JSON parse error (malformed JSON)
    - Timeout
    - Auth failure
    
    Usage in tests:
        with mock_junie_cli() as mock:
            # test Junie JSON parsing and telemetry extraction
    """
    from subprocess import CompletedProcess
    
    def _mock_junie(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 0:
            # Junie execution with JSON output
            if cmd[0] == "junie" and "--output-format=json" in cmd:
                junie_json = {
                    "result": "import anthropic\nclient = anthropic.Anthropic()",
                    "sessionId": "junie-sess-12345",
                    "llmUsage": [
                        {
                            "model": "claude-opus-4-1",
                            "inputTokens": 42,
                            "outputTokens": 156,
                            "cost": 0.00234
                        }
                    ]
                }
                return CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=json.dumps(junie_json),
                    stderr=""
                )
            # Login status check for JetBrains-managed setup
            if cmd == ["junie", "login", "status"]:
                return CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="authenticated",
                    stderr=""
                )
        return CompletedProcess(args=cmd, returncode=1, stdout="", stderr="Error")
    
    with patch("subprocess.run", side_effect=_mock_junie) as mock:
        yield mock


@pytest.fixture(scope="function")
def mock_env(monkeypatch):
    """
    Mock environment variables for per-test auth simulation.
    
    Provides a monkeypatch-based way to temporarily set/unset auth env vars
    without side effects on the actual environment after test completes.
    
    Supported auth vars:
    - OPENAI_API_KEY (Codex authentication)
    - JUNIE_API_KEY (Junie BYOK authentication)
    
    Returns the monkeypatch object for direct use in tests.
    
    Usage in tests:
        def test_codex_with_auth(mock_env):
            mock_env.setenv("OPENAI_API_KEY", "sk-test-123")
            # test Codex detection with auth
        
        def test_codex_without_auth(mock_env):
            mock_env.delenv("OPENAI_API_KEY", raising=False)
            # test Codex detection without auth
        
        # Env vars are automatically restored after test completes
    """
    return monkeypatch


@pytest.fixture(scope="function")
def isolation_test_mode(monkeypatch):
    """
    Enable THRENODY_TEST_MODE for hermetic provider discovery tests.
    
    Sets THRENODY_TEST_MODE=1 to stub real provider detection and uses
    only test-provider instances instead of scanning PATH for actual CLIs.
    
    Also clears real auth env vars to prevent accidental live API calls:
    - OPENAI_API_KEY cleared
    - JUNIE_API_KEY cleared
    - Any CLAUDE_* vars cleared
    
    Cleanup:
    - THRENODY_TEST_MODE is unset after test completes
    - Real env vars are restored
    
    Usage in tests:
        def test_something(isolation_test_mode):
            # Test runs with THRENODY_TEST_MODE=1
            # and no real auth credentials visible
    """
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("JUNIE_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION", raising=False)
    
    yield monkeypatch
    
    monkeypatch.delenv("THRENODY_TEST_MODE", raising=False)


# ============================================================================
# FIXTURE: mock_aider_binary (Phase 8 - Aider detection tests)
# ============================================================================


@pytest.fixture(scope="function")
def mock_aider_binary(monkeypatch):
    """
    Mock aider binary for hermetic Aider detection tests.
    
    Patches shutil.which and subprocess.run to simulate:
    - aider binary present at /usr/local/bin/aider
    - --list-models succeeds with sample model output
    - Other aider commands handled gracefully
    
    Usage:
        def test_aider_detection(mock_aider_binary, monkeypatch):
            monkeypatch.setenv("OPENAI_API_KEY", "test-key")
            readiness = _detect_aider(provider)
            assert readiness.routeable
    """
    import subprocess
    
    def mock_which(cmd):
        if cmd == "aider":
            return "/usr/local/bin/aider"
        return None
    
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "aider":
            if "--list-models" in cmd:
                # Simulate successful model discovery
                return subprocess.CompletedProcess(
                    cmd, returncode=0,
                    stdout="claude-3-sonnet-20241022\nclaude-opus-20250514\n",
                    stderr=""
                )
            # Other aider commands fall through to error
        raise subprocess.CalledProcessError(1, cmd)
    
    monkeypatch.setattr("shutil.which", mock_which)
    monkeypatch.setattr("subprocess.run", mock_run)


# ============================================================================
# FIXTURE: mock_q_binary (Phase 8 - Amazon Q detection tests)
# ============================================================================


@pytest.fixture(scope="function")
def mock_q_binary(monkeypatch):
    """
    Mock q binary for hermetic Amazon Q detection tests.
    
    Patches shutil.which and subprocess.run to simulate:
    - q binary present at /usr/local/bin/q
    - configure --profile succeeds (auth probe succeeds)
    - Other q commands handled gracefully
    
    Usage:
        def test_q_detection(mock_q_binary):
            readiness = _detect_q_kiro(provider)
            assert readiness.routeable
            assert readiness.metadata["binary"] == "q"
    """
    import subprocess
    
    def mock_which(cmd):
        if cmd == "q":
            return "/usr/local/bin/q"
        return None
    
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "q":
            if "configure" in cmd:
                # Auth probe succeeds
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            # Other q commands fall through to error
        raise subprocess.CalledProcessError(1, cmd)
    
    monkeypatch.setattr("shutil.which", mock_which)
    monkeypatch.setattr("subprocess.run", mock_run)


# ============================================================================
# FIXTURE: mock_kiro_binary (Phase 8 - Amazon Q kiro fallback tests)
# ============================================================================


@pytest.fixture(scope="function")
def mock_kiro_binary(monkeypatch):
    """
    Mock kiro binary fallback for hermetic Amazon Q detection tests.
    
    Patches shutil.which and subprocess.run to simulate:
    - q binary NOT installed (returns None)
    - kiro binary present at /usr/local/bin/kiro
    - configure --profile succeeds (auth probe succeeds)
    - Tests the q → kiro fallback behavior per D-01
    
    Usage:
        def test_q_kiro_fallback(mock_kiro_binary):
            readiness = _detect_q_kiro(provider)
            assert readiness.routeable
            assert readiness.metadata["binary"] == "kiro"
    """
    import subprocess
    
    def mock_which(cmd):
        if cmd == "q":
            return None  # q not installed
        if cmd == "kiro":
            return "/usr/local/bin/kiro"
        return None
    
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "kiro":
            if "configure" in cmd:
                # Auth probe succeeds
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
            # Other kiro commands fall through to error
        raise subprocess.CalledProcessError(1, cmd)
    
    monkeypatch.setattr("shutil.which", mock_which)
    monkeypatch.setattr("subprocess.run", mock_run)


# ============================================================================
# HELPER: reset_registry (Phase 7+ - Test cleanup)
# ============================================================================


def reset_registry():
    """
    Reset the global ProviderRegistry singleton for test isolation.
    
    Used in test teardown to ensure provider state doesn't leak
    between test cases.
    """
    from shared import discovery
    if hasattr(discovery, "_registry"):
        discovery._registry = None


# ============================================================================
# FIXTURE: mock_aider_execution (Phase 8 - Aider execution tests)
# ============================================================================


@pytest.fixture(scope="function")
def mock_aider_execution(monkeypatch):
    """Mock Aider execution that modifies files and returns cost tracking."""
    import subprocess
    
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] == "aider" and "--message" in cmd:
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout="Fixed 2 functions in src/handler.py\nAdded test case in tests/test_handler.py\n",
                stderr="Total cost: $0.0042\n"
            )
        raise subprocess.CalledProcessError(1, cmd)
    
    monkeypatch.setattr("subprocess.run", mock_run)


# ============================================================================
# FIXTURE: mock_q_kiro_execution (Phase 8 - Amazon Q/Kiro execution tests)
# ============================================================================


@pytest.fixture(scope="function")
def mock_q_kiro_execution(monkeypatch):
    """Mock Amazon Q/Kiro execution that returns text output."""
    import subprocess
    
    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 0 and cmd[0] in ["q", "kiro"] and "chat" in cmd and "--no-interactive" in cmd:
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout="Here's the solution:\n\nclass Handler:\n    def process(self):\n        pass\n",
                stderr=""
            )
        raise subprocess.CalledProcessError(1, cmd)
    
    monkeypatch.setattr("subprocess.run", mock_run)


# ============================================================================
# FIXTURE: mock_mistral_cli (Mistral Vibe detection/execution tests)
# ============================================================================


@pytest.fixture(scope="function")
def mock_mistral_cli(monkeypatch):
    """Mock Mistral Vibe CLI for hermetic provider tests.

    Patches shutil.which so ``vibe`` appears installed, and patches
    subprocess.run to return a canned text response when invoked with
    ``-p`` / ``--output text``.

    Usage::

        def test_something(mock_mistral_cli, monkeypatch):
            monkeypatch.setenv("MISTRAL_API_KEY", "test-key")
            from mistral.providers import _detect_mistral, build_mistral_provider
            p = build_mistral_provider()
            assert _detect_mistral(p).routeable
    """
    import subprocess

    original_which = shutil.which

    def mock_which(cmd):
        if cmd == "vibe":
            return "/usr/local/bin/vibe"
        return original_which(cmd)

    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["vibe"]:
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout="Here is the result from vibe.\n",
                stderr=""
            )
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("shutil.which", mock_which)
    monkeypatch.setattr("subprocess.run", mock_run)


# ============================================================================
# FIXTURE: mock_blackbox_cli (Blackbox AI detection/execution tests)
# ============================================================================


@pytest.fixture(scope="function")
def mock_blackbox_cli(monkeypatch):
    """Mock Blackbox AI CLI for hermetic provider tests.

    Patches shutil.which so ``blackbox`` appears installed, and patches
    subprocess.run to return a canned response.

    Usage::

        def test_something(mock_blackbox_cli, monkeypatch):
            monkeypatch.setenv("BLACKBOX_API_KEY", "test-key")
            from blackbox.providers import _detect_blackbox, build_blackbox_provider
            p = build_blackbox_provider()
            assert _detect_blackbox(p).routeable
    """
    import subprocess

    original_which = shutil.which

    def mock_which(cmd):
        if cmd == "blackbox":
            return "/usr/local/bin/blackbox"
        return original_which(cmd)

    def mock_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:1] == ["blackbox"]:
            return subprocess.CompletedProcess(
                cmd, returncode=0,
                stdout="Here is the result from blackbox.\n",
                stderr=""
            )
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("shutil.which", mock_which)
    monkeypatch.setattr("subprocess.run", mock_run)


# ============================================================================
# FIXTURE: agent_learning_db (Phase 10 - Agent learning pattern tracking)
# ============================================================================


@pytest.fixture(scope="function")
def agent_learning_db() -> Iterator[Database]:
    """
    Provide a clean Database instance with schema for agent learning tests.
    
    Returns:
        A Database object backed by a temporary SQLite database with all schema
        tables initialized, including subtask_patterns, approval_queue, and
        agent_definitions tables needed for Phase 10 testing.
    
    Pre-populated with:
        - Test project ID: "test-project"
        - Empty pattern/approval/agent tables ready for test population
    
    Cleanup:
        Temporary database file is deleted after test completes.
    
    Usage in Phase 10 tests:
        - Pattern tracking and recording
        - Draft gate logic validation
        - Approval queue lifecycle testing
        - Integration tests for agent learning
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    
    db = Database(db_path=db_path)
    
    # Initialize schema (all tables created including subtask_patterns, approval_queue, agent_definitions)
    with db.conn() as conn:
        # Schema initialization happens automatically in Database._init_schema
        pass
    
    yield db
    
    # Cleanup: close and delete temp database
    try:
        db.close()
    except Exception:
        pass
    
    try:
        db_path.unlink(missing_ok=True)
        # Also clean up WAL-related files
        (db_path.parent / f"{db_path.name}-wal").unlink(missing_ok=True)
        (db_path.parent / f"{db_path.name}-shm").unlink(missing_ok=True)
    except Exception:
        pass


# ============================================================================
# FIXTURE: mature_pattern_seed (Phase 10 - Pre-seeded pattern for testing)
# ============================================================================


@pytest.fixture(scope="function")
def mature_pattern_seed(agent_learning_db: Database) -> dict:
    """
    Pre-seed a mature pattern into agent_learning_db for draft gate testing.
    
    Given an agent_learning_db fixture, inserts a pattern with:
        - project_id: "test-project"
        - pattern_hash: deterministic hash of "test pattern for mature detection"
        - description: "test pattern for mature detection"
        - occurrence_count: 5 (meets recurrence threshold)
        - tier: "low"
        - examples: ["example 1", "example 2"]
        - rework_detected: False (positive signal)
        - eval_quality: 0.85 (acceptable quality)
    
    Returns:
        dict with keys: {project_id, pattern_hash, description, tier, occurrence_count}
    
    Usage in Phase 10 tests:
        - Draft gate True-case testing (pattern is ready)
        - Combined proof verification (recurrence + quality + low rework)
        - Tests that verify draft enqueuing on combined proof
    
    Note:
        Uses shared.agents.pattern_hash() for deterministic hashing.
    """
    from shared.agents import pattern_hash
    
    project_id = "test-project"
    description = "write tests for our mature detection flow"
    ph = pattern_hash(description)
    
    # Insert pattern with full quality signals
    with agent_learning_db.conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO subtask_patterns
            (pattern_hash, pattern_desc, occurrence_count, tier, last_seen, examples, rework_detected, eval_quality)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ph,
                description,
                5,  # occurrence_count (meets project-lane threshold)
                "low",  # tier
                time.time(),  # last_seen
                json.dumps(["example 1", "example 2"]),  # examples
                False,  # rework_detected
                0.85,  # eval_quality
            ),
        )
    
    return {
        "project_id": project_id,
        "pattern_hash": ph,
        "description": description,
        "tier": "low",
        "occurrence_count": 5,
    }
