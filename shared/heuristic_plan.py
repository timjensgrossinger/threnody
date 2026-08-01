"""Heuristic task decomposition without external LLM calls.

Used for host-native planning: MCP host shells decompose locally and execute
via host Task/Agent tools. No subprocess to Copilot, Codex, or other CLIs.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path, PurePosixPath

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
# The lookbehind excludes ``-`` so a hyphenated name is never split mid-token:
# without it ``auto-time.yml`` also matched as ``time.yml`` and
# ``atm-dispatcher.md`` as ``dispatcher.md``, inventing target files that the
# task never named.
_BARE_FILENAME = re.compile(
    rf"(?<![\w/.\-])([A-Za-z0-9_.-]+\.(?:{_FILE_EXT_GROUP}))\b",
    re.IGNORECASE,
)
# Matches a bare filename *or* a slashed path. Used to terminate a description
# window at the next file mention — ``_BARE_FILENAME`` alone cannot, because its
# lookbehind rejects any token preceded by ``/``, so a window anchored in a list
# of slashed paths ran on to the end of the paragraph.
_PATH_OR_FILE_TOKEN = re.compile(
    rf"(?<![\w/.\-])((?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:{_FILE_EXT_GROUP}))\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(
    rf"(?<=[,;])\s*(?=[A-Za-z0-9_.-]+\.(?:{_FILE_EXT_GROUP})\b)",
    re.IGNORECASE,
)
# A description window ends at the next list item, blank line, or this many
# chars — whichever comes first. One file, one clause.
_MAX_HINT_CHARS = 320
_LIST_ITEM_BREAK = re.compile(r"\n[ \t]*(?:[-*•]|\d+[.)])[ \t]")
# Trailing words that show the window was cut mid-clause. Stripped so the
# ownership sentence is never spliced onto a dangling connective.
_DANGLING_TAIL = re.compile(
    r"(?:\b(?:and|or|then|but|plus|with|into|from|to|by|for|of|in|on|at|as|via|"
    r"delete|remove|add|drop|keep|move|the|a|an)\b[\s,;:]*)+$",
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

    ordered = _drop_bare_duplicates(ordered)
    hints = _description_hints_by_path(task, [path for path, _ in ordered])
    return [(path, hints.get(path.lower(), "")) for path, _ in ordered]


def _drop_bare_duplicates(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Remove directory-less entries that restate an already-extracted path.

    ``agents/evaluator.py`` and a later bare ``evaluator.py`` are the same file
    mentioned twice; keeping both spawned two agents for one file (and the bare
    one lost its directory, so it pointed at the repo root).
    """
    qualified_names = {
        PurePosixPath(path).name.lower() for path, _ in entries if "/" in path
    }
    if not qualified_names:
        return entries
    return [
        (path, hint)
        for path, hint in entries
        if "/" in path or path.lower() not in qualified_names
    ]


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
            fragment = _bounded_fragment(task[start:end])
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
        # Bound the clause the same way as a window. _CLAUSE_SPLIT only splits
        # before a *bare* filename, so in a task written with slashed paths the
        # first "clause" is the entire task text — assigning it verbatim gave one
        # file a hint containing every other file's instructions.
        hints[path.lower()] = _clause_window(clause, file_match.group(1))

    # Count basenames so a hint is only inherited by basename when it is
    # unambiguous. Two files named SKILL.md (or __init__.py) previously shared
    # one clause, duplicating instructions across unrelated agents.
    basename_counts: dict[str, int] = {}
    for path in paths:
        name = PurePosixPath(path).name.lower()
        basename_counts[name] = basename_counts.get(name, 0) + 1

    for path in paths:
        key = path.lower()
        if key in hints:
            continue
        name = PurePosixPath(path).name
        # The clause loop above keys hints by the matched token, which is usually
        # the basename. Explicit paths are stored full ("lua/x/init.lua"), so try
        # to inherit the basename-keyed hint — but only when that basename is
        # unique among the extracted paths.
        base_key = name.lower()
        if base_key in hints and basename_counts.get(base_key, 0) <= 1:
            hints[key] = hints[base_key]
            continue
        window = _clause_window(task, path)
        hints[key] = window if len(window) >= 12 else f"Implement {path}"
    return hints


