"""DSL spec RC 3 §17: the adapter DSL JSON Schema
(schemas/minitrace-adapter-dsl-v0.1.0.schema.json) accepts every shipped
adapter DSL and every DSL fixture, and rejects the structural defects it is
meant to catch. Each negative case mutates a shipped DSL, so a schema that
accepted everything would fail here (the shipped corpus alone cannot tell).
Documents are loaded with the runner's own strict loader (§2)."""
from __future__ import annotations

import copy
import json
import pathlib
import sys

from jsonschema import Draft202012Validator

ADAPTERS = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = ADAPTERS.parent / "schemas" / "minitrace-adapter-dsl-v0.1.0.schema.json"
sys.path.insert(0, str(ADAPTERS))

from dsl_runner import load_adapter_yaml  # noqa: E402


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _dsls() -> tuple[list[pathlib.Path], list[pathlib.Path]]:
    every = sorted(ADAPTERS.rglob("*.dsl.yaml"))
    fixtures = [p for p in every if {"fuzz", "tests"} & set(p.relative_to(ADAPTERS).parts)]
    shipped = [p for p in every if p not in fixtures]
    return shipped, fixtures


def _load(p: pathlib.Path) -> dict:
    return load_adapter_yaml(p.read_text(encoding="utf-8"))


def _base(multi: bool) -> dict:
    shipped, _ = _dsls()
    for p in shipped:
        d = _load(p)
        if ("sessions" in d) == multi:
            return d
    raise AssertionError(f"no shipped {'multi' if multi else 'single'}-trace DSL found")


def _rule(d: dict, **kw) -> dict:
    d = copy.deepcopy(d)
    d["emit"][0].update(kw)
    return d


def _with(d: dict, fn) -> dict:
    d = copy.deepcopy(d)
    fn(d)
    return d


def test_shipped_corpus_is_nonempty_and_valid():
    v = _validator()
    shipped, fixtures = _dsls()
    # Canary: an empty glob would make every assertion below vacuous. The floor
    # holds in the dev tree and in the smaller release tree generated from it.
    assert len(shipped) >= 10, f"only {len(shipped)} shipped DSLs found"
    assert fixtures, "no DSL fixtures found"
    for p in shipped + fixtures:
        errors = [f"{'/'.join(map(str, e.path))}: {e.message}" for e in v.iter_errors(_load(p))]
        assert not errors, f"{p.relative_to(ADAPTERS)}: {errors[:3]}"


def test_accepts_what_the_spec_permits():
    v = _validator()
    single, multi = _base(False), _base(True)
    for name, d in [
        ("single-trace baseline", single),
        ("multi-trace baseline", multi),
        ("unknown top-level key (§2: MAY warn)", _with(single, lambda d: d.update(x_note=1))),
        ("nested combinators", _rule(single, when={"all_of": [{"any_of": [{"has": "a"}, {"field": "b", "present": True}]}]})),
        # DSL rc3 §7.1, spec 15321f73; ruled 2026-10-10: names
        # that only look like operator stay valid.
        ("operator look-alikes", _with(single, lambda d: d["session"].setdefault("static", {}).update(
            {"flags.operator_type": "x", "operators": "x", "x.operator.type": "x"}))),
        ("set to look-alikes of session.operator", _rule(single, do=[{"set": {
            "session.operators": 1, "state.operator.type": "h", "session.x.operator.type": "h"}}])),
        ("emit_turn body key operator", _rule(single, do=[{"emit_turn": {"index": 0, "operator": "x"}}])),
        ("state block key operator", _with(single, lambda d: d.setdefault("state", {}).update({"operator": {"type": "h"}}))),
        ("emit_turn with omit_if_none", _rule(single, do=[{"emit_turn": {"index": 0}, "omit_if_none": ["ordering"]}])),
    ]:
        errors = [e.message for e in v.iter_errors(d)]
        assert not errors, f"{name}: {errors[:3]}"


