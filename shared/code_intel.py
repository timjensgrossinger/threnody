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


class StructuralTraits(NamedTuple):
    """Coarse structural facts about a file, for deciding which reviewers to run.

    Deliberately separate from :class:`CodeIntel` (which is persisted in the
    ``code_intel`` table) so adding traits cannot break cached-row deserialization.

    ``parsed`` is the load-bearing field: it is True only when a real AST produced
    these answers. On a regex fallback the answers are guesses, and a guess must never
    be used to *skip* a reviewer — so callers gate on ``parsed``.
    """

    has_annotations: bool
    has_loops: bool
    has_io: bool
    parsed: bool


UNKNOWN_TRAITS = StructuralTraits(False, False, False, False)

# Call names that indicate I/O or an external round-trip. Used only to decide whether
# a performance reviewer has anything to look at, so false positives are cheap (an
# extra reviewer) and false negatives are not (a skipped reviewer) — hence broad.
_IO_CALL_NAMES: frozenset[str] = frozenset({
    "open", "read", "write", "readlines", "writelines", "load", "loads", "dump",
    "dumps", "get", "post", "put", "delete", "patch", "request", "urlopen",
    "connect", "execute", "executemany", "executescript", "fetchone", "fetchall",
    "fetchmany", "commit", "cursor", "run", "call", "check_output", "check_call",
    "Popen", "send", "recv", "sendall", "accept", "listen", "sleep", "fetch",
    "query", "save", "create", "update", "insert", "select", "scan", "glob",
    "iterdir", "walk", "copy", "copyfile", "move", "remove", "unlink", "rmtree",
})

_ANNOTATION_RE = re.compile(
    r"(?m)(?:^\s*\w+\s*:\s*[A-Za-z_\[][\w\[\], .|\"']*\s*(?:=|$)"  # annotated assignment
    r"|def\s+\w+\s*\([^)]*:\s*[A-Za-z_\[]"                          # annotated parameter
    r"|\)\s*->\s*[A-Za-z_\[])"                                      # return annotation
)
_LOOP_RE = re.compile(r"(?m)\b(?:for|while|forEach|map\s*\(|\.map\b|\.filter\b)\b")


def structural_traits(path: str, content: str | None) -> StructuralTraits:
    """Answer three yes/no questions about a file, with zero tokens.

    Used to skip a review dimension whose class of defect the file structurally
    cannot hold: no annotations anywhere means nothing for a type reviewer to check;
    no loops and no I/O means nothing for a performance reviewer to check.

    Deliberately independent of :func:`scan_smells`. That scan is the yardstick
    ``model_quality.record_static_recall_score`` grades reviewers against, so gating
    reviewers on it would make objective recall trivially satisfiable and destroy the
    signal. These traits are about a file's *shape*, not its suspected defects.
    """
    if not content or not content.strip():
        return UNKNOWN_TRAITS
    if len(content) > MAX_SCAN_BYTES:
        content = content[:MAX_SCAN_BYTES]
    if str(path).lower().endswith(".py"):
        try:
            tree = ast.parse(content)
        except (SyntaxError, ValueError, RecursionError):
            tree = None
        if tree is not None:
            has_annotations = False
            has_loops = False
            has_io = False
            for node in ast.walk(tree):
                if not has_annotations:
                    if isinstance(node, ast.AnnAssign):
                        has_annotations = True
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.returns is not None or any(
                            arg.annotation is not None
                            for arg in [
                                *node.args.args,
                                *node.args.kwonlyargs,
                                *node.args.posonlyargs,
                            ]
                        ):
                            has_annotations = True
                if not has_loops and isinstance(
                    node,
                    (
                        ast.For,
                        ast.AsyncFor,
                        ast.While,
                        ast.ListComp,
                        ast.SetComp,
                        ast.DictComp,
                        ast.GeneratorExp,
                    ),
                ):
                    has_loops = True
                if not has_io and isinstance(node, ast.Call):
                    func = node.func
                    name = (
                        func.attr
                        if isinstance(func, ast.Attribute)
                        else (func.id if isinstance(func, ast.Name) else "")
                    )
                    if name in _IO_CALL_NAMES:
                        has_io = True
                if has_annotations and has_loops and has_io:
                    break
            return StructuralTraits(has_annotations, has_loops, has_io, True)
    # Non-Python or unparseable: answer with the regex heuristic but mark it
    # unparsed, so callers treat the answers as unusable for skipping.
    return StructuralTraits(
        has_annotations=bool(_ANNOTATION_RE.search(content)),
        has_loops=bool(_LOOP_RE.search(content)),
        has_io=any(name in content for name in ("open(", "fetch(", "request", "query")),
        parsed=False,
    )


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


