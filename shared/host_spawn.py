"""Host-native spawn contract helpers for meta-harness v2."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping

from .config import TGsConfig, normalize_caller_id, normalize_routing_policy_shell_id
from .context import is_within_repo, normalize_target_path
from .discovery import HOST_PROVIDER_NAMES, ROUTER_ONLY_PROVIDERS

HOST_SPAWN_ERROR = "HostNativeRequired"
HOST_EXECUTION_CONTRACT = "spawn_subagents"
# Opt-in alternative to spawn_subagents: emit a Claude Code Dynamic Workflow JS
# script the host launches via the Workflow tool. claude-code only. See
# shared/workflow_emit.py. Requires Claude Code v2.1.154+ (operator opt-in implies it).
WORKFLOW_EXECUTION_CONTRACT = "emit_workflow"
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
    task_id: str | None = None
    run_id: str | None = None

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
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        return payload


def host_tool_for_caller(caller: str | None) -> str:
    normalized = normalize_caller_id(caller)
    if normalized == "claude-code":
        return "Agent"
    return "Task"


def host_native_method_for_tier(tier: str) -> str:
    return "direct_edit" if tier == "low" else "host_task"


def _live_tier_model_for_caller(
    caller: str | None,
    tier: str,
    registry: Any | None = None,
) -> str | None:
    normalized = normalize_caller_id(caller)
    if not normalized or registry is None:
        return None
    provider_list = getattr(registry, "available_providers", None)
    if not isinstance(provider_list, list):
        return None
    for provider in provider_list:
        if getattr(provider, "name", None) != normalized:
            continue
        tier_models = getattr(provider, "tier_models", None)
        if not isinstance(tier_models, dict):
            return None
        candidate = tier_models.get(tier)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        return None
    return None


def host_native_model_for_tier(
    config: TGsConfig,
    caller: str | None,
    tier: str,
    registry: Any | None = None,
) -> str | None:
    if registry is None:
        try:
            from .discovery import get_registry

            registry = get_registry()
        except Exception:
            log.debug("host_native_model_for_tier: registry unavailable", exc_info=True)
            registry = None

    live_model = _live_tier_model_for_caller(caller, tier, registry)
    if live_model:
        return live_model

    if config is None:
        return None

    shell_id = normalize_routing_policy_shell_id(normalize_caller_id(caller))
    if shell_id is None:
        return None
    profile = config.routing_policy.effective_profile(shell_id)
    model = profile.tier_model_mapping.get(tier)
    return model if isinstance(model, str) and model.strip() else None


def workflow_emit_enabled(config: TGsConfig, caller: str | None) -> bool:
    """True when the caller is claude-code and the operator opted into workflow emission.

    Gated on ``routing_policy.shells.claude-code.workflow_emit``. Other host shells
    have no Workflow-tool equivalent, so emission is claude-code only.
    """
    if normalize_caller_id(caller) != "claude-code":
        return False
    if config is None:
        return False
    try:
        profile = config.routing_policy.effective_profile("claude-code")
    except Exception:
        log.debug("workflow_emit_enabled: profile lookup failed", exc_info=True)
        return False
    return bool(getattr(profile, "workflow_emit", False))


def consensus_in_workflow_enabled(config: TGsConfig, caller: str | None) -> bool:
    """True when the operator opted into rendering consensus INTO the workflow script.

    Requires ``workflow_emit`` (the consensus phase lives in the emitted script) and is
    claude-code only. When false, the swarm path runs consensus queens as separate host
    agents (hybrid default).
    """
    if not workflow_emit_enabled(config, caller):
        return False
    try:
        profile = config.routing_policy.effective_profile("claude-code")
    except Exception:
        log.debug("consensus_in_workflow_enabled: profile lookup failed", exc_info=True)
        return False
    return bool(getattr(profile, "consensus_in_workflow", False))


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
    subagent_type: str | None = None,
    read_only: bool = False,
) -> HostSpawnSpec:
    # Review agents use named subagent types only on claude-code; other hosts fall back to tier.
    normalized_caller = normalize_caller_id(caller)
    is_claude_code = normalized_caller == "claude-code"
    resolved_subagent_type = (
        subagent_type
        if subagent_type and is_claude_code
        else subagent_type_for_tier(tier)
    )
    # read_only tasks must never use direct_edit — they read source context only.
    method = "host_task" if read_only else host_native_method_for_tier(tier)
    return HostSpawnSpec(
        tool=host_tool_for_caller(caller),
        method=method,
        model=model or host_native_model_for_tier(config, caller, tier),
        subagent_type=resolved_subagent_type,
        prompt=prompt,
        tier=tier,
        caller=normalized_caller,
        wave_id=wave_id,
        target_files=list(target_files or []),
        id=spawn_id,
    )


def _subtask_target_files(subtask: Mapping[str, Any]) -> list[str]:
    target_file = subtask.get("target_file")
    if isinstance(target_file, str) and target_file.strip():
        return [target_file.strip()]
    return []


def enrich_host_spawn_waves(
    waves: list[dict[str, Any]],
    *,
    force_spawn: bool = True,
) -> list[dict[str, Any]]:
    """Apply host handoff execution contract to wave payloads."""
    if not force_spawn or not waves:
        return waves
    enriched: list[dict[str, Any]] = []
    for wave in waves:
        if not isinstance(wave, dict):
            enriched.append(wave)
            continue
        next_wave = dict(wave)
        next_wave["execution_contract"] = HOST_EXECUTION_CONTRACT
        agents_raw = next_wave.get("agents")
        if isinstance(agents_raw, list):
            next_agents: list[dict[str, Any]] = []
            for agent in agents_raw:
                if not isinstance(agent, dict):
                    next_agents.append(agent)
                    continue
                next_agent = dict(agent)
                next_agent["method"] = "host_task"
                next_agent["spawn_required"] = True
                next_agents.append(next_agent)
            next_wave["agents"] = next_agents
            next_wave.update(_batch_spawn_metadata(next_agents))
        enriched.append(next_wave)
    return enriched


def _batch_spawn_metadata(agents: list[Any]) -> dict[str, Any]:
    """Machine-readable same-wave launch metadata for host-native handoffs."""
    return {
        "parallel_start_required": True,
        "spawn_batch": [
            dict(agent) if isinstance(agent, dict) else agent for agent in agents
        ],
    }


_BARE_FILE_TOKEN = re.compile(r"[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,4}")


def _is_fragment_prompt(text: str, target_basename: str | None = None) -> bool:
    """True when *text* is an incoherent fragment, not an executable prompt.

    Guards against truncated prose slices (e.g. ``"someuser/"``) that the
    lexical heuristic can produce from task text. Keyed on path/identifier shape,
    not length — a terse-but-real description ("auth module") is not a fragment.
    """
    t = (text or "").strip()
    if not t:
        return True
    if target_basename and t.strip("/").lower() == target_basename.strip("/").lower():
        return True
    # Whitespace-free path slice or bare filename token => fragment.
    if not any(ws in t for ws in (" ", "\t", "\n")):
        if "/" in t or "\\" in t:
            return True
        if _BARE_FILE_TOKEN.fullmatch(t):
            return True
        if len(t) < 3:
            return True
    return False


def _target_within_workspace(target: str, root: str) -> bool:
    try:
        resolved = normalize_target_path(target, root)
    except ValueError:
        return False
    return is_within_repo(resolved, root)


def sanitize_plan_for_host(
    plan_dict: dict[str, Any],
    *,
    workspace_root: str | None,
    task: str | None,
    default_tier: str = "medium",
) -> dict[str, Any]:
    """Drop unsafe/incoherent subtasks before host-wave or workflow emission.

    Mutates *plan_dict* in place and returns a sanitization report. Subtask
    ``target_file`` values that escape *workspace_root* (out-of-root, traversal,
    sensitive dirs) are stripped; subtasks whose prompt is a fragment/empty are
    dropped. ``waves`` and ``depends_on`` are repaired to match. If nothing
    survives, the plan collapses to a single coherent agent over the full task.
    """
    report: dict[str, Any] = {
        "dropped_targets": [],
        "dropped_subtasks": [],
        "collapsed_to_single": False,
        "reasons": [],
    }
    subtasks = plan_dict.get("subtasks")
    if not isinstance(subtasks, list):
        return report

    root = str(workspace_root).strip() if workspace_root else ""

    surviving: list[dict[str, Any]] = []
    dropped_ids: set[Any] = set()
    for raw in subtasks:
        if not isinstance(raw, dict):
            continue
        st = dict(raw)
        sid = st.get("id")
        target = st.get("target_file")
        target_basename: str | None = None
        if isinstance(target, str) and target.strip():
            target_basename = PurePosixPath(target.strip().replace("\\", "/")).name
            # read_only subtasks (e.g. review fanout) never write — a target
            # outside the workspace is safe, so skip containment stripping.
            read_only = bool(st.get("read_only"))
            if root and not read_only and not _target_within_workspace(target.strip(), root):
                report.setdefault("dropped_targets", []).append(
                    {"id": sid, "target_file": target}
                )
                report.setdefault("reasons", []).append(
                    f"subtask {sid}: target '{target}' outside workspace root"
                )
                st.pop("target_file", None)
                target = None
        desc = str(st.get("description") or "")
        # Only treat the (stripped) target basename as a fragment signal once the
        # target itself has been removed — a coherent prompt for a valid file is fine.
        if _is_fragment_prompt(desc, None if target else target_basename):
            report.setdefault("dropped_subtasks", []).append(
                {"id": sid, "description": desc[:80]}
            )
            report.setdefault("reasons", []).append(
                f"subtask {sid}: fragment/empty prompt"
            )
            if sid is not None:
                dropped_ids.add(sid)
            continue
        surviving.append(st)

    if dropped_ids:
        for st in surviving:
            deps = st.get("depends_on")
            if isinstance(deps, list):
                st["depends_on"] = [d for d in deps if d not in dropped_ids]

    surviving_ids = {st.get("id") for st in surviving}
    waves = plan_dict.get("waves")
    if isinstance(waves, list):
        new_waves: list[list[Any]] = []
        for wave in waves:
            if not isinstance(wave, list):
                continue
            kept = [sid for sid in wave if sid in surviving_ids]
            if kept:
                new_waves.append(kept)
        plan_dict["waves"] = new_waves
    plan_dict["subtasks"] = surviving

    if not surviving:
        report["collapsed_to_single"] = True
        report.setdefault("reasons", []).append(
            "all subtasks unsafe/incoherent; collapsed to single full-task agent"
        )
        tier = default_tier if default_tier in {"low", "medium", "high"} else "medium"
        full = (str(task).strip() if task else "") or "Complete the requested task."
        plan_dict["subtasks"] = [
            {"id": 1, "description": full, "tier": tier, "depends_on": []}
        ]
        plan_dict["waves"] = [[1]]
        plan_dict["topology"] = "linear"
        plan_dict["strategy"] = "sequential"

    plan_dict["sanitization"] = report
    dropped_targets = report.get("dropped_targets", [])
    dropped_subtasks = report.get("dropped_subtasks", [])
    collapsed = report.get("collapsed_to_single", False)
    if dropped_targets or dropped_subtasks or collapsed:
        log.info(
            "host plan sanitized: %d target(s) dropped, %d subtask(s) dropped, collapsed=%s",
            len(dropped_targets),
            len(dropped_subtasks),
            collapsed,
        )
    return report


def build_host_spawn_waves(
    plan_dict: Mapping[str, Any],
    *,
    config: TGsConfig,
    caller: str | None,
    registry: Any | None = None,
) -> list[dict[str, Any]]:
    subtasks = plan_dict.get("subtasks")
    waves = plan_dict.get("waves")
    if not isinstance(subtasks, list) or not isinstance(waves, list):
        return []

    subtask_by_id: dict[Any, dict[str, Any]] = {}
    for raw in subtasks:
        raw_id = raw.get("id") if isinstance(raw, dict) else None
        if raw_id is not None:
            subtask_by_id[raw_id] = raw

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
                log.warning(
                    "host_spawn_waves: skipping subtask %r with empty prompt "
                    "(should have been handled by sanitize_plan_for_host)",
                    sid,
                )
                continue
            if _caller_is_host(caller):
                model = host_native_model_for_tier(
                    config,
                    caller,
                    tier,
                    registry=registry,
                )
            else:
                raw_model = subtask.get("model")
                model = (
                    str(raw_model).strip()
                    if isinstance(raw_model, str) and str(raw_model).strip()
                    else None
                )
            raw_subagent_type = subtask.get("subagent_type")
            subtask_subagent_type = (
                str(raw_subagent_type).strip()
                if isinstance(raw_subagent_type, str) and str(raw_subagent_type).strip()
                else None
            )
            subtask_read_only = bool(subtask.get("read_only", False))
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
                    subagent_type=subtask_subagent_type,
                    read_only=subtask_read_only,
                ).to_dict()
            )
        if agents:
            host_waves.append({"wave": wave_idx, "parallel": len(agents) > 1, "agents": agents})
    if _caller_is_host(caller) and host_waves:
        return enrich_host_spawn_waves(host_waves)
    return host_waves


def build_consensus_wave(
    *,
    config: TGsConfig,
    caller: str | None,
    task_text: str,
    wave_index: int,
    registry: Any | None = None,
) -> dict[str, Any] | None:
    """Build the host-native consensus wave appended after worker waves.

    Returns ``None`` unless consensus and its host-native variant are enabled and
    the caller is a host shell. Each queen is a *read-only* persona-diverse review
    agent the host spawns via its ``Agent``/``Task`` tool — always on the host
    model. Host-native queens never cross providers (that would require subprocess
    delegation, which the host-native contract forbids); persona diversity is the
    diversity source here.
    """
    if not _caller_is_host(caller):
        return None
    if not getattr(config, "consensus_enabled", False):
        return None
    if not getattr(config, "consensus_host_native_enabled", False):
        return None

    from .consensus import build_queen_prompt, consensus_review_instruction, select_personas

    n_queens = getattr(config, "consensus_queens", 2)
    personas = select_personas(n_queens, config)
    if len(personas) < 2:
        return None
    queen_tier = getattr(config, "consensus_queen_tier", "low")
    review_prompt = consensus_review_instruction(task_text)

    agents: list[dict[str, Any]] = []
    for persona in personas:
        persona_id = persona.get("id") or "queen"
        spec = build_host_spawn(
            config=config,
            caller=caller,
            tier=queen_tier,
            prompt=build_queen_prompt(review_prompt, persona),
            wave_id=f"consensus-wave-{wave_index}",
            spawn_id=f"queen-{persona_id}",
            read_only=True,
        ).to_dict()
        spec["persona"] = persona_id
        spec["wave_kind"] = "consensus"
        spec["spawn_required"] = True
        agents.append(spec)

    wave = {
        "wave": wave_index,
        "wave_kind": "consensus",
        "parallel": True,
        "execution_contract": HOST_EXECUTION_CONTRACT,
        "agents": agents,
        "personas": [p.get("id") for p in personas],
    }
    wave.update(_batch_spawn_metadata(agents))
    return wave


def build_judge_spawn(
    *,
    config: TGsConfig,
    caller: str | None,
    task_text: str,
    judge_prompt: str,
    wave_index: int,
) -> dict[str, Any]:
    """Build the single read-only judge spawn spec for the lazy arbitration round."""
    judge_tier = getattr(config, "consensus_judge_tier", "low")
    spec = build_host_spawn(
        config=config,
        caller=caller,
        tier=judge_tier,
        prompt=judge_prompt,
        wave_id=f"consensus-judge-{wave_index}",
        spawn_id="consensus-judge",
        read_only=True,
    ).to_dict()
    spec["wave_kind"] = "consensus_judge"
    spec["spawn_required"] = True
    return spec


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


def effective_planner_host_execution_mode(config: TGsConfig, caller: str | None) -> str:
    normalized = normalize_caller_id(caller)
    by_caller = getattr(config, "planner_host_execution_mode_by_caller", None) or {}
    if normalized and isinstance(by_caller, dict):
        override = by_caller.get(normalized)
        if isinstance(override, str) and override.strip().lower() in {"host_native", "delegate"}:
            return override.strip().lower()
    default_mode = getattr(config, "planner_host_execution_mode", "host_native")
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