def _negative_cases() -> list[tuple[str, dict]]:
    single, multi = _base(False), _base(True)
    on_block = {"foreach_block": "$record.x"}
    return [
        # Every rule names the shape, so that only the second key is wrong.
        ("both session and sessions", _with(single, lambda d: (d.update(sessions={"a": {}}),
                                                               [r.update(target="a") for r in d["emit"]]))),
        ("neither session nor sessions", _with(single, lambda d: d.pop("session"))),
        ("multi-trace rule without target", _with(multi, lambda d: d["emit"][0].pop("target"))),
        ("unknown dsl_version", _with(single, lambda d: d["adapter"].update(dsl_version="0.2.0"))),
        ("unknown adapter key", _with(single, lambda d: d["adapter"].update(version="1"))),
        ("unknown input type", _with(single, lambda d: d["input"].update(type="csv"))),
        ("unknown rule key", _rule(single, whne=1)),
        ("rule without when", _with(single, lambda d: d["emit"][0].pop("when"))),
        ("block_type outside on_block", _rule(single, when={"block_type": "text"})),
        ("block_field outside on_block", _rule(single, when={"block_field": "a", "value": 1})),
        ("has_any false", _rule(single, when={"has_any": False})),
        ("empty all_of", _rule(single, when={"all_of": []})),
        ("scope without test", _rule(single, when={"field": "a"})),
        ("scope with both tests", _rule(single, when={"field": "a", "value": 1, "present": True})),
        ("two scopes", _rule(single, when={"field": "a", "container_field": "b", "value": 1})),
        ("unknown action", _rule(single, do=[{"emit_thing": {}}])),
        ("two actions in one mapping", _rule(single, do=[{"set": {"state.a": 1}, "note": {"message": "x"}}])),
        ("omit_if_none beside another action", _rule(single, do=[{"emit_tool_call": {}, "omit_if_none": ["a"]}])),
        ("set to session.lineage", _rule(single, do=[{"set": {"session.lineage": []}}])),
        ("set outside state./session.", _rule(single, do=[{"set": {"trace.x": 1}}])),
        ("accumulate outside state.", _rule(single, do=[{"accumulate": {"session.x": 1}}])),
        ("lineage in session.static", _with(single, lambda d: d["session"].setdefault("static", {}).update({"lineage": []}))),
        ("lineage path in session.derived", _with(single, lambda d: d["session"].setdefault("derived", {}).update({"lineage.0.type": "x"}))),
        ("lineage in finalize.computed", _with(single, lambda d: d.setdefault("finalize", {}).setdefault("computed", {}).update({"lineage": "x"}))),
        # DSL rc3 §8.2.3, spec ee68faba: tool_calls twins of the lineage cases.
        ("set to session.tool_calls", _rule(single, do=[{"set": {"session.tool_calls": []}}])),
        ("set below session.tool_calls", _rule(single, do=[{"set": {"session.tool_calls.0.id": "x"}}])),
        ("tool_calls in session.static", _with(single, lambda d: d["session"].setdefault("static", {}).update({"tool_calls": []}))),
        ("tool_calls path in session.derived", _with(single, lambda d: d["session"].setdefault("derived", {}).update({"tool_calls.0.id": "x"}))),
        ("tool_calls in session.passthrough", _with(single, lambda d: d["session"].setdefault("passthrough", {}).update({"tool_calls": "x"}))),
        ("tool_calls in finalize.computed", _with(single, lambda d: d.setdefault("finalize", {}).setdefault("computed", {}).update({"tool_calls": "x"}))),
        # DSL rc3 §8.2.3 (spec c827be26; ruled 2026-10-05, option 1): $state.tool_calls_by_id is read, never written.
        ("set to state.tool_calls_by_id", _rule(single, do=[{"set": {"state.tool_calls_by_id": {}}}])),
        ("set below state.tool_calls_by_id", _rule(single, do=[{"set": {"state.tool_calls_by_id.5.id": "x"}}])),
        ("accumulate below state.tool_calls_by_id", _rule(single, do=[{"accumulate": {"state.tool_calls_by_id.5.n": 1}}])),
        ("tool_calls_by_id in the state block", _with(single, lambda d: d.setdefault("state", {}).update({"tool_calls_by_id": {}}))),
        ("dotted tool_calls_by_id key in the state block", _with(single, lambda d: d.setdefault("state", {}).update({"tool_calls_by_id.5": {}}))),
        ("operator.type in session.static", _with(single, lambda d: d["session"].setdefault("static", {}).update({"operator.type": "human"}))),
        ("nested operator claim in passthrough", _with(single, lambda d: d["session"].setdefault("passthrough", {}).update({"operator": {"capture_mode": "full"}}))),
        # DSL rc3 §7.1, spec 15321f73; ruled 2026-10-10: the key
        # operator and every key beneath it, whatever the value, everywhere.
        ("operator.notes in session.static", _with(single, lambda d: d["session"].setdefault("static", {}).update({"operator.notes": "x"}))),
        ("operator object in session.static", _with(single, lambda d: d["session"].setdefault("static", {}).update({"operator": {"notes": "x"}}))),
        ("operator null in session.derived", _with(single, lambda d: d["session"].setdefault("derived", {}).update({"operator": None}))),
        ("operator expression in session.derived", _with(single, lambda d: d["session"].setdefault("derived", {}).update({"operator.type": "$record.t"}))),
        ("operator in finalize.computed", _with(single, lambda d: d.setdefault("finalize", {}).setdefault("computed", {}).update({"operator.capture_mode": "$state.m"}))),
        ("set session.operator", _rule(single, do=[{"set": {"session.operator": {"type": "human"}}}])),
        ("set below session.operator", _rule(single, do=[{"set": {"session.operator.capture_purpose.reference": "x"}}])),
        ("operator in a sessions shape", _with(multi, lambda d: next(iter(d["sessions"].values())).setdefault("static", {}).update({"operator.notes": "x"}))),
        ("operation type outside §10", _with(single, lambda d: d.update(operation_types={"bash": "execute"}))),
        # DSL rc3 §15 (spec f2430aed; ruled 2026-10-09): a key with an empty segment.
        ("set key with an inner empty segment", _rule(single, do=[{"set": {"state.a..b": 1}}])),
        ("set key state.", _rule(single, do=[{"set": {"state.": 1}}])),
        ("set key session.", _rule(single, do=[{"set": {"session.": 1}}])),
        ("accumulate key with a trailing dot", _rule(single, do=[{"accumulate": {"state.c.": 1}}])),
        ("accumulate key with an empty segment in on_block", _rule(single, **on_block, on_block=[
            {"when": {"has_any": True}, "do": [{"accumulate": {"state.a..b": 1}}]}])),
        ("static key with an empty segment", _with(single, lambda d: d["session"].setdefault("static", {}).update({"flags..x": 1}))),
        ("derived key with a trailing dot", _with(single, lambda d: d["session"].setdefault("derived", {}).update({"a.": "x"}))),
        ("passthrough key with a leading dot", _with(single, lambda d: d["session"].setdefault("passthrough", {}).update({".a": "x"}))),
        ("finalize.computed key with an empty segment", _with(single, lambda d: d.setdefault("finalize", {}).setdefault("computed", {}).update({"metrics..n": "1"}))),
        ("sessions-shape static key with an empty segment", _with(multi, lambda d: next(iter(d["sessions"].values())).setdefault("static", {}).update({"flags..x": 1}))),
        ("foreach_block without on_block", _rule(single, **on_block)),
        ("nested foreach_block", _rule(single, **on_block, on_block=[{"when": {"has_any": True}, "do": [], "foreach_block": "y"}])),
        # Ruled 2026-10-10, match the schema: operand and body types.
        ("has not a string", _rule(single, when={"has": ["x"]})),
        ("field not a string", _rule(single, when={"field": 5, "value": 1})),
        ("container_field not a string", _rule(single, when={"container_field": ["a"], "present": True})),
        ("block_field not a string", _rule(single, **on_block, on_block=[{"when": {"block_field": 5, "value": 1}, "do": []}])),
        ("present a string", _rule(single, when={"field": "a", "present": "false"})),
        ("present a number", _rule(single, when={"field": "a", "present": 0})),
        ("has_any a string", _rule(single, when={"has_any": "false"})),
        ("has_any a number", _rule(single, when={"has_any": 1})),
        ("attach_tool_result without target_id", _rule(single, do=[{"attach_tool_result": {}}])),
        ("attach_tool_result unknown key", _rule(single, do=[{"attach_tool_result": {"target_id": "a", "result": {}}}])),
        ("attach_tool_result output not a mapping", _rule(single, do=[{"attach_tool_result": {"target_id": "a", "output": 5}}])),
        ("note unknown key", _rule(single, do=[{"note": {"msg": "x"}}])),
        ("note level not a string", _rule(single, do=[{"note": {"level": 5, "message": "x"}}])),
        ("rule name not a string", _rule(single, name=5)),
        ("rule target not a string", _rule(single, target=["a"])),
        # DSL rc3 §8 and §15 (D3, ruled 2026-10-18): a literal
        # foreach_block that is neither a string nor null.
        *((f"foreach_block literal {kind}", _rule(single, foreach_block=v,
                                                  on_block=[{"when": {"has_any": True}, "do": []}]))
          for kind, v in (("number", 5), ("boolean", True), ("list", ["a"]), ("object", {"a": 1}))),
        # Ruled 2026-10-18: refuse at load, as the schema.
        ("rule with when only", _with(single, lambda d: [d["emit"][0].pop(k, None)
                                                         for k in ("do", "foreach_block", "on_block")])),
        ("on_block sub-rule without do", _rule(single, **on_block, on_block=[{"when": {"has_any": True}}])),
        # Ruled 2026-10-18, refuse at load, as the schema: these
        # crashed (AttributeError, KeyError, TypeError) or, for an empty
        # sessions, were refused for the rule's target.
        ("session null", _with(single, lambda d: d.update(session=None))),
        ("session a list", _with(single, lambda d: d.update(session=[1]))),
        ("sessions a list", _with(multi, lambda d: d.update(sessions=[1]))),
        ("sessions empty", _with(multi, lambda d: d.update(sessions={}))),
        ("sessions shape not a mapping", _with(multi, lambda d: d.update(sessions={k: 1 for k in d["sessions"]}))),
        ("adapter absent", _with(single, lambda d: d.pop("adapter"))),
        ("adapter not a mapping", _with(single, lambda d: d.update(adapter=5))),
        ("adapter without dsl_version", _with(single, lambda d: d["adapter"].pop("dsl_version"))),
        # DSL rc3 §4 and §15 (ruled 2026-10-18: UTF-8 only): encoding, where
        # present, is exactly utf-8.
        *((f"encoding {enc!r}", _with(single, lambda d, e=enc: d["input"].update(encoding=e)))
          for enc in ("UTF-8", "utf8", "latin-1", "utf-8-sig", "utf-16", "idna", "hex", "foo", 8, None)),
    ]


