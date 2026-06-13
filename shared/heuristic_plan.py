"""Heuristic task decomposition without external LLM calls.

Used for host-native planning: MCP host shells decompose locally and execute
via host Task/Agent tools. No subprocess to Copilot, Codex, or other CLIs.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .context import extract_references

_FILE_EXT_GROUP = (
    r"py|ts|tsx|js|jsx|html|htm|css|scss|vue|svelte|go|rs|java|kt|rb|cs|yaml|yml|json|toml|md"
)
_NUMBERED_FILE = re.compile(
    r"\(\d+\)\s*([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z][A-Za-z0-9]*)",
    re.IGNORECASE,
)
_BARE_FILENAME = re.compile(
    rf"(?<![\w/.])([A-Za-z0-9_.-]+\.(?:{_FILE_EXT_GROUP}))\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(
    rf"(?<=[,;])\s*(?=[A-Za-z0-9_.-]+\.(?:{_FILE_EXT_GROUP})\b)",
    re.IGNORECASE,
)
_INTEGRATION_STEMS = frozenset({"main", "cli", "app", "__init__", "index"})

_WORD_NUMBERS: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_COUNTED_FANOUT = re.compile(
    rf"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    rf"(?:numbered\s+)?"
    rf"([A-Za-z0-9_.-]+)\.({_FILE_EXT_GROUP})\b"
    r"(?:\s+numbered)?",
    re.IGNORECASE,
)
_NUMBERED_BEFORE_FILE = re.compile(
    rf"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+numbered\s+"
    rf"([A-Za-z0-9_.-]+)\.({_FILE_EXT_GROUP})\b",
    re.IGNORECASE,
)
_DIR_PREFIX = re.compile(
    r"(?:\bin\s+|(?:under|into)\s+)([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)/?",
    re.IGNORECASE,
)


def _parse_count_token(raw: str) -> int | None:
    token = raw.strip().lower()
    if token.isdigit():
        value = int(token)
        return value if 1 <= value <= 32 else None
    return _WORD_NUMBERS.get(token)


def _directory_prefix_from_task(task: str) -> str:
    match = _DIR_PREFIX.search(task)
    if not match:
        return ""
    return _normalize_path(match.group(1)).rstrip("/")


def _prefixed_path(prefix: str, relative: str) -> str:
    rel = _normalize_path(relative)
    if not prefix:
        return rel
    return f"{prefix}/{rel}"


def _expand_numbered_fanout(task: str) -> list[tuple[str, str]] | None:
    """Expand 'Create 4 greet.py numbered' into greet1.py … greet4.py."""
    match = _NUMBERED_BEFORE_FILE.search(task) or _COUNTED_FANOUT.search(task)
    if not match:
        return None
    count = _parse_count_token(match.group(1))
    if count is None:
        return None
    stem = match.group(2)
    ext = match.group(3)
    prefix = _directory_prefix_from_task(task)
    base_hint = task.strip()
    expanded: list[tuple[str, str]] = []
    for index in range(1, count + 1):
        filename = f"{stem}{index}.{ext}"
        path = _prefixed_path(prefix, filename)
        expanded.append((path, f"Create {path} ({index} of {count}): {base_hint}"))
    return expanded


def _normalize_path(path: str) -> str:
    return path.strip().replace("\\", "/")


def _basename(path: str) -> str:
    return PurePosixPath(_normalize_path(path)).name.lower()


def _stem(path: str) -> str:
    return PurePosixPath(_normalize_path(path)).stem.lower()


def _is_integration_file(path: str) -> bool:
    name = _basename(path)
    stem = _stem(path)
    if stem in _INTEGRATION_STEMS:
        return True
    return name in {"index.ts", "index.tsx", "index.js", "index.jsx", "index.html"}


def _extract_explicit_file_entries(task: str) -> list[tuple[str, str]]:
    """Extract file paths explicitly mentioned in task text (no intent inference)."""
    if not isinstance(task, str) or not task.strip():
        return []

    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(path: str, hint: str = "") -> None:
        normalized = _normalize_path(path)
        key = normalized.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append((normalized, hint.strip()))

    fanout = _expand_numbered_fanout(task)
    if fanout:
        for path, hint in fanout:
            _add(path, hint)
        hints = _description_hints_by_path(task, [path for path, _ in ordered])
        return [(path, hints.get(path.lower(), hint)) for path, hint in fanout]

    for ref in extract_references(task):
        _add(ref.path)

    for match in _NUMBERED_FILE.finditer(task):
        _add(match.group(1))

    for match in _BARE_FILENAME.finditer(task):
        _add(match.group(1))

    if not ordered:
        return []

    hints = _description_hints_by_path(task, [path for path, _ in ordered])
    return [(path, hints.get(path.lower(), "")) for path, _ in ordered]


def _task_has_html_css_js(task_lower: str) -> bool:
    has_html = bool(re.search(r"\bhtml\b", task_lower))
    has_css = bool(re.search(r"\bcss\b", task_lower))
    has_js = bool(re.search(r"\b(?:javascript|js)\b", task_lower))
    return has_html and has_css and has_js


def _is_webapp_intent(task_lower: str) -> bool:
    if re.search(r"\b(?:web\s*app|webapp)\b", task_lower):
        return True
    has_python = bool(re.search(r"\b(?:python|flask|fastapi|django)\b", task_lower))
    return has_python and _task_has_html_css_js(task_lower)


def _is_fullstack_intent(task_lower: str) -> bool:
    if re.search(r"\b(?:full\s*stack|fullstack)\b", task_lower):
        return True
    if re.search(r"\bopenapi\b", task_lower):
        return True
    has_frontend = bool(re.search(r"\b(?:frontend|react|vue|angular)\b", task_lower))
    has_backend = bool(re.search(r"\b(?:backend|api|server)\b", task_lower))
    has_contract = bool(re.search(r"\b(?:contract|parallel)\b", task_lower))
    return has_frontend and has_backend and has_contract


def infer_intent_file_entries(task: str) -> list[tuple[str, str]]:
    """Infer multi-file scaffolding from task intent when paths are not listed."""
    if not isinstance(task, str) or not task.strip():
        return []
    task_lower = task.lower()
    prefix = _directory_prefix_from_task(task)
    base_hint = task.strip()

    if _is_webapp_intent(task_lower):
        blueprint = [
            "app.py",
            "templates/index.html",
            "static/css/style.css",
            "static/js/app.js",
        ]
    elif _task_has_html_css_js(task_lower):
        blueprint = ["index.html", "style.css", "app.js"]
    else:
        return []

    entries: list[tuple[str, str]] = []
    for rel in blueprint:
        path = _prefixed_path(prefix, rel)
        entries.append((path, f"Create or update {path}: {base_hint}"))
    return entries


def infer_fullstack_subtasks(task: str, prefix: str) -> list[dict[str, object]]:
    """Build fullstack contract-first subtasks with dependency waves."""
    base_hint = task.strip()
    contract = _prefixed_path(prefix, "openapi.yaml")
    backend = _prefixed_path(prefix, "app.py")
    frontend = _prefixed_path(prefix, "templates/index.html")
    integration = _prefixed_path(prefix, "tests/integration.py")
    return [
        {
            "id": 1,
            "description": f"Define API contract in {contract}: {base_hint}",
            "target_file": contract,
            "depends_on": [],
        },
        {
            "id": 2,
            "description": f"Implement backend in {backend} consuming the contract: {base_hint}",
            "target_file": backend,
            "depends_on": [1],
        },
        {
            "id": 3,
            "description": f"Implement frontend in {frontend} consuming the contract: {base_hint}",
            "target_file": frontend,
            "depends_on": [1],
        },
        {
            "id": 4,
            "description": f"Integration and wire-up in {integration}: {base_hint}",
            "target_file": integration,
            "depends_on": [2, 3],
        },
    ]


def extract_task_file_entries(
    task: str,
    *,
    intent_templates: bool = True,
) -> list[tuple[str, str]]:
    """Return ordered (path, description_hint) pairs from explicit paths and intent."""
    explicit = _extract_explicit_file_entries(task)
    if len(explicit) >= 2:
        return explicit
    if len(explicit) == 1:
        return explicit
    if not intent_templates:
        return []
    return infer_intent_file_entries(task)


def _description_hints_by_path(task: str, paths: list[str]) -> dict[str, str]:
    hints: dict[str, str] = {}
    numbered = list(_NUMBERED_FILE.finditer(task))
    if numbered:
        for idx, match in enumerate(numbered):
            path = _normalize_path(match.group(1))
            start = match.end()
            end = numbered[idx + 1].start() if idx + 1 < len(numbered) else len(task)
            fragment = task[start:end].strip(" ,;:-")
            if fragment:
                hints[path.lower()] = f"Create {path}: {fragment}".strip()
        return hints

    clauses = _CLAUSE_SPLIT.split(task)
    for clause in clauses:
        clause = clause.strip()
        if not clause:
            continue
        file_match = _BARE_FILENAME.search(clause) or _NUMBERED_FILE.search(clause)
        if not file_match:
            continue
        path = _normalize_path(file_match.group(1))
        hints[path.lower()] = clause.strip(" ,;")

    for path in paths:
        key = path.lower()
        if key in hints:
            continue
        name = PurePosixPath(path).name
        pattern = re.compile(
            rf"{re.escape(name)}[^.;,\n]{{0,120}}",
            re.IGNORECASE,
        )
        match = pattern.search(task)
        if match:
            hints[key] = match.group(0).strip(" ,;")
    return hints


def _tier_for_subtask(*, file_count: int, default_tier: str) -> str:
    if default_tier not in {"low", "medium", "high"}:
        default_tier = "low"
    if file_count <= 1:
        return "high" if default_tier == "high" else "low"
    return "low"


def _subtasks_from_entries(
    entries: list[tuple[str, str]],
    *,
    default_tier: str,
    topology: str | None,
) -> dict[str, object]:
    integration_ids: list[int] = []
    foundation_ids: list[int] = []
    subtasks: list[dict[str, object]] = []
    for index, (path, hint) in enumerate(entries, start=1):
        description = hint or f"Create or update {path} as described in the task."
        tier = _tier_for_subtask(file_count=len(entries), default_tier=default_tier)
        subtasks.append(
            {
                "id": index,
                "description": description,
                "tier": tier,
                "target_file": path,
                "single_file_insertion": False,
                "depends_on": [],
            }
        )
        if _is_integration_file(path):
            integration_ids.append(index)
        else:
            foundation_ids.append(index)

    if integration_ids and foundation_ids:
        foundation_set = set(foundation_ids)
        for subtask in subtasks:
            if int(subtask.get("id", -1)) in integration_ids:
                subtask["depends_on"] = sorted(foundation_set)

    has_deps = any(subtask.get("depends_on") for subtask in subtasks)
    normalized_topology = str(topology or "").strip().lower()
    if normalized_topology in {"star", "hierarchical", "dag", "linear"}:
        plan_topology = normalized_topology
    else:
        plan_topology = "dag" if has_deps else "linear"

    return {
        "analysis": (
            f"Host-native heuristic plan: {len(subtasks)} file-scoped subtask(s) "
            "from task text. No external planner LLM was called."
        ),
        "subtasks": subtasks,
        "strategy": "dag" if has_deps else "parallel",
        "topology": plan_topology,
    }


def build_heuristic_plan_payload(
    task: str,
    *,
    default_tier: str = "medium",
    max_agents: int | None = None,
    topology: str | None = None,
    intent_templates: bool = True,
) -> dict[str, object]:
    """Build planner JSON compatible with ``Planner._build_plan`` without an LLM."""
    # Review fanout: REVIEW: sentinel → per-file × dimension DAG plan
    from .review_fanout import is_review_intent, build_review_subtasks
    if isinstance(task, str) and is_review_intent(task):
        entries = extract_task_file_entries(task, intent_templates=False)
        return build_review_subtasks(entries, task, max_agents=max_agents)  # type: ignore[return-value]

    task_lower = task.lower() if isinstance(task, str) else ""
    prefix = _directory_prefix_from_task(task) if isinstance(task, str) else ""

    if intent_templates and isinstance(task, str) and _is_fullstack_intent(task_lower):
        raw_subtasks = infer_fullstack_subtasks(task, prefix)
        for subtask in raw_subtasks:
            subtask["tier"] = _tier_for_subtask(
                file_count=len(raw_subtasks),
                default_tier=default_tier,
            )
            subtask["single_file_insertion"] = False
        if max_agents is not None:
            try:
                cap = max(1, int(max_agents))
                raw_subtasks = raw_subtasks[:cap]
            except (TypeError, ValueError):
                pass
        normalized_topology = str(topology or "").strip().lower()
        plan_topology = normalized_topology if normalized_topology in {
            "star", "hierarchical", "dag", "linear",
        } else "dag"
        return {
            "analysis": (
                f"Host-native heuristic plan: {len(raw_subtasks)} fullstack subtask(s) "
                "from intent template. No external planner LLM was called."
            ),
            "subtasks": raw_subtasks,
            "strategy": "dag",
            "topology": plan_topology,
        }

    entries = extract_task_file_entries(task, intent_templates=intent_templates)
    if max_agents is not None:
        try:
            cap = max(1, int(max_agents))
        except (TypeError, ValueError):
            cap = None
        else:
            entries = entries[:cap]

    if not entries:
        tier = default_tier if default_tier in {"low", "medium", "high"} else "medium"
        return {
            "analysis": (
                "Host-native heuristic plan: single subtask (no file paths detected). "
                "No external planner LLM was called."
            ),
            "subtasks": [
                {
                    "id": 1,
                    "description": task.strip(),
                    "tier": tier,
                    "depends_on": [],
                }
            ],
            "strategy": "sequential",
            "topology": topology or "linear",
        }

    return _subtasks_from_entries(
        entries,
        default_tier=default_tier,
        topology=topology,
    )


def file_entries_from_paths(
    paths: list[str],
    *,
    task_hint: str = "",
) -> list[tuple[str, str]]:
    """Build file entries for mid-run plan expansion."""
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    hint = task_hint.strip()
    for raw in paths:
        path = _normalize_path(str(raw))
        if not path or path.lower() in seen:
            continue
        seen.add(path.lower())
        description = f"Create or update {path}"
        if hint:
            description = f"{description}: {hint}"
        entries.append((path, description))
    return entries
