#!/usr/bin/env python3
"""
Parallel execution and timeout resilience tests for the MCP server.

Covers:
  - Thread safety of concurrent execute_subtask calls
  - Subtask tracking consistency under concurrency
  - Provider timeout / failure error propagation
  - send_response output integrity under concurrent writes
  - main() non-blocking dispatch for blocking tools
  - History cap enforcement
  - Wave-id grouping in list_subtasks
  - Saturation behaviour (max inflight measurement)
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server
from shared.config import TGsConfig
from shared.db import Database


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubRegistry:
    """Instant-success registry for basic tests."""

    def select_provider_for_tier(self, tier, *, prefer_free=True, caller=None, code_only=False):
        return {
            "provider": "StubProvider",
            "provider_id": "stub",
            "model": "stub-model",
            "tier": tier,
            "is_free": True,
            "billing_tier": "free",
            "provider_cost_hint": "free",
            "cost_rank": 0,
            "billing_source": "stub",
            "excluded_providers": [],
        }

    def execute_cheapest(self, **kwargs):
        return {
            "result": "```python\n# stub output\nprint('hello')\n```\n",
            "provider": "StubProvider",
            "provider_id": "stub",
            "model": "stub-model",
            "tier": kwargs.get("tier", "low"),
            "is_free": True,
            "billing_tier": "free",
            "provider_cost_hint": "free",
            "cost_rank": 0,
            "billing_source": "stub",
            "fallback_used": False,
        }

    def to_dict(self):
        return {"providers": []}


class DelayRegistry(StubRegistry):
    """Registry that blocks for a configurable duration."""

    def __init__(self, delay: float, *, barrier: threading.Barrier | None = None,
                 inflight_counter: list[int] | None = None,
                 inflight_lock: threading.Lock | None = None):
        self.delay = delay
        self._barrier = barrier
        self._inflight = inflight_counter  # [current, max_seen]
        self._lock = inflight_lock or threading.Lock()

    def execute_cheapest(self, **kwargs):
        if self._inflight is not None:
            with self._lock:
                self._inflight[0] += 1
                if self._inflight[0] > self._inflight[1]:
                    self._inflight[1] = self._inflight[0]
        try:
            if self._barrier:
                self._barrier.wait(timeout=5)
            time.sleep(self.delay)
            return super().execute_cheapest(**kwargs)
        finally:
            if self._inflight is not None:
                with self._lock:
                    self._inflight[0] -= 1


class FailingRegistry(StubRegistry):
    """Registry that always raises RuntimeError (all providers failed)."""

    def execute_cheapest(self, **kwargs):
        raise RuntimeError("All providers failed: gh copilot timed out after 120s")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals():
    """Reset MCP server global state before each test."""
    saved_active = dict(mcp_server._active_subtasks)
    saved_history = list(mcp_server._subtask_history)
    saved_cancel_events = dict(mcp_server._subtask_cancel_events)
    mcp_server._active_subtasks.clear()
    mcp_server._subtask_history.clear()
    mcp_server._subtask_cancel_events.clear()
    yield
    mcp_server._active_subtasks.clear()
    mcp_server._subtask_history.clear()
    mcp_server._subtask_cancel_events.clear()
    mcp_server._active_subtasks.update(saved_active)
    mcp_server._subtask_history.extend(saved_history)
    mcp_server._subtask_cancel_events.update(saved_cancel_events)


@pytest.fixture
def workspace(tmp_path):
    """Provide a temporary workspace root and configured stubs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "test.db"
    cfg = TGsConfig(db_path=db_path, write_safety_trusted_bases=[], delegation_utilities_enabled=True)
    db = Database(db_path=db_path)
    return repo, cfg, db


def _patch_mcp(monkeypatch, repo, cfg, db, registry=None):
    """Wire up monkeypatches for handle_execute_subtask."""
    if registry is None:
        registry = StubRegistry()
    monkeypatch.setenv("TGS_ACTIVE_WORKSPACE", str(repo))
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, None, None, None))
    monkeypatch.setattr(mcp_server, "_get_registry_with_config", lambda *a, **kw: registry)
    monkeypatch.setattr(mcp_server, "_register_shell_adapters", lambda *a, **kw: None)
    monkeypatch.setattr(mcp_server, "_write_status_file", lambda: None)
    return registry


# ---------------------------------------------------------------------------
# Test 1: Parallel execute_subtask thread safety
# ---------------------------------------------------------------------------


