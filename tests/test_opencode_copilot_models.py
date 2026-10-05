"""OpenCode verbose-catalog tiering, Copilot help-config discovery, models.dev effort gating."""
from __future__ import annotations

import copy
import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared import model_capabilities as mc
from shared.config import (
    TGsConfig,
    _shell_tier_model_defaults,
    parse_routing_policy_config,
)
from shared.discovery import (
    BUILTIN_PROVIDERS,
    ProviderRegistry,
    _build_gh_copilot_command,
)
from shared.effort_support import model_effort_accepted
from shared.model_catalog import ModelCatalog
from shared.model_registry import (
    assign_provider_relative_tiers,
    bootstrap_tier_map,
    normalize_models,
    tier_projection,
)
from shared.provider_model_adapters import (
    CopilotHelpConfigDiscoveryAdapter,
    OpenCodeModelDiscoveryAdapter,
    parse_copilot_help_config,
    parse_opencode_verbose,
    tier_copilot_catalog,
    tier_opencode_catalog,
)


def _details(model_id: str, *, family: str | None, cost: tuple[float, float], context: int,
             release: str, variants: tuple[str, ...] = (), status: str = "active",
             reasoning: bool = True) -> str:
    provider, _, bare = model_id.partition("/")
    payload = {
        "id": bare,
        "providerID": provider,
        "name": bare.title(),
        "status": status,
        "cost": {"input": cost[0], "output": cost[1], "cache": {"read": 0, "write": 0}},
        "limit": {"context": context, "output": 32000},
        "capabilities": {"reasoning": reasoning, "toolcall": True, "input": {"text": True}},
        "release_date": release,
        "variants": {name: {"reasoningEffort": name} for name in variants},
    }
    if family is not None:
        payload["family"] = family
    return f"{model_id}\n{json.dumps(payload, indent=2)}\n"


OPENCODE_VERBOSE = "".join([
    "INFO  2026-10-05 refreshing models\n",  # banner noise
    _details("opencode/nemotron-3-ultra-free", family="nemotron-free", cost=(0, 0),
             context=1_000_000, release="2026-06-04"),
    _details("opencode/space-bunny-free", family=None, cost=(0, 0), context=1_048_576,
             release="2026-09-23", variants=("low", "medium", "high", "xhigh", "max")),
    _details("opencode/big-pickle", family="big-pickle", cost=(0, 0), context=200_000,
             release="2025-10-17"),
    _details("opencode-go/glm-5.3", family="glm", cost=(1.4, 4.4), context=1_000_000,
             release="2026-08-14", variants=("low", "high", "max")),
    _details("openrouter/openrouter/auto", family="auto", cost=(0, 0), context=2_000_000,
             release="2023-11-08"),
    _details("github-copilot/claude-sonnet-5", family="claude-sonnet", cost=(2, 10),
             context=1_000_000, release="2026-06-30", variants=("low", "medium", "high")),
    _details("github-copilot/claude-opus-5.5", family="claude-opus", cost=(4, 20),
             context=1_000_000, release="2026-09-22", variants=("low", "medium", "high", "max")),
    _details("github-copilot/claude-opus-4.8-fast", family="claude-opus", cost=(10, 50),
             context=200_000, release="2026-05-28"),
    _details("anthropic/claude-sonnet-5.5", family="claude-sonnet", cost=(3, 15),
             context=1_000_000, release="2026-09-28", variants=("low", "high")),
    _details("anthropic/claude-opus-5.5", family="claude-opus", cost=(5, 25),
             context=1_000_000, release="2026-09-22"),
    _details("anthropic/claude-opus-6-preview", family="claude-opus", cost=(5, 25),
             context=1_000_000, release="2026-10-01"),
    _details("anthropic/claude-sonnet-4", family="claude-sonnet", cost=(3, 15),
             context=200_000, release="2026-10-02", status="deprecated"),
    "anthropic/no-details-model\n",
])

