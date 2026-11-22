"""Cross-trace reference resolution for paired-trace adapter outputs.

A trace-pair adapter (e.g. ATBench-Claw → agent-session + judge-session per record)
emits two structurally related traces. The judge-session's `lineage[].target_content_hash`
must reference the agent-session's content_hash, which depends on the agent-session's
final byte content. The hash cannot be known until all per-record processing is done.

This module implements the runner-side post-processing pass with **topological
resolution**:

    1. For each trace, identify its outgoing cross-trace placeholders (references
       to peer traces in the form `${trace:<id>:content_hash}`).
    2. Process traces in dependency order: a trace is ready when all of its
       outgoing references point at traces whose hashes are already known.
    3. For each ready trace: resolve its placeholders using already-computed peer
       hashes, then compute its own content_hash over the now-resolved canonical
       body, then add it to the known-hashes pool.
    4. Repeat until all traces are resolved. A cycle does not abort the set
       (DSL spec rc3 §13.2 R3): before resolution, every placeholder edge
       whose source and target lie in the same cyclic component is omitted,
       with a diagnostic. An unresolvable placeholder (empty id, peer not in
       the set) omits its edge with a diagnostic (R4). A trace whose id is
       not a non-empty string, or is shared with another trace, is not
       emitted (R5).
    5. Stamp each trace's `content_hash` field with its computed hash.

NORMATIVE RULES (DSL spec rc3 §13.2):

    R1 — Hash-after-resolution rule. A trace's `content_hash` MUST be computed
         AFTER its outgoing cross-trace placeholders are resolved. The hash
         covers the resolved canonical body, so a verifier who recomputes the
         hash on the published trace gets the stored value (round-trip property).

    R2 — One-shot back-fill rule. Cross-trace reference back-fill MUST be
         applied exactly once per emission cycle. Runners MUST NOT re-run
         back-fill on already-resolved traces. Re-application would recompute
         hashes over the resolved body containing now-real hashes (not
         placeholders), producing different values — this is silent drift.

    R3 — Acyclic graph rule. Cross-trace placeholder graphs MUST be acyclic.
         A → B → A cycles cannot be resolved as they are. Ids come from source
         data, so a cycle is removed instead of rejected: every placeholder
         edge inside a cyclic component (a strongly connected component of
         more than one trace, or a trace with an edge to itself) is omitted,
         edges leaving a component are kept. No trace is ranked, so no id
         order decides which edge is lost; a trace can still remove the
         in-cycle edges of any trace it forms a cycle with, and every
         omission is reported.

    R4 — Unresolvable placeholders degrade: a target_trace_id that is not a
         string equal to the placeholder's trace-id, an empty trace-id or a
         peer not in the emitted set omits the edge with a diagnostic; never
         a null hash.

    R5 — Unusable and duplicate trace ids degrade: a trace whose id is not a
         non-empty string is not emitted, and when several traces share an id
         none of them is emitted, each with a diagnostic. Keeping the first
         would let an earlier record shadow the genuine trace.

The post-processing pass is order-dependent: a trace's hash is committed at the
moment its placeholders resolve and its canonical body stabilises. If a peer
trace is mutated after hash computation, the existing back-filled reference
becomes invalidated — by design, matching minitrace-spec §3.4's rule that
"Additions or redactions applied after hash computation invalidate the hash
and require recomputation."

Canonicalisation:
    RFC 8785 (JSON Canonicalization Scheme), via the `rfc8785` library.
    minitrace-spec v0.3.0 §3.4 pins JCS so the canonical form is byte-identical
    across conformant implementations and languages; an independently-implemented
    (e.g. Rust) verifier reproduces the same content_hash. Earlier reference
    revisions used json.dumps(sort_keys, tight separators, ensure_ascii=False),
    which is NOT JCS and diverges on number formatting and non-BMP key ordering.

Status: step-1 primitive per ATBench-Claw scoping. Designed to be
standalone-testable (no DSL runner coupling) so that step 2's DSL changes can
target a known-good primitive.

Revision history:
    2026-05-04 v1: initial implementation. Algorithm: hash-before-resolution.
                   BUG: judge.content_hash did not round-trip (verifier seeing
                   resolved body computed a different hash than the stored one).
    2026-05-04 v2: corrected to hash-after-resolution with topological ordering.
                   Round-trip test added. Cyclic-graph detection added.
    2026-05-07 v3: scoped placeholder scan to structural fields only
                   (STRUCTURAL_PLACEHOLDER_FIELDS). Surfaced by the claude-code
                   adapter smoke test against a self-referential corpus: when
                   the agent operating the harness discusses spec syntax, the
                   ${trace:...} pattern leaks into turn.content via legitimate
                   text capture. Free-text fields are not valid lineage homes,
                   so scanning them produced false-positive 'unknown trace id'
                   errors. R4 below codifies the scope.
    2026-05-27 v4: canonicalisation swapped from json.dumps to RFC 8785 (JCS)
                   via the `rfc8785` library, after minitrace-spec §3.4 dropped
                   "or equivalent deterministic form" and pinned JCS. Restores
                   reference-impl conformance to the spec; makes content_hash
                   reproducible by independently-implemented (e.g. Rust) verifiers.
    2026-09-25 v5: DSL spec RC 3 §13.1. A placeholder is the WHOLE value of
                   lineage[].target_content_hash, grammar
                   ^\\$\\{trace:(.+):([a-z_]+)\\}$ (id up to the last ':'), matched
                   with fullmatch; substitution replaces the whole value. v3/R4
                   scanned every string under `lineage` for substrings, so text
                   in a lineage extension field would have been rewritten, and
                   the id grammar [^:}]+ skipped ids containing ':' or '}'
                   silently. A whole-value placeholder naming a field other
                   than content_hash is rejected, not ignored.
    2026-10-03 v6: DSL spec rc3 §13.2. R3 omits every placeholder edge inside
                   a cyclic component in one pass before resolution (it used
                   to drop the smallest id's edges, which let data that builds
                   a cycle choose the losing trace). R5: a missing, empty,
                   non-string or shared trace id drops that trace (all traces
                   sharing the id) with a diagnostic instead of aborting the set.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

import rfc8785

# Whole-value grammar (DSL spec RC 3 §13.1). Use with fullmatch only: `$` in
# Python also matches before a trailing newline. DOTALL so the id may contain
# any character, as the spec states; the id runs to the last ':'.
PLACEHOLDER_PATTERN = re.compile(r"\$\{trace:(.+):([a-z_]+)\}", re.DOTALL)

# R4 (v5) — the placeholder-bearing field is fixed by the spec, not by adapters:
# `target_content_hash` of each `lineage[]` edge. Placeholder-shaped text in any
# other field (turn content, annotation text, tool_call arguments, lineage
# extension fields) is content: never resolved, never an error. A session that
# discusses the format quotes the syntax; that is data, not a reference.
BEARING_FIELD = ("lineage", "target_content_hash")


def placeholder_target(value: Any) -> str | None:
    """The referenced trace id if `value` is a whole-value content_hash
    placeholder, else None. A whole-value placeholder naming any other field
    raises: RC 3 defines only content_hash."""
    if not isinstance(value, str):
        return None
    m = PLACEHOLDER_PATTERN.fullmatch(value)
    if m is None:
        return None
    if m.group(2) != "content_hash":
        raise ValueError(f"placeholder names unspecified field {m.group(2)!r} (only content_hash is defined)")
    return m.group(1)


def canonicalize_for_hash(trace: dict) -> bytes:
    """Return the RFC 8785 (JCS) canonical bytes used for content_hash computation.

    Per minitrace-spec v0.3.0 §3.4: SHA-256 over the JCS canonical form of the
    trace EXCLUDING the content_hash field itself. JCS output is UTF-8 bytes.
    """
    body = {k: v for k, v in trace.items() if k != "content_hash"}
    return rfc8785.dumps(body)


def compute_content_hash(trace: dict) -> str:
    """Return the trace's content_hash in `sha256:<hex>` form."""
    canonical = canonicalize_for_hash(trace)  # JCS bytes
    digest = hashlib.sha256(canonical).hexdigest()
    return f"sha256:{digest}"


