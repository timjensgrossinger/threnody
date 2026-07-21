"""Heuristic task decomposition without external LLM calls.

Used for host-native planning: MCP host shells decompose locally and execute
via host Task/Agent tools. No subprocess to Copilot, Codex, or other CLIs.
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from .config import (
    DEFAULT_RISK_FILENAME_PATTERNS,
    DEFAULT_ROUTING_EXCEPTION_FILETYPES,
    DEFAULT_ROUTING_EXCEPTION_PATHS,
)
from .context import extract_references

log = logging.getLogger(__name__)

_FILE_EXT_GROUP = (
    r"py|ts|tsx|js|jsx|html|htm|css|scss|vue|svelte|go|rs|java|kt|rb|cs|yaml|yml|json|toml|md"
    r"|lua|c|h|cpp|hpp|cc|sh|swift|ex|exs|ini|cfg|tf"
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
_INTEGRATION_STEMS = frozenset(
    {"main", "cli", "app", "__init__", "index", "init", "setup", "mod", "lib", "entry", "bootstrap"}
)

# Source vs documentation/config extension classes (for complexity tiering).
_SOURCE_EXTS = frozenset(
    {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".lua", ".go", ".rs", ".c", ".h",
        ".cpp", ".hpp", ".cc", ".java", ".kt", ".rb", ".cs", ".vue", ".svelte",
        ".swift", ".ex", ".exs", ".sh",
    }
)
_DOC_EXTS = frozenset({".md", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt"})

# Task keywords that signal genuine design complexity (push tier toward high).
_COMPLEXITY_KEYWORDS = frozenset(
    {
        "design", "architecture", "schema", "protocol", "interface", "refactor",
        "concurrency", "async", "state machine", "parser", "compiler", "distributed",
    }
)

# Keywords that, combined with a shared directory, indicate interdependent files.
_COUPLING_KEYWORDS = frozenset(
    {"schema", "contract", "shared", "protocol", "api", "event", "interface", "module"}
)

_TIER_ORDER = ("low", "medium", "high")


def _compile_risk_filename_re(patterns) -> "re.Pattern[str] | None":
    """Compile the security-risk vocabulary into a *filename* matcher.

    Boundary is start-of-string or any non-alphanumeric char (NOT ``\\b``), so a
    token is caught across underscore/hyphen compounds — ``credential`` matches
    ``setup_credentials.py`` where ``\\b`` would fail (``_`` is a word char).
    Returns None for an empty list (risk floor becomes a no-op).
    """
    cleaned = [re.escape(str(p).strip()) for p in (patterns or []) if str(p).strip()]
    if not cleaned:
        return None
    return re.compile(r"(?:^|[^a-z0-9])(?:" + "|".join(cleaned) + r")", re.IGNORECASE)


# Fallback risk matcher from bundled defaults; live operator config (when
# available) is compiled in build_heuristic_plan_payload and takes precedence.
_DEFAULT_RISK_FILENAME_RE = _compile_risk_filename_re(DEFAULT_RISK_FILENAME_PATTERNS)

# Test-file detection: test_*, *_test.*, *.test.*, *.spec.*, or under a tests/ dir.
_TEST_FILE_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__)/|(?:^|/)test_[^/]+$|_test\.[^/]+$|\.(?:test|spec)\.[^/]+$",
    re.IGNORECASE,
)

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


_ABS_OR_HOME = re.compile(r"^(?:[A-Za-z]:[\\/]|/|~)")
# First-segment roots that are never a legit repo-relative path. extract_references
# emits a leading-slash-stripped duplicate of absolute paths (e.g. an absolute
# /Users/.../a.py also surfaces as "Users/.../a.py"); reject those too.
_SYSTEM_ROOT_SEGMENTS = frozenset(
    {"users", "home", "root", "etc", "var", "tmp", "private",
     "library", "system", "opt", "usr", "bin", "sbin"}
)


def _is_safe_relative_path(path: str) -> bool:
    """Host-native targets must be repo-relative file paths.

    Rejects absolute/home-anchored paths (the source of home-dir and plan-file
    capture), system-root-anchored relatives, parent traversal, and
    fragment-shaped tokens with no real extension. Spurious prose slices are
    dropped so the empty-entries single-subtask fallback can fire.
    """
    p = (path or "").strip()
    if not p or p.endswith("/"):
        return False
    if _ABS_OR_HOME.match(p):
        return False
    parts = PurePosixPath(p).parts
    if ".." in parts:
        return False
    if parts and parts[0].lower() in _SYSTEM_ROOT_SEGMENTS:
        return False
    suffix = PurePosixPath(p).suffix
    return len(suffix) >= 2  # require a real ".ext"


def _is_integration_file(path: str) -> bool:
    name = _basename(path)
    stem = _stem(path)
    if stem in _INTEGRATION_STEMS:
        return True
    return name in {"index.ts", "index.tsx", "index.js", "index.jsx", "index.html"}


def _extract_explicit_file_entries(
    task: str, *, allow_external: bool = False
) -> list[tuple[str, str]]:
    """Extract file paths explicitly mentioned in task text (no intent inference).

    *allow_external* keeps absolute/out-of-root paths — used by the read-only
    review fanout, which legitimately targets arbitrary files. Write fanout
    leaves it False so spurious home/plan-file slices are dropped.
    """
    if not isinstance(task, str) or not task.strip():
        return []

    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(path: str, hint: str = "") -> None:
        normalized = _normalize_path(path)
        if not allow_external and not _is_safe_relative_path(normalized):
            return
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
    allow_external: bool = False,
) -> list[tuple[str, str]]:
    """Return ordered (path, description_hint) pairs from explicit paths and intent.

    *allow_external* is forwarded to the explicit extractor; the read-only review
    fanout sets it True to keep absolute review targets.
    """
    explicit = _extract_explicit_file_entries(task, allow_external=allow_external)
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
        # The clause loop above keys hints by the matched token, which is usually
        # the basename. Explicit paths are stored full ("lua/x/init.lua"), so first
        # try to inherit the basename-keyed hint before falling back to extraction.
        base_key = name.lower()
        if base_key in hints:
            hints[key] = hints[base_key]
            continue
        window = _clause_window(task, name)
        hints[key] = window if len(window) >= 12 else f"Implement {path}"
    return hints


def _clause_window(task: str, name: str) -> str:
    """Extract the descriptive clause for *name* without cutting at the first comma.

    Captures text from the filename up to the next file token (so each file gets
    its own clause), allowing commas/periods that sit inside balanced parentheses.
    """
    idx = task.lower().find(name.lower())
    if idx == -1:
        return ""
    tail = task[idx:]
    # End the window at the next distinct file token, or a hard newline break.
    end = len(tail)
    for match in _BARE_FILENAME.finditer(tail):
        if match.start() >= len(name):  # skip the filename we started on
            end = match.start()
            break
    newline = tail.find("\n\n")
    if newline != -1:
        end = min(end, newline)
    window = tail[:end].strip(" ,;:-\t")
    # Drop a dangling unbalanced opening paren left by the cut.
    if window.count("(") > window.count(")"):
        cut = window.rfind("(")
        if cut != -1:
            window = window[:cut].strip(" ,;:-\t")
    return window


def _tier_for_subtask(*, file_count: int, default_tier: str) -> str:
    if default_tier not in {"low", "medium", "high"}:
        default_tier = "low"
    if file_count <= 1:
        return "high" if default_tier == "high" else "low"
    return "low"


def _is_test_file(path: str) -> bool:
    return bool(_TEST_FILE_RE.search(_normalize_path(path)))


def _test_subject_stem(path: str) -> str:
    """Strip common test markers from a stem to find the code-under-test name."""
    stem = _stem(path)
    stem = re.sub(r"^test[_-]", "", stem)
    stem = re.sub(r"[_-]test$", "", stem)
    stem = re.sub(r"\.(?:test|spec)$", "", stem)
    return stem


def _floor_tier(tier: str, floor: str) -> str:
    if tier not in _TIER_ORDER:
        tier = "low"
    if floor not in _TIER_ORDER:
        return tier
    return _TIER_ORDER[max(_TIER_ORDER.index(tier), _TIER_ORDER.index(floor))]


def _risk_floor_for(path: str, risk_re, floor_tier: str) -> str | None:
    """Return floor_tier if the basename matches the risk vocabulary, else None."""
    if risk_re is None or floor_tier not in _TIER_ORDER:
        return None
    return floor_tier if risk_re.search(_basename(path)) else None


def _tier_for_file(
    path: str,
    *,
    default_tier: str,
    entries: list[tuple[str, str]],
    risk_re=None,
    floor_tier: str = "medium",
) -> str:
    """Per-file tier for the flat (non-coupled) fanout.

    Preserves the historical baseline (plain files → ``low``; ``default_tier``
    only lifts when it is ``high``) and adds two risk-aware escalations:

    - **Risk floor**: a security-sensitive basename (credential/auth/crypto/…)
      is floored to ``floor_tier`` so credential code is never routed to the
      cheapest tier.
    - **Test-inherit**: a test file inherits the tier of the code under test
      (matched by stem within ``entries``) instead of collapsing to doc-low; a
      test with no locatable subject floors to ``floor_tier``.
    """
    if _is_test_file(path):
        subject = _test_subject_stem(path)
        for other_path, _hint in entries:
            if _normalize_path(other_path) == _normalize_path(path):
                continue
            if _is_test_file(other_path):
                continue
            if _stem(other_path) == subject and subject:
                return _tier_for_file(
                    other_path,
                    default_tier=default_tier,
                    entries=entries,
                    risk_re=risk_re,
                    floor_tier=floor_tier,
                )
        # No code-under-test sibling — a standalone test is non-trivial enough to
        # warrant the risk floor rather than doc-low.
        if default_tier == "high":
            return "high"
        return floor_tier if floor_tier in _TIER_ORDER else "medium"

    tier = "high" if default_tier == "high" else "low"
    risk = _risk_floor_for(path, risk_re, floor_tier)
    if risk is not None:
        tier = _floor_tier(tier, risk)
    return tier


def _ownership_line(target_files: list[str]) -> str:
    """Explicit scope sentence so the prompt agrees with target_files (#3)."""
    listed = ", ".join(target_files)
    return (
        f" You own exactly these files: {listed}. "
        "Do not create or edit any other file."
    )


def _finalize_subtasks(subtasks: list[dict[str, object]]) -> list[dict[str, object]]:
    """Give every file-scoped subtask an authoritative target_files list and an
    ownership sentence, so prompt scope can never exceed declared ownership (#3).

    Task-level subtasks (no target file) are left untouched.
    """
    for st in subtasks:
        tfs = st.get("target_files")
        if isinstance(tfs, list) and tfs:
            target_files = [str(p) for p in tfs if str(p).strip()]
        else:
            tf = st.get("target_file")
            target_files = [str(tf)] if isinstance(tf, str) and tf.strip() else []
        if not target_files:
            continue
        # Preserve order, drop dupes.
        seen: set[str] = set()
        deduped = [p for p in target_files if not (p.lower() in seen or seen.add(p.lower()))]
        st["target_files"] = deduped
        desc = str(st.get("description", "")).rstrip()
        if "You own exactly these files:" not in desc:
            st["description"] = desc + _ownership_line(deduped)
    return subtasks


def _complexity_tier(*, paths: list[str], task_lower: str, coupled: bool, default_tier: str) -> str:
    """Tier from file-type, design keywords, and coupling. default_tier is a floor."""
    exts = {PurePosixPath(_normalize_path(p)).suffix.lower() for p in paths}
    if exts & _SOURCE_EXTS:
        base = "medium"
    elif exts and exts <= _DOC_EXTS:
        base = "low"
    else:
        base = "low"
    idx = _TIER_ORDER.index(base)
    if any(kw in task_lower for kw in _COMPLEXITY_KEYWORDS):
        idx = max(idx, _TIER_ORDER.index("high"))
    if coupled:
        idx = min(idx + 1, len(_TIER_ORDER) - 1)
    if default_tier in _TIER_ORDER:
        idx = max(idx, _TIER_ORDER.index(default_tier))
    return _TIER_ORDER[min(idx, len(_TIER_ORDER) - 1)]


def _entry_parent(path: str) -> str:
    parent = str(PurePosixPath(_normalize_path(path)).parent)
    return "" if parent in ("", ".") else parent


def _coupled_group_indices(entries: list[tuple[str, str]], task_lower: str) -> list[int]:
    """1-based indices of entries that form a coupled group (dir-cohesion proxy).

    Couples >=2 SOURCE files sharing the same non-empty parent directory — the
    directory cohesion is the signal, so no coupling keyword is required (the old
    keyword gate silently split genuinely interdependent modules). Non-source
    files (docs/config) and top-level files never couple, so flat multi-file
    tasks and mixed webapp fan-outs (backend vs frontend in different dirs) stay
    independent. ``task_lower`` is retained for signature stability.

    A true import/call-graph would be more precise, but the heuristic path plans
    from task TEXT — target files often do not exist yet (scaffolding) — so a
    dir-cohesion proxy is used deliberately instead of reading files.
    """
    by_dir: dict[str, list[int]] = {}
    for index, (path, _hint) in enumerate(entries, start=1):
        if PurePosixPath(_normalize_path(path)).suffix.lower() not in _SOURCE_EXTS:
            continue
        parent = _entry_parent(path)
        if not parent:
            continue
        by_dir.setdefault(parent, []).append(index)

    coupled: set[int] = set()
    for ids in by_dir.values():
        if len(ids) < 2:
            continue
        # Skip replicated fan-outs (greet1.py/greet2.py/…): distinct *roles* signal
        # a coupled module; a single repeated base stem signals independent copies.
        bases = {re.sub(r"\d+$", "", _stem(entries[i - 1][0])) for i in ids}
        if len(bases) < 2:
            continue
        coupled.update(ids)
    return sorted(coupled)


def assess_task_complexity(task: str) -> dict[str, object]:
    """Cheap signal of whether a task warrants the real LLM planner over heuristics."""
    if not isinstance(task, str) or not task.strip():
        return {"complex": False, "coupled": False, "source_count": 0, "design_keyword": False}
    task_lower = task.lower()
    try:
        entries = extract_task_file_entries(task, intent_templates=False)
    except Exception:
        entries = []
    coupled = len(_coupled_group_indices(entries, task_lower)) >= 2
    source_count = sum(
        1 for path, _ in entries if PurePosixPath(_normalize_path(path)).suffix.lower() in _SOURCE_EXTS
    )
    design_keyword = any(kw in task_lower for kw in _COMPLEXITY_KEYWORDS)
    return {
        "complex": bool(coupled or source_count >= 4 or design_keyword),
        "coupled": coupled,
        "source_count": source_count,
        "design_keyword": design_keyword,
    }


def _coupled_subtasks(
    entries: list[tuple[str, str]],
    coupled_ids: list[int],
    *,
    default_tier: str,
    topology: str | None,
    task_lower: str,
    strategy: str,
    risk_re=None,
    floor_tier: str = "medium",
) -> dict[str, object]:
    """Build a plan for a detected coupled group.

    "single"   -> one higher-tier subtask owning all coupled files (no extra wave).
    "contract" -> wave 1 defines a shared interface file; the rest depend on it.
    Non-coupled entries (if any) are appended as independent subtasks.
    """
    coupled_set = set(coupled_ids)
    members = [entries[i - 1] for i in coupled_ids]
    others = [(i, entries[i - 1]) for i in range(1, len(entries) + 1) if i not in coupled_set]
    member_paths = [p for p, _ in members]
    tier = _complexity_tier(
        paths=member_paths, task_lower=task_lower, coupled=True, default_tier=default_tier
    )
    # Risk floor: a coupled group containing a security-sensitive file runs at
    # least at floor_tier, taking the group's max tier (#4).
    if risk_re is not None:
        for mp in member_paths:
            if _risk_floor_for(mp, risk_re, floor_tier) is not None:
                tier = _floor_tier(tier, floor_tier)
                break

    # Pick the interface/primary file: an integration file if present, else the first.
    primary_idx = 0
    for j, (path, _hint) in enumerate(members):
        if _is_integration_file(path):
            primary_idx = j
            break
    primary_path = member_paths[primary_idx]

    subtasks: list[dict[str, object]] = []
    if strategy == "contract":
        interface_hint = members[primary_idx][1] or f"Define the shared interface in {primary_path}"
        subtasks.append(
            {
                "id": 1,
                "description": f"Define the shared interface first — {interface_hint}",
                "tier": tier,
                "target_file": primary_path,
                "single_file_insertion": False,
                "depends_on": [],
            }
        )
        next_id = 2
        for j, (path, hint) in enumerate(members):
            if j == primary_idx:
                continue
            subtasks.append(
                {
                    "id": next_id,
                    "description": (hint or f"Implement {path}")
                    + f" (depends on the interface in {primary_path})",
                    "tier": tier,
                    "target_file": path,
                    "single_file_insertion": False,
                    "depends_on": [1],
                }
            )
            next_id += 1
    else:  # "single"
        parts = []
        for path, hint in members:
            parts.append(hint if hint else path)
        merged = "; ".join(parts)
        subtasks.append(
            {
                "id": 1,
                "description": (
                    "Implement the coupled module as one coherent unit "
                    f"(shared interface across {len(members)} files): {merged}"
                ),
                "tier": tier,
                "target_file": primary_path,
                "target_files": member_paths,
                "single_file_insertion": False,
                "depends_on": [],
            }
        )
        next_id = 2

    # Append any non-coupled entries as independent subtasks.
    for _orig_idx, (path, hint) in others:
        subtasks.append(
            {
                "id": next_id,
                "description": hint or f"Create or update {path} as described in the task.",
                "tier": _tier_for_file(
                    path,
                    default_tier=default_tier,
                    entries=entries,
                    risk_re=risk_re,
                    floor_tier=floor_tier,
                ),
                "target_file": path,
                "single_file_insertion": False,
                "depends_on": [],
            }
        )
        next_id += 1

    _finalize_subtasks(subtasks)
    has_deps = any(st.get("depends_on") for st in subtasks)
    normalized_topology = str(topology or "").strip().lower()
    if normalized_topology in {"star", "hierarchical", "dag", "linear"}:
        plan_topology = normalized_topology
    else:
        plan_topology = "dag" if has_deps else "linear"
    return {
        "analysis": (
            f"Host-native heuristic plan: detected a coupled file group "
            f"({len(members)} files); strategy={strategy}. No external planner LLM was called."
        ),
        "subtasks": subtasks,
        "strategy": "dag" if has_deps else ("parallel" if len(subtasks) > 1 else "sequential"),
        "topology": plan_topology,
    }


def _subtasks_from_entries(
    entries: list[tuple[str, str]],
    *,
    default_tier: str,
    topology: str | None,
    task: str = "",
    coupled_strategy: str = "single",
    risk_re=None,
    floor_tier: str = "medium",
) -> dict[str, object]:
    # Detect a coupled file group first; if present, plan it coherently instead
    # of fanning out independent low-tier agents that cannot integrate.
    task_lower = task.lower() if isinstance(task, str) else ""
    coupled_ids = _coupled_group_indices(entries, task_lower)
    if len(coupled_ids) >= 2:
        strategy = coupled_strategy if coupled_strategy in {"single", "contract"} else "single"
        return _coupled_subtasks(
            entries,
            coupled_ids,
            default_tier=default_tier,
            topology=topology,
            task_lower=task_lower,
            strategy=strategy,
            risk_re=risk_re,
            floor_tier=floor_tier,
        )

    integration_ids: list[int] = []
    foundation_ids: list[int] = []
    subtasks: list[dict[str, object]] = []
    for index, (path, hint) in enumerate(entries, start=1):
        description = hint or f"Create or update {path} as described in the task."
        tier = _tier_for_file(
            path,
            default_tier=default_tier,
            entries=entries,
            risk_re=risk_re,
            floor_tier=floor_tier,
        )
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

    _finalize_subtasks(subtasks)
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


def _load_review_tier_bias() -> dict[tuple[str, str], int] | None:
    """Load the learned review-tier bias map (cold path). Fail-safe → None.

    Gated by config.review_learning_enabled. Any failure (no DB, no config, empty
    table) yields None so build_review_subtasks falls back to the pure heuristic —
    the fresh-repo / no-data path stays exactly as before.
    """
    try:
        from .config import TGsConfig

        if not getattr(TGsConfig.from_yaml(), "review_learning_enabled", True):
            return None
        from .agents import _get_agent_db
        from .review_learning import load_review_tier_bias

        return load_review_tier_bias(_get_agent_db())
    except Exception:  # pragma: no cover - learning read is best-effort
        return None


def _load_risk_floor() -> tuple["re.Pattern[str] | None", str]:
    """Resolve the risk-floor matcher + tier from live config. Fail-safe.

    Returns ``(risk_re, floor_tier)``. On any failure (or when disabled) falls
    back to the bundled default vocabulary so the floor still protects credential
    filenames — but returns ``(None, ...)`` when the operator disables it.
    """
    try:
        from .config import TGsConfig

        cfg = TGsConfig.from_yaml()
        if not getattr(cfg, "risk_floor_enabled", True):
            return None, "medium"
        floor_tier = getattr(cfg, "risk_floor_tier", "medium")
        if floor_tier not in _TIER_ORDER:
            floor_tier = "medium"
        risk_re = _compile_risk_filename_re(getattr(cfg, "risk_filename_patterns", None))
        return (risk_re or _DEFAULT_RISK_FILENAME_RE), floor_tier
    except Exception:  # pragma: no cover - config read is best-effort
        return _DEFAULT_RISK_FILENAME_RE, "medium"


def _load_exempt() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Resolve direct-edit exempt filetypes + path basenames from live config.

    Fail-safe to the bundled defaults (``.md``/``.mdc`` + known AI-assistant
    instruction files). Used to fold exempt files into an inline bucket instead
    of spawning an agent for them.
    """
    try:
        from .config import TGsConfig

        cfg = TGsConfig.from_yaml()
        re_cfg = getattr(cfg, "routing_exceptions", None)
        filetypes = tuple(getattr(re_cfg, "filetypes", None) or DEFAULT_ROUTING_EXCEPTION_FILETYPES)
        paths = tuple(getattr(re_cfg, "paths", None) or DEFAULT_ROUTING_EXCEPTION_PATHS)
        return filetypes, paths
    except Exception:  # pragma: no cover - config read is best-effort
        return tuple(DEFAULT_ROUTING_EXCEPTION_FILETYPES), tuple(DEFAULT_ROUTING_EXCEPTION_PATHS)


def _is_exempt_entry(path: str, filetypes: tuple[str, ...], paths: tuple[str, ...]) -> bool:
    """Lightweight, DB-free direct-edit exemption check (suffix + basename)."""
    normalized = _normalize_path(path)
    suffix = PurePosixPath(normalized).suffix.lower()
    if suffix and suffix in {str(ft).strip().lower() for ft in filetypes}:
        return True
    base = _basename(normalized)
    return base in {str(p).strip().lower() for p in paths if "/" not in str(p) and "." in str(p)}


def build_heuristic_plan_payload(
    task: str,
    *,
    default_tier: str = "medium",
    max_agents: int | None = None,
    topology: str | None = None,
    intent_templates: bool = True,
    coupled_strategy: str = "single",
) -> dict[str, object]:
    """Build planner JSON compatible with ``Planner._build_plan`` without an LLM."""
    # Review fanout: REVIEW: sentinel → per-file × dimension DAG plan
    from .review_fanout import is_review_intent, build_review_subtasks, strip_dims_token
    if isinstance(task, str) and is_review_intent(task):
        # Review fanout is read-only — allow absolute/out-of-root review targets.
        # Strip the [dims=...] intent token first so it is never mistaken for a
        # file path; build_review_subtasks re-parses intent from the full task.
        entries = extract_task_file_entries(
            strip_dims_token(task), intent_templates=False, allow_external=True
        )
        tier_bias = _load_review_tier_bias()
        return build_review_subtasks(
            entries, task, max_agents=max_agents, tier_bias=tier_bias
        )  # type: ignore[return-value]

    task_lower = task.lower() if isinstance(task, str) else ""
    prefix = _directory_prefix_from_task(task) if isinstance(task, str) else ""

    # Config-derived context (loaded once): risk-aware tier floor (#4) and the
    # direct-edit exemption lists (#5). Both fail-safe to bundled defaults.
    risk_re, floor_tier = _load_risk_floor()
    exempt_filetypes, exempt_paths = _load_exempt()

    if intent_templates and isinstance(task, str) and _is_fullstack_intent(task_lower):
        raw_subtasks = infer_fullstack_subtasks(task, prefix)
        fs_entries = [(str(st.get("target_file", "")), "") for st in raw_subtasks]
        for subtask in raw_subtasks:
            subtask["tier"] = _tier_for_file(
                str(subtask.get("target_file", "")),
                default_tier=default_tier,
                entries=fs_entries,
                risk_re=risk_re,
                floor_tier=floor_tier,
            )
            subtask["single_file_insertion"] = False
        if max_agents is not None:
            try:
                cap = max(1, int(max_agents))
                raw_subtasks = raw_subtasks[:cap]
            except (TypeError, ValueError):
                pass
        _finalize_subtasks(raw_subtasks)
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

    all_entries = extract_task_file_entries(task, intent_templates=intent_templates)

    # Fold direct-edit exempt files (.md/.mdc, CLAUDE.md, …) into an inline bucket
    # instead of spawning a dedicated agent for each (#5).
    inline_files: list[str] = []
    entries: list[tuple[str, str]] = []
    for path, hint in all_entries:
        if _is_exempt_entry(path, exempt_filetypes, exempt_paths):
            if path not in inline_files:
                inline_files.append(path)
        else:
            entries.append((path, hint))

    if max_agents is not None:
        try:
            cap = max(1, int(max_agents))
        except (TypeError, ValueError):
            cap = None
        else:
            entries = entries[:cap]

    if not entries:
        # No agent work remains. If only exempt files were named, surface them as
        # an inline bucket with no subtasks; otherwise fall back to one task-level
        # subtask (no file paths detected at all).
        if inline_files:
            return {
                "analysis": (
                    f"Host-native heuristic plan: {len(inline_files)} direct-edit "
                    "exempt file(s) folded inline; no agents spawned."
                ),
                "subtasks": [],
                "inline_files": inline_files,
                "strategy": "sequential",
                "topology": topology or "linear",
            }
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

    payload = _subtasks_from_entries(
        entries,
        default_tier=default_tier,
        topology=topology,
        task=task if isinstance(task, str) else "",
        coupled_strategy=coupled_strategy,
        risk_re=risk_re,
        floor_tier=floor_tier,
    )
    if inline_files:
        payload["inline_files"] = inline_files
        base_analysis = str(payload.get("analysis", "")).rstrip()
        payload["analysis"] = (
            f"{base_analysis} {len(inline_files)} direct-edit exempt file(s) folded inline."
        )
    return payload


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
