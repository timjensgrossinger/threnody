import pytest

from solution import retry


def test_succeeds_first_try():
    calls = []

    @retry(3)
    def f():
        calls.append(1)
        return 'ok'

    assert f() == 'ok'
    assert len(calls) == 1


def test_retries_then_succeeds():
    calls = []

    @retry(3)
    def f():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError('nope')
        return 'ok'

    assert f() == 'ok'
    assert len(calls) == 3


def test_exhausts_and_reraises():
    calls = []

    @retry(2)
    def f():
        calls.append(1)
        raise KeyError('boom')

    with pytest.raises(KeyError):
        f()
    assert len(calls) == 2


def test_unlisted_exception_propagates_immediately():
    calls = []

    @retry(3, exceptions=(ValueError,))
    def f():
        calls.append(1)
        raise TypeError('other')

    with pytest.raises(TypeError):
        f()
    assert len(calls) == 1


def test_on_retry_callback():
    seen = []

    @retry(3, on_retry=lambda n, exc: seen.append((n, type(exc).__name__)))
    def f():
        raise ValueError('x')

    with pytest.raises(ValueError):
        f()
    assert len(seen) == 2


def test_invalid_attempts():
    with pytest.raises(ValueError):
        retry(0)


def test_preserves_metadata():
    @retry(2)
    def documented():
        """Doc here."""
        return 1

    assert documented.__name__ == 'documented'
    assert documented.__doc__ == 'Doc here.'


def test_arguments_forwarded():
    @retry(2)
    def add(a, b=0):
        return a + b

    assert add(1, b=2) == 3
