"""Host-native spawn contract helpers for meta-harness v2."""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Mapping

from .config import (
    SUPPORTED_ROUTING_POLICY_SHELLS,
    TGsConfig,
    normalize_caller_id,
    normalize_routing_policy_shell_id,
)
from .context import is_within_repo, normalize_target_path
from .discovery import HOST_PROVIDER_NAMES, ROUTER_ONLY_PROVIDERS
from .roles import derive_role_from_task, DEFAULT_ROLE

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
    # Where this agent must leave output for its dependents (set only when something
    # actually depends on it), and the artifacts it should read first.
    artifact_path: str | None = None
    upstream: list[dict[str, Any]] = field(default_factory=list)
    # Prompt-independent learning key. Carried through the spawn payload so pattern
    # tracking keys on the *kind* of work rather than the rendered prompt, which
    # changes with prompt-economy settings. Absent → learning falls back to hashing
    # the description, as it always did.
    pattern_hash: str | None = None
    role: str | None = None
    # True for review/diagnosis agents. Emitted so "this agent must not write" is
    # machine-readable rather than only stated in prose the host may not follow —
    # and so the routing guard can tell a review target (named to be READ) apart
    # from a write target instead of issuing a write guard for every review run.
    read_only: bool = False
    workspace: str | None = None
    effort: str | None = None

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
        if self.pattern_hash:
            payload["pattern_hash"] = self.pattern_hash
        if self.artifact_path:
            payload["artifact_path"] = self.artifact_path
        if self.upstream:
            payload["upstream"] = [dict(item) for item in self.upstream]
        if self.role:
            payload["role"] = self.role
        if self.read_only:
            payload["read_only"] = True
        if self.workspace:
            payload["workspace"] = self.workspace
        if self.effort:
            payload["effort"] = self.effort
        return payload

    def to_agy_dict(self) -> dict[str, Any]:
        """Output agy-native format: array of subagent configs."""
        config: dict[str, Any] = {
            "TypeName": self.subagent_type.replace("threnody-", "") if self.subagent_type else self.tier,
            "Role": self.role or "Implementer",
            "Prompt": self.prompt,
            "Workspace": self.workspace or determine_workspace_mode({
                "target_files": self.target_files,
                "read_only": self.method == "host_task" and not self.target_files,
            }),
        }
        if self.model:
            config["Model"] = self.model
        if self.effort:
            config["Effort"] = self.effort
        if self.id:
            config["Id"] = self.id
        return config

    def to_claude_dict(self) -> dict[str, Any]:
        """Translate to Claude Code Agent tool format."""
        return {
            "tool": "Agent",
            "subagent_type": self.subagent_type,
            "prompt": self.prompt,
            "model": self.model,
            "tier": self.tier,
        }

    def to_copilot_dict(self) -> dict[str, Any]:
        """Translate to GitHub Copilot Task format."""
        return {
            "tool": "Task",
            "subagent_type": self.subagent_type,
            "prompt": self.prompt,
            "model": self.model,
            "tier": self.tier,
        }

    def to_codex_dict(self) -> dict[str, Any]:
        """Translate to OpenAI Codex Task format."""
        return self.to_copilot_dict()

    def to_cursor_dict(self) -> dict[str, Any]:
        """Translate to Cursor Task format."""
        return self.to_copilot_dict()

    def to_junie_dict(self) -> dict[str, Any]:
        """Translate to JetBrains Junie Task format."""
        return self.to_copilot_dict()

    def to_opencode_dict(self) -> dict[str, Any]:
        """Translate to OpenCode Task format."""
        return self.to_copilot_dict()


def host_tool_for_caller(caller: str | None) -> str:
    normalized = normalize_caller_id(caller)
    if normalized == "claude-code":
        return "Agent"
    if normalized == "antigravity":
        return "invoke_subagent"
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


