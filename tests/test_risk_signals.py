"""Tests for the shared security-risk vocabulary and the router's risk floor.

The defect these cover: routing tier was a function of the caller's prose. A
task described as "harden the prompt injection surface in text_safety.py"
matched none of the nine words in the risk vocabulary and scored the base score,
so it routed to the cheapest tier while a routine task that happened to use four
keywords scored higher.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from shared.config import (
    DEFAULT_RISK_FILENAME_PATTERNS,
    TGsConfig,
    ThresholdConfig,
    LOW_TIER_CEILING,
    LOW_TIER_FLOOR,
    MEDIUM_HIGH_BOUNDARY_CEILING,
    MEDIUM_HIGH_BOUNDARY_FLOOR,
)
from shared.risk_signals import (
    CONCRETE_HIGH_RISK_SIGNALS,
    FILENAME_RISK_TOKENS,
    PROSE_RISK_TOKENS,
    RISK_SIGNALS,
    VOCABULARY,
    FileRisk,
    TaskRiskEvidence,
    collect_task_evidence,
    compile_risk_floor_re,
    has_concrete_high_risk_signals,
    has_risk_signals,
)
from shared.router import TaskRouter


# ---------------------------------------------------------------------------
# Vocabulary: one source, no drift
# ---------------------------------------------------------------------------

def test_filename_and_prose_tokens_are_subsets_of_the_vocabulary() -> None:
    """Both exported sets must be derived, never hand-maintained.

    This is the assertion that makes the drift impossible. The config half used
    to be a 9-word copy carrying a comment asking the reader to keep it in sync
    with a ~30-token content scan; it did not, and two of its words ("oauth",
    "saml") were missing from the scan entirely.
    """
    plain = {tok.plain for tok in VOCABULARY}
    assert set(FILENAME_RISK_TOKENS) <= plain
    assert set(PROSE_RISK_TOKENS) <= plain


def test_config_risk_patterns_are_the_derived_filename_tokens() -> None:
    assert DEFAULT_RISK_FILENAME_PATTERNS == FILENAME_RISK_TOKENS


def test_prose_set_is_narrower_than_the_filename_set() -> None:
    """Two different jobs, deliberately two different sets.

    ``billing.py`` names a payment surface worth a floor; "find files related to
    billing logic" is a read-only question carrying no risk, and flooring it
    spent a tier for nothing.
    """
    assert set(PROSE_RISK_TOKENS) < set(FILENAME_RISK_TOKENS)
    for topic_only in ("billing", "payment", "subprocess"):
        assert topic_only in FILENAME_RISK_TOKENS
        assert topic_only not in PROSE_RISK_TOKENS


@pytest.mark.parametrize(
    "token",
    ["injection", "sanitiz", "redact", "untrusted", "privacy", "pii", "xss", "csrf"],
)
def test_untrusted_input_vocabulary_is_present(token: str) -> None:
    """The gap that caused the misroute: none of these were in either half."""
    assert token in PROSE_RISK_TOKENS


@pytest.mark.parametrize(
    "content",
    [
        "prompt injection defence",
        "sanitize the markup",
        "redaction of api_key values",
        "untrusted page content",
        "pii handling",
    ],
)
def test_content_scan_matches_untrusted_input_vocabulary(content: str) -> None:
    assert has_risk_signals(content)


def test_concrete_subset_is_a_strict_subset_of_the_generic_scan() -> None:
    for probe in ("shell=True", "pickle.loads(x)", "os.system('ls')", "yaml.load(f)"):
        assert has_concrete_high_risk_signals(probe)
        assert has_risk_signals(probe)
    # A risky topic with no dangerous construct is generic-only.
    assert has_risk_signals("rotate the password")
    assert not has_concrete_high_risk_signals("rotate the password")


def test_concrete_pattern_is_unchanged_by_the_extraction() -> None:
    """The concrete subset gates the review fanout's high tier; it must not move.

    Extracting the vocabulary was allowed to *widen* the generic scan. Widening
    the concrete scan would silently re-tier existing review cells.
    """
    for probe in ("injection", "sanitize", "redact", "privacy", "pii"):
        assert not has_concrete_high_risk_signals(probe)


def test_empty_content_never_matches() -> None:
    assert not has_risk_signals("")
    assert not has_concrete_high_risk_signals("")


def test_compile_risk_floor_re_returns_none_for_empty_vocabulary() -> None:
    assert compile_risk_floor_re([]) is None
    assert compile_risk_floor_re(["  ", ""]) is None


def test_compile_risk_floor_re_matches_word_start_with_suffix() -> None:
    matcher = compile_risk_floor_re(["auth", "credential"])
    assert matcher is not None
    assert matcher.search("authentication")
    assert matcher.search("credentials")
    # Not a bare substring match: the token must start at a word boundary.
    assert not matcher.search("oauthorize")


# ---------------------------------------------------------------------------
# Evidence collection
# ---------------------------------------------------------------------------

def test_evidence_is_empty_and_falsy_for_no_paths() -> None:
    ev = collect_task_evidence([])
    assert not ev
    assert ev.files == ()
    assert ev.risk_files == 0
    assert ev.security_smells == 0
    assert ev.max_loc == 0


def test_evidence_scores_a_risky_file(tmp_path: Path) -> None:
    target = tmp_path / "session.py"
    target.write_text(
        "import sqlite3\n"
        "def lookup(conn, name):\n"
        "    return conn.execute(f'SELECT * FROM users WHERE n={name}').fetchone()\n"
    )
    ev = collect_task_evidence([str(target)])
    assert ev.any_risk
    assert ev.any_security_smell, "f-string SQL interpolation is a high-severity security smell"
    assert ev.max_loc == 3


def test_evidence_skips_content_scan_for_non_source_files(tmp_path: Path) -> None:
    """A Markdown file has no exploitable construct; scanning it only adds noise."""
    doc = tmp_path / "notes.md"
    doc.write_text("the password is hunter2 and we call pickle.loads everywhere\n")
    ev = collect_task_evidence([str(doc)])
    assert len(ev.files) == 1
    assert ev.files[0].content_risk is False
    assert ev.files[0].security_smells == 0


def test_evidence_matches_a_risky_basename_even_without_content(tmp_path: Path) -> None:
    doc = tmp_path / "credentials.md"
    doc.write_text("nothing interesting\n")
    ev = collect_task_evidence([str(doc)])
    assert ev.files[0].basename_risk is True
    assert ev.any_risk


def test_evidence_records_unreadable_paths_without_raising(tmp_path: Path) -> None:
    ev = collect_task_evidence([str(tmp_path / "gone.py")])
    assert ev.unresolved == (str(tmp_path / "gone.py"),)
    assert ev.risk_files == 0


def test_evidence_is_capped(tmp_path: Path) -> None:
    """route_task is a blocking call; a wide fan-out must not scan the repo."""
    paths = []
    for i in range(12):
        f = tmp_path / f"m{i}.py"
        f.write_text("x = 1\n")
        paths.append(str(f))
    assert len(collect_task_evidence(paths, max_files=8).files) == 8


def test_clean_file_is_risk_free(tmp_path: Path) -> None:
    f = tmp_path / "geometry.py"
    f.write_text("def area(w, h):\n    return w * h\n")
    ev = collect_task_evidence([str(f)])
    assert not ev.any_risk
    assert not ev.any_security_smell


# ---------------------------------------------------------------------------
# The router's two-step floor
# ---------------------------------------------------------------------------

@pytest.fixture()
def router(test_config_fixture: TGsConfig) -> TaskRouter:
    return TaskRouter(test_config_fixture)


def test_security_prose_no_longer_routes_to_the_cheapest_tier(router: TaskRouter) -> None:
    """The reproduced misroute. Previously low at the base score of 0.10.

    Two independent repairs now cover it: "harden" is a medium scale verb, and
    "injection" is in the risk vocabulary. Only the tier is asserted — which of
    the two does the work is not the contract.
    """
    assert router.classify(
        "harden the prompt injection surface in text_safety.py"
    ).tier == "medium"


def test_risk_vocabulary_alone_lifts_a_zero_scoring_task(router: TaskRouter) -> None:
    """Isolates the floor: no complexity signal fires, so only the floor can act."""
    bare = router.classify("look at the untrusted page content")
    assert bare.tier == "medium"
    assert "security_floor=medium" in bare.reason
    assert "floor_evidence=task_text" in bare.reason


def test_read_only_question_about_a_risky_topic_stays_low(router: TaskRouter) -> None:
    """Guards the over-escalation this fix could have introduced."""
    assert router.classify("Find files related to billing logic.").tier == "low"


def test_file_content_alone_raises_the_floor(router: TaskRouter, tmp_path: Path) -> None:
    """The task text says nothing risky; the target file does."""
    target = tmp_path / "scanner.py"
    target.write_text("def check(t):\n    return t.get('password')\n")
    assert router.classify("tidy the helper").tier == "low"
    with_files = router.classify(
        "tidy the helper", evidence=collect_task_evidence([str(target)])
    )
    assert with_files.tier == "medium"
    assert "risk_files:1" in with_files.reason


def test_a_security_defect_in_a_target_reaches_the_high_floor(
    router: TaskRouter, tmp_path: Path
) -> None:
    target = tmp_path / "store.py"
    target.write_text(
        "def q(conn, uid):\n"
        "    return conn.execute(f'SELECT * FROM t WHERE id={uid}').fetchall()\n"
    )
    decision = router.classify(
        "rewrite the query builder", evidence=collect_task_evidence([str(target)])
    )
    assert decision.tier == "high"
    assert "security_smells:1" in decision.reason


def test_high_floor_lifts_a_task_the_score_leaves_at_medium(
    router: TaskRouter, tmp_path: Path
) -> None:
    """Isolates the high floor from the evidence score.

    A neutrally-phrased task over a file with a detected security defect scores
    0.40 (base plus the evidence term), which lands medium. Only the floor can
    take it to high.
    """
    target = tmp_path / "store.py"
    target.write_text(
        "def q(conn, uid):\n"
        "    return conn.execute(f'SELECT * FROM t WHERE id={uid}').fetchall()\n"
    )
    decision = router.classify(
        "tidy the helper", evidence=collect_task_evidence([str(target)])
    )
    assert decision.tier == "high"
    assert "security_floor=high" in decision.reason
    assert "floor_evidence=" in decision.reason


def test_trivial_edit_to_a_dangerous_file_is_capped_at_medium(
    router: TaskRouter, tmp_path: Path
) -> None:
    """A defect describes the file, not the edit.

    "Add a docstring to db.py" cannot touch that file's SQL interpolations, so it
    must not inherit their tier. Without this cap the high floor turned every
    comment change in a risky file into the most expensive tier of work.
    """
    target = tmp_path / "store.py"
    target.write_text(
        "def q(conn, uid):\n"
        "    return conn.execute(f'SELECT * FROM t WHERE id={uid}').fetchall()\n"
    )
    decision = router.classify(
        "add a docstring", evidence=collect_task_evidence([str(target)])
    )
    assert decision.tier == "medium"
    assert "capped:trivial" in decision.reason


def test_floor_never_lowers_a_score_derived_tier(router: TaskRouter, tmp_path: Path) -> None:
    target = tmp_path / "safe.py"
    target.write_text("x = 1\n")
    prompt = "architect the distributed bytecode interpreter"
    assert router.classify(prompt).tier == "high"
    high = router.classify(prompt, evidence=collect_task_evidence([str(target)]))
    assert high.tier == "high"


def test_disabled_floor_yields_no_floor(test_config_fixture: TGsConfig) -> None:
    test_config_fixture.risk_floor_enabled = False
    router = TaskRouter(test_config_fixture)
    # "credential" is in the risk vocabulary but not in DEFAULT_OVERRIDES["high"]
    # (unlike "oauth"/"saml"), so the floor is the only thing that could lift it.
    assert router.classify("rotate the stored credential").tier == "low"
    enabled = TaskRouter(test_config_fixture.__class__(
        risk_floor_enabled=True, db_path=test_config_fixture.db_path,
    ))
    assert enabled.classify("rotate the stored credential").tier == "medium"


def test_evidence_defaults_to_none_for_every_other_caller(router: TaskRouter) -> None:
    """classify() must stay behaviour-identical without evidence."""
    assert router.classify("add a docstring").tier == "low"


# ---------------------------------------------------------------------------
# Score model
# ---------------------------------------------------------------------------

def test_universal_low_verbs_no_longer_contribute(router: TaskRouter) -> None:
    """add/update/fix/change/write/create/remove fired on nearly every task.

    Each was worth +0.06, so they raised every score by a similar amount and
    discriminated nothing while inflating keyword-dense prompts.
    """
    for verb in ("add", "update", "fix", "change", "write", "create", "remove"):
        decision = router.classify(f"{verb} the thing")
        assert f"{verb}(+" not in decision.reason, decision.reason


def test_per_level_hits_are_capped(router: TaskRouter) -> None:
    """Vocabulary density must not masquerade as difficulty."""
    dense = router.classify("implement and integrate and migrate and configure it")
    assert "(capped)" in dense.reason
    # Two medium hits at 0.12 plus the 0.10 base, and nothing more.
    assert dense.score == pytest.approx(0.34, abs=0.01)


def test_rewrite_and_refactor_are_no_longer_asymmetric(router: TaskRouter) -> None:
    """"rewrite" was in no vocabulary while "refactor" sat at high."""
    assert router.classify("rewrite the scanner").tier == "medium"
    assert router.classify("refactor the scanner").tier == "medium"


def test_evidence_contributes_to_the_score(router: TaskRouter, tmp_path: Path) -> None:
    small = tmp_path / "small.py"
    small.write_text("def f():\n    return 1\n")
    big = tmp_path / "big.py"
    big.write_text("\n".join(f"password_{i} = {i}" for i in range(700)) + "\n")
    bare = router.classify("tidy the helper").score
    with_small = router.classify(
        "tidy the helper", evidence=collect_task_evidence([str(small)])
    ).score
    with_big = router.classify(
        "tidy the helper", evidence=collect_task_evidence([str(big)])
    ).score
    assert bare == with_small, "a clean small file adds nothing"
    assert with_big > bare, "a large risky file must raise the score"


# ---------------------------------------------------------------------------
# Re-derived tier bounds
# ---------------------------------------------------------------------------

def test_bounds_admit_the_default_thresholds() -> None:
    t = ThresholdConfig()
    assert LOW_TIER_FLOOR <= t.low_max <= LOW_TIER_CEILING
    assert MEDIUM_HIGH_BOUNDARY_FLOOR <= t.medium_max <= MEDIUM_HIGH_BOUNDARY_CEILING


def test_low_floor_sits_below_the_medium_score_cluster() -> None:
    """Defect 7: adaptive learning could never correct a sub-0.50 misroute.

    compute_thresholds clamps to LOW_TIER_FLOOR and adjusts by at most 0.10, so
    with a floor of 0.50 a task scoring 0.32 stayed on the low tier no matter how
    many failures accumulated against it. The floor must sit below the observed
    medium cluster for feedback to have anywhere to land.
    """
    assert LOW_TIER_FLOOR < 0.30


def test_thresholds_still_cannot_collapse() -> None:
    t = ThresholdConfig(low_max=0.9, medium_max=0.1)
    assert t.medium_max > t.low_max


def test_floor_evidence_names_the_detector_that_fired(
    router: TaskRouter, tmp_path: Path
) -> None:
    """A smell-only file must not be reported as a content-vocabulary match.

    ``conn.execute(f"...")`` is a confirmed high-severity smell that matches no
    risk token, so the explanation has to name the smell, not "file_content".
    """
    target = tmp_path / "store.py"
    target.write_text(
        "def q(conn, uid):\n"
        "    return conn.execute(f'SELECT * FROM t WHERE id={uid}').fetchall()\n"
    )
    evidence = collect_task_evidence([str(target)])
    assert evidence.any_risk, "a detected defect counts as risk"
    assert not evidence.any_vocabulary_risk, "no vocabulary token matches this file"
    reason = router.classify("tidy the helper", evidence=evidence).reason
    assert "security_smells=" in reason
    assert "file_content" not in reason


def _clean_module(path: Path, defs: int) -> Path:
    """A file with no risk vocabulary and no detectable defect."""
    path.write_text("\n".join(f"def f{i}(a, b):\n    return a * {i} + b" for i in range(defs)))
    return path


def test_size_alone_never_reaches_the_high_tier(router: TaskRouter, tmp_path: Path) -> None:
    """Only a detected security defect opens the high floor.

    Size is a real difficulty signal but not a danger signal, so a large clean
    file must not be able to buy the most expensive tier on LOC alone.
    """
    big = _clean_module(tmp_path / "geometry.py", 400)
    decision = router.classify("tidy the helper", evidence=collect_task_evidence([str(big)]))
    assert decision.tier == "medium"


def test_only_the_large_band_flips_a_neutral_task(router: TaskRouter, tmp_path: Path) -> None:
    """Mirrors review_fanout.tier_for's bands (<230 / 230-600 / >600).

    24% of this repo's ``shared/*.py`` files exceed 600 LOC, so the band that
    flips a tier had to be the one that genuinely predicts edit risk.
    """
    mid = _clean_module(tmp_path / "mid.py", 150)
    assert router.classify(
        "tidy the helper", evidence=collect_task_evidence([str(mid)])
    ).tier == "low"


def test_a_trivial_task_stays_low_even_in_a_large_file(
    router: TaskRouter, tmp_path: Path
) -> None:
    big = _clean_module(tmp_path / "geometry.py", 400)
    assert router.classify(
        "add a docstring", evidence=collect_task_evidence([str(big)])
    ).tier == "low"


def test_empty_file_is_clean_not_unresolved(tmp_path: Path) -> None:
    """A 0-byte file is readable, so it is a clean result rather than a failure."""
    empty = tmp_path / "empty.py"
    empty.write_text("")
    ev = collect_task_evidence([str(empty)])
    assert ev.unresolved == ()
    assert ev.files[0].loc == 0
    assert not ev.any_risk


# ---------------------------------------------------------------------------
# risk_floor_high_tier parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "generic,high,expect_high",
    [
        ("medium", "high", "high"),
        ("medium", "medium", "medium"),   # collapse the floor to one step
        ("medium", "low", "medium"),      # inverted -> raised to match generic
        ("high", "medium", "high"),       # inverted -> raised to match generic
        ("medium", "bogus", "high"),      # unknown -> documented default
    ],
)
def test_risk_floor_high_tier_is_never_below_the_generic_step(
    tmp_path: Path, generic: str, high: str, expect_high: str
) -> None:
    """A high step below the generic step would invert the two.

    File evidence would then be able to *lower* a floor the task text had
    already set, which is the one thing a floor must never do.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(f"risk_floor_tier: {generic}\nrisk_floor_high_tier: {high}\n")
    cfg = TGsConfig.from_yaml(cfg_path)
    assert cfg.risk_floor_high_tier == expect_high