def _anchor_offset(task: str, path: str) -> tuple[int, int]:
    """Locate *path* in *task*, returning ``(start, matched_len)``.

    Anchors on the **full path** so the directory prefix is never severed. When
    only the basename appears verbatim (the path was normalized, e.g. ``./x.py``
    → ``x.py``), the match is extended left over path characters so the window
    still starts at the beginning of the path token rather than mid-path.
    """
    lowered = task.lower()
    idx = lowered.find(path.lower())
    if idx != -1:
        return idx, len(path)
    name = PurePosixPath(path).name
    if not name:
        return -1, 0
    idx = lowered.find(name.lower())
    if idx == -1:
        return -1, 0
    start = idx
    while start > 0 and (task[start - 1].isalnum() or task[start - 1] in "_.-/"):
        start -= 1
    return start, (idx - start) + len(name)


def _bounded_fragment(text: str) -> str:
    """Cap a free-text fragment to one clause, trimming a mid-clause cut."""
    fragment = (text or "").strip(" ,;:-\t\n")
    if not fragment:
        return ""
    item_break = _LIST_ITEM_BREAK.search(fragment)
    if item_break:
        fragment = fragment[: item_break.start()]
    blank_line = fragment.find("\n\n")
    if blank_line != -1:
        fragment = fragment[:blank_line]
    if len(fragment) > _MAX_HINT_CHARS:
        capped = fragment[:_MAX_HINT_CHARS]
        boundary = max(capped.rfind(". "), capped.rfind("; "), capped.rfind(", "))
        fragment = capped[: boundary + 1] if boundary > 0 else capped
    return _trim_dangling(fragment)


def _instruction_prefix(task: str, idx: int) -> str:
    """Leading instruction text on the same line/sentence, before *idx*.

    ``Delete a/x.py, a/y.py, a/z.py.`` gives every listed file a window of just
    its own name; the verb lives once, before the list. Without this each agent
    got a prompt that was only a filename and the action was lost.
    """
    line_start = task.rfind("\n", 0, idx) + 1
    sentence_start = task.rfind(". ", line_start, idx)
    start = sentence_start + 2 if sentence_start != -1 else line_start
    prefix_region = task[start:idx]
    # Remove sibling file names (never drag another agent's file into this
    # prompt) but keep the shared instruction verb: "Delete a.py, b.py, c.py"
    # must give every listed file the verb, not just the first.
    prefix_region = _PATH_OR_FILE_TOKEN.sub(" ", prefix_region)
    # A trailing partial path ("From tests/") belongs to this file's own token,
    # not to the instruction — drop it so the prompt does not read "tests/ x.py".
    prefix_region = re.sub(r"[A-Za-z0-9_.\-]*/\s*$", "", prefix_region)
    # Collapse the punctuation and connectives left behind by the removals.
    prefix_region = re.sub(
        r"(?:[\s,;:]|\band\b|\bor\b|\bplus\b|&)+$", " ", prefix_region, flags=re.IGNORECASE
    )
    prefix_region = re.sub(r"\s+", " ", prefix_region)
    prefix = prefix_region.lstrip(" \t-*•,;:").lstrip()
    if len(prefix) > 120:
        # Cut at a word boundary so the prompt never starts mid-word.
        clipped = prefix[-120:]
        space = clipped.find(" ")
        prefix = clipped[space + 1:] if space != -1 else clipped
    return prefix.strip()


