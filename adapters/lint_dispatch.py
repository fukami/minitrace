#!/usr/bin/env python3
"""Lint: unguarded emit_turn overlapping another turn/tool rule.

Why this exists (2026-05-27)
----------------------------
The top-level rule loop in `dsl_runner._build_trace` is ALL-MATCH: every
rule whose `when:` matches a record fires, with no break (since commit
02badb67). That makes a broad `emit_turn` rule dangerous: if its `when:`
also matches records that a *different* rule already handles as a tool
result / tool call / another turn, the record gets a spurious extra turn.

This is exactly how pi regressed. pi's `user_or_assistant_message` rule
was guarded only by `has: message`; under all-match it fired on
`toolResult` records too (which the `tool_result_message` rule attaches),
inflating turn_count 5 -> 8 for twelve days undetected.

What this flags
---------------
For every top-level rule R that emits a turn, we check every *other*
top-level rule S that is turn-bearing or tool-semantic (does `emit_turn`,
`attach_tool_result`, or `emit_tool_call`). If R's `when:` is not provably
disjoint from S's, R could double-fire on S's records -> finding.

"Provably disjoint" = some field is constrained by both predicates to
value sets that do not intersect. `any_of` of `field == value` over the
same field folds to the union of those values (so pi's fixed
`any_of: [role==user, role==assistant]` reads as a real guard that
contradicts `role==toolResult`). Value-equality constraints
(`field: X, value: Y` and `container_field: X, value: Y`) participate, and so
does presence (`field: X, present: P`, keyed `present:X`): the runner reads it
as `(value is not None) == P`, so `present: true` and `present: false` on the
same field cannot both hold (claude-code's mode-segment rule pair, 2b0f7d36).
`has` / `has_any` are treated as non-discriminating, which is the
conservative direction (it can only over-report, never miss a bug).

The intended single-rule pattern (a tool rule doing BOTH `attach` and
`emit_turn` in its own `do:`) never trips this, because R and S are the
same rule and self-pairs are skipped.

Second check: predicates that never match
-----------------------------------------
dsl_runner.predicate_matches() evaluates a fixed set of predicate shapes and
returns False for anything else, silently: vibe's `equals:` and opencode's
`length_gt` rules never fired (f533ad69, 9499d90a). Every `when:` (including
on_block sub-rules) is checked with dsl_runner.dsl_predicate_problems, the
runner's own definition of a recognised shape, so the two cannot drift.

Third check: adapters the runner refuses at load
------------------------------------------------
Every shipped adapter (outside tests/ and fuzz/) goes through
dsl_runner.dsl_load_problems, the checks run() refuses an adapter text on at
load, and a refusal is reported with run()'s own message, so lint is clean
exactly when run() would load it. A refused adapter is not linted further.

Fourth check: a note on every record
------------------------------------
A top-level rule whose `when:` always matches (`has_any: true`, or an
all_of/any_of that reduces to it) and whose `do:` holds a `note` writes that
note for every record, handled ones included: pi's unhandled-record catch-all
gave two diagnostics per record. The spec allows such a rule, so the runner
loads it; the lint fails on it in a shipped adapter (ruled 2026-10-21).
on_block catch-alls are first-match and are not flagged.

Run:
    python adapters/lint_dispatch.py            # scan all shipped adapters
    python adapters/lint_dispatch.py PATH ...   # scan specific DSL files
Exit code 1 if any finding, 0 if clean.
"""
from __future__ import annotations

import pathlib
import sys
from glob import glob
from typing import Any

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from dsl_runner import dsl_load_problems, dsl_predicate_problems, load_adapter_yaml  # noqa: E402

TURN_ACTION = "emit_turn"
# A rule S "competes" with a turn rule if firing on the same record would
# mean that record carries two turns, or a turn plus a tool semantic.
COMPETING_ACTIONS = frozenset({"emit_turn", "attach_tool_result", "emit_tool_call"})


def rule_actions(rule: dict) -> set[str]:
    actions: set[str] = set()
    for act in (rule.get("do") or []):
        actions |= set(act.keys())
    return actions


def _hashable(v: Any) -> Any:
    """A key that two `value:` operands share exactly when the runner's
    §8.3 equality (dsl_runner.json_equal) holds between them: typed,
    so true and 1 differ; numbers by value, so 1 and 1.0 agree; lists by
    their elements in order and objects by their members, whatever the key
    order. Python's == and repr disagreed both ways (true == 1; repr([1])
    != repr([1.0])). A string, number or null is its own key: among those
    Python's == is already §8.3 equality (1 == 1.0, and no cross-type match)."""
    if isinstance(v, bool):
        return ("b", v)
    if isinstance(v, list):
        return ("l", tuple(_hashable(x) for x in v))
    if isinstance(v, dict):
        return ("o", frozenset((k, _hashable(x)) for k, x in v.items()))
    return v