COPILOT_HELP = """Configuration Settings:

  `logLevel`: log level for CLI; defaults to "default".

  `model`: AI model to use for Copilot CLI; can be changed with /model command or --model flag option.
    - "claude-sonnet-5"
    - "claude-sonnet-4.6"
    - "claude-haiku-4.5"
    - "claude-opus-4.8"
    - "claude-opus-4.8-fast"
    - "claude-opus-4.7"
    - "gpt-5.6-sol"
    - "gpt-5.6-terra"
    - "gpt-5.4"
    - "gpt-5.4-mini"
    - "gpt-5-mini"
    - "gemini-3.5-flash"
    - "kimi-k2.7-code"

  `contextTier`: context window tier for tiered-pricing models (e.g., "default" or "long_context").
    - "default"
"""

MODELS_DEV = {
    "github-copilot": {
        "models": {
            "gpt-5-mini": {"reasoning": True, "reasoning_options": [
                {"type": "effort", "values": ["low", "medium", "high"]}]},
            "gpt-5.4": {"reasoning": True, "reasoning_options": [
                {"type": "effort", "values": ["none", "low", "medium", "high", "xhigh"]}]},
            "claude-opus-4.8": {"reasoning": True, "reasoning_options": [
                {"type": "effort", "values": ["low", "medium", "high", "xhigh", "max", "ultra"]}]},
            "claude-haiku-4.5": {"reasoning": True, "reasoning_options": [
                {"type": "budget_tokens", "min": 1024, "max": 32000}]},
            "gpt-5.4-nano": {"reasoning": True, "reasoning_options": []},
            "gpt-4.1": {"reasoning": False},
            "mystery-reasoner": {"reasoning": True},
        }
    },
    "anthropic": {
        "models": {
            "claude-opus-5.5": {"reasoning": True, "reasoning_options": [
                {"type": "effort", "values": ["low", "medium", "high", "max"]}]},
        }
    },
}


@pytest.fixture
def models_dev_file() -> Path:
    path = Path(os.environ["THRENODY_OPENCODE_MODELS_JSON"])
    path.write_text(json.dumps(MODELS_DEV), encoding="utf-8")
    return path


def _opencode_models():
    entries = tier_opencode_catalog(parse_opencode_verbose(OPENCODE_VERBOSE))
    models = normalize_models("opencode", entries, source="live_provider_catalog")
    assign_provider_relative_tiers(models)
    return {model.model_id: model for model in models}


def _builtin(name: str):
    return copy.deepcopy(next(p for p in BUILTIN_PROVIDERS if p.name == name))


# --- OpenCode: parsing ---------------------------------------------------------