def determine_workspace_mode(task: Mapping[str, Any]) -> str:
    """Auto-pick workspace isolation mode based on task characteristics.
    
    Returns:
        "branch" — isolated git branch (safest, for multi-file writes)
        "share" — light worktree (middle ground, for read-only reviews)
        "inherit" — shared workspace (fastest, for simple edits)
    """
    target_files = list(task.get("target_files") or [])
    read_only = bool(task.get("read_only", False))
    
    if read_only:
        return "share"
    if len(target_files) > 3:
        return "branch"
    if any("*" in f or "?" in f for f in target_files):
        return "branch"
    if len(target_files) == 0:
        return "inherit"
    return "inherit"


def named_subagent_types_supported(config: TGsConfig, caller: str | None) -> bool:
    """True when *caller* resolves a named ``subagent_type`` to a real definition.

    Capability-driven rather than hardcoded to claude-code: ``install.sh`` exports
    reviewer definitions to every shell in ``NAMED_SUBAGENT_TYPE_SHELLS`` via
    ``agent_export``, so those shells can honor a named type too. A shell without a
    definition directory falls back to the tier-derived type, which is what every
    shell did before capabilities existed.
    """
    if config is None:
        return False
    shell_id = normalize_routing_policy_shell_id(normalize_caller_id(caller))
    # An unrecognized shell must not inherit a capability by way of
    # effective_profile()'s advisory fallback — an unknown host has no exported
    # definition to resolve the named type against.
    if shell_id is None or shell_id not in SUPPORTED_ROUTING_POLICY_SHELLS:
        return False
    try:
        profile = config.routing_policy.effective_profile(shell_id)
    except Exception:
        log.debug("named_subagent_types_supported: profile lookup failed", exc_info=True)
        return False
    return bool(getattr(profile, "named_subagent_types", False))


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
    pattern_hash: str | None = None,
    artifact_path: str | None = None,
    upstream: list[dict[str, Any]] | None = None,
    role: str | None = None,
) -> HostSpawnSpec:
    # Review agents use named subagent types on shells that resolve them to an
    # exported definition; every other host falls back to the tier-derived type.
    normalized_caller = normalize_caller_id(caller)
    resolved_subagent_type = (
        subagent_type
        if subagent_type and named_subagent_types_supported(config, caller)
        else subagent_type_for_tier(tier)
    )
    # read_only tasks must never use direct_edit — they read source context only.
    method = "host_task" if read_only else host_native_method_for_tier(tier)

    # Smart workspace picker
    workspace = determine_workspace_mode({
        "target_files": list(target_files or []),
        "read_only": read_only,
    })

    # Map effort from tier (agy-specific)
    effort_map = {"low": "low", "medium": "high", "high": "high"}
    effort = effort_map.get(tier, "medium")

    resolved_role = role or derive_role_from_task(prompt)
    enriched_prompt = prompt
    if resolved_role and not prompt.startswith("["):
        enriched_prompt = f"[{resolved_role}] {prompt}"

    return HostSpawnSpec(
        tool=host_tool_for_caller(caller),
        method=method,
        model=model or host_native_model_for_tier(config, caller, tier),
        subagent_type=resolved_subagent_type,
        prompt=enriched_prompt,
        tier=tier,
        caller=normalized_caller,
        wave_id=wave_id,
        target_files=list(target_files or []),
        id=spawn_id,
        workspace=workspace,
        effort=effort,
        pattern_hash=pattern_hash,
        artifact_path=artifact_path,
        upstream=list(upstream or []),
        role=resolved_role,
        read_only=bool(read_only),
    )


def _subtask_target_files(subtask: Mapping[str, Any]) -> list[str]:
    """Authoritative owned-file list for a subtask.

    Prefers the plural ``target_files`` list (coupled groups own several files);
    falls back to the scalar ``target_file``. Deduped, order-preserving, so a
    coupled subtask's full ownership is honored downstream instead of dropped.
    """
    result: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            cleaned = value.strip()
            if cleaned.lower() not in seen:
                seen.add(cleaned.lower())
                result.append(cleaned)

    plural = subtask.get("target_files")
    if isinstance(plural, (list, tuple)):
        for item in plural:
            _add(item)
    _add(subtask.get("target_file"))
    return result


