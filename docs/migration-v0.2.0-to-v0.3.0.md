# Migrating traces from v0.2.0 to v0.3.0

> **Status: non-normative.** The requirements are in `minitrace-spec`
> v0.3.0, §12.1 (migration) and Appendix E (changes from v0.2.x). This
> guide arranges them as working steps and points to the clause behind
> each one. Where the guide and the specification differ, the
> specification applies.

This guide is for anyone holding traces written against minitrace v0.2.0,
the last public release, and for anyone writing a consumer that reads both
versions. v0.3.0 is defined over v0.2.0 as its baseline: the v0.2.0
specification ships alongside it as `spec/minitrace-spec-v0.2.0.md`, and
its definitions apply to v0.3.0 traces except where v0.3.0 states
otherwise (§1.4).

The companion specifications released with v0.3.0 (adapter DSL, envelope,
scenario) have no v0.2.0 predecessor and need no migration.

## What stays the same

v0.3.0 keeps the v0.2.0 top-level structure. `turns[]` keeps its
`index`-based addressing, `tool_calls[]` stays at top level and keeps
`emitting_turn_index`, and annotations keep `annotator`, `category`,
`taxonomy_mappings` and the `handover` scope. `ToolCall.spawned_agent` is
not deprecated. Every other v0.2.0 field passes through unchanged,
within the exceptions §3.1 lists, which step 3 below applies (§12.1.5,
Appendix E.3).

Preservation goes further than the fields v0.3.0 names. A producer does
not strip members because v0.3.0 does not mention them, and a consumer
re-serialising a trace keeps every member at any depth, including members
it does not recognise and members inside the items of arrays (§3.1).

## Migration steps

Work on a copy. The v0.2.0 input is kept unchanged and the v0.3.0 trace is
the result of the steps below applied to the copy (§12.1.6).

### 1. Reformat the version and add the trace type

- `schema_version` changes from `minitrace-v0.2.0` to `0.3.0` (§12.1.1).
- `trace_type` is added with the value `agent-session`, which is correct
  for any v0.2.0 session trace (§12.1.1, §3.2).

Both changes are mechanical.

### 2. Supply the `operator` object

This is the one step that needs input from the trace owner. v0.2.0 has no
`operator` object, v0.3.0 requires it at top level, and its values cannot
be inferred from session content (§12.1.3, §5).

| Field | When required | Where the value comes from |
|---|---|---|
| `operator.type` | always | The owner should confirm it rather than assume it. `human` is the default and the usual value for an interactive session. |
| `operator.operator_aware` | `type` is `human` | The owner states whether the operator was informed of capture. There is no default, and harness logs cannot answer it (§5.4). |
| `operator.capture_mode` | `type` is `human` or `delegated` | For `human`: `scrubbed` only if the owner accepts it explicitly; `full` with a `capture_purpose`; or, after redaction (§8), the mode that describes the result (`scrubbed`, `pseudonymised` or `reference_only`). For `delegated`: the owner states the mode, and there is no default (§5.2). |
| `operator.capture_purpose` | `type` is `human` and `capture_mode` is `full` | An object with `legal_basis`, `reference` and `authorised_at` (§5.3). For non-human operators it is left absent or null. |

A migration tool that cannot obtain these values refuses to emit a v0.3.0
trace; it does not fill them in (§5.4, §12.1.3). A `human` trace that
contains identifiable operator content is either authorised for `full` or
redacted, and its `capture_mode` then describes what the trace actually
holds.

### 3. Bring values within the v0.3.0 constraints

v0.3.0 enforces three constraints that a v0.2.0 trace may not meet. The
migrating party applies them to the copy (§12.1.6, step 3; Appendix E.1).

- **Timestamps** take the form `YYYY-MM-DDThh:mm:ss.ffffffZ`, exactly six
  fractional digits, UTC, the same precision throughout the trace (§3.5).
  Second-precision timestamps are padded with `.000000`, and
  sub-microsecond values are truncated, not rounded. There are no leap
  seconds: a source leap second, `23:59:60`, is written as
  `23:59:59.999999`. v0.3.0 also requires a timestamp to be
  calendar-valid, with a month, day, hour, minute and second that exist,
  where v0.2.0 required only an ISO 8601 form in UTC; a timestamp that is
  not calendar-valid, other than a source leap second, is not replaced by
  this step (§12.1.6; see What migration does not fix).
- **Tool-call arguments** in `tool_calls[].input.arguments` are an object,
  or null when the source carries no arguments for the call or none that
  can be read as an object. v0.2.0 typed them as an object, with null as
  its default for an object field; v0.3.0 enforces it, so a JSON-encoded
  string is parsed into its object and a string is never kept (§4.8.1).
- **Numbers and JSON form.** A number whose value lies outside
  ±(2^53−1) is carried as a JSON string. The trace is an I-JSON message:
  UTF-8, no duplicate member names, and no unpaired surrogates or
  noncharacters in any string, member names included. Its nesting
  depth is at most 64 (§3.4).

### 4. Optional additions

Nothing else is required. The new top-level arrays (`segment_markers`,
`policy_triggers`, `lineage`, `redactions`) migrate as empty, except
`redactions`, which holds the records (§8.1) of any redaction applied at
migration under step 2 (§5.2, §12.1.3, §12.1.4). `lineage` stays empty
even where the v0.2.0 trace records a spawned agent:
`tool_calls[].spawned_agent` passes through unchanged, and no delegation
edge (§7.1) is added at migration; a consumer can append one to the
migrated trace afterwards (§12.1.5, §3.1). The new annotation
fields `source.type` and `claim_type` are
optional, and `taxonomy_mappings` may be kept, flattened to `taxonomy[]`,
or both (§12.1.2, §6.7.4).

