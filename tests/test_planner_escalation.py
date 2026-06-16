#!/usr/bin/env python3
"""LLM-escalation escape hatch for the host-native heuristic planner.

Covers _planner_plan_for_caller in mcp_server: complex tasks escalate to the LLM
planner, simple tasks stay heuristic, escalation degrades gracefully when the
backend is unavailable, and the behaviour is gated by config.
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
from shared.config import TGsConfig
from shared.db import Database
from shared.planner import CLIBackend, Planner
from shared.router import TaskRouter


class RecordingBackend(CLIBackend):
    """Always returns no output (simulates an unavailable/erroring LLM planner)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        self.calls.append(prompt)
        return None


class PlanBackend(CLIBackend):
    """Returns a valid PLAN_JSON payload so escalation yields a real LLM plan."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, prompt: str, model: str | None = None, timeout: int = 120) -> str | None:
        self.calls.append(prompt)
        return (
            '<PLAN_JSON>{"analysis":"llm-planned","subtasks":'
            '[{"id":1,"description":"design the shared interface","tier":"high","depends_on":[]}],'
            '"strategy":"sequential","topology":"linear"}</PLAN_JSON>'
        )


class StubRegistry:
    def select_provider(self, tier: str, *, caller: str | None = None, prefer_free: bool = True):
        from types import SimpleNamespace

        return SimpleNamespace(
            name="cursor",
            display_name="Cursor",
            resolve_model=lambda _tier: "cursor-model",
            cost_rank=0,
            billing_tier="free",
            is_free=True,
        )


# Coupled (shared dir + 'interface'/'schema' keyword), 4 source files, design keywords.
COMPLEX_TASK = (
    "Refactor the shared parser interface across pkg/a.py, pkg/b.py, "
    "pkg/c.py and pkg/d.py so they share one event schema."
)
SIMPLE_TASK = "Create greet.py in sandbox/demo that prints hello."


def _harness(monkeypatch, tmp, cfg, backend):
    db_path = Path(tmp) / "escalation.db"
    cfg.db_path = db_path
    db = Database(db_path=db_path)
    planner = Planner(cfg, backend, db=db)
    router = TaskRouter(cfg)
    monkeypatch.setattr(mcp_server, "_ensure_init", lambda: (cfg, db, router, planner, None))
    monkeypatch.setattr(mcp_server, "get_registry", lambda: StubRegistry())
    monkeypatch.setattr(mcp_server, "_resolve_caller", lambda: "cursor")


def test_complex_task_escalates_to_llm_planner_when_refinement_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as td:
        backend = PlanBackend()
        cfg = TGsConfig(heuristic_complexity_llm_fallback=True)
        cfg.host_fast_start.llm_refinement = True
        _harness(monkeypatch, td, cfg, backend)
        result = mcp_server.handle_plan_task({"task": COMPLEX_TASK + " refinement-enabled"})
        assert backend.calls, "LLM planner backend should have been invoked"
        assert result.get("planner_mode") == "heuristic_escalated"


def test_complex_task_falls_back_when_refinement_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as td:
        backend = RecordingBackend()
        cfg = TGsConfig(heuristic_complexity_llm_fallback=True)
        cfg.host_fast_start.llm_refinement = True
        _harness(monkeypatch, td, cfg, backend)
        result = mcp_server.handle_plan_task({"task": COMPLEX_TASK + " backend-unavailable"})
        assert backend.calls, "escalation should be attempted"
        # Backend returned nothing → graceful fall back to heuristic.
        assert result.get("planner_mode") == "heuristic"
        assert result.get("subtasks")


def test_fast_start_blocks_configured_pre_spawn_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as td:
        backend = RecordingBackend()
        cfg = TGsConfig(heuristic_complexity_llm_fallback=True)
        _harness(monkeypatch, td, cfg, backend)
        result = mcp_server.handle_plan_task({"task": COMPLEX_TASK + " fast-start-block"})
        assert backend.calls == [], "fast-start default must not call the LLM planner before handoff"
        assert result.get("planner_mode") == "heuristic"


def test_simple_task_stays_heuristic_no_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as td:
        backend = RecordingBackend()
        _harness(monkeypatch, td, TGsConfig(), backend)
        result = mcp_server.handle_plan_task({"task": SIMPLE_TASK + " simple-no-escalation"})
        assert backend.calls == [], "simple task must not call the LLM planner"
        assert result.get("planner_mode") == "heuristic"


def test_escalation_disabled_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as td:
        backend = RecordingBackend()
        cfg = TGsConfig(heuristic_complexity_llm_fallback=False)
        _harness(monkeypatch, td, cfg, backend)
        result = mcp_server.handle_plan_task({"task": COMPLEX_TASK + " disabled"})
        assert backend.calls == [], "escalation disabled → no LLM call"
        assert result.get("planner_mode") == "heuristic"
