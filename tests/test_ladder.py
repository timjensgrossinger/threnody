"""Tests for shared/ladder.py — the graded task ladder.

Two properties make this a benchmark rather than theatre:
  1. Every shipped case's grader ACCEPTS a correct solution (otherwise every model
     would be reported as failing).
  2. Every grader REJECTS a wrong or empty solution (otherwise every model would be
     reported as passing).
Both are asserted below against real reference solutions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared import ladder
from shared.db import Database

# Reference solutions, kept inline so the assertions above are self-contained and
# the shipped cases can never silently drift away from being solvable.
REFERENCE: dict[str, str] = {
    "l0-return-constant": "def answer():\n    return 42\n",
    "l0-fix-off-by-one": "def last_index(items):\n    return len(items) - 1\n",
    "l1-word-frequency": (
        "_PUNCT = \".,!?;:'\\\"\"\n\n\n"
        "def word_counts(text):\n"
        "    counts = {}\n"
        "    for token in (text or '').split():\n"
        "        word = token.strip(_PUNCT).lower()\n"
        "        if not word:\n"
        "            continue\n"
        "        counts[word] = counts.get(word, 0) + 1\n"
        "    return counts\n"
    ),
    "l2-lru-cache": (
        "from collections import OrderedDict\n\n\n"
        "class LRUCache:\n"
        "    def __init__(self, capacity):\n"
        "        self.capacity = max(0, int(capacity))\n"
        "        self._data = OrderedDict()\n\n"
        "    def get(self, key):\n"
        "        if key not in self._data:\n"
        "            return None\n"
        "        self._data.move_to_end(key)\n"
        "        return self._data[key]\n\n"
        "    def put(self, key, value):\n"
        "        if self.capacity == 0:\n"
        "            return\n"
        "        if key in self._data:\n"
        "            self._data.move_to_end(key)\n"
        "        self._data[key] = value\n"
        "        while len(self._data) > self.capacity:\n"
        "            self._data.popitem(last=False)\n"
    ),
    "l5-refactor-validator": (
        "_REQUIRED_FIELDS = ('name', 'email', 'age')\n\n\n"
        "def _is_empty(value):\n"
        "    return value is None or value == ''\n\n\n"
        "def validate(record):\n"
        "    errors = []\n"
        "    for field in _REQUIRED_FIELDS:\n"
        "        if field not in record:\n"
        "            errors.append(f'missing:{field}')\n"
        "        elif _is_empty(record[field]):\n"
        "            errors.append(f'empty:{field}')\n"
        "    return errors\n"
    ),
    "l6-event-store": (
        "class ConcurrencyError(RuntimeError):\n"
        "    \"\"\"Raised on an expected_version mismatch.\"\"\"\n\n\n"
        "class EventStore:\n"
        "    def __init__(self):\n"
        "        self._events = {}\n\n"
        "    def append(self, event):\n"
        "        current = self.version(event.aggregate_id)\n"
        "        if event.expected_version != current:\n"
        "            raise ConcurrencyError('version mismatch')\n"
        "        self._events.setdefault(event.aggregate_id, []).append(event)\n\n"
        "    def replay(self, aggregate_id):\n"
        "        return list(self._events.get(aggregate_id, []))\n\n"
        "    def version(self, aggregate_id):\n"
        "        return len(self._events.get(aggregate_id, []))\n"
    ),
    "l3-expression-parser": (
        "\"\"\"Recursive-descent arithmetic evaluator.\"\"\"\n"
        "\n"
        "\n"
        "def _tokenize(expr):\n"
        "    tokens = []\n"
        "    i = 0\n"
        "    text = expr or \"\"\n"
        "    while i < len(text):\n"
        "        ch = text[i]\n"
        "        if ch.isspace():\n"
        "            i += 1\n"
        "            continue\n"
        "        if ch.isdigit():\n"
        "            start = i\n"
        "            while i < len(text) and text[i].isdigit():\n"
        "                i += 1\n"
        "            tokens.append((\"num\", int(text[start:i])))\n"
        "            continue\n"
        "        if ch in \"+-*/()\":\n"
        "            tokens.append((ch, ch))\n"
        "            i += 1\n"
        "            continue\n"
        "        raise ValueError(f\"unexpected character: {ch!r}\")\n"
        "    return tokens\n"
        "\n"
        "\n"
        "class _Parser:\n"
        "    def __init__(self, tokens):\n"
        "        self._tokens = tokens\n"
        "        self._pos = 0\n"
        "\n"
        "    def _peek(self):\n"
        "        return self._tokens[self._pos][0] if self._pos < len(self._tokens) else None\n"
        "\n"
        "    def _advance(self):\n"
        "        token = self._tokens[self._pos]\n"
        "        self._pos += 1\n"
        "        return token\n"
        "\n"
        "    def parse(self):\n"
        "        value = self._expr()\n"
        "        if self._pos != len(self._tokens):\n"
        "            raise ValueError(\"trailing input\")\n"
        "        return value\n"
        "\n"
        "    def _expr(self):\n"
        "        value = self._term()\n"
        "        while self._peek() in (\"+\", \"-\"):\n"
        "            op = self._advance()[0]\n"
        "            rhs = self._term()\n"
        "            value = value + rhs if op == \"+\" else value - rhs\n"
        "        return value\n"
        "\n"
        "    def _term(self):\n"
        "        value = self._factor()\n"
        "        while self._peek() in (\"*\", \"/\"):\n"
        "            op = self._advance()[0]\n"
        "            rhs = self._factor()\n"
        "            if op == \"*\":\n"
        "                value = value * rhs\n"
        "            else:\n"
        "                if rhs == 0:\n"
        "                    raise ValueError(\"division by zero\")\n"
        "                value = value / rhs\n"
        "        return value\n"
        "\n"
        "    def _factor(self):\n"
        "        kind = self._peek()\n"
        "        if kind == \"num\":\n"
        "            return self._advance()[1]\n"
        "        if kind == \"(\":\n"
        "            self._advance()\n"
        "            value = self._expr()\n"
        "            if self._peek() != \")\":\n"
        "                raise ValueError(\"unbalanced parentheses\")\n"
        "            self._advance()\n"
        "            return value\n"
        "        raise ValueError(\"expected a number or '('\")\n"
        "\n"
        "\n"
        "def evaluate(expr):\n"
        "    tokens = _tokenize(expr)\n"
        "    if not tokens:\n"
        "        raise ValueError(\"empty expression\")\n"
        "    return _Parser(tokens).parse()\n"
    ),
    "l4-retry-backoff": (
        "\"\"\"Retry decorator factory.\"\"\"\n"
        "import functools\n"
        "\n"
        "\n"
        "def retry(attempts, exceptions=(Exception,), on_retry=None):\n"
        "    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:\n"
        "        raise ValueError(\"attempts must be an integer >= 1\")\n"
        "\n"
        "    def decorator(func):\n"
        "        @functools.wraps(func)\n"
        "        def wrapper(*args, **kwargs):\n"
        "            for attempt in range(1, attempts + 1):\n"
        "                try:\n"
        "                    return func(*args, **kwargs)\n"
        "                except exceptions as exc:\n"
        "                    if attempt == attempts:\n"
        "                        raise\n"
        "                    if on_retry is not None:\n"
        "                        on_retry(attempt, exc)\n"
        "            raise AssertionError(\"unreachable\")\n"
        "\n"
        "        return wrapper\n"
        "\n"
        "    return decorator\n"
    ),
    "l2-fix-xss": (
        "\"\"\"Renders a comment feed as HTML.\"\"\"\n"
        "import html\n"
        "\n"
        "\n"
        "def render_comment(author, body):\n"
        "    return (\n"
        "        '<li class=\"comment\">'\n"
        "        '<span class=\"author\">' + html.escape(author, quote=True) + '</span>'\n"
        "        '<p class=\"body\">' + html.escape(body, quote=True) + '</p>'\n"
        "        '</li>'\n"
        "    )\n"
        "\n"
        "\n"
        "def render_feed(comments):\n"
        "    items = \"\".join(render_comment(a, b) for a, b in comments)\n"
        "    return '<ul class=\"feed\">' + items + '</ul>'\n"
    ),
    "l2-fix-sql-injection": (
        "\"\"\"Tiny user repository over sqlite3.\"\"\"\n"
        "import sqlite3\n"
        "\n"
        "\n"
        "def connect():\n"
        "    conn = sqlite3.connect(\":memory:\")\n"
        "    conn.execute(\"CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)\")\n"
        "    conn.execute(\"INSERT INTO users (name, email) VALUES ('alice', 'a@example.com')\")\n"
        "    conn.execute(\"INSERT INTO users (name, email) VALUES ('bob', 'b@example.com')\")\n"
        "    conn.commit()\n"
        "    return conn\n"
        "\n"
        "\n"
        "def find_by_name(conn, name):\n"
        "    cur = conn.execute(\"SELECT id, name, email FROM users WHERE name = ?\", (name,))\n"
        "    return cur.fetchall()\n"
        "\n"
        "\n"
        "def find_by_name_and_email(conn, name, email):\n"
        "    cur = conn.execute(\n"
        "        \"SELECT id, name, email FROM users WHERE name = ? AND email = ?\",\n"
        "        (name, email),\n"
        "    )\n"
        "    return cur.fetchall()\n"
        "\n"
        "\n"
        "def add_user(conn, name, email):\n"
        "    conn.execute(\"INSERT INTO users (name, email) VALUES (?, ?)\", (name, email))\n"
        "    conn.commit()\n"
        "    return conn.execute(\"SELECT COUNT(*) FROM users\").fetchone()[0]\n"
        "\n"
        "\n"
        "def delete_by_name(conn, name):\n"
        "    conn.execute(\"DELETE FROM users WHERE name = ?\", (name,))\n"
        "    conn.commit()\n"
        "    return conn.execute(\"SELECT COUNT(*) FROM users\").fetchone()[0]\n"
    ),
    "l2-crud-handlers": (
        "\"\"\"In-memory CRUD store for task records.\"\"\"\n"
        "\n"
        "_FIELDS = (\"title\", \"done\")\n"
        "\n"
        "\n"
        "class TaskStore:\n"
        "    def __init__(self):\n"
        "        self._records = {}\n"
        "        self._next_id = 1\n"
        "\n"
        "    @staticmethod\n"
        "    def _validate_title(title):\n"
        "        if not isinstance(title, str) or not title.strip():\n"
        "            raise ValueError(\"title must be a non-empty string\")\n"
        "        return title\n"
        "\n"
        "    def create(self, title, done=False):\n"
        "        self._validate_title(title)\n"
        "        record = {\"id\": self._next_id, \"title\": title, \"done\": bool(done)}\n"
        "        self._records[record[\"id\"]] = record\n"
        "        self._next_id += 1\n"
        "        return dict(record)\n"
        "\n"
        "    def get(self, task_id):\n"
        "        record = self._records.get(task_id)\n"
        "        return dict(record) if record is not None else None\n"
        "\n"
        "    def list(self, done=None):\n"
        "        out = []\n"
        "        for record in self._records.values():\n"
        "            if done is not None and record[\"done\"] is not bool(done):\n"
        "                continue\n"
        "            out.append(dict(record))\n"
        "        return out\n"
        "\n"
        "    def update(self, task_id, **fields):\n"
        "        unknown = set(fields) - set(_FIELDS)\n"
        "        if unknown:\n"
        "            raise KeyError(f\"unknown field(s): {sorted(unknown)}\")\n"
        "        record = self._records.get(task_id)\n"
        "        if record is None:\n"
        "            return None\n"
        "        if \"title\" in fields:\n"
        "            self._validate_title(fields[\"title\"])\n"
        "            record[\"title\"] = fields[\"title\"]\n"
        "        if \"done\" in fields:\n"
        "            record[\"done\"] = bool(fields[\"done\"])\n"
        "        return dict(record)\n"
        "\n"
        "    def delete(self, task_id):\n"
        "        return self._records.pop(task_id, None) is not None\n"
    ),
    "l3-type-hardening": (
        "\"\"\"Parses a settings mapping into normalised values.\"\"\"\n"
        "from typing import Any, Mapping\n"
        "\n"
        "DEFAULT_PORT = 8080\n"
        "\n"
        "_MIN_PORT = 1\n"
        "_MAX_PORT = 65535\n"
        "\n"
        "\n"
        "def parse_port(settings: Mapping[str, Any]) -> int:\n"
        "    value = settings.get(\"port\", DEFAULT_PORT)\n"
        "    if isinstance(value, bool) or not isinstance(value, (int, str)):\n"
        "        raise TypeError(f\"port must be an int or numeric str, got {type(value).__name__}\")\n"
        "    try:\n"
        "        port = int(value)\n"
        "    except (TypeError, ValueError) as exc:\n"
        "        raise ValueError(f\"port is not a valid integer: {value!r}\") from exc\n"
        "    if not _MIN_PORT <= port <= _MAX_PORT:\n"
        "        raise ValueError(f\"port out of range: {port}\")\n"
        "    return port\n"
        "\n"
        "\n"
        "def parse_hosts(settings: Mapping[str, Any]) -> list[str]:\n"
        "    value = settings.get(\"hosts\")\n"
        "    if value is None:\n"
        "        return []\n"
        "    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):\n"
        "        raise TypeError(f\"hosts must be a list or tuple, got {type(value).__name__}\")\n"
        "    for item in value:\n"
        "        if not isinstance(item, str):\n"
        "            raise TypeError(f\"hosts entries must be str, got {type(item).__name__}\")\n"
        "    return list(value)\n"
        "\n"
        "\n"
        "def parse_debug(settings: Mapping[str, Any]) -> bool:\n"
        "    value = settings.get(\"debug\", False)\n"
        "    if not isinstance(value, bool):\n"
        "        raise TypeError(f\"debug must be a bool, got {type(value).__name__}\")\n"
        "    return value\n"
        "\n"
        "\n"
        "def parse_name(settings: Mapping[str, Any]) -> str | None:\n"
        "    value = settings.get(\"name\")\n"
        "    if value is None:\n"
        "        return None\n"
        "    if not isinstance(value, str):\n"
        "        raise TypeError(f\"name must be a str, got {type(value).__name__}\")\n"
        "    return value.strip()\n"
        "\n"
        "\n"
        "def parse_all(settings: Mapping[str, Any]) -> dict[str, Any]:\n"
        "    return {\n"
        "        \"port\": parse_port(settings),\n"
        "        \"hosts\": parse_hosts(settings),\n"
        "        \"debug\": parse_debug(settings),\n"
        "        \"name\": parse_name(settings),\n"
        "    }\n"
    ),
    "l3-fix-interval-overlap": (
        "\"\"\"Reservation conflict checks over half-open intervals [start, end).\"\"\"\n"
        "\n"
        "\n"
        "def overlaps(a_start, a_end, b_start, b_end):\n"
        "    return a_start < b_end and b_start < a_end\n"
        "\n"
        "\n"
        "def conflicts(new_booking, existing):\n"
        "    start, end = new_booking\n"
        "    for other_start, other_end in existing:\n"
        "        if overlaps(start, end, other_start, other_end):\n"
        "            return True\n"
        "    return False\n"
        "\n"
        "\n"
        "def first_free_slot(existing, duration, day_start=9, day_end=17):\n"
        "    for candidate in range(day_start, day_end - duration + 1):\n"
        "        if not conflicts((candidate, candidate + duration), existing):\n"
        "            return candidate\n"
        "    return None\n"
    ),
}


def reference_executor(case: ladder.LadderCase, tier: str) -> ladder.ExecutionOutput:
    content = REFERENCE.get(case.case_id)
    if content is None:
        return ladder.ExecutionOutput(error=f"no reference for {case.case_id}")
    return ladder.ExecutionOutput(content=content, model="reference", provider="stub")


def broken_executor(case: ladder.LadderCase, tier: str) -> ladder.ExecutionOutput:
    return ladder.ExecutionOutput(content="# does nothing\n", model="broken", provider="stub")


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(db_path=tmp_path / "cache.db")


def covered_cases() -> list[ladder.LadderCase]:
    """Shipped cases that have an inline reference solution here."""
    return [c for c in ladder.load_cases() if c.case_id in REFERENCE]


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


class TestLoadCases:
    def test_ships_cases_across_levels(self):
        cases = ladder.load_cases()
        assert cases, "no ladder cases are shipped"
        levels = {c.level for c in cases}
        assert 0 in levels
        assert max(levels) >= 5, "ladder should reach the harder rungs"

    def test_case_ids_are_unique(self):
        ids = [c.case_id for c in ladder.load_cases()]
        assert len(ids) == len(set(ids))

    def test_cases_are_level_ordered(self):
        levels = [c.level for c in ladder.load_cases()]
        assert levels == sorted(levels)

    def test_every_case_has_a_target_and_grader(self):
        for case in ladder.load_cases():
            assert case.target_file
            assert case.grader
            assert 0 <= case.level <= 6

    def test_every_case_ships_a_hidden_test(self):
        # The grader has to have something to run, or a pass would be meaningless.
        for case in ladder.load_cases():
            assert any(
                Path(rel).name.startswith("test_") for rel in case.seed
            ), f"{case.case_id} ships no test file"

    def test_filter_by_level(self):
        assert all(c.level == 0 for c in ladder.load_cases(levels=[0]))

    def test_filter_by_case_id(self):
        got = ladder.load_cases(case_ids=["l0-return-constant"])
        assert [c.case_id for c in got] == ["l0-return-constant"]

    def test_missing_root_returns_empty(self, tmp_path: Path):
        assert ladder.load_cases(tmp_path / "nope") == []

    def test_malformed_manifest_raises(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "bad"
        case_dir.mkdir(parents=True)
        (case_dir / "case.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError):
            ladder.load_case(case_dir)

    def test_missing_required_field_raises(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "bad"
        case_dir.mkdir(parents=True)
        (case_dir / "case.json").write_text(
            json.dumps({"id": "x", "level": 0, "prompt": "p"}), encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="target_file"):
            ladder.load_case(case_dir)

    def test_out_of_range_level_raises(self, tmp_path: Path):
        case_dir = tmp_path / "L9" / "bad"
        case_dir.mkdir(parents=True)
        (case_dir / "case.json").write_text(
            json.dumps({"id": "x", "level": 9, "prompt": "p", "target_file": "s.py"}),
            encoding="utf-8",
        )
        with pytest.raises(RuntimeError, match="0-6"):
            ladder.load_case(case_dir)


# ---------------------------------------------------------------------------
# Grader correctness — the load-bearing pair
# ---------------------------------------------------------------------------


def test_every_shipped_case_has_a_reference_solution():
    """The grader pair below parametrizes over REFERENCE, so a case missing from it
    is never asserted to accept a correct solution NOR to reject broken code — it
    can silently become unsolvable, which is exactly what tests/ladder/README.md
    warns about. `l3-expression-parser` and `l4-retry-backoff` had already drifted
    into that state, and nothing failed. `covered_cases()` existed to express this
    invariant and was dead code; this is the assertion that makes it load-bearing.
    """
    shipped = {c.case_id for c in ladder.load_cases()}
    covered = {c.case_id for c in covered_cases()}
    missing = sorted(shipped - covered)
    assert not missing, (
        "shipped ladder cases with no inline REFERENCE solution: "
        f"{missing}. Add one, or the grader is never verified in either direction."
    )


def test_every_shipped_case_declares_a_kind():
    """`kind` is the axis that answers "good at what"; a case without one is invisible
    to `build_min_passing_tier_by_kind` and contributes only to the difficulty axis.
    """
    kindless = sorted(c.case_id for c in ladder.load_cases() if not c.kind)
    assert not kindless, f"ladder cases missing a `kind` in case.json: {kindless}"


def test_case_kinds_are_all_recognised():
    """A case's `kind` must be one the planner can also derive from a real task.

    A typo in a `case.json` would otherwise create a private bucket that grades a
    model against work no live task is ever classified into — graded evidence that
    can never be applied.
    """
    from shared.task_kinds import KNOWN_KINDS

    unknown = sorted(
        f"{c.case_id}:{c.kind}" for c in ladder.load_cases()
        if c.kind and c.kind not in KNOWN_KINDS
    )
    assert not unknown, f"ladder kinds outside task_kinds.KNOWN_KINDS: {unknown}"


class TestGraderAcceptsCorrectSolutions:
    @pytest.mark.parametrize("case_id", sorted(REFERENCE))
    def test_reference_solution_passes(self, case_id: str):
        cases = ladder.load_cases(case_ids=[case_id])
        assert cases, f"case {case_id} is not shipped"
        result = ladder.run_case(cases[0], "high", executor=reference_executor)
        assert result.passed, (
            f"grader for {case_id} rejects a correct solution:\n{result.grader_output}"
        )


class TestGraderRejectsWrongSolutions:
    @pytest.mark.parametrize("case_id", sorted(REFERENCE))
    def test_broken_solution_fails(self, case_id: str):
        cases = ladder.load_cases(case_ids=[case_id])
        result = ladder.run_case(cases[0], "low", executor=broken_executor)
        assert not result.passed, f"grader for {case_id} accepts broken code"

    def test_empty_output_fails_with_an_error(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        result = ladder.run_case(
            cases[0], "low", executor=lambda c, t: ladder.ExecutionOutput(content="  ")
        )
        assert result.passed is False
        assert result.error == "empty output"

    def test_executor_error_is_recorded_not_raised(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        result = ladder.run_case(
            cases[0], "low", executor=lambda c, t: ladder.ExecutionOutput(error="provider down")
        )
        assert result.passed is False
        assert result.error == "provider down"


# ---------------------------------------------------------------------------
# Sandbox behavior
# ---------------------------------------------------------------------------


class TestSandbox:
    def test_materialize_writes_seed_files(self, tmp_path: Path):
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        sandbox = ladder.materialize(case, tmp_path / "box")
        for rel in case.seed:
            assert (sandbox / rel).is_file()

    def test_repo_is_not_touched(self, tmp_path: Path):
        # The runner must never write into the case directory itself.
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        before = sorted(p.name for p in Path(case.source).rglob("*"))
        ladder.run_case(case, "high", executor=reference_executor)
        after = sorted(p.name for p in Path(case.source).rglob("*"))
        assert before == after

    def test_code_fence_is_stripped(self, tmp_path: Path):
        case = ladder.load_cases(case_ids=["l0-return-constant"])[0]
        fenced = "```python\ndef answer():\n    return 42\n```\n"
        result = ladder.run_case(
            case, "high", executor=lambda c, t: ladder.ExecutionOutput(content=fenced)
        )
        assert result.passed, "a fenced response should still grade as correct"

    def test_grader_timeout_is_a_failure(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "slow"
        (case_dir / "seed").mkdir(parents=True)
        (case_dir / "seed" / "test_x.py").write_text("def test_x():\n    pass\n")
        (case_dir / "case.json").write_text(json.dumps({
            "id": "slow", "level": 0, "prompt": "p", "target_file": "s.py",
            "grader": "python3 -c \"import time; time.sleep(5)\"",
            "grader_timeout_seconds": 1,
        }), encoding="utf-8")
        case = ladder.load_case(case_dir)
        result = ladder.run_case(
            case, "low", executor=lambda c, t: ladder.ExecutionOutput(content="x = 1\n")
        )
        assert result.passed is False
        assert "timed out" in result.grader_output

    def test_unparseable_grader_is_a_failure(self, tmp_path: Path):
        case_dir = tmp_path / "L0" / "badcmd"
        (case_dir / "seed").mkdir(parents=True)
        (case_dir / "case.json").write_text(json.dumps({
            "id": "badcmd", "level": 0, "prompt": "p", "target_file": "s.py",
            "grader": '"unterminated',
        }), encoding="utf-8")
        passed, output = ladder.grade(ladder.load_case(case_dir), tmp_path)
        assert passed is False
        assert "unparseable" in output


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestBuildCasePrompt:
    def test_includes_task_and_output_contract(self):
        case = ladder.load_cases(case_ids=["l0-return-constant"])[0]
        prompt = ladder.build_case_prompt(case)
        assert case.prompt.strip() in prompt
        assert case.target_file in prompt
        assert "no markdown fences" in prompt

    def test_hidden_tests_are_not_leaked(self):
        # Showing the grader's tests would make the benchmark measure nothing.
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        prompt = ladder.build_case_prompt(case)
        assert "test_solution.py" not in prompt
        assert "def test_" not in prompt

    def test_visible_seed_files_are_included(self):
        case = ladder.load_cases(case_ids=["l0-fix-off-by-one"])[0]
        prompt = ladder.build_case_prompt(case)
        assert "def last_index" in prompt


# ---------------------------------------------------------------------------
# Aggregation + ledger
# ---------------------------------------------------------------------------


class TestMinPassingTier:
    def _r(self, level, tier, case_id, passed):
        return ladder.LadderResult(
            case_id=case_id, level=level, tier=tier, passed=passed, model="m"
        )

    def test_cheapest_passing_tier_wins(self):
        results = [
            self._r(0, "low", "a", True),
            self._r(0, "high", "a", True),
        ]
        assert ladder.min_passing_tier_by_level(results) == {0: "low"}

    def test_escalates_when_cheap_tier_fails(self):
        results = [
            self._r(2, "low", "a", False),
            self._r(2, "medium", "a", True),
        ]
        assert ladder.min_passing_tier_by_level(results) == {2: "medium"}

    def test_level_absent_when_nothing_passes(self):
        results = [self._r(3, "low", "a", False), self._r(3, "high", "a", False)]
        assert ladder.min_passing_tier_by_level(results) == {}

    def test_partial_sweep_does_not_count(self):
        # One of two cases passing is not evidence the tier handles the level.
        results = [
            self._r(1, "low", "a", True),
            self._r(1, "low", "b", False),
            self._r(1, "high", "a", True),
            self._r(1, "high", "b", True),
        ]
        assert ladder.min_passing_tier_by_level(results) == {1: "high"}

    def test_empty_results(self):
        assert ladder.min_passing_tier_by_level([]) == {}


class TestSummarize:
    def test_pass_rate_per_tier(self):
        results = [
            ladder.LadderResult("a", 0, "low", True),
            ladder.LadderResult("b", 1, "low", False),
            ladder.LadderResult("a", 0, "high", True),
        ]
        summary = ladder.summarize(results)
        assert summary["total_runs"] == 3
        assert summary["by_tier"]["low"]["pass_rate"] == 0.5
        assert summary["by_tier"]["high"]["pass_rate"] == 1.0

    def test_render_handles_no_sweep(self):
        summary = ladder.summarize([ladder.LadderResult("a", 0, "low", False)])
        text = ladder.render_summary(summary)
        assert "No level was swept cleanly" in text
        assert "Failures:" in text

    def test_render_lists_min_tiers(self):
        summary = ladder.summarize([ladder.LadderResult("a", 0, "low", True)])
        assert "L0: low" in ladder.render_summary(summary)


class TestLedgerRecording:
    def test_run_ladder_records_events(self, db: Database):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        ladder.run_ladder(
            cases=cases, tiers=("low",), executor=reference_executor, db=db
        )
        with db.conn() as conn:
            rows = conn.execute(
                "SELECT source, sub_dimension, score_0_10 FROM model_quality_events"
            ).fetchall()
        assert rows == [("ladder", "L0", 10.0)]

    def test_failure_records_zero(self, db: Database):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        ladder.run_ladder(cases=cases, tiers=("low",), executor=broken_executor, db=db)
        with db.conn() as conn:
            score = conn.execute("SELECT score_0_10 FROM model_quality_events").fetchone()[0]
        assert score == 0.0

    def test_min_passing_tier_map_from_ledger(self, db: Database):
        cases = ladder.load_cases(case_ids=["l0-return-constant", "l2-lru-cache"])

        def tiered(case, tier):
            if tier == "high" or case.level == 0:
                return reference_executor(case, tier)
            return ladder.ExecutionOutput(content="# nope\n", model=f"model-{tier}")

        # Attribute distinct models per tier so the map is per-model.
        def tiered_named(case, tier):
            out = tiered(case, tier)
            return ladder.ExecutionOutput(
                content=out.content, error=out.error, model=f"model-{tier}", provider="stub"
            )

        ladder.run_ladder(
            cases=cases, tiers=("low", "high"), executor=tiered_named, db=db
        )
        from shared.model_quality import build_min_passing_tier_map

        mapping = build_min_passing_tier_map(db)
        assert mapping["model-low"] == {"L0": "low"}
        assert mapping["model-high"]["L2"] == "high"

    def test_no_db_still_runs(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        results = ladder.run_ladder(cases=cases, tiers=("low",), executor=reference_executor)
        assert len(results) == 1 and results[0].passed

    def test_unknown_tiers_are_ignored(self):
        cases = ladder.load_cases(case_ids=["l0-return-constant"])
        results = ladder.run_ladder(
            cases=cases, tiers=("bogus",), executor=reference_executor
        )
        assert results == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_list_prints_cases(self, capsys):
        assert ladder.main(["list"]) == 0
        out = capsys.readouterr().out
        assert "l0-return-constant" in out

    def test_list_respects_level_filter(self, capsys):
        assert ladder.main(["list", "--level", "0"]) == 0
        out = capsys.readouterr().out
        assert "l0-return-constant" in out
        assert "l6-event-store" not in out

    def test_default_command_is_list(self, capsys):
        assert ladder.main([]) == 0
        assert "l0-return-constant" in capsys.readouterr().out

    def test_run_rejects_unknown_tier(self, capsys):
        assert ladder.main(["run", "--tier", "turbo"]) == 1
        assert "unknown tier" in capsys.readouterr().out

    def test_run_rejects_empty_case_selection(self, capsys):
        assert ladder.main(["run", "--case", "does-not-exist"]) == 1
        assert "no matching ladder cases" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Staleness — a tier→model change silently invalidates that tier's evidence
# ---------------------------------------------------------------------------


class TestStaleTiers:
    """The ledger already records which model produced each verdict, so "was this
    tier's evidence collected on the model it uses now" needs no extra storage.

    Without this, the competence tables read as current while describing a model the
    tier no longer resolves to — worse than having no data, because it looks
    authoritative.
    """

    @staticmethod
    def _seed(db: Database, mapping: dict[str, str]) -> None:
        from shared import model_quality as mq

        for tier, model in mapping.items():
            mq.record_ladder_score(
                db, model=model, effort=None, level_label="L0", passed=True,
                tier=tier, case_id="c1", run_id="ladder-1", kind="bugfix",
            )

    def test_changed_model_is_reported_stale(self, db: Database) -> None:
        self._seed(db, {"low": "haiku", "medium": "sonnet-4-5", "high": "opus"})
        current = {"low": "haiku", "medium": "sonnet-4-6", "high": "opus"}
        assert ladder.stale_tiers(db, current) == ["medium"]

    def test_unchanged_mapping_is_not_stale(self, db: Database) -> None:
        mapping = {"low": "haiku", "medium": "sonnet-4-5", "high": "opus"}
        self._seed(db, mapping)
        assert ladder.stale_tiers(db, mapping) == []

    def test_never_graded_tier_is_absent_not_stale(self, db: Database) -> None:
        """"Stale" presupposes something was once fresh. A tier with no graded rows
        is simply missing from the competence tables."""
        self._seed(db, {"low": "haiku"})
        assert ladder.stale_tiers(db, {"low": "haiku", "high": "opus"}) == []

    def test_empty_ledger_yields_nothing(self, db: Database) -> None:
        assert ladder.stale_tiers(db, {"low": "haiku", "high": "opus"}) == []

    def test_tier_absent_from_current_mapping_is_ignored(self, db: Database) -> None:
        self._seed(db, {"low": "haiku", "high": "opus"})
        assert ladder.stale_tiers(db, {"low": "haiku"}) == []

    def test_graded_models_by_tier_reads_the_tier_column(self, db: Database) -> None:
        from shared.model_quality import graded_models_by_tier

        self._seed(db, {"low": "haiku", "high": "opus"})
        assert graded_models_by_tier(db) == {"low": {"haiku"}, "high": {"opus"}}


class TestLadderRootOverride:
    def test_env_override_redirects_the_case_root(self, monkeypatch, tmp_path):
        """A module constant used as a default argument would freeze at import, so
        the root has to resolve at call time for an override to apply."""
        monkeypatch.setenv("THRENODY_LADDER_DIR", str(tmp_path / "nowhere"))
        assert ladder.ladder_dir() == tmp_path / "nowhere"
        assert ladder.load_cases() == []

    def test_default_root_ships_the_bundled_cases(self, monkeypatch):
        monkeypatch.delenv("THRENODY_LADDER_DIR", raising=False)
        assert len(ladder.load_cases()) > 0