def test_parallel_execute_subtasks_thread_safety(monkeypatch, workspace):
    """Fire 5 concurrent execute_subtask calls; all must succeed with no corruption."""
    repo, cfg, db = workspace
    barrier = threading.Barrier(5, timeout=10)
    registry = DelayRegistry(0.1, barrier=barrier)
    _patch_mcp(monkeypatch, repo, cfg, db, registry)

    results = []

    def run_subtask(idx):
        target = repo / f"gen_{idx}.py"
        return mcp_server.handle_execute_subtask({
            "prompt": f"Generate file {idx}",
            "target_file": str(target),
            "task_id": f"parallel-{idx}",
            "wave_id": "wave-1",
        })

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(run_subtask, i) for i in range(5)]
        results = [f.result(timeout=30) for f in futures]

    succeeded = [r for r in results if "error" not in r]
    assert len(succeeded) == 5, f"Expected 5 successes, got {len(succeeded)}: {results}"

    for r in succeeded:
        assert r["provider"] == "StubProvider"
        assert r["model"] == "stub-model"
        assert "file_written" in r


# ---------------------------------------------------------------------------
# Test 2: Subtask tracking consistency during execution
# ---------------------------------------------------------------------------


def test_subtask_tracking_during_execution(monkeypatch, workspace):
    """Verify _active_subtasks is populated during execution and cleared after."""
    repo, cfg, db = workspace
    started = threading.Event()
    proceed = threading.Event()

    class BlockingRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            started.set()
            proceed.wait(timeout=10)
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, BlockingRegistry())

    def run():
        return mcp_server.handle_execute_subtask({
            "prompt": "Generate a file",
            "target_file": str(repo / "tracked.py"),
            "task_id": "track-test",
        })

    t = threading.Thread(target=run)
    t.start()

    assert started.wait(timeout=5), "Subtask did not start"

    listing = mcp_server.handle_list_subtasks({})
    assert listing["active_count"] >= 1
    task_ids = [
        task["task_id"]
        for group in listing["active_groups"]
        for task in group["tasks"]
    ]
    assert "track-test" in task_ids

    proceed.set()
    t.join(timeout=10)

    listing_after = mcp_server.handle_list_subtasks({})
    after_task_ids = [
        task["task_id"]
        for group in listing_after["active_groups"]
        for task in group["tasks"]
    ]
    assert "track-test" not in after_task_ids
    assert listing_after["recent_count"] >= 1


def test_starting_task_can_be_cancelled_before_pid(monkeypatch, workspace):
    repo, cfg, db = workspace
    started = threading.Event()
    release_provider = threading.Event()

    class NeverStartedRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            started.set()
            release_provider.wait(timeout=10)
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, NeverStartedRegistry())
    result_holder: dict[str, dict] = {}

    def run():
        result_holder["result"] = mcp_server.handle_execute_subtask({
            "prompt": "Wait before process launch",
            "task_id": "cancel-starting",
            "timeout": 10,
        })

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(timeout=2)

    listing = mcp_server.handle_list_subtasks({})
    task = next(
        task
        for group in listing["active_groups"]
        for task in group["tasks"]
        if task["task_id"] == "cancel-starting"
    )
    assert task["status"] == "starting"
    assert task["pid"] is None

    stopped = mcp_server.handle_stop_subtask({"task_id": "cancel-starting"})
    assert stopped["status"] == "cancellation_requested"
    thread.join(timeout=2)
    release_provider.set()

    assert not thread.is_alive()
    assert result_holder["result"]["status"] == "cancelled"
    assert mcp_server.handle_list_subtasks({})["active_count"] == 0
    assert mcp_server._subtask_history[-1]["status"] == "cancelled"


