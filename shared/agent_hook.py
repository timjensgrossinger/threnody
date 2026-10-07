"""Claude Code Agent-tool hook: model + reasoning effort on every subagent spawn.

Claude Code's Agent tool takes a ``model`` but no effort; effort exists only as
``effort:`` frontmatter on an agent definition, and definitions are frozen when
the session starts. A spawn from a skill (GSD, team, code-review, auto-time …)
never asked Threnody, so before this hook it ran on whatever the caller happened
to name. The ``pre`` subcommand closes that gap for every spawn:

* routes the spawn heuristically (``TaskRouter`` with no DB — no LLM, no network,
  no sqlite) to a tier, a model alias and a reasoning effort;
* fills ``model`` when the call has none and the definition pins none;
* swaps ``subagent_type`` for the ``<base>-<effort>`` variant when this session
  has loaded one (``host_spawn.resolve_spawn_type``), and generates the variant
  for the *next* session when it does not exist yet;
* rewrites a type the session cannot load (a Threnody-generated variant newer
  than the session) back to its base, so the spawn cannot fail with
  "Agent type not found".

Precedence, strongest first: an explicit ``model`` in the call; the definition's
own ``model:`` / ``effort:``; a variant the caller named; routing. On a haiku
model no effort variant is chosen or generated (``effort_source:
model_unsupported``) — Claude Code accepts ``effort`` on haiku but it changes
nothing, and generating ``<base>-low`` for every low-tier spawn only clutters the
agents directory.

A spawn Threnody planned itself (an already-resolved ``threnody-<tier>-<effort>`` /
``threnody-review-<dim>-<effort>`` type, or a prompt pointing into this install's
``runs/<id>/``) is not routed again; only the loadability net runs
(``effort_source: planned``). A bare ``threnody-<tier>`` keeps its tier and gets the
routed effort's tier variant.

``effort_source`` values are the resolver's (``host_spawn.resolve_spawn_type``) plus
two of this hook's own: ``planned`` and ``model_unsupported`` (haiku).

Other subcommands only append ledger lines (``shared/agent_ledger.py``):
``post`` (PostToolUse — the concrete ``resolvedModel``), ``subagent-start``,
``subagent-stop``, and ``session-start`` which also records when the session
loaded its definitions.

Every subcommand prints nothing on failure and the wrapper always exits 0: a
broken hook must never block a spawn. ``THRENODY_AGENT_HOOK=off`` or config
``agent_hook.enabled: false`` turns ``pre`` into a no-op.

Permissions: ``pre`` answers ``permissionDecision: "allow"`` only for the ``Agent``
tool and only for a call it rewrote (Claude Code applies ``updatedInput`` only with
a decision). It never reads or changes ``permission_mode`` and never answers for any
other tool.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

CALLER = "claude-code"
_AGENT_TOOL = "Agent"
_KILL_VALUES = frozenset({"off", "0", "false", "no", "disabled"})
# Routing reads only the start of a long prompt: the router scores vocabulary,
# not length, and an unbounded regex pass is what would break the latency budget.
_ROUTE_TEXT_MAX = 4000
_PROMPT_SCAN_MAX = 200_000
_MODEL_FAMILIES = ("haiku", "sonnet", "opus")

# Already-resolved Threnody types: the effort is in the name, so routing again
# could only contradict the plan. A bare ``threnody-<tier>`` is not in here.
_THRENODY_TYPE_RE = re.compile(
    r"^threnody-(?:low|medium|high|review-[A-Za-z0-9_.-]+)-(?:low|medium|high)$"
)
_TIER_TYPE_RE = re.compile(r"^threnody-(?P<tier>low|medium|high)$")
_SPAWN_MARKER_RE = re.compile(r"<!--\s*threnody:spawn\b(?P<attrs>[^>]*)-->")
_MARKER_ATTR_RE = re.compile(r"\b(run|id)=([A-Za-z0-9_.:-]+)")
_EFFORT_SUFFIX_RE = re.compile(r"^(?P<base>.+)-(?P<effort>low|medium|high)$")
_VARIANT_MARKER = "<!-- threnody:effort-variant "


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def model_family(model: Any) -> str | None:
    """``haiku`` | ``sonnet`` | ``opus`` for an alias or a concrete model id."""
    text = _str(model).lower()
    for family in _MODEL_FAMILIES:
        if family in text:
            return family
    return None


def _kill_switch_env() -> bool:
    return _str(os.environ.get("THRENODY_AGENT_HOOK")).lower() in _KILL_VALUES


def _load_config() -> Any:
    from .config import CONFIG_YAML, TGsConfig

    try:
        return TGsConfig.from_yaml(CONFIG_YAML)
    except Exception:  # noqa: BLE001 - a broken config.yaml must not block a spawn
        return TGsConfig()


def _hook_enabled(config: Any) -> bool:
    section = getattr(config, "agent_hook", None)
    return bool(getattr(section, "enabled", True))


def _user_agents_dir() -> Path:
    from . import host_spawn

    return host_spawn.claude_agents_dir()


def _project_agent_dirs(cwd: Any) -> list[Path]:
    """``.claude/agents`` from *cwd* upward, nearest first, stopping below ``$HOME``."""
    text = _str(cwd)
    if not text:
        return []
    try:
        here = Path(text).expanduser().resolve(strict=False)
        home = Path.home().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return []
    dirs: list[Path] = []
    for candidate in (here, *here.parents):
        if candidate == home:
            break
        agents = candidate / ".claude" / "agents"
        if agents.is_dir():
            dirs.append(agents)
    return dirs


def _search_dirs(cwd: Any) -> list[Path]:
    """Where Claude Code finds a definition: the project's dirs win over the user's."""
    dirs = _project_agent_dirs(cwd)
    user = _user_agents_dir()
    if user not in dirs:
        dirs.append(user)
    return dirs


def _find_definition(name: str, dirs: list[Path]) -> Path | None:
    if not name or "/" in name or "\\" in name or ":" in name or name.startswith("."):
        return None
    for directory in dirs:
        candidate = directory / f"{name}.md"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _is_marked_variant(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
    except OSError:
        return False
    return _VARIANT_MARKER in head


def _is_builtin(name: str) -> bool:
    from .host_spawn import BUILTIN_SUBAGENT_TYPES

    return name.strip().lower() in BUILTIN_SUBAGENT_TYPES


def session_start_ts(payload: dict[str, Any]) -> float | None:
    """When this session loaded its agent definitions, as epoch seconds.

    ``agent_sessions.json`` (written at SessionStart) → transcript birth time →
    just before the first variant this hook ever generated → None. The third
    rung exists because ``None`` means "no freshness check": without it a variant
    generated by one spawn would be named by the next spawn of the same session,
    which has not loaded it.
    """
    from . import agent_ledger

    recorded = agent_ledger.session_start_for(payload.get("session_id"))
    if recorded is not None:
        return recorded
    from .host_spawn import session_start_from_transcript

    from_transcript = session_start_from_transcript(payload.get("transcript_path"))
    if from_transcript is not None:
        return from_transcript
    attempts = agent_ledger.read_json_state(agent_ledger.VARIANTS_NAME).values()
    stamps = [v for v in attempts if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return float(min(stamps)) - 1.0 if stamps else None


def planned_marker(tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """Run/spawn ids when Threnody planned this spawn, else None.

    Threnody's own spawn payloads name a ``threnody-*`` type and, when anything
    depends on the agent, point it into ``<install>/runs/<run_id>/``. An explicit
    ``<!-- threnody:spawn run=… id=… -->`` comment is accepted too.
    """
    prompt = tool_input.get("prompt")
    text = prompt[:_PROMPT_SCAN_MAX] if isinstance(prompt, str) else ""
    found: dict[str, Any] = {}
    marker = _SPAWN_MARKER_RE.search(text)
    if marker:
        for key, value in _MARKER_ATTR_RE.findall(marker.group("attrs")):
            found["run_id" if key == "run" else "spawn_id"] = value
        found["via"] = "marker"
    if not found and text:
        try:
            from .run_log import runs_root

            root = re.escape(str(runs_root()).rstrip("/"))
            match = re.search(
                root + r"/(?P<run>[A-Za-z0-9_.-]+)/(?:(?:artifacts|findings)/"
                r"(?P<spawn>[A-Za-z0-9_.-]+?)\.md|synthesis\.md)",
                text,
            )
        except Exception:  # noqa: BLE001 - marker detection is best-effort
            match = None
        if match:
            found = {"run_id": match.group("run"), "via": "run_path"}
            if match.group("spawn"):
                found["spawn_id"] = match.group("spawn")
    if not found and _THRENODY_TYPE_RE.match(_str(tool_input.get("subagent_type"))):
        found = {"via": "threnody_type"}
    return found or None


def route(text: str, config: Any, *, tier: str | None = None) -> dict[str, Any]:
    """Tier, effort and Agent-tool model alias for *text*. No DB, no network.

    *tier* pins the tier (a ``threnody-<tier>`` type already names one); the
    effort is then derived for that tier from the prompt's duration bucket.
    """
    from typing import ClassVar

    from .host_spawn import host_native_model_for_tier, normalize_effort
    from .router import TaskRouter, reasoning_params_for

    decision = TaskRouter(config, db=None).classify(text or "subagent task")
    routed_tier = decision.tier if decision.tier in ("low", "medium", "high") else "medium"
    effort = normalize_effort(getattr(decision, "reasoning_effort", None))
    if tier in ("low", "medium", "high") and tier != routed_tier:
        routed_tier, effort = tier, None
    if effort is None:
        effort = reasoning_params_for(
            getattr(decision, "expected_duration_bucket", "medium"), routed_tier
        )[0]
    tier = routed_tier

    class _NoRegistry:  # skip live discovery: it may probe CLIs
        available_providers: ClassVar[list[Any]] = []

    alias = host_native_model_for_tier(config, CALLER, tier, registry=_NoRegistry())
    return {"tier": tier, "effort": effort, "model": model_family(alias)}


def ensure_loadable(
    name: str, dirs: list[Path], session_start: float | None
) -> tuple[str, str | None]:
    """*name*, or its base when this session cannot have loaded *name*.

    Covers a ``<base>-<effort>`` type that does not exist at all, and a
    Threnody-generated one (marker in the file) written after the session
    started. A user's own unmarked file is left alone: it may be an edit of a
    definition the session did load.
    """
    if not name or ":" in name or _is_builtin(name):
        return name, None
    suffix = _EFFORT_SUFFIX_RE.match(name)
    if not suffix:
        return name, None
    base = suffix.group("base")
    path = _find_definition(name, dirs)
    if path is None:
        if _find_definition(base, dirs) is not None:
            return base, "variant_missing"
        return name, None
    if session_start is None or not _is_marked_variant(path):
        return name, None
    try:
        newer = path.stat().st_mtime > float(session_start)
    except (OSError, TypeError, ValueError):
        return name, None
    if newer and _find_definition(base, dirs) is not None:
        return base, "variant_created_after_session_start"
    return name, None


def maybe_create_variant(base: str, effort: str, base_path: Path) -> str | None:
    """Generate ``<base>-<effort>.md`` for the next session, once per pair.

    Only for definitions in the user agents dir — a variant there of a
    project-local base would shadow a different project's agent of the same
    name. Never for plugin agents, never over a file Threnody did not write
    (``export_effort_variant`` refuses those).
    """
    from . import agent_ledger

    if ":" in base or base_path.parent != _user_agents_dir():
        return None
    key = f"{base}|{effort}"
    attempts = agent_ledger.read_json_state(agent_ledger.VARIANTS_NAME)
    if key in attempts:
        return None
    attempts[key] = round(time.time(), 3)
    agent_ledger.write_json_state(agent_ledger.VARIANTS_NAME, attempts)
    try:
        from .agent_export import export_effort_variant

        dest = export_effort_variant(base_path, effort, base_path.parent)
    except Exception:  # noqa: BLE001 - a failed export only costs the next session's variant
        return None
    return str(dest) if dest is not None else None


# ---------------------------------------------------------------------------
# pre
# ---------------------------------------------------------------------------

def decide(payload: dict[str, Any], config: Any) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """``(updated_input or None, ledger record)`` for one PreToolUse payload."""
    from .host_spawn import (
        normalize_effort,
        read_definition_frontmatter,
        resolve_spawn_type,
    )

    tool_input = payload["tool_input"]
    requested_type = _str(tool_input.get("subagent_type"))
    requested_model = _str(tool_input.get("model")) or None
    session_start = session_start_ts(payload)
    dirs = _search_dirs(payload.get("cwd"))
    record: dict[str, Any] = {
        "requested_type": requested_type or None,
        "requested_model": requested_model,
    }
    new_type = requested_type
    new_model = requested_model
    model_source = "explicit" if requested_model else None

    planned = planned_marker(tool_input)
    if planned:
        record.update(
            planned=planned.get("via"),
            run_id=planned.get("run_id"),
            spawn_id=planned.get("spawn_id"),
            effort_source="planned",
        )
        definition = _find_definition(requested_type, dirs)
        if definition is not None:
            declared = read_definition_frontmatter(definition)
            record["applied_effort"] = normalize_effort(declared.get("effort")) or (
                declared.get("effort") or None
            )
            if not model_source and model_family(declared.get("model")):
                model_source = "definition"
    elif not requested_type or _is_builtin(requested_type):
        routed = route(_routing_text(tool_input), config)
        record.update(tier=routed["tier"], requested_effort=routed["effort"])
        record.update(effort_source="not_applicable", effort_unapplied_reason="builtin_type")
        if not requested_model and routed["model"]:
            new_model, model_source = routed["model"], "routed"
    elif ":" in requested_type:
        # Plugin agent: its definition lives in the plugin cache, which cannot be
        # resolved reliably here — and plugin authors pin their own model.
        routed = route(_routing_text(tool_input), config)
        record.update(
            tier=routed["tier"],
            requested_effort=routed["effort"],
            effort_source="pending_restart",
            effort_unapplied_reason="plugin_definition_unresolvable",
        )
    else:
        tier_type = _TIER_TYPE_RE.match(requested_type)
        routed = route(
            _routing_text(tool_input), config, tier=tier_type.group("tier") if tier_type else None
        )
        record.update(tier=routed["tier"], requested_effort=routed["effort"])
        base_path = _find_definition(requested_type, dirs)
        declared = read_definition_frontmatter(base_path) if base_path else {}
        declared_model = model_family(declared.get("model"))
        declared_effort = _str(declared.get("effort"))
        effective_model = requested_model or declared_model or routed["model"]
        if not requested_model and not declared_model and routed["model"]:
            new_model, model_source = routed["model"], "routed"
        elif declared_model and not model_source:
            model_source = "definition"
        if base_path is None:
            # Not ours to judge (--agents flag, managed dir, typo): keep it.
            record.update(
                effort_source="unknown_base", effort_unapplied_reason="definition_not_found"
            )
        elif declared_effort and normalize_effort(declared_effort) is None:
            # ``xhigh`` / ``max``: the resolver only knows low|medium|high.
            record.update(
                effort_source="definition",
                applied_effort=declared_effort,
                effort_unapplied_reason="definition_declares_effort",
            )
        elif not declared_effort and model_family(effective_model) == "haiku":
            record.update(
                effort_source="model_unsupported", effort_unapplied_reason="haiku_ignores_effort"
            )
        else:
            resolution = resolve_spawn_type(
                caller=CALLER,
                base=requested_type,
                tier=routed["tier"],
                effort=routed["effort"],
                session_start_ts=session_start,
                agents_dir=base_path.parent,
            )
            record.update(
                requested_effort=resolution.requested_effort or routed["effort"],
                applied_effort=resolution.applied_effort,
                effort_source=resolution.effort_source,
                effort_unapplied_reason=resolution.effort_unapplied_reason,
                base_type=resolution.base_subagent_type or requested_type,
            )
            if resolution.effort_source != "unknown_base":
                new_type = resolution.subagent_type
            if resolution.variant_to_create and resolution.requested_effort:
                base_name = resolution.base_subagent_type or requested_type
                base_def = _find_definition(base_name, dirs)
                if base_def is not None:
                    created = maybe_create_variant(
                        base_name, resolution.requested_effort, base_def
                    )
                    if created:
                        record["variant_created"] = created

    safe_type, safety_reason = ensure_loadable(new_type, dirs, session_start)
    if safe_type != new_type:
        record.update(
            effort_source="pending_restart",
            effort_unapplied_reason=safety_reason,
            applied_effort=None,
            base_type=safe_type,
        )
        if not record.get("requested_effort"):
            suffix = _EFFORT_SUFFIX_RE.match(new_type)
            record["requested_effort"] = suffix.group("effort") if suffix else None
        new_type = safe_type

    record["resolved_type"] = new_type or None
    resolved_alias = new_model
    if not resolved_alias:
        resolved_def = _find_definition(new_type, dirs)
        if resolved_def is not None:
            resolved_alias = model_family(read_definition_frontmatter(resolved_def).get("model"))
    record["resolved_model_alias"] = resolved_alias
    record["model_source"] = model_source

    changed = (new_type and new_type != requested_type) or (new_model and new_model != requested_model)
    if not changed:
        record["decision"] = "pass"
        return None, record
    updated = dict(tool_input)  # updatedInput replaces the whole input
    if new_type and new_type != requested_type:
        updated["subagent_type"] = new_type
    if new_model and new_model != requested_model:
        updated["model"] = new_model
    record["decision"] = "rewrite"
    return updated, record


def _routing_text(tool_input: dict[str, Any]) -> str:
    description = _str(tool_input.get("description"))
    prompt = tool_input.get("prompt")
    body = prompt[:_ROUTE_TEXT_MAX] if isinstance(prompt, str) else ""
    return f"{description}\n{body}".strip()


def _reason(updated: dict[str, Any], original: dict[str, Any], record: dict[str, Any]) -> str:
    parts: list[str] = []
    if updated.get("subagent_type") != original.get("subagent_type"):
        parts.append(f"subagent_type {original.get('subagent_type')!s} -> {updated.get('subagent_type')}")
    if updated.get("model") != original.get("model"):
        parts.append(f"model -> {updated.get('model')}")
    effort = record.get("applied_effort")
    if effort:
        parts.append(f"effort {effort}")
    return "Threnody agent hook: " + ", ".join(parts)


def handle_pre(payload: Any) -> dict[str, Any] | None:
    """Hook output for a PreToolUse payload, or None for a no-op."""
    from . import agent_ledger

    if not isinstance(payload, dict) or _kill_switch_env():
        return None
    if payload.get("tool_name") != _AGENT_TOOL:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    base_event = {
        "event": "pre",
        "session_id": payload.get("session_id"),
        "tool_use_id": payload.get("tool_use_id"),
        "cwd": payload.get("cwd"),
        "caller": CALLER,
        "description": agent_ledger.truncate_description(tool_input.get("description")),
    }
    try:
        config = _load_config()
        if not _hook_enabled(config):
            return None
        updated, record = decide(payload, config)
    except Exception as exc:  # noqa: BLE001 - logged as decision "error", spawn proceeds untouched
        agent_ledger.append_event(
            {
                **base_event,
                "requested_type": _str(tool_input.get("subagent_type")) or None,
                "requested_model": _str(tool_input.get("model")) or None,
                "decision": "error",
                "error": f"{type(exc).__name__}: {exc}"[:200],
            }
        )
        return None
    agent_ledger.append_event({**base_event, **record})
    if updated is None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": _reason(updated, tool_input, record),
            "updatedInput": updated,
        }
    }


# ---------------------------------------------------------------------------
# Ledger-only events
# ---------------------------------------------------------------------------

def handle_post(payload: Any) -> None:
    from . import agent_ledger

    if not isinstance(payload, dict) or payload.get("tool_name") != _AGENT_TOOL:
        return
    response = payload.get("tool_response")
    response = response if isinstance(response, dict) else {}
    duration = payload.get("duration_ms", response.get("duration_ms", response.get("totalDurationMs")))
    agent_ledger.append_event(
        {
            "event": "post",
            "session_id": payload.get("session_id"),
            "tool_use_id": payload.get("tool_use_id"),
            "agent_id": response.get("agentId") or response.get("agent_id"),
            "agent_type": response.get("agentType") or response.get("agent_type"),
            "resolved_model": response.get("resolvedModel") or response.get("model"),
            "status": response.get("status"),
            "duration_ms": duration if isinstance(duration, (int, float)) else None,
        }
    )


def handle_subagent(payload: Any, kind: str) -> None:
    from . import agent_ledger

    if not isinstance(payload, dict):
        return
    agent_ledger.append_event(
        {
            "event": kind,
            "session_id": payload.get("session_id"),
            "agent_id": payload.get("agent_id"),
            "agent_type": payload.get("agent_type"),
        }
    )


def handle_session_start(payload: Any) -> None:
    """Remember when this process loaded its agent definitions.

    Only ``startup`` / ``resume`` start a process; ``clear`` and ``compact`` keep
    the definitions the session already has, so they must not move the time.
    """
    from . import agent_ledger

    if not isinstance(payload, dict):
        return
    source = _str(payload.get("source")).lower()
    if source and source not in {"startup", "resume"}:
        return
    agent_ledger.record_session_start(_str(payload.get("session_id")))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SUBCOMMANDS = ("pre", "post", "subagent-start", "subagent-stop", "session-start")
# A hook payload is the tool input plus a few ids; anything past this is not one.
# A truncated read fails to parse, which is the no-op.
_STDIN_MAX = 16 * 1024 * 1024


def run(subcommand: str, raw: str) -> str:
    """Stdout for *subcommand* given stdin *raw*; ``""`` is the no-op."""
    try:
        payload = json.loads(raw) if raw.strip() else None
    except (ValueError, TypeError):
        return ""
    try:
        if subcommand == "pre":
            out = handle_pre(payload)
            return json.dumps(out) if out else ""
        if subcommand == "post":
            handle_post(payload)
        elif subcommand in ("subagent-start", "subagent-stop"):
            handle_subagent(payload, subcommand)
        elif subcommand == "session-start":
            handle_session_start(payload)
    except Exception:  # noqa: BLE001 - a hook must never fail the tool call
        return ""
    return ""


def main(argv: list[str] | None = None) -> int:
    import logging

    logging.disable(logging.CRITICAL)  # stderr noise reaches the user's transcript
    args = list(sys.argv[1:] if argv is None else argv)
    subcommand = args[0] if args else ""
    if subcommand not in _SUBCOMMANDS:
        return 0
    try:
        raw = sys.stdin.read(_STDIN_MAX)
    except Exception:  # noqa: BLE001 - unreadable stdin is the no-op
        return 0
    out = run(subcommand, raw)
    if out:
        sys.stdout.write(out + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
