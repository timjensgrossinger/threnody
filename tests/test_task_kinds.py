#!/usr/bin/env python3
"""Tests for shared/task_kinds.py — the "what is this work about" axis.

Why this axis exists at all: ``roles.py`` says what posture an agent takes and the
ladder's ``level`` says how hard a case is. Neither can express "is this model good
at fixing XSS" — "fix this XSS hole" and "fix this off-by-one" are both Debugger,
and difficulty says nothing about subject matter.

The contract that matters most is the pairing with the graded ladder: a case graded
as ``xss-fix`` is only useful if a real task is also classified as ``xss-fix``.
``tests/test_ladder.py`` asserts the other half (every shipped case's kind is in
``KNOWN_KINDS``).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.task_kinds import KNOWN_KINDS, UNKNOWN_KIND, derive_kind_from_task


class TestSecurityKindsWinOverGeneric:
    """A security fix is also a "fix"; the specific vulnerability class is the
    useful answer, so the specific patterns are ordered first."""

    @pytest.mark.parametrize(
        "text",
        [
            "Fix the XSS vulnerability in the comment renderer",
            "escape the html in render_comment",
            "cross-site scripting in the feed template",
            "unescaped output reaches the DOM",
        ],
    )
    def test_xss(self, text: str) -> None:
        assert derive_kind_from_task(text) == "xss-fix"

    @pytest.mark.parametrize(
        "text",
        [
            "close the SQL injection in repo.py",
            "use parameterised queries instead of string formatting",
            "switch to prepared statements",
            "sqli in find_by_name",
        ],
    )
    def test_sql_injection(self, text: str) -> None:
        assert derive_kind_from_task(text) == "sql-injection-fix"

    @pytest.mark.parametrize(
        "text",
        [
            "fix the path traversal in the archive extractor",
            "there is a command injection in the deploy script",
            "remove the hardcoded secret from settings",
            "patch the SSRF in the webhook fetcher",
            "authentication bypass on the admin route",
        ],
    )
    def test_generic_security(self, text: str) -> None:
        assert derive_kind_from_task(text) == "security-fix"


class TestEngineeringKinds:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("add type hints to config.py", "type-hardening"),
            ("annotate parse_port and make mypy pass", "type-hardening"),
            ("write boilerplate CRUD handlers for tasks", "boilerplate-crud"),
            ("create endpoints for the user resource", "boilerplate-crud"),
            ("generate serializers for the order model", "boilerplate-crud"),
            ("refactor the validator to de-duplicate the checks", "refactor"),
            ("restructure the payments module", "refactor"),
            ("fix the off-by-one in last_index", "bugfix"),
            ("the feed doesn't work, here is the traceback", "bugfix"),
            ("this is a regression since last release", "bugfix"),
            ("design an API for retries", "api-design"),
            ("build a decorator factory for backoff", "api-design"),
            ("implement an LRU cache", "data-structure"),
            ("add a ring buffer for the log tail", "data-structure"),
            ("implement a JSON parser", "algorithm"),
            ("reduce the time complexity of the scan", "algorithm"),
            ("implement support for webhooks", "implementation"),
        ],
    )
    def test_classification(self, text: str, expected: str) -> None:
        assert derive_kind_from_task(text) == expected


class TestNoKind:
    """An unrecognised task must yield no kind, so no bias applies.

    This is the common and safe outcome — the classifier feeds a clamped one-tier
    nudge, and guessing a kind would apply graded evidence about entirely different
    work.
    """

    @pytest.mark.parametrize(
        "text",
        ["", "   ", "update the README wording", "bump the version to 1.2.3"],
    )
    def test_unknown(self, text: str) -> None:
        assert derive_kind_from_task(text) == UNKNOWN_KIND

    def test_unknown_is_falsy(self) -> None:
        """Callers gate on truthiness, so the sentinel must not be a real key."""
        assert not UNKNOWN_KIND
        assert UNKNOWN_KIND not in KNOWN_KINDS


class TestTaxonomyContract:
    def test_every_derived_kind_is_known(self) -> None:
        """A kind the classifier can produce but the taxonomy does not list would
        never be matched by a ladder case, so its evidence could never be applied."""
        samples = [
            "fix the XSS in the renderer",
            "close the SQL injection",
            "fix the path traversal",
            "add type hints",
            "scaffold CRUD endpoints",
            "refactor the module",
            "fix the crash",
            "design an API",
            "implement an LRU cache",
            "write a tokenizer",
            "implement support for webhooks",
            "add a docstring stub",
        ]
        derived = {k for k in (derive_kind_from_task(s) for s in samples) if k}
        unknown = sorted(derived - KNOWN_KINDS)
        assert not unknown, f"kinds outside KNOWN_KINDS: {unknown}"

    def test_classification_is_deterministic(self) -> None:
        text = "fix the XSS vulnerability in the comment renderer"
        assert len({derive_kind_from_task(text) for _ in range(5)}) == 1

    def test_case_insensitive(self) -> None:
        assert derive_kind_from_task("FIX THE XSS HOLE") == "xss-fix"
        assert derive_kind_from_task("Refactor The Validator") == "refactor"
