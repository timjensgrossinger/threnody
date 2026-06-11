#!/usr/bin/env python3
"""
Threnody shared configuration.

All configurable values with sane defaults. Threshold bounds, template
definitions, TTLs, token ceilings, intent modifier weights.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import ipaddress
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import logging
import os

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised via shell fallback path
    yaml = None

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path("~/.local/lib/threnody").expanduser()
DB_PATH = BASE_DIR / "cache.db"
CONFIG_YAML = BASE_DIR / "config.yaml"

# ---------------------------------------------------------------------------
# Hard bounds — tier boundaries can NEVER collapse past these
# ---------------------------------------------------------------------------
LOW_TIER_FLOOR = 0.50
LOW_TIER_CEILING = 0.75
MEDIUM_HIGH_BOUNDARY_FLOOR = 0.75
MEDIUM_HIGH_BOUNDARY_CEILING = 0.95

# ---------------------------------------------------------------------------
# Token ceilings per tier (runaway kill switch)
# ---------------------------------------------------------------------------
TOKEN_CEILING_LOW = 1500
TOKEN_CEILING_MEDIUM = 4000
TOKEN_CEILING_HIGH = 8000

# ---------------------------------------------------------------------------
# Speculative execution (Phase 6)
# ---------------------------------------------------------------------------
SPECULATION_MARGIN = 0.05          # score must be within this of a tier boundary
SPECULATION_MIN_OUTPUT_CHARS = 40  # output shorter than this fails quality check
SPECULATION_ERROR_PATTERNS: list[str] = [
    r"(?i)\b(error|traceback|exception|failed|fatal)\b",
    r"(?i)\bnot found\b",
    r"(?i)\bsyntax error\b",
    r"(?i)\bundefined\b",
    r"(?i)\bpermission denied\b",
]

# ---------------------------------------------------------------------------
# Diff-based context injection (Phase 6)
# ---------------------------------------------------------------------------
CONTEXT_MAX_LINES_PER_FILE = 200   # cap per file to keep prompts small
CONTEXT_MAX_TOTAL_CHARS = 6000     # hard cap on injected context block
CONTEXT_FUNCTION_RADIUS = 5        # lines above/below a matched function
CONTEXT_MAX_FILE_BYTES = 2_097_152 # 2 MiB — skip files larger than this
ARTIFACT_MAX_INLINE_CHARS = 2048   # cap per artifact summary before injection truncation

# ---------------------------------------------------------------------------
# Surgical edit modes (rewrite / blocks)
# ---------------------------------------------------------------------------
SURGICAL_EDIT_MAX_FILE_BYTES = 32_768          # 32 KiB — max file for mode=rewrite
SURGICAL_EDIT_BLOCKS_MAX_FILE_BYTES = 131_072  # 128 KiB — max file for mode=blocks
SURGICAL_EDIT_LENGTH_RATIO_MIN = 0.5           # reject if output < 50 % of original
SURGICAL_EDIT_SHRINK_KEYWORDS: frozenset[str] = frozenset({
    "delete", "remove", "drop", "strip", "cleanup", "clean up",
    "prune", "trim", "shrink", "minimise", "minimize", "consolidate", "collapse",
})
PLANNER_ALLOW_TOPOLOGY_FALLBACK = False

# ---------------------------------------------------------------------------
# Plan cache TTL
# ---------------------------------------------------------------------------
PLAN_CACHE_TTL_HOURS = 168  # 7 days
RESULT_CACHE_TTL_HOURS = 168
CURRENT_PLAN_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Intent modifier keywords and weights
# ---------------------------------------------------------------------------
SPEED_SIGNALS: dict[str, float] = {
    "quick": -0.15,
    "just": -0.12,
    "fast": -0.15,
    "rough": -0.12,
    "simple": -0.10,
    "wip": -0.12,
    "draft": -0.10,
    "trivial": -0.12,
    "tiny": -0.10,
    "minor": -0.10,
}

QUALITY_SIGNALS: dict[str, float] = {
    "thorough": 0.15,
    "double check": 0.12,
    "production": 0.15,
    "careful": 0.12,
    "comprehensive": 0.15,
    "review": 0.10,
    "make sure": 0.12,
    "robust": 0.12,
    "bulletproof": 0.15,
    "enterprise": 0.12,
}

REASONING_SIGNALS: dict[str, float] = {
    # Creative generation
    "brainstorm":     0.18,
    "ideate":         0.18,
    "compose":        0.14,
    "draft":          0.10,
    "suggest":        0.10,
    "generate ideas": 0.18,

    # Evaluation / judgment
    "tradeoff":       0.18,
    "pros and cons":  0.18,
    "compare":        0.14,
    "recommend":      0.14,
    "evaluate":       0.18,
    "critique":       0.18,
    "prioritize":     0.14,
    "weigh":          0.14,

    # Reasoning / explanation
    "explain why":    0.18,
    "think through":  0.18,
    "justify":        0.14,
    "reasoning":      0.18,
    "analyze":        0.14,

    # Tone / style / persuasion
    "tone":           0.14,
    "voice":          0.14,
    "compelling":     0.18,
    "persuasive":     0.14,
    "engaging":       0.12,
    "narrative":      0.14,
}

# ---------------------------------------------------------------------------
# Subtask templates — route to low tier, skip LLM decomposition
# ---------------------------------------------------------------------------
@dataclass
class SubtaskTemplate:
    """A pre-built template for common subtask patterns."""
    pattern: str              # regex or keyword to match
    tier: str                 # always "low" for templates
    prompt_template: str      # the prompt with {target} placeholder
    description: str          # human-readable description

SUBTASK_TEMPLATES: list[SubtaskTemplate] = [
    SubtaskTemplate(
        pattern=r"add error handling",
        tier="low",
        prompt_template=(
            "Add comprehensive error handling to {target}. "
            "Wrap risky operations in try/except blocks with specific exception types. "
            "Log errors with context. Re-raise or return error indicators as appropriate."
        ),
        description="Add error handling to a module or function",
    ),
    SubtaskTemplate(
        pattern=r"add type hints?",
        tier="low",
        prompt_template=(
            "Add Python type hints to all function signatures and return types in {target}. "
            "Use modern syntax (X | None instead of Optional[X]). "
            "Import types from typing or collections.abc as needed."
        ),
        description="Add type hints to a module",
    ),
    SubtaskTemplate(
        pattern=r"write (?:unit )?tests? for",
        tier="low",
        prompt_template=(
            "Write unit tests for {target}. Use pytest. "
            "Cover happy path, edge cases (empty input, None, boundary values), "
            "and error conditions. Mock external dependencies."
        ),
        description="Write unit tests for a module or function",
    ),
    SubtaskTemplate(
        pattern=r"lint and format|format and lint",
        tier="low",
        prompt_template=(
            "Lint and format {target}. Apply consistent formatting, "
            "fix any lint warnings, ensure PEP 8 compliance."
        ),
        description="Lint and format code",
    ),
    SubtaskTemplate(
        pattern=r"add logging",
        tier="low",
        prompt_template=(
            "Add logging to {target} using Python's logging module. "
            "Use appropriate log levels (debug for internals, info for state changes, "
            "warning for recoverable issues, error for failures). "
            "Include structured context in log messages."
        ),
        description="Add logging to a module",
    ),
    SubtaskTemplate(
        pattern=r"add docstrings?",
        tier="low",
        prompt_template=(
            "Add docstrings to all public functions, classes, and methods in {target}. "
            "Use Google-style docstrings. Include Args, Returns, and Raises sections."
        ),
        description="Add docstrings to a module",
    ),
    SubtaskTemplate(
        pattern=r"add comments?",
        tier="low",
        prompt_template=(
            "Add clarifying comments to complex logic in {target}. "
            "Don't comment obvious code. Focus on 'why', not 'what'."
        ),
        description="Add comments to complex code",
    ),
]

# ---------------------------------------------------------------------------
# Complexity scoring signals (from original config.yaml)
# ---------------------------------------------------------------------------
DEFAULT_COMPLEXITY_SIGNALS: dict[str, list[str]] = {
    "high": [
        "refactor", "redesign", "optimize", "performance", "concurren",
        "parallel", "async", "distributed", "connection pool",
        "database layer", "microservice",
        "end-to-end", "e2e", "full stack", "cross-cutting", "system-wide",
        "multi-step", "pipeline", "workflow engine",
    ],
    "medium": [
        "implement", "integrate", "migrate", "test suite", "error handling",
        "middleware", "authentication", "authorization",
        "build", "set up", "configure", "scaffold", "bootstrap", "wire up",
        "connect", "hook up", "register", "provision",
    ],
    "low": [
        "add", "update", "fix", "change", "write", "create", "remove",
    ],
}

DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    "high": 0.20,
    "medium": 0.12,
    "low": 0.06,
}

DEFAULT_BASE_SCORE = 0.10
VALID_BILLING_TIERS = frozenset({"free", "subscription", "metered"})
VALID_ENDPOINT_PROVIDER_SCOPES = frozenset({"local", "network"})
VALID_ENDPOINT_PROVIDER_KINDS = frozenset({"ollama", "openai-compatible"})
VALID_ENDPOINT_PROVIDER_SCHEMES = frozenset({"http", "https"})
UNLIMITED_PARALLELISM = -1
VALID_ROUTING_POLICY_MODES = frozenset({"default", "guarded", "advisory", "custom"})
SUPPORTED_ROUTING_POLICY_SHELLS = (
    "claude-code",
    "github-copilot-cli",
    "cursor",
    "codex",
    "junie",
    "opencode",
)
ROUTING_POLICY_SHELL_ALIASES = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "github-copilot": "github-copilot-cli",
    "github-copilot-cli": "github-copilot-cli",
    "copilot": "github-copilot-cli",
    "gh-copilot": "github-copilot-cli",
    "cursor": "cursor",
    "codex": "codex",
    "openai-codex": "codex",
    "junie": "junie",
    "opencode": "opencode",
}
ROUTING_POLICY_HOOK_CAPABLE_SHELLS = frozenset({"claude-code"})
ROUTING_POLICY_SHELL_BOOTSTRAP_IDS = {
    "claude-code": "claude-code",
    "github-copilot-cli": "github-copilot",
    "cursor": "cursor",
    "codex": "codex",
    "junie": "junie",
    "opencode": "opencode",
}
DEFAULT_ROUTING_TIER_MODELS = {
    "low": "gpt-5-mini",
    "medium": "claude-sonnet-4.6",
    "high": "claude-opus-4.6",
}


def _shell_tier_model_defaults(shell_id: str) -> dict[str, str]:
    """Return per-shell host-native tier models, falling back to generic defaults."""
    from .model_registry import bootstrap_tier_map

    bootstrap_id = ROUTING_POLICY_SHELL_BOOTSTRAP_IDS.get(shell_id)
    if bootstrap_id is None:
        return dict(DEFAULT_ROUTING_TIER_MODELS)
    mapped = bootstrap_tier_map(bootstrap_id)
    if not mapped:
        return dict(DEFAULT_ROUTING_TIER_MODELS)
    merged = dict(DEFAULT_ROUTING_TIER_MODELS)
    merged.update(mapped)
    return merged

# Keyword overrides that bypass scoring entirely
DEFAULT_OVERRIDES: dict[str, list[str]] = {
    "low": [
        "docstring", "type hint", "add comment", "rename",
        "lint", "simple", "one-liner", "list files", "find files",
        "boilerplate", "typo", "spelling",
        "bump version", "update version", "changelog",
        "add log", "add logging statement", "print statement",
    ],
    "high": [
        "architect", "architecture", "design", "security review", "threat model",
        "multi-tenant", "system design", "compliance",
        "penetration test", "vulnerability", "cryptograph",
        "zero trust", "audit trail", "rbac", "role-based", "sso", "oauth",
        "saml", "pen test", "pentest",
        "scaffold", "database connection",
    ],
}

# ---------------------------------------------------------------------------
# Orchestrator defaults
# ---------------------------------------------------------------------------
DEFAULT_PLANNER_MODEL = "claude-sonnet-4-6"
DEFAULT_PLANNER_TIMEOUT = 120

DEFAULT_DELEGATION_UTILITIES: tuple[str, ...] = ("opencode", "aider")

# ---------------------------------------------------------------------------
# Adaptive threshold defaults
# ---------------------------------------------------------------------------
@dataclass
class ThresholdConfig:
    """Adaptive threshold configuration with hard bounds."""
    low_max: float = 0.55        # initial low/medium boundary
    medium_max: float = 0.80     # initial medium/high boundary

    def __post_init__(self) -> None:
        self.clamp()

    def clamp(self) -> None:
        """Enforce hard bounds — boundaries can never collapse."""
        self.low_max = max(LOW_TIER_FLOOR, min(LOW_TIER_CEILING, self.low_max))
        self.medium_max = max(MEDIUM_HIGH_BOUNDARY_FLOOR,
                              min(MEDIUM_HIGH_BOUNDARY_CEILING, self.medium_max))
        if self.medium_max <= self.low_max:
            self.medium_max = self.low_max + 0.05


@dataclass
class ParallelismConfig:
    """Execution settings for wave-level parallelism.

    ``max_workers`` uses ``UNLIMITED_PARALLELISM`` to mean "run as many workers
    as the current wave needs". Positive values remain explicit caps.
    """
    enabled: bool = True
    max_workers: int = UNLIMITED_PARALLELISM
    swarm_max_agents: int = 12
    speculation_workers: int = 1
    warm_path_workers: int = 2


def normalize_parallelism_limit(
    value: Any,
    *,
    zero_means_disabled: bool = False,
) -> int | None:
    """Return a positive explicit limit, ``0`` for disable, or ``None``.

    ``UNLIMITED_PARALLELISM`` and ``None`` both mean "no built-in cap".
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return None
    if limit == UNLIMITED_PARALLELISM:
        return None
    if limit == 0:
        return 0 if zero_means_disabled else None
    if limit < 0:
        return None
    return limit



