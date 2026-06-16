#!/usr/bin/env python3
"""Tests for diff-based context injection (Phase 6)."""
import sys, os, unittest, tempfile, shutil
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.db import Database
from shared.planner import Subtask
from shared.context import (
    ARTIFACT_INJECTION_SIZE_BUDGET,
    FileReference,
    build_artifact_context_block,
    compact_summary_truncate,
    extract_references,
    find_function_boundaries,
    read_file_context,
    read_source_cached,
    clear_source_cache,
    build_context_block,
    enrich_subtask,
    normalize_target_path,
    is_within_repo,
)
import shared.context as context_module


# ---------------------------------------------------------------------------
# extract_references tests
# ---------------------------------------------------------------------------

class TestExtractReferences(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(extract_references(""), [])

    def test_no_file_paths(self):
        self.assertEqual(extract_references("just some plain text"), [])

    def test_backtick_path(self):
        refs = extract_references("fix `src/foo.py` please")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].path, "src/foo.py")

    def test_absolute_path(self):
        refs = extract_references("edit /usr/local/src/main.py")
        paths = {r.path for r in refs}
        self.assertIn("/usr/local/src/main.py", paths)

    def test_relative_path(self):
        refs = extract_references("check ./shared/router.py")
        paths = {r.path for r in refs}
        self.assertIn("./shared/router.py", paths)

    def test_bare_path(self):
        refs = extract_references("update shared/config.py")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].path, "shared/config.py")

    def test_multiple_paths(self):
        refs = extract_references("fix src/a.py and src/b.py")
        paths = {r.path for r in refs}
        self.assertEqual(len(paths), 2)
        self.assertIn("src/a.py", paths)
        self.assertIn("src/b.py", paths)

    def test_deduplicated(self):
        refs = extract_references("fix `src/foo.py` and also src/foo.py again")
        self.assertEqual(len(refs), 1)

    def test_function_reference(self):
        refs = extract_references("fix `def classify` in `shared/router.py`")
        self.assertEqual(len(refs), 1)
        self.assertIn("classify", refs[0].functions)

    def test_class_reference(self):
        refs = extract_references("update `class TaskRouter` in `shared/router.py`")
        self.assertEqual(len(refs), 1)
        self.assertIn("TaskRouter", refs[0].functions)

    def test_line_reference(self):
        refs = extract_references("check line 42 in shared/router.py")
        self.assertEqual(len(refs), 1)
        self.assertIn((42, 42), refs[0].line_ranges)

    def test_line_range(self):
        refs = extract_references("check lines 10-20 in shared/router.py")
        self.assertEqual(len(refs), 1)
        self.assertIn((10, 20), refs[0].line_ranges)

    def test_combined(self):
        refs = extract_references("fix def foo at line 42 in shared/router.py")
        self.assertEqual(len(refs), 1)
        self.assertIn("foo", refs[0].functions)
        self.assertIn((42, 42), refs[0].line_ranges)


# ---------------------------------------------------------------------------
# find_function_boundaries tests
# ---------------------------------------------------------------------------

class TestFindFunctionBoundaries(unittest.TestCase):
    def test_simple_function(self):
        lines = [
            "def hello():",
            "    return 'world'",
            "",
            "x = 1",
        ]
        bounds = find_function_boundaries(lines, "hello")
        self.assertEqual(len(bounds), 1)
        start, end = bounds[0]
        self.assertEqual(start, 0)
        self.assertEqual(end, 1)

    def test_method_in_class(self):
        lines = [
            "class Foo:",
            "    def bar(self):",
            "        return 1",
            "",
            "    def baz(self):",
            "        return 2",
        ]
        bounds = find_function_boundaries(lines, "bar")
        self.assertEqual(len(bounds), 1)
        start, end = bounds[0]
        self.assertEqual(start, 1)
        self.assertLessEqual(end, 3)

    def test_tab_indented_method(self):
        lines = [
            "class Foo:",
            "\tdef bar(self):",
            "\t\treturn 1",
            "",
            "x = 1",
        ]
        bounds = find_function_boundaries(lines, "bar")
        self.assertEqual(bounds, [(1, 2)])

    def test_multiple_definitions(self):
        lines = [
            "class A:",
            "    def run(self):",
            "        pass",
            "",
            "class B:",
            "    def run(self):",
            "        pass",
        ]
        bounds = find_function_boundaries(lines, "run")
        self.assertEqual(len(bounds), 2)

    def test_not_found(self):
        lines = ["def hello():", "    pass"]
        bounds = find_function_boundaries(lines, "missing")
        self.assertEqual(bounds, [])

    def test_empty_lines(self):
        bounds = find_function_boundaries([], "foo")
        self.assertEqual(bounds, [])


