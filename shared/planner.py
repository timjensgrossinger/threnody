#!/usr/bin/env python3
"""
Threnody task planner.

Extracts the planning logic from the original orchestrator.
Uses CLI backends (gh copilot or claude) as the reasoning brain.
Includes plan caching with structural hashing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .config import (
    TGsConfig,
    SUBTASK_TEMPLATES,
    SubtaskTemplate,
)
from .discovery import (
    _build_gh_copilot_command,
    _copilot_neutral_cwd,
    _copilot_subprocess_env,
    _copilot_supports_model_flag,
)
from .db import Database
from .agents import AgentRegistry, build_learned_agent_runtime_context
from .style import StyleLearner, DecompositionPrefs

log = logging.getLogger(__name__)


def _fanout_plan_hash(plan: "ExecutionPlan") -> str:
    payload = {
        "analysis": plan.analysis,
        "subtasks": [
            {
                "id": st.id,
                "description": st.description,
                "tier": st.tier,
                "model": st.model,
                "provider": st.provider,
                "provider_id": st.provider_id,
                "depends_on": st.depends_on,
            }
            for st in plan.subtasks
        ],
        "strategy": plan.strategy,
        "total_agents": plan.total_agents,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ConvergenceTarget:
    """Retry policy for a subtask — re-executes until gate score meets threshold."""
    min_score: float = 0.8        # gate score threshold (0.0–1.0)
    max_rounds: int = 3           # hard cap; prevents infinite loops
    backoff_seconds: float = 0.0  # blocking sleep between rounds


@dataclass
class Subtask:
    id: int
    stable_id: str | None = field(default=None, kw_only=True)
    description: str
    tier: str              # low | medium | high
    model: str = ""
    provider: str | None = None
    provider_id: str | None = None
    depends_on: list[int] = field(default_factory=list)
    from_template: bool = False
    agent_context: str | None = None  # Injected by agent registry matching
    consumes: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    is_coordinator: bool = False
    target_file: str | None = None
    workspace_root: str | None = None
    single_file_insertion: bool = False
    edit_mode: str = "write"   # write | rewrite | blocks | patch
    op_class: str = "side_effecting"  # replayable | side_effecting | approval_required
    session_id: str | None = None  # plan 10: reuse persistent worker session
    convergence_target: ConvergenceTarget | None = None  # plan 14: quality convergence loop
    _consumes_explicit: bool = False
    _produces_explicit: bool = False
    _is_coordinator_explicit: bool = False

@dataclass
class TokenEstimate:
    prompt_chars: int = 0
    response_chars: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.prompt_chars // 4

    @property
    def response_tokens(self) -> int:
        return self.response_chars // 4

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.response_tokens

@dataclass
class ExecutionPlan:
    analysis: str
    subtasks: list[Subtask]
    waves: list[list[int]]
    total_agents: int
    strategy: str            # parallel | sequential | dag
    topology: str = "dag"
    max_rounds: int = 3
    token_budget: int | None = None
    planner_tokens: TokenEstimate = field(default_factory=TokenEstimate)
    estimated_agent_tokens: int = 0
    cache_hit: bool = False
    for_each_nodes: list["ForEachNode"] = field(default_factory=list)
    _topology_explicit: bool = False
    _max_rounds_explicit: bool = False


@dataclass
class SubtaskTemplate:
    """Template for generating per-item subtasks in a for_each node."""
    description_template: str  # supports {item} substitution
    tier: str = "low"
    model: str = ""
    target_file_template: str | None = None  # supports {item} substitution


@dataclass
class ForEachNode:
    """A for_each plan node that fans out over a runtime-determined list.

    source: JSONPath expression into a prior subtask output (e.g. "$.files[*]"),
            or the literal string "static" to use static_items.
    template: SubtaskTemplate — description_template uses {item} for substitution.
    concurrency: max parallel executions (0 = use config default).
    aggregate: "list" | "map" | "merge" | "first_success" — result reducer.
    static_items: pre-populated item list when source="static".
    """
    node_id: str
    source: str
    template: SubtaskTemplate
    concurrency: int = 0
    aggregate: str = "list"
    static_items: list[str] = field(default_factory=list)


def validate_plan(plan: ExecutionPlan) -> None:
    """Validate planner metadata required by downstream execution."""
    invalid_ids = [
        str(st.id)
        for st in plan.subtasks
        if not isinstance(st.model, str) or not st.model.strip()
    ]
    if invalid_ids:
        raise ValueError(
            "Plan contains subtasks with missing model metadata: "
            + ", ".join(invalid_ids)
        )


# ---------------------------------------------------------------------------
# Phase 3: Fan-out configuration and decision types
# ---------------------------------------------------------------------------

@dataclass
class FanOutConfig:
    """Opt-in fan-out parameters for the planner layer.

    These govern whether the planner surface should signal that fan-out is
    appropriate for a given plan.  Actual fan-out *execution* belongs to the
    orchestrator — the planner only produces a :class:`FanOutDecision`.
    """
    opt_in_fanout: bool = False
    max_routers: int = 3
    domain_confidence_threshold: float = 0.75
    per_router_budget: int = 50_000   # tokens per individual router call
    budget_limit: int = 200_000       # total token hard cap across all routers


@dataclass
class FanOutDecision:
    """Planner-layer assessment of whether fan-out should proceed.

    This is *advisory* — callers (orchestrators, MCP handlers) consume it and
    decide how to act.  The planner never executes fan-out itself.

    reason values
    -------------
    ``"disabled"``        opt_in_fanout is False — skip fan-out entirely.
    ``"single_route"``    plan has ≤ 1 subtask — fan-out would be pointless.
    ``"budget_exceeded"`` total estimated tokens exceed ``budget_limit``.
    ``"fanout"``          fan-out is appropriate; ``subtask_ids`` are eligible.
    """
    enabled: bool
    router_count: int
    reason: str
    subtask_ids: list[int] = field(default_factory=list)
    # Optional explainability and topology bias hints introduced for Phase 14
    topology_hint: str | None = None
    topology_bias_reason: str | None = None
    matched_urgency_signals: list[str] = field(default_factory=list)


class BudgetExceededError(RuntimeError):
    """Raised by :func:`evaluate_fanout` when the estimated token cost of a
    plan exceeds the ``budget_limit`` configured in :class:`FanOutConfig`.

    Kept deliberately narrow — it carries only the numeric context needed for
    the caller to log or surface a useful message.
    """

    def __init__(self, estimated: int, limit: int) -> None:
        super().__init__(
            f"Estimated tokens ({estimated}) exceed fan-out budget limit ({limit})"
        )
        self.estimated = estimated
        self.limit = limit


def evaluate_fanout(
    plan: ExecutionPlan,
    config: FanOutConfig | None = None,
    db: "Database | None" = None,
    urgency_score: float | None = None,
) -> FanOutDecision:
    """Planner-layer fan-out evaluation — purely advisory, never executes.

    Determines whether the caller should proceed with fan-out for *plan*,
    applying cap-budget guard-rails defined in *config*.

    This implementation is backward-compatible: callers that do not pass
    ``urgency_score`` will observe the previous behaviour. When present,
    ``urgency_score`` is clamped to [0.0, 1.0] and used to conservatively bias
    router_count and topology selection in favour of faster single-hop
    topologies under higher urgency.

    Numeric gates are conservative by design (see decisions D-05 and D-06):
    - urgency_score > 0.30  -> reduce router_count by 1 (conservative bias)
    - urgency_score >= 0.60 -> when plan shape is fan-out-friendly, prefer
      ``star`` topology via ``topology_hint`` (still subject to caps)

    Parameters
    ----------
    plan:
        The :class:`ExecutionPlan` produced by :meth:`Planner.plan`.
    config:
        Fan-out configuration.  A default (opt-out) config is used if *None*.
    db:
        Optional :class:`~shared.db.Database` handle.  When provided, a
        ``fanout_decision`` telemetry row is written so the decision is
        observable without requiring the orchestrator to be involved.
    urgency_score:
        Optional pre-computed urgency score in the [0.0, 1.0] range. If not
        provided, the function will attempt to read ``plan.urgency_score`` if
        present. If still unavailable, behaviour is unchanged (no bias).

    Returns
    -------
    FanOutDecision
        Advisory decision.  ``enabled=False`` means the caller should use the
        normal single-router path.

    Raises
    ------
    BudgetExceededError
        When ``opt_in_fanout`` is ``True`` and the plan's estimated token cost
        exceeds ``config.budget_limit``.  A disabled fan-out never raises.
    """
    if config is None:
        config = FanOutConfig()

    # Attempt to source urgency from the plan if caller didn't supply it.
    if urgency_score is None:
        urgency_score = getattr(plan, "urgency_score", None)
    if urgency_score is None:
        urgency_score = 0.0

    # Clamp to [0.0, 1.0] per T-14-04
    try:
        urgency_score = float(urgency_score)
    except Exception:
        urgency_score = 0.0
    urgency_score = max(0.0, min(1.0, urgency_score))
    topology_hint: str | None = None
    topology_bias_reason: str | None = None
    matched_urgency_signals: list[str] = []

    def _log_decision(reason: str, enabled: bool, router_count: int, extra: dict | None = None) -> None:
        if db is None:
            return
        try:
            # Preserve backward-compatible telemetry 'reason' string for
            # legacy consumers and tests. Include extended explainability
            # payload in parse_diagnostics so new consumers can read it without
            # breaking existing expectations.
            payload = {
                "reason": reason,
                "urgency_score": urgency_score,
            }
            if extra:
                payload.update(extra)
            db.log_agent_result(
                session_id="planner-fanout",
                task_hash=_fanout_plan_hash(plan),
                agent_id=0,
                tier="medium",
                model="planner",
                success=enabled,
                tokens_used=plan.estimated_agent_tokens,
                provider_name="planner",
                reason=reason,
                parse_diagnostics=json.dumps(payload),
                version="fanout-eval",
                # Phase 15 explainability fields
                urgency_score=urgency_score,
                selected_topology=topology_hint,
                fanout_final_action=reason,
            )
        except Exception:  # never let telemetry break the caller
            log.debug("evaluate_fanout: telemetry write failed", exc_info=True)

    # ── 1. Opt-in gate ────────────────────────────────────────────────────
    if not config.opt_in_fanout:
        _log_decision("disabled", False, 1)
        return FanOutDecision(enabled=False, router_count=1, reason="disabled")

    # ── 2. Budget hard cap ────────────────────────────────────────────────
    estimated = plan.estimated_agent_tokens
    if estimated > config.budget_limit:
        _log_decision("budget_exceeded", False, 0, {"estimated": estimated, "limit": config.budget_limit})
        raise BudgetExceededError(estimated, config.budget_limit)

    # ── 3. Single-route fallback ──────────────────────────────────────────
    if plan.total_agents <= 1:
        _log_decision("single_route", False, 1)
        return FanOutDecision(enabled=False, router_count=1, reason="single_route")

    # ── 4. Fan-out eligible ───────────────────────────────────────────────
    eligible_all = [st.id for st in plan.subtasks]
    # Start with the naive eligible set trimmed to max_routers
    eligible = eligible_all[: config.max_routers]

    # Conservative urgency bias: when modest urgency is present, reduce
    # router_count by 1 to prefer fewer parallel routers. This is intentionally
    # conservative to avoid undermining quality-sensitive parallelism.
    router_count = len(eligible)

    if urgency_score > 0.30:
        # Reduce router_count by 1 at minimum under low-to-moderate urgency.
        router_count = max(1, router_count - 1)
        matched_urgency_signals.append("urgency_bias:modest")

    # Higher gate: prefer star when the plan shape looks fan-out-friendly
    # (parallel strategy, no inter-subtask dependencies) and urgency is high.
    is_fanout_friendly = (
        getattr(plan, "strategy", None) == "parallel"
        and all((not st.depends_on) for st in plan.subtasks)
    )
    if urgency_score >= 0.60 and is_fanout_friendly and router_count > 1:
        topology_hint = "star"
        topology_bias_reason = "urgency_high_and_plan_parallel"
        matched_urgency_signals.append("urgency_bias:high")

    # Ensure we never suggest more routers than allowed by max_routers
    router_count = min(router_count, config.max_routers)

    # Select that many eligible subtask ids
    chosen = eligible[:router_count]

    # Compose human-readable reason that preserves prior semantics but adds
    # explainability about urgency-derived choices.
    reason = "fanout"
    if matched_urgency_signals:
        reason = "fanout_urgency_biased"

    extra = {
        "topology_hint": topology_hint,
        "topology_bias_reason": topology_bias_reason,
        "matched_urgency_signals": matched_urgency_signals,
    }
    _log_decision(reason, True, router_count, extra)

    return FanOutDecision(
        enabled=True,
        router_count=router_count,
        reason=reason,
        subtask_ids=chosen,
        topology_hint=topology_hint,
        topology_bias_reason=topology_bias_reason,
        matched_urgency_signals=matched_urgency_signals,
    )


# ---------------------------------------------------------------------------
# Phase 37: execute_swarm auto-topology selection
# ---------------------------------------------------------------------------

# Very-high urgency alone may promote star topology when hierarchy is not
# confirmed. Reuses the same urgency boundary already used by evaluate_fanout.
AUTO_TOPOLOGY_STAR_URGENCY_THRESHOLD = 0.60
# Very-high complexity alone may promote star topology for broad swarm tasks.
AUTO_TOPOLOGY_STAR_COMPLEXITY_THRESHOLD = 0.60


def _plan_has_parent_children(plan_meta: dict[str, Any]) -> bool:
    if bool(plan_meta.get("has_parent_children")):
        return True
    subtasks = plan_meta.get("subtasks")
    if not isinstance(subtasks, list):
        return False
    for subtask in subtasks:
        if not isinstance(subtask, dict):
            continue
        parent_id = subtask.get("parent_id")
        if parent_id not in (None, "", []):
            return True
    return False


def _coerce_auto_topology_complexity(
    plan_meta: dict[str, Any],
    *,
    router_count: int,
    config: TGsConfig,
) -> float:
    raw_complexity = plan_meta.get("complexity_score")
    if isinstance(raw_complexity, (int, float)):
        return round(max(0.0, min(1.0, float(raw_complexity))), 2)

    task_chars = 0
    raw_task_chars = plan_meta.get("task_chars")
    if isinstance(raw_task_chars, int):
        task_chars = max(0, raw_task_chars)
    elif isinstance(raw_task_chars, float):
        task_chars = max(0, int(raw_task_chars))

    max_agents = max(1, int(config.swarm_max_agents))
    _inv_agents = pow(max_agents, -1) if max_agents != 0 else 0.0
    router_component = min(max(router_count, 0) * _inv_agents, 1.0)
    _inv_chars = pow(4_000.0, -1)
    char_component = min(task_chars * _inv_chars, 1.0)
    hierarchy_component = 0.15 if _plan_has_parent_children(plan_meta) else 0.0
    return round(max(router_component, min(char_component + hierarchy_component, 1.0)), 2)


def _build_auto_topology_advisory_plan(router_count: int) -> ExecutionPlan:
    normalized_count = max(2, int(router_count))
    subtasks = [
        Subtask(
            id=index + 1,
            description=f"auto-topology-advisory-{index + 1}",
            tier="low",
            model="planner",
            depends_on=[],
        )
        for index in range(normalized_count)
    ]
    return ExecutionPlan(
        analysis="Phase 37 auto-topology advisory plan",
        subtasks=subtasks,
        waves=[list(range(1, normalized_count + 1))],
        total_agents=normalized_count,
        strategy="parallel",
        estimated_agent_tokens=normalized_count * 1_000,
    )


def make_auto_topology_decision(
    plan_meta: dict[str, Any],
    urgency_score: float,
    router_count: int,
    *,
    config: TGsConfig,
    db: Database | None = None,
) -> tuple[str, str]:
    """Choose the effective swarm topology for execute_swarm auto mode.

    The decision is advisory and additive: hierarchy wins only when a concrete
    parent-child structure is already present in planner-shaped metadata; star
    may be selected for very high urgency or very high complexity; otherwise we
    keep the balanced DAG default.
    """
    del db  # Reserved for future non-breaking telemetry expansion.

    normalized_urgency = max(0.0, min(1.0, float(urgency_score)))
    normalized_router_count = max(1, int(router_count))
    if _plan_has_parent_children(plan_meta):
        return "hierarchical", "hierarchy_detected"

    complexity_score = _coerce_auto_topology_complexity(
        plan_meta,
        router_count=normalized_router_count,
        config=config,
    )
    if normalized_urgency >= AUTO_TOPOLOGY_STAR_URGENCY_THRESHOLD:
        advisory_plan = _build_auto_topology_advisory_plan(normalized_router_count)
        advisory = evaluate_fanout(
            advisory_plan,
            config=FanOutConfig(
                opt_in_fanout=True,
                max_routers=max(2, normalized_router_count),
                budget_limit=max(advisory_plan.estimated_agent_tokens * 2, 200_000),
            ),
            db=None,
            urgency_score=normalized_urgency,
        )
        if advisory.topology_hint == "star" or normalized_router_count > 1:
            return "star", "urgency_high"
        return "dag", "balanced_default"
    if complexity_score >= AUTO_TOPOLOGY_STAR_COMPLEXITY_THRESHOLD:
        return "star", "complexity_high"
    return "dag", "balanced_default"


# ---------------------------------------------------------------------------
# Planning prompt
# ---------------------------------------------------------------------------

PLANNING_PROMPT = """\
You are a task planner for a coding agent swarm. Your job is to analyse a \
coding task and produce an execution plan.

