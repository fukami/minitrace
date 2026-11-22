# Drift ledger

A per-adapter, machine-readable record of the Axis-C drifts the drift watch has
surfaced and triaged. It is the dissector-maintenance discipline written down:
each documented format transition an adapter carries across harness versions is
recorded with a verdict, so a known, triaged drift is dismissed without the watch
going blind to a genuinely new shape.

## Why

The drift watch (`adapters/drift_core.py`, wired per harness in
`adapters/<h>/regression/drift_watch.py`) compares two frozen rungs and gates
(exit 1) on structural drift. Most drift is not adapter-breakage: a harness
denormalizes a column, adds a redundant serialization, or bumps an in-band
version. Without a ledger, every such drift re-fires FATAL on every run, and the
signal that matters — a genuinely new trace-content shape — drowns. The ledger
records the triage once. A drift a ledger entry covers downgrades to an
acknowledged line; a drift no entry covers still gates.

An adapter supports a version *range* (e.g. opencode dispatches 1.2.20 and
1.14.40), and the ledger is that range's changelog: read end to end it is a
reverse-engineered history of how the harness's native format evolved, as seen
through the adapter.

## File

`adapters/<h>/regression/drift-ledger.yaml`. A `DriftConfig(ledger=...)` points
the watch at it; a missing file means no ledger (every drift gates, unchanged).

```yaml
entries:
  - id: OC-0001                       # <ADAPTER>-<seq>, stable handle
    introduced_at: { from: "1.2.20", to: "1.14.40" }
    signal: new_unmapped              # the drift_core signal class (below)
    matches:                          # the concrete surface this entry covers
      record_types: [session.created.1, message.part.updated.1]
      # items: [ "<recordtype>::<path>", ... ]   # exact-item fallback
      # path_prefixes: [ "<prefix>", ... ]        # prefix match on the item
    verdict: benign_redundant         # taxonomy below
    disposition: dismissed            # dismissed | fix_deferred | escalated
    rationale: >
      Why this verdict, in enough detail that a later reader can re-confirm it.
    confirmed_by: payload-inspection
    date: "2026-07-23"
    followup: null                    # a backlog note when disposition = fix_deferred
```

### Matching

For each drift item the watch raises, an entry covers it when the entry's
`signal` matches the item's signal class AND one of:

- the item's **record type** (the part before `::`, or the bare type for
  `new_record_type` / `dark_cell`) is in `matches.record_types`, or
- the full item string is in `matches.items`, or
- the item string starts with one of `matches.path_prefixes`.

Record-type matching is **exact**. Where a harness versions its record types in
the type string (opencode: `session.created.1`), a bumped or novel type does not
match a listed one and correctly re-fires. Do not normalize away such suffixes.

### Signal classes

The `signal` values map to `drift_core` gates: `new_record_type`, `new_unmapped`,
`dropped_dependency`, `dark_cell`.

## Coverage declaration

Every adapter with regression rungs carries a top-level `coverage:` block in its
`drift-ledger.yaml` — the adapter's version-support claim, from which release
docs (per-adapter version tables) generate:

```yaml
coverage:
  tested: ["0.114.0", "0.132.0", "0.145.0"]   # exactly the regression/<v>/ rung dirs
  policy: point-tested
```

`point-tested` states the claim honestly: **supported = the tested rung
versions**; versions *between* rungs are expected-compatible unless a ledger
entry or changelog note says otherwise. Rungs are points, not ranges — the
ledger's changelog is the evidence for what happened between them.

Invariants (the fleet sweep, `adapters/run_fleet_sweep.py`, gates these):

- `coverage.tested` must equal the `regression/<version>/` rung dir names —
  adding or retiring a rung without updating the claim fails the sweep.
- An adapter with rung dirs but no ledger file (or no `coverage:` block) fails.

The block is ignored by `drift_core.load_ledger` (which reads only `entries`),
so it has no effect on gating. It is implementation metadata, not trace-spec or
envelope-spec surface — nothing about it enters the spec delta lane.

## Taxonomies

**verdict** — what the drift *is*:

| verdict | meaning |
|---|---|
| `benign_redundant` | content variance or a redundant serialization; no new trace-content kind |
| `mapped` | real, but the adapter already handles it (informational) |
| `deferred_fix` | real; the adapter should map/handle it, parked for a fixing pass |
| `spec_candidate` | a new trace-content kind — a §16 first-deployment candidate for the trace spec (AGENTS.md, the §16 convention-absorption rule) |

**disposition** — what we *do* about it (all are non-gating once ledgered):

| disposition | meaning |
|---|---|
| `dismissed` | closed; nothing more to do |
| `fix_deferred` | stays visible every run as backlog; drained at fixing time |
| `escalated` | raised to the spec (§16) pipeline |

## Procedure

1. Run the delta (`drift_watch --baseline <a> --candidate-inputs <b>/inputs --operator-type <type>`).
   Between any two frozen rungs, not only baseline→newest, so a version boundary
   can be studied in isolation.
2. For each drift, assign a `verdict` and `disposition`. Prefer confirming a
   verdict from the actual payloads over trusting a changelog.
3. Write or extend a ledger entry. Key `introduced_at` to the *narrowest* edge
   where the drift is confirmed introduced; a wider delta that also crosses it
   references the entry instead of re-logging.
4. Re-run. Ledgered drifts show as `known-drift <id>` acknowledged lines; only
   un-covered drifts remain red.

`fix_deferred` entries are the fixing-time adapter-work backlog. `spec_candidate`
entries are the trace-spec funnel output. The ledger entries are adapter-level
data with no trace-spec footprint; this format and its taxonomies are adapter
methodology.