# Calls that unconditionally produce an unverified TLS context.
_TLS_UNVERIFIED_CALLS = frozenset({
    "ssl._create_unverified_context",
    "ssl._https_verify_certificates",
})
# Functions whose contract is "trust this string as markup". Passing a non-literal
# is the canonical stored/reflected XSS shape in Python web code.
_HTML_SAFE_MARKERS = frozenset({"mark_safe", "Markup"})
# Decorators that switch off CSRF protection for a view.
_CSRF_EXEMPT_DECORATORS = frozenset({"csrf_exempt", "csrf.exempt", "exempt"})


def _own_statements(node: ast.AST) -> "list[ast.stmt]":
    """Statements belonging to *node*, not descending into nested functions/classes.

    Needed so a rule about one function's returns is not triggered by a closure
    defined inside it.
    """
    out: list[ast.stmt] = []
    for field in ("body", "orelse", "finalbody"):
        for stmt in getattr(node, field, []) or []:
            out.append(stmt)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            out.extend(_own_statements(stmt))
    for handler in getattr(node, "handlers", []) or []:
        for stmt in handler.body:
            out.append(stmt)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            out.extend(_own_statements(stmt))
    return out


def _statement_blocks(node: ast.AST) -> "list[list[ast.stmt]]":
    """The straight-line statement lists directly owned by *node*."""
    blocks: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        block = getattr(node, field, None)
        if isinstance(block, list) and len(block) > 1:
            blocks.append(block)
    for handler in getattr(node, "handlers", []) or []:
        if isinstance(handler.body, list) and len(handler.body) > 1:
            blocks.append(handler.body)
    return blocks


def _elif_chain_tests(node: ast.If) -> "list[ast.expr]":
    """Every test in one if/elif chain, in source order."""
    tests: list[ast.expr] = [node.test]
    current = node
    while (
        len(current.orelse) == 1
        and isinstance(current.orelse[0], ast.If)
    ):
        current = current.orelse[0]
        tests.append(current.test)
    return tests


def _dump_test(test: ast.expr) -> str:
    """Stable textual key for a branch test, or '' if it cannot be dumped."""
    try:
        return ast.dump(test, annotate_fields=False)
    except Exception:  # pragma: no cover - defensive
        return ""


def _is_constant_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _keyword_is_true_value(call: ast.Call, name: str, expected: object) -> bool:
    """True when keyword *name* is present as the literal constant *expected*."""
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            if kw.value.value is expected:
                return True
    return False