RESPOND WITH ONLY a JSON payload wrapped exactly like this — no markdown fences, \
no prose outside the wrapper:

<PLAN_JSON>
{{ ...valid JSON... }}
</PLAN_JSON>

JSON schema:
{{
  "analysis": "2-3 sentences: what this task involves and why you split it this way",
  "subtasks": [
    {{
      "id": 1,
      "description": "specific, actionable instruction for one coding agent",
      "tier": "low|medium|high",
      "model": "explicit routed model when known; otherwise repeat the chosen tier label",
      "provider": "optional human-readable provider name when known",
      "provider_id": "optional provider identifier when known",
      "target_file": "relative path to the primary output file (e.g. calc.py), or null",
      "single_file_insertion": false,
      "is_coordinator": false,
      "depends_on": []
    }}
  ],
  "strategy": "parallel|sequential|dag",
  "topology": "linear|star|hierarchical|dag"
}}

TIER RULES (use the cheapest tier that can handle each subtask):
- "low"    = single-file implementations, config parsing, data models, \
boilerplate, docstrings, type hints, simple CRUD, comments, README, \
scaffolding, formatting — anything a junior dev could write correctly
- "medium" = multi-file changes with shared interfaces, complex business \
logic, concurrency, error handling with retries, integration code that \
must coordinate multiple modules
- "high"   = architecture design, security review, complex algorithms, \
system design, threat modelling, performance-critical code

