from solution import word_counts


def test_basic():
    assert word_counts('a b a') == {'a': 2, 'b': 1}


def test_case_and_punctuation():
    assert word_counts('Hello, hello world!') == {'hello': 2, 'world': 1}


def test_empty():
    assert word_counts('') == {}


def test_only_punctuation():
    assert word_counts('... !!!') == {}