def _set_at_path(node: Any, path: tuple[str | int, ...], value: Any) -> None:
    cursor = node
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value


_WELLFORMED_HASH = re.compile(r"sha256:[0-9a-f]{64}")


def _bearing_field_problems(trace: dict) -> list[str]:
    """RC 3 §13.1, producer side, mirroring minitrace-verify: a present
    `lineage` is a list of edge objects, and every non-null
    `target_content_hash` is a whole-value placeholder or a well-formed hash.
    Stamping anything else would publish a trace the verifier rejects.
    A placeholder whose trace-id is empty or malformed is not a problem here:
    R4 (§13.2) omits its edge instead (see _malformed_placeholder)."""
    edges = trace.get(BEARING_FIELD[0])
    if edges is None:
        return []
    if not isinstance(edges, list):
        return [f"lineage is not a list (got {type(edges).__name__})"]
    out = []
    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            out.append(f"lineage[{i}] is not an object (got {type(edge).__name__})")
            continue
        value = edge.get(BEARING_FIELD[1])
        if value is None or _malformed_placeholder(value):
            continue
        if not isinstance(value, str):
            out.append(f"lineage[{i}].target_content_hash is not a string")
        elif not (_WELLFORMED_HASH.fullmatch(value) or PLACEHOLDER_PATTERN.fullmatch(value)):
            out.append(f"lineage[{i}].target_content_hash is neither a well-formed hash "
                       f"nor a placeholder: {value[:80]!r}")
        # A placeholder edge whose target_trace_id is not its trace-id never
        # reaches here: R4 omits it first (_target_mismatch).
    return out


