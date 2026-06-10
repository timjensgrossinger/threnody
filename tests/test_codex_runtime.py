from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex.providers import CodexProvider
from codex.providers_legacy import adapter_from_legacy
from shared.config import TGsConfig
from shared.discovery import (
    CLIProvider,
    DetectReason,
    ProviderReadiness,
    ProviderRegistry,
)
from shared.orchestrator import Orchestrator, Provider
from shared.planner import Subtask
from shared.snapshot import FileSnapshot


def _routeable_provider(name: str, model: str, cost: int) -> CLIProvider:
    provider = CLIProvider(
        name=name,
        binary=name,
        display_name=name,
        tier_models={"low": model},
        cost_rank={"low": cost},
    )
    provider.readiness = ProviderReadiness(
        routeable=True,
        reason=DetectReason.READY,
    )
    return provider


def test_codex_adapter_builds_concrete_provider() -> None:
    provider = adapter_from_legacy().invoke("build_provider")

    assert isinstance(provider, CodexProvider)
    assert provider.resolve_model("low") == "gpt-5.5"


def test_codex_provider_raises_on_cli_failure(monkeypatch) -> None:
    provider = CodexProvider()
    monkeypatch.setattr("codex.providers.shutil.which", lambda _binary: "/usr/bin/codex")
    monkeypatch.setattr(
        "codex.providers.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            "",
            "usage limit reached",
        ),
    )

    with pytest.raises(RuntimeError, match="usage limit reached"):
        provider.execute(
            Subtask(id=7, description="return ok", tier="low", model="gpt-5.5"),
            "gpt-5.5",
        )


def test_explicit_provider_id_restricts_registry_selection(monkeypatch) -> None:
    copilot = _routeable_provider("github-copilot", "gpt-5-mini", 0)
    codex = _routeable_provider("codex", "gpt-5.5", 1)
    monkeypatch.setattr("shared.discovery.BUILTIN_PROVIDERS", [copilot, codex])
    registry = ProviderRegistry(
        config_overrides={
            "preferred_routing_by_caller": {
                "codex": {"low": [{"provider": "codex"}]},
            },
        },
    )

    selected = registry.select_provider_for_tier(
        "low",
        caller="codex",
        provider_id="codex",
    )

    assert selected is not None
    assert selected["provider_id"] == "codex"
    assert selected["model"] == "gpt-5.5"


def test_cli_provider_subprocess_does_not_inherit_mcp_stdin(monkeypatch) -> None:
    provider = _routeable_provider("codex", "gpt-5.5", 1)
    provider.command_builder = lambda *_args, **_kwargs: [
        "codex",
        "exec",
        "prompt",
    ]
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert provider.execute("prompt", "gpt-5.5", retries=0) == "ok"
    assert captured["stdin"] is subprocess.DEVNULL


def test_snapshot_ignores_router_runtime_files(tmp_path: Path) -> None:
    status_file = tmp_path / "threnody-status.json"
    db_wal = tmp_path / "cache.db-wal"
    status_file.write_text("{}")
    db_wal.write_bytes(b"before")
    snapshot = FileSnapshot(tmp_path)
    snapshot.take()

    status_file.write_text('{"active": true}')
    db_wal.write_bytes(b"after")

    assert snapshot.diff_since() == []


class _PrimaryProvider(Provider):
    def resolve_model(self, tier: str) -> str:
        return f"primary-{tier}"

    def execute(
        self,
        subtask: Subtask,
        model: str,
        timeout: int = 120,
    ) -> str:
        return model

    def available_tiers(self) -> list[str]:
        return ["low"]


class _OverrideProvider(Provider):
    def __init__(self) -> None:
        self.seen_model: str | None = None

    def resolve_model(self, tier: str) -> str:
        return f"override-{tier}"

    def execute(
        self,
        subtask: Subtask,
        model: str,
        timeout: int = 120,
    ) -> str:
        self.seen_model = model
        return "ok"

    def available_tiers(self) -> list[str]:
        return ["low"]


class _Planner:
    _backend = SimpleNamespace(call=lambda *_args, **_kwargs: "summary")


def test_orchestrator_resolves_model_from_spillover_provider(
    tmp_path: Path,
) -> None:
    config = TGsConfig(db_path=tmp_path / "runtime.db")
    orchestrator = Orchestrator(config, _PrimaryProvider(), _Planner())
    override = _OverrideProvider()
    subtask = Subtask(
        id=1,
        description="return ok",
        tier="low",
        model="low",
    )

    result = orchestrator.execute_subtask(
        subtask,
        provider_override=override,
    )

    assert result.output == "ok"
    assert result.model == "override-low"
    assert override.seen_model == "override-low"


def test_orchestrator_passes_caller_to_spillover(tmp_path: Path) -> None:
    config = TGsConfig(db_path=tmp_path / "runtime.db")
    provider = _OverrideProvider()
    calls: list[dict[str, object]] = []

    class Registry:
        def plan_spillover_allocation(self, tier, count, **kwargs):
            calls.append({"tier": tier, "count": count, **kwargs})
            return {
                "primary": {},
                "assignments": [
                    {
                        "provider_id": "codex",
                        "provider": "Codex",
                        "slots": count,
                        "metadata": {},
                    }
                ],
                "remaining": 0,
            }

    orchestrator = Orchestrator(
        config,
        _PrimaryProvider(),
        _Planner(),
        provider_registry=Registry(),
        providers_map={"codex": provider},
        caller="codex",
    )

    result = orchestrator.execute_wave(
        1,
        [Subtask(id=1, description="return ok", tier="low", model="low")],
    )

    assert result[0].output == "ok"
    assert calls[0]["caller"] == "codex"