def test_verbose_parser_pairs_ids_with_multiline_json_and_skips_noise() -> None:
    entries = parse_opencode_verbose(OPENCODE_VERBOSE)
    ids = [entry["model_id"] for entry in entries]
    assert len(ids) == 13 and "INFO" not in " ".join(ids)
    by_id = {entry["model_id"]: entry for entry in entries}
    bunny = by_id["opencode/space-bunny-free"]
    assert bunny["input_price_per_million"] == 0.0
    assert bunny["context_window"] == 1_048_576
    assert bunny["reasoning_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert bunny["provider_metadata"]["variants_listed"] is True
    assert by_id["anthropic/claude-sonnet-5.5"]["provider_metadata"]["release_date"] == "2026-09-28"
    assert by_id["anthropic/claude-sonnet-4"]["deprecated"] is True
    assert by_id["anthropic/no-details-model"]["provider_metadata"] == {"provider": "anthropic"}


def test_verbose_parser_survives_truncated_json() -> None:
    raw = "opencode/a-free\n{\n  \"id\": \"a-free\",\n  \"cost\": {\nopencode/b-free\n" + _details(
        "opencode/c-free", family=None, cost=(0, 0), context=10, release="2026-01-01")
    ids = [entry["model_id"] for entry in parse_opencode_verbose(raw)]
    assert ids == ["opencode/a-free", "opencode/b-free", "opencode/c-free"]


# --- OpenCode: tiering ---------------------------------------------------------


def test_opencode_tiers_from_verbose_evidence() -> None:
    models = _opencode_models()
    assert tier_projection(list(models.values())) == {
        # Zero-cost Zen models: -free suffix, reasoning, then the largest context.
        "low": "opencode/space-bunny-free",
        # Newest claude-sonnet on a configured provider.
        "medium": "anthropic/claude-sonnet-5.5",
        # Same release date on two providers: the cheaper listing wins.
        "high": "github-copilot/claude-opus-5.5",
    }
    assert models["opencode/space-bunny-free"].tier_reason.startswith("opencode:zero_cost,provider=opencode")
    assert models["anthropic/claude-sonnet-5.5"].tier_reason == (
        "opencode:family=claude-sonnet,release=2026-09-28,provider=anthropic"
    )


def test_opencode_routers_deprecated_and_variants_never_route() -> None:
    models = _opencode_models()
    auto = models["openrouter/openrouter/auto"]
    assert (auto.tier, auto.routeable, auto.tier_reason) == (None, False, "opencode:excluded=router")
    old = models["anthropic/claude-sonnet-4"]
    assert old.routeable is False and old.tier_reason == "opencode:excluded=status:deprecated"
    assert models["anthropic/claude-opus-6-preview"].tier_reason == "opencode:skipped=preview"
    assert models["github-copilot/claude-opus-4.8-fast"].tier_reason == "opencode:skipped=fast"
    # Zero-cost but neither -free nor the best: catalogued, unrouted.
    assert models["opencode/big-pickle"].routeable is False
    assert sum(model.routeable for model in models.values()) == 3


def test_opencode_projection_widens_only_filled_tiers(temp_db_fixture) -> None:
    catalog = ModelCatalog(db=temp_db_fixture)
    entries = tier_opencode_catalog(parse_opencode_verbose(OPENCODE_VERBOSE))
    catalog.refresh("opencode", entries, source="live_provider_catalog")
    provider = _builtin("opencode")
    catalog._project_provider_catalog(provider)

    assert provider.tier_models == {
        "low": "opencode/space-bunny-free",
        "medium": "anthropic/claude-sonnet-5.5",
        "high": "github-copilot/claude-opus-5.5",
    }
    assert provider.allowed_auto_route_tiers == ("low",)  # static floor untouched
    assert provider.cost_rank == {"low": 0, "medium": 2, "high": 3}
    routeable = {row["model_id"] for row in provider.model_catalog if row["auto_routeable"]}
    assert routeable == set(provider.tier_models.values())


def test_opencode_without_configured_providers_stays_low_only(temp_db_fixture) -> None:
    only_zen = "".join(
        _details(model_id, family=None, cost=(0, 0), context=1000, release="2026-01-01")
        for model_id in ("opencode/alpha-free", "opencode/beta")
    ) + _details("openrouter/openrouter/auto", family="auto", cost=(0, 0), context=1, release="2023-01-01")
    catalog = ModelCatalog(db=temp_db_fixture)
    catalog.refresh("opencode", tier_opencode_catalog(parse_opencode_verbose(only_zen)))
    provider = _builtin("opencode")
    catalog._project_provider_catalog(provider)
    assert provider.tier_models == {"low": "opencode/alpha-free"}
    assert set(provider.cost_rank) == {"low"}


def test_opencode_plain_list_fallback_fills_low_from_free_suffix() -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        if "--verbose" in cmd:
            return SimpleNamespace(returncode=1, stdout="", stderr="unknown flag")
        return SimpleNamespace(
            returncode=0,
            stdout="opencode/zeta-free\nopencode/big-pickle\nanthropic/claude-sonnet-5.5\n",
            stderr="",
        )

    with patch("shared.provider_model_adapters.subprocess.run", side_effect=fake_run):
        result = OpenCodeModelDiscoveryAdapter().discover_live()
    assert [call[-1] for call in calls] == ["--verbose", "models"]
    assert result is not None and result.successful
    assign_provider_relative_tiers(result.models)
    # No family/release evidence: medium/high are left empty, never guessed.
    assert tier_projection(result.models) == {"low": "opencode/zeta-free"}


def test_opencode_never_falls_back_to_generic_tier_ids() -> None:
    defaults = _shell_tier_model_defaults("opencode")
    assert defaults == bootstrap_tier_map("opencode")
    assert set(defaults) == {"low"}
    profile = TGsConfig.defaults().routing_policy.effective_profile("opencode")
    assert set(profile.tier_model_mapping) == {"low"}
    assert "claude-sonnet-5" not in profile.tier_model_mapping.values()


def test_shell_override_without_mapping_keeps_shell_defaults() -> None:
    policy = parse_routing_policy_config(
        {"mode": "custom", "shells": {"codex": {"mode": "guarded"}, "opencode": {"mode": "guarded"}}}
    )
    assert policy.effective_profile("codex").tier_model_mapping == _shell_tier_model_defaults("codex")
    assert set(policy.effective_profile("opencode").tier_model_mapping) == {"low"}


# --- OpenCode: --variant gating --------------------------------------------------


def _opencode_command(model: str, effort: str | None, catalog=None) -> list[str]:
    provider = _builtin("opencode")
    provider.model_catalog = catalog or []
    return provider._build_command("hi", model, effort=effort)


def test_opencode_variant_only_when_model_lists_it(models_dev_file) -> None:
    catalog = [
        {"model_id": "opencode/space-bunny-free", "reasoning_levels": ["low", "medium", "high"],
         "provider_metadata": {"variants_listed": True}},
        {"model_id": "opencode/nemotron-3-ultra-free", "reasoning_levels": [],
         "provider_metadata": {"variants_listed": True}},
    ]
    cmd = _opencode_command("opencode/space-bunny-free", "high", catalog)
    assert cmd[cmd.index("--variant") + 1] == "high"
    assert "--variant" not in _opencode_command("opencode/space-bunny-free", "max", catalog)
    assert "--variant" not in _opencode_command("opencode/nemotron-3-ultra-free", "high", catalog)
    assert "--variant" not in _opencode_command("opencode/unknown", "high", catalog)
    # No catalog row: models.dev keyed on the provider prefix.
    via_dev = _opencode_command("anthropic/claude-opus-5.5", "max")
    assert via_dev[via_dev.index("--variant") + 1] == "max"
    assert "--variant" not in _opencode_command("anthropic/claude-opus-5.5", "xhigh")


def test_opencode_registry_effort_reports_dropped_variant() -> None:
    reg = ProviderRegistry.__new__(ProviderRegistry)
    reg._config_overrides = {}
    provider = _builtin("opencode")
    provider.model_catalog = [
        {"model_id": "opencode/x-free", "reasoning_levels": [], "provider_metadata": {"variants_listed": True}},
    ]
    assert reg._resolve_effort_for_provider(provider, "low", None, "low", model="opencode/x-free") == (None, None)


# --- Copilot: help config ---------------------------------------------------------


def test_copilot_help_config_parser_reads_only_the_model_block() -> None:
    ids = parse_copilot_help_config(COPILOT_HELP)
    assert ids[0] == "claude-sonnet-5" and ids[-1] == "kimi-k2.7-code"
    assert len(ids) == 13 and "default" not in ids
    assert parse_copilot_help_config("no model block here\n") == []


def test_copilot_tiers_by_family_and_size(models_dev_file) -> None:
    entries = tier_copilot_catalog(parse_copilot_help_config(COPILOT_HELP))
    models = normalize_models("github-copilot", entries, source="copilot_help_config")
    assign_provider_relative_tiers(models)
    by_id = {model.model_id: model for model in models}
    assert tier_projection(models) == {
        # Bootstrap picks the CLI still lists win their tier (gpt-5-mini is 0x).
        "low": "gpt-5-mini",
        "medium": "gpt-5.4",
        # claude-opus-5 is not listed: the newest standard listed opus takes high.
        "high": "claude-opus-4.8",
    }
    assert by_id["claude-opus-4.8-fast"].tier == "high"
    assert by_id["claude-opus-4.8-fast"].provider_metadata["tier_rank"]["high"] > 0
    assert by_id["claude-haiku-4.5"].tier == "low"
    assert by_id["gemini-3.5-flash"].tier_reason == "copilot:size_token=flash"
    assert by_id["kimi-k2.7-code"].tier == "medium"
    assert by_id["gpt-5.6-sol"].tier == "high" and by_id["gpt-5.6-terra"].tier == "medium"
    assert all(model.provider_metadata["verified"] is True for model in models)
    assert by_id["gpt-5-mini"].request_multiplier == 0.0
    assert by_id["gpt-5-mini"].reasoning_levels == ("low", "medium", "high")
    # Levels outside the CLI enum are dropped.
    assert "ultra" not in by_id["claude-opus-4.8"].reasoning_levels


def test_copilot_adapter_runs_help_config_in_sandbox_env() -> None:
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=list(cmd), env=kw.get("env"), cwd=kw.get("cwd"))
        return SimpleNamespace(returncode=0, stdout=COPILOT_HELP, stderr="")

    adapter = CopilotHelpConfigDiscoveryAdapter(
        env_factory=lambda: {"COPILOT_HOME": "/sandbox"}, cwd_factory=lambda: "/sandbox"
    )
    with patch("shared.provider_model_adapters.subprocess.run", side_effect=fake_run):
        result = adapter.discover_live()
    assert seen["cmd"] == ["gh", "copilot", "--", "help", "config"]
    assert seen["env"] == {"COPILOT_HOME": "/sandbox"} and seen["cwd"] == "/sandbox"
    assert result is not None and result.source == "copilot_help_config" and len(result.models) == 13

    with patch(
        "shared.provider_model_adapters.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="nothing", stderr=""),
    ):
        assert adapter.discover_live() is None
    with patch(
        "shared.provider_model_adapters.subprocess.run",
        return_value=SimpleNamespace(returncode=4, stdout="", stderr="auth"),
    ), pytest.raises(RuntimeError):
        adapter.discover_live()