@dataclass
class BudgetConfig:
    """Per-task budget defaults for orchestrated execution."""
    default_hard_cap_tokens: int | None = 8000
    default_soft_warning_pct: float = 0.8


@dataclass
class SpilloverConfig:
    """Configuration for multi-provider spillover behaviour.

    - enabled: whether spillover allocation is enabled (defaults to True)
    - per_provider_concurrency: optional map of provider_id -> integer limit
      When a provider is absent from the map, capacity is treated as unbounded
      (within any global/system caps enforced elsewhere).
    """

    enabled: bool = True
    per_provider_concurrency: dict[str, int | None] = field(default_factory=dict)

    def get_provider_capacity(self, provider_id: str) -> int | None:
        """Return configured concurrency for provider_id or None when unspecified.

        Matching is case-insensitive.
        """
        if not provider_id:
            return None
        normalized = provider_id.strip().lower()
        val = self.per_provider_concurrency.get(provider_id)
        if val is None:
            val = self.per_provider_concurrency.get(normalized)
        if isinstance(val, int) and val >= 0:
            return val
        return None


@dataclass(frozen=True)
class ProviderCostOverride:
    """User-supplied billing metadata override for a provider tier."""

    cost_rank: int | None = None
    billing_tier: str | None = None
    provider_cost_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.cost_rank is not None:
            payload["cost_rank"] = self.cost_rank
        if self.billing_tier is not None:
            payload["billing_tier"] = self.billing_tier
        if self.provider_cost_hint is not None:
            payload["provider_cost_hint"] = self.provider_cost_hint
        return payload


@dataclass(frozen=True)
class RoutingPreference:
    """Ordered tie-break preference for provider selection within one tier."""

    provider: str | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.model is not None:
            payload["model"] = self.model
        return payload


def normalize_routing_policy_shell_id(shell_id: str | None) -> str | None:
    """Return the canonical routing-policy shell id, if supported."""
    if not shell_id:
        return None
    normalized = shell_id.strip().lower().replace("_", "-")
    return ROUTING_POLICY_SHELL_ALIASES.get(normalized, normalized)


def normalize_caller_id(caller_id: str | None) -> str | None:
    """Return the canonical caller/provider id used by runtime routing."""
    if not caller_id:
        return None
    normalized = re.sub(r"[\s_]+", "-", caller_id.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if normalized in {"copilot", "github-copilot-cli", "gh-copilot", "gh"}:
        return "github-copilot"
    if normalized == "claude":
        return "claude-code"
    if normalized == "openai-codex":
        return "codex"
    return normalized


@dataclass(frozen=True)
class ShellRoutingProfile:
    """Resolved routing-instruction policy for one AI shell."""

    shell_id: str
    route_task_mandatory: bool = False
    low_tier_execute_subtask: bool = False
    agent_transparency_required: bool = False
    direct_edit_hooks: bool = False
    tier_model_mapping: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROUTING_TIER_MODELS))

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_task_mandatory": self.route_task_mandatory,
            "low_tier_execute_subtask": self.low_tier_execute_subtask,
            "agent_transparency_required": self.agent_transparency_required,
            "direct_edit_hooks": self.direct_edit_hooks,
            "tier_model_mapping": dict(sorted(self.tier_model_mapping.items())),
        }


@dataclass(frozen=True)
class RoutingPolicyConfig:
    """Global and per-shell routing-instruction policy."""

    mode: str = "default"
    shells: dict[str, ShellRoutingProfile] = field(default_factory=dict)

    def effective_profile(self, shell_id: str | None) -> ShellRoutingProfile:
        """Return the resolved profile for a shell under the current global mode."""
        canonical = normalize_routing_policy_shell_id(shell_id) or "github-copilot-cli"
        if canonical not in SUPPORTED_ROUTING_POLICY_SHELLS:
            log.warning("routing_policy: unsupported shell %r; using advisory defaults", shell_id)
            canonical = "github-copilot-cli"
        base = _recommended_shell_profile(canonical) if self.mode in {"default", "custom"} else _mode_shell_profile(canonical, self.mode)
        override = self.shells.get(canonical)
        if override is None:
            return base
        merged_models = dict(base.tier_model_mapping)
        merged_models.update(override.tier_model_mapping)
        direct_edit_hooks = override.direct_edit_hooks and canonical in ROUTING_POLICY_HOOK_CAPABLE_SHELLS
        return ShellRoutingProfile(
            shell_id=canonical,
            route_task_mandatory=override.route_task_mandatory,
            low_tier_execute_subtask=override.low_tier_execute_subtask,
            agent_transparency_required=override.agent_transparency_required,
            direct_edit_hooks=direct_edit_hooks,
            tier_model_mapping=merged_models,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"mode": self.mode}
        if self.shells:
            payload["shells"] = {
                shell_id: profile.to_dict()
                for shell_id, profile in sorted(self.shells.items())
            }
        return payload


def _mode_shell_profile(shell_id: str, mode: str) -> ShellRoutingProfile:
    tier_models = _shell_tier_model_defaults(shell_id)
    if mode == "guarded":
        return ShellRoutingProfile(
            shell_id=shell_id,
            route_task_mandatory=True,
            low_tier_execute_subtask=False,
            agent_transparency_required=True,
            direct_edit_hooks=shell_id in ROUTING_POLICY_HOOK_CAPABLE_SHELLS,
            tier_model_mapping=tier_models,
        )
    return ShellRoutingProfile(shell_id=shell_id, tier_model_mapping=tier_models)


def _recommended_shell_profile(shell_id: str) -> ShellRoutingProfile:
    if shell_id == "claude-code":
        return _mode_shell_profile(shell_id, "guarded")
    return _mode_shell_profile(shell_id, "advisory")


def _normalize_endpoint_kind(value: Any) -> str | None:
    text = _normalize_config_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered == "openai":
        return "openai-compatible"
    return lowered


def _base_url_is_loopback(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = parsed.hostname
    if host is None:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_endpoint_base_url(
    value: str,
    *,
    scope: str,
) -> tuple[bool, str | None]:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False, "invalid URL"
    scheme = (parsed.scheme or "").lower()
    if scheme not in VALID_ENDPOINT_PROVIDER_SCHEMES:
        return False, "scheme must be http or https"
    if parsed.username is not None or parsed.password is not None:
        return False, "embedded credentials are not allowed"
    if parsed.hostname is None:
        return False, "hostname is required"
    if scope == "local" and not _base_url_is_loopback(value):
        return False, "local scope requires a loopback base_url"
    if scope == "network" and scheme != "https":
        return False, "network scope requires https"
    return True, None


@dataclass(frozen=True)
class EndpointProviderConfig:
    """User-defined or config-backed HTTP endpoint provider."""

    name: str
    kind: str
    base_url: str
    scope: str = "network"
    enabled: bool = True
    tier_models: dict[str, str] = field(default_factory=dict)
    cost_rank: dict[str, int] = field(default_factory=dict)
    api_key_env: str | None = None
    verify_tls: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "scope": self.scope,
            "enabled": self.enabled,
        }
        if self.tier_models:
            payload["tier_models"] = dict(sorted(self.tier_models.items()))
        if self.cost_rank:
            payload["cost_rank"] = dict(sorted(self.cost_rank.items()))
        if self.api_key_env is not None:
            payload["api_key_env"] = self.api_key_env
        if self.verify_tls is False:
            payload["verify_tls"] = False
        return payload


@dataclass(frozen=True)
class VerifyGateSignalConfig:
    """Config for a single verify gate signal (lint, types, tests)."""
    command: str = "auto"
    required: bool = False
    timeout_seconds: int = 120


