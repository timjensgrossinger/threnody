"""Grader for the type-hardening case: annotations present AND enforced."""
import inspect
import typing

import pytest

import config
from config import parse_all, parse_debug, parse_hosts, parse_name, parse_port

FUNCS = [parse_port, parse_hosts, parse_debug, parse_name, parse_all]


# --- behaviour must be preserved ------------------------------------------------

def test_defaults():
    assert parse_port({}) == config.DEFAULT_PORT
    assert parse_hosts({}) == []
    assert parse_debug({}) is False
    assert parse_name({}) is None


def test_happy_path():
    out = parse_all({"port": 9000, "hosts": ["a", "b"], "debug": True, "name": "  x  "})
    assert out == {"port": 9000, "hosts": ["a", "b"], "debug": True, "name": "x"}


def test_port_accepts_numeric_string():
    assert parse_port({"port": "9001"}) == 9001


# --- annotations must exist -----------------------------------------------------

@pytest.mark.parametrize("fn", FUNCS, ids=[f.__name__ for f in FUNCS])
def test_fully_annotated(fn):
    sig = inspect.signature(fn)
    assert sig.return_annotation is not inspect.Signature.empty, (
        f"{fn.__name__} has no return annotation"
    )
    for name, param in sig.parameters.items():
        assert param.annotation is not inspect.Signature.empty, (
            f"{fn.__name__} parameter {name!r} has no annotation"
        )


def test_annotations_resolve():
    for fn in FUNCS:
        typing.get_type_hints(fn)


def test_uses_pep604_not_optional():
    import pathlib

    src = pathlib.Path("config.py").read_text()
    assert "Optional[" not in src, "use `X | None`, not Optional[X]"


# --- validation must be enforced ------------------------------------------------

def test_wrong_type_raises_typeerror_naming_the_key():
    with pytest.raises(TypeError) as exc:
        parse_hosts({"hosts": "not-a-list"})
    assert "hosts" in str(exc.value)

    with pytest.raises(TypeError) as exc:
        parse_name({"name": 123})
    assert "name" in str(exc.value)

    with pytest.raises(TypeError) as exc:
        parse_port({"port": ["9000"]})
    assert "port" in str(exc.value)


def test_out_of_range_port_raises_valueerror():
    with pytest.raises(ValueError):
        parse_port({"port": 0})
    with pytest.raises(ValueError):
        parse_port({"port": 70000})
    with pytest.raises(ValueError):
        parse_port({"port": -1})


def test_non_numeric_port_string_is_an_error():
    with pytest.raises((TypeError, ValueError)):
        parse_port({"port": "not-a-number"})