def _clause_window(task: str, path: str) -> str:
    """Extract the descriptive clause for *path* — exactly one clause, no overlap.

    The window starts at the path token (prefixed by any shared instruction verb
    on the same line) and ends at the earliest of: the next file mention, the
    next list item, a blank line, or ``_MAX_HINT_CHARS``. Previously it ran to
    the end of the paragraph, so every file in a bulleted list inherited every
    later file's instructions and the joined coupled prompt repeated the same
    block once per member.
    """
    idx, matched_len = _anchor_offset(task, path)
    if idx == -1:
        return ""
    prefix = _instruction_prefix(task, idx)
    tail = task[idx:]
    end = len(tail)

    # Next file mention (path or bare filename), skipping the token we start on.
    for match in _PATH_OR_FILE_TOKEN.finditer(tail):
        if match.start() >= matched_len:
            end = match.start()
            break

    # Next list item — a bulleted instruction list gives one clause per line.
    item_break = _LIST_ITEM_BREAK.search(tail)
    if item_break:
        end = min(end, item_break.start())

    blank_line = tail.find("\n\n")
    if blank_line != -1:
        end = min(end, blank_line)

    if end > _MAX_HINT_CHARS:
        # Trim back to the last clause boundary inside the cap so the window
        # never ends mid-word.
        capped = tail[:_MAX_HINT_CHARS]
        boundary = max(capped.rfind(". "), capped.rfind("; "), capped.rfind(", "))
        end = boundary + 1 if boundary > 0 else _MAX_HINT_CHARS

    window = tail[:end].strip(" ,;:-\t\n")
    # Drop a dangling unbalanced opening paren left by the cut.
    if window.count("(") > window.count(")"):
        cut = window.rfind("(")
        if cut != -1:
            window = window[:cut].strip(" ,;:-\t")
    window = _trim_dangling(window)
    if prefix and window:
        return f"{prefix}{window}" if prefix.endswith((" ", "\t")) else f"{prefix} {window}"
    return window


def _trim_dangling(text: str) -> str:
    """Strip a trailing connective left by a mid-clause cut ("… then delete")."""
    trimmed = (text or "").strip(" ,;:-\t\n")
    if not trimmed:
        return ""
    stripped = _DANGLING_TAIL.sub("", trimmed).strip(" ,;:-\t\n")
    # Never trim the clause away entirely — a short real hint stays as-is.
    return stripped or trimmed


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