@dataclass(frozen=True)
class VerifyGateConfig:
    """Janitor-style verify gate run after file-writing subtasks."""
    enabled: bool = False
    mode: str = "warn"  # warn | block
    signals: dict[str, VerifyGateSignalConfig] = field(default_factory=lambda: {
        "lint": VerifyGateSignalConfig(command="auto", required=False),
        "types": VerifyGateSignalConfig(command="auto", required=True),
        "tests": VerifyGateSignalConfig(command="auto", required=True),
    })


@dataclass(frozen=True)
class WorktreeConfig:
    """Worktree isolation settings for execute_subtask."""
    enabled: bool = False
    ttl_hours: float = 24.0
    base_path: str = ""  # empty → default ~/.local/lib/threnody/worktrees


@dataclass(frozen=True)
class SessionConfig:
    """Persistent worker session settings (plan 10)."""
    enabled: bool = True
    idle_ttl: float = 300.0   # seconds before idle session is reaped
    max_per_provider: int = 8  # cap on concurrent sessions per provider

@dataclass(frozen=True)
class ConvergenceConfig:
    """Quality convergence loop settings (plan 14)."""
    enabled: bool = True
    default_min_score: float = 0.0  # 0.0 = off by default; subtask must opt in
    default_max_rounds: int = 3


@dataclass(frozen=True)
class ContextCompressionConfig:
    """Context compression settings (plan 15)."""
    enabled: bool = True
    layers: tuple[str, ...] = ("diff", "truncate", "dedup", "strip")
    max_context_chars: int = 8000
    min_ratio_to_log: float = 0.5


def _normalize_config_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _parse_basic_yaml_scalar(value: str) -> Any:
    lowered = value.lower()
    if value == "{}":
        return {}
    if value == "[]":
        return []
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _load_basic_yaml_mapping(text: str) -> dict[str, Any]:
    """Tiny YAML fallback for simple installer config maps when PyYAML is absent."""
    lines: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        line_without_comment = raw_line.split(" #", 1)[0].rstrip()
        stripped = line_without_comment.strip()
        if not stripped:
            continue
        indent = len(line_without_comment) - len(line_without_comment.lstrip(" "))
        lines.append((indent, stripped))

    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]

    for index, (indent, stripped) in enumerate(lines):
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]

        if stripped.startswith("-"):
            if not isinstance(current, list):
                log.warning("PyYAML fallback: sequence entry without a list parent ignored: %s", stripped)
                continue
            item = stripped[1:].strip()
            if not item:
                child: dict[str, Any] = {}
                current.append(child)
                stack.append((indent, child))
                continue
            if ":" in item and not item.startswith(("'", '"')):
                key, value = item.split(":", 1)
                key = key.strip()
                value = value.strip()
                child = {}
                current.append(child)
                if key:
                    if value:
                        child[key] = _parse_basic_yaml_scalar(value)
                    else:
                        next_is_list = (
                            index + 1 < len(lines)
                            and lines[index + 1][0] > indent
                            and lines[index + 1][1].startswith("-")
                        )
                        grandchild: Any = [] if next_is_list else {}
                        child[key] = grandchild
                        stack.append((indent, child))
                        stack.append((indent + 1, grandchild))
                        continue
                stack.append((indent, child))
                continue
            current.append(_parse_basic_yaml_scalar(item))
            continue

        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            continue
        if not isinstance(current, dict):
            log.warning("PyYAML fallback: mapping entry without a mapping parent ignored: %s", stripped)
            continue
        value = value.strip()
        if value:
            current[key] = _parse_basic_yaml_scalar(value)
            continue
        next_is_list = (
            index + 1 < len(lines)
            and lines[index + 1][0] > indent
            and lines[index + 1][1].startswith("-")
        )
        child = [] if next_is_list else {}
        current[key] = child
        stack.append((indent, child))
    return root