def test_rejects_structural_defects():
    v = _validator()
    accepted = [name for name, d in _negative_cases() if not list(v.iter_errors(d))]
    assert not accepted, f"schema accepted: {accepted}"


# Schema cases the runner does not refuse at load: outside DSL rc3 §15's list
# and the 2026-10-10 shape ruling, each awaiting its own ruling (ruled 2026-10-10:
# exempt by name). Fixing one moves it out of this set, or this test fails.
# Empty since the 2026-10-17 ruling: every case is ruled and refused.
_RUNNER_LOAD_EXEMPT_837 = frozenset()


def test_the_runner_refuses_at_load_what_the_schema_rejects():
    # Ruled 2026-10-10, own check plus a schema cross-test: every
    # negative case above is refused by run() before any input is read
    # (SystemExit; the input path does not exist, so reaching input is a
    # ValueError), except the named exempt set, which reaches input. A new
    # schema case is therefore either refused by the runner or named here.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    import dsl_runner  # noqa: PLC0415
    cases = _negative_cases()
    names = [n for n, _ in cases]
    assert len(names) == len(set(names)) and _RUNNER_LOAD_EXEMPT_837 <= set(names), \
        sorted(_RUNNER_LOAD_EXEMPT_837 - set(names))
    refused, reached = [], []
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        for name, d in cases:
            p = td / "a.dsl.yaml"
            p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
            try:
                dsl_runner.run(p, td / "missing.json", None, operator_override={"type": "scripted"})
            except SystemExit:
                refused.append(name)
            except ValueError:
                reached.append(name)
    assert sorted(reached) == sorted(_RUNNER_LOAD_EXEMPT_837), \
        (sorted(set(reached) - _RUNNER_LOAD_EXEMPT_837), sorted(_RUNNER_LOAD_EXEMPT_837 - set(reached)))
    assert len(refused) + len(reached) == len(cases) and len(refused) >= 40, (len(refused), len(reached))


