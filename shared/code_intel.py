"""Deterministic code intelligence: entity index + static smell scan.

Zero tokens, zero LLM, zero network. Two pure scanners over already-read file
content, plus a ``(path, content_sha)``-keyed cache so a re-scan of an unchanged
file costs one indexed read:

* :func:`scan_entities` — stdlib :mod:`ast` for Python; a brace/keyword regex
  fallback for every other extension. Yields per-entity nesting depth, branch
  count, and parameter count, which :mod:`shared.review_fanout` blends into its
  structural-density score. The AST path measures the same quantities the regex
  heuristic approximates, on the same 0.0-1.0 scale and against the same
  thresholds — so ``profile_key_for`` bucket names stay stable and learned
  ``review_tier_bias`` rows remain valid keys.
* :func:`scan_smells` — high-precision static defect patterns keyed to the
  existing review dimensions. Deliberately conservative: these become *leads*
  injected into review prompts, and the ``high``-severity subset becomes the
  expected-findings set for :func:`shared.model_quality.record_static_recall_score`.
  A noisy rule here would wrongly punish a good model, so a rule is only added
  when its false-positive rate is near zero.

Everything is best-effort: a syntax error, an unreadable file, or a missing DB
degrades to the regex path or an empty scan and never raises into a caller.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from shared.db import Database

log = logging.getLogger(__name__)

# Severity ladder. Only HIGH is treated as an expected finding for scoring —
# medium/low are advisory leads that must not penalise a reviewer that skips them.
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
_SEVERITY_RANK = {SEVERITY_LOW: 0, SEVERITY_MEDIUM: 1, SEVERITY_HIGH: 2}

# Dimension keys must match shared.review_fanout.REVIEW_DIMENSIONS.
DIM_SECURITY = "security"
DIM_LOGIC = "logic"
DIM_EDGE = "edge"
DIM_TYPES = "types"
DIM_PERFORMANCE = "performance"

_PY_SUFFIXES = frozenset({".py", ".pyi"})

# Cap the scanned byte count so a vendored bundle or generated blob cannot stall
# plan building. Larger files fall back to the regex path on a truncated head.
MAX_SCAN_BYTES = 512 * 1024


class Entity(NamedTuple):
    """One named definition in a file."""

    name: str
    kind: str  # "function" | "async_function" | "class" | "block"
    lineno: int
    end_lineno: int
    branch_count: int
    nesting_depth: int
    param_count: int


class Smell(NamedTuple):
    """One statically detected defect lead."""

    rule_id: str
    dimension: str
    severity: str
    line: int
    message: str


class CodeIntel(NamedTuple):
    """Scan result for one file at one content revision."""

    path: str
    content_sha: str
    entities: tuple[Entity, ...]
    smells: tuple[Smell, ...]
    max_depth: int
    branch_count: int
    def_count: int
    parsed: bool  # True when a real AST parse produced the stats


EMPTY_INTEL = CodeIntel("", "", (), (), 0, 0, 0, False)


def content_sha(content: str) -> str:
    """Short stable digest of file content — the cache and findings revision key."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Entity scanning
# ---------------------------------------------------------------------------

# Statement nodes that introduce a branch (decision point).
_BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.ExceptHandler,
    ast.IfExp,
    ast.BoolOp,
    ast.Assert,
)
# Nodes that open a nested block for depth accounting.
_BLOCK_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)

if hasattr(ast, "Match"):  # Python 3.10+
    _BRANCH_NODES = _BRANCH_NODES + (ast.Match,)  # type: ignore[assignment]
    _BLOCK_NODES = _BLOCK_NODES + (ast.Match,)  # type: ignore[assignment]


def _param_count(node: ast.AST) -> int:
    args = getattr(node, "args", None)
    if not isinstance(args, ast.arguments):
        return 0
    n = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
    if args.vararg is not None:
        n += 1
    if args.kwarg is not None:
        n += 1
    return n


