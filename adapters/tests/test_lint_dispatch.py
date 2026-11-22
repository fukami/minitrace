"""Tests for lint_dispatch — the unguarded-emit_turn lint.

Pins three things:
  1. The shipped adapter corpus is clean (no adapter currently trips it).
  2. The lint actually FIRES on the pi-before-fix shape (a guard against a
     no-op lint — the failure mode a lint must never have).
  3. pi's actual fix pattern (an `any_of` role allowlist) and the intended
     single-rule attach+turn pattern both read as clean.
Plus unit coverage of the value-constraint / disjointness core.

Run:
    python adapters/tests/test_lint_dispatch.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from lint_dispatch import (  # noqa: E402
    disjoint,
    discover_adapters,
    lint_adapter,
    value_constraints,
)


# ---------------------------------------------------------------------------
# Corpus: every shipped adapter is clean.

def test_shipped_corpus_is_clean() -> None:
    paths = discover_adapters()
    assert paths, "no adapter DSLs discovered — glob or path wrong"
    offenders = {}
    for path in paths:
        dsl = yaml.safe_load(open(path))
        if not isinstance(dsl, dict):
            continue
        findings = lint_adapter(dsl, path)
        if findings:
            offenders[path] = [repr(f) for f in findings]
    assert not offenders, f"adapters tripped the dispatch lint: {offenders}"


# ---------------------------------------------------------------------------
# The lint must catch the exact regression it exists for.

PI_BEFORE_FIX = {
    "adapter": "pi-test",
    "session": {},
    "emit": [
        # tool_result_message — attaches the result to its tool_call
        {"when": {"all_of": [{"has": "message"},
                             {"field": "message.role", "value": "toolResult"}]},
         "do": [{"attach_tool_result": {"target_id": "x"}}]},
        # user_or_assistant_message — BEFORE FIX: guarded only by has:message,
        # so under all-match dispatch it also fires on toolResult records.
        {"when": {"has": "message"},
         "do": [{"emit_turn": {"index": "1"}}]},
    ],
}

PI_AFTER_FIX = {
    "adapter": "pi-test",
    "session": {},
    "emit": [
        {"when": {"all_of": [{"has": "message"},
                             {"field": "message.role", "value": "toolResult"}]},
         "do": [{"attach_tool_result": {"target_id": "x"}}]},
        # AFTER FIX: positive role allowlist contradicts role==toolResult.
        {"when": {"all_of": [{"has": "message"},
                             {"any_of": [{"field": "message.role", "value": "user"},
                                         {"field": "message.role", "value": "assistant"}]}]},
         "do": [{"emit_turn": {"index": "1"}}]},
    ],
}

SINGLE_RULE_ATTACH_PLUS_TURN = {
    "adapter": "codex-test",
    "session": {},
    "emit": [
        {"when": {"field": "role", "value": "user"}, "do": [{"emit_turn": {"index": "1"}}]},
        # the intended pattern: one rule both attaches and emits a turn.
        {"when": {"field": "role", "value": "tool"},
         "do": [{"attach_tool_result": {"target_id": "x"}}, {"emit_turn": {"index": "2"}}]},
    ],
}


def test_pi_before_fix_is_flagged() -> None:
    findings = lint_adapter(PI_BEFORE_FIX, "pi-before")
    assert len(findings) == 1, f"expected exactly one finding, got {[repr(f) for f in findings]}"
    f = findings[0]
    assert f.i == 1 and f.j == 0, "should flag the emit_turn rule against the attach rule"


def test_pi_after_fix_clears() -> None:
    assert lint_adapter(PI_AFTER_FIX, "pi-after") == [], (
        "the role-allowlist guard must read as disjoint from role==toolResult")


def test_single_rule_attach_plus_turn_not_flagged() -> None:
    # The tool rule does both actions itself (self-pair skipped); the user
    # turn rule is disjoint by role. Nothing to flag.
    assert lint_adapter(SINGLE_RULE_ATTACH_PLUS_TURN, "codex") == []


def test_overlapping_emit_turn_pair_flagged_once() -> None:
    dsl = {
        "adapter": "t", "session": {},
        "emit": [
            {"when": {"has": "message"}, "do": [{"emit_turn": {"index": "1"}}]},
            {"when": {"field": "role", "value": "user"}, "do": [{"emit_turn": {"index": "2"}}]},
        ],
    }
    findings = lint_adapter(dsl, "two-turns")
    assert len(findings) == 1, f"symmetric turn pair should report once, got {len(findings)}"


# ---------------------------------------------------------------------------
# value_constraints / disjoint core.

def test_value_constraints_single_equality() -> None:
    assert value_constraints({"field": "role", "value": "user"}) == {"role": frozenset({"user"})}


def test_value_constraints_has_yields_nothing() -> None:
    assert value_constraints({"has": "message"}) == {}
    assert value_constraints({"has_any": True}) == {}


def test_present_is_its_own_namespace() -> None:
    assert value_constraints({"field": "x", "present": True}) == {"present:x": frozenset({True})}
    assert value_constraints({"container_field": "x", "present": False}) == {
        "present:container:x": frozenset({False})}


def test_present_true_and_false_are_disjoint() -> None:
    a = value_constraints({"all_of": [{"field": "k", "value": "p"}, {"field": "m", "present": False}]})
    b = value_constraints({"all_of": [{"field": "k", "value": "p"}, {"field": "m", "present": True}]})
    assert disjoint(a, b) is True


def test_present_same_polarity_not_disjoint() -> None:
    a = value_constraints({"field": "m", "present": True})
    assert disjoint(a, value_constraints({"field": "m", "present": True})) is False
    # presence and a value on the same field do not contradict (different keys)
    assert disjoint(a, value_constraints({"field": "m", "value": "x"})) is False


def test_any_of_of_equalities_unions_same_field() -> None:
    when = {"any_of": [{"field": "role", "value": "user"},
                       {"field": "role", "value": "assistant"}]}
    assert value_constraints(when) == {"role": frozenset({"user", "assistant"})}


def test_any_of_only_constrains_fields_present_in_all_branches() -> None:
    # role is constrained in both branches; foo only in one -> dropped.
    when = {"any_of": [{"all_of": [{"field": "role", "value": "user"}, {"field": "foo", "value": 1}]},
                       {"field": "role", "value": "assistant"}]}
    assert value_constraints(when) == {"role": frozenset({"user", "assistant"})}


def test_all_of_intersects_same_field() -> None:
    when = {"all_of": [{"field": "x", "value": "a"}, {"has": "y"}]}
    assert value_constraints(when) == {"x": frozenset({"a"})}


def test_container_field_value_is_namespaced() -> None:
    assert value_constraints({"container_field": "defense_type", "value": "none"}) == {
        "container:defense_type": frozenset({"none"})}


def test_disjoint_contradictory_values() -> None:
    assert disjoint({"role": frozenset({"user"})}, {"role": frozenset({"toolResult"})}) is True


def test_not_disjoint_when_values_overlap() -> None:
    assert disjoint({"role": frozenset({"user", "assistant"})},
                    {"role": frozenset({"assistant"})}) is False


def test_not_disjoint_when_no_shared_field() -> None:
    assert disjoint({}, {"role": frozenset({"toolResult"})}) is False


def test_value_overlap_follows_the_runners_equality() -> None:
    # Disjointness by DSL rc3 §8.3 equality (dsl_runner.json_equal),
    # not Python's == and repr: true and 1 never both match one record (no
    # false overlap), while [1] and [1.0], or objects in another key order,
    # match the same value (no missed overlap).
    from dsl_runner import json_equal  # noqa: PLC0415
    from lint_dispatch import _hashable  # noqa: PLC0415

    def vc(v):
        return value_constraints({"field": "f", "value": v})
    assert disjoint(vc(True), vc(1)) is True
    assert disjoint(vc(False), vc(0)) is True
    assert disjoint(vc("1"), vc(1)) is True
    assert disjoint(vc(1), vc(1.0)) is False
    assert disjoint(vc([1]), vc([1.0])) is False
    assert disjoint(vc([True]), vc([1])) is True
    assert disjoint(vc({"a": 1, "b": [2]}), vc({"b": [2.0], "a": 1})) is False
    assert disjoint(vc({"a": True}), vc({"a": 1})) is True
    values = [None, True, False, 0, 1, 1.0, 0.0, -0.0, 2, "1", "", "true", [], [1], [1.0], [True], [[1]],
              {}, {"a": 1}, {"a": 1.0}, {"a": True}, {"b": 1}, {"a": 1, "b": 2}, {"b": 2, "a": 1}, ["b", True]]
    for a in values:
        for b in values:
            assert (_hashable(a) == _hashable(b)) == json_equal(a, b), (a, b)


# --- never-matching predicates (dsl_runner.predicate_problems) -------------

from dsl_runner import dsl_predicate_problems, predicate_matches, predicate_problems  # noqa: E402


def test_recognised_shapes_have_no_problems() -> None:
    for pred in ({"field": "a", "value": 1}, {"field": "a", "present": True},
                 {"container_field": "a", "value": True}, {"has": "a"}, {"has_any": True},
                 {"all_of": [{"field": "a", "value": 1}, {"any_of": [{"has": "b"}]}]}):
        assert predicate_problems(pred) == [], pred
    assert predicate_problems({"block_type": "text"}, in_block=True) == []
    assert predicate_problems({"block_field": "x", "value": 1}, in_block=True) == []


def test_vibe_equals_and_opencode_length_gt_are_flagged_and_really_never_match() -> None:
    # The two shapes that sat dead in shipped adapters (f533ad69, 9499d90a).
    # Each must be flagged AND must evaluate False on a record/container that
    # satisfies its intent, so the flag describes the runner, not a guess.
    equals = {"field": "injected", "equals": True}
    length_gt = {"container_field": "events", "length_gt": 0}
    assert predicate_problems(equals) and predicate_problems(length_gt)
    assert predicate_matches(equals, {"injected": True}) is False
    assert predicate_matches(length_gt, {}, container={"events": [1, 2]}) is False


def test_other_never_matching_shapes_are_flagged() -> None:
    for pred in ({}, None, {"field": "a"}, {"field": "a", "value": 1, "equals": 1},
                 {"all_of": []}, {"all_of": [{"has": "a"}], "field": "b", "value": 1},
                 {"has_any": False}, {"bogus": 1}):
        assert predicate_problems(pred), pred
    assert predicate_problems({"block_type": "text"})  # outside on_block
    assert predicate_problems({"block_field": "x", "value": 1})


def _lint(paths: list[str]) -> tuple[int, str]:
    import contextlib  # noqa: PLC0415
    import io  # noqa: PLC0415
    import lint_dispatch  # noqa: PLC0415
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = lint_dispatch.main(["lint_dispatch.py", *paths])
    return rc, out.getvalue()


def test_lint_reports_every_load_refusal_with_the_runners_message() -> None:
    # Lint is clean exactly when run() loads a shipped adapter (ruled
    # 2026-10-19): a shape error and a later load check (dsl_version,
    # lineage) both fail lint, with the message run() exits with.
    import copy  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    import dsl_runner as D  # noqa: PLC0415
    base_path = pathlib.Path(__file__).resolve().parent.parent / "agentdojo" / "agentdojo.dsl.yaml"
    base = yaml.safe_load(base_path.read_text())
    shape = copy.deepcopy(base)
    shape["emit"][0]["bogus_key"] = 1
    version = copy.deepcopy(base)
    version["adapter"]["dsl_version"] = "0.2.0"
    lineage = copy.deepcopy(base)
    lineage["emit"][0]["do"].append({"emit_lineage": {"type": "judges", "target_content_hash": "sha256:00"}})
    do_not_list = copy.deepcopy(base)   # the other checks assume a list: not linted further
    do_not_list["emit"][0]["do"] = 5
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        assert _lint([str(base_path)])[0] == 0
        for name, dsl, head in (("shape", shape, "shape error"), ("version", version, "unsupported dsl_version"),
                                ("lineage", lineage, "lineage outside its one form"),
                                ("do", do_not_list, "shape error")):
            p = td / f"{name}.dsl.yaml"
            p.write_text(yaml.safe_dump(dsl, sort_keys=False))
            try:
                D.run(p, td / "absent.json", None, operator_override={"type": "scripted"})
                raise AssertionError(f"{name}: run() loaded the adapter")
            except SystemExit as e:
                message = str(e.code)
            assert message.startswith(head), (name, message[:120])
            rc, out = _lint([str(p)])
            assert rc == 1 and "refused at load" in out and message.splitlines()[0] in out, (name, out)
            assert all(line.strip() in out for line in message.splitlines()), (name, out)
            assert out.count("FAIL:") == 1, (name, out)
        # A load check that crashes is reported, and the files after it are
        # still linted. The shape check now refuses the list finalize that
        # crashed here, so the crash is injected for one marked adapter.
        import lint_dispatch  # noqa: PLC0415
        crash = copy.deepcopy(base)
        crash["constants"] = {"crash_marker": 1}
        p = td / "crash.dsl.yaml"
        p.write_text(yaml.safe_dump(crash, sort_keys=False))
        real = lint_dispatch.dsl_load_problems

        def crashing(dsl):
            if (dsl.get("constants") or {}).get("crash_marker"):
                raise AttributeError("'list' object has no attribute 'get'")
            return real(dsl)
        lint_dispatch.dsl_load_problems = crashing
        try:
            rc, out = _lint([str(p), str(td / "shape.dsl.yaml")])
        finally:
            lint_dispatch.dsl_load_problems = real
        assert rc == 1 and "crash.dsl.yaml" in out and "load check crashed (AttributeError" in out, out
        assert "shape.dsl.yaml" in out and "2 shipped adapter DSL(s) refused" in out, out


def test_load_check_covers_shipped_adapters_only() -> None:
    # Shipped means outside tests/ and fuzz/ (ruled 2026-10-19): the fuzz
    # probes refused by design stay in the other two checks, not this one.
    # Tree-relative (ruled 2026-10-19): adapters/fuzz/ does not ship, so the
    # probes are asserted where it exists, and then all three must be there.
    import lint_dispatch  # noqa: PLC0415
    import os  # noqa: PLC0415
    root = pathlib.Path(lint_dispatch.__file__).resolve().parent
    paths = discover_adapters()
    shipped = [p for p in paths if lint_dispatch.load_checked(p)]
    if (root / "fuzz").is_dir():
        probes = [p for p in paths if p.endswith(("_a15.dsl.yaml", "_a16.dsl.yaml", "gate-probes/lin.dsl.yaml"))]
        assert len(probes) == 3 and not any(lint_dispatch.load_checked(p) for p in probes), probes
        assert len(shipped) < len(paths), "every discovered DSL is load-checked, the probes included"
    rel = [pathlib.Path(p).relative_to(root).parts for p in shipped]
    assert shipped and all("tests" not in r[:-1] and "fuzz" not in r[:-1] for r in rel), rel
    assert not lint_dispatch.load_checked(str(root / "tests" / "x" / "a.dsl.yaml"))
    assert not lint_dispatch.load_checked(str(root / "fuzz" / "a.dsl.yaml"))
    assert lint_dispatch.load_checked(str(root / "pi" / "a.dsl.yaml"))
    assert lint_dispatch.load_checked("/elsewhere/a.dsl.yaml")
    # A relative argv path is classified as the absolute one; the path is
    # constructed, so the check holds in a tree without fuzz/ as well.
    probe = os.path.relpath(root / "fuzz" / "x" / "a.dsl.yaml")
    assert not os.path.isabs(probe) and not lint_dispatch.load_checked(probe), probe
    rc, out = _lint([])
    assert rc == 0 and f"{len(shipped)} shipped adapter DSL(s) load" in out, out


def test_dsl_scan_reaches_on_block_sub_rules() -> None:
    dsl = {"emit": [{"name": "r", "when": {"has": "a"},
                     "on_block": [{"name": "s", "when": {"block_field": "x", "equals": 1}}]}]}
    probs = dsl_predicate_problems(dsl)
    assert len(probs) == 1 and probs[0].startswith("r/s.when"), probs


# A top-level note on every record fails a shipped adapter (ruled 2026-10-21).

def _pi_with(rule: dict | None) -> dict:
    root = pathlib.Path(__file__).resolve().parent.parent
    dsl = yaml.safe_load((root / "pi" / "0.58.1.dsl.yaml").read_text(encoding="utf-8"))
    if rule is not None:
        dsl["emit"].append(rule)
    return dsl


def _lint_dsl(dsl: dict) -> tuple[int, str]:
    import tempfile  # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        p = pathlib.Path(td) / "probe.dsl.yaml"
        p.write_text(yaml.safe_dump(dsl, sort_keys=False), encoding="utf-8")
        return _lint([str(p)])


NOTE = [{"note": {"level": "warning", "message": "x"}}]


def test_note_on_every_record_fails_lint() -> None:
    import lint_dispatch  # noqa: PLC0415
    rc, out = _lint_dsl(_pi_with(None))
    assert rc == 0, out
    flooding = [{"has_any": True},
                {"all_of": [{"has_any": True}]},
                {"any_of": [{"has": "message"}, {"has_any": True}]}]
    for when in flooding:
        assert lint_dispatch.always_matches(when), when
        rc, out = _lint_dsl(_pi_with({"name": "every", "when": when,
                                      "do": [{"set": {"state.n": 1}}, *NOTE]}))
        assert rc == 1 and "1 shipped rule(s) writing a note for every record" in out, (when, out)
        assert f"rule[{len(_pi_with(None)['emit'])}] every" in out, out


def test_narrowed_or_noteless_or_on_block_catch_all_passes_lint() -> None:
    import lint_dispatch  # noqa: PLC0415
    clean = [
        {"name": "narrow", "when": {"field": "type", "value": "compaction"}, "do": NOTE},
        {"name": "partial", "when": {"all_of": [{"has_any": True}, {"has": "x"}]}, "do": NOTE},
        {"name": "noteless", "when": {"has_any": True}, "do": [{"set": {"state.n": 1}}]},
        {"name": "blocks", "when": {"has": "message"}, "foreach_block": "$record.message.content",
         "on_block": [{"name": "other", "when": {"has_any": True}, "do": NOTE}]},
    ]
    # has_any is exactly true (§8.1); a truthy string is not a catch-all.
    assert not lint_dispatch.always_matches({"has_any": "true"})
    for rule in clean:
        assert not lint_dispatch.always_matches(rule["when"]) or rule["name"] == "noteless", rule
        rc, out = _lint_dsl(_pi_with(rule))
        assert rc == 0, (rule["name"], out)


# Release wording (ruled 2026-10-20): what a user reads when an adapter is
# refused at load cites the published DSL spec, and the shipped adapters
# state conformance to the published specs, not to candidates or drafts.

def _load_message_strings() -> list[str]:
    import ast
    import inspect
    import dsl_runner
    fn = ast.parse(inspect.getsource(dsl_runner.dsl_load_problems)).body[0]
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    return [n.value for stmt in body for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_load_messages_cite_the_published_dsl_spec():
    import re
    strings = _load_message_strings()
    cites = [s for s in strings if "DSL spec" in s]
    assert len(cites) >= 10, cites
    bad = [s for s in cites if not re.search(r"DSL spec v0\.1\.0 §\d", s)]
    assert not bad, bad
    stale = [s for s in strings if re.search(r"\brc ?\d|\bRC ?\d|draft-\d", s)]
    assert not stale, stale


def test_a_refused_adapter_reports_the_published_dsl_spec():
    from dsl_runner import dsl_load_problems
    dsl = {"adapter": {"dsl_version": "0.1.0"}, "input": {"type": "json"},
           "session": {"static": {}, "derived": {"id": "x", "t": "v-${undefined_fn(record.a)}"}},
           "emit": []}
    message = dsl_load_problems(dsl) or ""
    assert "(DSL spec v0.1.0 §8.3)" in message, message


def test_shipped_adapter_headers_name_the_published_specs():
    import re
    adapters = pathlib.Path(__file__).resolve().parent.parent
    for name in ("agentdojo", "claude-code", "droid", "goose", "opencode", "vibe"):
        text = (adapters / name / f"{name}.dsl.yaml").read_text(encoding="utf-8")
        head = " ".join(line.lstrip("# ").strip() for line in text.splitlines()[:60]
                        if line.startswith("#"))
        assert "Conforms to minitrace-spec v0.3.0 + minitrace-adapter-dsl-spec v0.1.0." in head, name
        assert not re.search(r"draft-|-rc\d|RC ?\d", head.split("Conforms to", 1)[1][:120]), name


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
