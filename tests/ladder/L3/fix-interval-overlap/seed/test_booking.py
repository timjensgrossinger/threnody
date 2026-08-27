"""Hidden grader. Covers the reported failures plus cases repro.py does not show,
so a fix that special-cases the reproduction rather than the logic still fails.
"""
import pytest

from booking import conflicts, first_free_slot, overlaps


# --- half-open semantics --------------------------------------------------------

@pytest.mark.parametrize(
    "a_start,a_end,b_start,b_end,expected",
    [
        (9, 10, 10, 11, False),   # touching, a before b
        (10, 11, 9, 10, False),   # touching, b before a
        (9, 11, 10, 12, True),    # partial overlap
        (10, 12, 9, 11, True),    # partial overlap, reversed
        (9, 17, 10, 11, True),    # b inside a
        (10, 11, 9, 17, True),    # a inside b
        (9, 10, 9, 10, True),     # identical
        (9, 10, 11, 12, False),   # disjoint
        (11, 12, 9, 10, False),   # disjoint, reversed
        (9, 12, 11, 12, True),    # shared tail
        (9, 12, 9, 10, True),     # shared head
    ],
)
def test_overlaps(a_start, a_end, b_start, b_end, expected):
    assert overlaps(a_start, a_end, b_start, b_end) is expected


# --- conflicts -----------------------------------------------------------------

def test_conflicts_empty_calendar():
    assert conflicts((9, 10), []) is False


def test_conflicts_finds_any_overlap():
    assert conflicts((10, 11), [(12, 13), (10, 12)]) is True


def test_conflicts_allows_back_to_back():
    assert conflicts((10, 11), [(9, 10), (11, 12)]) is False


# --- first_free_slot -----------------------------------------------------------

def test_slot_at_end_of_day_is_offered():
    assert first_free_slot([(9, 16)], 1) == 16


def test_full_day_has_no_slot():
    assert first_free_slot([(9, 17)], 1) is None


def test_earliest_slot_wins():
    assert first_free_slot([], 2) == 9


def test_skips_occupied_and_finds_gap():
    assert first_free_slot([(9, 11), (12, 17)], 1) == 11


def test_duration_that_cannot_fit():
    assert first_free_slot([(9, 15)], 3) is None


def test_exact_fit_whole_day():
    assert first_free_slot([], 8) == 9


def test_duration_longer_than_day():
    assert first_free_slot([], 9) is None