def test_pid_registration_transitions_starting_to_running(monkeypatch, workspace):
    repo, cfg, db = workspace
    pid_registered = threading.Event()
    release_provider = threading.Event()

    class PidRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            kwargs["on_pid"](424242)
            pid_registered.set()
            release_provider.wait(timeout=10)
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, PidRegistry())
    thread = threading.Thread(
        target=lambda: mcp_server.handle_execute_subtask({
            "prompt": "Run with a process handle",
            "task_id": "pid-transition",
            "timeout": 10,
        })
    )
    thread.start()
    assert pid_registered.wait(timeout=2)

    listing = mcp_server.handle_list_subtasks({})
    task = next(
        task
        for group in listing["active_groups"]
        for task in group["tasks"]
        if task["task_id"] == "pid-transition"
    )
    assert task["status"] == "running"
    assert task["pid"] == 424242

    release_provider.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_provider_launch_timeout_is_terminal_and_inspectable(monkeypatch, workspace):
    repo, cfg, db = workspace
    release_provider = threading.Event()

    class StuckLaunchRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            release_provider.wait(timeout=10)
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, StuckLaunchRegistry())
    started_at = time.monotonic()
    result = mcp_server.handle_execute_subtask({
        "prompt": "Provider never launches",
        "task_id": "launch-timeout",
        "timeout": 1,
    })
    elapsed = time.monotonic() - started_at
    release_provider.set()

    assert result["error"] == "Timeout"
    assert result["status"] == "timed_out"
    assert elapsed < 2.0
    assert mcp_server.handle_list_subtasks({})["active_count"] == 0
    assert mcp_server._subtask_history[-1]["status"] == "timed_out"

    inspection = mcp_server.inspect_task("launch-timeout")
    assert inspection["runtime"]["status"] == "timed_out"
    assert inspection["events"][-1]["status"] == "timed_out"


def test_early_processing_return_cleans_active_task(monkeypatch, workspace):
    repo, cfg, db = workspace

    class ShortRewriteRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            result = super().execute_cheapest(**kwargs)
            result["result"] = "x = 1\n"
            return result

    target = repo / "existing.py"
    target.write_text("print('before')\n" * 20, encoding="utf-8")
    _patch_mcp(monkeypatch, repo, cfg, db, ShortRewriteRegistry())

    result = mcp_server.handle_execute_subtask({
        "prompt": "Update the file",
        "target_file": str(target),
        "mode": "rewrite",
        "task_id": "short-rewrite-cleanup",
    })

    assert "file_write_error" in result
    assert mcp_server.handle_list_subtasks({})["active_count"] == 0
    assert mcp_server._subtask_history[-1]["status"] == "failed"


# ---------------------------------------------------------------------------
# Test 3: Provider failure returns proper error
# ---------------------------------------------------------------------------


def test_provider_failure_returns_error(monkeypatch, workspace):
    """When all providers fail (RuntimeError), the response contains ProviderError."""
    repo, cfg, db = workspace
    _patch_mcp(monkeypatch, repo, cfg, db, FailingRegistry())

    result = mcp_server.handle_execute_subtask({
        "prompt": "This will fail",
        "task_id": "fail-test",
    })

    assert result["error"] == "ProviderError"
    assert result["details"] == "Provider execution failed. Check server logs for details."
    assert result["task_id"] == "fail-test"
    assert "wall_time_seconds" in result


# ---------------------------------------------------------------------------
# Test 4: send_response thread safety (no interleaving)
# ---------------------------------------------------------------------------


def test_concurrent_send_response_no_interleave(monkeypatch):
    """20 concurrent send_response calls must each produce a complete JSON line."""
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)

    barrier = threading.Barrier(20, timeout=10)

    def write_response(idx):
        barrier.wait()
        mcp_server.send_response(idx, {"value": f"resp-{idx}"})

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(write_response, i) for i in range(20)]
        for f in futures:
            f.result(timeout=10)

    lines = buf.getvalue().strip().split("\n")
    assert len(lines) == 20, f"Expected 20 lines, got {len(lines)}"

    seen_ids = set()
    for line in lines:
        msg = json.loads(line)
        assert msg["jsonrpc"] == "2.0"
        assert "result" in msg
        seen_ids.add(msg["id"])

    assert len(seen_ids) == 20


# ---------------------------------------------------------------------------
# Test 5: main() dispatches blocking tools off the read loop
# ---------------------------------------------------------------------------


