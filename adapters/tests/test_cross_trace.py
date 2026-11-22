"""Standalone test for cross_trace.py — step 1 of the ATBench-Claw adapter work.

Per review guidance (2026-05-04): the test must exercise BOTH the happy
path (back-fill works) AND mismatch detection (mutating one trace post-emission
makes the peer's lineage reference detect the mismatch). The latter is what
proves the integrity primitive actually works rather than just that the data
flows.

Synthetic record: one minimal agent-session trace + one minimal judge-session
trace with `lineage[0].target_content_hash` placeholder pointing at the
agent-session.

Run:
    python -m pytest adapters/tests/test_cross_trace.py -v
or:
    python adapters/tests/test_cross_trace.py
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from cross_trace import (  # noqa: E402
    PLACEHOLDER_PATTERN,
    back_fill_cross_trace_references,
    canonicalize_for_hash,
    compute_content_hash,
    make_cross_trace_reference,
)


def _make_synthetic_pair() -> tuple[dict, dict]:
    """Synthetic agent-session + judge-session pair for testing."""
    agent = {
        "schema_version": "0.3.0",
        "trace_type": "agent-session",
        "id": "agent-test-001",
        "operator": {
            "type": "scripted",
            "operator_aware": None,
            "capture_mode": "full",
            "capture_purpose": None,
        },
        "turns": [
            {"index": 0, "role": "user", "content": "test prompt"},
            {"index": 1, "role": "assistant", "content": "test response"},
        ],
        "tool_calls": [],
        "annotations": [],
        "lineage": [],
        "redactions": [],
    }
    judge = {
        "schema_version": "0.3.0",
        "trace_type": "judge-session",
        "id": "judge-test-001",
        "operator": {
            "type": "human",
            "operator_aware": True,
            "capture_mode": "scrubbed",
            "capture_purpose": None,
        },
        "turns": [],
        "tool_calls": [],
        "annotations": [
            {
                "id": "ann-001",
                "timestamp": "2026-05-04T00:00:00.000000Z",
                "annotator": "test-judge",
                "scope": {"type": "trace", "target_id": "agent-test-001"},
                "content": {"verdict": "test"},
                "claim_type": "classification",
            }
        ],
        "lineage": [
            {
                "type": "judges",
                "target_trace_id": "agent-test-001",
                "target_content_hash": make_cross_trace_reference("agent-test-001"),
                "authored_by": "test-judge",
                "authored_at": "2026-05-04T00:00:00.000000Z",
            }
        ],
        "redactions": [],
    }
    return agent, judge


def test_canonicalize_is_deterministic():
    t = {"id": "x", "schema_version": "0.3.0", "turns": [{"index": 0}]}
    a = canonicalize_for_hash(t)
    b = canonicalize_for_hash({"turns": [{"index": 0}], "schema_version": "0.3.0", "id": "x"})
    assert a == b, "canonicalisation must be order-independent"
    assert isinstance(a, bytes), "JCS canonical form is bytes"
    assert b"content_hash" not in a, "content_hash field must be excluded from canonical form"


def test_canonicalize_excludes_content_hash():
    t1 = {"id": "x", "schema_version": "0.3.0", "turns": []}
    t2 = {"id": "x", "schema_version": "0.3.0", "turns": [], "content_hash": "sha256:deadbeef"}
    assert canonicalize_for_hash(t1) == canonicalize_for_hash(t2), \
        "presence of content_hash must not affect canonical form"


def test_compute_content_hash_format():
    h = compute_content_hash({"id": "x", "schema_version": "0.3.0", "turns": []})
    assert h.startswith("sha256:"), "hash must be `sha256:` prefixed"
    assert len(h) == len("sha256:") + 64, "sha256 hex digest must be 64 hex chars"
    assert all(c in "0123456789abcdef" for c in h.split(":")[1]), "must be lowercase hex"


def test_happy_path_back_fill():
    """A trace pair with a placeholder lineage reference resolves correctly."""
    agent, judge = _make_synthetic_pair()
    placeholder = judge["lineage"][0]["target_content_hash"]
    assert PLACEHOLDER_PATTERN.search(placeholder), "test fixture must use placeholder syntax"

    pair = [agent, judge]
    back_fill_cross_trace_references(pair)

    # Both traces must now have content_hash stamped.
    assert agent.get("content_hash", "").startswith("sha256:"), "agent missing content_hash"
    assert judge.get("content_hash", "").startswith("sha256:"), "judge missing content_hash"

    # Judge's lineage target_content_hash must equal the agent's content_hash.
    assert judge["lineage"][0]["target_content_hash"] == agent["content_hash"], \
        "lineage target_content_hash must match agent's computed hash"

    # No placeholders may remain anywhere.
    full = json.dumps([agent, judge])
    assert "${trace:" not in full, "all placeholders must be resolved"


def test_mismatch_detection_after_post_emission_mutation():
    """If we mutate the agent-session after emission, recomputing its hash must
    differ from the back-filled reference in the judge — which IS the integrity
    contract per spec §3.4: 'Additions or redactions applied after hash computation
    invalidate the hash and require recomputation.'"""
    agent, judge = _make_synthetic_pair()
    pair = [agent, judge]
    back_fill_cross_trace_references(pair)

    committed_agent_hash = agent["content_hash"]
    committed_judge_lineage_target = judge["lineage"][0]["target_content_hash"]
    assert committed_agent_hash == committed_judge_lineage_target

    # Mutate agent post-emission: append a turn.
    agent["turns"].append({"index": 2, "role": "user", "content": "added later"})

    # Recompute agent hash from current content.
    new_hash = compute_content_hash(agent)

    # The committed reference in judge's lineage points at the OLD agent state.
    # The new agent hash MUST differ.
    assert new_hash != committed_judge_lineage_target, \
        "mutating agent must produce a different hash than the committed reference"
    assert new_hash != committed_agent_hash, \
        "agent's stamped content_hash is now stale"

    # Verifier-side check: a consumer recomputing the agent hash and comparing
    # to judge's lineage target_content_hash should see a mismatch.
    verifier_observed = compute_content_hash(agent)
    assert verifier_observed != judge["lineage"][0]["target_content_hash"], \
        "verifier must detect the mutation as a hash mismatch"