_ENTITY_KINDS: dict[type, str] = {
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "async_function",
    ast.ClassDef: "class",
}


def _collect_entities(node: ast.AST, out: list[Entity]) -> tuple[int, int]:
    """Single-pass descent: return ``(max_depth, branch_count)`` for the subtree.

    Appends an :class:`Entity` for every def/class encountered on the way back up,
    reusing the child results instead of re-walking each definition's subtree — so
    the whole file costs one traversal rather than one per definition.
    """
    max_depth = 0
    branches = 0
    for child in ast.iter_child_nodes(node):
        child_depth, child_branches = _collect_entities(child, out)
        branches += child_branches
        if isinstance(child, _BRANCH_NODES):
            branches += 1
        if isinstance(child, _BLOCK_NODES):
            child_depth += 1
        if child_depth > max_depth:
            max_depth = child_depth
        kind = _ENTITY_KINDS.get(type(child))
        if kind is not None:
            out.append(
                Entity(
                    name=getattr(child, "name", ""),
                    kind=kind,
                    lineno=child.lineno,
                    end_lineno=int(
                        getattr(child, "end_lineno", child.lineno) or child.lineno
                    ),
                    branch_count=child_branches,
                    nesting_depth=child_depth - 1,
                    param_count=_param_count(child),
                )
            )
    return max_depth, branches


def _entities_from_ast(tree: ast.AST) -> tuple[list[Entity], int, int]:
    """Return ``(entities, max_depth, total_branches)`` in one traversal."""
    out: list[Entity] = []
    max_depth, branches = _collect_entities(tree, out)
    out.sort(key=lambda e: (e.lineno, e.name))
    return out, max_depth, branches


# Regex fallback for non-Python sources. Mirrors the signals
# shared.review_fanout already uses, so the fallback stays on the same scale.
_FALLBACK_DEF = re.compile(
    r"^[ \t]*(?:export\s+)?(?:async\s+)?"
    r"(?:function|func|fn|def|class|interface|type|struct|impl|trait)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _entities_fallback(content: str) -> list[Entity]:
    out: list[Entity] = []
    for match in _FALLBACK_DEF.finditer(content):
        line = content.count("\n", 0, match.start()) + 1
        out.append(
            Entity(
                name=match.group(1),
                kind="block",
                lineno=line,
                end_lineno=line,
                branch_count=0,
                nesting_depth=0,
                param_count=0,
            )
        )
    return out


class EntityScan(NamedTuple):
    entities: list[Entity]
    parsed: bool
    max_depth: int
    branch_count: int


def scan_entities(path: str, content: str) -> EntityScan:
    """Return entities plus module-level depth/branch totals for ``content``.

    ``parsed`` is True only when a real AST produced the stats; callers use it to
    decide whether the branch/depth numbers are exact or must fall back to the
    regex heuristic. The totals cover module-level control flow too, and count each
    branch once (a parent definition's own ``branch_count`` includes its nested
    definitions, so summing entities would double-count).
    """
    if Path(path).suffix.lower() in _PY_SUFFIXES:
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError):
            log.debug("code_intel: ast.parse failed for %s", path, exc_info=True)
        else:
            entities, max_depth, branches = _entities_from_ast(tree)
            return EntityScan(entities, True, max_depth, branches)
    return EntityScan(_entities_fallback(content), False, 0, 0)


# ---------------------------------------------------------------------------
# Smell scanning — AST rules (Python)
# ---------------------------------------------------------------------------