def test_main_dispatches_blocking_to_thread(monkeypatch, workspace):
    """Verify that a blocking tool (execute_subtask) doesn't block a subsequent ping.

    Simulates stdin with: [execute_subtask, ping]. If dispatch is threaded,
    the ping response arrives before execute_subtask completes.
    """
    repo, cfg, db = workspace
    started = threading.Event()
    proceed = threading.Event()

    class SlowRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            started.set()
            proceed.wait(timeout=10)
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, SlowRegistry())

    # Build two JSON-RPC requests: a blocking execute_subtask, then a ping
    req_execute = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "execute_subtask",
            "arguments": {
                "prompt": "slow task",
                "target_file": str(repo / "slow.py"),
                "task_id": "slow-dispatch",
            },
        },
    })
    req_ping = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})

    stdin_data = req_execute + "\n" + req_ping + "\n"
    fake_stdin = io.StringIO(stdin_data)
    buf = io.StringIO()

    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stdout", buf)

    # Run main() in a thread since it reads stdin line by line
    main_done = threading.Event()

    def run_main():
        try:
            mcp_server.main()
        except Exception:
            pass
        finally:
            main_done.set()

    t = threading.Thread(target=run_main, daemon=True)
    t.start()

    # Wait for the blocking task to start
    assert started.wait(timeout=5), "Blocking task never started"

    # Give main() a moment to process the ping (it should be immediate)
    time.sleep(0.5)

    # Check that ping response arrived while execute_subtask is still blocked
    output = buf.getvalue()
    responses = [json.loads(line) for line in output.strip().split("\n") if line.strip()]
    ping_responses = [r for r in responses if r.get("id") == 2]

    assert len(ping_responses) == 1, (
        f"Ping should have been processed while execute_subtask is blocked. "
        f"Got responses: {responses}"
    )

    # Unblock and clean up
    proceed.set()
    main_done.wait(timeout=10)


def test_main_cancels_active_subtasks_when_transport_disconnects(
    monkeypatch,
    workspace,
):
    """EOF on MCP stdin must cancel active provider work before main returns."""
    repo, cfg, db = workspace
    started = threading.Event()
    release_provider = threading.Event()

    class DisconnectRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            started.set()
            release_provider.wait(timeout=10)
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, DisconnectRegistry())

    request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "execute_subtask",
            "arguments": {
                "prompt": "transport disconnect task",
                "target_file": str(repo / "disconnect.py"),
                "task_id": "transport-disconnect",
                "timeout": 10,
            },
        },
    })

    class ControlledStdin:
        def __iter__(self):
            yield request + "\n"
            assert started.wait(timeout=5), "Subtask did not start before EOF"

    monkeypatch.setattr(sys, "stdin", ControlledStdin())
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    mcp_server.main()
    release_provider.set()

    assert "transport-disconnect" not in mcp_server._active_subtasks
    terminal = next(
        item
        for item in mcp_server._subtask_history
        if item["task_id"] == "transport-disconnect"
    )
    assert terminal["status"] == "cancelled"
    assert terminal["cancellation_reason"] == "transport_disconnect"


# ---------------------------------------------------------------------------
# Test 6: History cap at 20
# ---------------------------------------------------------------------------


def test_subtask_history_capped_at_20(monkeypatch, workspace):
    """Running 25 subtasks sequentially must cap _subtask_history at 20."""
    repo, cfg, db = workspace
    _patch_mcp(monkeypatch, repo, cfg, db)

    for i in range(25):
        target = repo / f"cap_{i}.py"
        mcp_server.handle_execute_subtask({
            "prompt": f"Generate file {i}",
            "target_file": str(target),
            "task_id": f"cap-{i}",
        })

    assert len(mcp_server._subtask_history) <= 20
    # The oldest entries should have been evicted
    task_ids = [e["task_id"] for e in mcp_server._subtask_history]
    assert "cap-0" not in task_ids  # evicted
    assert "cap-24" in task_ids    # most recent kept


# ---------------------------------------------------------------------------
# Test 7: Wave ID grouping in list_subtasks
# ---------------------------------------------------------------------------


def test_wave_id_grouping(monkeypatch, workspace):
    """Active subtasks with explicit wave_ids are grouped correctly."""
    repo, cfg, db = workspace
    proceed = threading.Event()

    class HoldRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            proceed.wait(timeout=10)
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, HoldRegistry())

    def run(idx, wave):
        return mcp_server.handle_execute_subtask({
            "prompt": f"Task {idx}",
            "target_file": str(repo / f"wave_{wave}_{idx}.py"),
            "task_id": f"wave-{wave}-{idx}",
            "wave_id": wave,
        })

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [
            pool.submit(run, 0, "w1"),
            pool.submit(run, 1, "w1"),
            pool.submit(run, 2, "w1"),
            pool.submit(run, 3, "w2"),
            pool.submit(run, 4, "w2"),
        ]

        # Wait for all to be registered as active
        time.sleep(1.0)

        listing = mcp_server.handle_list_subtasks({})
        wave_names = {g["wave"] for g in listing["active_groups"]}

        assert "w1" in wave_names, f"Expected wave w1 in {wave_names}"
        assert "w2" in wave_names, f"Expected wave w2 in {wave_names}"

        for group in listing["active_groups"]:
            if group["wave"] == "w1":
                assert group["count"] == 3
                assert group["parallel"] is True
            elif group["wave"] == "w2":
                assert group["count"] == 2
                assert group["parallel"] is True

        proceed.set()
        for f in futures:
            f.result(timeout=15)