def test_verifier_round_trip():
    """R1 — Hash-after-resolution.

    After back-fill, recomputing each trace's content_hash on the resolved
    canonical body MUST equal the stored content_hash. This is the verifier-side
    integrity check: a downstream consumer canonicalising the published trace
    and computing SHA-256 must get the same value the producer stamped.

    THIS TEST CAUGHT THE STEP-1 v1 BUG (hash-before-resolution): the v1
    implementation hashed the unresolved body; the verifier saw the resolved
    body; the round-trip failed for traces with outgoing placeholders.
    """
    agent, judge = _make_synthetic_pair()
    pair = [agent, judge]
    back_fill_cross_trace_references(pair)

    for t in pair:
        recomputed = compute_content_hash(t)
        assert t["content_hash"] == recomputed, \
            f"trace {t['id']}: stored content_hash {t['content_hash']} != recomputed {recomputed}"


def test_one_shot_back_fill_warning():
    """R2 — One-shot back-fill rule.

    Re-applying back_fill_cross_trace_references on already-resolved traces
    SHOULD NOT silently produce different hashes. The contract is one-shot.
    (The current implementation does NOT detect this — it would re-resolve
    a no-op resolution and recompute hashes over the unchanged body, which
    happens to give the same result. But if a runner inadvertently re-runs
    this on traces whose hashes were committed under different rules, drift
    could occur. R2 is therefore a runner-behaviour contract, not enforced
    by this primitive in isolation. Test documents the expected runner
    discipline.)"""
    agent, judge = _make_synthetic_pair()
    pair = [agent, judge]
    back_fill_cross_trace_references(pair)
    first_pass = copy.deepcopy(pair)

    # Re-run. After resolution there are no placeholders; the algorithm should
    # be effectively a no-op for content_hash computation (since the body
    # hasn't changed and there are no outgoing placeholders to resolve).
    back_fill_cross_trace_references(pair)
    for i, (a, b) in enumerate(zip(first_pass, pair)):
        assert a["content_hash"] == b["content_hash"], \
            f"trace[{i}]: hash drifted on re-application ({a['content_hash']} → {b['content_hash']})"