def _subtask_id_key(value: Any) -> tuple[str, str]:
    """Build a hashable key that preserves the ID's runtime type."""
    return type(value).__name__, repr(value)


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
    """Machine-readable same-wave launch metadata for host-native handoffs.

    ``spawn_batch`` used to carry a verbatim copy of ``agents``, which was
    exactly half of ``host_spawn_waves`` — itself ~93% of the wire payload — for
    a field the execution note told the host to read *instead of* ``agents``, not
    in addition to. A 22-agent review handoff came to 55 KB and overflowed the
    host's context before the first agent spawned. The launch semantics live
    entirely in the flag; the agent list has always been ``agents``.
    """
    return {"parallel_start_required": True}


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


def review_cell_label(subtask: Mapping[str, Any]) -> str:
    """Return the ``"<path>:<dimension>"`` label for a review cell, else ``""``.

    This is the same label ``review_fanout`` uses for ``coverage.dropped_cells``
    and ``coverage.skipped_prior_review``, so a removal recorded anywhere in the
    pipeline can be reconciled against the expected set by string identity.
    """
    dim = subtask.get("review_dimension")
    path = subtask.get("target_file")
    if not isinstance(dim, str) or not dim.strip():
        return ""
    if not isinstance(path, str) or not path.strip():
        return ""
    return f"{path.strip()}:{dim.strip()}"


def sanitize_plan_for_host(
    plan_dict: dict[str, Any],
    *,
    workspace_root: str | None,
    task: str | None,
    default_tier: str = "medium",
    allow_external_read_only: bool = True,
    collapse_unsafe_to_single: bool = True,
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
        "dedup": [],
        "collapsed_to_single": False,
        "reasons": [],
    }
    subtasks = plan_dict.get("subtasks")
    if not isinstance(subtasks, list):
        return report

    root = str(workspace_root).strip() if workspace_root else ""

    surviving: list[dict[str, Any]] = []
    dropped_ids: set[tuple[str, str]] = set()
    stable_id = _subtask_id_key
    claimed: set[str] = set()
    for raw in subtasks:
        if not isinstance(raw, dict):
            continue
        st = dict(raw)
        sid = st.get("id")
        # Every removal below records the review cell it destroyed, so the coverage
        # contract can name it. Without this the report is a list of subtask ids
        # whose subtasks no longer exist, and a caller cannot tell which
        # (file x dimension) it lost.
        cell_label = review_cell_label(st)
        target = st.get("target_file")
        target_files = st.get("target_files")
        target_basename: str | None = None
        read_only = bool(st.get("read_only"))
        external_targets: list[str] = []
        if isinstance(target, str) and target.strip():
            if root and not _target_within_workspace(target.strip(), root):
                external_targets.append(target.strip())
        if isinstance(target_files, list):
            for candidate in target_files:
                if (
                    isinstance(candidate, str)
                    and candidate.strip()
                    and root
                    and not _target_within_workspace(candidate.strip(), root)
                ):
                    external_targets.append(candidate.strip())
        if external_targets and not allow_external_read_only:
            report.setdefault("dropped_subtasks", []).append(
                {"id": sid, "target_files": external_targets, "cell": cell_label}
            )
            report.setdefault("reasons", []).append(
                f"subtask {sid}: target outside workspace root"
            )
            if sid is not None:
                dropped_ids.add(stable_id(sid))
            continue
        if (
            root
            and isinstance(target_files, list)
            and (not read_only or not allow_external_read_only)
        ):
            safe_target_files = [
                candidate
                for candidate in target_files
                if isinstance(candidate, str)
                and _target_within_workspace(candidate.strip(), root)
            ]
            if safe_target_files:
                st["target_files"] = safe_target_files
            else:
                st.pop("target_files", None)
        if isinstance(target, str) and target.strip():
            target_basename = PurePosixPath(target.strip().replace("\\", "/")).name
            # read_only subtasks (e.g. review fanout) never write — a target
            # outside the workspace is safe, so skip containment stripping.
            if (
                root
                and (not read_only or not allow_external_read_only)
                and not _target_within_workspace(target.strip(), root)
            ):
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
                {"id": sid, "description": desc[:80], "cell": cell_label}
            )
            report.setdefault("reasons", []).append(
                f"subtask {sid}: fragment/empty prompt"
            )
            if sid is not None:
                dropped_ids.add(stable_id(sid))
            continue

        # Disjoint ownership (#2): every file is owned by exactly one subtask.
        # Trim already-claimed paths; drop a subtask whose ownership is fully
        # claimed by an earlier one (prevents two agents editing the same file).
        #
        # read_only subtasks are exempt for the same reason they are exempt from
        # containment stripping above: they never write, so they cannot conflict.
        # Review fanout gives every (file x dimension) cell the same target_file,
        # so applying ownership here would silently collapse an N-dimension review
        # to its first dimension per file. They must also not *claim* a path — a
        # reviewer holding ownership would evict the writer that follows it.
        owned = [] if read_only else _subtask_target_files(st)
        if owned:
            fresh = [p for p in owned if p.lower() not in claimed]
            removed = [p for p in owned if p.lower() in claimed]
            if not fresh:
                report.setdefault("dedup", []).append(
                    {"id": sid, "removed": removed, "dropped": True, "cell": cell_label}
                )
                report.setdefault("reasons", []).append(
                    f"subtask {sid}: ownership already claimed; dropped duplicate"
                )
                if sid is not None:
                    dropped_ids.add(stable_id(sid))
                continue
            if removed:
                report.setdefault("dedup", []).append(
                    {"id": sid, "removed": removed, "dropped": False}
                )
                report.setdefault("reasons", []).append(
                    f"subtask {sid}: removed already-claimed target(s) {removed}"
                )
                if isinstance(st.get("target_files"), (list, tuple)):
                    st["target_files"] = fresh
                tf = st.get("target_file")
                if isinstance(tf, str) and tf.strip().lower() in {r.lower() for r in removed}:
                    st["target_file"] = fresh[0]
            for p in fresh:
                claimed.add(p.lower())

        surviving.append(st)

    if dropped_ids:
        for st in surviving:
            deps = st.get("depends_on")
            if isinstance(deps, list):
                st["depends_on"] = [
                    d for d in deps if stable_id(d) not in dropped_ids
                ]

    surviving_ids = {stable_id(st.get("id")): st.get("id") for st in surviving}
    waves = plan_dict.get("waves")
    if isinstance(waves, list):
        new_waves: list[list[Any]] = []
        for wave in waves:
            if not isinstance(wave, list):
                continue
            kept = [
                surviving_ids[stable_id(sid)]
                for sid in wave
                if stable_id(sid) in surviving_ids
            ]
            if kept:
                new_waves.append(kept)
        plan_dict["waves"] = new_waves
    plan_dict["subtasks"] = surviving

    if not surviving and collapse_unsafe_to_single:
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
    deduped = report.get("dedup", [])
    collapsed = report.get("collapsed_to_single", False)
    if dropped_targets or dropped_subtasks or deduped or collapsed:
        log.info(
            "host plan sanitized: %d target(s) dropped, %d subtask(s) dropped, "
            "%d ownership dedup(s), collapsed=%s",
            len(dropped_targets),
            len(dropped_subtasks),
            len(deduped),
            collapsed,
        )
    return report