# ---------------------------------------------------------------------------
# read_file_context tests
# ---------------------------------------------------------------------------

class TestReadFileContext(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "test_module.py")
        with open(self.test_file, "w") as f:
            f.write(
                "import os\n"
                "\n"
                "def greet(name):\n"
                "    return f'Hello {name}'\n"
                "\n"
                "def farewell(name):\n"
                "    return f'Bye {name}'\n"
                "\n"
                "class Worker:\n"
                "    def run(self):\n"
                "        pass\n"
            )

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_read_full_file(self):
        ref = FileReference(path=self.test_file)
        content = read_file_context(ref, project_root=self.tmpdir)
        self.assertIsNotNone(content)
        self.assertIn("greet", content)
        self.assertIn("farewell", content)

    def test_read_specific_function(self):
        ref = FileReference(path=self.test_file, functions=["greet"])
        content = read_file_context(ref, project_root=self.tmpdir)
        self.assertIsNotNone(content)
        self.assertIn("greet", content)

    def test_read_line_range(self):
        ref = FileReference(path=self.test_file, line_ranges=[(3, 4)])
        content = read_file_context(ref, project_root=self.tmpdir)
        self.assertIsNotNone(content)
        self.assertIn("greet", content)

    def test_nonexistent_file(self):
        ref = FileReference(path="/nonexistent/path/to/file.py")
        content = read_file_context(ref)
        self.assertIsNone(content)

    def test_empty_file_returns_none(self):
        empty_file = os.path.join(self.tmpdir, "empty.py")
        Path(empty_file).write_text("", encoding="utf-8")
        ref = FileReference(path=empty_file)
        content = read_file_context(ref, project_root=self.tmpdir)
        self.assertIsNone(content)

    def test_function_not_found_returns_none(self):
        ref = FileReference(path=self.test_file, functions=["nonexistent_function"])
        content = read_file_context(ref, project_root=self.tmpdir)
        self.assertIsNone(content)


# ---------------------------------------------------------------------------
# build_context_block tests
# ---------------------------------------------------------------------------

class TestBuildContextBlock(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "sample.py")
        with open(self.test_file, "w") as f:
            f.write("def foo():\n    return 1\n")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_empty_refs(self):
        self.assertEqual(build_context_block([]), "")

    def test_unreadable_files(self):
        refs = [FileReference(path="/no/such/file.py")]
        self.assertEqual(build_context_block(refs), "")

    def test_readable_file_wrapped(self):
        refs = [FileReference(path=self.test_file)]
        block = build_context_block(refs, project_root=self.tmpdir)
        self.assertIn("--- RELEVANT SOURCE CODE ---", block)
        self.assertIn("--- END SOURCE CODE ---", block)
        self.assertIn("foo", block)

    def test_char_cap_respected(self):
        big_file = os.path.join(self.tmpdir, "big.py")
        with open(big_file, "w") as f:
            f.write("x = 1\n" * 5000)
        refs = [FileReference(path=big_file)]
        block = build_context_block(refs, project_root=self.tmpdir)
        # The block should exist but be capped
        self.assertGreater(len(block), 0)
        # Total should not massively exceed the cap (delimiter adds some)
        from shared.config import CONTEXT_MAX_TOTAL_CHARS
        # Content inside delimiters must be <= cap + delimiter overhead
        self.assertLessEqual(len(block), CONTEXT_MAX_TOTAL_CHARS + 200)


# ---------------------------------------------------------------------------
# enrich_subtask tests
# ---------------------------------------------------------------------------