_SECRET_NAME = re.compile(
    r"(?:pass(?:wd|word)|secret|api[_-]?key|apikey|auth[_-]?token|"
    r"access[_-]?token|credential|private[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
# Values that look like a placeholder rather than a real credential.
_PLACEHOLDER_VALUE = re.compile(
    r"^(?:x{3,}|\*{3,}|\.{3,}|changeme|change[_-]?me|placeholder|example|sample|"
    r"your[_-]?\w*|todo|none|null|nil|test|dummy|redacted|unset|default)$",
    re.IGNORECASE,
)
_TEMPLATE_CHARS = ("{", "}", "$", "<", ">")

_SQL_EXEC_METHODS = frozenset(
    {"execute", "executemany", "executescript", "raw", "execute_query"}
)
_NET_MODULES = frozenset({"requests", "httpx"})
_NET_METHODS = frozenset(
    {"get", "post", "put", "delete", "patch", "head", "options", "request"}
)


def _looks_like_secret_literal(value: str) -> bool:
    v = value.strip()
    if len(v) < 8:
        return False
    if _PLACEHOLDER_VALUE.match(v):
        return False
    if any(ch in v for ch in _TEMPLATE_CHARS):
        return False
    if len(set(v)) <= 2:  # "aaaaaaaa", "--------"
        return False
    return True


def _dotted_name(node: ast.AST) -> str:
    """Best-effort dotted source name for a Name/Attribute chain."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords)


def _keyword_is_true(call: ast.Call, name: str) -> bool:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def _is_interpolated(node: ast.AST) -> bool:
    """True for f-strings and ``+``/``%``-built strings — SQL injection shapes."""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr == "format"
    return False


def _body_is_silent(body: list[ast.stmt]) -> bool:
    """True when an except body swallows the error with no handling at all."""
    if len(body) != 1:
        return False
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is Ellipsis
    )


def _async_function_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            out.append((node.lineno, int(getattr(node, "end_lineno", node.lineno) or node.lineno)))
    return out


def _in_ranges(line: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in ranges)


def _scan_smells_ast(tree: ast.AST) -> list[Smell]:
    out: list[Smell] = []
    add = out.append
    async_ranges = _async_function_ranges(tree)

    for node in ast.walk(tree):
        # --- exception handling ---
        if isinstance(node, ast.ExceptHandler):
            if _body_is_silent(node.body):
                add(Smell(
                    "silent_except", DIM_EDGE, SEVERITY_HIGH, node.lineno,
                    "except body swallows the error with no logging or re-raise",
                ))
            elif node.type is None:
                add(Smell(
                    "bare_except", DIM_EDGE, SEVERITY_MEDIUM, node.lineno,
                    "bare except catches BaseException including KeyboardInterrupt",
                ))
            continue

        # --- mutable default argument ---
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [
                d for d in node.args.kw_defaults if d is not None
            ]
            if any(isinstance(d, (ast.List, ast.Dict, ast.Set)) for d in defaults):
                add(Smell(
                    "mutable_default_arg", DIM_LOGIC, SEVERITY_MEDIUM, node.lineno,
                    f"{node.name} has a mutable default argument shared across calls",
                ))
            continue

        # --- unbounded loop ---
        if isinstance(node, ast.While):
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                exits = any(
                    isinstance(n, (ast.Break, ast.Return, ast.Raise))
                    for n in ast.walk(node)
                )
                if not exits:
                    add(Smell(
                        "unbounded_loop", DIM_LOGIC, SEVERITY_MEDIUM, node.lineno,
                        "while True with no break, return, or raise",
                    ))
            continue

        # --- hardcoded secret ---
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [_dotted_name(t) for t in targets]
            value = node.value
            if (
                value is not None
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and any(_SECRET_NAME.search(n or "") for n in names)
                and _looks_like_secret_literal(value.value)
            ):
                add(Smell(
                    "hardcoded_secret", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                    f"credential-shaped name assigned a literal string ({names[0]})",
                ))
            continue

        if not isinstance(node, ast.Call):
            continue

        # --- call-based rules ---
        name = _dotted_name(node.func)
        short = name.rsplit(".", 1)[-1]

        if name in ("eval", "exec"):
            add(Smell(
                "eval_exec", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{name}() executes arbitrary code",
            ))
        elif name in ("os.system", "os.popen"):
            add(Smell(
                "os_system", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{name}() runs an unquoted shell command",
            ))
        elif name in ("pickle.loads", "pickle.load", "marshal.loads"):
            add(Smell(
                "unsafe_deserialize", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{name}() deserializes untrusted data into live objects",
            ))
        elif name == "yaml.load" and not _has_keyword(node, "Loader"):
            add(Smell(
                "yaml_load_unsafe", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                "yaml.load without an explicit safe Loader",
            ))
        elif _keyword_is_true(node, "shell"):
            add(Smell(
                "shell_true", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{name or 'call'}(shell=True) allows command injection",
            ))

        if short in _SQL_EXEC_METHODS and node.args and _is_interpolated(node.args[0]):
            add(Smell(
                "sql_interpolation", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{short}() receives an interpolated query instead of bound parameters",
            ))

        head = name.split(".", 1)[0]
        if head in _NET_MODULES and short in _NET_METHODS and not _has_keyword(node, "timeout"):
            add(Smell(
                "missing_timeout", DIM_EDGE, SEVERITY_MEDIUM, node.lineno,
                f"{name}() has no timeout and can hang indefinitely",
            ))
        elif name in ("urllib.request.urlopen", "urlopen") and not _has_keyword(node, "timeout"):
            add(Smell(
                "missing_timeout", DIM_EDGE, SEVERITY_MEDIUM, node.lineno,
                "urlopen() has no timeout and can hang indefinitely",
            ))

        if async_ranges and _in_ranges(node.lineno, async_ranges):
            if name == "time.sleep" or (head in _NET_MODULES and short in _NET_METHODS):
                add(Smell(
                    "blocking_call_in_async", DIM_PERFORMANCE, SEVERITY_HIGH, node.lineno,
                    f"{name}() blocks the event loop inside an async function",
                ))

    out.sort(key=lambda s: (s.line, s.rule_id))
    return out


# ---------------------------------------------------------------------------
# Smell scanning — regex rules (all languages)
# ---------------------------------------------------------------------------

_REGEX_RULES: tuple[tuple[str, str, str, "re.Pattern[str]", str], ...] = (
    (
        "empty_catch", DIM_EDGE, SEVERITY_HIGH,
        re.compile(r"catch\s*\([^)]*\)\s*\{\s*\}"),
        "empty catch block swallows the error",
    ),
    (
        "eval_exec", DIM_SECURITY, SEVERITY_HIGH,
        re.compile(r"\bnew\s+Function\s*\(|(?<![\w.])eval\s*\("),
        "dynamic code execution from a string",
    ),
    (
        "hardcoded_secret", DIM_SECURITY, SEVERITY_HIGH,
        re.compile(
            # [\w-]* absorbs an identifier tail (secret_key, api_key_v2) so the
            # keyword does not have to sit immediately before the assignment.
            r"(?:pass(?:wd|word)|secret|api[_-]?key|apikey|auth[_-]?token|access[_-]?token|"
            r"credential|private[_-]?key)[\w-]*\s*[:=]\s*[\"']([^\"'\n]{8,})[\"']",
            re.IGNORECASE,
        ),
        "credential-shaped name assigned a literal string",
    ),
    (
        "sql_interpolation", DIM_SECURITY, SEVERITY_HIGH,
        re.compile(
            r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b[^\n;]{0,200}?"
            r"(?:\"\s*\+|'\s*\+|\$\{|`\s*\+)",
            re.IGNORECASE,
        ),
        "SQL statement built by string concatenation or interpolation",
    ),
    (
        "suppressed_type_error", DIM_TYPES, SEVERITY_LOW,
        re.compile(r"@ts-ignore|@ts-expect-error|\bas\s+any\b|#\s*type:\s*ignore"),
        "type error suppressed rather than resolved",
    ),
)


def _scan_smells_regex(content: str) -> list[Smell]:
    out: list[Smell] = []
    for rule_id, dimension, severity, pattern, message in _REGEX_RULES:
        for match in pattern.finditer(content):
            if rule_id == "hardcoded_secret":
                captured = match.group(1) if match.groups() else ""
                if not _looks_like_secret_literal(captured):
                    continue
            line = content.count("\n", 0, match.start()) + 1
            out.append(Smell(rule_id, dimension, severity, line, message))
    out.sort(key=lambda s: (s.line, s.rule_id))
    return out


def _merge_smells(
    ast_smells: list[Smell], content: str, *, parsed: bool
) -> list[Smell]:
    """Combine AST and regex hits, dropping regex duplicates of AST-covered rules."""
    found: list[Smell] = list(ast_smells)
    for smell in _scan_smells_regex(content):
        # The AST rules already cover these precisely on a parsed Python file;
        # keeping the regex hit too would double-count in the recall ledger.
        if parsed and smell.rule_id in ("eval_exec", "hardcoded_secret", "sql_interpolation"):
            continue
        found.append(smell)
    seen: set[tuple[str, int]] = set()
    out: list[Smell] = []
    for smell in sorted(found, key=lambda s: (s.line, s.rule_id)):
        key = (smell.rule_id, smell.line)
        if key in seen:
            continue
        seen.add(key)
        out.append(smell)
    return out


def scan_smells(path: str, content: str) -> list[Smell]:
    """Return deduplicated static smells for ``content``.

    Python files get the AST rules plus the language-agnostic regex rules the AST
    does not cover (comment-based suppressions); everything else, and any Python
    file that fails to parse, gets the regex set only.
    """
    ast_smells: list[Smell] = []
    parsed = False
    if Path(path).suffix.lower() in _PY_SUFFIXES:
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError):
            log.debug("code_intel: smell ast.parse failed for %s", path, exc_info=True)
        else:
            ast_smells = _scan_smells_ast(tree)
            parsed = True
    return _merge_smells(ast_smells, content, parsed=parsed)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


# Category slugs a reviewer would plausibly use for each rule. Reviewers report
# `dimension/category` kebab-case slugs (see review_fanout.REVIEW_DIMENSIONS), which
# rarely match a rule id token-for-token — "os_system" and "command-injection" are
# the same defect. Recall scoring matches through this map, so a correct reviewer is
# never marked as having missed a finding it actually reported.
RULE_CATEGORY_ALIASES: dict[str, frozenset[str]] = {
    "eval_exec": frozenset({
        "eval", "exec", "code-injection", "command-injection", "rce", "dynamic-code",
    }),
    "os_system": frozenset({
        "os-command", "command-injection", "shell-injection", "command", "rce",
    }),
    "shell_true": frozenset({
        "shell", "shell-injection", "command-injection", "rce",
    }),
    "unsafe_deserialize": frozenset({
        "deserialization", "insecure-deserialization", "pickle", "rce",
    }),
    "yaml_load_unsafe": frozenset({
        "deserialization", "insecure-deserialization", "yaml", "rce",
    }),
    "sql_interpolation": frozenset({"sql-injection", "sqli", "sql"}),
    "hardcoded_secret": frozenset({
        "hardcoded-secret", "hardcoded-credential", "secret", "credential", "hardcoded",
    }),
    "silent_except": frozenset({
        "silent-failure", "swallowed-exception", "empty-catch", "error-handling",
        "missing-error-handling", "exception",
    }),
    "empty_catch": frozenset({
        "silent-failure", "swallowed-exception", "empty-catch", "error-handling",
        "missing-error-handling", "exception",
    }),
    "bare_except": frozenset({
        "bare-except", "broad-except", "error-handling", "exception",
    }),
    "missing_timeout": frozenset({
        "missing-timeout", "timeout", "hang", "unbounded-wait",
    }),
    "blocking_call_in_async": frozenset({
        "blocking-io", "blocking", "event-loop", "async",
    }),
    "mutable_default_arg": frozenset({
        "mutable-default", "default-argument", "shared-state",
    }),
    "unbounded_loop": frozenset({
        "infinite-loop", "unbounded", "unbounded-growth", "loop",
    }),
    "suppressed_type_error": frozenset({
        "suppressed-type", "type-ignore", "unsafe-cast", "any",
    }),
}


def rule_aliases(rule_id: str) -> frozenset[str]:
    """Category slugs that count as reporting ``rule_id``.

    Falls back to the rule id's own tokens for a rule with no explicit entry, so a
    newly added rule still matches on its obvious spelling.
    """
    known = RULE_CATEGORY_ALIASES.get(rule_id)
    if known:
        return known
    return frozenset({rule_id, rule_id.replace("_", "-")})


def smells_by_dimension(smells: "tuple[Smell, ...] | list[Smell]") -> dict[str, list[Smell]]:
    """Group smells by review dimension key."""
    out: dict[str, list[Smell]] = {}
    for smell in smells:
        out.setdefault(smell.dimension, []).append(smell)
    return out


def expected_findings(
    smells: "tuple[Smell, ...] | list[Smell]", dimension: str | None = None
) -> list[Smell]:
    """High-severity smells only — the set a reviewer is expected to catch.

    Medium/low smells are advisory leads and are deliberately excluded so a
    reviewer that ignores them is never scored down.
    """
    return [
        s
        for s in smells
        if s.severity == SEVERITY_HIGH and (dimension is None or s.dimension == dimension)
    ]


def max_severity(smells: "tuple[Smell, ...] | list[Smell]") -> str | None:
    """Highest severity present, or None for an empty set."""
    best: str | None = None
    for smell in smells:
        if best is None or _SEVERITY_RANK[smell.severity] > _SEVERITY_RANK[best]:
            best = smell.severity
    return best


def format_smell_leads(smells: "tuple[Smell, ...] | list[Smell]", *, limit: int = 12) -> str:
    """Render smells as a prompt block of leads to verify or refute.

    Framed as unconfirmed static hits on purpose — the reviewer must confirm each
    one against real control flow, and is told to say so when a lead is wrong.
    """
    if not smells:
        return ""
    ranked = sorted(
        smells, key=lambda s: (-_SEVERITY_RANK[s.severity], s.line)
    )[:limit]
    lines = [
        f"- line {s.line} [{s.severity}] {s.rule_id}: {s.message}" for s in ranked
    ]
    return (
        "\n\nStatic pre-scan leads (pattern matches only — NOT confirmed findings). "
        "Verify each against real control flow: report it if genuine, and say "
        "\"refuted: <rule_id>\" if it is a false positive. Findings outside this "
        "list are equally in scope.\n" + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Scan + cache
# ---------------------------------------------------------------------------

_MEM_CACHE: dict[tuple[str, str], CodeIntel] = {}
_MEM_CACHE_MAX = 512


def _mem_cache_put(key: tuple[str, str], intel: CodeIntel) -> None:
    if len(_MEM_CACHE) >= _MEM_CACHE_MAX:
        _MEM_CACHE.clear()
    _MEM_CACHE[key] = intel


def clear_intel_cache() -> None:
    """Drop the in-process scan cache (tests, long-lived servers)."""
    _MEM_CACHE.clear()


def _intel_to_payload(intel: CodeIntel) -> tuple[str, str]:
    return (
        json.dumps([list(e) for e in intel.entities]),
        json.dumps([list(s) for s in intel.smells]),
    )


def _intel_from_row(path: str, sha: str, row: Any) -> CodeIntel | None:
    try:
        entities = tuple(Entity(*e) for e in json.loads(row[0]))
        smells = tuple(Smell(*s) for s in json.loads(row[1]))
    except (TypeError, ValueError, IndexError):
        log.debug("code_intel: malformed cache row for %s", path, exc_info=True)
        return None
    return CodeIntel(
        path=path,
        content_sha=sha,
        entities=entities,
        smells=smells,
        max_depth=int(row[2] or 0),
        branch_count=int(row[3] or 0),
        def_count=int(row[4] or 0),
        parsed=bool(row[5]),
    )


def _db_cache_get(db: Database, path: str, sha: str) -> CodeIntel | None:
    try:
        with db.conn() as conn:
            row = conn.execute(
                "SELECT entities, smells, max_depth, branch_count, def_count, parsed "
                "FROM code_intel WHERE path = ? AND content_sha = ?",
                (path, sha),
            ).fetchone()
    except Exception:  # pragma: no cover - best-effort read
        log.debug("code_intel: cache read failed for %s", path, exc_info=True)
        return None
    return _intel_from_row(path, sha, row) if row else None


def _db_cache_put(db: Database, intel: CodeIntel) -> None:
    entities_json, smells_json = _intel_to_payload(intel)
    try:
        with db.conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO code_intel "
                "(path, content_sha, entities, smells, max_depth, branch_count, "
                "def_count, parsed, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    intel.path,
                    intel.content_sha,
                    entities_json,
                    smells_json,
                    intel.max_depth,
                    intel.branch_count,
                    intel.def_count,
                    1 if intel.parsed else 0,
                    time.time(),
                ),
            )
    except Exception:  # pragma: no cover - best-effort write
        log.debug("code_intel: cache write failed for %s", intel.path, exc_info=True)


def scan(
    path: str,
    *,
    content: str | None = None,
    db: "Database | None" = None,
) -> CodeIntel:
    """Scan ``path`` for entities and smells, using the caches when possible.

    ``content`` is reused when the caller already read the file (the common case —
    :mod:`shared.review_fanout` shares the mtime-keyed read cache). ``db`` enables
    the cross-process ``code_intel`` table; without it the in-process cache still
    applies. Returns :data:`EMPTY_INTEL`-shaped output on any failure.
    """
    if content is None:
        from .context import read_source_cached

        content = read_source_cached(Path(path), max_bytes=None)
    if content is None:
        return EMPTY_INTEL._replace(path=path)
    if len(content) > MAX_SCAN_BYTES:
        content = content[:MAX_SCAN_BYTES]

    sha = content_sha(content)
    key = (path, sha)
    cached = _MEM_CACHE.get(key)
    if cached is not None:
        return cached
    if db is not None:
        cached = _db_cache_get(db, path, sha)
        if cached is not None:
            _mem_cache_put(key, cached)
            return cached

    # One ast.parse serves both scanners — scan_entities/scan_smells stay usable
    # standalone, but the cached path never pays for a second parse.
    tree: ast.AST | None = None
    if Path(path).suffix.lower() in _PY_SUFFIXES:
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError):
            log.debug("code_intel: ast.parse failed for %s", path, exc_info=True)
    if tree is not None:
        entities, max_depth, branches = _entities_from_ast(tree)
        smells = _merge_smells(_scan_smells_ast(tree), content, parsed=True)
        parsed = True
    else:
        entities = _entities_fallback(content)
        max_depth = 0
        branches = 0
        smells = _merge_smells([], content, parsed=False)
        parsed = False
    intel = CodeIntel(
        path=path,
        content_sha=sha,
        entities=tuple(entities),
        smells=tuple(smells),
        max_depth=max_depth,
        branch_count=branches,
        def_count=len(entities),
        parsed=parsed,
    )
    _mem_cache_put(key, intel)
    if db is not None:
        _db_cache_put(db, intel)
    return intel


__all__ = [
    "SEVERITY_HIGH",
    "SEVERITY_MEDIUM",
    "SEVERITY_LOW",
    "DIM_SECURITY",
    "DIM_LOGIC",
    "DIM_EDGE",
    "DIM_TYPES",
    "DIM_PERFORMANCE",
    "MAX_SCAN_BYTES",
    "Entity",
    "EntityScan",
    "Smell",
    "CodeIntel",
    "EMPTY_INTEL",
    "content_sha",
    "scan",
    "scan_entities",
    "scan_smells",
    "smells_by_dimension",
    "expected_findings",
    "max_severity",
    "format_smell_leads",
    "clear_intel_cache",
]
