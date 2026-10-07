"""Host-native spawn contract helpers for meta-harness v2."""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .config import (
    SUPPORTED_ROUTING_POLICY_SHELLS,
    TGsConfig,
    normalize_caller_id,
    normalize_routing_policy_shell_id,
)
from .context import is_within_repo, normalize_target_path
from .discovery import HOST_PROVIDER_NAMES, ROUTER_ONLY_PROVIDERS
from .effort_support import default_routed_effort, host_native_effort_mode
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
    # APPLIED reasoning effort: set only when the chosen agent definition actually
    # pins it (the host has no per-call effort parameter) — see resolve_spawn_type.
    # Learning attributes the spawn to this, so it must never claim an effort that
    # was not pinned. `requested_effort` is what routing wanted, always carried.
    effort: str | None = None
    requested_effort: str | None = None
    # Why ``effort`` is what it is: which resolver rule chose the type and — when a
    # requested effort was not applied — why not. Without these a missing
    # ``effort`` reads the same whether the variant was never generated, was
    # generated mid-session, or the definition pins its own.
    effort_source: str | None = None
    effort_unapplied_reason: str | None = None
    # ``<base>-<effort>`` definition a caller may generate so the *next* session
    # can pin this effort. It cannot help the current one: definitions are frozen
    # when a session starts.
    variant_to_create: str | None = None
    # The named type the plan asked for, when the spawned type differs from it (an
    # effort variant, or a tier fallback for a definition that is not installed).
    # Lets learning still classify a review agent by its dimension.
    base_subagent_type: str | None = None

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
        if self.effort:
            payload["effort"] = self.effort
        if self.requested_effort:
            payload["requested_effort"] = self.requested_effort
        if self.effort_source:
            payload["effort_source"] = self.effort_source
        # Omitted when it only repeats effort_source: a wide review emits this per agent,
        # and the manifest's per-agent byte budget is what keeps it readable in one chunk.
        if self.effort_unapplied_reason and self.effort_unapplied_reason != self.effort_source:
            payload["effort_unapplied_reason"] = self.effort_unapplied_reason
        if self.variant_to_create:
            payload["variant_to_create"] = self.variant_to_create
        if self.base_subagent_type and self.base_subagent_type != self.subagent_type:
            payload["base_subagent_type"] = self.base_subagent_type
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


EFFORT_LEVELS = ("low", "medium", "high")


