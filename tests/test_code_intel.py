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


# ---------------------------------------------------------------------------
# Rules added to make `logic` and `types` gradable at all
# ---------------------------------------------------------------------------
#
# `expected_findings` filters to SEVERITY_HIGH, and `record_static_recall_score`
# early-returns on an empty expected set — so before these rules existed, a logic
# reviewer and a types reviewer could never be graded against anything.
#
# Precision matters more than recall here in a way that is easy to get backwards:
# this scan is the yardstick reviewers are graded against, so a false positive does
# not merely add noise, it marks a CORRECT reviewer as having missed a defect that
# was never there. Every negative case below is therefore as load-bearing as the
# positive ones.


class TestAddedHighSeverityRules:
    @staticmethod
    def _high(code: str) -> set[str]:
        from shared.code_intel import expected_findings, scan_smells

        return {s.rule_id for s in expected_findings(scan_smells("t.py", code))}

    # --- logic: unreachable_code ------------------------------------------------

    def test_return_then_statement_in_same_block(self):
        assert "unreachable_code" in self._high(
            "def f(x):\n    return x\n    y = 1\n"
        )

    def test_break_then_statement_in_loop_body(self):
        assert "unreachable_code" in self._high(
            "def f(xs):\n    for x in xs:\n        break\n        print(x)\n"
        )

    def test_raise_then_statement_inside_try(self):
        assert "unreachable_code" in self._high(
            "def f():\n"
            "    try:\n"
            "        raise ValueError('x')\n"
            "        print('dead')\n"
            "    except ValueError:\n"
            "        pass\n"
        )

    def test_return_inside_if_does_not_condemn_later_code(self):
        """Only the SAME block is examined — an early return is not a defect."""
        assert "unreachable_code" not in self._high(
            "def f(x):\n"
            "    if x:\n"
                "        return 1\n"
            "    y = x + 1\n"
            "    return y\n"
        )

    # --- logic: duplicate_branch_condition --------------------------------------

    def test_repeated_elif_condition(self):
        assert "duplicate_branch_condition" in self._high(
            "def f(x):\n"
            "    if x > 1:\n"
            "        return 'a'\n"
            "    elif x > 1:\n"
            "        return 'b'\n"
            "    return 'c'\n"
        )

    def test_distinct_elif_conditions_are_fine(self):
        assert "duplicate_branch_condition" not in self._high(
            "def f(x):\n"
            "    if x > 1:\n"
            "        return 'a'\n"
            "    elif x > 2:\n"
            "        return 'b'\n"
            "    return 'c'\n"
        )

    # --- types: none_annotated_returns_value ------------------------------------

    def test_none_annotated_returning_a_value(self):
        assert "none_annotated_returns_value" in self._high(
            "def f(x) -> None:\n    return x\n"
        )

    def test_bare_return_under_none_annotation_is_fine(self):
        assert "none_annotated_returns_value" not in self._high(
            "def f(x) -> None:\n    if x:\n        return\n    return\n"
        )

    def test_explicit_return_none_is_fine(self):
        assert "none_annotated_returns_value" not in self._high(
            "def f() -> None:\n    return None\n"
        )

    def test_nested_function_return_is_not_blamed_on_the_outer(self):
        """A closure's return says nothing about the enclosing signature."""
        assert "none_annotated_returns_value" not in self._high(
            "def outer() -> None:\n"
            "    def inner():\n"
            "        return 42\n"
            "    inner()\n"
        )

    def test_nested_class_method_return_is_not_blamed(self):
        assert "none_annotated_returns_value" not in self._high(
            "def outer() -> None:\n"
            "    class C:\n"
            "        def m(self):\n"
            "            return 1\n"
            "    C()\n"
        )

    # --- security ---------------------------------------------------------------

    def test_unverified_tls_context(self):
        assert "tls_verify_disabled" in self._high(
            "import ssl\nctx = ssl._create_unverified_context()\n"
        )

    def test_verify_false_keyword(self):
        assert "tls_verify_disabled" in self._high(
            "import requests\nrequests.get('https://x', verify=False)\n"
        )

    def test_verify_true_is_not_flagged(self):
        assert "tls_verify_disabled" not in self._high(
            "import requests\nrequests.get('https://x', verify=True)\n"
        )

    def test_extractall_without_filtering(self):
        assert "unsafe_archive_extract" in self._high(
            "import tarfile\n"
            "def go(p):\n"
            "    with tarfile.open(p) as t:\n"
            "        t.extractall('/tmp/out')\n"
        )

    def test_extractall_with_members_is_not_flagged(self):
        assert "unsafe_archive_extract" not in self._high(
            "import tarfile\n"
            "def go(p, names):\n"
            "    with tarfile.open(p) as t:\n"
            "        t.extractall('/tmp/out', members=names)\n"
        )

    def test_mark_safe_on_a_non_literal(self):
        assert "unescaped_html_output" in self._high(
            "from django.utils.safestring import mark_safe\n"
            "def r(v):\n    return mark_safe(v)\n"
        )

    def test_mark_safe_on_a_literal_is_not_flagged(self):
        """A string literal is developer-controlled, not attacker-controlled."""
        assert "unescaped_html_output" not in self._high(
            "from django.utils.safestring import mark_safe\n"
            "def r():\n    return mark_safe('<b>static</b>')\n"
        )

    def test_csrf_exempt_decorator(self):
        assert "csrf_exempt" in self._high(
            "@csrf_exempt\ndef view(request):\n    return None\n"
        )

    # --- non-Python side --------------------------------------------------------

    def test_js_inner_html_from_a_variable(self):
        from shared.code_intel import expected_findings, scan_smells

        hits = {
            s.rule_id
            for s in expected_findings(
                scan_smells("a.js", "function show(el, v) { el.innerHTML = v; }")
            )
        }
        assert "unescaped_html_output" in hits

    def test_js_inner_html_cleared_with_empty_string_is_not_flagged(self):
        """`el.innerHTML = ""` is the ordinary clear-the-node idiom."""
        from shared.code_intel import expected_findings, scan_smells

        hits = {
            s.rule_id
            for s in expected_findings(
                scan_smells("a.js", 'function clear(el) { el.innerHTML = ""; }')
            )
        }
        assert "unescaped_html_output" not in hits

    def test_js_reject_unauthorized_false(self):
        from shared.code_intel import expected_findings, scan_smells

        hits = {
            s.rule_id
            for s in expected_findings(
                scan_smells("a.js", "const o = { rejectUnauthorized: false };")
            )
        }
        assert "tls_verify_disabled" in hits

    # --- precision at repo scale -------------------------------------------------

    def test_added_rules_are_silent_on_this_repository(self):
        """Zero false positives across the repo's own source.

        A regression here means a reviewer of one of these files would be graded as
        having missed a defect that does not exist.
        """
        from pathlib import Path as _Path

        from shared.code_intel import expected_findings, scan_smells

        added = {
            "tls_verify_disabled",
            "unsafe_archive_extract",
            "unescaped_html_output",
            "csrf_exempt",
            "unreachable_code",
            "duplicate_branch_condition",
            "none_annotated_returns_value",
        }
        root = _Path(__file__).resolve().parent.parent
        hits: list[str] = []
        for path in sorted((root / "shared").glob("*.py")):
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover
                continue
            for smell in expected_findings(scan_smells(str(path), content)):
                if smell.rule_id in added:
                    hits.append(f"{path.name}:{smell.line} {smell.rule_id}")
        assert not hits, "added rules fired on repo source: " + "; ".join(hits)

    def test_every_added_rule_has_category_aliases(self):
        """Recall matching goes through the alias map, so a rule without entries can
        only be credited if the reviewer happens to echo the rule id verbatim."""
        from shared.code_intel import RULE_CATEGORY_ALIASES

        for rule_id in (
            "tls_verify_disabled",
            "unsafe_archive_extract",
            "unescaped_html_output",
            "csrf_exempt",
            "unreachable_code",
            "duplicate_branch_condition",
            "none_annotated_returns_value",
        ):
            assert rule_id in RULE_CATEGORY_ALIASES, f"{rule_id} has no aliases"
            assert RULE_CATEGORY_ALIASES[rule_id], f"{rule_id} alias set is empty"