def test_cyclic_reference_breaks_deterministically():
    """R3 — Acyclic graph rule (DSL spec rc3 §13.2).

    A cycle (A → B → A) cannot be resolved as is. Since ids can come from
    data, the set is not aborted: every edge inside the cycle is omitted and
    reported, so neither trace keeps an edge into its own cycle and no id
    decides which one loses."""
    a = {
        "schema_version": "0.3.0", "trace_type": "x-cyclic",
        "id": "trace-a", "operator": {"type": "scripted"}, "turns": [],
        "lineage": [{
            "type": "x-mutual",
            "target_trace_id": "trace-b",
            "target_content_hash": make_cross_trace_reference("trace-b"),
        }],
    }
    b = {
        "schema_version": "0.3.0", "trace_type": "x-cyclic",
        "id": "trace-b", "operator": {"type": "scripted"}, "turns": [],
        "lineage": [{
            "type": "x-mutual",
            "target_trace_id": "trace-a",
            "target_content_hash": make_cross_trace_reference("trace-a"),
        }],
    }
    import copy
    a2, b2 = copy.deepcopy(a), copy.deepcopy(b)
    diags = []
    back_fill_cross_trace_references([a, b], diags)
    assert a["lineage"] == [] and b["lineage"] == [], "both lose their in-cycle edge"
    assert [d["kind"] for d in diags] == ["lineage-edge-omitted"] * 2
    # Order-independent: the same set in the other order gives the same hashes.
    back_fill_cross_trace_references([b2, a2], [])
    assert (a2["content_hash"], b2["content_hash"]) == (a["content_hash"], b["content_hash"])


def test_self_reference_is_a_cycle():
    """R3: a session listed as its own subagent (data-built self-loop)."""
    a = {"schema_version": "0.3.0", "trace_type": "x", "id": "self",
         "operator": {"type": "scripted"}, "turns": [],
         "lineage": [{"type": "x", "target_trace_id": "self",
                      "target_content_hash": make_cross_trace_reference("self")}]}
    diags = []
    back_fill_cross_trace_references([a], diags)
    assert a["lineage"] == [] and a["content_hash"].startswith("sha256:")
    assert len(diags) == 1


def test_three_trace_chain_resolves():
    """A chain A ← B ← C (no cycles, three layers) resolves correctly.

    Demonstrates that topological resolution generalises beyond pairs."""
    a = {
        "schema_version": "0.3.0", "trace_type": "agent-session",
        "id": "agent-A", "operator": {"type": "scripted"},
        "turns": [{"index": 0, "role": "user", "content": "leaf"}], "lineage": [],
    }
    b = {
        "schema_version": "0.3.0", "trace_type": "judge-session",
        "id": "judge-B", "operator": {"type": "human"},
        "turns": [],
        "lineage": [{
            "type": "judges", "target_trace_id": "agent-A",
            "target_content_hash": make_cross_trace_reference("agent-A"),
            "authored_by": "judge-1", "authored_at": "2026-05-04T00:00:00.000000Z",
        }],
    }
    c = {
        "schema_version": "0.3.0", "trace_type": "judge-session",
        "id": "meta-C", "operator": {"type": "human"},
        "turns": [],
        "lineage": [{
            "type": "reviews", "target_trace_id": "judge-B",
            "target_content_hash": make_cross_trace_reference("judge-B"),
            "authored_by": "meta-judge", "authored_at": "2026-05-04T00:00:00.000000Z",
        }],
    }
    triple = [a, b, c]
    back_fill_cross_trace_references(triple)

    # All three round-trip
    for t in triple:
        assert compute_content_hash(t) == t["content_hash"], f"{t['id']} fails round-trip"

    # B's lineage points at A's hash
    assert b["lineage"][0]["target_content_hash"] == a["content_hash"]
    # C's lineage points at B's hash
    assert c["lineage"][0]["target_content_hash"] == b["content_hash"]
    # No remaining placeholders
    assert "${trace:" not in json.dumps(triple)