def _findings_protocol_block(run_id: str, spawn_id: str, dimension: str) -> str:
    """Instruction to write findings to a file and return only counts.

    Two costs disappear: the agent's findings stop being copied into the parent
    conversation (where every later turn re-sends them), and the merge step can read
    them directly instead of a synthesis agent being handed every prior agent's
    excerpt as context.
    """
    from .findings_merge import findings_path

    path = findings_path(run_id, spawn_id)
    return (
        f"Write your findings to {path} — one finding per line, in exactly the "
        "format given above, creating parent directories if needed. Write the file "
        "even when you find nothing (leave it empty).\n"
        "Then reply with ONLY this one-line summary and nothing else:\n"
        f"dim={dimension} total=<number of findings> high=<number of high or critical>\n"
        "Do not repeat the findings in your reply — they are read from the file."
    )


def _adjudication_block(run_id: str) -> str:
    """Tell the synthesis agent to also persist its report where finalize can read it.

    The reply stays exactly as before — the report inline — because the host surfaces
    that to the operator. This is a side channel: the file is what lets
    ``host_learning`` attribute a rejected finding back to the model that reported
    it. If the agent skips it, every reviewer in the run is simply scored as
    unadjudicated, which is the pre-existing behaviour.
    """
    from .findings_merge import synthesis_report_path

    path = synthesis_report_path(run_id)
    return (
        f"Also write this same report — both the Findings and Dropped sections, "
        f"verbatim — to {path}, creating parent directories if needed. "
        "It is read back to record which reviewer produced noise."
    )


