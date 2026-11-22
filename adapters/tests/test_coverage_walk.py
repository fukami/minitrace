"""Tests for coverage.walk_dict_paths: every element of a list is walked, not only the first.

Pins three things:
  1. A path present ONLY in a later list element is seen (the fix:
     union across all elements, not first-element sampling).
  2. Mixed lists (scalar first, dict later) still surface the dict paths.
  3. The pre-fix behaviour stays intact for the shapes it already handled
     (shape indicator from the first element, empty/scalar/nested cases).

Run:
    python adapters/tests/test_coverage_walk.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from coverage import walk_dict_paths  # noqa: E402


def paths(obj):
    return {p for p, _ in walk_dict_paths(obj)}


def test_union_across_list_elements():
    obj = {"items": [{"a": 1}, {"a": 1, "b": 2}, {"c": None}]}
    got = paths(obj)
    assert "items[].a" in got
    assert "items[].b" in got, "path only in a later element must be seen"
    assert "items[].c" in got, "path only in a later element must be seen"


def test_mixed_list_scalar_first():
    obj = {"items": ["x", {"late": 1}]}
    got = walk_dict_paths(obj)
    kinds = dict(got)
    assert kinds["items"] == "list[str]", "shape indicator stays first-element"
    assert "items[].late" in paths(obj), "dict element after a scalar must be walked"


def test_frequency_duplicates_kept():
    obj = {"items": [{"a": 1}, {"a": 2}]}
    got = [p for p, _ in walk_dict_paths(obj)]
    assert got.count("items[].a") == 2, "duplicates are frequencies, not deduped"


def test_prefix_shapes_unchanged():
    assert walk_dict_paths({}) == [("", "dict-empty")]
    assert walk_dict_paths({"a": []}) == [("a", "list-empty")]
    assert walk_dict_paths({"a": None}) == [("a", "null")]
    assert walk_dict_paths({"a": [[1]]}) == [("a", "list[list]")]
    assert paths({"a": {"b": {"c": 1}}}) == {"a.b.c"}
    assert paths({"a": [{"b": [{"c": 1}]}]}) == {"a", "a[].b", "a[].b[].c"}


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