def test_copilot_builtin_uses_help_config_adapter() -> None:
    adapter = _builtin("github-copilot").model_discovery_adapter
    assert isinstance(adapter, CopilotHelpConfigDiscoveryAdapter)
    assert adapter.env_factory is not None and adapter.cwd_factory is not None


# --- models.dev effort levels -------------------------------------------------------


def test_models_dev_effort_levels(models_dev_file) -> None:
    assert mc.models_dev_effort_levels("github-copilot", "gpt-5-mini") == {"low", "medium", "high"}
    assert mc.models_dev_effort_levels("github-copilot", "claude-haiku-4.5") == frozenset()
    assert mc.models_dev_effort_levels("github-copilot", "gpt-5.4-nano") == frozenset()
    assert mc.models_dev_effort_levels("github-copilot", "gpt-4.1") == frozenset()
    assert mc.models_dev_effort_levels("github-copilot", "mystery-reasoner") is None
    assert mc.models_dev_effort_levels("github-copilot", "not-listed") is None
    assert mc.copilot_effort_levels("claude-opus-4.8") == {"low", "medium", "high", "xhigh", "max"}


@pytest.mark.parametrize(("model", "effort", "expected"), [
    ("gpt-5-mini", "low", True),
    ("gpt-5-mini", "xhigh", False),
    ("claude-haiku-4.5", "low", False),  # budget_tokens only
    ("gpt-5.4-nano", "low", False),
    ("not-listed", "low", False),  # unknown: conservative
])
def test_copilot_argv_effort_gated_per_model(models_dev_file, model, effort, expected) -> None:
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=True),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=False),
    ):
        cmd = _build_gh_copilot_command("hi", model, effort)
    assert ("--effort" in cmd) is expected
    if expected:
        assert cmd[cmd.index("--effort") + 1] == effort
    assert cmd[cmd.index("--model") + 1] == model