# Instruction files each host actually loads into every subagent. Deliberately
# per-host rather than the union of all known instruction files: reporting a total no
# single run pays would overstate the tax and cost the number its credibility.
# ``AGENTS.md`` is the cross-tool convention and is read by all of them.
_HOST_INSTRUCTION_FILES: dict[str, tuple[str, ...]] = {
    "claude-code": ("CLAUDE.md", "AGENTS.md"),
    "github-copilot-cli": (
        ".github/copilot-instructions.md",
        "copilot-instructions.md",
        "AGENTS.md",
    ),
    "codex": ("AGENTS.md",),
    "cursor": (".cursorrules", "AGENTS.md"),
    "opencode": ("AGENTS.md",),
    "junie": ("AGENTS.md",),
}


def instruction_tax_report(
    config: TGsConfig,
    *,
    workspace_root: str | None,
    agent_count: int,
    caller: str | None = None,
) -> dict[str, Any] | None:
    """Report the per-agent instruction-file tax when it dominates a fan-out.

    Every host reloads the workspace's own instruction files (CLAUDE.md, AGENTS.md,
    …) into *each* subagent, so their combined size is multiplied by the agent count
    before any work happens. Threnody cannot trim them — they are the operator's
    files, and shrinking them may be exactly wrong — so the honest move is to state
    the number.

    Returns ``None`` when disabled, when the caller's instruction files are unknown or
    absent, or when the total is under the configured threshold.
    """
    if config is None or agent_count <= 0:
        return None
    economy = getattr(config, "prompt_economy", None)
    if economy is None or not getattr(economy, "instruction_tax_warning", False):
        return None
    if not workspace_root:
        return None
    shell_id = normalize_routing_policy_shell_id(normalize_caller_id(caller))
    candidates = _HOST_INSTRUCTION_FILES.get(shell_id or "")
    if not candidates:
        # Unknown host: which files it loads is a guess, and an inflated number is
        # worse than no number.
        return None
    try:
        from pathlib import Path

        root = Path(workspace_root)
        if not root.is_dir():
            return None
        files: list[dict[str, Any]] = []
        per_agent_bytes = 0
        for rel in candidates:
            candidate = root / rel
            try:
                if not candidate.is_file():
                    continue
                size = candidate.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            per_agent_bytes += size
            files.append({"path": rel, "bytes": size})
        if not files:
            return None
        threshold = int(getattr(economy, "instruction_tax_warn_bytes", 200_000) or 200_000)
        total_bytes = per_agent_bytes * agent_count
        if total_bytes < threshold:
            return None
        files.sort(key=lambda item: int(item["bytes"]), reverse=True)
        return {
            "shell": shell_id,
            "per_agent_bytes": per_agent_bytes,
            "agent_count": agent_count,
            "total_bytes": total_bytes,
            # ~4 bytes/token is the usual rough ratio for prose; stated as approximate
            # because the real number depends on the host's tokenizer.
            "approx_total_tokens": total_bytes // 4,
            "files": files,
            "details": (
                f"This workspace's instruction files total {per_agent_bytes:,} bytes and are "
                f"reloaded into each of {agent_count} agents (~{total_bytes // 4:,} tokens "
                "per run before any work). Threnody cannot trim them; shortening the "
                "largest file is the single biggest per-agent saving available."
            ),
        }
    except Exception:
        log.debug("instruction_tax_report failed", exc_info=True)
        return None