def _close_sentence(text: str) -> str:
    """Terminate *text* cleanly so an appended sentence cannot splice into it.

    Without this, a description ending mid-clause produced prompts like
    "…then delete You own exactly these files: …".
    """
    trimmed = _trim_dangling(text)
    if not trimmed:
        return ""
    return trimmed if trimmed[-1] in ".!?:" else f"{trimmed}."


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
            st["description"] = _close_sentence(desc) + _ownership_line(deduped)
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
        # One bullet per owned file. Joining the raw hints with "; " used to
        # concatenate overlapping clause windows, so the same instruction block
        # appeared once per member and read as a truncated path at each seam.
        bullets = "\n".join(
            f"- {path}: {_close_sentence(hint)}" if hint else f"- {path}"
            for path, hint in members
        )
        subtasks.append(
            {
                "id": 1,
                "description": (
                    "Implement the coupled module as one coherent unit "
                    f"(shared interface across {len(members)} files):\n{bullets}"
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


_DIAGNOSE_PROMPT = """\
Diagnose before any code is written. You are READ-ONLY: do not create, edit, or \
delete any file.

Read the target files and the code they depend on, then produce a change-spec the \
implementing agents will follow literally. Be specific enough that a cheaper model \
can execute it without re-deriving your reasoning.

Output exactly these sections:

## Findings
What the current code does that matters for this task, with file:line anchors.

## Change spec
Per file, an ordered list of concrete edits: which function/class, what changes, \
and the exact new behavior. Name real identifiers, not placeholders.

## Invariants
What must not break — existing callers, data shapes, error contracts, tests that \
already cover this.

## Risks
Where a naive edit would be wrong, and what to do instead.

Task: {task}
Target files: {files}
"""

_IMPLEMENT_NOTE = (
    " Follow the change-spec from the diagnosis subtask exactly: it names the "
    "edits, invariants, and risks for this file. The spawn payload's upstream "
    "artifact list gives the file to read it from. If the spec is wrong or "
    "incomplete for what you find in the code, say so in your output rather than "
    "silently improvising."
)


def _tier_step(tier: str, delta: int) -> str:
    """Shift a tier by ``delta`` steps, clamped to low..high."""
    try:
        idx = _TIER_ORDER.index(tier)
    except ValueError:
        return tier
    return _TIER_ORDER[max(0, min(len(_TIER_ORDER) - 1, idx + delta))]


def hybrid_profile_key(paths: list[str], tier: str) -> str:
    """Transferable learning key for the split: ``ext|file_count_bucket|tier``.

    Path-independent by design, like ``review_fanout.profile_key_for`` — what is
    learned about discounting a single dense ``.py`` edit should transfer to the
    next one, including in a repo never seen before.
    """
    exts = sorted({PurePosixPath(_normalize_path(p)).suffix.lower() or "noext" for p in paths})
    ext = exts[0] if len(exts) == 1 else "mixed"
    n = len(paths)
    bucket = "single" if n <= 1 else ("few" if n <= 3 else "many")
    return f"{ext}|{bucket}|{tier}"


def _derive_routing_hints(task: str) -> tuple[float, str]:
    """Compute (urgency_score, duration_bucket) from task text alone.

    Runs the router's own ``classify`` (with no Database, so no adaptive/project/
    time lookups and no I/O) rather than re-deriving from the raw keyword score.
    That matters: the raw score omits high-tier keyword overrides and the reasoning
    bump, so a genuinely complex task would otherwise be mislabelled ``short`` and
    silently lose the hybrid split. Fail-safe to ``(0.0, "medium")`` — the neutral
    values that change no behavior.
    """
    if not isinstance(task, str) or not task:
        return 0.0, "medium"
    try:
        from .config import TGsConfig
        from .router import TaskRouter

        decision = TaskRouter(TGsConfig.from_yaml(), db=None).classify(task)
        return (
            float(decision.urgency_score),
            str(getattr(decision, "expected_duration_bucket", "medium")),
        )
    except Exception:  # pragma: no cover - hint derivation is best-effort
        log.debug("heuristic_plan: routing hint derivation failed", exc_info=True)
        return 0.0, "medium"


def _load_hybrid_config() -> object | None:
    """Live hybrid config. Fail-safe → None (split disabled)."""
    try:
        from .config import TGsConfig

        return getattr(TGsConfig.from_yaml(), "hybrid", None)
    except Exception:  # pragma: no cover - config read is best-effort
        return None


def _load_hybrid_delta_bias() -> dict[str, int]:
    """Learned per-profile delta adjustment (cold path). Fail-safe → empty."""
    try:
        from .hybrid_learning import load_hybrid_delta_bias

        db = _intel_db()
        if db is None:
            return {}
        return load_hybrid_delta_bias(db)  # type: ignore[arg-type]
    except Exception:  # pragma: no cover - learning read is best-effort
        return {}


def apply_hybrid_split(
    payload: dict[str, object],
    *,
    task: str,
    urgency_score: float = 0.0,
    duration_bucket: str | None = None,
    config: object | None = None,
) -> dict[str, object]:
    """Rewrite an expensive write-path plan into diagnose → implement waves.

    Returns ``payload`` unchanged (same object) when the split does not apply, so
    every existing caller keeps its current behavior unless the conditions below
    all hold:

    * the split is enabled and at least one subtask is planned at ``min_tier``
    * those subtasks write files (read-only cells are never split)
    * the write set is within ``max_files`` — one diagnosis cannot stay coherent
      across an unbounded fan-out
    * the task is not urgent, and not a ``short`` job where the extra hop costs
      more latency than the discount saves
    * the plan does not already sequence a foundation step ahead of the writers —
      a contract-first or integration DAG has already paid for the upfront
      reasoning, so a diagnosis on top would be redundant overhead and would
      perturb the existing dependency contract

    Emits tiers only; model choice stays with the host/registry.
    """
    cfg = config if config is not None else _load_hybrid_config()
    if cfg is None or not getattr(cfg, "enabled", False):
        return payload
    subtasks = payload.get("subtasks")
    if not isinstance(subtasks, list) or not subtasks:
        return payload

    min_tier = str(getattr(cfg, "min_tier", "high"))
    if urgency_score >= float(getattr(cfg, "urgency_suppress_at", 0.6)):
        return payload
    if duration_bucket == "short":
        return payload

    targets: list[dict[str, object]] = []
    for st in subtasks:
        if not isinstance(st, dict):
            return payload
        if st.get("read_only"):
            continue
        if str(st.get("tier") or "") != min_tier:
            continue
        if not (st.get("target_file") or st.get("target_files")):
            continue
        existing_deps = st.get("depends_on")
        if isinstance(existing_deps, list) and existing_deps:
            # Already sequenced behind a foundation/contract step — that step is
            # the diagnosis. Bail out entirely rather than layering a second one.
            return payload
        targets.append(st)
    if not targets:
        return payload

    write_paths: list[str] = []
    for st in targets:
        for path in _subtask_paths(st):
            if path not in write_paths:
                write_paths.append(path)
    if not write_paths or len(write_paths) > int(getattr(cfg, "max_files", 8)):
        return payload

    delta = int(getattr(cfg, "implement_tier_delta", -1))
    profile_key = hybrid_profile_key(write_paths, min_tier)
    if getattr(cfg, "learning_enabled", True):
        adjustment = _load_hybrid_delta_bias().get(profile_key, 0)
        if adjustment:
            delta = max(-2, min(-1, delta + adjustment))
    implement_tier = _tier_step(min_tier, delta)
    if implement_tier == min_tier:
        # Nothing to save — the diagnosis hop would be pure overhead.
        return payload

    diagnose_id = max(int(st.get("id", 0) or 0) for st in subtasks) + 1
    diagnose = {
        "id": diagnose_id,
        "description": _DIAGNOSE_PROMPT.format(
            task=(task or "").strip(), files=", ".join(write_paths)
        ),
        "tier": min_tier,
        "read_only": True,
        "depends_on": [],
        "wave_kind": "diagnose",
        # Read-only: host_spawn.build_host_spawn forces host_task for these, so the
        # diagnosis agent cannot write even if the prompt were misread.
        "subagent_type": "",
    }
    for st in targets:
        st["tier"] = implement_tier
        st["wave_kind"] = "implement"
        st["hybrid_profile_key"] = profile_key
        st["hybrid_delta"] = delta
        existing = st.get("depends_on")
        deps = [int(d) for d in existing] if isinstance(existing, list) else []
        if diagnose_id not in deps:
            deps.append(diagnose_id)
        st["depends_on"] = deps
        desc = str(st.get("description", "")).rstrip()
        if _IMPLEMENT_NOTE.strip() not in desc:
            st["description"] = desc + _IMPLEMENT_NOTE

    out = dict(payload)
    out["subtasks"] = [diagnose] + list(subtasks)
    out["strategy"] = "dag"
    if str(out.get("topology") or "") not in {"star", "hierarchical", "dag"}:
        out["topology"] = "dag"
    base_analysis = str(out.get("analysis", "")).rstrip()
    out["analysis"] = (
        f"{base_analysis} Hybrid split: 1 read-only {min_tier}-tier diagnosis then "
        f"{len(targets)} {implement_tier}-tier implementer(s) over "
        f"{len(write_paths)} file(s)."
    )
    out["hybrid_split"] = {
        "diagnose_id": diagnose_id,
        "diagnose_tier": min_tier,
        "implement_tier": implement_tier,
        "delta": delta,
        "profile_key": profile_key,
        "files": write_paths,
    }
    return out


def _pack_subtasks_to_cap(payload: dict[str, object], max_agents: int | None) -> None:
    """Fit file-scoped subtasks into *max_agents* by merging, never by dropping.

    Files sharing a parent directory are merged first, so an over-budget fanout
    becomes fewer agents that each own several related files — every named file
    still has an owner. Mutates *payload* in place.
    """
    if max_agents is None:
        return
    try:
        raw_cap = int(max_agents)
    except (TypeError, ValueError):
        return
    # config.swarm_max_agents uses -1 for "unlimited"; anything below 1 means no
    # cap, never a cap of one.
    if raw_cap < 1:
        return
    cap = raw_cap

    subtasks = payload.get("subtasks")
    if not isinstance(subtasks, list) or len(subtasks) <= cap:
        return

    # Only file-scoped subtasks can be merged; task-level ones stay as they are.
    file_scoped = [st for st in subtasks if isinstance(st, dict) and _subtask_paths(st)]
    other = [st for st in subtasks if not (isinstance(st, dict) and _subtask_paths(st))]
    budget = max(1, cap - len(other))
    if len(file_scoped) <= budget:
        return

    # Group by parent directory, largest group first, so merging keeps cohesion.
    by_dir: dict[str, list[dict[str, object]]] = {}
    for st in file_scoped:
        by_dir.setdefault(_entry_parent(_subtask_paths(st)[0]), []).append(st)

    groups: list[list[dict[str, object]]] = list(by_dir.values())
    # Merge the largest group repeatedly until the group count fits the budget.
    while len(groups) > budget:
        groups.sort(key=len)
        smallest = groups.pop(0)
        groups.sort(key=len)
        groups[0].extend(smallest)
    # A single group may still exceed the budget — split it into `budget` chunks.
    if len(groups) == 1 and budget > 1 and len(groups[0]) > budget:
        flat = groups[0]
        size = -(-len(flat) // budget)
        groups = [flat[i:i + size] for i in range(0, len(flat), size)]

    merged: list[dict[str, object]] = []
    for group in groups:
        merged.append(group[0] if len(group) == 1 else _merge_subtasks(group))

    renumbered = other + merged
    for index, st in enumerate(renumbered, start=1):
        st["id"] = index
        # Merging invalidates cross-subtask dependencies; the packed agents are
        # independent owners, so drop edges rather than point them at dead ids.
        st["depends_on"] = []
    payload["subtasks"] = renumbered
    payload.pop("waves", None)
    # Record the squeeze so it reaches the host instead of only the server log:
    # fewer agents than named files is a plan the operator should see, even
    # though packing (unlike the old truncation) loses no file.
    payload["packing"] = {
        "trigger": "max_agents",
        "cap": cap,
        "subtasks_before": len(file_scoped) + len(other),
        "agents_after": len(renumbered),
        "files_packed": sum(len(_subtask_paths(st)) for st in merged),
    }
    log.info(
        "packed %d file-scoped subtask(s) into %d agent(s) to fit max_agents=%d",
        len(file_scoped), len(merged), cap,
    )


def _merge_subtasks(group: list[dict[str, object]]) -> dict[str, object]:
    """Merge several file-scoped subtasks into one multi-file owner."""
    paths: list[str] = []
    for st in group:
        for path in _subtask_paths(st):
            if path not in paths:
                paths.append(path)
    tier = "low"
    for st in group:
        tier = _floor_tier(tier, str(st.get("tier") or "low"))
    bullets = "\n".join(
        f"- {path}: {_close_sentence(_strip_ownership(str(st.get('description') or '')))}"
        for st, path in ((st, _subtask_paths(st)[0]) for st in group)
    )
    read_only = all(bool(st.get("read_only")) for st in group)
    merged: dict[str, object] = {
        "id": group[0].get("id", 1),
        "description": (
            f"Apply the described changes to these {len(paths)} related files:\n{bullets}"
        ),
        "tier": tier,
        "target_file": paths[0],
        "target_files": paths,
        "single_file_insertion": False,
        "depends_on": [],
    }
    if read_only:
        merged["read_only"] = True
    _finalize_subtasks([merged])
    return merged


def _strip_ownership(description: str) -> str:
    """Drop the ownership sentence so a merged prompt can restate it once."""
    marker = "You own exactly these files:"
    idx = description.find(marker)
    return description[:idx].rstrip() if idx != -1 else description.strip()


def _coverage_report(
    payload: dict[str, object],
    entries: list[tuple[str, str]],
    inline_files: list[str],
) -> dict[str, object]:
    """Account for every file the task named — assigned, inline, or deferred.

    A non-empty ``deferred`` list is a plan defect the host must surface; the
    packing above means it should stay empty. When the agent budget did bite,
    ``packed`` carries the squeeze so the host can report N files owned by
    fewer than N agents rather than the operator inferring it from the wave
    table. ``coverage`` is the only accounting key the planner propagates
    (``Plan.coverage``), so the packing record is folded in here.
    """
    named = [path for path, _hint in entries]
    assigned: set[str] = set()
    subtasks = payload.get("subtasks")
    if isinstance(subtasks, list):
        for st in subtasks:
            if isinstance(st, dict):
                assigned.update(path.lower() for path in _subtask_paths(st))
    deferred = [path for path in named if path.lower() not in assigned]
    report: dict[str, object] = {
        "files_total": len(named) + len(inline_files),
        "files_assigned": len(named) - len(deferred),
        "files_inline": len(inline_files),
        "deferred": deferred,
    }
    packing = payload.get("packing")
    if isinstance(packing, dict):
        report["packed"] = dict(packing)
    return report


def _packing_note(payload: dict[str, object]) -> str:
    """One sentence for ``analysis`` when the agent budget forced packing."""
    packing = payload.get("packing")
    if not isinstance(packing, dict):
        return ""
    return (
        f" Agent budget {packing.get('cap')} was below the planned subtask count "
        f"({packing.get('subtasks_before')}): packed into {packing.get('agents_after')} "
        f"multi-file agent(s); every named file still has an owner."
    )


def _subtask_paths(st: dict[str, object]) -> list[str]:
    """All declared write targets for a subtask, target_files preferred."""
    raw = st.get("target_files")
    if isinstance(raw, list) and raw:
        return [_normalize_path(str(p)) for p in raw if str(p).strip()]
    single = st.get("target_file")
    if isinstance(single, str) and single.strip():
        return [_normalize_path(single)]
    return []


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


def _load_quality_tier_bias() -> dict[tuple[str, str], int]:
    """Load the objective model-quality bias, keyed for the review tier map.

    Returns ``{(GLOBAL_PROFILE_KEY, dimension): step}`` — the ledger's signal is
    per-(model, dimension), so it applies to every file profile rather than one.
    Opt-in via ``model_quality.routing_bias_enabled``; any failure or missing data
    yields ``{}`` so the pure heuristic is unchanged on a fresh repo.
    """
    try:
        from .config import TGsConfig

        mq_cfg = getattr(TGsConfig.from_yaml(), "model_quality", None)
        if mq_cfg is None or not getattr(mq_cfg, "enabled", True):
            return {}
        if not getattr(mq_cfg, "routing_bias_enabled", False):
            return {}
        from .agents import _get_agent_db
        from .quality_bias import apply_quality_floor, load_model_quality_bias

        db = _get_agent_db()
        raw = apply_quality_floor(db, load_model_quality_bias(db))
    except Exception:  # pragma: no cover - learning read is best-effort
        return {}

    # Collapse (model, dimension) -> dimension. Different models can disagree on
    # the same dimension; only act when they agree, so one bad model never drags
    # every reviewer's tier.
    by_dimension: dict[str, set[int]] = {}
    for (_model, dimension), step in raw.items():
        by_dimension.setdefault(dimension, set()).add(step)
    from .review_fanout import GLOBAL_PROFILE_KEY

    return {
        (GLOBAL_PROFILE_KEY, dimension): steps.pop()
        for dimension, steps in by_dimension.items()
        if len(steps) == 1
    }


_FULL_SWEEP_RE = re.compile(
    r"\b(?:full(?:\s+(?:sweep|review|audit))?|audit|everything|entire|whole\s+repo)\b",
    re.IGNORECASE,
)


def _changed_file_entries(task: str) -> list[tuple[str, str]]:
    """Entries for a directory-target review, scoped to changed files.

    Returns ``[]`` — leaving today's behaviour untouched — when the task names no
    directory, when the operator asked for a full sweep, when ``review_scope`` is
    ``full``, or when there is no merge base to diff against (fresh repo, root
    commit, not a git checkout).
    """
    try:
        from .review_fanout import changed_files_under, directory_targets

        directories = directory_targets(task)
        if not directories:
            return []
        if _FULL_SWEEP_RE.search(task or ""):
            return []
        from .config import TGsConfig

        cfg = TGsConfig.from_yaml()
        if str(getattr(cfg, "review_scope", "changed") or "changed").lower() != "changed":
            return []
        workspace_root = str(Path.cwd())
        paths, ref = changed_files_under(workspace_root, directories)
        if not paths or not ref:
            return []
        log.info(
            "review_fanout: scoped %s to %d changed file(s) since %s",
            ", ".join(directories),
            len(paths),
            ref[:12],
        )
        return [(path, "") for path in paths]
    except Exception:  # pragma: no cover - best-effort
        log.debug("heuristic_plan: changed-file review scoping failed", exc_info=True)
        return []


def _intel_db() -> object | None:
    """Shared DB handle for the code_intel scan cache. Fail-safe → None.

    None only costs the cross-process cache; ``code_intel.scan`` still uses its
    in-process cache, so plan building works identically without a database.
    """
    try:
        from .agents import _get_agent_db

        return _get_agent_db()
    except Exception:  # pragma: no cover - cache handle is best-effort
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
    urgency_score: float | None = None,
    duration_bucket: str | None = None,
    caller: str | None = None,
) -> dict[str, object]:
    """Build planner JSON compatible with ``Planner._build_plan`` without an LLM.

    ``urgency_score`` / ``duration_bucket`` only gate the hybrid diagnose->implement
    split: urgent or short work skips the extra hop. Both are derived from the task
    text when not supplied, so callers that have a routing decision can pass it
    through and callers that do not still get the same gating.

    ``caller`` is the host shell id; it selects the prompt-economy capabilities for
    the emitted prompts. Omitting it keeps the pre-capability prompt wording exactly.
    """
    if urgency_score is None or duration_bucket is None:
        derived_urgency, derived_duration = _derive_routing_hints(task)
        if urgency_score is None:
            urgency_score = derived_urgency
        if duration_bucket is None:
            duration_bucket = derived_duration
    # Review fanout: REVIEW: sentinel → per-file × dimension DAG plan
    from .review_fanout import is_review_intent, build_review_subtasks, strip_dims_token
    if isinstance(task, str) and is_review_intent(task):
        # Review fanout is read-only — allow absolute/out-of-root review targets.
        # Strip the [dims=...] intent token first so it is never mistaken for a
        # file path; build_review_subtasks re-parses intent from the full task.
        entries = extract_task_file_entries(
            strip_dims_token(task), intent_templates=False, allow_external=True
        )
        if not entries:
            # No explicit file named. A directory target ("REVIEW: shared/") otherwise
            # degrades to a single agent over the whole task; scope it to the files
            # actually changed since the merge base instead. Each survivor is still
            # reviewed whole, so no learning key changes.
            entries = _changed_file_entries(task)
        tier_bias = _load_review_tier_bias()
        quality_bias = _load_quality_tier_bias()
        if quality_bias:
            tier_bias = {**(tier_bias or {}), **quality_bias}
        return build_review_subtasks(
            entries,
            task,
            max_agents=max_agents,
            tier_bias=tier_bias,
            db=_intel_db(),
            caller=caller,
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
        # Pack rather than truncate — a template file dropped here never gets built.
        fullstack_payload: dict[str, object] = {"subtasks": raw_subtasks}
        _pack_subtasks_to_cap(fullstack_payload, max_agents)
        raw_subtasks = list(fullstack_payload["subtasks"])  # type: ignore[arg-type]
        _finalize_subtasks(raw_subtasks)
        normalized_topology = str(topology or "").strip().lower()
        plan_topology = normalized_topology if normalized_topology in {
            "star", "hierarchical", "dag", "linear",
        } else "dag"
        # Template files are named by the plan, not the task, so account for them
        # here too — an under-budget fullstack fanout is as reportable as a
        # file-listed one.
        fullstack_payload["subtasks"] = raw_subtasks
        fs_coverage = _coverage_report(fullstack_payload, fs_entries, [])
        return apply_hybrid_split(
            {
                "analysis": (
                    f"Host-native heuristic plan: {len(raw_subtasks)} fullstack subtask(s) "
                    "from intent template. No external planner LLM was called."
                    + _packing_note(fullstack_payload)
                ),
                "subtasks": raw_subtasks,
                "strategy": "dag",
                "topology": plan_topology,
                "coverage": fs_coverage,
            },
            task=task if isinstance(task, str) else "",
            urgency_score=urgency_score,
            duration_bucket=duration_bucket,
        )

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
    # Fit the fanout to the agent budget by *packing* files into agents. The cap
    # used to slice the entry list, deleting every file past it from the plan.
    _pack_subtasks_to_cap(payload, max_agents)
    payload["coverage"] = _coverage_report(payload, entries, inline_files)
    if inline_files:
        payload["inline_files"] = inline_files
        base_analysis = str(payload.get("analysis", "")).rstrip()
        payload["analysis"] = (
            f"{base_analysis} {len(inline_files)} direct-edit exempt file(s) folded inline."
        )
    pack_note = _packing_note(payload)
    if pack_note:
        payload["analysis"] = f"{str(payload.get('analysis', '')).rstrip()}{pack_note}"
    # The record now lives in `coverage["packed"]`, which the planner propagates;
    # drop the transient top-level copy so the plan payload stays the known shape.
    payload.pop("packing", None)
    return apply_hybrid_split(
        payload,
        task=task if isinstance(task, str) else "",
        urgency_score=urgency_score,
        duration_bucket=duration_bucket,
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