def test_the_runner_accepts_operator_look_alikes_at_load():
    # The load refusal is for operator and beneath it only; the
    # look-alikes the schema accepts reach the input (ValueError: missing).
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    import dsl_runner  # noqa: PLC0415
    single = _base(False)
    cases = [
        _with(single, lambda d: d["session"].setdefault("static", {}).update(
            {"flags.operator_type": "x", "operators": "x", "x.operator.type": "x"})),
        _rule(single, do=[{"set": {"session.operators": 1, "state.operator.type": "h",
                                   "session.x.operator.type": "h"}}]),
        _with(single, lambda d: d.setdefault("state", {}).update({"operator": {"type": "h"}})),
    ]
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        for d in cases:
            assert not list(_validator().iter_errors(d))
            p = td / "a.dsl.yaml"
            p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
            try:
                dsl_runner.run(p, td / "missing.json", None, operator_override={"type": "scripted"})
            except ValueError:
                continue
            raise AssertionError(f"refused or ran: {d}")


def test_the_runner_refuses_a_multi_trace_rule_that_names_no_declared_shape():
    # DSL rc3 §14.3, ruled 2026-10-17: in multi-trace mode
    # every emit rule names a declared shape in target; a rule without one,
    # or naming a shape not under sessions, is refused at load (it was
    # dropped and never fired). A single-trace rule's target has no effect.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    import dsl_runner  # noqa: PLC0415
    multi, single = _base(True), _base(False)
    shape = sorted(multi["sessions"])[0]
    # A second declared shape, last in order, so that a target is checked
    # against every shape and not the first alone.
    second = shape + "-second"
    multi = _with(multi, lambda d: d["sessions"].update({second: copy.deepcopy(d["sessions"][shape])}))
    last = len(multi["emit"]) - 1
    assert last > 0 and list(multi["sessions"])[-1] == second, (last, list(multi["sessions"]))
    refused = {
        "first rule without target": _with(multi, lambda d: d["emit"][0].pop("target")),
        "last rule without target": _with(multi, lambda d: d["emit"][last].pop("target")),
        "undeclared target": _rule(multi, target=shape + "-undeclared"),
        "target of another letter case": _rule(multi, target=shape.upper()),
    }
    loads = {
        "multi-trace, declared target": _rule(multi, target=shape),
        "multi-trace, second declared target": _rule(multi, target=second),
        "single-trace, no target": _with(single, lambda d: d["emit"][0].pop("target", None)),
        "single-trace, any target": _rule(single, target="anything"),
    }
    assert shape.upper() != shape and shape.upper() not in multi["sessions"], shape
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        p = td / "a.dsl.yaml"
        for name, d in {**refused, **loads}.items():
            p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
            try:
                dsl_runner.run(p, td / "missing.json", None, operator_override={"type": "scripted"})
            except SystemExit as e:
                assert name in refused, (name, e.code)
                assert "target" in str(e.code) and "§14.3" in str(e.code), (name, e.code)
            except ValueError:   # reached the (missing) input: loaded
                assert name in loads, name
            else:
                raise AssertionError(f"ran on a missing input: {name}")


