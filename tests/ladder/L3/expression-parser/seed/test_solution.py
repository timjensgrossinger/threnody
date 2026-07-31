import pytest

from solution import evaluate


def test_precedence():
    assert evaluate('2 + 3 * 4') == 14


def test_parentheses():
    assert evaluate('(2 + 3) * 4') == 20


def test_left_associative_subtraction():
    assert evaluate('10 - 3 - 2') == 5


def test_division_is_float():
    assert evaluate('7 / 2') == 3.5


def test_nested():
    assert evaluate('((1 + 2) * (3 + 4)) - 5') == 16


def test_single_number():
    assert evaluate('42') == 42


@pytest.mark.parametrize('bad', ['2 +', '(1 + 2', '1 ++ 2', '', ')('])
def test_malformed_raises(bad):
    with pytest.raises(ValueError):
        evaluate(bad)


def test_no_eval_used():
    import pathlib
    src = pathlib.Path('solution.py').read_text()
    assert 'eval(' not in src
    assert 'exec(' not in src
