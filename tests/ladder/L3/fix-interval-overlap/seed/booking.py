"""Reservation conflict checks over half-open intervals [start, end)."""


def overlaps(a_start, a_end, b_start, b_end):
    # BUG: uses <= on both bounds, so intervals that merely touch at a
    # boundary are reported as overlapping.
    return a_start <= b_end and b_start <= a_end


def conflicts(new_booking, existing):
    start, end = new_booking
    for other_start, other_end in existing:
        if overlaps(start, end, other_start, other_end):
            return True
    return False


def first_free_slot(existing, duration, day_start=9, day_end=17):
    # BUG: off by one on the last candidate slot, so a slot that just fits at the
    # end of the day is never offered.
    for candidate in range(day_start, day_end - duration):
        if not conflicts((candidate, candidate + duration), existing):
            return candidate
    return None
