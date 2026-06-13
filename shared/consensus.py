"""Shared multi-queen consensus decision logic.

Pure, dependency-light helpers used by BOTH execution paths:

* the subprocess star coordinator
  (``shared/orchestrator.py`` :meth:`Orchestrator.run_coordinator_consensus`)
* the host-native consensus wave
  (``shared/host_learning.py`` :func:`ingest_host_wave`)

Each path owns *execution* (spawning queens — subprocess vs host ``Agent``);
this module owns the *decision*: persona generation, quorum + structural
agreement, and judge arbitration. Keeping it in one place means both paths run
identical, independently-tested logic, and a queen is no longer an identical
re-run of the coordinator but a distinct *stance*.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

log = logging.getLogger(__name__)

VALID_VERDICTS = frozenset({"complete", "another-pass"})

# ---------------------------------------------------------------------------
# Personas — reviewer *stances*, not providers. Diversity comes from differing
# instructions on the same host model (host-native cannot cross providers), so
# the stances must genuinely pull the decision in different directions.
# ---------------------------------------------------------------------------

QUEEN_PERSONAS: list[dict[str, str]] = [
    {
        "id": "correctness-first",
        "label": "Correctness-first reviewer",
        "instruction": (
            "You are the CORRECTNESS-FIRST consensus queen. Judge the worker results "
            "strictly on whether they are correct and complete against the task. Return "
            "verdict 'complete' only when the work fully and correctly satisfies the "
            "requirements; otherwise return 'another-pass' with precise next_work. Ignore "
            "delivery speed and scope-trimming pressure."
        ),
    },
    {
        "id": "risk-first",
        "label": "Risk-first reviewer",
        "instruction": (
            "You are the RISK-FIRST consensus queen. Judge the worker results on safety, "
            "regressions, edge cases, security, and missing error handling. Lean toward "
            "'another-pass' whenever a material risk is unaddressed, and name the specific "
            "risk in next_work."
        ),
    },
    {
        "id": "speed-first",
        "label": "Speed-first reviewer",
        "instruction": (
            "You are the SPEED-FIRST consensus queen. Judge whether the work is good enough "
            "to ship now. Prefer verdict 'complete' when the core task is met, avoiding "
            "unnecessary extra rounds. Request 'another-pass' only for blocking defects."
        ),
    },
]

def _index_personas(personas: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    index: dict[str, dict[str, str]] = {}
    for persona in personas:
        pid = persona.get("id")
        if pid:
            index[pid] = persona
    return index


_PERSONA_BY_ID: dict[str, dict[str, str]] = _index_personas(QUEEN_PERSONAS)


def select_personas(n: int, config: Any | None = None) -> list[dict[str, str]]:
    """Return ``n`` distinct personas (clamped 2..3).

    A ``config.consensus_personas`` list of persona ids overrides the default
    order; unknown ids are ignored. Falls back to the built-in set when the
    override yields fewer than two valid personas.
    """
    try:
        count = max(2, min(3, int(n)))
    except (TypeError, ValueError):
        count = 2
    override = getattr(config, "consensus_personas", None) if config is not None else None
    if isinstance(override, (list, tuple)) and override:
        chosen: list[dict[str, str]] = []
        for pid in override:
            persona = _PERSONA_BY_ID.get(str(pid).strip().lower())
            if persona is not None and persona not in chosen:
                chosen.append(persona)
        if len(chosen) >= 2:
            return chosen[:count]
    return [dict(p) for p in QUEEN_PERSONAS[:count]]


def persona_id_from_spawn_id(spawn_id: str | None) -> str | None:
    """Recover a persona id from a ``queen-<persona_id>`` spawn id."""
    if not isinstance(spawn_id, str):
        return None
    text = spawn_id.strip().lower()
    if text.startswith("queen-"):
        candidate = text[len("queen-"):]
        if candidate in _PERSONA_BY_ID:
            return candidate
    return text if text in _PERSONA_BY_ID else None


def build_queen_prompt(base_prompt: str, persona: Mapping[str, str]) -> str:
    """Prepend a persona stance to the coordinator/review prompt.

    Replaces the legacy ``[queen-N]`` prefix with a real instruction so queens
    actually diverge instead of being identical re-runs.
    """
    instruction = str(persona.get("instruction") or "").strip()
    base = str(base_prompt or "").strip()
    if not instruction:
        return base
    return f"{instruction}\n\n{base}"


def consensus_review_instruction(task_text: str) -> str:
    """Default review prompt a host-native consensus queen answers.

    The host spawns each queen *after* the worker waves, so the queen reviews
    the work already in its context and returns a coordinator-style decision.
    """
    task = str(task_text or "").strip()
    return (
        "Review the results produced by the preceding worker waves for this task:\n"
        f"{task}\n\n"
        "Decide whether the work is finished or needs another pass. Respond ONLY with JSON:\n"
        '{"verdict": "complete" | "another-pass", '
        '"amendment": null, '
        '"next_work": null | {"reason": "...", "targets": ["..."]}, '
        '"synthesis": {"summary": "..."}}'
    )


# ---------------------------------------------------------------------------
# Tally
# ---------------------------------------------------------------------------


@dataclass
class ConsensusResult:
    """Outcome of tallying queen proposals.

    ``winner`` is the chosen proposal dict (``None`` only when degraded with no
    valid proposals or when a judge is still required). ``winner_index`` indexes
    into the *valid* proposals list.
    """

    winner: dict[str, Any] | None
    winner_index: int | None
    winner_persona: str | None
    quorum: bool
    judge_needed: bool
    degraded: bool
    valid_count: int
    queens: int
    personas: list[str]
    agreement: bool = False
    dominant_verdict: str | None = None
    valid: list[dict[str, Any]] = field(default_factory=list)

    def event_payload(self, *, round: int | None = None) -> dict[str, Any]:
        """Build a ``consensus_vote`` swarm-event payload."""
        payload: dict[str, Any] = {
            "queens": self.queens,
            "valid": self.valid_count,
            "personas": list(self.personas),
            "quorum": self.quorum,
            "judge_needed": self.judge_needed,
            "degraded": self.degraded,
            "selected_persona": self.winner_persona,
        }
        if self.agreement:
            payload["agreement"] = True
        if self.dominant_verdict is not None:
            payload["dominant_verdict"] = self.dominant_verdict
        if round is not None:
            payload["round"] = round
        return payload


def _decision_key(proposal: Mapping[str, Any]) -> str:
    """Canonicalize a proposal's decision for structural comparison.

    Mirrors the original orchestrator ``_key`` (verdict + amendment +
    next_work) so subprocess behaviour is preserved exactly.
    """
    return json.dumps(
        {
            "verdict": proposal.get("verdict"),
            "amendment": proposal.get("amendment"),
            "next_work": proposal.get("next_work"),
        },
        sort_keys=True,
        default=str,
    )


def _persona_of(proposal: Mapping[str, Any]) -> str | None:
    value = proposal.get("persona")
    return str(value) if value else None


def consensus_tally(
    proposals: Sequence[Mapping[str, Any]],
    *,
    quorum: int = 2,
    queens: int | None = None,
) -> ConsensusResult:
    """Decide a winner from queen proposals.

    Decision order (each proposal may carry a ``persona`` key):

    1. **no valid** (verdict not in ``VALID_VERDICTS``) → degraded.
    2. **single valid** → that proposal, no judge.
    3. **full agreement** (all valid share verdict+amendment+next_work) → quorum.
    4. **quorum**: the most common *full decision* appears ``>= quorum`` times →
       that representative wins (structural agreement on a subset).
    5. **otherwise** → ``judge_needed`` (verdicts/amendments diverge).
    """
    valid = [
        dict(p)
        for p in proposals
        if isinstance(p, Mapping) and p.get("verdict") in VALID_VERDICTS
    ]
    n_queens = int(queens) if queens is not None else len(proposals)
    personas = [pid for pid in (_persona_of(p) for p in valid) if pid]
    try:
        quorum = max(2, int(quorum))
    except (TypeError, ValueError):
        quorum = 2

    if not valid:
        return ConsensusResult(
            winner=None,
            winner_index=None,
            winner_persona=None,
            quorum=False,
            judge_needed=False,
            degraded=True,
            valid_count=0,
            queens=n_queens,
            personas=personas,
            valid=[],
        )

    if len(valid) == 1:
        w = valid[0]
        return ConsensusResult(
            winner=w,
            winner_index=0,
            winner_persona=_persona_of(w),
            quorum=False,
            judge_needed=False,
            degraded=False,
            valid_count=1,
            queens=n_queens,
            personas=personas,
            dominant_verdict=str(w.get("verdict")),
            valid=valid,
        )

    keys = [_decision_key(p) for p in valid]
    verdicts = [str(p.get("verdict")) for p in valid]
    dominant_verdict = Counter(verdicts).most_common(1)[0][0]

    # Full unanimity.
    if len(set(keys)) == 1:
        w = valid[0]
        return ConsensusResult(
            winner=w,
            winner_index=0,
            winner_persona=_persona_of(w),
            quorum=True,
            judge_needed=False,
            degraded=False,
            valid_count=len(valid),
            queens=n_queens,
            personas=personas,
            agreement=True,
            dominant_verdict=dominant_verdict,
            valid=valid,
        )

    # Quorum on the full decision key.
    top_key, top_count = Counter(keys).most_common(1)[0]
    if top_count >= quorum:
        idx = keys.index(top_key)
        w = valid[idx]
        return ConsensusResult(
            winner=w,
            winner_index=idx,
            winner_persona=_persona_of(w),
            quorum=True,
            judge_needed=False,
            degraded=False,
            valid_count=len(valid),
            queens=n_queens,
            personas=personas,
            dominant_verdict=dominant_verdict,
            valid=valid,
        )

    return ConsensusResult(
        winner=None,
        winner_index=None,
        winner_persona=None,
        quorum=False,
        judge_needed=True,
        degraded=False,
        valid_count=len(valid),
        queens=n_queens,
        personas=personas,
        dominant_verdict=dominant_verdict,
        valid=valid,
    )


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------


def build_judge_prompt(
    valid: Sequence[Mapping[str, Any]],
    *,
    artifacts_context: str | None = None,
) -> str:
    """Prompt for the arbitration judge.

    Proposals are annotated with their persona id (not a blind index) and, when
    available, the worker artifacts under review — so the judge reasons about
    content rather than picking a number in the dark.
    """
    proposals_text = json.dumps(
        [
            {
                "index": i,
                "persona": p.get("persona"),
                "verdict": p.get("verdict"),
                "amendment": p.get("amendment"),
                "next_work": p.get("next_work"),
                "synthesis": p.get("synthesis"),
            }
            for i, p in enumerate(valid)
        ],
        indent=2,
        default=str,
    )
    ctx = ""
    if artifacts_context and str(artifacts_context).strip():
        ctx = f"\nWorker artifacts under review:\n{str(artifacts_context).strip()}\n"
    return (
        f"You are the consensus judge selecting the best coordinator decision from "
        f"{len(valid)} proposals produced by reviewer queens with different stances "
        f"(correctness-first, risk-first, speed-first). Weigh correctness and risk above "
        f"speed.\n{ctx}\n"
        f"Proposals:\n{proposals_text}\n\n"
        'Respond ONLY with JSON: {"selected": <index>, "reason": "..."}'
    )


def parse_judge_decision(
    raw: str | None,
    valid: Sequence[Mapping[str, Any]],
) -> tuple[int, bool]:
    """Parse the judge output into ``(selected_index, judge_used)``.

    Deterministic fallback when the judge output is missing/garbage or the index
    is out of range: first ``complete`` proposal, else index 0 — with
    ``judge_used=False`` so callers can record that arbitration degraded.
    """
    n = len(valid)

    def _fallback() -> tuple[int, bool]:
        complete = [i for i, p in enumerate(valid) if p.get("verdict") == "complete"]
        return (complete[0] if complete else 0, False)

    if n == 0:
        return (0, False)
    try:
        from .planner import _extract_json

        payload = _extract_json(str(raw or "").strip())
    except Exception:
        log.debug("judge output parse failed", exc_info=True)
        return _fallback()
    if isinstance(payload, dict):
        idx = payload.get("selected")
        if isinstance(idx, int) and 0 <= idx < n:
            return (idx, True)
    return _fallback()


__all__ = [
    "VALID_VERDICTS",
    "QUEEN_PERSONAS",
    "ConsensusResult",
    "select_personas",
    "persona_id_from_spawn_id",
    "build_queen_prompt",
    "consensus_review_instruction",
    "consensus_tally",
    "build_judge_prompt",
    "parse_judge_decision",
]
