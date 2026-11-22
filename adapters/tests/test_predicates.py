"""Predicate-evaluation tests for dsl_runner.predicate_matches.

Surfaced by droid adapter cycle-11 implementation findings (2026-05-12):

  1. `{ field: <path>, present: <bool> }` was absent from spec §8.1 AND
     the runner — predicate fell through to `return False` for any
     adapter using it (droid: provider_metadata / D8).

  2. `on_block` dispatch is first-match-wins per block (relied on by
     mast's `unknown_block: has_any: true` catch-all). Adapters that
     want to emit multiple events per block (droid: tool_call +
     risk_assessment annotation) MUST declare separate top-level rules
     each with its own `foreach_block` over the same collection.

Dispatch-model coverage added 2026-05-27, after commit
02badb67 silently switched the *top-level* rule loop from first-match
to all-match ("# No break: rules are ordered actions"), double-firing
pi's records that matched both `tool_result_message` (attach) and an
unguarded `user_or_assistant_message` (emit_turn). The two dispatch
models are asymmetric and were undocumented by any executing test:
  - top level (`dsl["emit"]`): ALL matching rules fire (no break)
  - `on_block` (per foreach block): FIRST matching rule wins (break)

This test pins all three: (1) the runner's `field + present` branch on
non-null / null / missing values; (2) top-level all-match double-fire;
(3) on_block first-match-wins. (2) and (3) drive `_build_trace`, not
just `predicate_matches`, so a future flip of either model breaks here.

Run:
    python -m pytest adapters/tests/test_predicates.py -v
or:
    python adapters/tests/test_predicates.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dsl_runner import _build_trace, predicate_matches  # noqa: E402


# ---------------------------------------------------------------------------
# `field + present` predicate (D8 fix)

def test_field_present_true_matches_non_null_value() -> None:
    record = {"openai_message_id": "msg_abc123"}
    pred = {"field": "openai_message_id", "present": True}
    assert predicate_matches(pred, record) is True


def test_field_present_true_does_not_match_null_value() -> None:
    record = {"openai_message_id": None}
    pred = {"field": "openai_message_id", "present": True}
    assert predicate_matches(pred, record) is False


def test_field_present_true_does_not_match_missing_key() -> None:
    record = {"other_field": "x"}
    pred = {"field": "openai_message_id", "present": True}
    assert predicate_matches(pred, record) is False


def test_field_present_false_matches_null_value() -> None:
    record = {"openai_message_id": None}
    pred = {"field": "openai_message_id", "present": False}
    assert predicate_matches(pred, record) is True


def test_field_present_false_matches_missing_key() -> None:
    record = {"other_field": "x"}
    pred = {"field": "openai_message_id", "present": False}
    assert predicate_matches(pred, record) is True


def test_field_present_false_does_not_match_non_null_value() -> None:
    record = {"openai_message_id": "msg_abc123"}
    pred = {"field": "openai_message_id", "present": False}
    assert predicate_matches(pred, record) is False


def test_field_present_works_with_deep_path() -> None:
    record = {"meta": {"openai_message_id": "msg_xyz"}}
    pred = {"field": "meta.openai_message_id", "present": True}
    assert predicate_matches(pred, record) is True

    record_null = {"meta": {"openai_message_id": None}}
    assert predicate_matches(pred, record_null) is False


def test_field_present_inside_all_of_combinator() -> None:
    """The droid D8 use case: combined with another predicate."""
    record = {"event_kind": "assistant_with_tools", "openai_message_id": "msg_1"}
    pred = {
        "all_of": [
            {"field": "event_kind", "value": "assistant_with_tools"},
            {"field": "openai_message_id", "present": True},
        ],
    }
    assert predicate_matches(pred, record) is True

    record_no_msgid = {"event_kind": "assistant_with_tools", "openai_message_id": None}
    assert predicate_matches(pred, record_no_msgid) is False


# ---------------------------------------------------------------------------
# Symmetry check: block_field + present should still work (regression guard)

def test_block_field_present_true_matches_non_null_value() -> None:
    block = {"id": "tool_call_1", "risk_level": "low"}
    pred = {"block_field": "risk_level", "present": True}
    assert predicate_matches(pred, {}, block=block) is True


def test_block_field_present_true_does_not_match_null_value() -> None:
    block = {"id": "tool_call_1", "risk_level": None}
    pred = {"block_field": "risk_level", "present": True}
    assert predicate_matches(pred, {}, block=block) is False


def test_container_field_present_true_matches_non_null_value() -> None:
    container = {"defense_type": "detected_with_warning"}
    pred = {"container_field": "defense_type", "present": True}
    assert predicate_matches(pred, {}, container=container) is True


# ---------------------------------------------------------------------------
# Dispatch-model semantics — these drive _build_trace, the
# loop that actually contains the two asymmetric dispatch models, not just
# predicate_matches in isolation.

def _minimal_dsl(emit: list[dict]) -> dict:
    """Smallest DSL _build_trace will accept: an adapter name, an empty
    session block, and the emit rules under test."""
    return {"adapter": "test", "session": {}, "emit": emit}


def test_top_level_dispatch_is_all_match() -> None:
    """Two top-level rules whose `when:` both match the same record BOTH
    fire (no break). This is the exact shape that double-fired pi after
    02badb67: a broad `has:`-only rule overlapping a specific rule. If the
    loop is ever switched back to first-match, only one turn would appear
    and this breaks loudly — the regression guard pi never had.
    """
    dsl = _minimal_dsl([
        # broad rule (pi's unguarded user_or_assistant_message analogue)
        {"when": {"has": "message"}, "do": [{"emit_turn": {"index": "broad"}}]},
        # specific rule (pi's tool_result_message analogue)
        {"when": {"field": "role", "value": "assistant"},
         "do": [{"emit_turn": {"index": "specific"}}]},
    ])
    record = {"message": {"content": "hi"}, "role": "assistant"}  # matches both
    trace, _ = _build_trace(dsl, [record], container=None, container_first=None)
    indices = [t["index"] for t in trace["turns"]]
    assert indices == ["broad", "specific"], (
        f"expected all-match double-fire ['broad','specific'], got {indices}")


def test_top_level_non_overlapping_rules_fire_once_each() -> None:
    """Sanity floor: with mutually-exclusive `when:` clauses (how every
    shipped adapter is written), each record fires exactly one rule. All-
    match dispatch is only a hazard under overlap."""
    dsl = _minimal_dsl([
        {"when": {"field": "role", "value": "user"},
         "do": [{"emit_turn": {"index": "u"}}]},
        {"when": {"field": "role", "value": "assistant"},
         "do": [{"emit_turn": {"index": "a"}}]},
    ])
    records = [{"role": "user"}, {"role": "assistant"}]
    trace, _ = _build_trace(dsl, records, container=None, container_first=None)
    assert [t["index"] for t in trace["turns"]] == ["u", "a"]


def test_on_block_dispatch_is_first_match_wins() -> None:
    """Inside a foreach_block, the FIRST matching on_block rule wins and the
    loop breaks (relied on by mast's `unknown_block: has_any: true` catch-
    all). The opposite of the top-level loop — pinning the asymmetry that
    an earlier review flagged as a latent footgun."""
    dsl = _minimal_dsl([
        {"when": {"has_any": True},
         "foreach_block": "record.blocks",
         "on_block": [
             {"when": {"has_any": True},
              "do": [{"emit_turn": {"index": "first"}}]},
             {"when": {"has_any": True},
              "do": [{"emit_turn": {"index": "second"}}]},
         ]},
    ])
    record = {"blocks": [{"type": "x"}]}  # single block, both on_block rules match
    trace, _ = _build_trace(dsl, [record], container=None, container_first=None)
    indices = [t["index"] for t in trace["turns"]]
    assert indices == ["first"], (
        f"expected first-match-wins ['first'], got {indices}")


# ---------------------------------------------------------------------------
# A note whose message fails to render (ruled 2026-10-21)

def _notes(message: str, record: dict) -> list[dict]:
    dsl = _minimal_dsl([{"when": {"has": "k"},
                         "do": [{"note": {"level": "warning", "message": message}}]}])
    _, diags = _build_trace(dsl, [record], container=None, container_first=None)
    return diags


def test_note_that_failed_to_render_is_reported_once_by_its_input_failure() -> None:
    # ${record} cannot interpolate a dict: the input failure names
    # note.message, and no second diagnostic with a null message follows.
    diags = _notes("keys=${record}", {"k": 1})
    assert [d["kind"] for d in diags] == ["input-failure"], diags
    assert diags[0]["field"] == "note.message", diags


def test_note_that_renders_null_without_a_failure_is_kept() -> None:
    # An absent path is null, not an input failure, so the note stays.
    diags = _notes("$record.absent", {"k": 1})
    assert [(d["kind"], d["message"]) for d in diags] == [("note", None)], diags


def test_note_that_renders_is_kept() -> None:
    diags = _notes("k=${record.k}", {"k": 1})
    assert [(d["kind"], d["level"], d["message"]) for d in diags] == [("note", "warning", "k=1")], diags


def test_pi_content_type_is_null_exactly_when_content_is_null() -> None:
    # Ruled 2026-10-21, not a spec requirement (content_type: trace v0.2.0
    # §3 via rc3 §1.4): a pi message without a text block has null content,
    # and its content_type is null with it.
    import yaml  # noqa: PLC0415
    adapters = pathlib.Path(__file__).resolve().parent.parent
    dsl = yaml.safe_load((adapters / "pi" / "0.58.1.dsl.yaml").read_text(encoding="utf-8"))

    def msg(content: list) -> dict:
        return {"type": "message", "message": {"role": "assistant", "timestamp": 1773500000000,
                                               "content": content}}
    records = [msg([{"type": "text", "text": "hi"}]),
               msg([{"type": "toolCall", "id": "c1", "name": "read", "arguments": {}}]),
               msg([{"type": "text", "text": ""}])]
    trace, _ = _build_trace(dsl, records, container=None, container_first=None)
    got = [(t["content"], t["content_type"]) for t in trace["turns"]]
    assert got == [("hi", "text"), (None, None), ("", "text")], got
    # An unusable text block is one input failure, not one per field.
    trace, diags = _build_trace(dsl, [msg([{"type": "text", "text": 5}])],
                                container=None, container_first=None)
    assert [(t["content"], t["content_type"]) for t in trace["turns"]] == [(None, None)], trace
    assert [d["kind"] for d in diags] == ["input-failure"], diags


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    tests = [obj for name, obj in inspect.getmembers(mod)
             if name.startswith("test_") and callable(obj)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