def value_constraints(when: dict | None) -> dict[str, frozenset]:
    """Map field-path -> frozenset of values the predicate REQUIRES.

    Only value-equality constraints contribute. `all_of` intersects per
    field (AND), `any_of` unions but only over fields constrained in
    *every* branch (OR). Fields not provably constrained are simply
    absent from the map.
    """
    out: dict[str, frozenset] = {}
    if not when:
        return out
    if "all_of" in when:
        for child in when["all_of"]:
            for f, vals in value_constraints(child).items():
                out[f] = (out[f] & vals) if f in out else vals
        return out
    if "any_of" in when:
        branch_maps = [value_constraints(b) for b in when["any_of"]]
        if not branch_maps:
            return out
        common = set(branch_maps[0])
        for m in branch_maps[1:]:
            common &= set(m)
        for f in common:
            union: set = set()
            for m in branch_maps:
                union |= set(m[f])
            out[f] = frozenset(union)
        return out
    if "field" in when and "value" in when:
        out[when["field"]] = frozenset({_hashable(when["value"])})
    elif "container_field" in when and "value" in when:
        out["container:" + when["container_field"]] = frozenset({_hashable(when["value"])})
    elif "field" in when and "present" in when:
        out["present:" + when["field"]] = frozenset({bool(when["present"])})
    elif "container_field" in when and "present" in when:
        out["present:container:" + when["container_field"]] = frozenset({bool(when["present"])})
    return out


def disjoint(a: dict[str, frozenset], b: dict[str, frozenset]) -> bool:
    """True if no record can satisfy both predicates' value constraints."""
    for field in set(a) & set(b):
        if not (a[field] & b[field]):
            return True
    return False


def _when_summary(w: dict | None) -> str:
    if not w:
        return "{}"
    if "all_of" in w:
        return "all_of[" + ", ".join(_when_summary(p) for p in w["all_of"]) + "]"
    if "any_of" in w:
        return "any_of[" + ", ".join(_when_summary(p) for p in w["any_of"]) + "]"
    if w.get("has_any"):
        return "has_any"
    if "has" in w:
        return "has:" + str(w["has"])
    if "field" in w and "value" in w:
        return str(w["field"]) + "==" + repr(w["value"])
    if "field" in w and "present" in w:
        return str(w["field"]) + " present:" + str(w["present"])
    if "container_field" in w:
        kind = "value" if "value" in w else "present"
        return "cfield:" + str(w["container_field"]) + "(" + kind + ")"
    return str(list(w))


class Finding:
    __slots__ = ("adapter", "i", "j", "turn_when", "other_when", "other_acts")

    def __init__(self, adapter, i, j, turn_when, other_when, other_acts):
        self.adapter = adapter
        self.i = i
        self.j = j
        self.turn_when = turn_when
        self.other_when = other_when
        self.other_acts = other_acts

    def __repr__(self) -> str:
        return (f"{self.adapter}: rule[{self.i}] emit_turn when={self.turn_when} "
                f"is NOT disjoint from rule[{self.j}] ({', '.join(sorted(self.other_acts))}) "
                f"when={self.other_when} -> emit_turn can double-fire on that rule's records")


def lint_adapter(dsl: dict, adapter: str) -> list[Finding]:
    rules = dsl.get("emit", []) or []
    enriched = [
        (i, rule_actions(r), value_constraints(r.get("when", {})), r.get("when", {}))
        for i, r in enumerate(rules)
    ]
    findings: list[Finding] = []
    seen: set[frozenset] = set()
    for i, acts_i, cons_i, when_i in enriched:
        if TURN_ACTION not in acts_i:
            continue
        for j, acts_j, cons_j, when_j in enriched:
            if j == i or not (acts_j & COMPETING_ACTIONS):
                continue
            if disjoint(cons_i, cons_j):
                continue
            pair = frozenset({i, j})
            # When both rules emit turns, the pair is symmetric; report once.
            if TURN_ACTION in acts_j and pair in seen:
                continue
            seen.add(pair)
            findings.append(Finding(adapter, i, j, _when_summary(when_i),
                                    _when_summary(when_j), acts_j))
    return findings


def always_matches(when: Any) -> bool:
    """True for a predicate every record satisfies: has_any: true, an all_of
    whose members all do, or an any_of with one member that does."""
    if not isinstance(when, dict):
        return False
    if set(when) == {"has_any"}:
        return when["has_any"] is True
    if set(when) == {"all_of"} and isinstance(when["all_of"], list) and when["all_of"]:
        return all(always_matches(c) for c in when["all_of"])
    if set(when) == {"any_of"} and isinstance(when["any_of"], list):
        return any(always_matches(c) for c in when["any_of"])
    return False


def every_record_notes(dsl: dict, adapter: str) -> list[str]:
    """Top-level rules that write a note for every record (fourth check)."""
    out: list[str] = []
    for i, rule in enumerate(dsl.get("emit", []) or []):
        if not isinstance(rule, dict) or not always_matches(rule.get("when")):
            continue
        if "note" in rule_actions(rule):
            out.append(f"{adapter}: rule[{i}] {rule.get('name', '?')!s}: when="
                       f"{_when_summary(rule.get('when'))} matches every record and "
                       f"its do: holds a note, so every record gets that note")
    return out