_EMPTY_ID_PLACEHOLDER = re.compile(r"\$\{trace::[a-z_]+\}")


def _malformed_placeholder(value: Any) -> bool:
    """The DSL-written frame intact but the trace-id empty: ${trace::content_hash},
    which is what a null id renders to (§8.3 interpolates null as ""). Data
    reaches only the id portion, so this is the input-made case R4 omits; a
    broken frame (trailing text, a prefix) is a DSL defect and still fails."""
    return isinstance(value, str) and _EMPTY_ID_PLACEHOLDER.fullmatch(value) is not None


def _placeholders_in_trace(trace: dict) -> list[tuple[tuple, str, str]]:
    """Return (path_in_trace, value, referenced_trace_id) for each whole-value
    placeholder in the bearing field `lineage[i].target_content_hash` (R4 v5).
    The path is rooted at the trace, so _set_at_path resolves correctly."""
    out = []
    edges = trace.get(BEARING_FIELD[0])
    if not isinstance(edges, list):
        return out
    for i, edge in enumerate(edges):
        if not isinstance(edge, dict):
            continue
        value = edge.get(BEARING_FIELD[1])
        target = placeholder_target(value)
        if target is not None:
            out.append(((BEARING_FIELD[0], i, BEARING_FIELD[1]), value, target))
    return out


def find_placeholders(traces: list[dict]) -> list[tuple[int, tuple, str, str]]:
    """List every cross-trace placeholder in the bearing field of each trace.

    Returns list of (trace_index, path_in_that_trace, placeholder_value, referenced_trace_id).
    """
    return [(i, path, value, target)
            for i, t in enumerate(traces)
            for path, value, target in _placeholders_in_trace(t)]


def _short(v: Any, limit: int = 80) -> str:
    """An id for a diagnostic: ids are source data, so the echo is bounded."""
    r = repr(v)
    return r if len(r) <= limit else r[:limit] + f"...({len(r)} chars)"


def _id_order(v: Any) -> tuple[str, str]:
    """Total order over ids of any type (mixed str/int ids made sorted()
    raise). After R5 every id is a string; the order only fixes traversal
    and diagnostic order, it decides no edge."""
    return (type(v).__name__, v if isinstance(v, str) else repr(v))