def _is_constant_str(node: ast.AST) -> bool:
    """True for a plain string literal — the safe argument to a mark-safe call."""
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


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
        # --- unreachable code ---
        # A statement that can never execute is a defect by construction, not a
        # style opinion: either the guard above it is wrong or the statement is
        # dead. Only the *same* block is examined, so a return inside an if does
        # not condemn the code after the if.
        if isinstance(node, (ast.Module, ast.If, ast.For, ast.While, ast.With,
                             ast.Try, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.AsyncFor, ast.AsyncWith)):
            for block in _statement_blocks(node):
                for index, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        nxt = block[index + 1]
                        add(Smell(
                            "unreachable_code", DIM_LOGIC, SEVERITY_HIGH, nxt.lineno,
                            f"statement is unreachable: preceded by "
                            f"{type(stmt).__name__.lower()} in the same block",
                        ))
                        break

        # --- duplicated branch condition ---
        # `if x: ... elif x: ...` means the second body is dead. Compared by
        # normalised source, so only a genuine textual duplicate matches.
        if isinstance(node, ast.If):
            seen: set[str] = set()
            for test in _elif_chain_tests(node):
                key = _dump_test(test)
                if key and key in seen:
                    add(Smell(
                        "duplicate_branch_condition", DIM_LOGIC, SEVERITY_HIGH,
                        test.lineno,
                        "branch condition repeats an earlier one, so this body is dead",
                    ))
                    break
                if key:
                    seen.add(key)

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

        # --- function-level rules ---
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + [
                d for d in node.args.kw_defaults if d is not None
            ]
            if any(isinstance(d, (ast.List, ast.Dict, ast.Set)) for d in defaults):
                add(Smell(
                    "mutable_default_arg", DIM_LOGIC, SEVERITY_MEDIUM, node.lineno,
                    f"{node.name} has a mutable default argument shared across calls",
                ))

            # CSRF protection switched off for a view. Unambiguous from the
            # decorator alone.
            for deco in node.decorator_list:
                deco_name = _dotted_name(deco.func if isinstance(deco, ast.Call) else deco)
                if deco_name.rsplit(".", 1)[-1] in _CSRF_EXEMPT_DECORATORS:
                    add(Smell(
                        "csrf_exempt", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                        f"{node.name} disables CSRF protection via @{deco_name}",
                    ))
                    break

            # `-> None` but a value is returned. The annotation and the return are
            # in direct contradiction, so this needs no inference to call a defect
            # — which is what makes it safe as a high-severity `types` rule. Nested
            # functions are skipped so an inner def's return is not blamed here.
            returns = node.returns
            if (
                isinstance(returns, ast.Constant) and returns.value is None
            ) or (
                isinstance(returns, ast.Name) and returns.id == "None"
            ):
                for inner in _own_statements(node):
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        if _is_constant_none(inner.value):
                            continue
                        add(Smell(
                            "none_annotated_returns_value", DIM_TYPES, SEVERITY_HIGH,
                            inner.lineno,
                            f"{node.name} is annotated -> None but returns a value",
                        ))
                        break
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

        # --- added rules -------------------------------------------------------
        # Every one of these is chosen to be UNAMBIGUOUS from the call site alone,
        # because static_recall grades reviewers against the high-severity set: a
        # false positive here does not merely add noise, it marks a correct reviewer
        # as having missed something that was never a defect. Anything needing taint
        # analysis to be certain (SSRF, most path traversal) is deliberately absent
        # rather than approximated.
        if name in _TLS_UNVERIFIED_CALLS or _keyword_is_true_value(node, "verify", False):
            add(Smell(
                "tls_verify_disabled", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{name or 'call'}() disables TLS certificate verification",
            ))

        if short == "extractall" and not _has_keyword(node, "members") \
                and not _has_keyword(node, "filter"):
            add(Smell(
                "unsafe_archive_extract", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{short}() extracts an archive without path filtering "
                "(entries may escape the destination)",
            ))

        if short in _HTML_SAFE_MARKERS and node.args and not _is_constant_str(node.args[0]):
            add(Smell(
                "unescaped_html_output", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                f"{short}() marks a non-literal value as trusted HTML",
            ))
        elif short == "render_template_string" and node.args and _is_interpolated(node.args[0]):
            add(Smell(
                "unescaped_html_output", DIM_SECURITY, SEVERITY_HIGH, node.lineno,
                "render_template_string() receives an interpolated template",
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
    (
        # Assigning to innerHTML/outerHTML from anything other than a literal, plus
        # the two React/DOM escape hatches. Restricted to an assignment from a
        # non-literal so `el.innerHTML = ""` (the common clear-the-node idiom) does
        # not register.
        "unescaped_html_output", DIM_SECURITY, SEVERITY_HIGH,
        re.compile(
            # Only an assignment from an identifier, call, or template literal.
            # A plain quoted literal is developer-controlled, and `el.innerHTML = ""`
            # is the ordinary clear-the-node idiom.
            r"\.(?:inner|outer)HTML\s*=\s*(?:[A-Za-z_$(]|`)"
            # Split so this rule definition does not match itself: a self-hit would
            # make `expected_findings` demand an XSS report from anyone reviewing
            # THIS file, marking a correct reviewer as having missed a non-defect.
            r"|dangerouslySetInner" r"HTML"
            r"|document\.write\s*\("
            r"|\.insertAdjacentHTML\s*\("
        ),
        "value written to the DOM as markup without escaping",
    ),
    (
        "tls_verify_disabled", DIM_SECURITY, SEVERITY_HIGH,
        re.compile(
            r"rejectUnauthorized\s*:\s*false"
            r"|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*[\"']?0"
            r"|InsecureSkipVerify\s*:\s*true"
            r"|curl_setopt\s*\([^)]*CURLOPT_SSL_VERIFYPEER\s*,\s*(?:false|0)",
            re.IGNORECASE,
        ),
        "TLS certificate verification disabled",
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
    "tls_verify_disabled": frozenset({
        "tls", "ssl", "certificate-validation", "cert-validation",
        "insecure-transport", "mitm", "weak-crypto", "verify-disabled",
    }),
    "unsafe_archive_extract": frozenset({
        "path-traversal", "zip-slip", "tar-slip", "directory-traversal",
        "arbitrary-file-write", "archive",
    }),
    "unescaped_html_output": frozenset({
        "xss", "cross-site-scripting", "html-injection", "unescaped-output",
        "template-injection", "output-encoding",
    }),
    "csrf_exempt": frozenset({
        "csrf", "cross-site-request-forgery", "csrf-exempt", "state-changing-get",
    }),
    "unreachable_code": frozenset({
        "unreachable", "unreachable-code", "dead-code", "control-flow",
    }),
    "duplicate_branch_condition": frozenset({
        "duplicate-condition", "duplicate-branch", "dead-branch", "dead-code",
        "wrong-condition", "control-flow",
    }),
    "none_annotated_returns_value": frozenset({
        "return-type", "incompatible-return", "type-mismatch", "annotation-mismatch",
        "wrong-return-type",
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
