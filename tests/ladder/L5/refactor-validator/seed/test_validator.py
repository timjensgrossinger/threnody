from validator import validate


def test_valid_record():
    assert validate({'name': 'a', 'email': 'b@c', 'age': 3}) == []


def test_missing_fields_in_order():
    assert validate({}) == ['missing:name', 'missing:email', 'missing:age']


def test_empty_values():
    assert validate({'name': '', 'email': '', 'age': None}) == [
        'empty:name', 'empty:email', 'empty:age',
    ]


def test_partial():
    assert validate({'name': 'x'}) == ['missing:email', 'missing:age']


def test_no_bare_except_pass():
    import pathlib
    src = pathlib.Path('validator.py').read_text()
    assert 'except Exception:\n        pass' not in src
