"""Reproduction of the reported failures. Run: python3 repro.py

Each line prints EXPECTED vs ACTUAL. All of them should agree once fixed.
"""
from booking import conflicts, first_free_slot, overlaps

checks = [
    # Touching intervals are NOT a conflict under half-open semantics.
    ("touching end-to-start", overlaps(9, 10, 10, 11), False),
    ("touching start-to-end", overlaps(10, 11, 9, 10), False),
    # Genuine partial overlap.
    ("partial overlap", overlaps(9, 11, 10, 12), True),
    # One interval fully inside the other.
    ("b contained in a", overlaps(9, 17, 10, 11), True),
    ("a contained in b", overlaps(10, 11, 9, 17), True),
    # Identical intervals conflict.
    ("identical", overlaps(9, 10, 9, 10), True),
    # A booking that exactly fills the tail of the day must be offered.
    ("slot at end of day", first_free_slot([(9, 16)], 1), 16),
    ("no room", first_free_slot([(9, 17)], 1), None),
]

failures = 0
for name, actual, expected in checks:
    ok = actual == expected
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}: expected {expected!r}, got {actual!r}")

print(f"\n{failures} failing check(s)")