def drop_unusable_and_duplicate_ids(traces: list[dict],
                                    diagnostics: list[dict],
                                    built_ids: list = ()) -> list[dict]:
    """DSL spec rc3 §13.2 R5, single- and multi-trace mode alike. Returns the
    traces that may be emitted: each id is a non-empty string held by no
    other trace. Every dropped trace is reported; nothing is converted to a
    string and nothing aborts. `built_ids` are the ids of traces built but
    already not emitted (for their turn index, DSL §4 rule 1, or as not
    I-JSON): R5 decides on the traces as built, so they count for the
    unique-id rule (2026-10-05) and a trace sharing one is not
    emitted either."""
    usable = []
    for t in traces:
        tid = t.get("id") if isinstance(t, dict) else None
        if isinstance(tid, str) and tid:
            usable.append(t)
        else:
            diagnostics.append({
                "level": "warning", "kind": "trace-dropped",
                "message": f"trace id {_short(tid)} is not a non-empty string; trace not emitted (R5)"})
    counts: dict[str, int] = {}
    for tid in [t["id"] for t in usable] + [i for i in built_ids if isinstance(i, str) and i]:
        counts[tid] = counts.get(tid, 0) + 1
    kept = []
    for t in usable:
        n = counts[t["id"]]
        if n == 1:
            kept.append(t)
        elif n > 0:
            diagnostics.append({
                "level": "warning", "kind": "trace-dropped",
                "message": f"trace id {_short(t['id'])} is shared by {n} traces; none emitted (R5)"})
            counts[t["id"]] = -n  # reported once per id
    return kept


def _omit_edges(trace: dict, drop, reason: str, diagnostics: list[dict]) -> None:
    """Remove the lineage edges for which drop(edge) is true, reporting each."""
    edges = trace.get(BEARING_FIELD[0])
    if not isinstance(edges, list):
        return
    kept = []
    for edge in edges:
        if isinstance(edge, dict) and drop(edge):
            named = (_short(edge["target_trace_id"]) if "target_trace_id" in edge
                     else "(no target_trace_id)")
            referenced = placeholder_target(edge.get(BEARING_FIELD[1]))
            binds = (f", placeholder references {_short(referenced)}"
                     if referenced is not None and referenced != edge.get("target_trace_id")
                     else "")
            diagnostics.append({
                "level": "warning", "kind": "lineage-edge-omitted",
                "message": f"trace {_short(trace.get('id'))}: lineage edge to "
                           f"{named} omitted ({reason}{binds})"})
        else:
            kept.append(edge)
    trace[BEARING_FIELD[0]] = kept


def _target_mismatch(edge: dict) -> bool:
    """R4 (§13.2): a placeholder edge resolves only when its target_trace_id is
    a string equal to the placeholder's trace-id. One that differs, is null,
    absent or not a string names one trace while binding another's hash (or
    none). An empty-id placeholder is left to _malformed_placeholder."""
    value = edge.get(BEARING_FIELD[1])
    referenced = placeholder_target(value)
    if referenced is None or _malformed_placeholder(value):
        return False
    return edge.get("target_trace_id") != referenced


def _strongly_connected(nodes: list[str], deps: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative, over sorted nodes and sorted successors,
    so the result does not depend on dict or set iteration order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    node_set = set(nodes)   # built once; per-visit rebuilds made cycles cubic
    for root in sorted(nodes, key=_id_order):
        if root in index:
            continue
        work = [(root, iter(sorted(deps.get(root, set()) & node_set, key=_id_order)))]
        index[root] = low[root] = counter; counter += 1
        stack.append(root); on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter; counter += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, iter(sorted(deps.get(w, set()) & node_set, key=_id_order))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop(); on_stack.discard(w); comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp, key=_id_order))
    return out