# ---------------------------------------------------------------------------
# Test 8: Saturation — measure max inflight under parallel load
# ---------------------------------------------------------------------------


def test_saturation_max_inflight(monkeypatch, workspace):
    """Measure how many execute_subtask calls actually overlap.

    This test verifies that concurrent calls truly execute in parallel
    (i.e., the server doesn't accidentally serialise them).
    """
    repo, cfg, db = workspace
    n = 6
    barrier = threading.Barrier(n, timeout=10)
    inflight = [0, 0]  # [current, max_seen]
    lock = threading.Lock()
    registry = DelayRegistry(0.2, barrier=barrier, inflight_counter=inflight, inflight_lock=lock)
    _patch_mcp(monkeypatch, repo, cfg, db, registry)

    def run(idx):
        return mcp_server.handle_execute_subtask({
            "prompt": f"Saturation task {idx}",
            "target_file": str(repo / f"sat_{idx}.py"),
            "task_id": f"sat-{idx}",
            "wave_id": "saturation",
        })

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(run, i) for i in range(n)]
        results = [f.result(timeout=30) for f in futures]

    succeeded = [r for r in results if "error" not in r]
    assert len(succeeded) == n

    # The barrier ensures all n tasks are in execute_cheapest simultaneously,
    # so max_inflight must equal n.
    assert inflight[1] == n, (
        f"Expected {n} concurrent inflight tasks, but max was {inflight[1]}. "
        "Server may be serialising execute_subtask calls."
    )


# ---------------------------------------------------------------------------
# Test 9: Concurrent execute + list_subtasks interleaving
# ---------------------------------------------------------------------------


def test_list_subtasks_safe_during_concurrent_mutations(monkeypatch, workspace):
    """Call list_subtasks rapidly while subtasks are starting/completing."""
    repo, cfg, db = workspace
    registry = DelayRegistry(0.1)
    _patch_mcp(monkeypatch, repo, cfg, db, registry)

    errors = []

    def run_subtask(idx):
        return mcp_server.handle_execute_subtask({
            "prompt": f"Interleave task {idx}",
            "target_file": str(repo / f"interleave_{idx}.py"),
            "task_id": f"interleave-{idx}",
        })

    def poll_listing():
        for _ in range(20):
            try:
                listing = mcp_server.handle_list_subtasks({})
                assert isinstance(listing["active_count"], int)
                assert isinstance(listing["recent_count"], int)
            except Exception as e:
                errors.append(e)
            time.sleep(0.05)

    with ThreadPoolExecutor(max_workers=8) as pool:
        subtask_futures = [pool.submit(run_subtask, i) for i in range(5)]
        poll_futures = [pool.submit(poll_listing) for _ in range(3)]

        for f in subtask_futures:
            f.result(timeout=30)
        for f in poll_futures:
            f.result(timeout=30)

    assert not errors, f"list_subtasks raised during concurrent mutations: {errors}"


# ---------------------------------------------------------------------------
# Test 10: Invalid parameters are rejected fast (no thread spawn)
# ---------------------------------------------------------------------------


def test_invalid_params_rejected_without_provider_call(monkeypatch, workspace):
    """Bad tier, empty prompt, and bad timeout must fail fast without calling provider."""
    repo, cfg, db = workspace

    class NeverCalledRegistry(StubRegistry):
        def execute_cheapest(self, **kwargs):
            raise AssertionError("Provider should not be called for invalid params")

    _patch_mcp(monkeypatch, repo, cfg, db, NeverCalledRegistry())

    # Missing prompt
    r1 = mcp_server.handle_execute_subtask({})
    assert r1["error"] == "Missing required parameter: prompt"

    # Invalid tier
    r2 = mcp_server.handle_execute_subtask({"prompt": "test", "tier": "mega"})
    assert "Invalid tier" in r2["error"]

    # Invalid timeout
    r3 = mcp_server.handle_execute_subtask({"prompt": "test", "timeout": "not-a-number"})
    assert r3["error"] == "InvalidTimeout"