def test_the_runner_refuses_the_session_input_and_set_shapes_at_load():
    # Ruled 2026-10-17, refusal at load: exactly one of
    # session and sessions (§2), an input type of json or jsonl (§4), and set
    # keys under state. or session. (§8.2.1). Before, the two keys or neither
    # failed after the input was read, an unknown input type crashed reading
    # it, and a set key elsewhere was written into the trace.
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    import dsl_runner  # noqa: PLC0415
    single, multi = _base(False), _base(True)
    last = len(single["emit"]) - 1
    assert last > 0, last

    def set_in_block(d: dict, key) -> None:
        d["emit"][0]["foreach_block"] = "$record.x"
        d["emit"][0]["on_block"] = [{"when": {"has_any": True}, "do": [{"set": {key: 1}}]}]
    refused = {   # name: (adapter, a word the reason must hold)
        "both session and sessions": (_with(single, lambda d: (d.update(sessions={"a": {}}),
                                                               [r.update(target="a") for r in d["emit"]])), "§2"),
        "neither session nor sessions": (_with(single, lambda d: d.pop("session")), "§2"),
        "input type csv": (_with(single, lambda d: d["input"].update(type="csv")), "§4"),
        "input type JSON": (_with(single, lambda d: d["input"].update(type="JSON")), "§4"),
        "input without type": (_with(single, lambda d: d["input"].pop("type")), "§4"),
        "input not a mapping": (_with(single, lambda d: d.update(input="json")), "§4"),
        "set trace.x in the first rule": (_rule(single, do=[{"set": {"trace.x": 1}}]), "§8.2.1"),
        "set x in the last rule": (_with(single, lambda d: d["emit"][last].update(do=[{"set": {"x": 1}}])), "§8.2.1"),
        "set state without a dot": (_rule(single, do=[{"set": {"state": 1}}]), "§8.2.1"),
        "set beside a good key": (_rule(single, do=[{"set": {"state.ok": 1, "states.x": 1}}]), "§8.2.1"),
        "set in an on_block sub-rule": (_with(single, lambda d: set_in_block(d, "flags.x")), "§8.2.1"),
        "set in a multi-trace rule": (_with(multi, lambda d: d["emit"][0].update(do=[{"set": {"trace.x": 1}}])),
                                      "§8.2.1"),
    }
    loads = {
        "single-trace": single, "multi-trace": multi, "input type jsonl": _with(single, lambda d: d["input"].update(type="jsonl")),
        "set under state. and session.": _rule(single, do=[{"set": {"state.a": 1, "session.b": 2}}]),
        "set under state. in an on_block sub-rule": _with(single, lambda d: set_in_block(d, "state.x")),
    }
    assert not set(refused) & set(loads), set(refused) & set(loads)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        p = td / "a.dsl.yaml"
        for name, d in [*((n, d) for n, (d, _) in refused.items()), *loads.items()]:
            p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
            try:
                dsl_runner.run(p, td / "missing.json", None, operator_override={"type": "scripted"})
            except SystemExit as e:
                assert name in refused and refused[name][1] in str(e.code), (name, e.code)
            except ValueError as e:   # reached the (missing) input: loaded
                assert name in loads and "unsupported input type" not in str(e), (name, e)
            else:
                raise AssertionError(f"ran on a missing input: {name}")