class TestEnrichSubtask(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.test_file = os.path.join(self.tmpdir, "mod.py")
        self.db_path = Path(self.tmpdir) / "artifacts.db"
        self.db = Database(self.db_path)
        with open(self.test_file, "w") as f:
            f.write("def foo():\n    return 42\n")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmpdir)

    def test_no_refs_returns_same_object(self):
        st = Subtask(id=1, description="just do something", tier="low")
        result = enrich_subtask(st)
        self.assertIs(result, st)

    def test_with_ref_enriches_description(self):
        st = Subtask(id=1, description=f"fix {self.test_file}", tier="low")
        result = enrich_subtask(st, project_root=self.tmpdir)
        self.assertGreater(len(result.description), len(st.description))
        self.assertIn("RELEVANT SOURCE CODE", result.description)

    def test_original_not_mutated(self):
        st = Subtask(id=1, description=f"fix {self.test_file}", tier="low")
        original_desc = st.description
        result = enrich_subtask(st, project_root=self.tmpdir)
        self.assertEqual(st.description, original_desc)
        self.assertIsNot(result, st)

    def test_enrich_injects_compact_summaries(self):
        self.db.save_artifact(
            execution_id="exec-1",
            plan_revision=1,
            wave=1,
            subtask_id="1",
            artifact_type="summary",
            full_payload="SECRET-RAW-PAYLOAD",
            compact_summary="short summary",
        )
        self.db.save_artifact(
            execution_id="exec-1",
            plan_revision=1,
            wave=1,
            subtask_id="2",
            artifact_type="analysis",
            full_payload="VERY-SECRET-RAW-PAYLOAD",
            compact_summary={"summary_text": "A" * 3000, "length_chars": 3000},
        )
        st = Subtask(
            id=3,
            description=f"consume results from {self.test_file}",
            tier="low",
            consumes=["summary", "analysis"],
        )
        with patch("shared.context.CONTEXT_MAX_TOTAL_CHARS", 1800):
            result = enrich_subtask(
                st,
                project_root=self.tmpdir,
                db=self.db,
                execution_id="exec-1",
                plan_revision=1,
                current_wave=2,
            )
        self.assertIn("--- ARTIFACT HANDOFF ---", result.description)
        self.assertIn("Artifact type: summary", result.description)
        self.assertIn("Artifact type: analysis", result.description)
        self.assertIn("Reference: artifact:", result.description)
        self.assertIn("short summary", result.description)
        self.assertIn("[truncated]", result.description)
        self.assertNotIn("SECRET-RAW-PAYLOAD", result.description)
        self.assertNotIn("VERY-SECRET-RAW-PAYLOAD", result.description)
        self.assertLessEqual(len(compact_summary_truncate("A" * 3000)), ARTIFACT_INJECTION_SIZE_BUDGET)

    def test_enrich_no_consumes_no_injection(self):
        self.db.save_artifact(
            execution_id="exec-1",
            plan_revision=1,
            wave=1,
            subtask_id="1",
            artifact_type="summary",
            full_payload="SECRET-RAW-PAYLOAD",
            compact_summary="short summary",
        )
        st = Subtask(id=2, description="just do something", tier="low")
        result = enrich_subtask(
            st,
            project_root=self.tmpdir,
            db=self.db,
            execution_id="exec-1",
            plan_revision=1,
            current_wave=2,
        )
        self.assertIs(result, st)

    def test_enrich_respects_execution_and_revision_scoping(self):
        self.db.save_artifact(
            execution_id="exec-1",
            plan_revision=1,
            wave=1,
            subtask_id="1",
            artifact_type="summary",
            full_payload="payload-one",
            compact_summary="summary-one",
        )
        self.db.save_artifact(
            execution_id="exec-2",
            plan_revision=1,
            wave=1,
            subtask_id="2",
            artifact_type="summary",
            full_payload="payload-two",
            compact_summary="summary-two",
        )
        self.db.save_artifact(
            execution_id="exec-1",
            plan_revision=2,
            wave=1,
            subtask_id="3",
            artifact_type="summary",
            full_payload="payload-three",
            compact_summary="summary-three",
        )
        st = Subtask(id=4, description="consume output", tier="low", consumes=["summary"])
        result = enrich_subtask(
            st,
            project_root=self.tmpdir,
            db=self.db,
            execution_id="exec-1",
            plan_revision=1,
            current_wave=2,
        )
        self.assertIn("summary-one", result.description)
        self.assertNotIn("summary-two", result.description)
        self.assertNotIn("summary-three", result.description)

    def test_enrich_uses_prefetched_artifacts_without_db_lookup(self):
        class ExplodingDB:
            def get_artifacts_for_consumes(self, *args, **kwargs):
                raise AssertionError("db lookup should not run when artifacts are prefetched")

        st = Subtask(id=5, description="consume output", tier="low", consumes=["summary"])
        result = enrich_subtask(
            st,
            project_root=self.tmpdir,
            db=ExplodingDB(),
            execution_id="exec-1",
            plan_revision=1,
            current_wave=2,
            prefetched_artifacts=[
                {
                    "artifact_type": "summary",
                    "summary_text": "prefetched-summary",
                    "length_chars": 18,
                    "artifact_ref": "artifact:prefetched",
                }
            ],
        )
        self.assertIn("prefetched-summary", result.description)
        self.assertIn("artifact:prefetched", result.description)

    def test_enrich_tolerates_artifact_lookup_failure(self):
        class ExplodingDB:
            def get_artifacts_for_consumes(self, *args, **kwargs):
                raise RuntimeError("boom")

        st = Subtask(id=6, description="consume output", tier="low", consumes=["summary"])
        result = enrich_subtask(
            st,
            project_root=self.tmpdir,
            db=ExplodingDB(),
            execution_id="exec-1",
            plan_revision=1,
            current_wave=2,
        )
        self.assertIs(result, st)

    def test_enrich_ignores_invalid_prefetched_artifacts(self):
        st = Subtask(id=7, description="consume output", tier="low", consumes=["summary"])
        result = enrich_subtask(
            st,
            project_root=self.tmpdir,
            prefetched_artifacts="not-a-list",
        )
        self.assertIs(result, st)


