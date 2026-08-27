"""Task-kind taxonomy — what a piece of work is *about*.

The distinction that makes this a separate axis from everything else already here:

* ``roles.py`` answers *what posture* the agent takes (Debugger, Implementer, …).
  Two tasks with the same role can be wildly different work — "fix this XSS hole"
  and "fix this off-by-one" are both Debugger.
* the ladder's ``level`` answers *how hard* a case is.
* a **kind** answers *what subject matter* it is: ``xss-fix``, ``boilerplate-crud``,
  ``refactor``. That is the axis "is this model good at fixing XSS" lives on, and
  neither of the other two can express it.

The canonical set lives here so the graded ladder (``case.json``'s ``kind``) and the
planner's classifier cannot drift apart: a ladder case graded as ``xss-fix`` is only
useful if a real task can also be recognised as ``xss-fix``.

Deliberately keyword-based and dependency-light, exactly like ``roles.py``. This is
a coarse classifier feeding a clamped one-tier nudge, not a semantic model, and an
unrecognised task returns ``""`` — no kind, no bias, pure heuristic.
"""
from __future__ import annotations

import re

# Security fix kinds. Listed before the generic kinds because "fix the XSS in the
# comment renderer" is both a bugfix and an xss-fix, and the specific answer is the
# useful one.
KIND_XSS_FIX = "xss-fix"
KIND_SQL_INJECTION_FIX = "sql-injection-fix"
KIND_SECURITY_FIX = "security-fix"

# General engineering kinds.
KIND_BOILERPLATE_CRUD = "boilerplate-crud"
KIND_TYPE_HARDENING = "type-hardening"
KIND_REFACTOR = "refactor"
KIND_BUGFIX = "bugfix"
KIND_ALGORITHM = "algorithm"
KIND_DATA_STRUCTURE = "data-structure"
KIND_API_DESIGN = "api-design"
KIND_IMPLEMENTATION = "implementation"
KIND_BOILERPLATE = "boilerplate"

# No kind could be determined. Never used as a lookup key.
UNKNOWN_KIND = ""

# Every kind a ladder case may declare. `tests/test_ladder.py` asserts the shipped
# cases stay inside this set, so a typo in a case.json cannot create a silent
# one-off bucket that no task will ever match.
KNOWN_KINDS: frozenset[str] = frozenset({
    KIND_XSS_FIX,
    KIND_SQL_INJECTION_FIX,
    KIND_SECURITY_FIX,
    KIND_BOILERPLATE_CRUD,
    KIND_TYPE_HARDENING,
    KIND_REFACTOR,
    KIND_BUGFIX,
    KIND_ALGORITHM,
    KIND_DATA_STRUCTURE,
    KIND_API_DESIGN,
    KIND_IMPLEMENTATION,
    KIND_BOILERPLATE,
})

# Ordered most-specific first; first match wins, mirroring roles.py.
_KIND_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    (
        KIND_XSS_FIX,
        re.compile(
            r"\b(?:xss|cross[- ]site[- ]scripting|html[- ]injection|"
            r"unescaped[- ]output|output[- ]encoding|escape\s+(?:the\s+)?html)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_SQL_INJECTION_FIX,
        re.compile(
            r"\b(?:sql[- ]?injection|sqli|parameteri[sz]ed?\s+quer(?:y|ies)|"
            r"bound\s+parameters?|prepared\s+statements?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_SECURITY_FIX,
        re.compile(
            r"\b(?:vulnerabilit(?:y|ies)|cve-\d|command[- ]injection|path[- ]traversal|"
            r"ssrf|csrf|insecure[- ]deserial\w*|hardcoded\s+(?:secret|credential)|"
            r"privilege[- ]escalation|auth(?:entication|orization)?\s+bypass)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_TYPE_HARDENING,
        re.compile(
            r"\b(?:type[- ]hints?|type[- ]annotations?|annotate\s+\w+|mypy|pyright|"
            r"type[- ]safety|strict[- ]typing|add\s+types?\b)",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_BOILERPLATE_CRUD,
        re.compile(
            r"\b(?:crud|scaffold\w*|boilerplate)\b"
            r"|\b(?:create|add|generate)\b[^.]{0,40}\b(?:endpoints?|handlers?|"
            r"controllers?|resources?|serializers?|models?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_REFACTOR,
        re.compile(
            r"\b(?:refactor\w*|de[- ]?duplicat\w+|extract\s+(?:a\s+)?(?:method|function|"
            r"class|module)|restructur\w+|reorgani[sz]\w+|clean\s*up\s+the\b)",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_BUGFIX,
        re.compile(
            r"\b(?:bug|off[- ]by[- ]one|regression|traceback|stack\s*trace|crash\w*|"
            r"failing\s+test|does\s*n[o']t\s+work|incorrect\s+result|wrong\s+(?:output|"
            r"value|result))\b"
            r"|\bfix\b(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_API_DESIGN,
        re.compile(
            r"\b(?:api\s+design|design\s+(?:an?\s+)?api|decorator\s+factory|"
            r"public\s+interface|contract\s+first|openapi)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_DATA_STRUCTURE,
        re.compile(
            r"\b(?:linked\s+list|binary\s+tree|hash\s+(?:map|table)|trie|heap|"
            r"lru\s+cache|ring\s+buffer|data\s+structure)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_ALGORITHM,
        re.compile(
            r"\b(?:algorithm|parser|tokeni[sz]er?|sort\w*\s+(?:the\s+)?\w+|"
            r"time\s+complexity|big[- ]o|dynamic\s+programming)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_BOILERPLATE,
        re.compile(
            r"\b(?:stub|skeleton|getters?\s+and\s+setters?|"
            r"trivial|one[- ]liner|docstrings?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        KIND_IMPLEMENTATION,
        re.compile(
            r"\b(?:implement\w*|build\s+(?:a|an|the)\b|write\s+(?:a|an|the)\b|"
            r"add\s+support\s+for)\b",
            re.IGNORECASE,
        ),
    ),
)


def derive_kind_from_task(description: str) -> str:
    """Classify *description* into a task kind, or ``""`` when nothing matches.

    First match over an ordered list, most specific first — so a security fix is
    reported as the specific vulnerability class rather than the generic
    ``bugfix`` that would also match. An empty result is the common and safe
    outcome; callers must treat it as "no evidence applies".
    """
    text = (description or "").strip()
    if not text:
        return UNKNOWN_KIND
    for kind, pattern in _KIND_PATTERNS:
        if pattern.search(text):
            return kind
    return UNKNOWN_KIND


__all__ = [
    "KNOWN_KINDS",
    "UNKNOWN_KIND",
    "derive_kind_from_task",
]