def repo_context_prefix(
    config: TGsConfig, *, workspace_root: str | None, task: str | None
) -> str:
    """The repo's learned beliefs + style, as a prefix for write-path prompts.

    Beliefs and the style profile already existed but only reached the subprocess path
    via ``context.enrich_subtask``, so every host-native agent rediscovered the repo's
    conventions from scratch — once per agent, every run. Bounded by
    ``BeliefsConfig.max_chars`` plus one line of style; empty on a fresh repo.

    Applied here rather than in the plan builder on purpose: plans are cached by task
    text, and baking repo-specific context into a cached plan would leak it across
    runs and workspaces.
    """
    if config is None or not workspace_root:
        return ""
    economy = getattr(config, "prompt_economy", None)
    if economy is None or not getattr(economy, "inject_beliefs_on_host", False):
        return ""
    try:
        from .agents import _get_agent_db
        from .context import build_repo_context_block

        try:
            db = _get_agent_db()
        except Exception:
            db = None
        return build_repo_context_block(
            workspace_root, query=task or "", db=db
        ).strip()
    except Exception:
        log.debug("host_spawn: repo context prefix failed", exc_info=True)
        return ""


def _spawn_id_for_subtask(subtask: Mapping[str, Any], fallback: Any) -> str:
    if subtask.get("id") is not None:
        return str(subtask["id"])
    if subtask.get("stable_id") is not None:
        return str(subtask["stable_id"])
    return str(fallback)


def _upstream_forwarding_enabled(config: TGsConfig) -> bool:
    host_native = getattr(config, "host_native", None) if config is not None else None
    return bool(getattr(host_native, "forward_upstream_results", False))


def _artifact_write_block(path: str) -> str:
    """Instruction for an agent whose output other agents depend on."""
    return (
        f"Other agents in this run depend on your output. Write it to {path} "
        "(create parent directories if needed) as well as replying normally, so they "
        "can read it directly instead of re-deriving your analysis."
    )


def _artifact_read_block(upstream: list[dict[str, Any]]) -> str:
    """Instruction for an agent whose dependencies left artifacts."""
    listed = "\n".join(
        f"- {item.get('id')}: {item.get('artifact_path')}" for item in upstream
    )
    return (
        "Read these upstream results before starting — they are the output of the "
        "agents this task depends on, and following them is cheaper and more accurate "
        "than re-deriving their conclusions:\n"
        f"{listed}\n"
        "If an artifact is missing or contradicts what you find in the code, say so "
        "in your output rather than silently improvising."
    )