def test_unknown_placeholder_omits_the_edge():
    agent, _ = _make_synthetic_pair()
    bad = {
        "schema_version": "0.3.0",
        "trace_type": "judge-session",
        "id": "judge-bad",
        "operator": {"type": "scripted"},
        "turns": [],
        "lineage": [
            {
                "type": "judges",
                "target_trace_id": "nonexistent",
                "target_content_hash": make_cross_trace_reference("nonexistent"),
            }
        ],
    }
    # DSL spec rc3 §13.2 R4: a peer missing from the set (e.g. a partial
    # capture) omits that edge with a diagnostic; the set is still emitted.
    diags = []
    back_fill_cross_trace_references([agent, bad], diags)
    assert bad["lineage"] == [] and "content_hash" in bad and "content_hash" in agent
    assert len(diags) == 1 and "nonexistent" in diags[0]["message"]


def test_duplicate_id_drops_every_holder():
    """R5 (DSL spec rc3 §13.2): a shared id emits none of its traces, so an
    earlier record cannot shadow the genuine one; the set is not aborted."""
    a = {"id": "dup", "schema_version": "0.3.0"}
    b = {"id": "dup", "schema_version": "0.3.0"}
    c = {"id": "other", "schema_version": "0.3.0"}
    diags = []
    kept = back_fill_cross_trace_references([a, b, c], diags)
    assert kept == [c] and c["content_hash"].startswith("sha256:")
    assert "content_hash" not in a and "content_hash" not in b
    assert len(diags) == 1 and "'dup'" in diags[0]["message"] and "R5" in diags[0]["message"]


def test_placeholder_in_content_field_ignored():
    """R4 — Structural-fields-only rule. Placeholder-shaped strings in free-text
    fields (turn content, annotation text, tool_call arguments) MUST NOT be
    treated as cross-trace references. Only STRUCTURAL_PLACEHOLDER_FIELDS are
    scanned. Surfaced 2026-05-07 by the claude-code adapter smoke test against
    a self-referential corpus where the agent discusses spec syntax."""
    agent, judge = _make_synthetic_pair()
    # Inject placeholder-shaped strings into free-text fields; these are NOT
    # real lineage references and must not raise "unknown trace id".
    agent["turns"][0]["content"] = (
        "Cross-trace placeholders look like ${trace:agent-X:content_hash} "
        "and ${trace:<trace-id>:content_hash} — example syntax, not real refs."
    )
    judge["annotations"][0]["content"]["text"] = (
        "See ${trace:nonexistent:content_hash} for an example."
    )
    # Real lineage placeholder remains in judge.lineage[0].target_content_hash.
    pair = [agent, judge]
    back_fill_cross_trace_references(pair)
    # The real lineage reference resolved correctly.
    assert judge["lineage"][0]["target_content_hash"] == agent["content_hash"]
    # Free-text placeholders are preserved verbatim (not substituted).
    assert "${trace:agent-X:content_hash}" in agent["turns"][0]["content"]
    assert "${trace:<trace-id>:content_hash}" in agent["turns"][0]["content"]
    assert "${trace:nonexistent:content_hash}" in judge["annotations"][0]["content"]["text"]


def test_placeholder_in_lineage_extension_field_ignored():
    """R4 v5 (DSL spec RC 3 §13.1): the bearing field is target_content_hash
    only. Placeholder-shaped text in another field of a lineage edge is content;
    v3 would have rewritten it (it scanned every string under `lineage`)."""
    agent, judge = _make_synthetic_pair()
    note = "compare ${trace:agent-test-001:content_hash} with the stored value"
    judge["lineage"][0]["x-note"] = note
    back_fill_cross_trace_references([agent, judge])
    assert judge["lineage"][0]["target_content_hash"] == agent["content_hash"]
    assert judge["lineage"][0]["x-note"] == note


def test_id_containing_colon_and_brace_resolves():
    """RC 3 grammar: the id runs to the last ':'. v3's [^:}]+ skipped such ids
    silently, leaving the placeholder unresolved in a stamped trace."""
    agent, judge = _make_synthetic_pair()
    odd = "agent:run}7"
    agent["id"] = odd
    judge["annotations"][0]["scope"]["target_id"] = odd
    judge["lineage"][0]["target_trace_id"] = odd
    judge["lineage"][0]["target_content_hash"] = make_cross_trace_reference(odd)
    back_fill_cross_trace_references([agent, judge])
    assert judge["lineage"][0]["target_content_hash"] == agent["content_hash"]