BIAS TOWARD LOW: most single-file modules (even with moderate logic) \
should be "low". Only use "medium" when the subtask genuinely requires \
reasoning about cross-module interactions or complex control flow.

DECOMPOSITION RULES:
- Set single_file_insertion=true when ALL changes are line insertions into exactly ONE existing file, has target_file set, and requires no cross-module reasoning (boilerplate, method stubs, config blocks)
- Each subtask must be independently actionable by a coding agent
- For a subtask with target_file, instruct the agent to return only the complete
  file content in a fenced code block. The runtime, not the agent CLI, writes it.
- If the task is simple enough for one agent, return exactly 1 subtask
- Do NOT over-decompose — 1 to 6 subtasks is the sweet spot
- depends_on lists subtask IDs that MUST complete before this one starts
- Always include a non-empty "model" for each subtask
- MAXIMISE PARALLELISM: only add a dependency if the subtask literally \
cannot be written without seeing the output of another subtask
- AIM FOR 2-3 WAVES MAXIMUM
- Independent subtasks go in the same wave (run in parallel)
- Use "dag" strategy when some subtasks depend on others
- Subtask descriptions must be specific enough that an agent can work \
without seeing the other subtasks
- Include shared interface contracts in each subtask description

TASK TO PLAN:
{task}"""


# ---------------------------------------------------------------------------
# CLI backend abstraction
# ---------------------------------------------------------------------------

class CLIBackend:
    """Abstract interface for calling an LLM via CLI."""

    def call(self, prompt: str, model: str | None = None,
             timeout: int = 120) -> str | None:
        raise NotImplementedError


class GhCopilotBackend(CLIBackend):
    """Call LLM via gh copilot CLI."""

    def __init__(self) -> None:
        self._model_flag: bool | None = None

    def _has_model_flag(self) -> bool:
        if self._model_flag is None:
            try:
                self._model_flag = _copilot_supports_model_flag()
            except RuntimeError:
                raise
        return bool(self._model_flag)

    def call(self, prompt: str, model: str | None = None,
             timeout: int = 120) -> str | None:
        try:
            if model and self._has_model_flag():
                cmd = _build_gh_copilot_command(prompt, model)
            else:
                cmd = _build_gh_copilot_command(prompt)
            run_kwargs: dict[str, Any] = {
                "capture_output": True,
                "text": True,
                "timeout": timeout,
            }
            try:
                run_kwargs["cwd"] = _copilot_neutral_cwd()
                run_kwargs["env"] = _copilot_subprocess_env()
            except (OSError, RuntimeError) as exc:
                log.warning("gh copilot sandbox setup failed: %s", exc)
                return None
            result = subprocess.run(
                cmd,
                **run_kwargs,
            )
            if result.stdout and '"quota_exceeded"' in result.stdout:
                log.warning("gh copilot quota exceeded")
                return None
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            if result.stderr.strip():
                log.debug("gh copilot stderr: %s", result.stderr[:200])
            return result.stdout.strip() if result.stdout.strip() else None
        except FileNotFoundError:
            log.warning("gh copilot not found in PATH")
            return None
        except subprocess.TimeoutExpired:
            log.warning("gh copilot timed out after %ds", timeout)
            return None
        except (OSError, RuntimeError) as exc:
            log.warning("gh copilot setup failed: %s", exc)
            return None


class ClaudeCodeBackend(CLIBackend):
    """Call LLM via claude CLI (print mode)."""

    def call(self, prompt: str, model: str | None = None,
             timeout: int = 120) -> str | None:
        try:
            cmd = ["claude", "-p"]
            if model:
                cmd.extend(["--model", model])
            result = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            if result.stderr.strip():
                log.debug("claude stderr: %s", result.stderr[:200])
            return None
        except FileNotFoundError:
            log.warning("claude CLI not found in PATH")
            return None
        except subprocess.TimeoutExpired:
            log.warning("claude CLI timed out after %ds", timeout)
            return None


class ProviderAgnosticBackend(CLIBackend):
    """Routes planning calls to the best available provider via ProviderRegistry."""

    def __init__(self, registry: "Any", caller: str = "mcp") -> None:
        self._registry = registry
        self._caller = caller

    def call(self, prompt: str, model: str | None = None,
             timeout: int = 120) -> str | None:
        try:
            result = self._registry.execute_cheapest(
                prompt,
                tier="medium",
                prefer_free=True,
                caller=self._caller,
                timeout=timeout,
            )
            return result.get("result")
        except RuntimeError as exc:
            log.warning("ProviderAgnosticBackend: all providers failed: %s", exc)
            return None
        except Exception as exc:
            log.warning("ProviderAgnosticBackend: unexpected error: %s", exc, exc_info=True)
            return None


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict | None:
    """Extract the first valid JSON object from LLM output."""
    raw = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    brace = raw.find("{")
    if brace == -1:
        return None
    depth = 0
    for i, ch in enumerate(raw[brace:], start=brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[brace:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_planner_output(raw: str) -> dict:
    """Parse a planner response wrapped in explicit delimiters."""
    raw = raw.strip()
    start = raw.find(PLAN_START)
    end = raw.rfind(PLAN_END)
    if start == -1 or end == -1 or end <= start:
        raise PlannerParseError("Missing required planner delimiters")

    payload = raw[start + len(PLAN_START):end].strip()
    if not payload:
        raise PlannerParseError("Planner output contained an empty plan payload")

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PlannerParseError(
            f"Invalid planner JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from exc


# ---------------------------------------------------------------------------
# Wave builder (topological sort)
# ---------------------------------------------------------------------------

def build_waves(subtasks: list[Subtask]) -> list[list[int]]:
    """Sort subtasks into dependency-ordered parallel waves."""
    known_ids = {st.id for st in subtasks}
    remaining: dict[int, set[int]] = {}
    for st in subtasks:
        deps = {dep for dep in st.depends_on if dep in known_ids}
        unknown = [dep for dep in st.depends_on if dep not in known_ids]
        if unknown:
            log.warning(
                "Ignoring unknown dependency IDs for subtask %d: %s",
                st.id,
                unknown,
            )
        remaining[st.id] = deps
    completed: set[int] = set()
    waves: list[list[int]] = []

    while remaining:
        ready = [sid for sid, deps in remaining.items()
                 if deps.issubset(completed)]
        if not ready:
            log.warning("Circular dependency in subtask DAG, forcing all remaining")
            waves.append(list(remaining.keys()))
            break
        waves.append(ready)
        for sid in ready:
            del remaining[sid]
            completed.add(sid)

    return waves


def validate_topology(
    execution_plan: ExecutionPlan,
) -> tuple[bool, list[str], str | None]:
    """Validate that explicit topology metadata matches dependency edges."""
    if not execution_plan._topology_explicit:
        return True, [], None

    declared = execution_plan.topology.strip().lower()
    valid_topologies = {"linear", "star", "hierarchical", "dag"}
    if declared not in valid_topologies:
        return (
            False,
            [f"declared topology '{execution_plan.topology}' is not supported"],
            "linear",
        )

    subtasks = execution_plan.subtasks
    known_ids = {st.id for st in subtasks}
    incoming: dict[int, set[int]] = {st.id: set() for st in subtasks}
    outgoing: dict[int, set[int]] = {st.id: set() for st in subtasks}
    for st in subtasks:
        for dep in st.depends_on:
            if dep not in known_ids:
                continue
            incoming[st.id].add(dep)
            outgoing[dep].add(st.id)

    indegree = {node: len(edges) for node, edges in incoming.items()}
    outdegree = {node: len(edges) for node, edges in outgoing.items()}
    roots = [node for node, degree in indegree.items() if degree == 0]
    leaves = [node for node, degree in outdegree.items() if degree == 0]
    edge_count = sum(len(edges) for edges in outgoing.values())

    remaining = {node: set(edges) for node, edges in incoming.items()}
    completed: set[int] = set()
    acyclic = True
    while remaining:
        ready = [node for node, deps in remaining.items() if deps.issubset(completed)]
        if not ready:
            acyclic = False
            break
        for node in ready:
            del remaining[node]
            completed.add(node)

    levels: dict[int, int] = {}
    if acyclic:
        unresolved = {node: set(edges) for node, edges in incoming.items()}
        level_completed: set[int] = set()
        while unresolved:
            ready = [node for node, deps in unresolved.items() if deps.issubset(level_completed)]
            if not ready:
                acyclic = False
                break
            for node in ready:
                parent_levels = [levels[parent] for parent in incoming[node]]
                levels[node] = (max(parent_levels) + 1) if parent_levels else 0
                del unresolved[node]
                level_completed.add(node)

    issues: list[str] = []
    if declared == "linear":
        if len(subtasks) <= 1:
            return True, [], None
        if not acyclic or edge_count != len(subtasks) - 1:
            issues.append(
                "declared topology 'linear' requires an acyclic single-path chain "
                "with exactly n-1 dependency edges"
            )
        if len(roots) != 1 or len(leaves) != 1:
            issues.append(
                "declared topology 'linear' requires exactly one root and one leaf"
            )
        if any(degree > 1 for degree in indegree.values()) or any(
            degree > 1 for degree in outdegree.values()
        ):
            issues.append(
                "declared topology 'linear' but at least one node has multiple predecessors or successors"
            )
    elif declared == "star":
        if len(subtasks) <= 1:
            return True, [], None
        if not acyclic:
            issues.append("declared topology 'star' must be acyclic")
        if len(roots) != 1:
            issues.append("declared topology 'star' requires a single root")
        else:
            root = roots[0]
            root_children = outgoing[root]
            if len(root_children) != len(subtasks) - 1:
                issues.append(
                    "declared topology 'star' but root does not connect directly to every other subtask"
                )
            for node in known_ids - {root}:
                if incoming[node] != {root}:
                    issues.append(
                        "declared topology 'star' but multiple non-root-to-child edges found"
                    )
                    break
                if outgoing[node]:
                    issues.append(
                        "declared topology 'star' but child subtasks have outgoing dependency edges"
                    )
                    break
    elif declared == "hierarchical":
        if not acyclic:
            issues.append("declared topology 'hierarchical' must be acyclic")
        if not roots:
            issues.append("declared topology 'hierarchical' requires at least one root")
        if levels and max(levels.values(), default=0) < 1 and len(subtasks) > 1:
            issues.append(
                "declared topology 'hierarchical' requires at least one parent-child dependency edge"
            )
    elif declared == "dag" and not acyclic:
        issues.append("declared topology 'dag' must be acyclic")

    if issues:
        return False, issues, "linear"
    return True, [], None


# ---------------------------------------------------------------------------
# Template matching
# ---------------------------------------------------------------------------

def match_template(description: str) -> SubtaskTemplate | None:
    """Check if a subtask description matches a known template."""
    desc_lower = description.lower()
    for template in SUBTASK_TEMPLATES:
        if re.search(template.pattern, desc_lower):
            return template
    return None


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

VALID_TIERS = {"low", "medium", "high"}
# Map legacy tier names from original planner prompts
TIER_ALIASES = {"mini": "low", "sonnet": "medium", "opus": "high"}
PLAN_START = "<PLAN_JSON>"
PLAN_END = "</PLAN_JSON>"
_SENSITIVE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_DOUBLE_QUOTED_RE = re.compile(r'"([^"\\]|\\.)*"')
_SINGLE_QUOTED_RE = re.compile(r"'([^'\\]|\\.)*'")


def _redact_quoted_strings(raw: str) -> str:
    redacted = _DOUBLE_QUOTED_RE.sub('"<redacted>"', raw)
    return _SINGLE_QUOTED_RE.sub("'<redacted>'", redacted)


def _sanitize_parse_diagnostics(raw: str) -> str:
    """Persist structural parse diagnostics without raw quoted content."""
    snippet = raw[:2048]
    snippet = _redact_quoted_strings(snippet)
    return _SENSITIVE_TOKEN_RE.sub("<redacted-token>", snippet)


class PlannerParseError(RuntimeError):
    """Raised when planner output is missing delimiters or valid JSON."""

    def __init__(self, detail: str, parse_diagnostics_id: int | None = None) -> None:
        super().__init__(detail)
        self.parse_diagnostics_id = parse_diagnostics_id


def validate_single_coordinator_per_wave(
    subtasks: list[Subtask],
    waves: list[list[int]],
) -> None:
    """Reject static plans that place multiple coordinators in the same wave."""
    subtasks_by_id = {st.id: st for st in subtasks}
    for wave_index, wave in enumerate(waves, start=1):
        coordinators = [
            subtask_id
            for subtask_id in wave
            if subtasks_by_id.get(subtask_id) is not None
            and subtasks_by_id[subtask_id].is_coordinator
        ]
        if len(coordinators) > 1:
            coordinator_list = ", ".join(str(subtask_id) for subtask_id in coordinators)
            raise PlannerParseError(
                "COOR-13-001 D-01/D-02: multiple coordinators in same wave are "
                f"invalid (wave {wave_index}: {coordinator_list})"
            )


def validate_no_duplicate_coordinator(
    current_plan: ExecutionPlan,
    amendment: dict[str, object],
) -> None:
    """Reject amendments that would introduce multiple coordinators in a wave."""
    updates = amendment.get("subtask_updates", [])
    amended_flags: dict[int, bool] = {}
    if isinstance(updates, list):
        for update in updates:
            if not isinstance(update, dict):
                continue
            raw_id = update.get("id")
            try:
                subtask_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if "is_coordinator" in update:
                amended_flags[subtask_id] = bool(update.get("is_coordinator"))

    if not amended_flags:
        return

    updated_subtasks: list[Subtask] = []
    for subtask in current_plan.subtasks:
        if subtask.id in amended_flags:
            updated_subtasks.append(
                replace(subtask, is_coordinator=amended_flags[subtask.id])
            )
        else:
            updated_subtasks.append(subtask)

    waves = build_waves(updated_subtasks)
    try:
        validate_single_coordinator_per_wave(updated_subtasks, waves)
    except PlannerParseError as exc:
        raise PlannerParseError(f"D-03: {exc}") from exc


class Planner:
    """
    LLM-backed task planner.

    Uses a CLI backend (gh copilot or claude) as the reasoning brain.
    Plans are cached by structural hash for reuse across both versions.
    """

    def __init__(self, config: TGsConfig, backend: CLIBackend,
                 db: Database | None = None,
                 agent_registry: AgentRegistry | None = None,
                 style_learner: StyleLearner | None = None) -> None:
        self._config = config
        self._backend = backend
        self._db = db
        self._agent_registry = agent_registry
        self._style_learner = style_learner
        self._decomp_prefs = DecompositionPrefs(db) if db else None
        self._planner_model = config.planner_model
        self._planner_timeout = config.planner_timeout

    def plan(
        self,
        task: str,
        skip_cache: bool = False,
        project_path: str | None = None,
        *,
        topology: str | None = None,
        max_agents: int | None = None,
    ) -> ExecutionPlan:
        """Decompose a task into an execution plan.

        Checks plan cache first. If miss, calls the LLM backend.
        """
        constraints: list[str] = []
        normalized_topology = str(topology or "").strip().lower()
        if normalized_topology:
            constraints.append(f"- Required topology: {normalized_topology}.")
            if normalized_topology == "star":
                constraints.append(
                    "- For star topology, include exactly one root subtask with "
                    "is_coordinator=true. Every other subtask must depend directly "
                    "and only on that coordinator."
                )
        if max_agents is not None:
            constraints.append(
                f"- Return no more than {max(1, int(max_agents))} total subtasks."
            )
        constraint_suffix = ""
        if constraints:
            constraint_suffix = "\n\nEXECUTION CONSTRAINTS:\n" + "\n".join(constraints)
        cache_key = task + constraint_suffix

        # 1. Check plan cache
        if not skip_cache and self._db:
            cached = self._db.plan_get(cache_key)
            if cached and "subtasks" in cached:
                log.info("Plan cache hit — reusing cached decomposition")
                plan = self._build_plan(cached, task)
                plan.cache_hit = True
                return plan

        # 2. Call LLM backend
        started_at = time.monotonic()
        prompt = PLANNING_PROMPT.format(task=task) + constraint_suffix

        # Inject style preamble if available
        style_preamble = ""
        if self._style_learner and project_path:
            try:
                style_preamble = self._style_learner.get_preamble(project_path)
            except Exception:
                log.debug("Failed to get style preamble", exc_info=True)

        # Inject granularity preference
        granularity_hint = ""
        if self._decomp_prefs and project_path:
            try:
                pref = self._decomp_prefs.get_preferred_granularity(project_path)
                if pref == "coarse":
                    granularity_hint = (
                        "\nDECOMPOSITION PREFERENCE: User prefers fewer, larger subtasks. "
                        "Aim for 1-3 subtasks maximum."
                    )
                elif pref == "fine":
                    granularity_hint = (
                        "\nDECOMPOSITION PREFERENCE: User prefers more granular subtasks. "
                        "Aim for 4-6 subtasks with clear separation."
                    )
            except Exception:
                log.debug("Failed to get decomp preference", exc_info=True)

        if style_preamble or granularity_hint:
            prompt += "\n\nADDITIONAL CONTEXT:"
            if style_preamble:
                prompt += f"\n{style_preamble}"
            if granularity_hint:
                prompt += granularity_hint

        raw = self._backend.call(prompt, self._planner_model,
                                 self._planner_timeout)

        planner_tokens = TokenEstimate(
            prompt_chars=len(prompt),
            response_chars=len(raw) if raw else 0,
        )

        if not raw:
            log.warning("Planner returned no output")
            diagnostics_id = self._log_planner_telemetry(
                task,
                planner_tokens=planner_tokens,
                estimated_tokens=planner_tokens.total_tokens,
                actual_tokens=self._backend_actual_tokens(planner_tokens.total_tokens),
                timing_ms=int((time.monotonic() - started_at) * 1000),
                success=False,
                parse_diagnostics="",
                reason="planner_no_output",
            )
            raise PlannerParseError("Planner returned no output", diagnostics_id)

        try:
            parsed = parse_planner_output(raw)
        except PlannerParseError as exc:
            log.warning("Planner output not parseable")
            log.debug("Planner output snippet: %s", _sanitize_parse_diagnostics(raw[:500]))
            diagnostics_id = self._log_planner_telemetry(
                task,
                planner_tokens=planner_tokens,
                estimated_tokens=planner_tokens.total_tokens,
                actual_tokens=self._backend_actual_tokens(planner_tokens.total_tokens),
                timing_ms=int((time.monotonic() - started_at) * 1000),
                success=False,
                parse_diagnostics=_sanitize_parse_diagnostics(raw),
                reason="planner_parse_fallback",
            )
            raise PlannerParseError(str(exc), diagnostics_id) from exc
        if "subtasks" not in parsed:
            diagnostics_id = self._log_planner_telemetry(
                task,
                planner_tokens=planner_tokens,
                estimated_tokens=planner_tokens.total_tokens,
                actual_tokens=self._backend_actual_tokens(planner_tokens.total_tokens),
                timing_ms=int((time.monotonic() - started_at) * 1000),
                success=False,
                parse_diagnostics=_sanitize_parse_diagnostics(raw),
                reason="planner_missing_subtasks",
            )
            raise PlannerParseError(
                "Planner output omitted required 'subtasks' field",
                diagnostics_id,
            )

        plan = self._build_plan(parsed, task)
        plan.planner_tokens = planner_tokens
        plan.estimated_agent_tokens = self._estimate_agent_tokens(plan)
        self._log_planner_telemetry(
            task,
            planner_tokens=planner_tokens,
            estimated_tokens=planner_tokens.total_tokens,
            actual_tokens=self._backend_actual_tokens(planner_tokens.total_tokens),
            timing_ms=int((time.monotonic() - started_at) * 1000),
            success=True,
            reason="planner_plan",
        )

        # 3. Cache the plan
        if self._db:
            self._db.plan_put(cache_key, self.plan_to_dict(plan), self._planner_model)

        return plan

    def _build_plan(self, parsed: dict, original_task: str) -> ExecutionPlan:
        """Build an ExecutionPlan from parsed planner JSON."""
        subtasks: list[Subtask] = []
        seen_ids: set[int] = set()
        raw_topology = parsed.get("topology")
        topology_explicit = isinstance(raw_topology, str) and bool(raw_topology.strip())
        topology = raw_topology.strip() if topology_explicit else "dag"
        raw_max_rounds = parsed.get("max_rounds")
        max_rounds_explicit = raw_max_rounds is not None
        max_rounds = 3
        if raw_max_rounds is not None:
            try:
                parsed_max_rounds = int(raw_max_rounds)
            except (TypeError, ValueError):
                log.warning("Ignoring invalid max_rounds value: %r", raw_max_rounds)
            else:
                if parsed_max_rounds > 0:
                    max_rounds = parsed_max_rounds
                else:
                    log.warning("Ignoring non-positive max_rounds value: %r", raw_max_rounds)
        stable_id_prefix = self._stable_id_prefix(parsed)

        for i, raw_subtask in enumerate(parsed.get("subtasks", [])):
            st_data = raw_subtask if isinstance(raw_subtask, dict) else {}
            raw_id = st_data.get("id", i + 1)
            try:
                st_id = int(raw_id)
            except (TypeError, ValueError):
                log.warning("Invalid subtask id %r; using fallback id %d", raw_id, i + 1)
                st_id = i + 1
            if st_id in seen_ids:
                st_id = max(seen_ids) + 1
            seen_ids.add(st_id)

            raw_tier = st_data.get("tier", "medium")
            tier = raw_tier.lower() if isinstance(raw_tier, str) else "medium"
            tier = TIER_ALIASES.get(tier, tier)
            if tier not in VALID_TIERS:
                tier = "medium"

            deps = st_data.get("depends_on", [])
            if not isinstance(deps, list):
                deps = []
            deps = [d for d in deps if isinstance(d, int) and d != st_id]

            raw_description = st_data.get("description", original_task)
            if isinstance(raw_description, str) and raw_description.strip():
                description = raw_description.strip()
            else:
                description = original_task

            consumes_explicit = "consumes" in st_data
            produces_explicit = "produces" in st_data
            is_coordinator_explicit = "is_coordinator" in st_data
            consumes = self._coerce_artifact_types(st_data.get("consumes"))
            produces = self._coerce_artifact_types(st_data.get("produces"))
            is_coordinator = self._coerce_bool(st_data.get("is_coordinator"))

            overlapping_artifacts = sorted(set(consumes) & set(produces))
            if overlapping_artifacts:
                raise PlannerParseError(
                    "TOPO-11-001 conflicting artifact metadata for subtask "
                    f"{st_id}: {', '.join(overlapping_artifacts)}"
                )

            raw_target_file = self._coerce_optional_text(st_data.get("target_file"))
            target_file: str | None = raw_target_file.strip() if raw_target_file else None
            single_file_insertion = bool(st_data.get("single_file_insertion", False))

            # Check if this subtask matches a template
            explicit_route_declared = any(
                self._coerce_optional_text(st_data.get(key)) is not None
                for key in ("model", "provider", "provider_id")
            )
            template = match_template(description)
            from_template = False
            if template and not explicit_route_declared:
                tier = template.tier
                from_template = True

            model, provider, provider_id = self._resolve_subtask_route(
                st_data,
                tier=tier,
            )
            raw_stable_id = self._coerce_optional_text(st_data.get("stable_id"))
            # D-01..D-04: emit readable stable IDs while preserving numeric IDs
            # as the internal dependency authority.
            stable_id = raw_stable_id or f"{stable_id_prefix}-task{i + 1:02d}"

            subtasks.append(Subtask(
                id=st_id,
                stable_id=stable_id,
                description=description,
                tier=tier,
                model=model,
                provider=provider,
                provider_id=provider_id,
                depends_on=deps,
                from_template=from_template,
                consumes=consumes,
                produces=produces,
                is_coordinator=is_coordinator,
                target_file=target_file,
                single_file_insertion=single_file_insertion,
                _consumes_explicit=consumes_explicit,
                _produces_explicit=produces_explicit,
                _is_coordinator_explicit=is_coordinator_explicit,
            ))

        if not subtasks:
            return self._single_agent_fallback(original_task)

        waves = build_waves(subtasks)
        # D-01/D-02: reject static plans with multiple same-wave coordinators
        # before they can reach the orchestrator. Missing metadata remains
        # opt-in and backward compatible because is_coordinator defaults False.
        validate_single_coordinator_per_wave(subtasks, waves)
        strategy = parsed.get("strategy", "parallel")
        if strategy not in ("parallel", "sequential", "dag"):
            strategy = "dag" if any(st.depends_on for st in subtasks) else "parallel"

        token_budget = parsed.get("token_budget")
        if token_budget is not None:
            try:
                token_budget = int(token_budget)
            except (TypeError, ValueError):
                log.warning("Ignoring invalid token_budget value: %r", token_budget)
                token_budget = None

        plan = ExecutionPlan(
            analysis=parsed.get("analysis", "Planner decomposition"),
            subtasks=self._auto_assign_agents(subtasks),
            waves=waves,
            total_agents=len(subtasks),
            strategy=strategy,
            topology=topology,
            max_rounds=max_rounds,
            token_budget=token_budget,
            _topology_explicit=topology_explicit,
            _max_rounds_explicit=max_rounds_explicit,
        )
        validate_plan(plan)
        return plan

    def _auto_assign_agents(self, subtasks: list[Subtask]) -> list[Subtask]:
        """Inject matching agent context preambles into subtask agent_context field.
        
        For each subtask:
        1. Check if agent_registry provided
        2. Try to match approved agent to subtask description
        3. If match found: inject agent context into subtask
        4. If no match or registry unavailable: route normally (backward compatible)
        
        Per D-01: Only active agents available for matching (not drafts).
        """
        if not self._agent_registry:
            return subtasks

        for st in subtasks:
            # Try new match_agent_to_subtask method first
            try:
                matched_agent = self._agent_registry.match_agent_to_subtask(st.description)
                if matched_agent:
                    st.agent_context = str(
                        matched_agent.get("context")
                        or build_learned_agent_runtime_context(matched_agent)
                    )
                    log.debug(f"Matched agent {matched_agent.get('agent_id', '?')} to subtask: {st.description[:50]}...")
                    continue
            except (AttributeError, Exception) as e:
                log.debug(f"match_agent_to_subtask failed: {e}, falling back to find_match")
            
            # Fallback to existing find_match for backward compatibility
            try:
                match = self._agent_registry.find_match(st.description)
                if match:
                    preamble = match.agent.context_preamble
                    if preamble and preamble not in st.description:
                        st.description = (
                            f"[Agent: {match.agent.pattern_hash[:8]}] "
                            f"{preamble}\n\n{st.description}"
                        )
                        log.info(
                            "Auto-assigned agent %s to subtask %d (score=%.2f)",
                            match.agent.pattern_hash[:8], st.id, match.score,
                        )
            except (AttributeError, Exception):
                # find_match not available, continue without agent matching
                pass
        
        return subtasks

    def _single_agent_fallback(self, task: str) -> ExecutionPlan:
        """Fallback: run the whole task as a single medium-tier agent."""
        model, provider, provider_id = self._resolve_subtask_route({}, tier="medium")
        st = Subtask(
            id=1,
            stable_id="phase00-plan01-task01",
            description=task,
            tier="medium",
            model=model,
            provider=provider,
            provider_id=provider_id,
        )
        plan = ExecutionPlan(
            analysis="Single-agent fallback (planner unavailable or simple task)",
            subtasks=[st],
            waves=[[1]],
            total_agents=1,
            strategy="sequential",
            topology="dag",
            max_rounds=3,
        )
        validate_plan(plan)
        return plan

    @staticmethod
    def _estimate_agent_tokens(plan: ExecutionPlan) -> int:
        total = 0
        for st in plan.subtasks:
            desc_tokens = len(st.description) // 4
            agent_overhead = 500
            estimated_output = desc_tokens * 3
            total += agent_overhead + desc_tokens + estimated_output
        return total

    def _backend_actual_tokens(self, fallback: int) -> int:
        for attr in ("last_actual_tokens", "actual_tokens", "last_token_usage"):
            value = getattr(self._backend, attr, None)
            if isinstance(value, int) and value >= 0:
                return value
        return fallback

    def _log_planner_telemetry(
        self,
        task: str,
        *,
        planner_tokens: TokenEstimate,
        estimated_tokens: int,
        actual_tokens: int,
        timing_ms: int,
        success: bool,
        parse_diagnostics: str | None = None,
        reason: str,
    ) -> int | None:
        if not self._db:
            return None
        return self._db.log_agent_result(
            session_id="planner",
            task_hash=self._db._key(task),
            agent_id=0,
            tier="medium",
            model=self._planner_model,
            success=success,
            tokens_used=planner_tokens.total_tokens,
            provider_name=self._backend.__class__.__name__,
            used_fallback=False,
            used_speculation=False,
            estimated_tokens=estimated_tokens,
            actual_tokens=actual_tokens,
            timing_ms=timing_ms,
            rework_count=0,
            parse_diagnostics=parse_diagnostics,
            reason=reason,
            version="planner",
        )

    @staticmethod
    def _coerce_artifact_types(value: object) -> list[str]:
        if isinstance(value, str):
            raw_items = [value]
        elif isinstance(value, list):
            raw_items = value
        else:
            return []

        items: list[str] = []
        seen: set[str] = set()
        for raw_item in raw_items:
            if not isinstance(raw_item, str):
                continue
            item = raw_item.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            items.append(item)
        return items

    @staticmethod
    def _coerce_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off", ""}:
                return False
            return False
        if isinstance(value, (int, float)):
            return bool(value)
        return False

    @staticmethod
    def _coerce_optional_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _sequence_number(
        value: object,
        *,
        default: int,
        pick_last: bool = False,
    ) -> int:
        if isinstance(value, int):
            return value if value > 0 else default
        if not isinstance(value, str):
            return default
        matches = re.findall(r"\d+", value)
        if not matches:
            return default
        selected = matches[-1] if pick_last else matches[0]
        try:
            number = int(selected)
        except ValueError:
            return default
        return number if number > 0 else default

    @classmethod
    def _stable_id_prefix(cls, parsed: dict) -> str:
        phase_number = cls._sequence_number(
            parsed.get("phase_number", parsed.get("phase")),
            default=0,
        )
        plan_number = cls._sequence_number(
            parsed.get("plan_number", parsed.get("plan_id", parsed.get("plan"))),
            default=1,
            pick_last=True,
        )
        return f"phase{phase_number:02d}-plan{plan_number:02d}"

    def _resolve_subtask_route(
        self,
        st_data: dict,
        *,
        tier: str,
    ) -> tuple[str, str | None, str | None]:
        model = self._coerce_optional_text(st_data.get("model"))
        provider = self._coerce_optional_text(st_data.get("provider"))
        provider_id = self._coerce_optional_text(st_data.get("provider_id"))
        if model is not None or provider is not None or provider_id is not None:
            if model is None:
                model = tier
            return model, provider, provider_id

        if tier:
            for preference in self._config.get_preferred_routing(tier):
                raw_preference_model = self._coerce_optional_text(
                    getattr(preference, "model", None)
                )
                preference_provider = self._coerce_optional_text(
                    getattr(preference, "provider", None)
                )
                if raw_preference_model is None and preference_provider is None:
                    continue
                preference_model = raw_preference_model or tier
                if preference_model or preference_provider:
                    return (
                        preference_model,
                        preference_provider,
                        None,
                    )

        return tier, None, None

    @staticmethod
    def plan_to_dict(plan: ExecutionPlan) -> dict:
        """Serialise an ExecutionPlan to JSON-safe dict."""
        pt = plan.planner_tokens
        payload = {
            "analysis": plan.analysis,
            "subtasks": [],
            "waves": plan.waves,
            "total_agents": plan.total_agents,
            "strategy": plan.strategy,
            "topology": plan.topology,
            "max_rounds": plan.max_rounds,
            "token_budget": plan.token_budget,
            "cache_hit": plan.cache_hit,
            "token_estimate": {
                "planner_input_tokens": pt.prompt_tokens,
                "planner_output_tokens": pt.response_tokens,
                "planner_total": pt.total_tokens,
                "estimated_agent_tokens": plan.estimated_agent_tokens,
                "estimated_total": pt.total_tokens + plan.estimated_agent_tokens,
            },
        }

        for st in plan.subtasks:
            subtask_payload = {
                "id": st.id,
                "stable_id": st.stable_id,
                "description": st.description,
                "tier": st.tier,
                "model": st.model,
                "depends_on": st.depends_on,
                "from_template": st.from_template,
            }
            if st.provider is not None:
                subtask_payload["provider"] = st.provider
            if st.provider_id is not None:
                subtask_payload["provider_id"] = st.provider_id
            if st._consumes_explicit or st.consumes:
                subtask_payload["consumes"] = st.consumes
            if st._produces_explicit or st.produces:
                subtask_payload["produces"] = st.produces
            if st._is_coordinator_explicit or st.is_coordinator:
                subtask_payload["is_coordinator"] = st.is_coordinator
            if st.single_file_insertion:
                subtask_payload["single_file_insertion"] = True
            if st.target_file is not None:
                subtask_payload["target_file"] = st.target_file
            payload.setdefault("subtasks", []).append(subtask_payload)
        return payload
