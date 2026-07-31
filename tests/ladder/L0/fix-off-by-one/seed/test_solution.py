from solution import last_index


def test_nonempty():
    assert last_index([1, 2, 3]) == 2


def test_single():
    assert last_index(['x']) == 0


def test_empty():
    assert last_index([]) == -1