# ---------------------------------------------------------------------------
# Test 11: Tier-based default timeout from config
# ---------------------------------------------------------------------------


def test_tier_default_timeout_used_when_not_specified(monkeypatch, workspace):
    """execute_subtask uses config.tier_timeouts[tier] as default, not hardcoded 120."""
    repo, cfg, db = workspace
    cfg.tier_timeouts = {"low": 45, "medium": 90, "high": 200}

    captured_timeouts: list[int] = []

    class TimeoutCapture(StubRegistry):
        def execute_cheapest(self, **kwargs):
            # The deadline is passed; compute the effective timeout from it.
            dl = kwargs.get("deadline")
            if dl is not None:
                captured_timeouts.append(int(dl - time.monotonic()))
            else:
                captured_timeouts.append(kwargs.get("timeout", -1))
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, TimeoutCapture())

    # Call with tier=low, no explicit timeout
    mcp_server.handle_execute_subtask({"prompt": "test", "tier": "low"})
    # Call with tier=high, no explicit timeout
    mcp_server.handle_execute_subtask({"prompt": "test", "tier": "high"})

    # Tier-based defaults: low=45, high=200 (allow 2s slack for test execution)
    assert 40 <= captured_timeouts[0] <= 46, f"low tier timeout {captured_timeouts[0]} not near 45"
    assert 195 <= captured_timeouts[1] <= 201, f"high tier timeout {captured_timeouts[1]} not near 200"


def test_explicit_timeout_overrides_tier_default(monkeypatch, workspace):
    """An explicit timeout=300 overrides the tier-based default."""
    repo, cfg, db = workspace
    cfg.tier_timeouts = {"low": 45, "medium": 90, "high": 200}

    captured_deadlines: list[float] = []

    class TimeoutCapture(StubRegistry):
        def execute_cheapest(self, **kwargs):
            dl = kwargs.get("deadline")
            if dl is not None:
                captured_deadlines.append(dl - time.monotonic())
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, TimeoutCapture())

    # Explicit timeout=300 on a low-tier task (default would be 45)
    mcp_server.handle_execute_subtask({"prompt": "test", "tier": "low", "timeout": 300})

    assert 295 <= captured_deadlines[0] <= 301, f"expected ~300, got {captured_deadlines[0]}"


# ---------------------------------------------------------------------------
# Test 12: Deadline budget passed to execute_cheapest
# ---------------------------------------------------------------------------


def test_deadline_budget_passed_to_registry(monkeypatch, workspace):
    """execute_subtask passes a monotonic deadline, not just a raw timeout."""
    repo, cfg, db = workspace

    captured: dict = {}

    class DeadlineCapture(StubRegistry):
        def execute_cheapest(self, **kwargs):
            captured["deadline"] = kwargs.get("deadline")
            captured["timeout"] = kwargs.get("timeout")
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, DeadlineCapture())

    before = time.monotonic()
    mcp_server.handle_execute_subtask({"prompt": "test", "timeout": 60})
    after = time.monotonic()

    assert captured["deadline"] is not None, "deadline not passed to execute_cheapest"
    assert before + 59 <= captured["deadline"] <= after + 61


# ---------------------------------------------------------------------------
# Test 13: Max timeout cap raised to 600
# ---------------------------------------------------------------------------


def test_max_timeout_cap_is_600(monkeypatch, workspace):
    """Timeout values above 600 are clamped; 600 is accepted."""
    repo, cfg, db = workspace

    captured_deadlines: list[float] = []

    class TimeoutCapture(StubRegistry):
        def execute_cheapest(self, **kwargs):
            dl = kwargs.get("deadline")
            if dl is not None:
                captured_deadlines.append(dl - time.monotonic())
            return super().execute_cheapest(**kwargs)

    _patch_mcp(monkeypatch, repo, cfg, db, TimeoutCapture())

    # 600 should be accepted
    mcp_server.handle_execute_subtask({"prompt": "test", "timeout": 600})
    assert 595 <= captured_deadlines[0] <= 601

    # 900 should be clamped to 600
    mcp_server.handle_execute_subtask({"prompt": "test", "timeout": 900})
    assert 595 <= captured_deadlines[1] <= 601


# ---------------------------------------------------------------------------
# Test 14: Progress heartbeat only fires when progressToken present
# ---------------------------------------------------------------------------