class TestArtifactContextHelpers(unittest.TestCase):
    def test_build_artifact_context_block_respects_total_budget(self):
        artifacts = [
            {
                "artifact_type": "summary",
                "summary_text": "A" * 900,
                "length_chars": 900,
                "artifact_ref": "artifact:one",
            },
            {
                "artifact_type": "analysis",
                "summary_text": "B" * 900,
                "length_chars": 900,
                "artifact_ref": "artifact:two",
            },
        ]
        block = build_artifact_context_block(artifacts, max_total_chars=1000)
        self.assertIn("artifact:one", block)
        self.assertNotIn("artifact:two", block)
        self.assertIn("[omitted 1 artifact(s) due to context budget]", block)

    def test_build_artifact_context_block_omits_tiny_last_artifact_cleanly(self):
        artifacts = [
            {
                "artifact_type": "summary",
                "summary_text": "A" * 90,
                "length_chars": 90,
                "artifact_ref": "artifact:one",
            },
            {
                "artifact_type": "analysis",
                "summary_text": "B" * 90,
                "length_chars": 90,
                "artifact_ref": "artifact:two",
            },
        ]
        block = build_artifact_context_block(artifacts, max_total_chars=210)
        self.assertIn("artifact:one", block)
        self.assertNotIn("artifact:two", block)
        self.assertNotIn("Artifact type: analysis", block)
        self.assertIn("[omitted 1 artifact(s) due to context budget]", block)

    def test_build_artifact_context_block_counts_wrapper_budget(self):
        artifacts = [
            {
                "artifact_type": "summary",
                "summary_text": "A" * 200,
                "length_chars": 200,
                "artifact_ref": "artifact:one",
            }
        ]
        block = build_artifact_context_block(artifacts, max_total_chars=120)
        self.assertLessEqual(len(block), 120)


