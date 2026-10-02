"""Per-shell reasoning-effort capability table and its consumers."""
from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared import effort_support as es
from shared.agent_export import export_codex_tier_agents
from shared.config import TGsConfig
from shared.discovery import BUILTIN_PROVIDERS, ProviderRegistry, _build_aider_command
from shared.orchestrator import Orchestrator
from shared.planner import Subtask

ROOT = Path(__file__).resolve().parent.parent


def _provider(name: str):
    return next(p for p in BUILTIN_PROVIDERS if p.name == name)


# --- table -------------------------------------------------------------------


def test_host_native_modes() -> None:
    assert es.host_native_effort_mode("claude-code") == "frontmatter"
    assert es.host_native_effort_mode("claude") == "frontmatter"
    assert es.host_native_effort_mode("codex") == "codex_toml"
    for shell in ("github-copilot-cli", "cursor", "opencode", "junie", "mystery", None, ""):
        assert es.host_native_effort_mode(shell) is None


def test_subprocess_support_only_for_verified_flags() -> None:
    for pid in ("claude-code", "codex", "github-copilot", "copilot", "aider", "opencode"):
        assert es.subprocess_effort_supported(pid), pid
    for pid in ("cursor", "junie", "mistral-vibe", "blackbox-ai", "amazon-q", "windsurf", "nope", None):
        assert not es.subprocess_effort_supported(pid), pid
    assert es.EFFORT_SUPPORT["cursor"].verified is False


def test_default_routed_effort_follows_tier() -> None:
    assert es.default_routed_effort("low") == "low"
    assert es.default_routed_effort("medium") == "medium"
    assert es.default_routed_effort("high") == "high"
    assert es.default_routed_effort("medium", "long") in {"high", "medium"}


# --- codex TOML ----------------------------------------------------------------


def test_codex_tier_agents_parse_and_carry_effort(tmp_path: Path) -> None:
    written = export_codex_tier_agents(tmp_path, TGsConfig.defaults())
    assert len(written) == 12
    base = tomllib.loads((tmp_path / "threnody-medium.toml").read_text(encoding="utf-8"))
    assert base["name"] == "threnody-medium"
    assert "model_reasoning_effort" not in base
    assert base["developer_instructions"].startswith("## Threnody host subagent")
    assert not base["developer_instructions"].startswith("---")
    for tier in ("low", "medium", "high"):
        for effort in ("low", "medium", "high"):
            data = tomllib.loads(
                (tmp_path / f"threnody-{tier}-{effort}.toml").read_text(encoding="utf-8")
            )
            assert data["name"] == f"threnody-{tier}-{effort}"
            assert data["model_reasoning_effort"] == effort
            assert data["description"] and data["developer_instructions"]


def test_codex_export_never_touches_foreign_files(tmp_path: Path) -> None:
    mine = tmp_path / "my-agent.toml"
    mine.write_text('name = "mine"\n', encoding="utf-8")
    export_codex_tier_agents(tmp_path, TGsConfig.defaults())
    assert mine.read_text(encoding="utf-8") == 'name = "mine"\n'


def test_codex_toml_escapes_hostile_source(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "threnody-low.md").write_text(
        '---\nname: threnody-low\ndescription: say "hi" \\ ok\n---\nbody """ with \\ and ☃ \U0001f600 \x7f\n',
        encoding="utf-8",
    )
    out = tmp_path / "out"
    export_codex_tier_agents(out, TGsConfig.defaults(), source_dir=src)
    data = tomllib.loads((out / "threnody-low-high.toml").read_text(encoding="utf-8"))
    assert '"""' in data["developer_instructions"]
    assert "\U0001f600" in data["developer_instructions"]


# --- subprocess argv ----------------------------------------------------------


def test_copilot_argv_gets_effort_after_separator() -> None:
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=False),
    ):
        cmd = _provider("github-copilot")._build_command("hi", "gpt-5-mini", effort="high")
        plain = _provider("github-copilot")._build_command("hi", "gpt-5-mini")
    assert cmd.index("--") < cmd.index("--effort")
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert "--effort" not in plain


def test_aider_argv_gets_reasoning_effort() -> None:
    cmd = _build_aider_command(_provider("aider"), "execute", "m", "p", "low")
    assert cmd[cmd.index("--reasoning-effort") + 1] == "low"
    assert "--reasoning-effort" not in _build_aider_command(_provider("aider"), "execute", "m", "p")
    via_builder = _provider("aider")._build_command("p", "m", effort="high")
    assert via_builder[via_builder.index("--reasoning-effort") + 1] == "high"


def test_cursor_argv_unchanged() -> None:
    cursor = _provider("cursor")
    with_effort = cursor._build_command("p", "m", effort="high")
    without = cursor._build_command("p", "m")
    assert "--reasoning-effort" in with_effort  # existing explicit behaviour kept
    assert "--reasoning-effort" not in without


def _registry(config: dict | None = None) -> ProviderRegistry:
    reg = ProviderRegistry.__new__(ProviderRegistry)
    reg._config_overrides = config or {}
    return reg


@pytest.mark.parametrize("name,expected", [
    ("github-copilot", ("high", "routed")),
    ("aider", ("high", "routed")),
    ("cursor", (None, None)),  # unverified: routed effort never added
])
def test_registry_routed_effort_only_for_verified_providers(name, expected) -> None:
    got = _registry()._resolve_effort_for_provider(_provider(name), "high", None, "high")
    assert got == expected


