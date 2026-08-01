"""Per-agent prompt assembly and budgeting for host-native waves.

Everything a subagent prompt carries is paid **once per agent**, so a fan-out past
~4 agents multiplies it. This module is the single place host-native prompts are
assembled, so that cost has one owner.

Two jobs:

1. **Ordering.** Stable text first, variable text last. Every major provider caches
   on an exact prefix, so N agents that share a stable block can hit that cache
   instead of each paying for it — but only if the shared text really is a prefix.
   Putting the file path first (``"Security review of shared/db.py: check for..."``)
   makes every prompt unique from character one and forfeits the hit.

2. **Budget.** An optional per-agent character cap. Overflow is *compressed*, never
   blindly truncated, and the stable block is compressed last — it holds the
   instructions, and an instruction-truncating cap is worse than no cap.

Pure and dependency-light (like :mod:`shared.consensus`): no DB, no config load, no
provider calls. Callers pass the budget in.

Public API
----------
    render(stable=..., variable=..., budget=...) -> RenderedPrompt
    effective_budget(config, caller) -> int
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

log = logging.getLogger(__name__)

# Separator between blocks. Blank-line separated so a stable block stays a stable
# *prefix* — appending a variable block must never alter a single byte before it.
BLOCK_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class RenderedPrompt:
    """Result of :func:`render`."""

    text: str
    stable_chars: int
    variable_chars: int
    original_chars: int
    layers_applied: tuple[str, ...] = ()

    @property
    def saved_chars(self) -> int:
        """Characters removed relative to the uncompressed assembly."""
        return max(0, self.original_chars - len(self.text))

    @property
    def compressed(self) -> bool:
        return bool(self.layers_applied)


def _clean_blocks(blocks: Sequence[str] | None) -> list[str]:
    """Drop empty/whitespace-only blocks, preserving order."""
    if not blocks:
        return []
    return [b.strip() for b in blocks if isinstance(b, str) and b.strip()]


def render(
    *,
    stable: Sequence[str] | None = None,
    variable: Sequence[str] | None = None,
    budget: int = 0,
) -> RenderedPrompt:
    """Assemble one agent prompt, stable blocks first, under an optional *budget*.

    *budget* is a character cap; ``0`` (or negative) disables it. Block order within
    each group is preserved exactly — this function concatenates, it never reorders
    semantic content.

    Overflow handling, in order:
      1. structural strip of the variable blocks (blank + comment-only lines)
      2. summary truncation of the variable text, with an explicit omission sentinel
      3. give up and keep the stable block whole, logging a warning

    Step 3 is deliberate: the stable block is the agent's instructions. Truncating it
    to satisfy a cap produces an agent that silently does the wrong job, which costs
    more than the tokens saved.
    """
    stable_blocks = _clean_blocks(stable)
    variable_blocks = _clean_blocks(variable)

    stable_text = BLOCK_SEPARATOR.join(stable_blocks)
    variable_text = BLOCK_SEPARATOR.join(variable_blocks)
    original = _join(stable_text, variable_text)
    original_len = len(original)

    if budget <= 0 or original_len <= budget:
        return RenderedPrompt(
            text=original,
            stable_chars=len(stable_text),
            variable_chars=len(variable_text),
            original_chars=original_len,
        )

    applied: list[str] = []
    # Only the variable part is ever compressed. `layers` is scoped deliberately:
    # ContextCompressor's dedup layer replaces repeated text with a `[ref: hash]`
    # marker, which is meaningless in a standalone prompt the receiving agent reads
    # in isolation.
    try:
        from .context import ContextCompressor
    except Exception:  # pragma: no cover - import guard
        log.debug("prompt_budget: ContextCompressor unavailable", exc_info=True)
        return RenderedPrompt(
            text=original,
            stable_chars=len(stable_text),
            variable_chars=len(variable_text),
            original_chars=original_len,
        )

    if variable_text:
        stripper = ContextCompressor(layers=["structural_strip"])
        result = stripper.compress(variable_text, "file")
        if result.layers_applied:
            variable_text = result.text
            applied.extend(result.layers_applied)

    if len(_join(stable_text, variable_text)) > budget and variable_text:
        room = budget - len(stable_text) - len(BLOCK_SEPARATOR)
        if room > 0:
            truncated = _truncate_to(variable_text, room)
            if len(truncated) < len(variable_text):
                variable_text = truncated
                applied.append("budget_truncation")

    text = _join(stable_text, variable_text)
    if len(text) > budget:
        log.warning(
            "prompt_budget: prompt is %d chars against a %d budget and cannot be "
            "reduced further without truncating instructions; emitting in full",
            len(text),
            budget,
        )

    return RenderedPrompt(
        text=text,
        stable_chars=len(stable_text),
        variable_chars=len(variable_text),
        original_chars=original_len,
        layers_applied=tuple(applied),
    )


def _truncate_to(text: str, room: int) -> str:
    """Shrink *text* to at most *room* chars, keeping the head and the tail.

    Head-biased (70/30): the opening of a variable block carries the target and the
    task, the tail usually carries constraints. An explicit sentinel states what was
    dropped so the agent knows its context is partial rather than assuming it is
    complete.

    Never returns something longer than the input — the reason this is not
    ``ContextCompressor``'s summary-truncation layer, whose keep sizes are fixed
    constants and can inflate a short-but-over-budget string.
    """
    if room <= 0 or len(text) <= room:
        return text
    sentinel_template = "\n[... {} chars omitted to fit the prompt budget ...]\n"
    # Reserve space for the sentinel itself; if the room is so small that nothing
    # meaningful survives alongside it, keep a plain head slice instead.
    reserve = len(sentinel_template.format(len(text)))
    usable = room - reserve
    if usable < 80:
        return text[:room]
    keep_head = max(1, int(usable * 0.7))
    keep_tail = max(1, usable - keep_head)
    omitted = len(text) - keep_head - keep_tail
    return text[:keep_head] + sentinel_template.format(omitted) + text[-keep_tail:]


def _join(stable_text: str, variable_text: str) -> str:
    if stable_text and variable_text:
        return f"{stable_text}{BLOCK_SEPARATOR}{variable_text}"
    return stable_text or variable_text


def effective_budget(config: Any, caller: str | None) -> int:
    """Per-agent character budget for *caller*: per-shell override, else global.

    Returns ``0`` when no cap applies. Never raises — a config surface that cannot be
    read must not block a spawn.
    """
    if config is None:
        return 0
    try:
        from .config import normalize_caller_id, normalize_routing_policy_shell_id

        shell_id = normalize_routing_policy_shell_id(normalize_caller_id(caller))
        if shell_id:
            profile = config.routing_policy.effective_profile(shell_id)
            per_shell = int(getattr(profile, "prompt_char_budget", 0) or 0)
            if per_shell > 0:
                return per_shell
    except Exception:
        log.debug("prompt_budget: per-shell budget lookup failed", exc_info=True)
    try:
        return max(0, int(getattr(config.prompt_economy, "prompt_char_budget", 0) or 0))
    except Exception:
        log.debug("prompt_budget: global budget lookup failed", exc_info=True)
        return 0


# How much of an agent prompt's static instruction text to emit inline.
#   legacy     — full inline prompt, path first. Byte-identical to pre-capability
#                behaviour; the state when the operator has not opted in.
#   definition — omit the static text entirely; the host resolves it from the
#                exported subagent definition.
#   reordered  — keep the static text (the host has no definition to read) but put
#                it first so N agents share a cacheable prefix.
BOILERPLATE_LEGACY = "legacy"
BOILERPLATE_DEFINITION = "definition"
BOILERPLATE_REORDERED = "reordered"


def boilerplate_mode(config: Any, caller: str | None) -> str:
    """Decide how to emit static instruction text for *caller*.

    The flag is the policy and the capability is the fact — both matter, but they
    fail differently. Without the flag nothing changes at all. With the flag but
    without ``named_subagent_types``, dropping the text would leave the agent with no
    instructions, so it is kept and merely moved to the front (still worth doing: it
    is what makes the prefix cacheable).
    """
    if config is None:
        return BOILERPLATE_LEGACY
    try:
        if not getattr(config.prompt_economy, "externalize_boilerplate", False):
            return BOILERPLATE_LEGACY
    except Exception:
        log.debug("prompt_budget: prompt_economy lookup failed", exc_info=True)
        return BOILERPLATE_LEGACY
    try:
        from .config import (
            SUPPORTED_ROUTING_POLICY_SHELLS,
            normalize_caller_id,
            normalize_routing_policy_shell_id,
        )

        shell_id = normalize_routing_policy_shell_id(normalize_caller_id(caller))
        # An unknown shell must not inherit a capability through the advisory
        # fallback in effective_profile() — it has no exported definition.
        if not shell_id or shell_id not in SUPPORTED_ROUTING_POLICY_SHELLS:
            return BOILERPLATE_REORDERED
        profile = config.routing_policy.effective_profile(shell_id)
        if bool(getattr(profile, "named_subagent_types", False)):
            return BOILERPLATE_DEFINITION
    except Exception:
        log.debug("prompt_budget: capability lookup failed", exc_info=True)
    return BOILERPLATE_REORDERED


__all__ = [
    "BLOCK_SEPARATOR",
    "BOILERPLATE_DEFINITION",
    "BOILERPLATE_LEGACY",
    "BOILERPLATE_REORDERED",
    "RenderedPrompt",
    "boilerplate_mode",
    "effective_budget",
    "render",
]