def load_checked(path: str) -> bool:
    """True for a shipped adapter, the ones the load check covers: outside
    tests/ and fuzz/ inside adapters/ (a path outside adapters/ counts as
    shipped). The fuzz probes are refused at load by design (ruled
    2026-10-19)."""
    adapters_dir = pathlib.Path(__file__).resolve().parent
    try:
        parts = pathlib.Path(path).resolve().relative_to(adapters_dir).parts
    except ValueError:
        return True
    return "tests" not in parts[:-1] and "fuzz" not in parts[:-1]


def _adapter_name(path: str) -> str:
    return pathlib.Path(path).parent.name + "/" + pathlib.Path(path).name


def discover_adapters() -> list[str]:
    adapters_dir = pathlib.Path(__file__).resolve().parent
    paths = glob(str(adapters_dir / "**" / "*.dsl.yaml"), recursive=True)
    # Exclusions are judged on path segments inside adapters/, so a checkout
    # that lives under e.g. ".../review-x/" does not silently empty the lint
    # gate, and an adapter directory such as "code-review-x/" is still linted.
    # Only a top-level review-* scratch copy is skipped, and that skip is
    # reported: a green lint must not hide an adapter it never read.
    kept: list[str] = []
    for p in paths:
        parts = pathlib.Path(p).relative_to(adapters_dir).parts
        if "fixtures" in parts[:-1]:
            continue
        if parts[0].startswith("review-"):
            print(f"note: not linted (review copy): {parts[0]}/{'/'.join(parts[1:])}",
                  file=sys.stderr)
            continue
        kept.append(p)
    return sorted(kept)


def main(argv: list[str]) -> int:
    paths = argv[1:] or discover_adapters()
    all_findings: list[Finding] = []
    unreadable: list[str] = []
    dead_predicates: list[str] = []
    refused: list[str] = []
    note_floods: list[str] = []
    load_count = 0
    for path in paths:
        try:
            dsl = load_adapter_yaml(pathlib.Path(path).read_text())
        except Exception as e:  # noqa: BLE001 - report, and fail the gate below
            unreadable.append(f"{path}: {e}")
            continue
        if not isinstance(dsl, dict):
            unreadable.append(f"{path}: not a mapping (got {type(dsl).__name__})")
            continue
        if load_checked(path):
            # run()'s own load checks: an adapter it refuses is not linted
            # further, since the checks below assume the shapes it checks.
            load_count += 1
            try:
                refusal = dsl_load_problems(dsl)
            except Exception as e:  # noqa: BLE001 - run() crashes on it too; report, do not abort the lint
                refusal = f"the load check crashed ({type(e).__name__}: {e}); run() fails on this adapter"
            if refusal is not None:
                refused.append(f"{_adapter_name(path)}: {refusal}")
                continue
            note_floods += every_record_notes(dsl, _adapter_name(path))
        all_findings += lint_adapter(dsl, _adapter_name(path))
        dead_predicates += [f"{_adapter_name(path)}: {p}" for p in dsl_predicate_problems(dsl)]
    if all_findings:
        print(f"FAIL: {len(all_findings)} unguarded-emit_turn finding(s):\n")
        for f in all_findings:
            print("  " + repr(f))
        print("\nGuard the emit_turn rule with a field==value (or any_of of them) "
              "that contradicts the competing rule, or merge the two into one rule.")
    if dead_predicates:
        print(f"FAIL: {len(dead_predicates)} predicate(s) outside the §8.1 forms (never matched, or refused at load):\n")
        for p in dead_predicates:
            print("  " + p)
        print("\nUse a shape dsl_runner.predicate_matches evaluates; derive anything "
              "else (lengths, comparisons) as a boolean in the preprocessor.")
    # An adapter lint could not check fails the gate: a green lint must mean
    # every discovered adapter was checked, not that the unreadable ones were
    # skipped.
    if unreadable:
        print(f"FAIL: {len(unreadable)} adapter DSL(s) could not be checked:\n")
        for u in unreadable:
            print("  " + u)
    if refused:
        print(f"FAIL: {len(refused)} shipped adapter DSL(s) refused at load by the runner:\n")
        for r in refused:
            print("  " + r.replace("\n", "\n  "))
    if note_floods:
        print(f"FAIL: {len(note_floods)} shipped rule(s) writing a note for every record:\n")
        for n in note_floods:
            print("  " + n)
        print("\nNarrow the rule's when: to the records the note is about, or "
              "remove the rule.")
    if all_findings or dead_predicates or unreadable or refused or note_floods:
        return 1
    print(f"OK: {len(paths)} adapter DSL(s) clean — no unguarded emit_turn overlap, "
          f"no never-matching predicate, no note on every record; "
          f"{load_count} shipped adapter DSL(s) load.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
