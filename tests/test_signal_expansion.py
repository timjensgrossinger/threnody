"""Tests for improvement 4 (keyword signal expansion) and improvement 5 (reasoning scoring)."""
from __future__ import annotations

import pytest

from shared.config import TGsConfig
from shared.router import TaskRouter


@pytest.fixture()
def router() -> TaskRouter:
    return TaskRouter(TGsConfig.defaults())


# ---------------------------------------------------------------------------
# Improvement 4 — keyword signal / override expansion
# ---------------------------------------------------------------------------


def test_add_authentication_not_low(router: TaskRouter) -> None:
    """Routine authentication work has a medium floor without forcing high."""
    result = router.classify("add authentication to the API")
    assert result.tier == "medium"
    assert result.override is False
    # Reached by the score ("authentication" is a medium complexity signal), not
    # by the risk floor. Either mechanism satisfies the contract in the docstring;
    # asserting the floor token pinned an implementation detail that has moved.
    assert result.tier != "high"


def test_set_up_database_medium(router: TaskRouter) -> None:
    """'set up' is a new medium signal."""
    result = router.classify("set up the database connection")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high, got {result.tier!r} (score={result.score})"
    )


def test_fix_typo_in_readme_low(router: TaskRouter) -> None:
    """'typo' + 'readme' are both low overrides."""
    result = router.classify("fix a typo in the readme")
    assert result.tier == "low", (
        f"Expected low, got {result.tier!r} (reason={result.reason})"
    )


def test_bump_version_low(router: TaskRouter) -> None:
    """'bump version' is a new low override."""
    result = router.classify("bump version to 1.2.3")
    assert result.tier == "low", (
        f"Expected low, got {result.tier!r} (reason={result.reason})"
    )


def test_rbac_high(router: TaskRouter) -> None:
    """'rbac' is a new high override."""
    result = router.classify("implement rbac for the admin panel")
    assert result.tier == "high", (
        f"Expected high, got {result.tier!r} (reason={result.reason})"
    )


def test_scaffold_microservice_medium_or_high(router: TaskRouter) -> None:
    """'scaffold' is a new medium signal."""
    result = router.classify("scaffold a new microservice")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high, got {result.tier!r} (score={result.score})"
    )


def test_add_logging_statement_low(router: TaskRouter) -> None:
    """'add logging statement' is a new low override."""
    result = router.classify("add a logging statement to the handler")
    assert result.tier == "low", (
        f"Expected low, got {result.tier!r} (reason={result.reason})"
    )


# ---------------------------------------------------------------------------
# Improvement 5 — reasoning / creativity scoring
# ---------------------------------------------------------------------------


def test_brainstorm_names_medium(router: TaskRouter) -> None:
    """'brainstorm' fires reasoning scoring → minimum medium."""
    result = router.classify("brainstorm names for the new service")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high, got {result.tier!r} (score={result.score})"
    )


def test_compare_architectures_medium(router: TaskRouter) -> None:
    """'compare' fires reasoning scoring → minimum medium."""
    result = router.classify("compare these two architecture approaches")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high, got {result.tier!r}"
    )


def test_compelling_readme_medium(router: TaskRouter) -> None:
    """'compelling' fires reasoning scoring → minimum medium."""
    result = router.classify("write a compelling readme for the project")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high, got {result.tier!r}"
    )


def test_evaluate_tradeoffs_medium(router: TaskRouter) -> None:
    """'evaluate' + 'tradeoff' both fire reasoning scoring."""
    result = router.classify("evaluate the tradeoffs of this database choice")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high, got {result.tier!r}"
    )


def test_explain_why_design_medium(router: TaskRouter) -> None:
    """'explain why' fires reasoning scoring → minimum medium."""
    result = router.classify("explain why this design is better")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high, got {result.tier!r}"
    )


def test_write_unit_test_low(router: TaskRouter) -> None:
    """Technical task — complexity score dominates, reasoning should not fire."""
    result = router.classify("write a unit test for the parser")
    assert result.tier == "low", (
        f"Expected low (complexity wins), got {result.tier!r} (score={result.score}, reason={result.reason})"
    )


def test_refactor_distributed_auth_not_low(router: TaskRouter) -> None:
    """High-complexity technical task — scores medium/high, definitely not low."""
    result = router.classify("refactor the distributed authentication service")
    assert result.tier in ("medium", "high"), (
        f"Expected medium/high (complexity wins), got {result.tier!r} (score={result.score})"
    )