def test_near_miss_in_bearing_field_raises():
    """RC 3 §13.1: a non-null target_content_hash is a whole-value placeholder or
    a well-formed hash. The producer refuses to stamp anything else, so it never
    publishes a trace the verifier rejects (v3 and a first v5 draft stamped it)."""
    for bad in ("${trace:agent-test-001:content_hash} ",
                "${trace:agent-test-001:content_hash}\n",
                "prefix ${trace:agent-test-001:content_hash}",
                "sha256:zz"):
        agent, judge = _make_synthetic_pair()
        judge["lineage"][0]["target_content_hash"] = bad
        try:
            back_fill_cross_trace_references([agent, judge])
        except ValueError as e:
            assert "neither a well-formed hash" in str(e), (bad, e)
        else:
            raise AssertionError(f"expected ValueError for {bad!r}")
        assert "content_hash" not in judge, "nothing may be stamped"


def test_empty_id_placeholder_omits_the_edge():
    """DSL spec rc3 §13.2 R4: ${trace::content_hash} (a null id rendered as "")
    is unresolvable; the edge is omitted and reported, the set is emitted.
    (Before R4 this was one of the near-misses that aborted the set.)"""
    agent, judge = _make_synthetic_pair()
    judge["lineage"][0]["target_content_hash"] = "${trace::content_hash}"
    diags = []
    back_fill_cross_trace_references([agent, judge], diags)
    assert judge["lineage"] == [] and "content_hash" in judge and "content_hash" in agent
    assert [d["kind"] for d in diags] == ["lineage-edge-omitted"]


def test_malformed_lineage_shape_raises():
    for lineage in ({"target_trace_id": "x"}, [["nested"]], ["${trace:agent-test-001:content_hash}"]):
        agent, judge = _make_synthetic_pair()
        judge["lineage"] = lineage
        try:
            back_fill_cross_trace_references([agent, judge])
        except ValueError as e:
            assert "lineage" in str(e), (lineage, e)
        else:
            raise AssertionError(f"expected ValueError for lineage={lineage!r}")


def test_unspecified_placeholder_field_raises():
    agent, judge = _make_synthetic_pair()
    judge["lineage"][0]["target_content_hash"] = "${trace:agent-test-001:other_field}"
    try:
        back_fill_cross_trace_references([agent, judge])
    except ValueError as e:
        assert "unspecified field" in str(e)
    else:
        raise AssertionError("expected ValueError for an unspecified placeholder field")