def _coerce_config_int(
    raw_value: Any,
    *,
    default: int,
    field_name: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        log.warning("%s: invalid integer value %r; using default %d", field_name, raw_value, default)
        return default
    if minimum is not None and value < minimum:
        log.warning(
            "%s: value %r is below minimum %d; using default %d",
            field_name,
            raw_value,
            minimum,
            default,
        )
        return default
    if maximum is not None and value > maximum:
        log.warning(
            "%s: value %r is above maximum %d; using default %d",
            field_name,
            raw_value,
            maximum,
            default,
        )
        return default
    return value


def _coerce_config_bool(raw_value: Any, *, default: bool, field_name: str) -> bool:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    log.warning("%s: expected boolean value %r; using default %s", field_name, raw_value, default)
    return default


def _normalize_routing_policy_mode(raw_mode: Any, *, field_name: str) -> str:
    mode = _normalize_config_text(raw_mode)
    if mode is None:
        return "default"
    mode = mode.lower()
    if mode == "strict":
        log.warning("%s: mode 'strict' is deprecated; use 'guarded'", field_name)
        return "guarded"
    if mode not in VALID_ROUTING_POLICY_MODES:
        log.warning("%s: invalid mode %r; using default", field_name, raw_mode)
        return "default"
    return mode


def _parse_tier_model_mapping(raw_value: Any, *, field_name: str) -> dict[str, str]:
    mapping = dict(DEFAULT_ROUTING_TIER_MODELS)
    if raw_value is None:
        return mapping
    if not isinstance(raw_value, Mapping):
        log.warning("%s: expected mapping; using defaults", field_name)
        return mapping
    for tier, raw_model in raw_value.items():
        if not isinstance(tier, str) or tier not in {"low", "medium", "high"}:
            log.warning("%s: invalid tier %r; skipping", field_name, tier)
            continue
        model = _normalize_config_text(raw_model)
        if model is None:
            log.warning("%s.%s: model must be a non-empty string; skipping", field_name, tier)
            continue
        mapping[tier] = model
    return mapping


def _parse_shell_routing_profile(
    shell_id: str,
    raw_profile: Any,
    *,
    global_mode: str,
) -> ShellRoutingProfile | None:
    canonical = normalize_routing_policy_shell_id(shell_id)
    if canonical is None:
        return None
    if canonical not in SUPPORTED_ROUTING_POLICY_SHELLS:
        log.warning("routing_policy.shells: unsupported shell %r; skipping", shell_id)
        return None
    if raw_profile is None:
        raw_profile = {}
    if not isinstance(raw_profile, Mapping):
        log.warning("routing_policy.shells.%s: expected mapping; skipping", shell_id)
        return None

    shell_mode_raw = raw_profile.get("mode")
    shell_mode = _normalize_routing_policy_mode(
        shell_mode_raw,
        field_name=f"routing_policy.shells.{canonical}.mode",
    ) if shell_mode_raw is not None else global_mode
    base = _recommended_shell_profile(canonical) if shell_mode in {"default", "custom"} else _mode_shell_profile(canonical, shell_mode)

    direct_edit_hooks = _coerce_config_bool(
        raw_profile.get("direct_edit_hooks"),
        default=base.direct_edit_hooks,
        field_name=f"routing_policy.shells.{canonical}.direct_edit_hooks",
    )
    if direct_edit_hooks and canonical not in ROUTING_POLICY_HOOK_CAPABLE_SHELLS:
        log.warning(
            "routing_policy.shells.%s.direct_edit_hooks is unsupported for this shell; disabling",
            canonical,
        )
        direct_edit_hooks = False

    return ShellRoutingProfile(
        shell_id=canonical,
        route_task_mandatory=_coerce_config_bool(
            raw_profile.get("route_task_mandatory"),
            default=base.route_task_mandatory,
            field_name=f"routing_policy.shells.{canonical}.route_task_mandatory",
        ),
        low_tier_execute_subtask=_coerce_config_bool(
            raw_profile.get("low_tier_execute_subtask"),
            default=base.low_tier_execute_subtask,
            field_name=f"routing_policy.shells.{canonical}.low_tier_execute_subtask",
        ),
        agent_transparency_required=_coerce_config_bool(
            raw_profile.get("agent_transparency_required"),
            default=base.agent_transparency_required,
            field_name=f"routing_policy.shells.{canonical}.agent_transparency_required",
        ),
        direct_edit_hooks=direct_edit_hooks,
        tier_model_mapping=_parse_tier_model_mapping(
            raw_profile.get("tier_model_mapping"),
            field_name=f"routing_policy.shells.{canonical}.tier_model_mapping",
        ),
    )


def parse_routing_policy_config(raw_policy: Any) -> RoutingPolicyConfig:
    """Parse routing_policy config, preserving legacy defaults when absent."""
    if raw_policy is None:
        return RoutingPolicyConfig()
    if not isinstance(raw_policy, Mapping):
        log.warning("routing_policy: expected mapping; using recommended defaults")
        return RoutingPolicyConfig()

    mode = _normalize_routing_policy_mode(raw_policy.get("mode"), field_name="routing_policy.mode")
    raw_shells = raw_policy.get("shells", {})
    shells: dict[str, ShellRoutingProfile] = {}
    if isinstance(raw_shells, Mapping):
        for raw_shell_id, raw_profile in raw_shells.items():
            if not isinstance(raw_shell_id, str):
                log.warning("routing_policy.shells: non-string shell id %r; skipping", raw_shell_id)
                continue
            profile = _parse_shell_routing_profile(raw_shell_id, raw_profile, global_mode=mode)
            if profile is not None:
                shells[profile.shell_id] = profile
    elif raw_shells is not None:
        log.warning("routing_policy.shells: expected mapping; ignoring")

    return RoutingPolicyConfig(mode=mode, shells=shells)


def _parse_routing_preference(
    raw_entry: Any,
    *,
    tier: str,
    index: int,
) -> RoutingPreference | None:
    if isinstance(raw_entry, str):
        text = _normalize_config_text(raw_entry)
        if text is None:
            log.warning(
                "preferred_routing: tier %r entry %d is blank; skipping",
                tier,
                index,
            )
            return None
        if "/" not in text:
            log.warning(
                "preferred_routing: tier %r entry %d must be a mapping or 'provider / model' string; skipping %r",
                tier,
                index,
                raw_entry,
            )
            return None
        provider_part, model_part = (part.strip() for part in text.split("/", 1))
        if not provider_part or not model_part:
            log.warning(
                "preferred_routing: tier %r entry %d must include both provider and model when using '/' syntax; skipping %r",
                tier,
                index,
                raw_entry,
            )
            return None
        return RoutingPreference(provider=provider_part, model=model_part)

    if not isinstance(raw_entry, Mapping):
        log.warning(
            "preferred_routing: tier %r entry %d must be a mapping or string; skipping %r",
            tier,
            index,
            raw_entry,
        )
        return None

    provider = _normalize_config_text(raw_entry.get("provider"))
    model = _normalize_config_text(raw_entry.get("model"))
    if provider is None and model is None:
        log.warning(
            "preferred_routing: tier %r entry %d must include provider and/or model; skipping %r",
            tier,
            index,
            raw_entry,
        )
        return None

    return RoutingPreference(provider=provider, model=model)


def _parse_preferred_routing_map(
    raw_preferences: Any,
    *,
    context: str = "preferred_routing",
) -> dict[str, list[RoutingPreference]]:
    if not isinstance(raw_preferences, Mapping):
        if raw_preferences not in (None, {}):
            log.warning("%s: expected mapping; skipping", context)
        return {}

    valid_tiers = {"low", "medium", "high"}
    validated_preferences: dict[str, list[RoutingPreference]] = {}
    for tier, raw_entries in raw_preferences.items():
        normalized_tier = tier.strip().lower() if isinstance(tier, str) else None
        if normalized_tier not in valid_tiers:
            log.warning(
                "%s: invalid tier %r (must be low/medium/high); skipping",
                context,
                tier,
            )
            continue
        if not isinstance(raw_entries, list):
            log.warning(
                "%s: tier %r must be a list; skipping",
                context,
                tier,
            )
            continue

        tier_preferences: list[RoutingPreference] = []
        for index, raw_entry in enumerate(raw_entries, start=1):
            parsed = _parse_routing_preference(
                raw_entry,
                tier=normalized_tier,
                index=index,
            )
            if parsed is not None:
                tier_preferences.append(parsed)

        if tier_preferences:
            validated_preferences[normalized_tier] = tier_preferences
    return validated_preferences


def _parse_preferred_routing_by_caller(raw_callers: Any) -> dict[str, dict[str, list[RoutingPreference]]]:
    if not isinstance(raw_callers, Mapping):
        if raw_callers not in (None, {}):
            log.warning("preferred_routing_by_caller: expected mapping; skipping")
        return {}

    parsed: dict[str, dict[str, list[RoutingPreference]]] = {}
    for caller_key, raw_preferences in raw_callers.items():
        if not isinstance(caller_key, str):
            log.warning("preferred_routing_by_caller: skipping non-string caller key %r", caller_key)
            continue
        normalized_caller = normalize_caller_id(caller_key)
        if not normalized_caller:
            log.warning("preferred_routing_by_caller: caller key %r is blank; skipping", caller_key)
            continue
        caller_preferences = _parse_preferred_routing_map(
            raw_preferences,
            context=f"preferred_routing_by_caller.{normalized_caller}",
        )
        if caller_preferences:
            parsed[normalized_caller] = caller_preferences
    return parsed



# ---------------------------------------------------------------------------
# Usage-window threshold routing
# ---------------------------------------------------------------------------

@dataclass
class UsageWindowEntry:
    hours: float
    budget_tokens: int | None
    threshold: float
    action: str  # "prefer_alternatives" | "cost_rank_boost" | "hard_exclude"


@dataclass
class ProviderUsageWindowConfig:
    windows: list[UsageWindowEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Routing exceptions
# ---------------------------------------------------------------------------

DEFAULT_ROUTING_EXCEPTION_FILETYPES: tuple[str, ...] = (".md", ".mdc")

DEFAULT_ROUTING_EXCEPTION_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "GEMINI.md",
    "AGENTS.md",
    "CONVENTIONS.md",
    "AIDER_CONVENTIONS.md",
    "copilot-instructions.md",
    ".github/copilot-instructions.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".cline_rules",
)


@dataclass
class ResilienceConfig:
    """Resilience and circuit-breaker settings. All fields have safe defaults."""
    retry_attempts: int = 3
    retry_base_delay_s: float = 0.5
    retry_max_delay_s: float = 8.0
    retry_jitter_ratio: float = 0.3
    cb_failure_threshold: int = 3
    cb_open_seconds: float = 120.0
    cb_quota_open_seconds: float = 1800.0
    cb_auth_open_seconds: float = 600.0
    auth_probe_ttl_seconds: float = 600.0
    auth_probe_enabled: bool = True
    stderr_snippet_chars: int = 2000
    health_probe_interval_s: float = 30.0


def _dedupe_patterns(*groups: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for pattern in group:
            normalized = str(pattern).strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(normalized)
    return result


@dataclass
class RoutingExceptions:
    """Static routing bypass rules loaded from config.yaml."""
    skills: list[str] = field(default_factory=list)
    filetypes: list[str] = field(
        default_factory=lambda: list(DEFAULT_ROUTING_EXCEPTION_FILETYPES)
    )
    projects: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=lambda: list(DEFAULT_ROUTING_EXCEPTION_PATHS))


# ---------------------------------------------------------------------------
# Full config loader
# ---------------------------------------------------------------------------
@dataclass
class TGsConfig:
    """Complete Threnody configuration."""
    # Complexity scoring
    signals: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_COMPLEXITY_SIGNALS))
    signal_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SIGNAL_WEIGHTS))
    base_score: float = DEFAULT_BASE_SCORE
    overrides: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_OVERRIDES))

    # Thresholds
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)

    # Orchestrator
    planner_model: str = DEFAULT_PLANNER_MODEL
    planner_timeout: int = DEFAULT_PLANNER_TIMEOUT
    parallelism: ParallelismConfig = field(default_factory=ParallelismConfig)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)

    # Cache
    db_path: Path = field(default_factory=lambda: DB_PATH)
    db_backup_keep: int = 3
    plan_cache_ttl_hours: int = PLAN_CACHE_TTL_HOURS
    result_cache_ttl_hours: int = RESULT_CACHE_TTL_HOURS

    # Synthesis behaviour (orchestrator end-of-run merge)
    synthesis_map_reduce: str = "auto"  # off | auto | always
    synthesis_chunk_chars: int = 12000

    # Token ceilings
    token_ceiling_low: int = TOKEN_CEILING_LOW
    token_ceiling_medium: int = TOKEN_CEILING_MEDIUM
    token_ceiling_high: int = TOKEN_CEILING_HIGH

    # Write safety (Path traversal protection - Wave 3: FNDX-04)
    write_safety_trusted_bases: list[Path] = field(default_factory=lambda: [Path.cwd()])

    # Extra trusted path prefixes for out-of-workspace writes (exact prefix match).
    # Format: list of absolute path strings in config.yaml under write_safety.extra_paths.
    write_safety_extra_paths: list[Path] = field(default_factory=list)

    # Inline code review gate
    code_review: bool = False
    code_review_tier: str = "all"
    auto_approve_timeout: int = 30

    # Phase 9: User-driven model tier overrides (DISC-05)
    # Format: {"model-id": "tier"} — e.g. {"gpt-4-turbo": "low"}
    model_tier_pins: dict[str, str] = field(default_factory=dict)

    # Provider billing/cost overrides
    # Format: {"provider-id": {"low": ProviderCostOverride(...)}}
    provider_cost_overrides: dict[str, dict[str, ProviderCostOverride]] = field(default_factory=dict)

    # Per-tier execution timeouts (seconds).  Parsed from providers.timeouts
    # in config.yaml.  Used by execute_subtask when no explicit timeout is given.
    tier_timeouts: dict[str, int] = field(
        default_factory=lambda: {"low": 60, "medium": 120, "high": 180},
    )

    # Per-provider per-tier timeout overrides (seconds).  Parsed from
    # providers.timeout_overrides in config.yaml.  When set for a provider,
    # execute_cheapest uses this timeout instead of the tier default for that
    # specific provider.  Deadline-based calls are unaffected.
    # Format: {"provider-id": {"low": 60, "medium": 600, "high": 720}}
    provider_timeout_overrides: dict[str, dict[str, int]] = field(default_factory=dict)

    # Provider per-tier effort defaults (optional)
    # Format: {"provider-id": {"low": "quick", "medium": "standard", "high": "thorough"}}
    # Values are strings and optional; absent entries mean no default is configured.
    provider_effort_defaults: dict[str, dict[str, str]] = field(default_factory=dict)

    # Spillover configuration (Wave 1: expose capacity metadata)
    # Format: SpilloverConfig(enabled=True, per_provider_concurrency={"provider-id": 3})
    spillover: "SpilloverConfig" = field(default_factory=lambda: SpilloverConfig())

    # Ordered tie-break preferences for equal-cost candidates in the same tier.
    # Format: {"low": [RoutingPreference(provider="claude-code", model="haiku")]}
    preferred_routing: dict[str, list[RoutingPreference]] = field(default_factory=dict)

    # Caller-scoped ordered tie-break preferences. When a caller has entries here,
    # they override the global preferred_routing map for that caller only.
    # Format: {"claude-code": {"low": [RoutingPreference(provider="mistral-vibe")]}}
    preferred_routing_by_caller: dict[str, dict[str, list[RoutingPreference]]] = field(default_factory=dict)

    # Configured HTTP-backed local/network endpoints.
    endpoint_providers: list[EndpointProviderConfig] = field(default_factory=list)

    # Per-caller provider allowlists.
    # Format: {"caller-id": ["provider-id-1", "provider-id-2"]}
    # Keys and values are normalized to lowercase. Absent caller = unrestricted.
    caller_provider_allowlists: dict[str, list[str]] = field(default_factory=dict)

    # Per-provider usage-window threshold routing.
    # Format: {"provider-id": ProviderUsageWindowConfig(windows=[...])}
    provider_usage_windows: dict[str, ProviderUsageWindowConfig] = field(default_factory=dict)

    # Globally disabled providers — skipped during routing regardless of detection.
    # Populated by settings wizard. Format: list of lowercase provider IDs.
    disabled_providers: list[str] = field(default_factory=list)

    # Providers that may execute via subprocess despite router-only defaults.
    # Format: list of lowercase provider IDs (e.g. claude-code).
    router_only_allow_execution: list[str] = field(default_factory=list)

    # Opt-in utility delegation for execute_subtask (meta-harness default: host-native only).
    delegation_utilities_enabled: bool = False

    # Provider ids allowed as execute_subtask targets when delegation_utilities_enabled.
    # Local loopback endpoints (ollama, configured local HTTPS) are always allowed when enabled.
    delegation_utilities: list[str] = field(
        default_factory=lambda: list(DEFAULT_DELEGATION_UTILITIES),
    )

    # execute_swarm host execution: host_native (plan handoff) or delegate (subprocess).
    swarm_host_execution_mode: str = "host_native"
    swarm_host_execution_mode_by_caller: dict[str, str] = field(default_factory=dict)

    # Janitor-style verify gate (plan 04).
    verify_gate: VerifyGateConfig = field(default_factory=VerifyGateConfig)

    # Worktree isolation for execute_subtask (plan 06).
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)

    # Persistent worker sessions (plan 10).
    session: SessionConfig = field(default_factory=SessionConfig)

    # Quality convergence loops (plan 14).
    convergence: ConvergenceConfig = field(default_factory=ConvergenceConfig)

    # Context compression (plan 15).
    context_compression: ContextCompressionConfig = field(default_factory=ContextCompressionConfig)

    # Static routing bypass rules (from config.yaml under routing_exceptions:).
    routing_exceptions: RoutingExceptions = field(default_factory=RoutingExceptions)

    # Shell-specific instruction/enforcement policy.
    routing_policy: RoutingPolicyConfig = field(default_factory=RoutingPolicyConfig)

    # Resilience: circuit breaker, retry, auth probe settings
    resilience: ResilienceConfig = field(default_factory=ResilienceConfig)

    # Improvement 1: immediate escalation retry when token ceiling exceeded
    escalation_retry_enabled: bool = True

    # Improvement 2: speculative execution at medium→high boundary
    speculation_require_free_lower: bool = True

    # Improvement 3: output quality check and retry
    output_quality_retry_enabled: bool = True
    quality_check_incomplete_output: bool = False

    # Improvement 5: reasoning/creativity scoring axis
    reasoning_scoring_enabled: bool = True

    # Surgical edit configuration (rewrite / blocks modes)
    surgical_edit_max_file_bytes: int = SURGICAL_EDIT_MAX_FILE_BYTES
    surgical_edit_blocks_max_file_bytes: int = SURGICAL_EDIT_BLOCKS_MAX_FILE_BYTES
    surgical_edit_length_ratio_min: float = SURGICAL_EDIT_LENGTH_RATIO_MIN
    execute_subtask_guard_strict: bool = False
    auto_cascade_mode: bool = True

    @property
    def swarm_max_agents(self) -> int:
        """Return the effective swarm agent hard cap for this config."""
        return max(
            1,
            _coerce_config_int(
                self.parallelism.swarm_max_agents,
                default=12,
                field_name="parallelism.swarm_max_agents",
                minimum=1,
            ),
        )

    @classmethod
    def defaults(cls) -> "TGsConfig":
        """Return a clamped default config."""
        cfg = cls()
        cfg.thresholds.clamp()
        return cfg

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "TGsConfig":
        """Load config from YAML file, falling back to defaults."""
        path = path or CONFIG_YAML
        if not path.exists():
            log.info("No config.yaml found, using defaults")
            return cls.defaults()
        if yaml is None:
            log.warning("PyYAML is unavailable; using limited config parser for %s", path)
            raw = _load_basic_yaml_mapping(path.read_text(encoding="utf-8"))
        else:
            raw = yaml.safe_load(path.read_text())
        if raw is None:
            raw = {}
        elif not isinstance(raw, Mapping):
            log.warning("Config root in %s must be a mapping; using defaults", path)
            raw = {}
        parallelism_raw = raw.get("parallelism", {})
        if not isinstance(parallelism_raw, Mapping):
            parallelism_raw = {}
        swarm_raw = raw.get("swarm", {})
        if not isinstance(swarm_raw, Mapping):
            swarm_raw = {}
        budgets_raw = raw.get("budgets", {})
        if not isinstance(budgets_raw, Mapping):
            budgets_raw = {}
        orchestrator_raw = raw.get("orchestrator", {})
        if not isinstance(orchestrator_raw, Mapping):
            orchestrator_raw = {}
        cache_raw = raw.get("cache", {})
        if not isinstance(cache_raw, Mapping):
            cache_raw = {}
        cfg = cls(
            signals=raw.get("signals", dict(DEFAULT_COMPLEXITY_SIGNALS)),
            signal_weights=raw.get("signal_weights", dict(DEFAULT_SIGNAL_WEIGHTS)),
            base_score=raw.get("base_score", DEFAULT_BASE_SCORE),
            overrides=raw.get("overrides", dict(DEFAULT_OVERRIDES)),
            planner_model=orchestrator_raw.get("planner_model", DEFAULT_PLANNER_MODEL),
            planner_timeout=orchestrator_raw.get("planner_timeout_seconds", DEFAULT_PLANNER_TIMEOUT),
            parallelism=ParallelismConfig(
                enabled=parallelism_raw.get("enabled", True),
                max_workers=parallelism_raw.get(
                    "max_workers",
                    UNLIMITED_PARALLELISM,
                ),
                swarm_max_agents=_coerce_config_int(
                    swarm_raw.get("max_agents", 12),
                    default=12,
                    field_name="swarm.max_agents",
                    minimum=1,
                ),
                speculation_workers=_coerce_config_int(
                    parallelism_raw.get("speculation_workers", 1),
                    default=1,
                    field_name="parallelism.speculation_workers",
                    minimum=1,
                    maximum=8,
                ),
                warm_path_workers=_coerce_config_int(
                    parallelism_raw.get("warm_path_workers", 2),
                    default=2,
                    field_name="parallelism.warm_path_workers",
                    minimum=1,
                    maximum=8,
                ),
            ),
            budgets=BudgetConfig(
                default_hard_cap_tokens=budgets_raw.get("default_hard_cap_tokens", 1000),
                default_soft_warning_pct=budgets_raw.get("default_soft_warning_pct", 0.8),
            ),
            plan_cache_ttl_hours=cache_raw.get("ttl_hours", PLAN_CACHE_TTL_HOURS),
            db_backup_keep=_coerce_config_int(cache_raw.get("backup_keep", 3), default=3, field_name="cache.backup_keep", minimum=1),
        )
        cfg.code_review = raw.get("code_review", False) is True
        raw_review_tier = raw.get("code_review_tier", "all")
        if isinstance(raw_review_tier, str) and raw_review_tier in {"all", "medium", "high"}:
            cfg.code_review_tier = raw_review_tier
        else:
            cfg.code_review_tier = "all"
        raw_auto_approve_timeout = raw.get("auto_approve_timeout", 30)
        if type(raw_auto_approve_timeout) is int and raw_auto_approve_timeout >= 0:
            cfg.auto_approve_timeout = raw_auto_approve_timeout
        else:
            cfg.auto_approve_timeout = 30

        cfg.routing_policy = parse_routing_policy_config(raw.get("routing_policy"))

        cfg.escalation_retry_enabled = raw.get("escalation_retry_enabled", True) is True
        cfg.speculation_require_free_lower = raw.get("speculation_require_free_lower", True) is True
        cfg.output_quality_retry_enabled = raw.get("output_quality_retry_enabled", True) is True
        cfg.quality_check_incomplete_output = raw.get("quality_check_incomplete_output", False) is True
        cfg.reasoning_scoring_enabled = raw.get("reasoning_scoring_enabled", True) is True

        synthesis_mode = orchestrator_raw.get("synthesis_map_reduce", "auto")
        if isinstance(synthesis_mode, str) and synthesis_mode.strip().lower() in {
            "off",
            "auto",
            "always",
        }:
            cfg.synthesis_map_reduce = synthesis_mode.strip().lower()
        else:
            cfg.synthesis_map_reduce = "auto"
        cfg.synthesis_chunk_chars = _coerce_config_int(
            orchestrator_raw.get("synthesis_chunk_chars", 12000),
            default=12000,
            field_name="orchestrator.synthesis_chunk_chars",
            minimum=1024,
            maximum=256_000,
        )

        verify_gate_raw = raw.get("verify_gate", {})
        if isinstance(verify_gate_raw, Mapping):
            mode = verify_gate_raw.get("mode", "warn")
            if mode not in {"warn", "block"}:
                log.warning("verify_gate.mode must be warn or block; using warn")
                mode = "warn"
            default_signals = VerifyGateConfig().signals
            raw_signals = verify_gate_raw.get("signals", {})
            parsed_signals: dict[str, VerifyGateSignalConfig] = {}
            if isinstance(raw_signals, Mapping):
                signal_names = set(default_signals) | {
                    str(name) for name in raw_signals
                }
                for signal_name in sorted(signal_names):
                    default_signal = default_signals.get(
                        signal_name,
                        VerifyGateSignalConfig(),
                    )
                    signal_raw = raw_signals.get(signal_name, {})
                    if not isinstance(signal_raw, Mapping):
                        log.warning(
                            "verify_gate.signals.%s must be a mapping; using defaults",
                            signal_name,
                        )
                        signal_raw = {}
                    command = signal_raw.get("command", default_signal.command)
                    if not isinstance(command, str):
                        command = default_signal.command
                    timeout_seconds = _coerce_config_int(
                        signal_raw.get(
                            "timeout_seconds",
                            default_signal.timeout_seconds,
                        ),
                        default=default_signal.timeout_seconds,
                        field_name=f"verify_gate.signals.{signal_name}.timeout_seconds",
                        minimum=1,
                        maximum=3600,
                    )
                    parsed_signals[signal_name] = VerifyGateSignalConfig(
                        command=command.strip(),
                        required=signal_raw.get(
                            "required",
                            default_signal.required,
                        ) is True,
                        timeout_seconds=timeout_seconds,
                    )
            else:
                parsed_signals = dict(default_signals)
            cfg.verify_gate = VerifyGateConfig(
                enabled=verify_gate_raw.get("enabled", False) is True,
                mode=mode,
                signals=parsed_signals,
            )

        # Phase 9: Load user model tier overrides from models.tier_pins
        models_section = raw.get("models", {})
        if isinstance(models_section, dict):
            raw_pins = models_section.get("tier_pins", {})
            if isinstance(raw_pins, dict):
                valid_tiers = {"low", "medium", "high"}
                validated_pins: dict[str, str] = {}
                for model_id, tier in raw_pins.items():
                    if not isinstance(model_id, str) or not isinstance(tier, str):
                        log.warning("model_tier_pins: skipping non-string entry %r: %r", model_id, tier)
                        continue
                    if tier not in valid_tiers:
                        log.warning("model_tier_pins: invalid tier %r for model %r (must be low/medium/high); skipping", tier, model_id)
                        continue
                    validated_pins[str(model_id)] = tier
                cfg.model_tier_pins = validated_pins

        # Parse spillover configuration (Wave 1)
        providers_section = raw.get("providers", {})
        spillover_raw = None
        if isinstance(raw, Mapping):
            # Top-level spillover
            spillover_raw = raw.get("spillover") or None
        if isinstance(providers_section, Mapping) and spillover_raw is None:
            spillover_raw = providers_section.get("spillover")

        if isinstance(spillover_raw, Mapping):
            enabled = spillover_raw.get("enabled", True) is True
            per_provider = spillover_raw.get("per_provider_concurrency", {})
            validated: dict[str, int | None] = {}
            if isinstance(per_provider, Mapping):
                for pid, val in per_provider.items():
                    if not isinstance(pid, str):
                        log.warning("spillover: skipping non-string provider key %r", pid)
                        continue
                    if val is None:
                        validated[pid.strip().lower()] = None
                        continue
                    try:
                        ival = int(val)
                        if ival < 0:
                            log.warning("spillover: invalid capacity %r for provider %r; skipping", val, pid)
                            continue
                        validated[pid.strip().lower()] = ival
                    except (TypeError, ValueError):
                        log.warning("spillover: invalid capacity %r for provider %r; skipping", val, pid)
                        continue
            cfg.spillover = SpilloverConfig(enabled=enabled, per_provider_concurrency=validated)
        else:
            # Ensure default SpilloverConfig is present
            cfg.spillover = getattr(cfg, "spillover", SpilloverConfig())

        providers_section = raw.get("providers", {})
        if isinstance(providers_section, Mapping):
            raw_endpoint_providers = providers_section.get("endpoint_providers", [])
            if isinstance(raw_endpoint_providers, list):
                valid_tiers = {"low", "medium", "high"}
                validated_endpoints: list[EndpointProviderConfig] = []
                seen_endpoint_names: set[str] = set()
                for index, raw_endpoint in enumerate(raw_endpoint_providers, start=1):
                    if not isinstance(raw_endpoint, Mapping):
                        log.warning(
                            "endpoint_providers: entry %d must be a mapping; skipping %r",
                            index,
                            raw_endpoint,
                        )
                        continue

                    name = _normalize_config_text(raw_endpoint.get("name"))
                    if name is None:
                        log.warning(
                            "endpoint_providers: entry %d is missing a non-empty name; skipping",
                            index,
                        )
                        continue

                    kind = _normalize_endpoint_kind(raw_endpoint.get("kind"))
                    if kind not in VALID_ENDPOINT_PROVIDER_KINDS:
                        log.warning(
                            "endpoint_providers: provider %r has invalid kind %r; skipping",
                            name,
                            raw_endpoint.get("kind"),
                        )
                        continue

                    base_url = _normalize_config_text(raw_endpoint.get("base_url"))
                    if base_url is None:
                        log.warning(
                            "endpoint_providers: provider %r is missing base_url; skipping",
                            name,
                        )
                        continue

                    scope = _normalize_config_text(raw_endpoint.get("scope")) or "network"
                    scope = scope.lower()
                    if scope not in VALID_ENDPOINT_PROVIDER_SCOPES:
                        log.warning(
                            "endpoint_providers: provider %r has invalid scope %r; skipping",
                            name,
                            raw_endpoint.get("scope"),
                        )
                        continue

                    enabled = raw_endpoint.get("enabled", True) is True
                    api_key_env = _normalize_config_text(raw_endpoint.get("api_key_env"))
                    raw_verify_tls = raw_endpoint.get("verify_tls", True)
                    if isinstance(raw_verify_tls, bool):
                        verify_tls = raw_verify_tls
                    else:
                        log.warning(
                            "endpoint_providers: provider %r verify_tls must be a boolean; using default true",
                            name,
                        )
                        verify_tls = True
                    if "allow_insecure" in raw_endpoint:
                        log.warning(
                            "endpoint_providers: provider %r uses removed field 'allow_insecure'; network endpoints now require https",
                            name,
                        )

                    normalized_name = name.strip().lower()
                    if normalized_name in seen_endpoint_names:
                        log.warning(
                            "endpoint_providers: duplicate provider name %r; skipping later entry",
                            name,
                        )
                        continue

                    valid_url, reason = _validate_endpoint_base_url(
                        base_url,
                        scope=scope,
                    )
                    if not valid_url:
                        log.warning(
                            "endpoint_providers: provider %r has invalid base_url %r (%s); skipping",
                            name,
                            base_url,
                            reason,
                        )
                        continue

                    tier_models: dict[str, str] = {}
                    raw_tier_models = raw_endpoint.get("tier_models", {})
                    if isinstance(raw_tier_models, Mapping):
                        for tier, raw_model in raw_tier_models.items():
                            if not isinstance(tier, str) or tier not in valid_tiers:
                                log.warning(
                                    "endpoint_providers: provider %r has invalid tier_models tier %r; skipping field",
                                    name,
                                    tier,
                                )
                                continue
                            model = _normalize_config_text(raw_model)
                            if model is None:
                                log.warning(
                                    "endpoint_providers: provider %r tier_models.%s must be a non-empty string; skipping field",
                                    name,
                                    tier,
                                )
                                continue
                            tier_models[tier] = model

                    if scope == "network" and not tier_models:
                        log.warning(
                            "endpoint_providers: network provider %r must define tier_models; skipping",
                            name,
                        )
                        continue

                    cost_rank: dict[str, int] = {}
                    raw_cost_rank = raw_endpoint.get("cost_rank", {})
                    if isinstance(raw_cost_rank, Mapping):
                        for tier, raw_rank in raw_cost_rank.items():
                            if not isinstance(tier, str) or tier not in valid_tiers:
                                log.warning(
                                    "endpoint_providers: provider %r has invalid cost_rank tier %r; skipping field",
                                    name,
                                    tier,
                                )
                                continue
                            if type(raw_rank) is not int or raw_rank < 0:
                                log.warning(
                                    "endpoint_providers: provider %r cost_rank.%s must be a non-negative int; skipping field",
                                    name,
                                    tier,
                                )
                                continue
                            cost_rank[tier] = raw_rank

                    validated_endpoints.append(
                        EndpointProviderConfig(
                            name=name,
                            kind=kind,
                            base_url=base_url,
                            scope=scope,
                            enabled=enabled,
                            tier_models=tier_models,
                            cost_rank=cost_rank,
                            api_key_env=api_key_env,
                            verify_tls=verify_tls,
                        )
                    )
                    seen_endpoint_names.add(normalized_name)
                cfg.endpoint_providers = validated_endpoints

            raw_cost_overrides = providers_section.get("cost_overrides", {})
            if isinstance(raw_cost_overrides, Mapping):
                valid_tiers = {"low", "medium", "high"}
                validated_overrides: dict[str, dict[str, ProviderCostOverride]] = {}
                for provider_id, raw_provider_overrides in raw_cost_overrides.items():
                    if not isinstance(provider_id, str):
                        log.warning(
                            "provider_cost_overrides: skipping non-string provider key %r",
                            provider_id,
                        )
                        continue
                    if not isinstance(raw_provider_overrides, Mapping):
                        log.warning(
                            "provider_cost_overrides: provider %r must map to tier objects; skipping",
                            provider_id,
                        )
                        continue

                    normalized_provider = provider_id.strip().lower()
                    provider_overrides: dict[str, ProviderCostOverride] = {}
                    for tier, raw_override in raw_provider_overrides.items():
                        if not isinstance(tier, str) or tier not in valid_tiers:
                            log.warning(
                                "provider_cost_overrides: invalid tier %r for provider %r (must be low/medium/high); skipping",
                                tier,
                                provider_id,
                            )
                            continue
                        if not isinstance(raw_override, Mapping):
                            log.warning(
                                "provider_cost_overrides: provider %r tier %r must be a mapping; skipping",
                                provider_id,
                                tier,
                            )
                            continue

                        raw_cost_rank = raw_override.get("cost_rank")
                        cost_rank: int | None = None
                        if raw_cost_rank is not None:
                            if type(raw_cost_rank) is not int or raw_cost_rank < 0:
                                log.warning(
                                    "provider_cost_overrides: invalid cost_rank %r for provider %r tier %r; skipping field",
                                    raw_cost_rank,
                                    provider_id,
                                    tier,
                                )
                            else:
                                cost_rank = raw_cost_rank

                        raw_billing_tier = raw_override.get("billing_tier")
                        billing_tier: str | None = None
                        if raw_billing_tier is not None:
                            if not isinstance(raw_billing_tier, str) or raw_billing_tier not in VALID_BILLING_TIERS:
                                log.warning(
                                    "provider_cost_overrides: invalid billing_tier %r for provider %r tier %r; skipping field",
                                    raw_billing_tier,
                                    provider_id,
                                    tier,
                                )
                            else:
                                billing_tier = raw_billing_tier

                        raw_provider_cost_hint = raw_override.get(
                            "provider_cost_hint",
                            raw_override.get("billing_note"),
                        )
                        provider_cost_hint: str | None = None
                        if raw_provider_cost_hint is not None:
                            if not isinstance(raw_provider_cost_hint, str):
                                log.warning(
                                    "provider_cost_overrides: invalid provider_cost_hint %r for provider %r tier %r; skipping field",
                                    raw_provider_cost_hint,
                                    provider_id,
                                    tier,
                                )
                            else:
                                provider_cost_hint = raw_provider_cost_hint.strip() or None

                        if billing_tier == "free" and cost_rank != 0:
                            if cost_rank is not None and cost_rank != 0:
                                log.warning(
                                    "provider_cost_overrides: provider %r tier %r sets billing_tier=free but cost_rank=%r; forcing cost_rank=0",
                                    provider_id,
                                    tier,
                                    cost_rank,
                                )
                            cost_rank = 0
                        elif cost_rank == 0 and billing_tier not in (None, "free"):
                            log.warning(
                                "provider_cost_overrides: provider %r tier %r sets cost_rank=0 with billing_tier=%r; forcing billing_tier='free'",
                                provider_id,
                                tier,
                                billing_tier,
                            )
                            billing_tier = "free"

                        if cost_rank is None and billing_tier is None and provider_cost_hint is None:
                            continue

                        provider_overrides[tier] = ProviderCostOverride(
                            cost_rank=cost_rank,
                            billing_tier=billing_tier,
                            provider_cost_hint=provider_cost_hint,
                        )

                    if provider_overrides:
                        validated_overrides[normalized_provider] = provider_overrides
                cfg.provider_cost_overrides = validated_overrides

            # Per-tier execution timeouts (providers.timeouts.low/medium/high)
            # plus provider-specific timeout overrides
            # (providers.timeouts.<provider>.<tier>).
            raw_timeouts = providers_section.get("timeouts", {})
            if isinstance(raw_timeouts, Mapping):
                valid_tiers = {"low", "medium", "high"}
                validated_timeout_overrides: dict[str, dict[str, int]] = {}
                for tier, raw_val in raw_timeouts.items():
                    if not isinstance(tier, str):
                        log.warning(
                            "tier_timeouts: invalid non-string key %r; skipping",
                            tier,
                        )
                        continue
                    normalized_key = tier.strip().lower()
                    if normalized_key in valid_tiers:
                        if type(raw_val) is not int or raw_val < 1:
                            log.warning(
                                "tier_timeouts: invalid value %r for tier %r (must be positive int); skipping",
                                raw_val,
                                tier,
                            )
                            continue
                        cfg.tier_timeouts[normalized_key] = raw_val
                        continue
                    if not isinstance(raw_val, Mapping):
                        log.warning(
                            "provider_timeout_overrides: provider %r must map to tier objects; skipping",
                            tier,
                        )
                        continue
                    provider_timeouts: dict[str, int] = {}
                    for provider_tier, provider_raw_val in raw_val.items():
                        if not isinstance(provider_tier, str) or provider_tier not in valid_tiers:
                            log.warning(
                                "provider_timeout_overrides: invalid tier %r for provider %r"
                                " (must be low/medium/high); skipping",
                                provider_tier,
                                tier,
                            )
                            continue
                        if type(provider_raw_val) is not int or provider_raw_val < 1:
                            log.warning(
                                "provider_timeout_overrides: invalid value %r for provider %r"
                                " tier %r (must be positive int); skipping",
                                provider_raw_val,
                                tier,
                                provider_tier,
                            )
                            continue
                        provider_timeouts[provider_tier] = provider_raw_val
                    if provider_timeouts:
                        validated_timeout_overrides[normalized_key] = provider_timeouts
                if validated_timeout_overrides:
                    cfg.provider_timeout_overrides.update(validated_timeout_overrides)

            # Per-provider per-tier timeout overrides (providers.timeout_overrides)
            raw_timeout_overrides = providers_section.get("timeout_overrides", {})
            if isinstance(raw_timeout_overrides, Mapping):
                valid_tiers = {"low", "medium", "high"}
                validated_timeout_overrides = dict(cfg.provider_timeout_overrides)
                for provider_id, raw_provider_timeouts in raw_timeout_overrides.items():
                    if not isinstance(provider_id, str):
                        log.warning(
                            "provider_timeout_overrides: skipping non-string provider key %r",
                            provider_id,
                        )
                        continue
                    if not isinstance(raw_provider_timeouts, Mapping):
                        log.warning(
                            "provider_timeout_overrides: provider %r must map to tier objects; skipping",
                            provider_id,
                        )
                        continue
                    normalized_provider = provider_id.strip().lower()
                    provider_timeouts: dict[str, int] = {}
                    for tier, raw_val in raw_provider_timeouts.items():
                        if not isinstance(tier, str) or tier not in valid_tiers:
                            log.warning(
                                "provider_timeout_overrides: invalid tier %r for provider %r"
                                " (must be low/medium/high); skipping",
                                tier,
                                provider_id,
                            )
                            continue
                        if type(raw_val) is not int or raw_val < 1:
                            log.warning(
                                "provider_timeout_overrides: invalid value %r for provider %r"
                                " tier %r (must be positive int); skipping",
                                raw_val,
                                provider_id,
                                tier,
                            )
                            continue
                        provider_timeouts[tier] = raw_val
                    if provider_timeouts:
                        validated_timeout_overrides[normalized_provider] = provider_timeouts
                cfg.provider_timeout_overrides = validated_timeout_overrides

            # Optional: provider per-tier effort defaults
            # Format mirrors provider_cost_overrides but maps tier -> effort string
            raw_efforts = providers_section.get("effort_defaults", {})
            if isinstance(raw_efforts, Mapping):
                valid_tiers = {"low", "medium", "high"}
                validated_efforts: dict[str, dict[str, str]] = {}
                for provider_id, raw_provider_efforts in raw_efforts.items():
                    if not isinstance(provider_id, str):
                        log.warning(
                            "provider_effort_defaults: skipping non-string provider key %r",
                            provider_id,
                        )
                        continue
                    if not isinstance(raw_provider_efforts, Mapping):
                        log.warning(
                            "provider_effort_defaults: provider %r must map to tier objects; skipping",
                            provider_id,
                        )
                        continue

                    normalized_provider = provider_id.strip().lower()
                    provider_efforts: dict[str, str] = {}
                    for tier, raw_eff in raw_provider_efforts.items():
                        if not isinstance(tier, str) or tier not in valid_tiers:
                            log.warning(
                                "provider_effort_defaults: invalid tier %r for provider %r (must be low/medium/high); skipping",
                                tier,
                                provider_id,
                            )
                            continue
                        if raw_eff is None:
                            continue
                        if not isinstance(raw_eff, str):
                            log.warning(
                                "provider_effort_defaults: provider %r tier %r must be a string; skipping",
                                provider_id,
                                tier,
                            )
                            continue
                        effort_val = raw_eff.strip()
                        if effort_val:
                            provider_efforts[tier] = effort_val

                    if provider_efforts:
                        validated_efforts[normalized_provider] = provider_efforts
                cfg.provider_effort_defaults = validated_efforts

            raw_preferences = providers_section.get("preferred_routing", {})
            cfg.preferred_routing = _parse_preferred_routing_map(raw_preferences)

            raw_caller_preferences = (
                providers_section.get("preferred_routing_by_caller")
                or providers_section.get("caller_preferred_routing")
                or {}
            )
            cfg.preferred_routing_by_caller = _parse_preferred_routing_by_caller(
                raw_caller_preferences,
            )

            # Optional: per-caller provider allowlists
            raw_caller_allowlists = providers_section.get("caller_allowlists", {})
            if isinstance(raw_caller_allowlists, Mapping):
                parsed_allowlists: dict[str, list[str]] = {}
                for caller_key, provider_list in raw_caller_allowlists.items():
                    if not isinstance(caller_key, str):
                        log.warning(
                            "caller_allowlists: skipping non-string caller key %r",
                            caller_key,
                        )
                        continue
                    if not isinstance(provider_list, list):
                        log.warning(
                            "caller_allowlists: caller %r must map to a list; skipping",
                            caller_key,
                        )
                        continue
                    normalized_caller = normalize_caller_id(caller_key)
                    if not normalized_caller:
                        continue
                    normalized_providers = [
                        str(p).strip().lower() for p in provider_list if p is not None
                    ]
                    if normalized_providers:
                        parsed_allowlists[normalized_caller] = normalized_providers
                cfg.caller_provider_allowlists = parsed_allowlists

            # Optional: globally disabled providers
            raw_disabled = providers_section.get("disabled", [])
            if isinstance(raw_disabled, list):
                cfg.disabled_providers = [
                    str(p).strip().lower() for p in raw_disabled if p is not None
                ]

            raw_router_only_allow = providers_section.get("router_only_allow_execution", [])
            if isinstance(raw_router_only_allow, list):
                cfg.router_only_allow_execution = [
                    str(p).strip().lower() for p in raw_router_only_allow if p is not None
                ]

            raw_delegation_enabled = providers_section.get("delegation_utilities_enabled")
            if isinstance(raw_delegation_enabled, bool):
                cfg.delegation_utilities_enabled = raw_delegation_enabled

            raw_delegation_utilities = providers_section.get("delegation_utilities")
            if isinstance(raw_delegation_utilities, list):
                normalized_utilities = [
                    str(p).strip().lower() for p in raw_delegation_utilities if p is not None
                ]
                if normalized_utilities:
                    cfg.delegation_utilities = normalized_utilities

        raw_swarm_host_mode = swarm_raw.get("host_execution_mode", "host_native")
        if isinstance(raw_swarm_host_mode, str) and raw_swarm_host_mode.strip().lower() in {
            "host_native",
            "delegate",
        }:
            cfg.swarm_host_execution_mode = raw_swarm_host_mode.strip().lower()
        raw_swarm_host_by_caller = swarm_raw.get("host_execution_mode_by_caller", {})
        if isinstance(raw_swarm_host_by_caller, Mapping):
            cfg.swarm_host_execution_mode_by_caller = {
                str(caller).strip().lower(): str(mode).strip().lower()
                for caller, mode in raw_swarm_host_by_caller.items()
                if isinstance(caller, str)
                and isinstance(mode, str)
                and str(mode).strip().lower() in {"host_native", "delegate"}
            }

        if isinstance(providers_section, dict):
            # Optional: per-provider usage-window thresholds
            raw_usage_windows = providers_section.get("usage_windows", {})
            if isinstance(raw_usage_windows, Mapping):
                parsed_windows: dict[str, ProviderUsageWindowConfig] = {}
                for provider_key, window_list in raw_usage_windows.items():
                    if not isinstance(provider_key, str):
                        log.warning("usage_windows: skipping non-string provider key %r", provider_key)
                        continue
                    if not isinstance(window_list, list):
                        log.warning("usage_windows: provider %r must map to a list; skipping", provider_key)
                        continue
                    entries: list[UsageWindowEntry] = []
                    for w in window_list:
                        if not isinstance(w, dict):
                            continue
                        try:
                            entries.append(UsageWindowEntry(
                                hours=float(w.get("hours", 0)),
                                budget_tokens=int(w.get("budget_tokens", 0)) if w.get("budget_tokens") is not None else None,
                                threshold=float(w.get("threshold", 0.8)),
                                action=str(w.get("action", "cost_rank_boost")),
                            ))
                        except (KeyError, ValueError, TypeError) as exc:
                            log.warning("usage_windows: skipping malformed entry for %r: %s", provider_key, exc)
                    if entries:
                        parsed_windows[provider_key.strip().lower()] = ProviderUsageWindowConfig(windows=entries)
                cfg.provider_usage_windows = parsed_windows

        # Parse write_safety.extra_paths
        _ws_raw = raw.get("write_safety", {})
        if isinstance(_ws_raw, Mapping):
            _extra = _ws_raw.get("extra_paths", [])
            if isinstance(_extra, list):
                _extra_paths: list[Path] = []
                for _p in _extra:
                    if isinstance(_p, str) and _p.strip():
                        _extra_paths.append(Path(_p.strip()).expanduser())
                cfg.write_safety_extra_paths = _extra_paths

        # Parse thresholds from YAML (legacy format support)
        t = raw.get("thresholds", {})
        if not isinstance(t, Mapping):
            t = {}
        if "mini_max" in t:
            cfg.thresholds.low_max = t.get("mini_max", cfg.thresholds.low_max)
        if "sonnet_max" in t:
            cfg.thresholds.medium_max = t.get("sonnet_max", cfg.thresholds.medium_max)

        _re_raw = raw.get("routing_exceptions", {})
        if isinstance(_re_raw, Mapping):
            def _str_list(key: str) -> list[str]:
                val = _re_raw.get(key, [])
                if not isinstance(val, list):
                    return []
                return [str(v).strip() for v in val if v and str(v).strip()]
            cfg.routing_exceptions = RoutingExceptions(
                skills=_str_list("skills"),
                filetypes=_dedupe_patterns(
                    DEFAULT_ROUTING_EXCEPTION_FILETYPES,
                    _str_list("filetypes"),
                ),
                projects=_str_list("projects"),
                commands=_str_list("commands"),
                callers=_str_list("callers"),
                paths=_dedupe_patterns(
                    DEFAULT_ROUTING_EXCEPTION_PATHS,
                    _str_list("paths"),
                ),
            )

        # Resilience config
        res_raw = raw.get("resilience", {})
        if isinstance(res_raw, Mapping):
            retry_raw = res_raw.get("retry", {}) or {}
            cb_raw = res_raw.get("circuit_breaker", {}) or {}
            auth_raw = res_raw.get("auth_probe", {}) or {}
            cfg.resilience = ResilienceConfig(
                retry_attempts=int(retry_raw.get("attempts", 3)),
                retry_base_delay_s=float(retry_raw.get("base_delay_s", 0.5)),
                retry_max_delay_s=float(retry_raw.get("max_delay_s", 8.0)),
                retry_jitter_ratio=float(retry_raw.get("jitter_ratio", 0.3)),
                cb_failure_threshold=int(cb_raw.get("failure_threshold", 3)),
                cb_open_seconds=float(cb_raw.get("open_seconds", 120.0)),
                cb_quota_open_seconds=float(cb_raw.get("quota_open_seconds", 1800.0)),
                cb_auth_open_seconds=float(cb_raw.get("auth_open_seconds", 600.0)),
                auth_probe_ttl_seconds=float(auth_raw.get("ttl_seconds", 600.0)),
                auth_probe_enabled=bool(auth_raw.get("enabled", True)),
                stderr_snippet_chars=int(res_raw.get("stderr_snippet_chars", 2000)),
                health_probe_interval_s=float(res_raw.get("health_probe_interval_s", 30.0)),
            )

        # Surgical edit settings
        surgical_raw = raw.get("surgical_edits", {})
        if isinstance(surgical_raw, dict):
            _se_max = surgical_raw.get("max_file_bytes", SURGICAL_EDIT_MAX_FILE_BYTES)
            cfg.surgical_edit_max_file_bytes = _coerce_config_int(
                _se_max, default=SURGICAL_EDIT_MAX_FILE_BYTES,
                field_name="surgical_edits.max_file_bytes", minimum=1024,
            )
            _se_blk = surgical_raw.get("blocks_max_file_bytes", SURGICAL_EDIT_BLOCKS_MAX_FILE_BYTES)
            cfg.surgical_edit_blocks_max_file_bytes = _coerce_config_int(
                _se_blk, default=SURGICAL_EDIT_BLOCKS_MAX_FILE_BYTES,
                field_name="surgical_edits.blocks_max_file_bytes", minimum=1024,
            )
            try:
                _se_ratio = float(surgical_raw.get("length_ratio_min", SURGICAL_EDIT_LENGTH_RATIO_MIN))
                cfg.surgical_edit_length_ratio_min = max(0.0, min(1.0, _se_ratio))
            except (TypeError, ValueError):
                cfg.surgical_edit_length_ratio_min = SURGICAL_EDIT_LENGTH_RATIO_MIN

        cfg.execute_subtask_guard_strict = bool(raw.get("execute_subtask_guard_strict", True))
        cfg.auto_cascade_mode = bool(raw.get("auto_cascade_mode", True))

        cfg.thresholds.clamp()
        return cfg

    def get_default_effort(self, provider_id: str, tier: str) -> str | None:
        """Return the configured default effort string for a provider+tier, or None.

        Provider and tier matching is case-insensitive; tier must be one of low/medium/high.
        """
        if not provider_id or not tier:
            return None
        normalized = provider_id.strip().lower()
        t = tier.strip().lower()
        per_provider = self.provider_effort_defaults.get(normalized)
        if not per_provider:
            return None
        val = per_provider.get(t)
        if isinstance(val, str) and val:
            return val
        return None

    def get_preferred_routing(
        self,
        tier: str,
        *,
        caller: str | None = None,
    ) -> list[RoutingPreference]:
        """Return configured tie-break preferences for a tier and optional caller."""
        if not tier:
            return []
        normalized_tier = tier.strip().lower()
        normalized_caller = normalize_caller_id(caller)
        if normalized_caller:
            caller_preferences = self.preferred_routing_by_caller.get(normalized_caller)
            if caller_preferences is not None and normalized_tier in caller_preferences:
                return list(caller_preferences[normalized_tier])
        return list(self.preferred_routing.get(normalized_tier, []))

    def get_provider_spillover_capacity(self, provider_id: str) -> int | None:
        """Return configured concurrency capacity for a provider, or None when unspecified.

        Matching is case-insensitive and tolerates both object-backed and dict-backed
        configuration (the registry may pass either a TGsConfig instance or a raw
        dict as config_overrides).
        """
        if not provider_id:
            return None
        # Prefer object-backed SpilloverConfig when present on self
        getter = getattr(self, "spillover", None)
        if isinstance(getter, SpilloverConfig):
            return getter.get_provider_capacity(provider_id)

        # Fallback: no explicit spillover configured
        return None

    @staticmethod
    def _routing_preference_to_dict(entry: Any) -> dict[str, str] | None:
        if isinstance(entry, RoutingPreference):
            return entry.to_dict()
        if isinstance(entry, Mapping):
            return RoutingPreference(
                provider=_normalize_config_text(entry.get("provider")),
                model=_normalize_config_text(entry.get("model")),
            ).to_dict()
        if isinstance(entry, str):
            parsed = _parse_routing_preference(entry, tier="legacy", index=1)
            return parsed.to_dict() if parsed is not None else None
        return None

    @classmethod
    def _routing_preferences_to_list(cls, entries: list[Any]) -> list[dict[str, str]]:
        return [
            serialized
            for entry in entries
            if (serialized := cls._routing_preference_to_dict(entry)) is not None
        ]

    @staticmethod
    def _usage_window_entry_to_dict(entry: Any) -> dict[str, Any] | None:
        if isinstance(entry, UsageWindowEntry):
            return {
                "hours": entry.hours,
                "budget_tokens": entry.budget_tokens,
                "threshold": entry.threshold,
                "action": entry.action,
            }
        if isinstance(entry, Mapping):
            return {
                "hours": entry.get("hours"),
                "budget_tokens": entry.get("budget_tokens"),
                "threshold": entry.get("threshold"),
                "action": entry.get("action"),
            }
        return None

    @classmethod
    def _usage_window_config_to_list(cls, config: Any) -> list[dict[str, Any]]:
        windows = getattr(config, "windows", None)
        if windows is None and isinstance(config, Mapping):
            windows = config.get("windows")
        if not isinstance(windows, list):
            return []
        return [
            serialized
            for entry in windows
            if (serialized := cls._usage_window_entry_to_dict(entry)) is not None
        ]

    def to_legacy_dict(self) -> dict[str, Any]:
        """Convert to the dict format the original code expected."""
        return {
            "models": {
                "mini": {"id": "gpt-5-mini", "description": "Free tier", "agents": 2},
                "sonnet": {"id": "claude-sonnet-4.6", "description": "Standard", "agents": 2},
                "opus": {"id": "claude-opus-4.6", "description": "Premium", "agents": 1},
                "tier_pins": self.model_tier_pins,
            },
            "providers": {
                "cost_overrides": {
                    provider_id: {
                        tier: override.to_dict()
                        for tier, override in sorted(overrides.items())
                    }
                    for provider_id, overrides in sorted(self.provider_cost_overrides.items())
                },
                "timeouts": dict(sorted(self.tier_timeouts.items())),
                "timeout_overrides": {
                    provider_id: dict(sorted(timeouts.items()))
                    for provider_id, timeouts in sorted(self.provider_timeout_overrides.items())
                },
                "effort_defaults": {
                    provider_id: {
                        tier: effort
                        for tier, effort in sorted(efforts.items())
                    }
                    for provider_id, efforts in sorted(self.provider_effort_defaults.items())
                },
                "preferred_routing": {
                    tier: self._routing_preferences_to_list(entries)
                    for tier, entries in sorted(self.preferred_routing.items())
                },
                "preferred_routing_by_caller": {
                    caller: {
                        tier: self._routing_preferences_to_list(entries)
                        for tier, entries in sorted(tier_map.items())
                    }
                    for caller, tier_map in sorted(self.preferred_routing_by_caller.items())
                },
                "usage_windows": {
                    provider_id: windows
                    for provider_id, config in sorted(self.provider_usage_windows.items())
                    if (windows := self._usage_window_config_to_list(config))
                },
                "endpoint_providers": [entry.to_dict() for entry in self.endpoint_providers],
                "router_only_allow_execution": list(self.router_only_allow_execution),
            },
            "thresholds": {
                "mini_max": self.thresholds.low_max,
                "sonnet_max": self.thresholds.medium_max,
            },
            "overrides": self.overrides,
            "signals": self.signals,
            "signal_weights": self.signal_weights,
            "base_score": self.base_score,
            "orchestrator": {
                "planner_model": self.planner_model,
                "planner_timeout_seconds": self.planner_timeout,
                "synthesis_map_reduce": self.synthesis_map_reduce,
                "synthesis_chunk_chars": self.synthesis_chunk_chars,
            },
            "parallelism": {
                "enabled": self.parallelism.enabled,
                "max_workers": self.parallelism.max_workers,
                "swarm_max_agents": self.parallelism.swarm_max_agents,
                "speculation_workers": self.parallelism.speculation_workers,
                "warm_path_workers": self.parallelism.warm_path_workers,
            },
            "swarm": {
                "max_agents": self.swarm_max_agents,
                "host_execution_mode": self.swarm_host_execution_mode,
                "host_execution_mode_by_caller": dict(
                    sorted(self.swarm_host_execution_mode_by_caller.items())
                ),
            },
            "budgets": {
                "default_hard_cap_tokens": self.budgets.default_hard_cap_tokens,
                "default_soft_warning_pct": self.budgets.default_soft_warning_pct,
            },
            "cache": {
                "ttl_hours": self.result_cache_ttl_hours,
            },
            "routing_policy": self.routing_policy.to_dict(),
            "escalation_retry_enabled": self.escalation_retry_enabled,
            "speculation_require_free_lower": self.speculation_require_free_lower,
            "output_quality_retry_enabled": self.output_quality_retry_enabled,
            "quality_check_incomplete_output": self.quality_check_incomplete_output,
            "reasoning_scoring_enabled": self.reasoning_scoring_enabled,
        }


def _test_mode_enabled() -> bool:
    from shared.env import test_mode_enabled

    return test_mode_enabled()


def load_eval_config(path: Path | None = None) -> TGsConfig:
    """Load config for eval surfaces, tolerating missing YAML support in test mode."""
    if path is None and _test_mode_enabled():
        return TGsConfig.defaults()

    target = path or CONFIG_YAML
    if yaml is None and target.exists() and not _test_mode_enabled():
        raise RuntimeError(
            f"PyYAML is required to load config from {target}; install pyyaml or remove the config file"
        )
    try:
        return TGsConfig.from_yaml(target)
    except RuntimeError as exc:
        if not _test_mode_enabled() or "PyYAML is required" not in str(exc):
            raise

        log.warning(
            "Falling back to default eval config in test mode because YAML support is unavailable for %s",
            target,
        )
        return TGsConfig.defaults()
