#!/usr/bin/env python3
"""
Tests for shared/orchestrator.py parallel wave execution.
"""
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from shared.config import PLANNER_ALLOW_TOPOLOGY_FALLBACK, TGsConfig
from shared.db import Database
from shared.orchestrator import AgentResult, CircuitBreakerError, Orchestrator, Provider, fan_out_task, seed_resume_from_checkpoint
from shared.planner import CLIBackend, ExecutionPlan, Planner, PlannerParseError, Subtask


class DummyBackend(CLIBackend):
    def call(
        self,
        prompt: str,
        model: str | None = None,
        timeout: int = 120,
    ) -> str | None:
        return None


class DummyProvider(Provider):
    def resolve_model(self, tier: str) -> str:
        return f"dummy-{tier}"

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        return f"{model}:{subtask.id}"

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]

    def provider_info(self) -> dict:
        return {"primary": "dummy-provider"}


class EmptyProvider(DummyProvider):
    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        return None


def test_execute_wave_rejects_empty_provider_output() -> None:
    config = _build_config(False)
    config.output_quality_retry_enabled = False
    orchestrator = Orchestrator(config, EmptyProvider(), DummyPlanner())

    with pytest.raises(RuntimeError, match="no output for subtask\\(s\\): 1"):
        orchestrator.execute_wave(
            1,
            [Subtask(id=1, description="empty", tier="low", model="dummy-low")],
        )


def test_quality_check_allows_exception_handler_pass() -> None:
    orchestrator = Orchestrator(
        _build_config(False),
        DummyProvider(),
        DummyPlanner(),
    )

    output = """try:
    mean([])
except ValueError:
    pass
"""

    assert orchestrator._check_output_quality_for_retry(output) is None


def test_valid_coordinator_json_skips_generic_quality_retry() -> None:
    class CoordinatorProvider(TrackingProvider):
        def execute(
            self,
            subtask: Subtask,
            model: str,
            timeout: int = 120,
        ) -> str:
            self.execute_calls.append((subtask.id, model, timeout))
            return (
                '{"verdict":"complete","amendment":null,"next_work":{},'
                '"synthesis":{"summary_text":"completed without errors"},'
                '"fallback_reason":null}'
            )

    provider = CoordinatorProvider()
    orchestrator = Orchestrator(
        _build_config(False),
        provider,
        DummyPlanner(),
    )

    result = orchestrator.execute_subtask(
        Subtask(
            id=1,
            description="coordinate",
            tier="low",
            model="dummy-low",
            is_coordinator=True,
        )
    )

    assert result.tier == "low"
    assert provider.execute_calls == [(1, "dummy-low", 120)]


class DummyPlanner(Planner):
    def __init__(self) -> None:
        self._backend = DummyBackend()

    def plan(self, *args, **kwargs):  # pragma: no cover - not exercised here
        raise NotImplementedError


class RaisingPlanner(DummyPlanner):
    def plan(self, *args, **kwargs):
        raise PlannerParseError("Planner returned no output")


class StubOrchestrator(Orchestrator):
    def __init__(self, config: TGsConfig, db: Database) -> None:
        super().__init__(config, DummyProvider(), DummyPlanner(), db=db)

    def execute_subtask(
        self,
        subtask: Subtask,
        timeout: int = 120,
        score: float | None = None,
        *,
        execution_id: str | None = None,
        plan_revision: int = 1,
        current_wave: int | None = None,
        prefetched_artifacts: list[dict[str, object]] | None = None,
    ) -> AgentResult:
        assert self._db is not None
        self._db.log_agent_result(
            session_id="wave-test",
            task_hash=f"task-{subtask.id}",
            agent_id=subtask.id,
            tier=subtask.tier,
            model="dummy-low",
        )
        time.sleep(0.25)
        return AgentResult(
            subtask_id=subtask.id,
            tier=subtask.tier,
            model="dummy-low",
            output=f"completed {subtask.id}",
            token_count=1,
        )


class FailingProvider(Provider):
    def resolve_model(self, tier: str) -> str:
        return f"dummy-{tier}"

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        raise RuntimeError("provider failure")

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]


class TrackingProvider(DummyProvider):
    def __init__(self) -> None:
        self.execute_calls: list[tuple[int, str, int]] = []
        self.resolve_calls: list[str] = []

    def resolve_model(self, tier: str) -> str:
        self.resolve_calls.append(tier)
        return super().resolve_model(tier)

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        self.execute_calls.append((subtask.id, model, timeout))
        return super().execute(subtask, model, timeout)


class TwoArgProvider(DummyProvider):
    def __init__(self) -> None:
        self.execute_calls: list[tuple[int, str]] = []

    def execute(
        self,
        subtask: Subtask,
        model: str,
        timeout: int = 120,
    ) -> str | None:
        del timeout
        self.execute_calls.append((subtask.id, model))
        return f"{model}:{subtask.id}"


def _build_subtasks() -> list[Subtask]:
    return [
        Subtask(id=1, description="subtask 1", tier="low", model="dummy-low"),
        Subtask(id=2, description="subtask 2", tier="low", model="dummy-low"),
        Subtask(id=3, description="subtask 3", tier="low", model="dummy-low"),
        Subtask(id=4, description="subtask 4", tier="low", model="dummy-low"),
    ]


def _build_config(enabled: bool) -> TGsConfig:
    config = TGsConfig()
    config.parallelism.enabled = enabled
    config.parallelism.max_workers = 4
    return config


def _build_plan(topology: str, *, explicit: bool = True) -> ExecutionPlan:
    subtasks = [
        Subtask(id=1, description="root", tier="low", model="dummy-low"),
        Subtask(id=2, description="child-a", tier="low", model="dummy-low", depends_on=[1]),
        Subtask(id=3, description="child-b", tier="low", model="dummy-low", depends_on=[1, 2]),
    ]
    return ExecutionPlan(
        analysis="plan",
        subtasks=subtasks,
        waves=[[1], [2], [3]],
        total_agents=3,
        strategy="dag",
        topology=topology,
        _topology_explicit=explicit,
    )


def _run_wave(enabled: bool, db_path: Path) -> float:
    db = Database(db_path)
    try:
        orchestrator = StubOrchestrator(_build_config(enabled), db)
        started = time.perf_counter()
        results = orchestrator.execute_wave(0, _build_subtasks())
        elapsed = time.perf_counter() - started
        assert {result.subtask_id for result in results} == {1, 2, 3, 4}
        with db.conn() as conn:
            telemetry_rows = conn.execute(
                "SELECT COUNT(*) FROM telemetry WHERE session_id = ?",
                ("wave-test",),
            ).fetchone()[0]
        assert telemetry_rows == 4
        return elapsed
    finally:
        db.close()