def normalize_effort(value: Any) -> str | None:
    """Return a valid effort level (low|medium|high) or None."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned if cleaned in EFFORT_LEVELS else None


def claude_agents_dir() -> Path:
    """Claude Code user agent-definition directory (module function so tests patch it)."""
    return Path.home() / ".claude" / "agents"


def effort_variant_subagent_type(tier: str, effort: str | None) -> str | None:
    """``threnody-<tier>-<effort>`` when both are valid, else None."""
    norm = normalize_effort(effort)
    if tier not in {"low", "medium", "high"} or norm is None:
        return None
    return f"threnody-{tier}-{norm}"


def codex_agents_dir() -> Path:
    """Codex custom-agent directory (module function so tests patch it)."""
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "agents"


def _effort_definition_path(
    caller: str | None, name: str, agents_dir: Path | None = None
) -> Path | None:
    mode = host_native_effort_mode(caller)
    if mode == "frontmatter":
        return (agents_dir or claude_agents_dir()) / f"{name}.md"
    if mode == "codex_toml":
        return codex_agents_dir() / f"{name}.toml"
    return None


def tier_subagent_type(caller: str | None, tier: str, effort: str | None = None) -> str:
    """Subagent type for a tier: the installed effort variant, else ``threnody-<tier>``."""
    return resolve_spawn_type(caller=caller, base=None, tier=tier, effort=effort).subagent_type


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


# ---------------------------------------------------------------------------
# Spawn-type resolution: which definition carries the routed effort
# ---------------------------------------------------------------------------

# Claude Code's own agent types. They have no definition file and take no
# effort at all, so a routed effort can never be pinned on them — and they must
# never be mistaken for an "unknown" type and replaced by a tier fallback.
# ``generalPurpose`` is what ``subagent_type_for_tier`` returns for a non-tier.
BUILTIN_SUBAGENT_TYPES = frozenset(
    {
        "explore",
        "plan",
        "general-purpose",
        "claude",
        "claude-code-guide",
        "statusline-setup",
        "generalpurpose",
    }
)

_EFFORT_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<effort>low|medium|high)$")
_TIER_BASE_RE = re.compile(r"^threnody-(?P<tier>low|medium|high)$")
# An agent name becomes a file name below, so anything that could walk out of the
# agents directory is treated as an unknown type rather than looked up.
# ``plugin:name`` is Claude Code's namespaced form for plugin-shipped agents.
_SAFE_AGENT_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}(?::[A-Za-z0-9][A-Za-z0-9_.-]{0,127})?$"
)

# (path) -> (mtime_ns, size, parsed frontmatter). Definitions are read on every
# spawn of a fan-out; the cache key includes mtime so an edited file is re-read.
_FRONTMATTER_CACHE: dict[str, tuple[int, int, dict[str, str]]] = {}
_FRONTMATTER_CACHE_MAX = 256


def read_definition_frontmatter(path: Path) -> dict[str, str]:
    """``key: value`` pairs of the first ``---`` block of *path*; ``{}`` if none.

    Deliberately not a YAML parser: agent frontmatter is flat ``key: value``
    lines, and this runs on files a user may have hand-written, so it must never
    raise or execute anything. Bounded to the first 200 lines.
    """
    try:
        st = path.stat()
    except OSError:
        return {}
    key = str(path)
    cached = _FRONTMATTER_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return dict(cached[2])
    meta: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            if first.strip() == "---":
                for _ in range(200):
                    line = fh.readline()
                    if not line or line.strip() == "---":
                        break
                    name, sep, value = line.partition(":")
                    if sep and name.strip() and not name.startswith((" ", "\t", "#")):
                        meta[name.strip().lower()] = value.strip().strip("'\"")
    except OSError:
        log.debug("frontmatter read failed for %s", path, exc_info=True)
        return {}
    if len(_FRONTMATTER_CACHE) >= _FRONTMATTER_CACHE_MAX:
        _FRONTMATTER_CACHE.clear()
    _FRONTMATTER_CACHE[key] = (st.st_mtime_ns, st.st_size, meta)
    return dict(meta)


def session_start_from_transcript(transcript_path: str | os.PathLike[str] | None) -> float | None:
    """Best-effort session start time for a Claude Code session, as epoch seconds.

    Hooks receive ``transcript_path``; the transcript is created when the session
    starts, so its creation time (``st_birthtime`` where the OS has one, else the
    earliest of ctime/mtime) bounds which agent definitions that session loaded.
    ``None`` when it cannot be read — :func:`resolve_spawn_type` then skips the
    freshness check rather than guessing.
    """
    if not transcript_path:
        return None
    try:
        st = Path(transcript_path).stat()
    except (OSError, ValueError):
        return None
    birth = getattr(st, "st_birthtime", None)
    if isinstance(birth, (int, float)) and birth > 0:
        return float(birth)
    return float(min(st.st_ctime, st.st_mtime))


# Fallback session start for resolve_spawn_type callers that pass none. The MCP
# server sets it to its own process start (see mcp_server.main): a stdio server is
# launched with the Claude Code session, so its start bounds which definitions the
# session loaded. It is a proxy — after a ``/mcp`` reconnect it is later than the
# real start, so a variant written in between counts as loaded when it is not; the
# Agent hook's loadability net (shared/agent_hook.py) rewrites such a spawn back to
# its base. Unset (None) outside the server, so tests and one-shot CLIs keep the
# old "no freshness check" behaviour.
_DEFAULT_SESSION_START_TS: float | None = None


def set_default_session_start(ts: float | None) -> None:
    """Set the session start :func:`resolve_spawn_type` assumes when given none."""
    global _DEFAULT_SESSION_START_TS
    _DEFAULT_SESSION_START_TS = float(ts) if ts is not None else None


@dataclass(frozen=True)
class SpawnTypeResolution:
    """Outcome of :func:`resolve_spawn_type`.

    ``applied_effort`` is set only when the chosen definition really pins it;
    ``requested_effort`` is what routing wanted. ``effort_source`` names the rule
    that decided (``tier_variant`` | ``caller_variant`` | ``definition`` |
    ``not_applicable`` | ``base_variant`` | ``pending_restart`` | ``unknown_base``)
    and ``effort_unapplied_reason`` says why a requested effort was not applied.
    """

    subagent_type: str
    applied_effort: str | None
    requested_effort: str | None
    effort_source: str | None = None
    effort_unapplied_reason: str | None = None
    variant_to_create: str | None = None
    base_subagent_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"subagent_type": self.subagent_type}
        for key in (
            "applied_effort",
            "requested_effort",
            "effort_source",
            "effort_unapplied_reason",
            "variant_to_create",
            "base_subagent_type",
        ):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload


def _definition_visible(path: Path, session_start_ts: float | None) -> bool:
    """True when *path* existed before the session started (or no start is known).

    A definition created or edited mid-session is not loaded until the next
    session, so naming it would make the host's Agent call fail outright.
    """
    if session_start_ts is None:
        return True
    try:
        return path.stat().st_mtime <= float(session_start_ts)
    except (OSError, TypeError, ValueError):
        return False


def _is_builtin_subagent_type(name: str) -> bool:
    return name.strip().lower() in BUILTIN_SUBAGENT_TYPES


def _resolve_tier_type(
    caller: str | None,
    tier: str,
    requested: str | None,
    session_start_ts: float | None,
    *,
    base_subagent_type: str | None = None,
) -> SpawnTypeResolution:
    """Rule a: ``threnody-<tier>-<effort>`` if installed and visible, else the tier type."""
    fallback = subagent_type_for_tier(tier)
    if requested is None:
        return SpawnTypeResolution(fallback, None, None, base_subagent_type=base_subagent_type)
    if host_native_effort_mode(caller) is None:
        return SpawnTypeResolution(
            fallback, None, requested, "not_applicable", "shell_has_no_host_native_effort",
            base_subagent_type=base_subagent_type,
        )
    name = effort_variant_subagent_type(tier, requested)
    path = _effort_definition_path(caller, name) if name else None
    try:
        installed = bool(path is not None and path.is_file())
    except OSError:
        installed = False
    if name and installed and path is not None:
        if _definition_visible(path, session_start_ts):
            return SpawnTypeResolution(
                name, requested, requested, "tier_variant", base_subagent_type=base_subagent_type
            )
        return SpawnTypeResolution(
            fallback, None, requested, "pending_restart", "variant_created_after_session_start",
            base_subagent_type=base_subagent_type,
        )
    return SpawnTypeResolution(
        fallback, None, requested, "tier_variant", "variant_not_installed",
        base_subagent_type=base_subagent_type,
    )


def resolve_spawn_type(
    *,
    caller: str | None,
    base: str | None,
    tier: str,
    effort: str | None,
    config: TGsConfig | None = None,
    session_start_ts: float | None = None,
    agents_dir: Path | None = None,
) -> SpawnTypeResolution:
    """Pick the ``subagent_type`` that actually carries the routed *effort*.

    The host's Agent tool has no effort parameter; effort exists only as
    ``effort:`` frontmatter on a definition, and definitions are frozen when a
    session starts. So "apply effort" means "name a definition that pins it and
    that this session has loaded". Rules, first match wins:

    a. no *base* → ``threnody-<tier>-<effort>`` if installed, else ``threnody-<tier>``.
    b. *base* is itself an installed ``…-low|medium|high`` variant → keep it.
    c. *base*'s definition declares ``effort:`` → keep it; the author wins over routing.
    d. *base* is a built-in type → keep it; built-ins take no effort.
    e. ``<base>-<effort>`` is installed (``.md`` only) → use it.
    f. otherwise keep *base* unpinned and name ``variant_to_create`` for the next
       session — unless *base* has no definition at all, in which case fall back to
       rule a: an unknown type makes the Agent call fail outright.

    *session_start_ts* (epoch seconds; see :func:`session_start_from_transcript`)
    rejects variants whose mtime is newer than the session — they exist on disk
    but the session has not loaded them. *config* applies the shell's
    ``named_subagent_types`` gate; ``None`` skips it (the caller already knows the
    host resolves names, e.g. a hook inspecting the host's own Agent call).

    *agents_dir* is where *base* and its ``<base>-<effort>`` siblings are looked
    up (default :func:`claude_agents_dir`); the Agent hook passes a project's
    ``.claude/agents`` when the base lives there. ``threnody-<tier>`` types are
    always looked up in the user directory, where install.sh puts them.
    *session_start_ts* ``None`` falls back to :func:`set_default_session_start`.
    """
    if session_start_ts is None:
        session_start_ts = _DEFAULT_SESSION_START_TS
    requested = normalize_effort(effort)
    name = base.strip() if isinstance(base, str) else ""
    if name and config is not None and not named_subagent_types_supported(config, caller):
        name = ""
    if not name:
        return _resolve_tier_type(caller, tier, requested, session_start_ts)

    tier_match = _TIER_BASE_RE.match(name)
    if tier_match:
        # ``threnody-medium`` names a tier, not a definition to derive variants of.
        return _resolve_tier_type(
            caller, tier_match.group("tier"), requested, session_start_ts
        )

    if not _SAFE_AGENT_NAME_RE.match(name):
        log.warning("resolve_spawn_type: unusable subagent_type %r; using tier type", name)
        fallback = _resolve_tier_type(caller, tier, requested, session_start_ts, base_subagent_type=name)
        return _with_source(fallback, "unknown_base")

    mode = host_native_effort_mode(caller)

    if ":" in name:
        # Plugin agents live in the plugin's own directory, which cannot be found
        # reliably from here — keep the name; a generated variant is flattened.
        variant = f"{name.replace(':', '-')}-{requested}" if requested else None
        vpath = _effort_definition_path(caller, variant, agents_dir) if variant else None
        if variant and vpath is not None and vpath.is_file() and _definition_visible(vpath, session_start_ts):
            return SpawnTypeResolution(variant, requested, requested, "base_variant", base_subagent_type=name)
        if requested is None:
            return SpawnTypeResolution(name, None, None, base_subagent_type=name)
        return SpawnTypeResolution(
            name, None, requested, "pending_restart", "plugin_definition_unresolvable",
            base_subagent_type=name,
        )

    if mode is None:
        # The shell resolves names but has no way to pin effort on a definition.
        if requested is None:
            return SpawnTypeResolution(name, None, None, base_subagent_type=name)
        return SpawnTypeResolution(
            name, None, requested, "not_applicable", "shell_has_no_host_native_effort",
            base_subagent_type=name,
        )

    # b. The caller named an explicit variant.
    suffix = _EFFORT_SUFFIX_RE.match(name)
    if suffix:
        own_path = _effort_definition_path(caller, name, agents_dir)
        if own_path is not None and own_path.is_file():
            if _definition_visible(own_path, session_start_ts):
                declared = (
                    normalize_effort(read_definition_frontmatter(own_path).get("effort"))
                    if mode == "frontmatter"
                    else None
                )
                applied = declared or suffix.group("effort")
                return SpawnTypeResolution(
                    name, applied, requested or applied, "caller_variant", base_subagent_type=name
                )
            # Present on disk, not loaded: resolve its base with the effort it named.
            requested = requested or suffix.group("effort")
            name = suffix.group("base")

    base_path = _effort_definition_path(caller, name, agents_dir)
    base_exists = bool(base_path is not None and base_path.is_file())

    # c. The definition pins its own effort.
    if mode == "frontmatter" and base_exists and base_path is not None:
        declared = normalize_effort(read_definition_frontmatter(base_path).get("effort"))
        if declared:
            return SpawnTypeResolution(
                name, declared, requested, "definition",
                "definition_declares_effort" if requested and requested != declared else None,
                base_subagent_type=name,
            )

    # d. Built-in type.
    if _is_builtin_subagent_type(name):
        if requested is None:
            return SpawnTypeResolution(name, None, None, base_subagent_type=name)
        return SpawnTypeResolution(
            name, None, requested, "not_applicable", "builtin_type", base_subagent_type=name
        )

    # e. ``<base>-<effort>`` is installed.
    variant = f"{name}-{requested}" if requested else None
    if variant:
        vpath = _effort_definition_path(caller, variant, agents_dir)
        if vpath is not None and vpath.is_file():
            if _definition_visible(vpath, session_start_ts):
                return SpawnTypeResolution(
                    variant, requested, requested, "base_variant", base_subagent_type=name
                )
            if base_exists or mode != "frontmatter":
                return SpawnTypeResolution(
                    name, None, requested, "pending_restart",
                    "variant_created_after_session_start", base_subagent_type=name,
                )

    # f. Unknown base on a shell whose definition directory we can see: never pass
    # it through. Codex review definitions are skills, not ``agents/*.toml``, so a
    # missing toml there proves nothing and the name is kept.
    if mode == "frontmatter" and not base_exists:
        log.warning(
            "resolve_spawn_type: no definition for subagent_type %r (%s); using tier type",
            name,
            base_path,
        )
        fallback = _resolve_tier_type(caller, tier, requested, session_start_ts, base_subagent_type=name)
        return _with_source(fallback, "unknown_base")

    if requested is None:
        return SpawnTypeResolution(name, None, None, base_subagent_type=name)
    if mode == "frontmatter":
        return SpawnTypeResolution(
            name, None, requested, "pending_restart", "variant_not_installed",
            variant_to_create=variant, base_subagent_type=name,
        )
    return SpawnTypeResolution(
        name, None, requested, "base_variant", "variant_not_installed", base_subagent_type=name
    )


def _with_source(resolution: SpawnTypeResolution, source: str) -> SpawnTypeResolution:
    """Re-label a tier fallback with the rule that forced it."""
    from dataclasses import replace

    reason = (
        source
        if resolution.applied_effort is None and resolution.requested_effort
        else None
    )
    return replace(resolution, effort_source=source, effort_unapplied_reason=reason)


def resolve_named_spawn_type(
    *,
    config: TGsConfig,
    caller: str | None,
    tier: str,
    subagent_type: str | None,
    effort: str | None,
    session_start_ts: float | None = None,
) -> SpawnTypeResolution:
    """Resolve what actually gets spawned for a plan subtask's named type.

    Review agents use named subagent types on shells that resolve them to an
    exported definition; every other host falls back to the tier-derived type.
    Both go through one resolver so a named type carries the routed effort too —
    it used to keep the bare name and silently drop the effort.
    """
    named_base = (
        subagent_type
        if subagent_type and named_subagent_types_supported(config, caller)
        else None
    )
    return resolve_spawn_type(
        caller=caller,
        base=named_base,
        tier=tier,
        effort=effort,
        # The capability gate was applied just above, including for config=None.
        config=None,
        session_start_ts=session_start_ts,
    )


def spawns_named_definition(resolution: SpawnTypeResolution, subagent_type: str | None) -> bool:
    """True when the spawned type is the definition (or a variant of it) *subagent_type* names.

    False for every fallback — a tier type standing in for a definition that is not
    installed (``unknown_base``), a shell that does not resolve names, a tier variant —
    because then the definition's instructions never reach the agent.
    """
    base = (subagent_type or "").strip()
    if not base or resolution.effort_source in {"unknown_base", "tier_variant"}:
        return False
    spawned = resolution.subagent_type
    return spawned == base or spawned.startswith(f"{base}-")


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
    effort: str | None = None,
    session_start_ts: float | None = None,
    resolution: SpawnTypeResolution | None = None,
) -> HostSpawnSpec:
    # *resolution* lets a caller that already resolved the type (to decide what the
    # prompt must carry) pass it in rather than resolve a second time.
    normalized_caller = normalize_caller_id(caller)
    if resolution is None:
        resolution = resolve_named_spawn_type(
            config=config,
            caller=caller,
            tier=tier,
            subagent_type=subagent_type,
            effort=effort,
            session_start_ts=session_start_ts,
        )
    # read_only tasks must never use direct_edit — they read source context only.
    method = "host_task" if read_only else host_native_method_for_tier(tier)
    resolved_role = role or derive_role_from_task(prompt)
    enriched_prompt = prompt
    if resolved_role and not prompt.startswith("["):
        enriched_prompt = f"[{resolved_role}] {prompt}"
    return HostSpawnSpec(
        tool=host_tool_for_caller(caller),
        method=method,
        model=model or host_native_model_for_tier(config, caller, tier),
        subagent_type=resolution.subagent_type,
        prompt=enriched_prompt,
        tier=tier,
        caller=normalized_caller,
        wave_id=wave_id,
        target_files=list(target_files or []),
        id=spawn_id,
        pattern_hash=pattern_hash,
        artifact_path=artifact_path,
        upstream=list(upstream or []),
        role=resolved_role,
        read_only=bool(read_only),
        effort=resolution.applied_effort,
        requested_effort=resolution.requested_effort,
        effort_source=resolution.effort_source,
        effort_unapplied_reason=resolution.effort_unapplied_reason,
        variant_to_create=resolution.variant_to_create,
        base_subagent_type=resolution.base_subagent_type,
    )


def _effort_for_subtask(
    subtask: Mapping[str, Any], tier: str, prompt: str, router_holder: list[Any], config: TGsConfig
) -> str:
    """Valid subtask ``reasoning_effort``, else derived from tier + duration bucket.

    *router_holder* is a one-slot list so a single TaskRouter is built lazily per
    waves call and reused across subtasks.
    """
    explicit = normalize_effort(subtask.get("reasoning_effort"))
    if explicit:
        return explicit
    duration = "medium"
    try:
        if not router_holder:
            from .router import TaskRouter

            router_holder.append(TaskRouter(config, db=None))
        duration = str(
            getattr(router_holder[0].classify(prompt), "expected_duration_bucket", "medium")
        )
    except Exception:
        log.debug("host_spawn_waves: duration classification failed", exc_info=True)
        if not router_holder:
            router_holder.append(None)  # don't retry a failing construction per subtask
    try:
        from .router import reasoning_params_for

        return reasoning_params_for(duration, tier)[0]
    except Exception:
        log.debug("host_spawn_waves: reasoning_params_for failed", exc_info=True)
        return "medium"


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


_OWNERSHIP_MARKER = "You own exactly these files:"
# Connective left dangling where a sibling path was blanked out of the prose:
# "in and ;", "in ;", "and ,", or a prompt that simply ends on "in"/"and".
_DANGLING_CONNECTIVE = re.compile(
    r"\b(?:in|on|at|into|via|with)\s+(?:and|or)\s*(?:[;,:.]|$)"
    r"|\b(?:in|on|at|into|via|with|and|or)\s+[;,]"
    r"|\b(?:in|and|or|on|at|with|into|via)\s*[.;:,]*\s*$",
    re.IGNORECASE,
)
# "tests/ test_x.py": a directory whose file name was split off by a clause cut.
_SPLIT_DIR_SHAPE = re.compile(r"(?<![\w/.\-])[\w.\-]+/\s+[\w.\-]+\.[A-Za-z][A-Za-z0-9]{0,4}\b")
_MIN_PROSE_WORDS = 4


def is_mangled_prompt(text: str) -> bool:
    """True when a heuristic-built prompt reads as spliced fragments.

    Operates on the description *without* the ownership sentence — that line is
    appended to every finalized subtask, so it would make any fragment look
    complete. Complements :func:`_is_fragment_prompt`, which only catches
    path-shaped slices: a lexical clause cut leaves prose such as
    ``"scoped verify signals in and ;"`` that has whitespace and so passes it.
    """
    body = (text or "")
    marker = body.find(_OWNERSHIP_MARKER)
    if marker != -1:
        body = body[:marker]
    body = body.strip()
    if not body:
        return True
    if _DANGLING_CONNECTIVE.search(body) or _SPLIT_DIR_SHAPE.search(body):
        return True
    words = [
        w for w in re.split(r"\s+", body)
        if w.strip(".,;:()") and "/" not in w and not _BARE_FILE_TOKEN.fullmatch(w.strip(".,;:()"))
    ]
    return len(words) < _MIN_PROSE_WORDS


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

    # A heuristic-built write prompt (it carries the ownership sentence) that reads
    # as spliced fragments cannot be repaired by dropping it: its siblings are cut
    # from the same task text, and keeping only the survivors silently loses files.
    # Fall back to one agent over the full task instead.
    for raw in subtasks:
        if not isinstance(raw, dict) or raw.get("read_only") or raw.get("review_dimension"):
            continue
        desc = str(raw.get("description") or "")
        if _OWNERSHIP_MARKER in desc and is_mangled_prompt(desc):
            report["collapsed_to_single"] = True
            report["fragment_prompt"] = {"id": raw.get("id"), "description": desc[:120]}
            report["reasons"].append(
                f"subtask {raw.get('id')}: mangled prompt fragment; "
                "collapsed to single full-task agent"
            )
            log.info("host plan: mangled prompt in subtask %s; collapsing to one agent", raw.get("id"))
            tiers = [str(s.get("tier")) for s in subtasks if isinstance(s, dict)]
            tier = default_tier if default_tier in {"low", "medium", "high"} else "medium"
            for t in tiers:
                if t in {"low", "medium", "high"} and (
                    ("low", "medium", "high").index(t) > ("low", "medium", "high").index(tier)
                ):
                    tier = t
            full = (str(task).strip() if task else "") or "Complete the requested task."
            plan_dict["subtasks"] = [
                {"id": 1, "description": full, "tier": tier, "depends_on": []}
            ]
            plan_dict["waves"] = [[1]]
            plan_dict["topology"] = "linear"
            plan_dict["strategy"] = "sequential"
            plan_dict["sanitization"] = report
            return report

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
    from .findings_merge import (
        FINDINGS_SEVERITY_WORDS,
        findings_line_example,
        findings_line_format,
        findings_path,
    )

    path = findings_path(run_id, spawn_id)
    # The format is spelled out here, not referred to: a definition-mode prompt
    # carries no report text, and a definition that is missing or shadowed carries
    # none either, so "the format given above" pointed at nothing.
    return (
        f"Write your findings to {path} (create parent dirs), one per line, exactly:\n"
        f"{findings_line_format(dimension)}\n"
        f"(SEVERITY: {FINDINGS_SEVERITY_WORDS}.) E.g.\n"
        f"{findings_line_example(dimension)}\n"
        "Leave the file empty if you find nothing.\n"
        "Then reply with ONLY this one-line summary and nothing else:\n"
        f"dim={dimension} total=<number of findings> high=<number of high or critical>"
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
# ``AGENTS.md`` is a cross-tool convention, but NOT every host reads it: Claude Code
# does not load it unless a CLAUDE.md imports it with ``@AGENTS.md``. Claude Code is
# therefore resolved by ``claude_code_instruction_files`` (the entry below is only a
# marker that the host is known). The other hosts' lists are unverified here and kept
# as they were.
_HOST_INSTRUCTION_FILES: dict[str, tuple[str, ...]] = {
    "claude-code": ("CLAUDE.md",),
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

# Bytes per token for the instruction tax. 4 B/token is the textbook ratio for English
# prose; these files are mixed German/English with tables, paths and code, which
# tokenize denser, so 3.5 is the more honest estimate. Still approximate.
_BYTES_PER_TOKEN = 3.5
# Claude Code follows ``@path`` imports up to five hops deep.
_CLAUDE_IMPORT_MAX_DEPTH = 5
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$", re.DOTALL | re.MULTILINE)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# ``@`` must start a token (so e-mail addresses do not match).
_IMPORT_RE = re.compile(r"(?<![\w@])@([^\s`<>()\[\]\"']+)")


def _claude_imports(text: str) -> list[str]:
    """``@path`` tokens in *text*, ignoring fenced blocks and inline code spans."""
    text = _FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    found: list[str] = []
    for raw in _IMPORT_RE.findall(text):
        ref = raw.rstrip(".,;:!?")
        if ref:
            found.append(ref)
    return found


def claude_code_instruction_files(
    workspace_root: str | Path,
    home: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> list[Path]:
    """Ordered, de-duplicated files Claude Code loads into a session at *workspace_root*.

    Covers the user-level ``CLAUDE.md`` (``config_dir`` or ``$CLAUDE_CONFIG_DIR`` or
    ``~/.claude``), ``CLAUDE.md`` / ``CLAUDE.local.md`` / ``.claude/CLAUDE.md`` in the
    workspace, ``CLAUDE.md`` in every parent up to the git repo root (no repo: up to
    ``home``, or the filesystem root if the workspace is outside it), ``.claude/rules``
    in the workspace and user dir (counted even when path-scoped: an upper bound), and
    ``@path`` imports of any loaded file (relative to the importer, ``~`` expanded,
    depth <= 5). ``AGENTS.md`` is only counted when imported. Missing files are skipped.
    """
    ws = Path(workspace_root).expanduser().resolve()
    home_dir = Path(home).expanduser() if home is not None else Path.home()
    try:
        home_dir = home_dir.resolve()
    except OSError:
        pass
    if config_dir is None:
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        config_dir = env if env else home_dir / ".claude"
    cfg = Path(config_dir).expanduser()

    roots: list[Path] = [cfg / "CLAUDE.md"]
    chain: list[Path] = []
    cur = ws
    while True:
        chain.append(cur)
        if (cur / ".git").exists() or cur == home_dir or cur.parent == cur:
            break
        cur = cur.parent
    for d in reversed(chain[1:]):  # outermost parents first, workspace last
        roots.append(d / "CLAUDE.md")
    roots += [ws / "CLAUDE.md", ws / ".claude" / "CLAUDE.md", ws / "CLAUDE.local.md"]
    for rules in (cfg / "rules", ws / ".claude" / "rules"):
        try:
            roots.extend(sorted(rules.rglob("*.md")) if rules.is_dir() else [])
        except OSError:
            continue

    seen: set[Path] = set()
    ordered: list[Path] = []

    def visit(path: Path, depth: int) -> None:
        try:
            resolved = path.resolve()
            if resolved in seen or not resolved.is_file():
                return
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        seen.add(resolved)
        ordered.append(resolved)
        if depth >= _CLAUDE_IMPORT_MAX_DEPTH:
            return
        for ref in _claude_imports(text):
            target = Path(ref).expanduser() if ref.startswith("~") else resolved.parent / ref
            visit(target, depth + 1)

    for root in roots:
        visit(root, 0)
    return ordered


def instruction_tax_report(
    config: TGsConfig,
    *,
    workspace_root: str | None,
    agent_count: int,
    caller: str | None = None,
    home: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Report the per-agent instruction-file tax when it dominates a fan-out.

    Every host reloads its instruction files (CLAUDE.md and what it imports, …) into
    *each* subagent, so their combined size is multiplied by the agent count before
    any work happens. Threnody cannot trim them — they are the operator's files, and
    shrinking them may be exactly wrong — so the honest move is to state the number.
    This measures context size, not billed cost: prompt caching makes every repeat
    after the first agent cheaper than a cold read.

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
        root = Path(workspace_root)
        if not root.is_dir():
            return None
        if shell_id == "claude-code":
            paths = claude_code_instruction_files(root, home=home, config_dir=config_dir)
        else:
            paths = [root / rel for rel in candidates]
        root_resolved = root.resolve()
        files: list[dict[str, Any]] = []
        per_agent_bytes = 0
        for candidate in paths:
            try:
                if not candidate.is_file():
                    continue
                size = candidate.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            try:
                shown = str(candidate.relative_to(root_resolved))
            except ValueError:
                try:
                    shown = str(candidate.relative_to(root))
                except ValueError:
                    shown = str(candidate)
            per_agent_bytes += size
            files.append({"path": shown, "bytes": size})
        if not files:
            return None
        threshold = int(getattr(economy, "instruction_tax_warn_bytes", 200_000) or 200_000)
        total_bytes = per_agent_bytes * agent_count
        if total_bytes < threshold:
            return None
        files.sort(key=lambda item: int(item["bytes"]), reverse=True)
        per_agent_tokens = int(per_agent_bytes / _BYTES_PER_TOKEN)
        total_tokens = int(total_bytes / _BYTES_PER_TOKEN)
        # Largest first: the point of naming files is telling the operator what to shrink.
        largest = sorted(files, key=lambda item: int(item["bytes"]), reverse=True)[:3]
        top = ", ".join(
            f"{item['path']} ({int(item['bytes']) / 1024:.1f} KB)" for item in largest
        )
        return {
            "shell": shell_id,
            "per_agent_bytes": per_agent_bytes,
            "agent_count": agent_count,
            "total_bytes": total_bytes,
            # Approximate: the real number depends on the host's tokenizer.
            "bytes_per_token": _BYTES_PER_TOKEN,
            "per_agent_tokens": per_agent_tokens,
            "approx_total_tokens": total_tokens,
            "files": files,
            "top_files": largest,
            "note": (
                "Context-size measure, not billed cost: prompt caching makes repeats "
                "after the first agent cheaper."
            ),
            "details": (
                f"Instruction files loaded per agent total {per_agent_bytes:,} bytes "
                f"(~{per_agent_tokens:,} tokens) and are reloaded into each of "
                f"{agent_count} agents (~{total_tokens:,} tokens of context per run "
                f"before any work). Largest: {top}. Threnody cannot trim them; shortening "
                "the largest file is the single biggest per-agent saving available. "
                "Prompt caching makes repeats after the first agent cheaper, so this "
                "measures context size, not billed cost."
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

    router_holder: list[Any] = []
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

            # Effort comes from the original description, before the repo prefix and
            # protocol blocks below inflate it.
            subtask_effort = _effort_for_subtask(
                subtask, tier, prompt, router_holder, config
            )

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
            # The stable instruction block (dimension focus + report text) was left out
            # of the cell's prompt because its definition carries it. Resolve now to
            # find out whether that definition is what will actually be spawned; if not
            # (not installed, shadowed, shell without named types), put it back.
            resolution = resolve_named_spawn_type(
                config=config,
                caller=caller,
                tier=tier,
                subagent_type=subtask_subagent_type,
                effort=subtask_effort,
            )
            if subtask.get("review_stable_stripped") and not spawns_named_definition(
                resolution, subtask_subagent_type
            ):
                try:
                    from .review_fanout import stable_instructions_for

                    inline = stable_instructions_for(review_dimension)
                    if inline:
                        prompt = f"{inline}\n\n{prompt}"
                except Exception:
                    log.debug("host_spawn_waves: inline instructions failed", exc_info=True)
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
                    effort=subtask_effort,
                    resolution=resolution,
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
    effort: str | None = None,
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
    # Queens used to spawn with no effort at all, so the variant lookup never ran
    # for them. An explicit *effort* wins; otherwise the tier's routed default.
    queen_effort = normalize_effort(effort) or default_routed_effort(queen_tier)
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
            effort=queen_effort,
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
    effort: str | None = None,
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
        effort=normalize_effort(effort) or default_routed_effort(judge_tier),
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
    effort: str | None = None,
) -> dict[str, Any]:
    """Refusal payload for same-host execute_subtask, carrying the spawn to use instead.

    *effort* is the routed reasoning effort; without it the host is told to spawn
    the bare tier type and the effort routing chose is lost on this path.
    """
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
            effort=effort,
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