def _materialize_replayed_findings(run_id: str, raw: Any) -> None:
    """Write prior-review findings into the run's findings dir.

    Cells served from prior-review memory have findings but never spawn an agent. The
    in-process merge reads only findings files, so without this a fully-cached review
    run would produce an empty report while the stored findings sat unused.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return
    try:
        from .findings_merge import REPLAY_SOURCE, Finding, write_findings

        records = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            summary = str(item.get("summary") or "").strip()
            path = str(item.get("path") or "").strip()
            if not summary or not path:
                continue
            try:
                line = int(item.get("line") or 0)
            except (TypeError, ValueError):
                line = 0
            records.append(
                Finding(
                    dimension=str(item.get("dimension") or "").strip().lower(),
                    category=str(item.get("category") or "").strip().lower(),
                    severity=str(item.get("severity") or "").strip().lower() or "medium",
                    path=path,
                    line=line,
                    description=summary,
                    source=REPLAY_SOURCE,
                )
            )
        if records:
            write_findings(run_id, REPLAY_SOURCE, records)
    except Exception:
        log.debug(
            "host_spawn: replayed findings materialization failed for %s",
            run_id,
            exc_info=True,
        )


def build_host_spawn_waves(
    plan_dict: Mapping[str, Any],
    *,
    config: TGsConfig,
    caller: str | None,
    registry: Any | None = None,
    run_id: str | None = None,
    workspace_root: str | None = None,
    task: str | None = None,
) -> list[dict[str, Any]]:
    subtasks = plan_dict.get("subtasks")
    waves = plan_dict.get("waves")
    if not isinstance(subtasks, list) or not isinstance(waves, list):
        return []

    subtask_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in subtasks:
        raw_id = raw.get("id") if isinstance(raw, dict) else None
        if raw_id is not None:
            subtask_by_id[_subtask_id_key(raw_id)] = raw

    # Review cells always write their findings to a file, in both synthesis modes.
    #
    # This used to be python-mode only, on the reasoning that an LLM synthesis
    # agent reads the replies. But the synthesis agent already depends on those
    # cells, so it is handed their file paths and can read them directly — and
    # gating on the mode meant the parsed, categorised findings existed only for
    # narrow reviews (python mode is chosen for <=6 cells / <=2 files). Those
    # categories are what `model_quality.record_static_recall_score` grades a
    # reviewer against, so the one objective review signal was unavailable for
    # exactly the broad reviews where it matters most, and the replies were being
    # re-sent through the conversation on top.
    findings_protocol = True
    if findings_protocol and run_id:
        _materialize_replayed_findings(run_id, plan_dict.get("replayed_findings"))

    # Dependency-result forwarding: work out which subtasks are depended upon (they
    # must leave an artifact) and, per subtask, where its dependencies left theirs.
    forward_upstream = bool(run_id) and _upstream_forwarding_enabled(config)
    # Resolved once per run: the text is identical for every agent, which is both the
    # point (shared cacheable prefix) and the reason not to rebuild it per subtask.
    repo_prefix = repo_context_prefix(
        config, workspace_root=workspace_root, task=task
    )
    depended_upon: set[tuple[str, str]] = set()
    if forward_upstream:
        for raw in subtasks:
            if not isinstance(raw, dict):
                continue
            for dep in raw.get("depends_on") or []:
                depended_upon.add(_subtask_id_key(dep))

    host_waves: list[dict[str, Any]] = []
    for wave_idx, wave_ids in enumerate(waves, start=1):
        if not isinstance(wave_ids, list):
            continue
        agents: list[dict[str, Any]] = []
        for sid in wave_ids:
            subtask = subtask_by_id.get(_subtask_id_key(sid))
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
            resolved_spawn_id = _spawn_id_for_subtask(subtask, sid)

            # Read-only agents are deliberately excluded: a reviewer primed with the
            # repo's prior beliefs is a biased reviewer, and its findings feed
            # review_learning — so contaminating them corrupts the signal, not just
            # the review.
            if repo_prefix and not subtask_read_only:
                prompt = f"{repo_prefix}\n\n{prompt}"

            artifact_path_str: str | None = None
            upstream_specs: list[dict[str, Any]] = []
            if forward_upstream:
                try:
                    from .run_log import artifact_path as _artifact_path

                    def _output_path(st: dict[str, Any], spawn: str) -> str:
                        """Where this agent leaves its output.

                        A review cell writes to its findings file and nowhere
                        else. Giving it a separate artifact path too would ask one
                        read-only agent for the same content twice, in two
                        formats, and leave the merge reading a different file than
                        the synthesis agent.
                        """
                        if str(st.get("review_dimension") or "").strip():
                            from .findings_merge import findings_path

                            return str(findings_path(run_id or "", spawn))
                        return str(_artifact_path(run_id or "", spawn))

                    is_review_cell = bool(
                        str(subtask.get("review_dimension") or "").strip()
                    )
                    if _subtask_id_key(sid) in depended_upon:
                        artifact_path_str = _output_path(subtask, resolved_spawn_id)
                        # The findings-protocol block below already tells a review
                        # cell where to write, in the format the merge parses.
                        if not is_review_cell:
                            prompt = (
                                prompt + "\n\n" + _artifact_write_block(artifact_path_str)
                            )
                    for dep in subtask.get("depends_on") or []:
                        dep_subtask = subtask_by_id.get(_subtask_id_key(dep))
                        if not isinstance(dep_subtask, dict):
                            continue
                        dep_spawn_id = _spawn_id_for_subtask(dep_subtask, dep)
                        upstream_specs.append(
                            {
                                "id": dep_spawn_id,
                                "artifact_path": _output_path(dep_subtask, dep_spawn_id),
                            }
                        )
                    if upstream_specs:
                        prompt = prompt + "\n\n" + _artifact_read_block(upstream_specs)
                except Exception:
                    log.debug(
                        "host_spawn_waves: upstream forwarding failed for %r",
                        sid,
                        exc_info=True,
                    )
            # Findings-file protocol: only for review cells, only when a run id is
            # known (it names the file), and only when the merge will actually read
            # those files. With synthesis_mode=llm the synthesis agent reads the
            # agents' replies, so redirecting them to disk would starve it.
            review_dimension = str(subtask.get("review_dimension") or "").strip()
            if review_dimension and run_id and findings_protocol:
                try:
                    prompt = (
                        prompt
                        + "\n\n"
                        + _findings_protocol_block(
                            run_id, resolved_spawn_id, review_dimension
                        )
                    )
                except Exception:
                    log.debug(
                        "host_spawn_waves: findings protocol injection failed",
                        exc_info=True,
                    )
            elif subtask.get("review_synthesis") and run_id:
                try:
                    prompt = prompt + "\n\n" + _adjudication_block(run_id)
                except Exception:
                    log.debug(
                        "host_spawn_waves: adjudication block injection failed",
                        exc_info=True,
                    )
            raw_role = subtask.get("role")
            subtask_role = (
                str(raw_role).strip()
                if isinstance(raw_role, str) and str(raw_role).strip()
                else None
            )
            agents.append(
                build_host_spawn(
                    config=config,
                    caller=caller,
                    tier=tier,
                    prompt=prompt,
                    wave_id=f"wave-{wave_idx}",
                    target_files=_subtask_target_files(subtask),
                    spawn_id=resolved_spawn_id,
                    model=model,
                    subagent_type=subtask_subagent_type,
                    read_only=subtask_read_only,
                    pattern_hash=(
                        str(subtask.get("pattern_hash")).strip()
                        if subtask.get("pattern_hash")
                        else None
                    ),
                    artifact_path=artifact_path_str,
                    upstream=upstream_specs,
                    role=subtask_role,
                ).to_dict()
            )
        if agents:
            host_waves.append({"wave": wave_idx, "parallel": len(agents) > 1, "agents": agents})
    if _caller_is_host(caller) and host_waves:
        return enrich_host_spawn_waves(host_waves)
    return host_waves


def build_plan_summary(plan_dict: Mapping[str, Any]) -> dict[str, Any]:
    """Build human-readable plan summary with role counts, targets, cost estimate."""
    subtasks = plan_dict.get("subtasks")
    waves = plan_dict.get("waves")
    if not isinstance(subtasks, list) or not isinstance(waves, list):
        return {}

    role_counts: dict[str, int] = {}
    target_files: set[str] = set()
    tier_counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0}

    for st in subtasks:
        if not isinstance(st, dict):
            continue
        role = st.get("role") or derive_role_from_task(str(st.get("description", "")))
        role_counts[role] = role_counts.get(role, 0) + 1
        tier = str(st.get("tier", "medium"))
        if tier in tier_counts:
            tier_counts[tier] += 1
        tfs = st.get("target_files")
        if isinstance(tfs, list):
            for tf in tfs:
                if isinstance(tf, str) and tf.strip():
                    target_files.add(tf.strip())
        else:
            tf = st.get("target_file")
            if isinstance(tf, str) and tf.strip():
                target_files.add(tf.strip())

    tier_cost_est = {"low": 0.005, "medium": 0.02, "high": 0.05}
    estimated_cost = sum(
        tier_counts.get(t, 0) * tier_cost_est.get(t, 0.02) for t in tier_counts
    )

    sorted_targets = sorted(target_files)[:10]
    targets_str = ", ".join(sorted_targets)
    if len(target_files) > 10:
        targets_str += f" (+{len(target_files) - 10} more)"

    role_parts = []
    for role, count in sorted(role_counts.items(), key=lambda x: -x[1]):
        role_parts.append(f"{role}\u00d7{count}")
    roles_str = ", ".join(role_parts) if role_parts else "Worker"

    n_tasks = len(subtasks)
    n_waves = len([w for w in waves if isinstance(w, list)])

    text = (
        f"{n_tasks} tasks / {n_waves} waves / {roles_str} "
        f"/ est ${estimated_cost:.2f} / targets: {targets_str or '(none)'}"
    )

    return {
        "text": text,
        "role_counts": role_counts,
        "target_files": sorted(target_files),
        "estimated_cost_usd": round(estimated_cost, 4),
        "n_tasks": n_tasks,
        "n_waves": n_waves,
    }


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