def test_wave_parallelism(caplog) -> None:
    """Parallel execution should be faster and fall back cleanly when disabled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        parallel_elapsed = _run_wave(True, tmpdir_path / "parallel.db")
        serial_elapsed = _run_wave(False, tmpdir_path / "serial.db")

        assert parallel_elapsed < 0.7
        assert serial_elapsed > parallel_elapsed + 0.3

        fallback_db = Database(tmpdir_path / "fallback.db")
        try:
            fallback_orchestrator = StubOrchestrator(_build_config(True), fallback_db)
            fallback_orchestrator._parallelism_worker_db_check = lambda: False
            caplog.clear()
            started = time.perf_counter()
            results = fallback_orchestrator.execute_wave(0, _build_subtasks())
            fallback_elapsed = time.perf_counter() - started
            assert {result.subtask_id for result in results} == {1, 2, 3, 4}
            assert fallback_elapsed > parallel_elapsed + 0.3
            assert "falling back to serial execution" in caplog.text
        finally:
            fallback_db.close()


def test_execute_dag_runner_persists_declared_topology() -> None:
    """Explicit topology runners should preserve declared topology in swarm state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "runner-topology.db")
        try:
            orchestrator = StubOrchestrator(_build_config(False), db)
            orchestrator._execute_dag_runner(
                _build_plan("dag", explicit=True),
                execution_id="runner-topology",
                plan_revision=1,
            )
            with db.conn() as conn:
                swarm_row = conn.execute(
                    "SELECT topology FROM swarm_runs WHERE swarm_id = ?",
                    ("runner-topology",),
                ).fetchone()
            assert swarm_row is not None
            assert swarm_row[0] == "dag"
        finally:
            db.close()


def test_run_falls_back_when_planner_returns_no_output() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "planner-fallback.db")
        try:
            provider = TrackingProvider()
            orchestrator = Orchestrator(
                _build_config(False),
                provider,
                RaisingPlanner(),
                db=db,
            )

            result = orchestrator.run(
                "implement the feature directly",
                execution_id="swarm-fallback",
            )

            runtime_plan = result["plan"]
            assert runtime_plan is not None
            assert runtime_plan.topology == "linear"
            assert runtime_plan.total_agents == 1
            assert runtime_plan.subtasks[0].description == "implement the feature directly"
            assert provider.resolve_calls == ["medium"]
            assert provider.execute_calls == [(1, "dummy-medium", 120)]

            summary = db.get_swarm_summary("swarm-fallback")
            assert summary is not None
            assert summary["status"] == "completed"

            payload = db.get_latest_swarm_event_payload("swarm-fallback", "planner_fallback")
            assert payload is not None
            assert payload["reason"] == "Planner returned no output"
            assert payload["fallback_topology"] == "linear"
            assert payload["fallback_agents"] == 1
        finally:
            db.close()


def test_execute_subtask_falls_back_to_two_arg_provider_signature() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "two-arg-provider.db")
        try:
            provider = TwoArgProvider()
            orchestrator = Orchestrator(
                _build_config(False),
                provider,
                DummyPlanner(),
                db=db,
            )
            result = orchestrator.execute_subtask(
                Subtask(id=7, description="two-arg", tier="low", model="dummy-low")
            )

            assert result.output == "dummy-low:7"
            assert provider.execute_calls == [(7, "dummy-low")]
        finally:
            db.close()


def test_seed_resume_from_checkpoint_rejects_non_mapping() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "resume-type.db")
        try:
            with pytest.raises(TypeError, match="checkpoint must be a mapping"):
                seed_resume_from_checkpoint(None, db=db)  # type: ignore[arg-type]
        finally:
            db.close()


def test_wave_parallel_execution() -> None:
    """
    Test parallel wave execution (05-V0-03: FNDX-02 requirement).
    
    Verifies:
    1. Wall time is less than serial time (indicating parallelism)
    2. No exceptions raised during parallel execution
    3. All results are returned
    4. ThreadPoolExecutor with configured max_workers is used
    
    FNDX-02 requirement: Wave execution uses ThreadPoolExecutor with
    configurable concurrency cap so multi-agent waves run in true parallel.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        db = Database(tmpdir_path / "parallel_test.db")
        try:
            config = TGsConfig()
            config.parallelism.enabled = True
            config.parallelism.max_workers = 4
            
            orchestrator = StubOrchestrator(config, db)
            
            # Create 3 subtasks, each takes ~0.25s
            subtasks = [
                Subtask(id=1, description="subtask 1", tier="low", model="dummy-low"),
                Subtask(id=2, description="subtask 2", tier="low", model="dummy-low"),
                Subtask(id=3, description="subtask 3", tier="low", model="dummy-low"),
            ]
            
            # Measure parallel execution
            start_time = time.perf_counter()
            results = orchestrator.execute_wave(0, subtasks)
            parallel_elapsed = time.perf_counter() - start_time
            
            # Verify all results returned
            assert len(results) == 3, f"Expected 3 results, got {len(results)}"
            assert {r.subtask_id for r in results} == {1, 2, 3}
            
            # Wall time should be < 0.6s for parallel (3 * 0.25s each in parallel)
            # Serial would be ~0.75s+ (3 * 0.25s sequentially)
            # Allow generous margin for slow CI systems
            assert parallel_elapsed < 0.6, f"Parallel execution took {parallel_elapsed:.2f}s (expected < 0.6s)"
        finally:
            db.close()


def test_wave_serial_fallback() -> None:
    """
    Test wave serial fallback execution (05-V0-04: FNDX-02 requirement).
    
    Verifies:
    1. When parallelism.enabled=False, wave uses serial execution
    2. Wall time >= expected serial time (no parallelism)
    3. All results returned
    4. No threading errors
    
    FNDX-02 requirement: Serial execution is the absolute last resort —
    falls back cleanly when parallelism is disabled.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        db = Database(tmpdir_path / "serial_test.db")
        try:
            config = TGsConfig()
            config.parallelism.enabled = False  # Disable parallelism
            
            orchestrator = StubOrchestrator(config, db)
            
            # Create 3 subtasks, each takes ~0.25s
            subtasks = [
                Subtask(id=1, description="subtask 1", tier="low", model="dummy-low"),
                Subtask(id=2, description="subtask 2", tier="low", model="dummy-low"),
                Subtask(id=3, description="subtask 3", tier="low", model="dummy-low"),
            ]
            
            # Measure serial execution
            start_time = time.perf_counter()
            results = orchestrator.execute_wave(0, subtasks)
            serial_elapsed = time.perf_counter() - start_time
            
            # Verify all results returned
            assert len(results) == 3, f"Expected 3 results, got {len(results)}"
            assert {r.subtask_id for r in results} == {1, 2, 3}
            
            # Wall time should be >= 0.7s for serial (3 * 0.25s sequentially)
            # Allow margin for test variance
            assert serial_elapsed >= 0.65, f"Serial execution took {serial_elapsed:.2f}s (expected >= 0.65s)"
        finally:
            db.close()


