"""Per-shell reasoning-effort capability table.

One place that says, for each supported shell/provider, whether the routed
reasoning effort can actually be *applied*, and how. Two independent surfaces:

* ``host_native`` — the host spawns the subagent itself, so effort must be baked
  into a named agent definition (``frontmatter`` = Claude ``effort:`` in a ``.md``;
  ``codex_toml`` = ``model_reasoning_effort`` in ``~/.codex/agents/*.toml``).
* ``subprocess`` — Threnody launches the CLI and can add a flag.

``verified`` is False where the flag was not confirmed against docs or ``--help``
(Cursor): an unverified shell is never *given* a routed effort, only forwards an
explicit one through its existing builder. Dependency-light on purpose.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EffortSupport:
    host_native: str | None  # "frontmatter" | "codex_toml" | None
    subprocess: bool  # Threnody may add a routed effort to the CLI argv
    flag: str | None  # human-readable subprocess flag
    verified: bool


EFFORT_SUPPORT: dict[str, EffortSupport] = {
    "claude-code": EffortSupport("frontmatter", True, "--effort <e>", True),
    "codex": EffortSupport("codex_toml", True, '-c model_reasoning_effort="<e>"', True),
    # Copilot agent files have no effort key, so no host-native pinning. Subprocess flag verified via
    # `gh copilot -- --help`: --effort, --reasoning-effort <none|minimal|low|medium|high|xhigh|max> (we route low|medium|high).
    "github-copilot": EffortSupport(None, True, "--effort <e>", True),
    "aider": EffortSupport(None, True, "--reasoning-effort <e>", True),
    # Variant names are provider/model specific; forwarded as-is.
    "opencode": EffortSupport(None, True, "--variant <e>", True),
    # Builder forwards an explicit effort, but the flag is unverified: never route one.
    "cursor": EffortSupport(None, False, "--reasoning-effort <e>", False),
    "junie": EffortSupport(None, False, None, True),
    "mistral-vibe": EffortSupport(None, False, None, True),
    "blackbox-ai": EffortSupport(None, False, None, True),
    "amazon-q": EffortSupport(None, False, None, True),
    "kiro": EffortSupport(None, False, None, True),
    "windsurf": EffortSupport(None, False, None, True),
}

_ALIASES = {
    "copilot": "github-copilot",
    "github-copilot-cli": "github-copilot",
    "gh-copilot": "github-copilot",
    "gh": "github-copilot",
    "claude": "claude-code",
    "openai-codex": "codex",
}


def _canonical(shell_id: str | None) -> str | None:
    if not shell_id or not isinstance(shell_id, str):
        return None
    key = shell_id.strip().lower().replace("_", "-")
    return _ALIASES.get(key, key)


def support_for(shell_id: str | None) -> EffortSupport | None:
    key = _canonical(shell_id)
    return EFFORT_SUPPORT.get(key) if key else None


def host_native_effort_mode(caller: str | None) -> str | None:
    """``frontmatter`` | ``codex_toml`` | None for *caller*."""
    entry = support_for(caller)
    return entry.host_native if entry else None


def subprocess_effort_supported(provider_id: str | None) -> bool:
    """True when a routed (not explicitly requested) effort may be added to argv."""
    entry = support_for(provider_id)
    return bool(entry and entry.subprocess and entry.verified)


def default_routed_effort(tier: str, duration: str | None = None) -> str:
    """Provider-independent routed effort: ``router.reasoning_params_for``."""
    try:
        from .router import reasoning_params_for

        return reasoning_params_for(duration or "medium", tier)[0]
    except Exception:
        log.debug("routed effort derivation failed", exc_info=True)
        return "medium"