def test_copilot_effort_dropped_without_model_flag(models_dev_file) -> None:
    with (
        patch("shared.discovery._copilot_supports_model_flag", return_value=False),
        patch("shared.discovery._copilot_supports_disable_builtin_mcps", return_value=False),
    ):
        cmd = _build_gh_copilot_command("hi", "gpt-5-mini", "low")
    assert "--effort" not in cmd and "--model" not in cmd


def test_copilot_provider_effort_applied_for(models_dev_file) -> None:
    import copilot.providers as mod

    prov = mod.CopilotProvider()
    with patch("shared.discovery._copilot_supports_model_flag", return_value=True):
        assert prov.effort_applied_for("gpt-5-mini", effort="low") is True
        assert prov.effort_applied_for("claude-haiku-4.5", effort="low") is False
        assert prov.effort_applied_for("not-listed", effort="low") is False
    assert prov.effort_applied_for("gpt-5-mini") is True
    assert prov.effort_applied_for("claude-haiku-4.5") is False


def test_orchestrator_records_no_effort_when_model_drops_it(models_dev_file) -> None:
    import copilot.providers as mod
    from shared.orchestrator import Orchestrator
    from shared.planner import Subtask

    orch = Orchestrator.__new__(Orchestrator)
    orch._config = SimpleNamespace(get_default_effort=lambda *_a: None)
    subtask = Subtask(id=1, description="d", tier="low", provider_id="github-copilot")
    prov = mod.CopilotProvider()
    with patch("shared.discovery._copilot_supports_model_flag", return_value=True):
        assert orch._applied_effort(prov, subtask, "low", "gpt-5-mini") == "low"
        assert orch._applied_effort(prov, subtask, "low", "claude-haiku-4.5") is None