def test_the_runner_refuses_an_adapter_key_a_rule_without_when_and_an_unpaired_block_at_load():
    # Ruled 2026-10-17, as the schema: an unknown key in
    # the adapter block (§3) was ignored; a rule or an on_block sub-rule
    # without when (§8) loaded with a warning and never fired; a
    # foreach_block without on_block iterated for nothing, and an on_block
    # without foreach_block was ignored. A literal null foreach_block beside
    # an on_block stays valid (§8: null iterates nothing).
    import tempfile  # noqa: PLC0415
    import yaml  # noqa: PLC0415
    import dsl_runner  # noqa: PLC0415
    single, multi = _base(False), _base(True)
    last = len(single["emit"]) - 1
    assert last > 0, last
    sub = {"when": {"has_any": True}, "do": []}

    def blocks(d: dict, i: int, **kw) -> None:
        for k in ("foreach_block", "on_block"):
            d["emit"][i].pop(k, None)
        d["emit"][i].update(kw)
    refused = {   # name: (adapter, text the reason must hold)
        "adapter.version": (_with(single, lambda d: d["adapter"].update(version="1")), "adapter: unknown key 'version' (§3)"),
        "adapter.harness_versoin": (_with(single, lambda d: d["adapter"].update(harness_versoin="1")), "'harness_versoin' (§3)"),
        "unknown adapter key first": (_with(single, lambda d: d.update(adapter={"notes": "x", **d["adapter"]})),
                                      "adapter: unknown key 'notes' (§3)"),
        "first rule without when": (_with(single, lambda d: d["emit"][0].pop("when")), "no when"),
        "last rule without when": (_with(single, lambda d: d["emit"][last].pop("when")), "no when"),
        "multi-trace rule without when": (_with(multi, lambda d: d["emit"][0].pop("when")), "no when"),
        "on_block sub-rule without when": (_with(single, lambda d: blocks(
            d, 0, foreach_block="$record.x", on_block=[sub, {"do": []}])), "no when"),
        "foreach_block without on_block": (_with(single, lambda d: blocks(d, 0, foreach_block="$record.x")),
                                           "foreach_block without on_block"),
        "on_block without foreach_block": (_with(single, lambda d: blocks(d, last, on_block=[sub])),
                                           "on_block without foreach_block"),
        "null foreach_block without on_block": (_with(single, lambda d: blocks(d, 0, foreach_block=None)),
                                                "foreach_block without on_block"),
        # Ruled 2026-10-18, refuse at load, as the schema:
        # a rule with when only did nothing; a sub-rule without do swallowed
        # its block (first match wins) and is written do: [] for that.
        "first rule with when only": (_with(single, lambda d: (blocks(d, 0), d["emit"][0].pop("do", None))),
                                      "neither do nor foreach_block"),
        "last rule with when only": (_with(single, lambda d: (blocks(d, last), d["emit"][last].pop("do", None))),
                                     "neither do nor foreach_block"),
        "multi-trace rule with when only": (_with(multi, lambda d: (blocks(d, 0), d["emit"][0].pop("do", None))),
                                            "neither do nor foreach_block"),
        "first on_block sub-rule without do": (_with(single, lambda d: blocks(
            d, 0, foreach_block="$record.x", on_block=[{"when": {"has_any": True}}, sub])), "do: []"),
        "last on_block sub-rule without do": (_with(single, lambda d: blocks(
            d, 0, foreach_block="$record.x", on_block=[sub, {"when": {"has_any": True}}])), "do: []"),
        # Ruled 2026-10-18: refuse at load, as the schema.
        "session null": (_with(single, lambda d: d.update(session=None)), "session: a mapping (§7), not null"),
        "session a string": (_with(single, lambda d: d.update(session="x")), "session: a mapping (§7), not a string"),
        "sessions a list": (_with(multi, lambda d: d.update(sessions=[1])), "sessions: a mapping of shape name to shape (§14), not an array"),
        "sessions null": (_with(multi, lambda d: d.update(sessions=None)), "sessions: a mapping of shape name to shape (§14), not null"),
        "sessions empty": (_with(multi, lambda d: d.update(sessions={})), "sessions: no shape declared (§14)"),
        "sessions shape null": (_with(multi, lambda d: d.update(sessions={k: None for k in d["sessions"]})),
                                f"sessions.{next(iter(multi['sessions']))}: a mapping (§14), not null"),
        # Ruled 2026-10-19: refuse at load, as the schema. These passed
        # load and lint, and then crashed the build (TypeError,
        # AttributeError) on any input, or crashed a load check.
        "state a list": (_with(single, lambda d: d.update(state=[1])), "state: a mapping (§5), not an array"),
        "state null": (_with(single, lambda d: d.update(state=None)), "state: a mapping (§5), not null"),
        "constants a list": (_with(single, lambda d: d.update(constants=[1])), "constants: a mapping (§6), not an array"),
        "finalize a list": (_with(single, lambda d: d.update(finalize=[1])), "finalize: a mapping (§9), not an array"),
        "finalize a string": (_with(single, lambda d: d.update(finalize="x")), "finalize: a mapping (§9), not a string"),
        "finalize.computed a list": (_with(single, lambda d: d.update(finalize={"computed": [1]})),
                                     "finalize.computed: a mapping (§9), not an array"),
        "input.container a list": (_with(single, lambda d: d["input"].update(container=[1])),
                                   "input.container: a mapping (§4.1), not an array"),
        # Ruled 2026-10-19, as the schema: a record_path
        # that is not a string, or absent, crashed run() on any input; an
        # empty container converted nothing, unreported.
        "record_path a number": (_with(single, lambda d: d["input"].update(container={"record_path": 5})),
                                 "input.container.record_path: a string (§4.1), not a number"),
        "record_path null": (_with(single, lambda d: d["input"].update(container={"record_path": None})),
                             "input.container.record_path: a string (§4.1), not null"),
        "record_path a list": (_with(single, lambda d: d["input"].update(container={"record_path": ["m"]})),
                               "input.container.record_path: a string (§4.1), not an array"),
        "record_path absent": (_with(single, lambda d: d["input"].update(container={"path": "m"})),
                               "input.container.record_path: absent; it is required (§4.1)"),
        "container empty": (_with(single, lambda d: d["input"].update(container={})),
                            "input.container.record_path: absent; it is required (§4.1)"),
        "container unknown key": (_with(single, lambda d: d["input"]["container"].update(path="m")),
                                  "input.container: unknown key 'path' (§4.1)"),
        "session.static a list": (_with(single, lambda d: d["session"].update(static=[1])),
                                  "session.static: a mapping (§7), not an array"),
        "session.derived null": (_with(single, lambda d: d["session"].update(derived=None)),
                                 "session.derived: a mapping (§7), not null"),
        "session.passthrough a list": (_with(single, lambda d: d["session"].update(passthrough=[1])),
                                       "session.passthrough: a mapping (§7), not an array"),
        "sessions shape static a list": (_with(multi, lambda d: next(iter(d["sessions"].values())).update(static=[1])),
                                         f"sessions.{next(iter(multi['sessions']))}.static: a mapping (§7), not an array"),
        "sessions shape passthrough a number": (
            _with(multi, lambda d: next(iter(d["sessions"].values())).update(passthrough=5)),
            f"sessions.{next(iter(multi['sessions']))}.passthrough: a mapping (§7), not a number"),
        "last of two shapes derived a list": (
            _with(multi, lambda d: d["sessions"].update(zz_extra={"static": {}, "derived": [1]})),
            "sessions.zz_extra.derived: a mapping (§7), not an array"),
        "adapter absent": (_with(single, lambda d: d.pop("adapter")), "adapter: absent; it is required (§3)"),
        "adapter a number": (_with(single, lambda d: d.update(adapter=5)), "adapter: a mapping (§3), not a number"),
        "adapter without dsl_version": (_with(single, lambda d: d["adapter"].pop("dsl_version")),
                                        "adapter.dsl_version: absent; it is required (§3)"),
        # A document root that is not a mapping crashed.
        "root a list": ([1], "the adapter text: a mapping of top-level keys (§2), not an array"),
        "root null": (None, "the adapter text: a mapping of top-level keys (§2), not null"),
        "root a string": ("x", "the adapter text: a mapping of top-level keys (§2), not a string"),
        # DSL rc3 §4 (ruled 2026-10-18: UTF-8 only): these loaded, and then
        # read the input as another codec, skipped valid utf-16 lines, crashed
        # (hex) or ended the conversion as unreadable (an unknown name).
        "encoding UTF-8": (_with(single, lambda d: d["input"].update(encoding="UTF-8")),
                           "input.encoding: utf-8 only, not 'UTF-8' (§4)"),
        "encoding latin-1": (_with(single, lambda d: d["input"].update(encoding="latin-1")),
                             "input.encoding: utf-8 only, not 'latin-1' (§4)"),
        "encoding hex": (_with(single, lambda d: d["input"].update(encoding="hex")),
                         "input.encoding: utf-8 only, not 'hex' (§4)"),
        "encoding a number": (_with(single, lambda d: d["input"].update(encoding=8)),
                              "input.encoding: utf-8 only, not 8 (§4)"),
    }
    loads = {
        "all six adapter keys": single,
        "state, constants and finalize absent": _with(single, lambda d: [d.pop(k, None) for k in ("state", "constants", "finalize")]),
        "empty state, constants, finalize.computed and session maps": _with(single, lambda d: (
            d.update(state={}, constants={}, finalize={"computed": {}}),
            d["session"].update(static={}, derived={}, passthrough={}))),
        "record_path $self": _with(single, lambda d: d["input"].update(container={"record_path": "$self"})),
        "encoding utf-8": _with(single, lambda d: d["input"].update(encoding="utf-8")),
        "encoding absent": _with(single, lambda d: d["input"].pop("encoding", None)),
        "foreach_block with on_block": _with(single, lambda d: blocks(d, 0, foreach_block="$record.x", on_block=[sub])),
        "null foreach_block with on_block": _with(single, lambda d: blocks(d, 0, foreach_block=None, on_block=[sub])),
        "rule with foreach_block and no do": _with(single, lambda d: (
            d["emit"][0].pop("do", None), blocks(d, 0, foreach_block="$record.x", on_block=[sub]))),
        "rule with do: [] only": _with(single, lambda d: (blocks(d, 0), d["emit"][0].update(do=[]))),
        "on_block sub-rule with do: []": _with(single, lambda d: blocks(
            d, 0, foreach_block="$record.x", on_block=[{"when": {"has_any": True}, "do": []}])),
    }
    schema_adapter = _validator().schema["properties"]["adapter"]
    if "$ref" in schema_adapter:
        schema_adapter = _validator().schema["$defs"][schema_adapter["$ref"].rsplit("/", 1)[1]]
    assert set(single["adapter"]) == set(schema_adapter["properties"]), sorted(single["adapter"])
    assert not set(refused) & set(loads), set(refused) & set(loads)
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        p = td / "a.dsl.yaml"
        for name, d in [*((n, d) for n, (d, _) in refused.items()), *loads.items()]:
            p.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
            try:
                dsl_runner.run(p, td / "missing.json", None, operator_override={"type": "scripted"})
            except SystemExit as e:
                assert name in refused and refused[name][1] in str(e.code), (name, e.code)
            except ValueError:   # reached the (missing) input: loaded
                assert name in loads, name
            else:
                raise AssertionError(f"ran on a missing input: {name}")
        # An empty adapter file reads as null.
        p.write_text("", encoding="utf-8")
        try:
            dsl_runner.run(p, td / "missing.json", None, operator_override={"type": "scripted"})
        except SystemExit as e:
            assert "a mapping of top-level keys (§2), not null" in str(e.code), e.code
        else:
            raise AssertionError("an empty adapter file was not refused")
    # An on_block without foreach_block and without do is
    # told to add the foreach_block, not that the rule does nothing.
    d = _with(single, lambda d: (d["emit"][last].pop("do", None), blocks(d, last, on_block=[sub])))
    problems = dsl_runner.dsl_shape_problems(d)
    assert any("on_block without foreach_block" in p for p in problems), problems
    assert not any("neither do nor foreach_block" in p for p in problems), problems


def main() -> int:
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
