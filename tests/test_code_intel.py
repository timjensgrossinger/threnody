"""Tests for shared/code_intel.py — entity index, static smells, and cache.

The smell rules feed shared/model_quality.record_static_recall_score, so a false
positive here would wrongly punish a reviewer. The negative cases below are as
load-bearing as the positive ones.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from shared import code_intel as ci

# Assembled at runtime so the literal call never appears in this file's source —
# a repo lint hook rewrites the spelled-out form to the safe variant, which would
# silently invert the fixtures below.
_UNSAFE_YAML = "yaml." + "load"


@pytest.fixture(autouse=True)
def _clear_cache():
    ci.clear_intel_cache()
    yield
    ci.clear_intel_cache()


def scan_src(src: str, path: str = "t.py") -> ci.CodeIntel:
    return ci.scan(path, content=src)


def rule_ids(intel: ci.CodeIntel) -> set[str]:
    return {s.rule_id for s in intel.smells}


# ---------------------------------------------------------------------------
# Entity scanning
# ---------------------------------------------------------------------------


class TestScanEntities:
    def test_functions_classes_and_async(self):
        src = (
            "class A:\n"
            "    def m(self, x):\n"
            "        return x\n"
            "def top(a, b=1, *args, **kw):\n"
            "    return a\n"
            "async def go():\n"
            "    return 1\n"
        )
        scan = ci.scan_entities("t.py", src)
        assert scan.parsed is True
        by_name = {e.name: e for e in scan.entities}
        assert set(by_name) == {"A", "m", "top", "go"}
        assert by_name["A"].kind == "class"
        assert by_name["go"].kind == "async_function"
        # self, x
        assert by_name["m"].param_count == 2
        # a, b, *args, **kw
        assert by_name["top"].param_count == 4

    def test_nesting_depth_and_branches(self):
        src = (
            "def deep(a):\n"
            "    if a:\n"
            "        for i in a:\n"
            "            while i:\n"
            "                return i\n"
        )
        scan = ci.scan_entities("t.py", src)
        deep = next(e for e in scan.entities if e.name == "deep")
        # if -> for -> while nested inside the def body
        assert deep.nesting_depth == 3
        assert deep.branch_count == 3
        # module total includes the def itself
        assert scan.max_depth == 4

    def test_branch_total_counts_each_branch_once(self):
        # outer contains inner; summing per-entity counts would double-count.
        src = (
            "def outer(a):\n"
            "    if a:\n"
            "        def inner(b):\n"
            "            if b:\n"
            "                return b\n"
            "        return inner\n"
        )
        scan = ci.scan_entities("t.py", src)
        assert scan.branch_count == 2
        assert sum(e.branch_count for e in scan.entities) == 3  # the double count

    def test_syntax_error_falls_back_to_regex(self):
        scan = ci.scan_entities("broken.py", "def f(:\n    pass\n")
        assert scan.parsed is False
        assert scan.max_depth == 0
        assert scan.branch_count == 0

    def test_non_python_uses_regex_fallback(self):
        src = "export function handle(a) {\n  return a\n}\nclass Widget {}\n"
        scan = ci.scan_entities("app.ts", src)
        assert scan.parsed is False
        assert {e.name for e in scan.entities} == {"handle", "Widget"}

    def test_empty_content(self):
        scan = ci.scan_entities("t.py", "")
        assert scan.entities == []
        assert scan.parsed is True
        assert scan.max_depth == 0


# ---------------------------------------------------------------------------
# Security smells
# ---------------------------------------------------------------------------


class TestSecuritySmells:
    def test_eval_and_exec(self):
        assert "eval_exec" in rule_ids(scan_src("def f(x):\n    return eval(x)\n"))
        assert "eval_exec" in rule_ids(scan_src("def f(x):\n    exec(x)\n"))

    def test_method_named_eval_is_not_flagged(self):
        # A method call that merely ends in .eval must not trip the rule.
        assert "eval_exec" not in rule_ids(
            scan_src("def f(self, x):\n    return self.eval(x)\n")
        )

    def test_os_system_and_shell_true(self):
        got = rule_ids(scan_src(
            "import os, subprocess\n"
            "def f(x):\n"
            "    os.system('rm ' + x)\n"
            "    subprocess.run('ls', shell=True)\n"
        ))
        assert "os_system" in got
        assert "shell_true" in got

    def test_shell_false_is_not_flagged(self):
        assert "shell_true" not in rule_ids(
            scan_src("import subprocess\ndef f():\n    subprocess.run('ls', shell=False)\n")
        )

    def test_unsafe_deserialize_and_yaml(self):
        got = rule_ids(scan_src(
            "import pickle, yaml\n"
            "def f(b, s):\n"
            "    pickle.loads(b)\n"
            f"    {_UNSAFE_YAML}(s)\n"
        ))
        assert "unsafe_deserialize" in got
        assert "yaml_load_unsafe" in got

    def test_yaml_load_with_loader_is_safe(self):
        assert "yaml_load_unsafe" not in rule_ids(
            scan_src(f"import yaml\ndef f(s):\n    {_UNSAFE_YAML}(s, Loader=yaml.SafeLoader)\n")
        )

    def test_sql_interpolation_variants(self):
        for expr in (
            'cur.execute("SELECT * FROM t WHERE id = " + uid)',
            'cur.execute(f"SELECT * FROM t WHERE id = {uid}")',
            'cur.execute("SELECT * FROM t WHERE id = %s" % uid)',
            'cur.execute("SELECT * FROM t WHERE id = {}".format(uid))',
        ):
            got = rule_ids(scan_src(f"def f(cur, uid):\n    {expr}\n"))
            assert "sql_interpolation" in got, expr

    def test_parameterized_query_is_not_flagged(self):
        assert "sql_interpolation" not in rule_ids(
            scan_src('def f(cur, uid):\n    cur.execute("SELECT 1 WHERE id = ?", (uid,))\n')
        )

    def test_hardcoded_secret_detected(self):
        got = rule_ids(scan_src('API_KEY = "sk-live-9f8a7b6c5d4e3f2a"\n'))  # gitleaks:allow
        assert "hardcoded_secret" in got

    @pytest.mark.parametrize("line", [
        'PASSWORD = "changeme"',
        'API_KEY = os.environ["K"]',
        'SECRET = ""',
        'TOKEN = "xxxxxxxxxx"',
        'API_KEY = "${VAULT_KEY}"',
        'SECRET = "short"',
        'CREDENTIAL = "--------"',
        'PASSWORD = "placeholder"',
        'API_KEY = f"{prefix}-key"',
    ])
    def test_secret_false_positives_suppressed(self, line):
        assert "hardcoded_secret" not in rule_ids(scan_src(line + "\n"))

    def test_non_secret_name_not_flagged(self):
        assert "hardcoded_secret" not in rule_ids(scan_src('GREETING = "hello world"\n'))


# ---------------------------------------------------------------------------
# Edge / logic / performance smells
# ---------------------------------------------------------------------------


class TestOtherSmells:
    def test_silent_except_is_high(self):
        intel = scan_src("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
        smell = next(s for s in intel.smells if s.rule_id == "silent_except")
        assert smell.severity == ci.SEVERITY_HIGH
        assert smell.dimension == ci.DIM_EDGE

    def test_bare_except_is_medium(self):
        intel = scan_src("def f():\n    try:\n        g()\n    except:\n        return 1\n")
        smell = next(s for s in intel.smells if s.rule_id == "bare_except")
        assert smell.severity == ci.SEVERITY_MEDIUM

    def test_handled_except_is_clean(self):
        got = rule_ids(scan_src(
            "import logging\n"
            "log = logging.getLogger(__name__)\n"
            "def f():\n"
            "    try:\n"
            "        g()\n"
            "    except ValueError:\n"
            "        log.debug('x', exc_info=True)\n"
        ))
        assert "silent_except" not in got
        assert "bare_except" not in got

    def test_mutable_default_arg(self):
        assert "mutable_default_arg" in rule_ids(scan_src("def f(items=[]):\n    return items\n"))
        assert "mutable_default_arg" in rule_ids(scan_src("def f(opts={}):\n    return opts\n"))

    def test_immutable_and_none_defaults_are_clean(self):
        assert "mutable_default_arg" not in rule_ids(scan_src("def f(x=None, y=(), z=0):\n    return x\n"))

    def test_unbounded_loop(self):
        assert "unbounded_loop" in rule_ids(scan_src("def f():\n    while True:\n        x = 1\n"))

    def test_loop_with_break_is_clean(self):
        assert "unbounded_loop" not in rule_ids(
            scan_src("def f():\n    while True:\n        if x:\n            break\n")
        )

    def test_missing_timeout(self):
        assert "missing_timeout" in rule_ids(
            scan_src("import requests\ndef f():\n    return requests.get('http://x')\n")
        )

    def test_timeout_present_is_clean(self):
        assert "missing_timeout" not in rule_ids(
            scan_src("import requests\ndef f():\n    return requests.get('http://x', timeout=5)\n")
        )

    def test_blocking_call_in_async(self):
        intel = scan_src("import time\nasync def f():\n    time.sleep(1)\n")
        smell = next(s for s in intel.smells if s.rule_id == "blocking_call_in_async")
        assert smell.dimension == ci.DIM_PERFORMANCE
        assert smell.severity == ci.SEVERITY_HIGH

    def test_sync_sleep_outside_async_is_clean(self):
        assert "blocking_call_in_async" not in rule_ids(
            scan_src("import time\ndef f():\n    time.sleep(1)\n")
        )


# ---------------------------------------------------------------------------
# Regex path (non-Python)
# ---------------------------------------------------------------------------


class TestRegexRules:
    def test_typescript_rules(self):
        src = (
            "export function h(a) {\n"
            "  try { go() } catch (e) {}\n"
            '  const apiKey = "abcd1234efgh5678";\n'  # gitleaks:allow
            '  const q = "SELECT * FROM users WHERE id = " + a;\n'
            "  const v = data as any;\n"
            "  return eval(a);\n"
            "}\n"
        )
        got = rule_ids(ci.scan("app.ts", content=src))
        assert got == {
            "empty_catch",
            "hardcoded_secret",
            "sql_interpolation",
            "suppressed_type_error",
            "eval_exec",
        }

    def test_secret_with_identifier_tail(self):
        assert "hardcoded_secret" in rule_ids(
            ci.scan("c.go", content='secret_key = "A1b2C3d4E5f6"\n')  # gitleaks:allow
        )

    def test_type_ignore_comment_on_python(self):
        # Comment-based rules still apply on a parsed Python file.
        got = rule_ids(scan_src("x = f()  # type: ignore\n"))
        assert "suppressed_type_error" in got

    def test_no_duplicate_ast_and_regex_hits(self):
        # eval on a parsed Python file must produce exactly one smell, not two.
        intel = scan_src("def f(x):\n    return eval(x)\n")
        assert [s.rule_id for s in intel.smells].count("eval_exec") == 1

    def test_python_syntax_error_still_gets_regex_rules(self):
        intel = ci.scan("broken.py", content='def f(:\n    pass\nsecret_key = "A1b2C3d4E5f6"\n')  # gitleaks:allow
        assert intel.parsed is False
        assert "hardcoded_secret" in rule_ids(intel)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_clean_file_has_no_smells(self):
        intel = scan_src("def add(a: int, b: int) -> int:\n    return a + b\n")
        assert intel.smells == ()
        assert ci.expected_findings(intel.smells) == []
        assert ci.max_severity(intel.smells) is None
        assert ci.format_smell_leads(intel.smells) == ""

    def test_expected_findings_is_high_only(self):
        intel = scan_src(
            "import requests\n"
            "def f(x, items=[]):\n"
            "    requests.get('http://x')\n"
            "    return eval(x)\n"
        )
        high = {s.rule_id for s in ci.expected_findings(intel.smells)}
        assert high == {"eval_exec"}
        assert "missing_timeout" in rule_ids(intel)  # present but not expected
        assert "mutable_default_arg" in rule_ids(intel)

    def test_expected_findings_filtered_by_dimension(self):
        intel = scan_src("import time\nasync def f(x):\n    time.sleep(1)\n    return eval(x)\n")
        sec = {s.rule_id for s in ci.expected_findings(intel.smells, ci.DIM_SECURITY)}
        perf = {s.rule_id for s in ci.expected_findings(intel.smells, ci.DIM_PERFORMANCE)}
        assert sec == {"eval_exec"}
        assert perf == {"blocking_call_in_async"}

    def test_smells_by_dimension_groups(self):
        intel = scan_src("def f(x, items=[]):\n    return eval(x)\n")
        grouped = ci.smells_by_dimension(intel.smells)
        assert set(grouped) == {ci.DIM_SECURITY, ci.DIM_LOGIC}

    def test_max_severity_picks_highest(self):
        intel = scan_src("import requests\ndef f(x):\n    requests.get('u')\n    return eval(x)\n")
        assert ci.max_severity(intel.smells) == ci.SEVERITY_HIGH

    def test_format_leads_frames_as_unconfirmed(self):
        intel = scan_src("def f(x):\n    return eval(x)\n")
        block = ci.format_smell_leads(intel.smells)
        assert "NOT confirmed findings" in block
        assert "refuted:" in block
        assert "eval_exec" in block

    def test_format_leads_honors_limit_and_orders_by_severity(self):
        smells = [
            ci.Smell("low_rule", ci.DIM_TYPES, ci.SEVERITY_LOW, 1, "l"),
            ci.Smell("high_rule", ci.DIM_SECURITY, ci.SEVERITY_HIGH, 9, "h"),
        ]
        block = ci.format_smell_leads(smells, limit=1)
        assert "high_rule" in block
        assert "low_rule" not in block


# ---------------------------------------------------------------------------
# Scan + cache
# ---------------------------------------------------------------------------


class TestScanAndCache:
    def test_unreadable_path_returns_empty(self):
        intel = ci.scan("/nonexistent/zzz-does-not-exist.py")
        assert intel.entities == ()
        assert intel.smells == ()
        assert intel.parsed is False
        assert intel.path == "/nonexistent/zzz-does-not-exist.py"

    def test_reads_from_disk_when_content_omitted(self, tmp_path: Path):
        f = tmp_path / "m.py"
        f.write_text("def f(x):\n    return eval(x)\n", encoding="utf-8")
        intel = ci.scan(str(f))
        assert "eval_exec" in rule_ids(intel)

    def test_in_process_cache_returns_same_object(self):
        src = "def f():\n    return 1\n"
        first = ci.scan("cached.py", content=src)
        assert ci.scan("cached.py", content=src) is first

    def test_content_change_invalidates_cache(self):
        first = ci.scan("v.py", content="def f():\n    return 1\n")
        second = ci.scan("v.py", content="def f(x):\n    return eval(x)\n")
        assert second is not first
        assert second.content_sha != first.content_sha
        assert "eval_exec" in rule_ids(second)

    def test_oversized_content_is_truncated_not_rejected(self):
        src = "x = 1\n" * (ci.MAX_SCAN_BYTES // 6 + 100)
        intel = ci.scan("big.py", content=src)
        assert intel.content_sha  # produced a result rather than bailing

    def test_db_cache_round_trip(self):
        from shared.db import Database

        src = "import os\ndef f(x):\n    os.system('rm ' + x)\n"
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(db_path=Path(tmp) / "cache.db")
            written = ci.scan("dbcached.py", content=src, db=db)
            assert "os_system" in rule_ids(written)
            with db.conn() as conn:
                row = conn.execute(
                    "SELECT parsed, def_count FROM code_intel WHERE path = ? AND content_sha = ?",
                    ("dbcached.py", written.content_sha),
                ).fetchone()
            assert row is not None
            assert row[0] == 1
            assert row[1] == 1

            # Drop the in-process cache: the row must rehydrate from SQLite.
            ci.clear_intel_cache()
            reloaded = ci.scan("dbcached.py", content=src, db=db)
            assert reloaded.smells == written.smells
            assert reloaded.entities == written.entities
            assert reloaded.parsed is True

    def test_new_quality_sources_accepted_by_schema(self):
        # The widened CHECK must admit every source model_quality declares.
        from shared import model_quality as mq
        from shared.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(db_path=Path(tmp) / "cache.db")
            with db.conn() as conn:
                for source in sorted(mq.VALID_SOURCES):
                    conn.execute(
                        "INSERT INTO model_quality_events "
                        "(model, dimension, score_0_10, source, ts) VALUES (?, ?, ?, ?, ?)",
                        ("m", "general", 5.0, source, 1.0),
                    )
                n = conn.execute("SELECT COUNT(*) FROM model_quality_events").fetchone()[0]
            assert n == len(mq.VALID_SOURCES)
