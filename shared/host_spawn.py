"""Host-native spawn contract helpers for meta-harness v2."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import TGsConfig, normalize_caller_id, normalize_routing_policy_shell_id
from .discovery import HOST_PROVIDER_NAMES, ROUTER_ONLY_PROVIDERS

HOST_SPAWN_ERROR = "HostNativeRequired"
COMPLIANCE_WARNING = (
    "router_only_allow_execution bypasses host-native execution and may violate "
    "provider OAuth policy — see docs/LEGAL.md"
)


@dataclass(frozen=True)
class HostSpawnSpec:
    """Machine-readable instruction for the MCP host to spawn a subagent."""

    tool: str
    method: str
    model: str | None
    subagent_type: str
    prompt: str
    tier: str
    caller: str | None = None
    wave_id: str | None = None
    target_files: list[str] = field(default_factory=list)
    id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool,
            "method": self.method,
            "subagent_type": self.subagent_type,
            "tier": self.tier,
            "prompt": self.prompt,
        }
        if self.model:
            payload["model"] = self.model
        if self.caller:
            payload["caller"] = self.caller
        if self.wave_id is not None:
            payload["wave_id"] = self.wave_id
        if self.target_files:
            payload["target_files"] = list(self.target_files)
        if self.id is not None:
            payload["id"] = self.id
        return payload


def host_tool_for_caller(caller: str | None) -> str:
    normalized = normalize_caller_id(caller)
    if normalized == "claude-code":
        return "Agent"
    return "Task"


def host_native_method_for_tier(tier: str) -> str:
    return "direct_edit" if tier == "low" else "host_task"


def host_native_model_for_tier(
    config: TGsConfig,
    caller: str | None,
    tier: str,
) -> str | None:
    shell_id = normalize_routing_policy_shell_id(normalize_caller_id(caller))
    if shell_id is None:
        return None
    profile = config.routing_policy.effective_profile(shell_id)
    model = profile.tier_model_mapping.get(tier)
    return model if isinstance(model, str) and model.strip() else None


def subagent_type_for_tier(tier: str) -> str:
    if tier in {"low", "medium", "high"}:
        return f"threnody-{tier}"
    return "generalPurpose"


def build_host_spawn(
    *,
    config: TGsConfig,
    caller: str | None,
    tier: str,
    prompt: str,
    wave_id: str | None = None,
    target_files: list[str] | None = None,
    spawn_id: str | None = None,
    model: str | None = None,
) -> HostSpawnSpec:
    return HostSpawnSpec(
        tool=host_tool_for_caller(caller),
        method=host_native_method_for_tier(tier),
        model=model or host_native_model_for_tier(config, caller, tier),
        subagent_type=subagent_type_for_tier(tier),
        prompt=prompt,
        tier=tier,
        caller=normalize_caller_id(caller),
        wave_id=wave_id,
        target_files=list(target_files or []),
        id=spawn_id,
    )


def _subtask_target_files(subtask: Mapping[str, Any]) -> list[str]:
    target_file = subtask.get("target_file")
    if isinstance(target_file, str) and target_file.strip():
        return [target_file.strip()]
    return []


def build_host_spawn_waves(
    plan_dict: Mapping[str, Any],
    *,
    config: TGsConfig,
    caller: str | None,
) -> list[dict[str, Any]]:
    subtasks = plan_dict.get("subtasks")
    waves = plan_dict.get("waves")
    if not isinstance(subtasks, list) or not isinstance(waves, list):
        return []

    subtask_by_id: dict[Any, dict[str, Any]] = {}
    for raw in subtasks:
        if isinstance(raw, dict) and raw.get("id") is not None:
            subtask_by_id[raw["id"]] = raw

    host_waves: list[dict[str, Any]] = []
    for wave_idx, wave_ids in enumerate(waves, start=1):
        if not isinstance(wave_ids, list):
            continue
        agents: list[dict[str, Any]] = []
        for sid in wave_ids:
            subtask = subtask_by_id.get(sid)
            if not isinstance(subtask, dict):
                continue
            tier = str(subtask.get("tier") or "medium")
            prompt = str(subtask.get("description") or "").strip()
            if not prompt:
                continue
            raw_model = subtask.get("model")
            model = str(raw_model).strip() if isinstance(raw_model, str) and str(raw_model).strip() else None
            agents.append(
                build_host_spawn(
                    config=config,
                    caller=caller,
                    tier=tier,
                    prompt=prompt,
                    wave_id=f"wave-{wave_idx}",
                    target_files=_subtask_target_files(subtask),
                    spawn_id=str(subtask.get("id") or subtask.get("stable_id") or sid),
                    model=model,
                ).to_dict()
            )
        if agents:
            host_waves.append({"wave": wave_idx, "parallel": len(agents) > 1, "agents": agents})
    return host_waves


def _normalize_provider_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower().replace("_", "-")


def _caller_is_host(caller: str | None) -> bool:
    normalized = normalize_caller_id(caller)
    return bool(normalized and normalized in HOST_PROVIDER_NAMES)


def _provider_matches_caller(registry: Any, provider: Any, caller: str | None) -> bool:
    matcher = getattr(registry, "_caller_matches_provider", None)
    if callable(matcher):
        return bool(matcher(provider, caller))
    normalized_caller = normalize_caller_id(caller)
    provider_name = getattr(provider, "name", None)
    if not normalized_caller or not isinstance(provider_name, str):
        return False
    return normalized_caller == _normalize_provider_id(provider_name)


def router_only_execution_allowed(
    registry: Any,
    provider: Any,
    caller: str | None,
    tier: str,
) -> bool:
    checker = getattr(registry, "_router_only_execution_allowed", None)
    if callable(checker):
        return bool(checker(provider, caller=caller, tier=tier, caller_allowlists=None))
    return False


def _provider_stub(name: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(name=name, display_name=name)


def would_self_delegate(
    registry: Any,
    *,
    caller: str | None,
    tier: str,
    provider_id: str | None = None,
    caller_allowlists: dict[str, list[str]] | None = None,
    prefer_free: bool = True,
) -> bool:
    if not _caller_is_host(caller):
        return False

    normalized_caller = normalize_caller_id(caller)
    requested_provider = _normalize_provider_id(provider_id)
    if requested_provider:
        if requested_provider in ROUTER_ONLY_PROVIDERS:
            if router_only_execution_allowed(
                registry, _provider_stub(requested_provider), caller, tier
            ):
                return False
            ordered_fn = getattr(registry, "_ordered_execution_candidates", None)
            if callable(ordered_fn):
                providers, _ = ordered_fn(
                    tier,
                    caller=caller,
                    caller_allowlists=caller_allowlists,
                )
                for provider in providers:
                    if _normalize_provider_id(getattr(provider, "name", None)) == requested_provider:
                        return False
            return True
        if normalized_caller and requested_provider == normalized_caller:
            return True
        caller_ids = getattr(registry, "_caller_identifiers", lambda _c: set())(caller)
        provider_ids = getattr(registry, "_provider_identifiers", lambda _p: set())(
            _provider_stub(requested_provider)
        )
        if caller_ids & provider_ids:
            return True
        return False

    ordered_fn = getattr(registry, "_ordered_execution_candidates", None)
    if not callable(ordered_fn):
        return True
    ordered, _excluded = ordered_fn(
        tier,
        caller=caller,
        caller_allowlists=caller_allowlists,
        prefer_free=prefer_free,
    )
    if not ordered:
        return True
    return _provider_matches_caller(registry, ordered[0], caller)


def build_host_native_required_response(
    *,
    config: TGsConfig,
    caller: str | None,
    tier: str,
    prompt: str,
    delegation_targets: list[str],
    target_file: str | None = None,
    compliance_warning: str | None = None,
) -> dict[str, Any]:
    target_files = [target_file] if isinstance(target_file, str) and target_file.strip() else []
    payload: dict[str, Any] = {
        "error": HOST_SPAWN_ERROR,
        "details": "Same-host work must run via host subagent tool, not execute_subtask.",
        "host_spawn": build_host_spawn(
            config=config,
            caller=caller,
            tier=tier,
            prompt=prompt,
            target_files=target_files,
        ).to_dict(),
        "delegation_targets": delegation_targets,
    }
    if compliance_warning:
        payload["compliance_warning"] = compliance_warning
    return payload


def effective_swarm_host_execution_mode(config: TGsConfig, caller: str | None) -> str:
    normalized = normalize_caller_id(caller)
    by_caller = getattr(config, "swarm_host_execution_mode_by_caller", None) or {}
    if normalized and isinstance(by_caller, dict):
        override = by_caller.get(normalized)
        if isinstance(override, str) and override.strip().lower() in {"host_native", "delegate"}:
            return override.strip().lower()
    default_mode = getattr(config, "swarm_host_execution_mode", "host_native")
    if isinstance(default_mode, str) and default_mode.strip().lower() == "delegate":
        return "delegate"
    if _caller_is_host(caller):
        return "host_native"
    return "delegate"

DELEGATION_DISABLED_ERROR = "DelegationDisabled"
HOST_DELEGATION_BLOCKED_ERROR = "HostDelegationBlocked"
DELEGATION_NOT_ALLOWED_ERROR = "DelegationNotAllowed"


def _normalize_delegation_provider_id(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower().replace("_", "-")


def provider_is_host_execution_target(provider_id: str | None) -> bool:
    normalized = _normalize_delegation_provider_id(provider_id)
    return bool(normalized and normalized in HOST_PROVIDER_NAMES)


def validate_execute_subtask_delegation(
    registry: Any,
    config: TGsConfig,
    *,
    provider_id: str | None,
) -> dict[str, Any] | None:
    """Return an error payload when execute_subtask delegation is not permitted."""
    if not getattr(config, "delegation_utilities_enabled", False):
        return {
            "error": DELEGATION_DISABLED_ERROR,
            "details": (
                "Utility delegation is disabled. Host shells execute via host_spawn "
                "(Agent/Task). Set providers.delegation_utilities_enabled: true in "
                "config.yaml to delegate to OpenCode, Aider, or local endpoints only."
            ),
        }

    if provider_id is None:
        return None

    normalized = _normalize_delegation_provider_id(provider_id)
    if normalized is None:
        return None

    allowlist = {
        str(item).strip().lower()
        for item in getattr(config, "delegation_utilities", []) or []
        if isinstance(item, str) and item.strip()
    }
    if provider_is_host_execution_target(normalized) and normalized not in allowlist:
        return {
            "error": HOST_DELEGATION_BLOCKED_ERROR,
            "details": (
                "Host CLIs execute via host_spawn; Threnody does not subprocess to "
                "other host backends (Copilot, Codex, Cursor, Junie). OpenCode is only "
                "allowed when listed in providers.delegation_utilities."
            ),
            "provider_id": normalized,
        }

    matcher = getattr(registry, "_matches_provider", None)
    checker = getattr(registry, "_provider_allowed_as_delegation_target", None)
    if not callable(matcher) or not callable(checker):
        allowlist = {
            str(item).strip().lower()
            for item in getattr(config, "delegation_utilities", []) or []
            if isinstance(item, str) and item.strip()
        }
        if normalized not in allowlist and not normalized.startswith("local-"):
            return {
                "error": DELEGATION_NOT_ALLOWED_ERROR,
                "details": (
                    f"Provider '{normalized}' is not in providers.delegation_utilities. "
                    "Allowed utility targets: OpenCode, Aider, and local loopback endpoints."
                ),
                "provider_id": normalized,
            }
        return None

    for provider in getattr(registry, "available_providers", []) or []:
        if matcher(provider, normalized):
            if checker(provider):
                return None
            reason_fn = getattr(registry, "_delegation_target_exclusion_reason", None)
            reason = reason_fn(provider) if callable(reason_fn) else "not an allowed utility target"
            return {
                "error": DELEGATION_NOT_ALLOWED_ERROR,
                "details": reason,
                "provider_id": normalized,
            }

    return {
        "error": DELEGATION_NOT_ALLOWED_ERROR,
        "details": f"Provider '{normalized}' is not installed or not routable for delegation.",
        "provider_id": normalized,
    }