def back_fill_cross_trace_references(traces: list[dict],
                                     diagnostics: list[dict] | None = None,
                                     built_ids: list = ()) -> list[dict]:
    """Resolve `${trace:<id>:content_hash}` placeholders across a paired trace set.

    Algorithm (topological, hash-after-resolution per R1, DSL spec rc3 §13):

        1. R5: drop (and report) every trace whose id is not a non-empty
           string, and every trace whose id another trace shares.
        2. R4: omit (and report) every edge whose target_trace_id is not a
           string equal to its placeholder's trace-id, whose placeholder is
           malformed, or that names a trace not in the set; these never abort
           the set. The producer-side lineage checks run before, on the
           lineage as built.
        3. R3: in each cyclic component (a strongly connected component of
           more than one trace, or a trace with an edge to itself), omit (and
           report) every placeholder edge whose target lies in the same
           component. One pass leaves an acyclic graph; no trace is ranked.
        4. Iterate in topological order: a trace is *ready* when all peers it
           references are resolved; replace each placeholder with the peer
           hash, THEN compute the trace's own content_hash (R1).
        5. Stamp each trace's `content_hash` field with its computed hash.

    Round-trip property: after back-fill, recomputing `compute_content_hash(t)`
    on any trace MUST equal `t['content_hash']`.

    Returns the traces that may be emitted, in input order. Their dicts are
    mutated in place; a trace dropped under R5 is left untouched and is not
    in the result. Omissions are appended to `diagnostics` when given, else
    written to stderr.
    """
    import sys  # noqa: PLC0415
    diags: list[dict] = [] if diagnostics is None else diagnostics
    kept = drop_unusable_and_duplicate_ids(traces, diags, built_ids)
    by_id: dict[str, dict] = {t["id"]: t for t in kept}

    # The producer-side checks run on the lineage as built, before any
    # omission, so their abort names the item's own index and carries no
    # omission. (A placeholder naming a field other than content_hash still
    # aborts later, from placeholder_target in the R4 pass.) They do not look
    # at target_trace_id, so no edge R4 omits can abort the set here.
    for tid, t in by_id.items():
        problems = _bearing_field_problems(t)
        if problems:
            raise ValueError(f"trace `{_short(tid)}`: " + "; ".join(problems))

    # R4: a target_trace_id that differs from the placeholder's trace-id, or
    # is null, absent or not a string (ruled 2026-10-27; a differing one used
    # to abort the set); malformed placeholders; references to traces not in
    # the set.
    for tid in sorted(by_id, key=_id_order):
        t = by_id[tid]
        _omit_edges(t, _target_mismatch,
                    "target_trace_id not the placeholder's trace-id, R4", diags)
        _omit_edges(t, lambda e: _malformed_placeholder(e.get(BEARING_FIELD[1])),
                    "malformed placeholder, R4", diags)
        _omit_edges(t, lambda e: (placeholder_target(e.get(BEARING_FIELD[1])) or "") != ""
                    and placeholder_target(e.get(BEARING_FIELD[1])) not in by_id,
                    "referenced trace not in the emitted set, R4", diags)

    # Dependencies computed once and refreshed only for a trace whose edges
    # change: rescanning every trace on every pass made long chains quadratic
    # in edges and cycles worse.
    deps = {tid: {ref for _, _, ref in _placeholders_in_trace(t)} for tid, t in by_id.items()}

    # R3, one pass: the edges left after it only join different components.
    for comp in _strongly_connected(sorted(by_id, key=_id_order), deps):
        if len(comp) == 1 and comp[0] not in deps[comp[0]]:
            continue  # not cyclic
        members = set(comp)
        for tid in comp:
            _omit_edges(by_id[tid],
                        lambda e: placeholder_target(e.get(BEARING_FIELD[1])) in members,
                        "inside a reference cycle, R3", diags)
            deps[tid] = {ref for _, _, ref in _placeholders_in_trace(by_id[tid])}

    resolved: dict[str, str] = {}  # tid -> sha256 hash
    remaining = set(by_id.keys())
    while remaining:
        ready = sorted((tid for tid in remaining if deps[tid] <= resolved.keys()), key=_id_order)
        if not ready:
            raise RuntimeError("cross-trace graph still cyclic after R3 (internal error)")
        for tid in ready:
            t = by_id[tid]
            # the placeholder is the field's whole value, so it is replaced whole
            for path, _value, referenced_id in _placeholders_in_trace(t):
                _set_at_path(t, path, resolved[referenced_id])
            resolved[tid] = compute_content_hash(t)  # R1
            remaining.discard(tid)

    for tid, t in by_id.items():
        t["content_hash"] = resolved[tid]

    if diagnostics is None:
        for d in diags:
            sys.stderr.write(f"  [{d['level']}] {d['message']}\n")
    return kept


def make_cross_trace_reference(target_trace_id: str) -> str:
    """Helper for adapter authors: construct the placeholder string."""
    return f"${{trace:{target_trace_id}:content_hash}}"