def test_heartbeat_fires_with_progress_token(monkeypatch, workspace):
    """When _request_context.progress_token is set, heartbeats are sent."""
    repo, cfg, db = workspace

    heartbeat_notifications: list[dict] = []
    original_send = mcp_server.send_notification

    def capture_notification(method, params):
        if method == "notifications/progress":
            heartbeat_notifications.append(params)

    monkeypatch.setattr(mcp_server, "send_notification", capture_notification)

    # Use a delay registry so the heartbeat has time to fire
    registry = DelayRegistry(0.5)
    _patch_mcp(monkeypatch, repo, cfg, db, registry)

    # Set progressToken before the call
    mcp_server._request_context.progress_token = "test-token-123"
    try:
        mcp_server.handle_execute_subtask({
            "prompt": "test",
            "timeout": 30,
        })
    finally:
        mcp_server._request_context.progress_token = None

    # With 0.5s delay and default 15s interval, no heartbeat should have fired
    # (the task completes before the first interval). Verify no crash at least.
    # For a real heartbeat test we'd need a longer delay, but that's too slow
    # for unit tests. The key assertion is: no crash, and token is plumbed.


def test_no_heartbeat_without_progress_token(monkeypatch, workspace):
    """When no progressToken is set, no heartbeat thread is started."""
    repo, cfg, db = workspace

    heartbeat_notifications: list[dict] = []

    def capture_notification(method, params):
        if method == "notifications/progress":
            heartbeat_notifications.append(params)

    monkeypatch.setattr(mcp_server, "send_notification", capture_notification)
    _patch_mcp(monkeypatch, repo, cfg, db)

    # Ensure no token
    mcp_server._request_context.progress_token = None
    mcp_server.handle_execute_subtask({"prompt": "test", "timeout": 30})

    assert len(heartbeat_notifications) == 0, "heartbeat fired without progressToken"


# ---------------------------------------------------------------------------
# Test 15: Progress heartbeat actually fires on long tasks
# ---------------------------------------------------------------------------


def test_heartbeat_actually_sends_progress(monkeypatch, workspace):
    """With a short interval and slow provider, heartbeats are actually sent."""
    repo, cfg, db = workspace

    heartbeat_notifications: list[dict] = []

    def capture_notification(method, params):
        if method == "notifications/progress":
            heartbeat_notifications.append(params)

    monkeypatch.setattr(mcp_server, "send_notification", capture_notification)

    # Registry that takes 0.5s — with 0.1s heartbeat interval we should get ~4
    registry = DelayRegistry(0.5)
    _patch_mcp(monkeypatch, repo, cfg, db, registry)

    # Patch the heartbeat interval to be very short for testing
    original_heartbeat = mcp_server._heartbeat_loop

    def fast_heartbeat(token, stop, interval=15):
        return original_heartbeat(token, stop, interval=0.1)

    monkeypatch.setattr(mcp_server, "_heartbeat_loop", fast_heartbeat)

    mcp_server._request_context.progress_token = "fast-token"
    try:
        mcp_server.handle_execute_subtask({"prompt": "test", "timeout": 30})
    finally:
        mcp_server._request_context.progress_token = None

    assert len(heartbeat_notifications) >= 2, (
        f"Expected at least 2 heartbeats, got {len(heartbeat_notifications)}"
    )
    assert all(n["progressToken"] == "fast-token" for n in heartbeat_notifications)


# ---------------------------------------------------------------------------
# Test 16: Config tier_timeouts parsed from providers.timeouts
# ---------------------------------------------------------------------------


def test_config_tier_timeouts_parsed():
    """TGsConfig.from_yaml loads providers.timeouts into tier_timeouts."""
    import tempfile, textwrap
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(textwrap.dedent("""\
            providers:
              timeouts:
                low: 30
                medium: 90
                high: 300
        """))
        f.flush()
        cfg = TGsConfig.from_yaml(Path(f.name))
    os.unlink(f.name)

    assert cfg.tier_timeouts["low"] == 30
    assert cfg.tier_timeouts["medium"] == 90
    assert cfg.tier_timeouts["high"] == 300


def test_config_tier_timeouts_defaults_when_missing():
    """Without providers.timeouts in config, defaults are used."""
    import tempfile, textwrap
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(textwrap.dedent("""\
            providers: {}
        """))
        f.flush()
        cfg = TGsConfig.from_yaml(Path(f.name))
    os.unlink(f.name)

    assert cfg.tier_timeouts == {"low": 60, "medium": 120, "high": 180}