class TestContextBlockHelpers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.file_a = Path(self.tmpdir) / "a.py"
        self.file_b = Path(self.tmpdir) / "b.py"
        self.file_a.write_text("A" * 60)
        self.file_b.write_text("B" * 60)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_build_context_block_respects_separator_budget(self):
        refs = [
            FileReference(path="a.py"),
            FileReference(path="b.py"),
        ]
        block = build_context_block(refs, project_root=self.tmpdir, max_total_chars=121)
        self.assertLessEqual(len(block), 121)
        body = block.split("--- RELEVANT SOURCE CODE ---\n", 1)[1].split("\n--- END SOURCE CODE ---", 1)[0]
        self.assertEqual(len(body), 66)
        self.assertIn("a.py", body)
        self.assertNotIn("b.py", body)

    def test_build_context_block_counts_wrapper_budget(self):
        refs = [FileReference(path="a.py")]
        block = build_context_block(refs, project_root=self.tmpdir, max_total_chars=80)
        self.assertLessEqual(len(block), 80)


# ---------------------------------------------------------------------------
# target path helper tests
# ---------------------------------------------------------------------------

class TestTargetPathHelpers(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo_root = Path(self.tmpdir) / "repo"
        self.repo_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_normalize_target_path_with_repo_relative_path(self):
        target = normalize_target_path("src/out.py", self.repo_root)
        self.assertEqual(target, (self.repo_root / "src" / "out.py").resolve(strict=False))

    def test_normalize_target_path_rejects_parent_traversal(self):
        with self.assertRaises(ValueError):
            normalize_target_path("../outside.py", self.repo_root)

    def test_is_within_repo_for_inside_and_outside_paths(self):
        inside = self.repo_root / "inside.py"
        outside = Path(self.tmpdir) / "outside.py"
        self.assertTrue(is_within_repo(inside, self.repo_root))
        self.assertFalse(is_within_repo(outside, self.repo_root))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class TestSourceCache(unittest.TestCase):
    """Wave-scoped file-content cache: dedup reads, never serve stale."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        clear_source_cache()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)
        clear_source_cache()

    def test_read_file_context_identical_pre_post_cache(self):
        p = Path(self.td) / "mod.py"
        p.write_text("def foo():\n    return 1\n", encoding="utf-8")
        ref = FileReference(path="mod.py")
        clear_source_cache()
        first = read_file_context(ref, project_root=self.td)
        second = read_file_context(ref, project_root=self.td)  # served from cache
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_unchanged_file_read_from_disk_once(self):
        p = Path(self.td) / "a.py"
        p.write_text("x = 1\n", encoding="utf-8")
        clear_source_cache()
        # Prime the cache, then spy: a second read must NOT touch disk.
        self.assertEqual(read_source_cached(p), "x = 1\n")
        with patch.object(Path, "read_text", side_effect=AssertionError("disk re-read")) as spy:
            self.assertEqual(read_source_cached(p), "x = 1\n")
            spy.assert_not_called()

    def test_mtime_change_invalidates_no_staleness(self):
        p = Path(self.td) / "b.py"
        p.write_text("v = 1\n", encoding="utf-8")
        clear_source_cache()
        self.assertEqual(read_source_cached(p), "v = 1\n")
        # Rewrite with new content + force a distinct mtime_ns.
        p.write_text("v = 2\n", encoding="utf-8")
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        self.assertEqual(read_source_cached(p), "v = 2\n")  # fresh, never stale

    def test_byte_cap_gate(self):
        from shared.config import CONTEXT_MAX_FILE_BYTES
        p = Path(self.td) / "big.py"
        p.write_text("a" * (CONTEXT_MAX_FILE_BYTES + 10), encoding="utf-8")
        clear_source_cache()
        # Capped read rejects oversize; uncapped read returns content.
        self.assertIsNone(read_source_cached(p, max_bytes=CONTEXT_MAX_FILE_BYTES))
        self.assertIsNotNone(read_source_cached(p, max_bytes=None))


def run_tests(name: str):
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    total = result.testsRun
    failures = len(result.failures) + len(result.errors)
    print(f"{total - failures} passed, {failures} failed")
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_tests(__name__)
    sys.exit(0 if success else 1)