def main():
    tests = [
        ("canonicalize is deterministic", test_canonicalize_is_deterministic),
        ("canonicalize excludes content_hash", test_canonicalize_excludes_content_hash),
        ("compute_content_hash format", test_compute_content_hash_format),
        ("happy path back-fill", test_happy_path_back_fill),
        ("mismatch detection after post-emission mutation", test_mismatch_detection_after_post_emission_mutation),
        ("verifier round-trip (R1 — hash-after-resolution)", test_verifier_round_trip),
        ("one-shot back-fill behaves cleanly on re-run (R2)", test_one_shot_back_fill_warning),
        ("cyclic reference broken deterministically (R3, rc3)", test_cyclic_reference_breaks_deterministically),
        ("self-reference is a cycle (R3, rc3)", test_self_reference_is_a_cycle),
        ("three-trace chain resolves topologically", test_three_trace_chain_resolves),
        ("unknown placeholder omits the edge (R4, rc3)", test_unknown_placeholder_omits_the_edge),
        ("duplicate id drops every holder (R5, rc3)", test_duplicate_id_drops_every_holder),
        ("placeholder in content field ignored (R4)", test_placeholder_in_content_field_ignored),
        ("placeholder in lineage extension field ignored (R4 v5)", test_placeholder_in_lineage_extension_field_ignored),
        ("id containing ':' and '}' resolves (RC 3 grammar)", test_id_containing_colon_and_brace_resolves),
        ("near-miss in bearing field raises", test_near_miss_in_bearing_field_raises),
        ("empty-id placeholder omits the edge (R4, rc3)", test_empty_id_placeholder_omits_the_edge),
        ("malformed lineage shape raises", test_malformed_lineage_shape_raises),
        ("unspecified placeholder field raises", test_unspecified_placeholder_field_raises),
        ("placeholder must reference the edge's own target", test_placeholder_must_reference_the_edges_own_target),
        ("lineage shape error names its own index", test_lineage_shape_error_beside_a_mismatched_edge_names_its_own_index),
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def test_placeholder_must_reference_the_edges_own_target():
    """DSL spec rc3 §13.2 R4: a placeholder edge resolves only when its
    target_trace_id is a string equal to the placeholder's trace-id. One that
    differs, is null or is absent is omitted and reported, and the set is
    emitted (ruled 2026-10-27; before, a differing id aborted the set and a
    null or absent one kept the edge). An equal edge beside it is kept."""
    bad_edges = {
        "differs": {"target_trace_id": "agent-A"},
        "differs, empty string": {"target_trace_id": ""},
        "null": {"target_trace_id": None},
        "absent": {},
    }
    for label, named in bad_edges.items():
        a = {"id": "agent-A", "schema_version": "0.3.0", "turns": []}
        b = {"id": "agent-B", "schema_version": "0.3.0", "turns": []}
        bad = {"type": "judges", **named,
               "target_content_hash": make_cross_trace_reference("agent-B")}
        good = {"type": "judges", "target_trace_id": "agent-A",
                "target_content_hash": make_cross_trace_reference("agent-A")}
        j = {"id": "judge", "schema_version": "0.3.0", "turns": [],
             "lineage": [bad, good]}
        diags = []
        back_fill_cross_trace_references([a, b, j], diags)
        assert all("content_hash" in t for t in (a, b, j)), label
        assert j["lineage"] == [good], (label, j["lineage"])
        assert good["target_content_hash"] == a["content_hash"], label
        assert [d["kind"] for d in diags] == ["lineage-edge-omitted"], (label, diags)
        # The reason is the target_trace_id rule, and the message names the
        # trace the placeholder binds.
        assert "not the placeholder's trace-id" in diags[0]["message"], (label, diags)
        assert "agent-B" in diags[0]["message"], (label, diags)
        if label == "absent":
            assert "(no target_trace_id)" in diags[0]["message"], diags
    # Every such edge in one trace is omitted, not only the first.
    a = {"id": "agent-A", "schema_version": "0.3.0", "turns": []}
    b = {"id": "agent-B", "schema_version": "0.3.0", "turns": []}
    good = {"type": "judges", "target_trace_id": "agent-A",
            "target_content_hash": make_cross_trace_reference("agent-A")}
    bad = [{"type": "judges", **named,
            "target_content_hash": make_cross_trace_reference("agent-B")}
           for named in bad_edges.values()]
    j = {"id": "judge", "schema_version": "0.3.0", "turns": [],
         "lineage": [bad[0], good, *bad[1:]]}
    diags = []
    back_fill_cross_trace_references([a, b, j], diags)
    assert j["lineage"] == [good], j["lineage"]
    assert [d["kind"] for d in diags] == ["lineage-edge-omitted"] * len(bad), diags


def test_lineage_shape_error_beside_a_mismatched_edge_names_its_own_index():
    """A lineage the producer-side checks reject aborts the set before any R4
    omission: the error names the item's index in the lineage as built, and an
    aborted set carries no omission diagnostic."""
    import pytest
    a = {"id": "agent-A", "schema_version": "0.3.0", "turns": []}
    j = {"id": "judge", "schema_version": "0.3.0", "turns": [],
         "lineage": [{"type": "judges", "target_trace_id": "Z",
                      "target_content_hash": make_cross_trace_reference("agent-A")}, 3]}
    diags = []
    with pytest.raises(ValueError, match=r"lineage\[1\] is not an object"):
        back_fill_cross_trace_references([a, j], diags)
    assert diags == []


if __name__ == "__main__":
    sys.exit(main())