def test_registry_effort_resolution_uses_chosen_model(models_dev_file) -> None:
    reg = ProviderRegistry.__new__(ProviderRegistry)
    reg._config_overrides = {}
    provider = _builtin("github-copilot")
    assert reg._resolve_effort_for_provider(provider, "low", None, "low", model="gpt-5-mini") == ("low", "routed")
    assert reg._resolve_effort_for_provider(provider, "low", "high", None, model="claude-haiku-4.5") == (None, None)
    assert model_effort_accepted("codex", "anything", "high") is True


def test_models_dev_offline_without_cache_is_unknown(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_network_allowed", lambda: True)
    monkeypatch.setattr(mc, "_FETCH_ATTEMPTED_AT", 0.0)
    calls: list[str] = []

    def offline(request, timeout):
        calls.append(request.full_url)
        raise OSError("network unreachable")

    monkeypatch.setattr(mc.urllib.request, "urlopen", offline)
    assert mc.models_dev_effort_levels("github-copilot", "gpt-5-mini") is None
    assert mc.models_dev_effort_levels("github-copilot", "gpt-5-mini") is None
    assert calls == [mc.MODELS_DEV_URL]  # one attempt per TTL, not one per call
    assert not Path(os.environ["THRENODY_MODELS_DEV_CACHE"]).exists()


def test_models_dev_fetch_writes_cache_subset(monkeypatch) -> None:
    monkeypatch.setattr(mc, "_network_allowed", lambda: True)
    monkeypatch.setattr(mc, "_FETCH_ATTEMPTED_AT", 0.0)
    body = dict(MODELS_DEV)
    body["github-copilot"] = {"name": "GitHub Copilot", "models": {
        mid: dict(entry, cost={"input": 1}) for mid, entry in MODELS_DEV["github-copilot"]["models"].items()
    }}
    monkeypatch.setattr(
        mc.urllib.request, "urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(body).encode("utf-8")),
    )
    assert mc.copilot_effort_levels("gpt-5-mini") == {"low", "medium", "high"}
    written = json.loads(Path(os.environ["THRENODY_MODELS_DEV_CACHE"]).read_text(encoding="utf-8"))
    assert set(written["github-copilot"]["models"]["gpt-5-mini"]) == {"reasoning", "reasoning_options"}


def test_models_dev_stale_cache_used_when_offline(models_dev_file, monkeypatch) -> None:
    old = time.time() - 10 * mc.CACHE_TTL_SECONDS
    os.utime(models_dev_file, (old, old))
    monkeypatch.setattr(mc, "_network_allowed", lambda: False)
    assert mc.copilot_effort_levels("gpt-5-mini") == {"low", "medium", "high"}


def test_network_disabled_offline_and_in_test_mode(monkeypatch) -> None:
    assert mc._network_allowed() is False  # conftest sets THRENODY_MODELS_DEV_OFFLINE
    monkeypatch.delenv("THRENODY_MODELS_DEV_OFFLINE")
    monkeypatch.setenv("THRENODY_TEST_MODE", "1")
    assert mc._network_allowed() is False
