"""DSL spec RC 3 §8.4 / §13.1: the `trace:` special form is valid only as the whole
template of `target_content_hash` in an `emit_lineage` action; a runner rejects any
other placement when it loads the adapter."""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ADAPTERS))

import dsl_runner  # noqa: E402

FIXTURE = ADAPTERS / "tests" / "fixtures" / "synthetic-trace-pair.dsl.yaml"
INPUT = ADAPTERS / "tests" / "fixtures" / "synthetic-trace-pair.input.json"
GOOD = "${trace:agent-${container.id}:content_hash}"


def _lineage_dsl(value: str, key: str = "target_content_hash") -> dict:
    return {"emit": [{"name": "r", "do": [{"emit_lineage": {"type": "judges", key: value}}]}]}


def test_whole_template_in_bearing_field_is_fine():
    assert dsl_runner.dsl_placeholder_problems(_lineage_dsl(GOOD)) == []


def test_prefix_or_suffix_in_bearing_field_is_rejected():
    for bad in ("x " + GOOD, GOOD + " ", GOOD + "${record.id}"):
        problems = dsl_runner.dsl_placeholder_problems(_lineage_dsl(bad))
        assert problems and "whole template" in problems[0], bad


def test_form_in_any_other_field_is_rejected():
    assert dsl_runner.dsl_placeholder_problems(_lineage_dsl(GOOD, key="x-note"))
    dsl = {"emit": [{"name": "r", "do": [{"emit_annotation": {"content": {"text": GOOD}}}]}]}
    problems = dsl_runner.dsl_placeholder_problems(dsl)
    assert problems and "outside an emit_lineage target_content_hash" in problems[0]


def test_on_block_sub_rule_is_a_valid_home():
    dsl = {"emit": [{"name": "r", "on_block": [
        {"name": "b", "do": [{"emit_lineage": {"target_content_hash": GOOD}}]}]}]}
    assert dsl_runner.dsl_placeholder_problems(dsl) == []


def test_suffix_match_elsewhere_is_rejected():
    """Review 2026-09-25: a bare (emit_lineage, target_content_hash) suffix
    nested somewhere else, in constants, in session.static, or in a key."""
    nested = {"emit": [{"name": "r", "do": [{"emit_annotation": {"content": {
        "emit_lineage": {"target_content_hash": GOOD}}}}]}]}
    consts = {"constants": {"emit_lineage": {"target_content_hash": GOOD}}}
    static = {"session": {"static": {"lineage": [{"target_content_hash": GOOD}]}}}
    key = {"emit": [{"name": "r", "do": [{"emit_annotation": {GOOD: "x"}}]}]}
    for dsl in (nested, consts, static, key):
        assert dsl_runner.dsl_placeholder_problems(dsl), dsl


def test_other_interpolation_is_not_affected():
    dsl = {"emit": [{"name": "r", "do": [{"emit_turn": {"content": "${record.text}"}}]}]}
    assert dsl_runner.dsl_placeholder_problems(dsl) == []


def test_every_shipped_dsl_is_clean():
    files = sorted(ADAPTERS.glob("*/*.dsl.yaml")) + sorted(ADAPTERS.glob("*/*/*.dsl.yaml"))
    assert files
    for f in files:
        assert dsl_runner.dsl_placeholder_problems(yaml.safe_load(f.read_text())) == [], f


def test_run_rejects_misplaced_placeholder_at_load(tmp_path):
    """The mechanism itself: run() refuses the adapter before reading input."""
    text = FIXTURE.read_text()
    assert text.count(GOOD) == 1
    moved = text.replace(f'target_content_hash: "{GOOD}"', 'target_content_hash: null') \
                .replace('authored_by: "synthetic-test-judge"',
                         f'authored_by: "{GOOD}"')
    bad = tmp_path / "bad.dsl.yaml"
    bad.write_text(moved)
    with pytest.raises(SystemExit) as exc:
        dsl_runner.run(bad, INPUT, None, operator_override={"type": "scripted"})
    assert "misplaced cross-trace placeholder" in str(exc.value)
    # the unmodified fixture still loads and runs
    dsl_runner.run(FIXTURE, INPUT, None, operator_override={"type": "scripted"})