def test_content_vocabulary_nudges_the_score_but_sets_no_floor(
    router: TaskRouter, tmp_path: Path
) -> None:
    """Content vocabulary is weak evidence and must not force a tier.

    It fired on 62% of this repo's shared modules, topped by the module that
    defines the vocabulary and by a static analyser, while a file holding two
    real defects scored a single hit. Raw hit count cannot separate "handles
    secrets" from "mentions secrets", so only a filename match or a detected
    defect earns a floor.
    """
    target = tmp_path / "notes_helper.py"  # innocuous name
    target.write_text("# discusses password and token handling\nX = 1\n")
    evidence = collect_task_evidence([str(target)])
    assert evidence.files[0].content_risk
    assert not evidence.any_basename_risk
    reason = router.classify("tidy the helper", evidence=evidence).reason
    assert "security_floor" not in reason, "content alone must not floor"
    assert "risk_files:1" in reason, "but it must still move the score"


def test_a_risky_filename_still_sets_the_medium_floor(
    router: TaskRouter, tmp_path: Path
) -> None:
    """The strong half of vocabulary evidence: the file was named for what it holds."""
    target = tmp_path / "credentials.py"
    target.write_text("X = 1\n")
    evidence = collect_task_evidence([str(target)])
    assert evidence.any_basename_risk
    # Assert the floor mechanism directly. Going through .reason would be
    # testing which of two independent repairs happened to act first: the
    # evidence score also reaches medium here, and the floor only annotates the
    # reason when it actually *changes* the tier.
    floor, why = router._resolve_risk_floor("tidy the helper", evidence)
    assert floor == "medium"
    assert why == "file_name"
    assert router.classify("tidy the helper", evidence=evidence).tier == "medium"