A carried v0.2.0 annotation gets neither `authored_at` nor
`author_signature`: both are set when an annotation is written into a
trace, and a migrating party adds neither (§6.8).

A producer that leaves such an array out instead of writing it empty
produces a trace that validates the same but has a different
`content_hash` (§3.1).

### 5. Compute `content_hash`, if wanted

`content_hash` is optional on a standalone trace (§3.4). If it is set, it
is computed last (§12.1.4): `sha256:` followed by the lowercase hex SHA-256 of the
RFC 8785 serialisation of the trace with `content_hash` removed. Any later
change to the trace requires recomputing it.

It is required on a trace that a lineage edge requiring integrity
references, from the time the edge is written, and on a trace in an
envelope integrity chain (§3.4, §7.1). A trace migrated without it can
have it set later: a consumer re-serialising the trace may set
`content_hash` where it is absent or null (§3.1), as the producer of such
an edge may on the trace it references (§7.1). An envelope that declares
`0.1.1` or later bundles only v0.3.x traces; only one that declares
`0.1.0` can bundle a v0.2.0 trace that has not been migrated (envelope
§12).

### 6. Validate

The JSON Schemas ship in `schemas/`, and `schemas/validate.py` accepts trace
files or directories as arguments:

```sh
python3 schemas/validate.py path/to/trace.minitrace.json
```

The schemas check a subset of the requirements (§13). A trace that passes
them can still be non-conformant, for example through a string-valued
`input.arguments` or a number outside the numeric domain. Steps 2 and 3
are checked against the text, not only against the schemas.

## What migration does not fix

A v0.2.0 trace that carries a value outside a v0.2.0 enumeration that
v0.3.0 keeps closed was already invalid under v0.2.0, and the steps above
change no such value, so the migrated trace is not conformant either
(§12.1.6). The enumerations v0.3.0 opens (`profile`,
`timing.privacy_level`, `coordination.human_attention` and `quality`,
§3.1) are not affected: a value outside their v0.2.0 sets is conformant in
v0.3.0 without any change. One case is
accepted: the value `tool_output` in `tool_calls[].output.content_origin`
is allowed as a legacy value and read as v0.2.0 reads null (§4.8.3). A
v0.2.0 trace missing a minimum field (§3.1), or carrying null in one that
no `remove` redaction recorded in `redactions[]` set to null, is not fixed
either: migration adds none. A redaction does not empty `turns` or
change `profile`: a `field` target MUST NOT name `/turns`, an item of it,
or `/profile`, whatever the operation (§8.2); a turn is redacted by an
`event` target. Nor is a negative integer in a `metrics` key v0.2.0
names: the base schema now gives those integers a minimum of 0 (§3.1), so
such a trace fails validation after migration. Nor is a
`tool_calls[].output.result` that is neither a string nor null: v0.2.0
typed it `string | null`, v0.3.0 keeps `tool_calls` as v0.2.0 defines it
(§3.1), and no migration step changes it, so such a trace was already
outside v0.2.0 and fails the base schema after migration. Nor is a
timestamp that is not calendar-valid, such as one on 30 February or at
hour 25, other than a source leap second: the migration steps do not
replace it, so the migrated trace is not conformant (§12.1.6, Appendix
E.1). No shipped validator checks calendar validity (see the threat
model), so, unlike the two cases before it, such a trace still passes
validation.

## Notes for consumers

A consumer that reads both versions should allow for these differences:

- **`metrics` keys are no longer required.** v0.2.0 required
  `metrics.turn_count` and `metrics.tool_call_count`; v0.3.0 withdraws
  that, and an empty `metrics` object is valid. A consumer does not assume
  that any `metrics` key is present (§3.1).
- **Three provenance and timing fields are no longer required.**
  `provenance.converted_at`, `provenance.converter_version` and
  `timing.duration_seconds` are SHOULD in v0.3.0; a consumer does not
  assume them (§3.1).
- **`profile` is an open value set.** `controlled` and `organic` keep their
  meaning; a trace with any other value carries the Organic-column minimum
  fields; such a trace is conformant, and the specification gives the value
  no meaning (§3.1).
- **`timing.privacy_level` is an open value set.** `full`, `anonymous` and
  `minimal` keep their meaning, and the v0.2.0 rules on which `timing`
  fields are null bind only those three values. A trace with another value
  is conformant, and the specification gives the value no meaning (§3.1).
- **`coordination.human_attention` and `quality` are open value sets.**
  `focused`, `divided` and `unknown`, and the tiers `A` to `D`, keep their
  meaning, and the v0.2.0 tier definitions bind only a trace carrying that
  tier. A trace with another value is conformant, and the specification
  gives the value no meaning (§3.1).
- **`schema_version` is any `0.3.<n>`.** A consumer of the line does not
  reject a trace for the patch revision it declares, and treats members it
  does not recognise as passthrough (§3.1).
- **Timestamps are parsed at any ISO 8601 precision on read.** The
  microsecond rule binds producers only (§3.5).
- **Annotations may carry `taxonomy_mappings`, `taxonomy[]`, or both**, and
  a consumer accepts either form (§12.1.2).
- **Omitted optional arrays read as empty** (§3.1).

The full list of additive and relaxing changes, none of which requires
migration, is in Appendix E.2.
