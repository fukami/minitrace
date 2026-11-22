"""Tests for coverage.compute_unmapped with an integer record_path segment.

record_path_get reads a canonical non-negative integer segment
(no sign, no leading zero) at a list as an index, and at an object as a key.
The consumed side cannot know which, so such a segment consumes both
spellings: the seen-path list spelling `[]` and the literal key (ruled
2026-10-18: both forms, every combination). The list entry above an index
segment is not consumed by it (ruled 2026-10-18: leave `a` unmapped).

Run:
    python adapters/tests/test_coverage_record_path.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from coverage import collect_seen_paths, compute_unmapped  # noqa: E402


def unmapped_container(container, record_path):
    seen = collect_seen_paths([container], type_field="type")
    got = compute_unmapped(seen, {}, container_record_path=record_path)
    return got.get("_container", [])


def test_index_segment_at_a_list_consumes_the_list_spelling():
    c = {"a": [{"x": 0}, {"events": [{"role": "u"}]}]}
    # a[].x is element 0, which the adapter does not read; `a` stays unmapped.
    assert unmapped_container(c, "a.1.events") == ["a", "a[].x"]


def test_index_segment_at_an_object_keeps_the_literal_key():
    c = {"a": {"1": {"events": [{"role": "u"}]}}}
    assert unmapped_container(c, "a.1.events") == []


def test_every_combination_of_index_segments_is_consumed():
    # list index then object key
    c1 = {"a": [{"b": {"2": {"events": [{"r": 1}]}}}]}
    assert unmapped_container(c1, "a.0.b.2.events") == ["a"]
    # object key then list index
    c2 = {"a": {"1": [{"events": [{"r": 1}]}]}}
    assert unmapped_container(c2, "a.1.0.events") == ["a.1"]
    # object key then object key
    c3 = {"a": {"1": {"0": {"events": [{"r": 1}]}}}}
    assert unmapped_container(c3, "a.1.0.events") == []


def test_a_non_canonical_segment_stays_a_key():
    # record_path_get reads no record through a non-canonical segment at a list.
    c = {"a": [{"events": [{"role": "u"}]}]}
    for seg in ("01", "00", "+1", "-1", "1e0"):
        got = unmapped_container(c, f"a.{seg}.events")
        assert "a[].events" in got, (seg, got)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