def test_registry_precedence_explicit_then_config_then_routed() -> None:
    cfg = {"provider_effort_defaults": {"github-copilot": {"high": "low"}}}
    prov = _provider("github-copilot")
    assert _registry(cfg)._resolve_effort_for_provider(prov, "high", "medium", "high") == ("medium", "explicit")
    assert _registry(cfg)._resolve_effort_for_provider(prov, "high", None, "high") == ("low", "config_default")
    assert _registry()._resolve_effort_for_provider(prov, "high", None, "high") == ("high", "routed")


# --- orchestrator precedence -------------------------------------------------


class _Cfg:
    def __init__(self, pins: dict | None = None) -> None:
        self.pins = pins or {}

    def get_default_effort(self, provider_id: str, tier: str):
        return self.pins.get((provider_id, tier))


def _orch(pins: dict | None = None) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch._config = _Cfg(pins)
    return orch


def _st(provider_id: str | None, **extra) -> Subtask:
    st = Subtask(id=1, description="d", tier="medium", provider_id=provider_id)
    for k, v in extra.items():
        setattr(st, k, v)
    return st


def test_orchestrator_config_pin_wins() -> None:
    orch = _orch({("codex", "medium"): "low"})
    assert orch._effort_for_subtask(_st("codex", reasoning_effort="high"), "medium") == "low"


def test_orchestrator_routed_when_unpinned() -> None:
    orch = _orch()
    assert orch._effort_for_subtask(_st("codex"), "high") == "high"
    assert orch._effort_for_subtask(_st("codex"), "low") == "low"
    assert orch._effort_for_subtask(_st("codex", reasoning_effort="high"), "medium") == "high"
    assert orch._effort_for_subtask(_st("codex", reasoning_effort="bogus"), "medium") == "medium"


def test_orchestrator_unsupported_or_unknown_provider_is_none() -> None:
    orch = _orch()
    assert orch._effort_for_subtask(_st("cursor"), "high") is None
    assert orch._effort_for_subtask(_st("junie"), "high") is None
    assert orch._effort_for_subtask(_st(None), "high") is None


# --- orchestrator direct-execute paths ---------------------------------------


def _load_claude_provider():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "claude_code_providers_under_test", ROOT / "claude-code" / "providers.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _capture_run(monkeypatch, module):
    seen: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        seen.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return seen


def test_claude_provider_argv_effort(monkeypatch) -> None:
    mod = _load_claude_provider()
    prov = mod.ClaudeCodeProvider()
    prov._claude_available = True
    prov._copilot_available = False
    seen = _capture_run(monkeypatch, mod)
    prov.execute(_st(None), "sonnet", 5, effort="high")
    prov.execute(_st(None), "sonnet", 5)
    assert seen[0][seen[0].index("--effort") + 1] == "high"
    assert "--effort" not in seen[1]
    assert prov.effort_applied_for("sonnet") is True
    prov._copilot_available = True
    assert prov.effort_applied_for("gpt-5-mini") is False  # gh copilot agent route drops it


def test_copilot_provider_argv_effort(monkeypatch) -> None:
    import copilot.providers as mod

    prov = mod.CopilotProvider()
    prov._gh_available = True
    seen = _capture_run(monkeypatch, mod)
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=False),
    ):
        prov.execute(_st(None), "gpt-5-mini", 5, effort="medium")
        prov.execute(_st(None), "gpt-5-mini", 5)
    assert seen[0][seen[0].index("--effort") + 1] == "medium"
    assert "--effort" not in seen[1]


def test_codex_provider_argv_effort(monkeypatch) -> None:
    import codex.providers as mod

    seen = _capture_run(monkeypatch, mod)
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/codex")
    prov = mod.CodexProvider()
    prov.execute(_st(None), "gpt-5", 5, effort="high")
    prov.execute(_st(None), "gpt-5", 5)
    assert 'model_reasoning_effort="high"' in seen[0]
    assert not any("model_reasoning_effort" in a for a in seen[1])


class _NoEffortProvider:
    def execute(self, subtask, model, timeout=120):
        return "x"


class _EffortProvider:
    effort_provider_id = "codex"

    def __init__(self) -> None:
        self.got: list[str | None] = []

    def execute(self, subtask, model, timeout=120, effort=None):
        self.got.append(effort)
        return "x"


def test_call_provider_execute_passes_effort_only_when_accepted() -> None:
    ep = _EffortProvider()
    Orchestrator._call_provider_execute(ep, _st(None), "m", 5, effort="high")
    Orchestrator._call_provider_execute(ep, _st(None), "m", 5)
    assert ep.got == ["high", None]
    # a provider without an effort param must not receive the kwarg
    assert Orchestrator._call_provider_execute(_NoEffortProvider(), _st(None), "m", 5, effort="high") == "x"


def test_applied_effort_none_when_not_applied() -> None:
    orch = _orch()
    assert orch._applied_effort(_NoEffortProvider(), _st("codex"), "high", "m") is None
    # provider id falls back to the provider's own effort_provider_id
    assert orch._applied_effort(_EffortProvider(), _st(None), "high", "m") == "high"

    class Gated(_EffortProvider):
        def effort_applied_for(self, model):
            return False

    assert orch._applied_effort(Gated(), _st(None), "high", "m") is None
    # unsupported provider id: nothing to apply even though execute accepts the kwarg
    class Cursorish(_EffortProvider):
        effort_provider_id = "cursor"

    assert orch._applied_effort(Cursorish(), _st(None), "high", "m") is None