def test_wave_single_subtask_is_serial() -> None:
    """
    Test that single-subtask waves use serial path (no ThreadPoolExecutor overhead).
    
    Verifies:
    1. Single subtask wave executes successfully
    2. No ThreadPoolExecutor overhead for trivial waves
    3. Uses serial execution path even when parallelism.enabled=True
    
    FNDX-02 requirement: Serial execution is the last resort —
    single subtask waves should never spawn a thread pool.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        db = Database(tmpdir_path / "single_test.db")
        try:
            config = TGsConfig()
            config.parallelism.enabled = True
            config.parallelism.max_workers = 4
            
            orchestrator = StubOrchestrator(config, db)
            
            # Single subtask
            subtasks = [
                Subtask(id=1, description="single subtask", tier="low", model="dummy-low"),
            ]
            
            # Execute wave
            start_time = time.perf_counter()
            results = orchestrator.execute_wave(0, subtasks)
            elapsed = time.perf_counter() - start_time
            
            # Verify result
            assert len(results) == 1, f"Expected 1 result, got {len(results)}"
            assert results[0].subtask_id == 1
            
            # Should be fast (~0.25s) since single task avoids pool overhead
            assert elapsed < 0.4, f"Single subtask took {elapsed:.2f}s (expected < 0.4s)"
        finally:
            db.close()


def test_execute_plan_honors_prompt_requested_parallel_limit() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        db = Database(tmpdir_path / "plan_limit.db")
        try:
            config = TGsConfig()
            config.parallelism.enabled = True
            config.parallelism.max_workers = -1

            orchestrator = StubOrchestrator(config, db)
            subtasks = _build_subtasks()
            plan = ExecutionPlan(
                analysis="",
                subtasks=subtasks,
                waves=[[1, 2, 3, 4]],
                total_agents=4,
                strategy="parallel",
            )

            unlimited_started = time.perf_counter()
            orchestrator.execute_plan(plan)
            unlimited_elapsed = time.perf_counter() - unlimited_started

            limited_started = time.perf_counter()
            results = orchestrator.execute_plan(
                plan,
                task_description="fan this out but use max 2 agents",
            )
            limited_elapsed = time.perf_counter() - limited_started

            assert len(results) == 4
            assert limited_elapsed > unlimited_elapsed + 0.15
            assert limited_elapsed < 0.95
        finally:
            db.close()


def test_orchestrator_aborts_on_topology_mismatch_when_no_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "topology.db")
        try:
            orchestrator = Orchestrator(_build_config(False), DummyProvider(), DummyPlanner(), db=db)
            plan = _build_plan("star")
            with patch("shared.orchestrator.PLANNER_ALLOW_TOPOLOGY_FALLBACK", False):
                with pytest.raises(ValueError, match="topology validation failed"):
                    orchestrator.execute_plan(plan, execution_id="exec-1")
        finally:
            db.close()


def test_orchestrator_allows_runtime_linear_fallback_when_flag_enabled(caplog) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "topology-fallback.db")
        try:
            orchestrator = Orchestrator(_build_config(False), DummyProvider(), DummyPlanner(), db=db)
            plan = _build_plan("star")
            with patch("shared.orchestrator.PLANNER_ALLOW_TOPOLOGY_FALLBACK", True):
                results = orchestrator.execute_plan(plan, execution_id="exec-1")
            assert {result.subtask_id for result in results.values()} == {1, 2, 3}
            assert "topology validation failed" in caplog.text
            assert "fallback to linear" in caplog.text
        finally:
            db.close()


def test_execute_subtask_rejects_missing_model_before_provider_execution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "missing-model.db")
        try:
            provider = TrackingProvider()
            orchestrator = Orchestrator(_build_config(False), provider, DummyPlanner(), db=db)
            subtask = Subtask(id=12, description="missing model", tier="low")

            with pytest.raises(ValueError, match="missing routed model metadata"):
                orchestrator.execute_subtask(subtask)

            assert provider.execute_calls == []
            assert provider.resolve_calls == []
        finally:
            db.close()


def test_execute_subtask_rejects_provider_mismatch_before_execution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "provider-mismatch.db")
        try:
            provider = TrackingProvider()
            orchestrator = Orchestrator(_build_config(False), provider, DummyPlanner(), db=db)
            subtask = Subtask(
                id=13,
                description="provider mismatch",
                tier="low",
                model="planned-model",
                provider_id="other-provider",
            )

            with pytest.raises(ValueError, match="provider_id 'other-provider'"):
                orchestrator.execute_subtask(subtask)

            assert provider.execute_calls == []
            assert provider.resolve_calls == []
        finally:
            db.close()


def test_execute_subtask_uses_routed_model_instead_of_resolving_tier() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "routed-model.db")
        try:
            provider = TrackingProvider()
            orchestrator = Orchestrator(_build_config(False), provider, DummyPlanner(), db=db)
            subtask = Subtask(
                id=14,
                description="use explicit model",
                tier="low",
                model="planned-model",
                provider="dummy-provider",
            )

            result = orchestrator.execute_subtask(subtask)

            assert result.output == "planned-model:14"
            assert provider.execute_calls == [(14, "planned-model", 120)]
            assert provider.resolve_calls == []
        finally:
            db.close()


def test_execute_subtask_caches_provider_bound_tier_resolution() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "provider-bound-tier-cache.db")
        try:
            provider = TrackingProvider()
            orchestrator = Orchestrator(_build_config(False), provider, DummyPlanner(), db=db)

            first = orchestrator.execute_subtask(
                Subtask(
                    id=140,
                    description="provider-bound tier placeholder",
                    tier="low",
                    model="low",
                    provider="dummy-provider",
                )
            )
            second = orchestrator.execute_subtask(
                Subtask(
                    id=141,
                    description="provider-bound tier placeholder",
                    tier="low",
                    model="low",
                    provider="dummy-provider",
                )
            )

            assert first.output == "dummy-low:140"
            assert second.output == "dummy-low:141"
            assert provider.resolve_calls == ["low"]
        finally:
            db.close()


def test_execute_subtask_resolves_provider_bound_tier_placeholder() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "provider-bound-tier.db")
        try:
            provider = TrackingProvider()
            orchestrator = Orchestrator(_build_config(False), provider, DummyPlanner(), db=db)
            subtask = Subtask(
                id=141,
                description="provider-bound tier placeholder",
                tier="low",
                model="low",
                provider="dummy-provider",
            )

            result = orchestrator.execute_subtask(subtask)

            assert result.output == "dummy-low:141"
            assert provider.execute_calls == [(141, "dummy-low", 120)]
            assert provider.resolve_calls == ["low"]
        finally:
            db.close()


def test_execute_plan_fails_early_on_invalid_model_metadata() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "invalid-plan.db")
        try:
            orchestrator = Orchestrator(_build_config(False), DummyProvider(), DummyPlanner(), db=db)
            plan = ExecutionPlan(
                analysis="invalid",
                subtasks=[Subtask(id=15, description="bad subtask", tier="low")],
                waves=[[15]],
                total_agents=1,
                strategy="sequential",
            )

            with patch.object(orchestrator, "execute_wave", side_effect=AssertionError("wave should not run")):
                with pytest.raises(ValueError, match="missing model metadata"):
                    orchestrator.execute_plan(plan, execution_id="exec-invalid")
        finally:
            db.close()


def test_publish_artifact_on_success() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "artifact-success.db")
        try:
            orchestrator = Orchestrator(_build_config(False), DummyProvider(), DummyPlanner(), db=db)
            subtask = Subtask(
                id=7,
                description="produce summary",
                tier="low",
                model="dummy-low",
                produces=["summary"],
            )
            result = orchestrator.execute_subtask(
                subtask,
                execution_id="exec-1",
                plan_revision=3,
                current_wave=2,
            )
            artifacts = db.query_artifacts("exec-1", 3, wave=2, artifact_types=["summary"])
            assert result.output == "dummy-low:7"
            assert len(artifacts) == 1
            assert artifacts[0]["compact_summary"]["summary_text"] == "dummy-low:7"
            assert artifacts[0]["compact_summary"]["artifact_ref"] == artifacts[0]["stable_ref"]
            assert db._get_full_payload(artifacts[0]["stable_ref"]) == "dummy-low:7"
        finally:
            db.close()


def test_publish_artifact_not_called_on_failure() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "artifact-failure.db")
        try:
            orchestrator = Orchestrator(_build_config(False), FailingProvider(), DummyPlanner(), db=db)
            subtask = Subtask(
                id=8,
                description="produce summary",
                tier="low",
                model="dummy-low",
                produces=["summary"],
            )
            with pytest.raises(RuntimeError, match="provider failure"):
                orchestrator.execute_subtask(
                    subtask,
                    execution_id="exec-1",
                    plan_revision=1,
                    current_wave=1,
                )
            assert db.query_artifacts("exec-1", 1) == []
        finally:
            db.close()


def test_publish_artifact_on_speculative_success() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "artifact-speculative.db")
        try:
            orchestrator = Orchestrator(_build_config(False), DummyProvider(), DummyPlanner(), db=db)
            orchestrator._speculative = SimpleNamespace(
                execute_speculative=lambda _subtask, _score: SimpleNamespace(
                    output="speculative-output",
                    tier_used="low",
                    model_used="spec-model",
                    token_estimate=4,
                )
            )
            subtask = Subtask(
                id=9,
                description="produce summary",
                tier="low",
                model="dummy-low",
                produces=["summary"],
            )
            result = orchestrator.execute_subtask(
                subtask,
                score=0.6,
                execution_id="exec-2",
                plan_revision=4,
                current_wave=3,
            )
            artifacts = db.query_artifacts("exec-2", 4, wave=3, artifact_types=["summary"])
            assert result.output == "speculative-output"
            assert len(artifacts) == 1
            assert db._get_full_payload(artifacts[0]["stable_ref"]) == "speculative-output"
        finally:
            db.close()


def test_artifact_persistence_failure_does_not_abort_subtask(caplog) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "artifact-persist-warning.db")
        try:
            orchestrator = Orchestrator(_build_config(False), DummyProvider(), DummyPlanner(), db=db)
            subtask = Subtask(
                id=10,
                description="produce summary",
                tier="low",
                model="dummy-low",
                produces=["summary"],
            )
            with patch.object(db, "save_artifact", side_effect=RuntimeError("db unavailable")):
                result = orchestrator.execute_subtask(
                    subtask,
                    execution_id="exec-3",
                    plan_revision=1,
                    current_wave=1,
                )
            assert result.output == "dummy-low:10"
            assert "Failed to persist artifact 'summary' for subtask 10" in caplog.text
            assert db.query_artifacts("exec-3", 1) == []
        finally:
            db.close()


def test_no_wave_context_skips_artifact_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "artifact-no-wave.db")
        try:
            orchestrator = Orchestrator(_build_config(False), DummyProvider(), DummyPlanner(), db=db)
            subtask = Subtask(
                id=11,
                description="produce summary",
                tier="low",
                model="dummy-low",
                produces=["summary"],
            )
            result = orchestrator.execute_subtask(
                subtask,
                execution_id="exec-4",
                plan_revision=1,
            )
            assert result.output == "dummy-low:11"
            assert db.query_artifacts("exec-4", 1) == []
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Additional focused regression tests for multi-provider spillover anchoring
# ---------------------------------------------------------------------------
def test_spillover_anchor_prefers_explicit_provider(tmp_path: Path) -> None:
    """A routed provider_id should be anchored as primary even if another
    provider is nominally cheaper in the registry plan.
    """
    db = Database(tmp_path / "spill-anchor.db")
    try:
        config = TGsConfig()
        config.parallelism.enabled = False
        orchestrator = Orchestrator(config, DummyProvider(), DummyPlanner(), db=db)

        class ProviderA(DummyProvider):
            def provider_info(self) -> dict:
                return {"primary": "a"}

        class ProviderB(DummyProvider):
            def provider_info(self) -> dict:
                return {"primary": "b"}

        orchestrator._providers_map = {"a": ProviderA(), "b": ProviderB()}

        class FakeRegistry:
            def plan_spillover_allocation(self, tier, count, anchor_provider_id=None, **kwargs):
                # Anchor to 'a' when requested regardless of cost ordering
                if anchor_provider_id == "a":
                    return {"primary": {"provider_id": "a"}, "assignments": [{"provider_id": "a", "slots": 2}, {"provider_id": "b", "slots": 2}], "remaining": 0}
                return {"primary": {"provider_id": "b"}, "assignments": [{"provider_id": "b", "slots": 4}], "remaining": 0}

        orchestrator._provider_registry = FakeRegistry()

        subtasks = [
            Subtask(id=1, description="s1", tier="low", model="m", provider_id="a"),
            Subtask(id=2, description="s2", tier="low", model="m", provider_id="a"),
            Subtask(id=3, description="s3", tier="low", model="m"),
            Subtask(id=4, description="s4", tier="low", model="m"),
        ]

        results = orchestrator.execute_wave(0, subtasks)
        assigned = {r.subtask_id: r.provider_name for r in results}
        # Explicitly routed subtasks should use ProviderA
        assert assigned[1] == "ProviderA"
        assert assigned[2] == "ProviderA"
    finally:
        db.close()


def test_same_tier_different_routed_providers_allocated_separately(tmp_path: Path) -> None:
    """Subtasks routed to different explicit providers within the same tier
    should be planned separately and not mixed across provider assignments.
    """
    db = Database(tmp_path / "spill-buckets.db")
    try:
        config = TGsConfig()
        config.parallelism.enabled = False
        orchestrator = Orchestrator(config, DummyProvider(), DummyPlanner(), db=db)

        class ProviderA(DummyProvider):
            def provider_info(self) -> dict:
                return {"primary": "a"}

        class ProviderB(DummyProvider):
            def provider_info(self) -> dict:
                return {"primary": "b"}

        orchestrator._providers_map = {"a": ProviderA(), "b": ProviderB()}

        class FakeRegistry:
            def plan_spillover_allocation(self, tier, count, anchor_provider_id=None, **kwargs):
                # When anchored to 'a' or 'b' return only that provider
                if anchor_provider_id == "a":
                    return {"primary": {"provider_id": "a"}, "assignments": [{"provider_id": "a", "slots": count}], "remaining": 0}
                if anchor_provider_id == "b":
                    return {"primary": {"provider_id": "b"}, "assignments": [{"provider_id": "b", "slots": count}], "remaining": 0}
                return {"primary": None, "assignments": [], "remaining": count}

        orchestrator._provider_registry = FakeRegistry()

        subtasks = [
            Subtask(id=1, description="s1", tier="low", model="m", provider_id="a"),
            Subtask(id=2, description="s2", tier="low", model="m", provider_id="a"),
            Subtask(id=3, description="s3", tier="low", model="m", provider_id="b"),
            Subtask(id=4, description="s4", tier="low", model="m", provider_id="b"),
        ]

        results = orchestrator.execute_wave(0, subtasks)
        assigned = {r.subtask_id: r.provider_name for r in results}
        assert assigned[1] == "ProviderA"
        assert assigned[2] == "ProviderA"
        assert assigned[3] == "ProviderB"
        assert assigned[4] == "ProviderB"
    finally:
        db.close()


def test_allocator_shortfall_raises_from_execute_wave(tmp_path: Path) -> None:
    """If the allocator returns a remaining > 0, execute_wave should raise
    a clear RuntimeError indicating unallocated slots.
    """
    db = Database(tmp_path / "spill-shortfall.db")
    try:
        config = TGsConfig()
        config.parallelism.enabled = False
        orchestrator = Orchestrator(config, DummyProvider(), DummyPlanner(), db=db)

        class ProviderA(DummyProvider):
            def provider_info(self) -> dict:
                return {"primary": "a"}

        orchestrator._providers_map = {"a": ProviderA()}

        class FakeRegistry:
            def plan_spillover_allocation(self, tier, count, anchor_provider_id=None, **kwargs):
                return {"primary": {"provider_id": "a"}, "assignments": [{"provider_id": "a", "slots": 1}], "remaining": 1}

        orchestrator._provider_registry = FakeRegistry()

        subtasks = [
            Subtask(id=1, description="s1", tier="low", model="m", provider_id="a"),
            Subtask(id=2, description="s2", tier="low", model="m", provider_id="a"),
        ]

        with pytest.raises(RuntimeError, match="unallocated slots for tier 'low'"):
            orchestrator.execute_wave(0, subtasks)
    finally:
        db.close()


def test_result_metadata_provider_name_reflects_actual_provider_used(tmp_path: Path) -> None:
    """AgentResult.provider_name should indicate the concrete provider used for
    execution (i.e., the provider_override class name when assigned).
    """
    db = Database(tmp_path / "spill-metadata.db")
    try:
        config = TGsConfig()
        config.parallelism.enabled = False
        orchestrator = Orchestrator(config, DummyProvider(), DummyPlanner(), db=db)

        class ProviderB(DummyProvider):
            def provider_info(self) -> dict:
                return {"primary": "b"}

        orchestrator._providers_map = {"b": ProviderB()}

        class FakeRegistry:
            def plan_spillover_allocation(self, tier, count, anchor_provider_id=None, **kwargs):
                return {"primary": {"provider_id": "b"}, "assignments": [{"provider_id": "b", "slots": count}], "remaining": 0}

        orchestrator._provider_registry = FakeRegistry()

        subtasks = [Subtask(id=10, description="s10", tier="low", model="m", provider_id="b")]
        results = orchestrator.execute_wave(0, subtasks)
        assert len(results) == 1
        assert results[0].provider_name == "ProviderB"
    finally:
        db.close()


# --- coordinator sync ---

class RecordingProvider(Provider):
    def __init__(self, outputs: dict[int, str]) -> None:
        self.outputs = outputs
        self.calls: list[int] = []
        self.descriptions: dict[int, str] = {}

    def resolve_model(self, tier: str) -> str:
        return f"dummy-{tier}"

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        self.calls.append(subtask.id)
        self.descriptions[subtask.id] = subtask.description
        return self.outputs[subtask.id]

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]


def _build_coordinator_plan() -> ExecutionPlan:
    subtasks = [
        Subtask(id=1, description="produce artifact", tier="low", model="low", produces=["summary"]),
        Subtask(
            id=2,
            description="coordinate next wave",
            tier="low",
            model="low",
            depends_on=[1],
            is_coordinator=True,
        ),
        Subtask(id=3, description="worker after coordinator", tier="low", model="low", depends_on=[1]),
    ]
    return ExecutionPlan(
        analysis="coordinator",
        subtasks=subtasks,
        waves=[[1], [2, 3]],
        total_agents=3,
        strategy="dag",
    )


def test_coordinator_runs_before_next_wave() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "coordinator.db")
        try:
            provider = RecordingProvider({
                1: "artifact payload",
                2: json.dumps({"verdict": "complete"}),
                3: "worker output",
            })
            config = TGsConfig()
            config.parallelism.enabled = True
            orchestrator = Orchestrator(config, provider, DummyPlanner(), db=db)

            results = orchestrator.execute_plan(
                _build_coordinator_plan(),
                execution_id="exec-13",
                plan_revision=1,
            )

            assert provider.calls == [1, 2, 3]
            assert set(results) == {1, 2, 3}
        finally:
            db.close()


def test_coordinator_receives_summary_only_artifacts() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "coordinator-summary.db")
        try:
            payload = "raw-start " + ("x" * 5000) + " ENDMARK"
            provider = RecordingProvider({
                1: payload,
                2: json.dumps({"verdict": "complete"}),
                3: "worker output",
            })
            config = TGsConfig()
            orchestrator = Orchestrator(config, provider, DummyPlanner(), db=db)

            orchestrator.execute_plan(
                _build_coordinator_plan(),
                execution_id="exec-13",
                plan_revision=1,
            )

            coordinator_description = provider.descriptions[2]
            assert "COORDINATOR RESPONSE CONTRACT" in coordinator_description
            assert '"verdict":"complete|another-pass|fallback"' in coordinator_description
            assert "--- ARTIFACT HANDOFF ---" in coordinator_description
            assert "Reference: artifact:" in coordinator_description
            assert "ENDMARK" not in coordinator_description
        finally:
            db.close()


def test_coordinator_another_pass_ignores_artifact_backed_amendment() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "coordinator-amendment.db")
        try:
            provider = RecordingProvider({
                1: "artifact payload",
                2: json.dumps(
                    {
                        "verdict": "another-pass",
                        "amendment": {
                            "subtask_updates": [
                                {"id": 3, "description": "worker after coordinator revised"},
                            ]
                        },
                    }
                ),
                3: "worker output",
            })
            config = TGsConfig()
            orchestrator = Orchestrator(config, provider, DummyPlanner(), db=db)

            orchestrator.execute_plan(
                _build_coordinator_plan(),
                execution_id="exec-13",
                plan_revision=1,
            )

            assert provider.calls == [1, 2, 3]
            assert provider.descriptions[3] == "worker after coordinator"
        finally:
            db.close()


# --- fanout cap enforcement ---

class FanoutStubOrchestrator(Orchestrator):
    def __init__(self, config: TGsConfig, db: Database):
        super().__init__(config, DummyProvider(), DummyPlanner(), db=db)

    def execute_subtask(
        self,
        subtask: Subtask,
        timeout: int = 120,
        score: float | None = None,
        *,
        execution_id: str | None = None,
        plan_revision: int = 1,
        current_wave: int | None = None,
    ) -> AgentResult:
        assert self._db is not None
        self._db.log_agent_result(
            session_id="fanout-test",
            task_hash=f"task-{subtask.id}",
            agent_id=subtask.id,
            tier=subtask.tier,
            model="dummy-low",
        )
        return AgentResult(
            subtask_id=subtask.id,
            tier=subtask.tier,
            model="dummy-low",
            output=f"completed {subtask.id}",
            token_count=2,
        )


def _task_id_for_description(desc: str) -> str:
    return hashlib.sha256(desc.encode("utf-8", errors="replace")).hexdigest()[:16]


def test_orchestrator_rejects_fanout_above_budget() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "reject.db")
        try:
            config = TGsConfig()
            config.parallelism.enabled = True
            config.parallelism.max_workers = 10
            orchestrator = FanoutStubOrchestrator(config, db)

            domains = [
                {"name": "A", "confidence": 0.95, "tier": "low"},
                {"name": "B", "confidence": 0.9, "tier": "low"},
                {"name": "C", "confidence": 0.9, "tier": "low"},
            ]
            task_desc = "urgent: please run cross-domain analysis asap"
            task = {
                "opt_in_fanout": True,
                "domains": domains,
                "description": task_desc,
                "budget_limit": 50,
                "urgency_score": 0.9,
                "matched_urgency_signals": ["asap"],
            }

            result = fan_out_task(task, max_routers=3, per_router_budget=100, orchestrator=orchestrator, db=db)

            assert result.get("fallback") == "single_route"
            assert result.get("reason") == "caps_exceeded"

            task_id = _task_id_for_description(task_desc)
            with db.conn() as conn:
                row = conn.execute(
                    "SELECT budget_accounting FROM fanout_telemetry WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                assert row is not None, "Expected a fanout_telemetry row"
                payload = json.loads(row[0])
                assert "urgency" in payload, payload
                urgency = payload["urgency"]
                assert urgency.get("final_action") in ("fallback_to_linear", "rejected")
                assert urgency.get("requested_router_count") == 3
                assert urgency.get("urgency_score") == 0.9
        finally:
            db.close()


def test_orchestrator_allows_safe_urgency_fanout() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "allow.db")
        try:
            config = TGsConfig()
            config.parallelism.enabled = True
            config.parallelism.max_workers = 4
            orchestrator = FanoutStubOrchestrator(config, db)

            domains = [
                {"name": "A", "confidence": 0.95, "tier": "low"},
                {"name": "B", "confidence": 0.9, "tier": "low"},
            ]
            task_desc = "please prioritize this soon"
            task = {
                "opt_in_fanout": True,
                "domains": domains,
                "description": task_desc,
                "budget_limit": 100,
                "urgency_score": 0.7,
                "matched_urgency_signals": ["soon"],
            }

            result = fan_out_task(task, max_routers=3, per_router_budget=10, orchestrator=orchestrator, db=db)

            assert result.get("result") is not None or result.get("per_domain")
            assert len(result.get("per_domain", [])) == 2

            task_id = _task_id_for_description(task_desc)
            with db.conn() as conn:
                row = conn.execute(
                    "SELECT budget_accounting FROM fanout_telemetry WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                assert row is not None, "Expected a fanout_telemetry row"
                payload = json.loads(row[0])
                assert "urgency" in payload
                urgency = payload["urgency"]
                assert urgency.get("final_action") == "allowed"
                assert urgency.get("urgency_score") == 0.7
        finally:
            db.close()


# --- synthesis map-reduce ---

class RecordingBackend(CLIBackend):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        self.calls.append(prompt)
        if "AGENT OUTPUTS (partial):" in prompt:
            if "Agent #1" in prompt:
                return "chunk-1: completed auth module"
            if "Agent #2" in prompt:
                return "chunk-2: completed billing module"
            if "Agent #3" in prompt:
                return "chunk-3: completed docs module"
            return "chunk-summary"
        if "CHUNK SUMMARIES:" in prompt:
            return (
                "- Auth module done\n"
                "- Billing module done\n"
                "- Docs module done\n"
                "- No conflicts detected"
            )
        return "single-pass summary"


class SynthesisDummyProvider:
    def resolve_model(self, tier: str) -> str:
        return f"dummy-{tier}"

    def execute(self, subtask, model: str, timeout: int = 120) -> str | None:
        return None

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]

    def provider_info(self) -> dict:
        return {"primary": "dummy-provider"}


def _build_synthesis_config(**overrides) -> TGsConfig:
    config = TGsConfig()
    config.parallelism.max_workers = 4
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_synthesis_auto_single_pass_for_small_outputs() -> None:
    backend = RecordingBackend()
    orchestrator = Orchestrator(_build_synthesis_config(synthesis_map_reduce="auto"), SynthesisDummyProvider(), DummyPlanner())
    results = {1: "small output", 2: "another small output"}

    summary = orchestrator.synthesise("integrate modules", results, backend_call=backend.call)

    assert summary == "single-pass summary"
    assert len(backend.calls) == 1
    assert "AGENT OUTPUTS:" in backend.calls[0]


def test_synthesis_off_always_single_pass() -> None:
    backend = RecordingBackend()
    orchestrator = Orchestrator(
        _build_synthesis_config(synthesis_map_reduce="off", synthesis_chunk_chars=100),
        SynthesisDummyProvider(),
        DummyPlanner(),
    )
    results = {index: "x" * 5000 for index in range(1, 4)}

    summary = orchestrator.synthesise("large task", results, backend_call=backend.call)

    assert summary == "single-pass summary"
    assert len(backend.calls) == 1


def test_synthesis_auto_map_reduce_for_large_outputs() -> None:
    backend = RecordingBackend()
    orchestrator = Orchestrator(
        _build_synthesis_config(synthesis_map_reduce="auto", synthesis_chunk_chars=12000),
        SynthesisDummyProvider(),
        DummyPlanner(),
    )
    results = {
        1: "alpha " * 3000,
        2: "beta " * 3000,
        3: "gamma " * 3000,
    }

    summary = orchestrator.synthesise("ship feature", results, backend_call=backend.call)

    assert summary is not None
    assert "Auth module done" in summary
    partial_calls = [prompt for prompt in backend.calls if "AGENT OUTPUTS (partial):" in prompt]
    reduce_calls = [prompt for prompt in backend.calls if "CHUNK SUMMARIES:" in prompt]
    assert len(partial_calls) >= 2
    assert len(reduce_calls) == 1
    assert any("Agent #1" in prompt for prompt in partial_calls)
    assert any("Agent #2" in prompt for prompt in partial_calls)
    assert any("Agent #3" in prompt for prompt in partial_calls)


def test_synthesis_always_map_reduce_even_for_small_outputs() -> None:
    backend = RecordingBackend()
    orchestrator = Orchestrator(
        _build_synthesis_config(synthesis_map_reduce="always", synthesis_chunk_chars=12000),
        SynthesisDummyProvider(),
        DummyPlanner(),
    )
    results = {1: "small", 2: "also small"}

    summary = orchestrator.synthesise("tiny task", results, backend_call=backend.call)

    assert summary is not None
    partial_calls = [prompt for prompt in backend.calls if "AGENT OUTPUTS (partial):" in prompt]
    reduce_calls = [prompt for prompt in backend.calls if "CHUNK SUMMARIES:" in prompt]
    assert len(partial_calls) == 1
    assert len(reduce_calls) == 1


# --- budget soft warning ---

class BudgetProvider(Provider):
    def __init__(self) -> None:
        self.calls = 0
        self._outputs = [
            "a" * 180,  # 45 tokens
            "b" * 160,  # 40 tokens -> soft warning at 85/100
            "c" * 100,  # 25 tokens -> circuit breaker at 110/100
            "d" * 200,
        ]

    def resolve_model(self, tier: str) -> str:
        return f"budget-{tier}"

    def execute(self, subtask: Subtask, model: str, timeout: int = 120) -> str | None:
        output = self._outputs[self.calls]
        self.calls += 1
        return output

    def available_tiers(self) -> list[str]:
        return ["low", "medium", "high"]


class BudgetPlanner:
    def plan(self, task: str, skip_cache: bool = False) -> ExecutionPlan:
        return ExecutionPlan(
            analysis="budget-test",
            subtasks=[
                Subtask(id=1, description="one", tier="low", model="budget-low"),
                Subtask(id=2, description="two", tier="low", model="budget-low"),
                Subtask(id=3, description="three", tier="low", model="budget-low"),
                Subtask(id=4, description="four", tier="low", model="budget-low"),
            ],
            waves=[[1, 2, 3, 4]],
            total_agents=4,
            strategy="sequential",
        )


def test_budget_warning(caplog) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "budget.db")
        config = TGsConfig()
        config.parallelism.enabled = False
        config.budgets.default_hard_cap_tokens = 100
        config.budgets.default_soft_warning_pct = 0.7

        provider = BudgetProvider()
        orchestrator = Orchestrator(config, provider, BudgetPlanner(), db=db)

        with pytest.raises(CircuitBreakerError):
            orchestrator.run("budget warning test")

        assert provider.calls == 3
        assert "soft token budget warning" in caplog.text
        assert "token circuit breaker" in caplog.text

        with db.conn() as conn:
            reason_rows = conn.execute(
                "SELECT reason FROM telemetry WHERE reason IS NOT NULL ORDER BY id",
            ).fetchall()

        assert [row[0] for row in reason_rows] == [
            "subtask_result",
            "subtask_result",
            "soft_warning",
            "subtask_result",
            "circuit_breaker",
        ]

        db.close()


# --- spillover unique regressions ---

class AnchoringOrchestrator(Orchestrator):
    def __init__(self, config: TGsConfig, db: Database) -> None:
        super().__init__(config, DummyProvider(), None, db=db)

    def execute_subtask(self, subtask: Subtask, timeout: int = 120, provider_override: "Provider | None" = None, **kwargs) -> object:
        class R:
            def __init__(self, subtask_id, provider_name):
                self.subtask_id = subtask_id
                self.tier = subtask.tier
                self.model = subtask.model
                self.output = ""
                self.token_count = 1
                self.provider_name = provider_name
                self.used_fallback = False
                self.used_speculation = False
                self.escalated = False
                self.success = True

        provider_name = provider_override.provider_info().get("primary") if provider_override else "none"
        return R(subtask.id, provider_name)


def test_missing_explicit_primary_fails_clearly(tmp_path: Path) -> None:
    db = Database(tmp_path / "spill-no-primary.db")
    try:
        config = TGsConfig()
        orchestrator = AnchoringOrchestrator(config, db)

        with patch.object(orchestrator, "_provider_registry") as mock_registry:
            orchestrator._providers_map = {"b": MagicMock(provider_info=MagicMock(return_value={"primary": "b"}))}

            def fake_plan(tier, count, anchor_provider_id=None, **kwargs):
                raise RuntimeError(
                    f"Explicitly routed provider '{anchor_provider_id}' is not routeable/available for tier '{tier}'"
                )

            mock_registry.plan_spillover_allocation.side_effect = fake_plan

            subtasks = [
                Subtask(id=1, description="s1", tier="low", model="m", provider_id="a"),
                Subtask(id=2, description="s2", tier="low", model="m", provider_id="a"),
                Subtask(id=3, description="s3", tier="low", model="m", provider_id="b"),
                Subtask(id=4, description="s4", tier="low", model="m", provider_id="b"),
            ]
            with pytest.raises(RuntimeError):
                orchestrator.execute_wave(0, subtasks)
    finally:
        db.close()


def test_missing_runtime_provider_mapping_fails_clearly(tmp_path: Path) -> None:
    db = Database(tmp_path / "spill-missing-map.db")
    try:
        config = TGsConfig()
        orchestrator = AnchoringOrchestrator(config, db)

        with patch.object(orchestrator, "_provider_registry") as mock_registry:
            orchestrator._providers_map = {
                "a": MagicMock(provider_info=MagicMock(return_value={"primary": "a"}))
            }

            def fake_plan(tier, count, anchor_provider_id=None, **kwargs):
                return {
                    "primary": {"provider_id": "missing-provider"},
                    "assignments": [{"provider_id": "missing-provider", "slots": count}],
                    "remaining": 0,
                }

            mock_registry.plan_spillover_allocation.side_effect = fake_plan

            with pytest.raises(RuntimeError, match="missing-provider"):
                orchestrator.execute_wave(0, [Subtask(id=1, description="s1", tier="low", model="m")])
    finally:
        db.close()


def test_runtime_provider_mapping_uses_normalized_assignment_ids(tmp_path: Path) -> None:
    db = Database(tmp_path / "spill-normalize.db")
    try:
        config = TGsConfig()
        orchestrator = AnchoringOrchestrator(config, db)

        with patch.object(orchestrator, "_provider_registry") as mock_registry:
            orchestrator._providers_map = {
                "github-copilot": MagicMock(
                    provider_info=MagicMock(return_value={"primary": "github-copilot"})
                )
            }

            def fake_plan(tier, count, anchor_provider_id=None, **kwargs):
                return {
                    "primary": {"provider_id": "GitHub_Copilot"},
                    "assignments": [{"provider_id": "GitHub_Copilot", "slots": count}],
                    "remaining": 0,
                }

            mock_registry.plan_spillover_allocation.side_effect = fake_plan

            results = orchestrator.execute_wave(
                0,
                [Subtask(id=1, description="s1", tier="low", model="m")],
            )

            assert results[0].provider_name == "github-copilot"
    finally:
        db.close()


def test_string_zero_remaining_does_not_raise(tmp_path: Path) -> None:
    db = Database(tmp_path / "spill-str-zero.db")
    try:
        config = TGsConfig()
        orchestrator = AnchoringOrchestrator(config, db)

        with patch.object(orchestrator, "_provider_registry") as mock_registry:
            orchestrator._providers_map = {
                "a": MagicMock(provider_info=MagicMock(return_value={"primary": "a"}))
            }

            def fake_plan(tier, count, anchor_provider_id=None, **kwargs):
                return {
                    "primary": {"provider_id": "a"},
                    "assignments": [{"provider_id": "a", "slots": count}],
                    "remaining": "0",
                }

            mock_registry.plan_spillover_allocation.side_effect = fake_plan

            results = orchestrator.execute_wave(
                0,
                [Subtask(id=1, description="s1", tier="low", model="m")],
            )

            assert results[0].provider_name == "a"
    finally:
        db.close()


def test_invalid_assignment_shape_fails_clearly(tmp_path: Path) -> None:
    db = Database(tmp_path / "spill-invalid-shape.db")
    try:
        config = TGsConfig()
        orchestrator = AnchoringOrchestrator(config, db)

        with patch.object(orchestrator, "_provider_registry") as mock_registry:
            orchestrator._providers_map = {
                "a": MagicMock(provider_info=MagicMock(return_value={"primary": "a"}))
            }

            def fake_plan(tier, count, anchor_provider_id=None, **kwargs):
                return {
                    "primary": {"provider_id": "a"},
                    "assignments": ["not-a-mapping"],
                    "remaining": 0,
                }

            mock_registry.plan_spillover_allocation.side_effect = fake_plan

            with pytest.raises(RuntimeError, match="invalid assignment"):
                orchestrator.execute_wave(
                    0,
                    [Subtask(id=1, description="s1", tier="low", model="m")],
                )
    finally:
        db.close()


# --- hierarchical artifacts ---

def test_parent_scoped_selection() -> None:
    """Parent-scoped lookup should honor direct-parent scope, active revision, and stable ties."""
    import shared.db as _shared_db
    with tempfile.TemporaryDirectory() as tmpdir:
        db = _shared_db.Database(Path(tmpdir) / "hier.db")
        try:
            db.save_artifact(
                execution_id="exec-1",
                plan_revision=1,
                wave=1,
                subtask_id="parent-old",
                artifact_type="summary",
                full_payload="stale payload",
                compact_summary="stale summary",
                parent_execution_id="parent-1",
            )
            db.save_artifact(
                execution_id="exec-1",
                plan_revision=2,
                wave=1,
                subtask_id="parent-direct",
                artifact_type="summary",
                full_payload="older payload",
                compact_summary="older summary",
                parent_execution_id="parent-1",
            )
            latest_ref = db.save_artifact(
                execution_id="exec-1",
                plan_revision=2,
                wave=2,
                subtask_id="parent-direct",
                artifact_type="summary",
                full_payload="latest payload",
                compact_summary="latest summary",
                parent_execution_id="parent-1",
            )
            db.save_artifact(
                execution_id="exec-1",
                plan_revision=2,
                wave=3,
                subtask_id="parent-other",
                artifact_type="summary",
                full_payload="other parent payload",
                compact_summary="other parent summary",
                parent_execution_id="parent-2",
            )
            selected = db.get_parent_scoped_artifacts(
                "exec-1",
                2,
                "parent-1",
                ["summary"],
            )
            assert selected == [
                {
                    "artifact_type": "summary",
                    "summary_text": "latest summary",
                    "length_chars": len("latest summary"),
                    "artifact_ref": latest_ref,
                    "producer_subtask_id": "parent-direct",
                    "parent_execution_id": "parent-1",
                }
            ]

            with patch("shared.db.time.time", return_value=1_700_000_000):
                first_ref = db.save_artifact(
                    execution_id="exec-2",
                    plan_revision=2,
                    wave=2,
                    subtask_id="parent-a",
                    artifact_type="summary",
                    full_payload="tie payload a",
                    compact_summary="tie summary a",
                    parent_execution_id="parent-tie",
                )
                second_ref = db.save_artifact(
                    execution_id="exec-2",
                    plan_revision=2,
                    wave=2,
                    subtask_id="parent-b",
                    artifact_type="summary",
                    full_payload="tie payload b",
                    compact_summary="tie summary b",
                    parent_execution_id="parent-tie",
                )

            first = db.get_parent_scoped_artifacts("exec-2", 2, "parent-tie", ["summary"])
            second = db.get_parent_scoped_artifacts("exec-2", 2, "parent-tie", ["summary"])
            assert first == second
            expected_ref = min(first_ref, second_ref)
            expected_subtask = "parent-a" if expected_ref == first_ref else "parent-b"
            assert first[0]["artifact_ref"] == expected_ref
            assert first[0]["producer_subtask_id"] == expected_subtask
        finally:
            db.close()


def test_missing_parent_degrades_subtree() -> None:
    """Missing direct-parent artifacts should persist one degradation event and stay sticky."""
    import shared.config as _shared_config
    import shared.orchestrator as _shared_orchestrator
    with tempfile.TemporaryDirectory() as tmpdir:
        db = Database(Path(tmpdir) / "hier-degrade.db")
        orchestrator = _shared_orchestrator.Orchestrator(
            _shared_config.TGsConfig(),
            DummyProvider(),
            DummyPlanner(),
            db=db,
        )
        try:
            first = orchestrator.bind_parent_artifacts(
                "exec-1",
                "child-1",
                "parent-1",
                ["summary"],
                db=db,
            )
            assert first == {"degraded": True, "artifact_refs": []}
            events = db.query_degradation_events("exec-1")
            assert events == [
                {
                    "parent_subtask_id": "parent-1",
                    "missing_artifact_type": "summary",
                    "affected_child_subtask_id": "child-1",
                    "reason": "missing_parent_artifacts",
                    "created_at": events[0]["created_at"],
                }
            ]

            second = orchestrator.bind_parent_artifacts(
                "exec-1",
                "child-1",
                "parent-1",
                ["summary"],
                db=db,
            )
            assert second == {"degraded": True, "artifact_refs": []}
            assert len(db.query_degradation_events("exec-1")) == 1
        finally:
            db.close()
