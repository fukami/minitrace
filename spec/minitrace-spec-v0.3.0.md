# minitrace Specification v0.3.0

**Status:** Release.
**Date:** 2026-10-02 (RC 3); last amended 2026-11-12.
**Predecessor:** v0.3.0 RC 1. v0.3.0 RC 2 is not a predecessor: each of its changes was decided again for RC 3.

---

## 0. Summary

v0.3.0 is an additive layer over the v0.2.x trace shape. The v0.2.x top-level structure (`turns[]`, `tool_calls[]` with `emitting_turn_index`, `flags`, `environment`, `operational_context`, `timing`, `coordination`, `metrics`, and the rest) is preserved unchanged; v0.3.0 adds new top-level fields and extends existing ones.

v0.3.0 establishes:

- A three-layer architecture separating trace format, envelope, and adapters.
- A trace-type discriminator with an open extension registry.
- A first-class `operator` object capturing capture mode, capture purpose, and the operator-aware transparency flag.
- An extended annotation layer (new `source.type`, `claim_type`, `event_range` and `segment` scopes; disagreement preservation).
- Typed trace-of-trace lineage edges (baseline set; others reserved).
- Typed redaction operations with reason codes.
- Anchoring of harm annotation to Article 3(49) AI Act categories rather than a novel taxonomy.
- Near-miss vocabulary at session and trace level (§9.4).
- Segment markers and policy triggers as new top-level event arrays alongside `turns` and `tool_calls`.
- A pinned timestamp precision and content-hash canonicalisation rule.

Breaking changes are minimised. The `operator` object is the only structural addition that requires user input during migration. Every other v0.2.x field passes through unchanged, within the exceptions §3.1 lists. v0.3.0 producers MUST NOT strip unrecognised fields of a v0.2.x source trace, and v0.3.0 consumers MUST preserve every field when re-serialising, recognised or not, at any depth (§3.1); v0.3.0 does not redefine, drop, or restructure any v0.2.x top-level block.

---

## 1. Scope

### 1.1 What minitrace is

minitrace is a session trace format for capturing human-AI coding interactions and adjacent workflows. A minitrace instance is a single session represented as a structured, DuckDB-queryable JSON document.

### 1.2 What minitrace is not

- Not an orchestration format. Manifests, dispatch, capability binding, and operational identity are out of scope.
- Not an evidence bundle format. Integrity chains across multiple artefacts, witness data, and monitoring outputs belong to the **envelope** layer (sibling spec).
- Not a harness-specific format. Mapping from harness-native output (Claude Code, Codex, etc.) to canonical minitrace is performed by **adapters** (sibling spec).

### 1.3 Three-layer architecture

| Layer | Specification | Scope |
|---|---|---|
| Trace | `minitrace-spec` (this document) | Behavioural session content |
| Envelope | `minitrace-envelope-spec` (sibling) | Operational integrity, manifest, evidence |
| Adapter | Per-harness DSL files (sibling) | Mapping from harness output to canonical trace |

A minitrace instance MAY exist independently of an envelope. An envelope always references at least one minitrace instance.

### 1.4 Baseline

v0.3.0 is defined over a baseline. "v0.2.x" in this specification means minitrace-spec v0.2.0, the only published v0.2 release, shipped alongside this specification as `minitrace-spec-v0.2.0.md` (SHA-256 `2d46d69f9e9d6cd9dbdd3373089de433acd0dea390486e700213812024c9ad77`). Its definitions of fields, shapes and value sets apply to v0.3.0 traces except where this specification states otherwise.

---

## 2. Terminology

**Conventions.** The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in BCP 14 (RFC 2119, RFC 8174) when, and only when, they appear in all capitals, as shown here. A trace that does not follow a SHOULD is not non-conformant for that reason alone.

Canonical terms and their relationships.

| Term | Definition |
|---|---|
| Trace | A single minitrace instance (JSON document). Primary term for the artefact. |
| Session | The underlying activity the trace captures. A trace represents a session. |
| Operator | The principal directing the session (human, AI overseer, or scripted). |
| Agent | The AI system being directed by the operator. |
| Turn | An operator-agent exchange. Top-level array entry. |
| Tool call | A tool invocation by the agent. Top-level array entry; linked to its turn via `emitting_turn_index`. |
| Segment marker | A structural event delimiting a span of turns sharing a mode or focus. Top-level array entry. |
| Policy trigger | A runtime signal from harness or in-flight classifier. Top-level array entry. |
| Event | Generic term for any item in `turns[]`, `tool_calls[]`, `segment_markers[]`, or `policy_triggers[]`. |
| Event order | A deterministic ordering across the four event arrays, derived on read per §4.3. Not stored. |
| Annotation | Human-authored or tool-authored metadata attached to a scope. |
| Lineage | Typed relationship between traces. |
| Redaction | Typed removal or transformation of content with an auditable reason. |

---

## 3. Trace Structure

### 3.1 Top-level shape

A minitrace v0.3.0 instance is a JSON document combining the v0.2.x baseline fields with the v0.3.0 additions listed below.

**v0.3.0 additions (top-level fields not present in v0.2.x):**

```json
{
  "trace_type": "agent-session",
  "schema_uri": "<URI>",
  "operator": { ... },
  "segment_markers": [ ... ],
  "policy_triggers": [ ... ],
  "lineage": [ ... ],
  "redactions": [ ... ],
  "content_hash": "sha256:..."
}
```

**v0.2.x fields retained unchanged:** `id`, `schema_version` (value reformatted, see below), `profile`, `scenario_id`, `quality`, `title`, `summary`, `classification`, `provenance`, `flags`, `environment`, `operational_context`, `timing`, `condition`, `coordination`, `handover`, `turns`, `tool_calls` (top-level, with `emitting_turn_index` linking each call to its turn), `outcome`, `annotations`, `metrics`. v0.3.0 does not redefine the shape or semantics of any of these, except the requirement status of `metrics` keys and of the three v0.2.x minimum fields, and the value sets of `profile`, `timing.privacy_level`, `coordination.human_attention` and `quality`, stated below, and the constraints of §3.4 (numeric domain, depth limit, JSON form), §3.5 (timestamp precision, within the scope stated there) and §4.8.1 (the shape of `tool_calls[].input.arguments`, an object or null), and the legacy value `tool_output` that §4.8.3 accepts for `tool_calls[].output.content_origin`; in addition, a migrating party MAY replace an annotation's `taxonomy_mappings` by its flattening (§6.7.4, §12.1.2). New annotation fields (§6) are additive within the existing annotation object. Likewise, the `flags` object is extended additively by §7.5 (`flags.wraps_inner`, REQUIRED on harness-wrap producers); the v0.2.x flag set and its semantics are unchanged.

**`metrics` is an open object.** The keys v0.2.x names carry these types when present (the v0.2.x types; the schema adds a minimum of 0 to the integers), and the schema checks them:

| Keys | Type |
|---|---|
| `turn_count`, `tool_call_count`, `read_count`, `modify_count`, `create_count`, `execute_count`, `delegate_count`, `subagent_count`, `subagent_tool_calls` | integer, at least 0 |
| `total_input_tokens`, `total_output_tokens`, `total_cache_read_tokens`, `total_cache_creation_tokens`, `total_reasoning_tokens`, `total_tool_tokens`, `model_switches`, `unique_models`, `median_response_tokens`, `max_response_tokens` | integer, at least 0, or null |
| `read_ratio`, `time_to_first_action`, `idle_ratio`, `session_cost` | number or null |

No `metrics` key is required: the v0.2.x Required status of `metrics.turn_count` and `metrics.tool_call_count` is withdrawn in v0.3.0, and an empty object is valid. Producers MAY add keys not named here; consumers MUST NOT assume that any `metrics` key is present.

**v0.2.x minimum fields.** A trace whose `trace_type` is `agent-session` MUST carry the fields v0.2.0 requires of every session record (v0.2.0 §2, *Minimum required fields by profile*, with its footnote [3]), the Controlled column applying to a trace whose `profile` is `controlled` and the Organic column to every other trace; footnote [3] requires a controlled trace to identify `environment.model`, `environment.model_version` and `environment.agent_version`. The requirement status v0.2.0 gives these fields is part of the definitions §1.4 applies. The exceptions are every `metrics` key, including the token counts footnote [3] makes Required for controlled traces (no `metrics` key is required, above), and `provenance.converted_at`, `provenance.converter_version` and `timing.duration_seconds`, which a producer SHOULD carry, since a trace produced natively has no conversion to record and a source does not always give the session's duration; consumers MUST NOT assume that `provenance.converted_at`, `provenance.converter_version` or `timing.duration_seconds` is present. A minimum field counts as carried when it is present with a value other than null, since v0.2.0 reads an absent field as null; a field that a `remove` redaction recorded in `redactions[]` has set to null, directly or by setting an object that holds it to null, still counts as carried (§8). `profile` is an open value set, not limited to `x-*` values: `controlled` and `organic` keep their v0.2.0 meaning, and a producer MAY use another value. Other v0.2.0 rules that name a profile bind only a trace carrying that value. The minimum fields of other trace types are those this specification and their type-specific schema state.

**`timing.privacy_level` is an open value set**, not limited to `x-*` values: `full`, `anonymous` and `minimal` keep their v0.2.0 meaning, and a producer MAY use another value. The v0.2.0 rules that tie `timing.started_at`, `timing.ended_at`, `timing.hour_of_day` and `timing.day_of_week` to a privacy level bind only a trace carrying that level. A value other than these three has no meaning this specification defines, and does not take the meaning of an `operator.capture_mode` value of the same spelling (§5.2).

**`coordination.human_attention` and `quality` are open value sets**, not limited to `x-*` values: `focused`, `divided` and `unknown`, and the tiers `A`, `B`, `C` and `D`, keep their v0.2.0 meaning, and a producer MAY use another value. The v0.2.0 definitions of the four tiers bind only a trace carrying that tier. A value other than these for its field has no meaning this specification defines.

Required v0.3.0 additions: `trace_type`, `operator`. `schema_version` (already required in v0.2.x) MUST carry a version of the `0.3` line, `0.3.<n>` with `<n>` a non-negative integer in plain decimal, and producers SHOULD declare the revision whose members they emit (this specification: `0.3.0`). A validator or consumer implementing any revision of the line MUST NOT reject a trace for the `0.3.<n>` the trace declares, for any `<n>`, and a validator checks the trace against the requirements of the revision it implements; a member it does not recognise is passthrough content, whose presence does not affect the verdict (its value remains subject to §3.4), and a consumer re-serialising the trace preserves it (the field preservation requirement below).

`schema_version` follows this specification's version policy. It takes the form and precedence of Semantic Versioning 2.0.0 (`MAJOR.MINOR.PATCH`) and, before `1.0.0`, adds a compatibility promise that Semantic Versioning does not make: between two versions of the form `0.Y.Z`, a revision that changes `Y` (`0.3.0` to `0.4.0`), like the revision to `1.0.0`, MAY contain breaking changes and requires explicit migration, and a revision that changes only `Z` (`0.3.0` to `0.3.1`) MUST be backwards-compatible, additive or editorial only. Such a revision adds no value to any enumeration this specification lists, whether or not that enumeration admits `x-*` values, and no baseline `trace_type`, so promotion from `x-*` waits for a change of `Y`; it removes or relaxes no requirement on traces; and a requirement it adds on traces binds only traces that declare it or a later revision. A trace is held to no requirement of a revision later than the one it declares, so a consumer can rely on a requirement that a revision adds over an earlier one only for a trace that declares that revision or a later one. A trace that conforms to a later revision of the line therefore passes a validator of an earlier one, which may accept a trace that a later revision rejects. From `1.0.0` on, Semantic Versioning 2.0.0 applies as published.

Conditionally required: `schema_uri`, REQUIRED and non-null when `trace_type` is neither `agent-session` nor `judge-session` (an unknown type; see §3.2).

Optional v0.3.0 additions: `segment_markers`, `policy_triggers`, `lineage`, `redactions`, `content_hash`. Omitted optional arrays are equivalent to empty arrays for validation and projection purposes; omitted optional scalars are unset. The equivalence does not extend to `content_hash` (§3.4): an omitted array and an empty one serialise differently, so adding or dropping an empty array changes the hash.

**Field preservation requirement.** v0.3.0 producers MUST NOT strip fields from a v0.2.x source trace simply because v0.3.0 does not name them, at any depth; v0.3.0 consumers MUST preserve, when re-serialising, every member, whether or not they recognise it, at any depth: at the top level, inside objects, and inside the items of arrays such as `turns[]`, `tool_calls[]` and `annotations[]`. Preserving a member keeps its name and its value, except as a redaction recorded in `redactions[]` changes it (§8); recognising a member gives a consumer no licence to drop or rewrite it, and dropping one changes `content_hash` (§3.4). For a consumer re-serialising a trace, only these additions are not a change to the members that contain them: appending an annotation (§6), setting `source.type` or `claim_type` on an annotation that lacks it (§6.2, §6.3), setting `content_hash` (§3.4) on a trace where it is absent or null, appending a lineage edge (§7) and appending a redaction record (§8), creating the array if it is absent. After any other such addition or a recorded redaction, a `content_hash` that is present is recomputed (§3.4). The v0.3.0 specification names a closed set of additions and a closed set of changes to existing fields; everything outside that set is passthrough content whose preservation is part of conformance.

A minimal valid trace has zero-length `turns` only if the trace type explicitly permits it (e.g. `judge-session` operating on a referenced trace).

### 3.2 Trace type discriminator

The `trace_type` field is REQUIRED. It is drawn from an open registry.

| Type | Status | Purpose |
|---|---|---|
| `agent-session` | Baseline v0.3.0 | Agent-driven session (default for v0.2.x migration) |
| `judge-session` | Baseline v0.3.0 | Panel judgment over other traces |
| `x-*` | Experimental | Local extension; never reserved, except the prefixes §10.3 lists |
| `ext:<vendor>:<name>` | Vendor-scoped | Namespaced extension |

Validation rules:

- Known types (`agent-session`, `judge-session`) MUST validate against both the base schema (§13) and the type-specific schema.
- Every other `trace_type` value is an unknown type, whichever types a given validator recognises. A trace of an unknown type MUST carry a non-null `schema_uri` pointing to a type-specific JSON Schema, and MUST validate against the base schema and against the schema at `schema_uri`. Validators MAY fetch and apply the referenced schema; a validator that does not apply it checks the base schema only, and a validator that cannot reach the schema URI MUST accept the trace as valid-but-uninterpreted and emit a warning.
- The base schema covers the shape of all fields in §3.1; type-specific schemas constrain domain-specific extensions.

Anticipated but unspecified in v0.3.0: `analysis-session`, `scenario-session`, `overseer-session`, `replay-session`, `aggregate-session`. These names are RESERVED; experimental use SHOULD go via `x-analysis-session` etc.

### 3.3 Trace identity

Trace `id` MUST be unique within any consumer's working set. Producers SHOULD use high-entropy identifiers (UUIDv4 or UUIDv7 recommended). Reference architectures MAY derive trace ID from filename stem (e.g. `019d376d-0103-7dc3-a96d-650c7c2e1cf7.minitrace.json` → `019d376d-0103-7dc3-a96d-650c7c2e1cf7`). Duplicate IDs in the same consumer context MUST be treated as an error at load time.

Aggregators ingesting traces from multiple producers MAY scope IDs by origin (e.g. `<origin>/<id>`) to avoid statistical collisions across organisations or harnesses. Such scoping is an aggregator concern and does not change the trace's own `id` field; the in-trace `id` remains the unscoped value as authored by the producer.

Trace IDs are identifiers, not access tokens. Access control is the responsibility of the storage and transport layers; spec conformance does not rely on ID secrecy.

### 3.4 Integrity

`content_hash` is the string `sha256:` followed by the 64 lowercase hexadecimal digits of the SHA-256 digest of the RFC 8785 (JCS) serialisation of the trace object with its top-level `content_hash` member removed. No other transformation is applied before hashing: in particular, timestamps are hashed as stored, and §3.5 is what makes their stored form reproducible. The stored serialisation of a trace need not be canonical; a producer computes the hash over the canonical form. Those bytes are reproducible by any RFC 8785 implementation regardless of programming language (subject to the numeric-domain caveat in §14.3), which is the property that lets an independently-implemented verifier reproduce a producer's `content_hash`.

The hash covers the trace as it stands at hash-computation time, including any redactions already applied. Additions other than setting `content_hash` (§3.1), and redactions, applied after hash computation invalidate the hash and require recomputation. Consumers verifying a hash MUST use the canonical content as redacted at hash-computation time, not the unredacted state. Cross-trace integrity (an edge from one trace to another) follows the same rule: see §7.1 for how `target_content_hash` references a specific historical state of the referenced trace.

| Context | Requirement |
|---|---|
| Standalone trace | Hash OPTIONAL |
| Trace referenced by a lineage edge requiring integrity (§7.1) | Hash REQUIRED on the referenced trace at the time the reference is authored |
| Trace included in an envelope integrity chain | Hash REQUIRED (envelope spec governs) |

A trace without a hash MAY be referenced by edge types that do not require integrity; edges requiring integrity MUST NOT be created without the referenced trace having a hash.

**Numeric domain.** A JSON number in a trace denotes the IEEE 754 binary64 value nearest to it, which is the numeric model RFC 8785 canonicalises. A number whose binary64 value lies outside the inclusive range [−(2^53−1), 2^53−1] MUST be carried as a JSON string, in whatever notation it is written (`9007199254740992`, `9007199254740992.0`, `1e16` and `1e300` alike); a trace carrying one as a JSON number is not conformant. This includes a number whose nearest binary64 value is infinite (for example `1e999`); NaN and ±Infinity are not JSON. The rule is decided by value, not by spelling, so canonicalising a conformant trace keeps it conformant. Wherever this specification or its schemas require an integer, a number whose binary64 value is a whole number is one, whatever its notation (`2`, `2.0` and `2e0` alike); a boolean is never a number. Negative zero is permitted and canonicalises to `0` (RFC 8785 §3.2.2.3), so `-0` and `0` yield the same `content_hash`. Canonicalisation vectors for the numeric domain ship as `adapters/tests/vectors/canonical-vectors.json`.

**JSON form.** A trace MUST be an I-JSON message (RFC 7493): among other things it is UTF-8, no object has two members with the same name, and no string, member name included, contains an unpaired surrogate or a noncharacter. Input that breaks these rules has no single canonical form, and implementations disagree on it: one keeps the last duplicate, another refuses.

**Nesting depth.** The depth of a JSON value is 0 for a scalar and, for an array or object, one more than the deepest of its members, so `[]` and `{}` have depth 1 and `{"a": [1]}` has depth 2. A trace MUST have depth at most 64; a trace with greater depth is not conformant. Consumers and verifiers MUST NOT reject a trace for its depth when the depth is at most 64. The limit keeps canonicalisation and verification within the stack of common parsers (one widely used Rust parser refuses input at about 128 levels by default) and lies far above the depth of real session data.

### 3.5 Timestamp normalisation

All timestamp fields in this specification are ISO 8601 strings in UTC with the `Z` suffix. Their form, the §3.5 form, is the RFC 3339 `date-time` restricted to `YYYY-MM-DDThh:mm:ss.ffffffZ`: a four-digit year, two-digit month, day, hour, minute and second, exactly six fractional digits and an uppercase `Z`, with no offset and no other ISO 8601 form (the base schema's timestamp pattern). A timestamp MUST be calendar-valid in the proleptic Gregorian calendar: month `01` to `12`, a day that exists in that month and year, hour `00` to `23`, minute and second `00` to `59`. There are no leap seconds: a source leap second is written as `23:59:59.999999`, the last representable microsecond of that minute, moving toward the earlier instant as the truncation rule below does. Timestamps in the §3.5 form are compared by their bytes, which for valid values is also the order of the instants they denote (§4.3).

ISO 8601 permits arbitrary fractional-second precision. RFC 8785 canonicalises JSON structure but does not normalise timestamp string representation, so two producers serialising the same instant at different precisions yield different bytes for the same logical content. To make hashing and the byte-identical event order in §4.3 reproducible across implementations, this specification pins fractional-second precision.

- Producers MUST emit timestamps with **microsecond** precision (six fractional digits), e.g. `2026-05-03T10:00:00.123456Z`.
- Producers MUST use the same precision for every timestamp within a single trace.
- Sub-microsecond source clocks (e.g. Go `time.Now()` with nanosecond resolution) MUST be truncated, not rounded, before serialisation.
- Producers without sub-second source clocks MAY pad with zeros (`.000000Z`) but MUST NOT omit the fractional component for v0.3.0 traces.
- Consumers MUST parse any ISO 8601 fractional precision on read; the producer-side rule constrains emission only.

#### 3.5.1 Timestamp-fallback discipline

All ISO 8601 timestamp fields in this specification MUST carry a non-null value when the schema declares them required (e.g. `Annotation.timestamp`, `Redaction.authorised_at`, `PolicyTrigger.timestamp`, lineage edge `authored_at` for `judges` / `reviews` / `audits` types, §7.2). Producers MUST supply a timestamp at microsecond precision per §3.5.

When source data lacks per-event temporal anchors (e.g. corpora that record event order but not wall-clock time), producers SHOULD use a deterministic fallback timestamp and document the fallback choice in `provenance` or in adapter documentation. Recommended fallback strategies:

1. **Trace-emission time** — the timestamp at which the trace was authored from the source. Suitable when the trace itself is the temporal anchor.
2. **Source dataset publication date** — the publication date of the source corpus. Suitable when the source data is a static published artefact (e.g. a benchmark release).
3. **Synthetic monotonic timestamp from a documented epoch** — a per-trace counter mapped to ISO timestamps from a fixed start. Suitable for ordered-but-untimed source data where absolute timing is meaningless.

The fallback choice is producer-side discipline. The spec requires that some timestamp be present and that the producer's choice be documentable; consumers MUST NOT assume that timestamps are wall-clock-accurate without verifying via envelope clock attestation (envelope §8) or producer documentation.

The integrity property the spec preserves is that every timestamped event has SOME temporal claim, with documentable basis. Wall-clock accuracy is a separate concern handled by envelope clock attestation and is out of scope here.

### 3.6 Clock authority

The ordering key in §4.3 assumes comparable timestamps within a trace. A single trace MUST be authored against a single logical clock; if the producer pipeline spans multiple physical sources (e.g. distributed harness, multiple worker hosts), the producer is responsible for reconciling timestamps to a single authority before emission. Skew handling and clock attestation are envelope concerns, not trace concerns. The trace presents one consistent timeline; how that timeline was assembled is out of scope here.

---

## 4. Events

### 4.1 Canonical event arrays

A trace stores events in four top-level arrays. Two are inherited from v0.2.x; two are added in v0.3.0.

| Collection | Status | Shape |
|---|---|---|
| `turns[]` | v0.2.x baseline, preserved | Ordered array of turns. Each turn carries `index`, which MUST equal its position in the array, counting from 0. |
| `tool_calls[]` | v0.2.x baseline, preserved | Ordered array of tool calls at trace level. Each tool call carries `id` and OPTIONAL `emitting_turn_index` linking it to a turn. |
| `segment_markers[]` | v0.3.0 addition | Ordered array of segment markers (§4.5). |
| `policy_triggers[]` | v0.3.0 addition | Ordered array of policy triggers (§4.6). |

There is no nested form. Tool calls live at trace level; the parent-turn relationship is carried by `emitting_turn_index` per the v0.2.x convention. Segment markers and policy triggers are siblings to `turns` and `tool_calls`, not nested inside them.

Turn shape (v0.2.x minimal, unchanged):

```json
{
  "index": 0,
  "timestamp": "<ISO 8601>",
  "role": "user",
  "content": "..."
}
```

Tool call shape (v0.2.x minimal, unchanged; v0.3.0 OPTIONAL additions noted):

```json
{
  "id": "tc-00042",
  "timestamp": "<ISO 8601>",
  "emitting_turn_index": 3,
  "tool_name": "...",
  "input": { ... },
  "output": { ... },
  "invocation_source": "agent_choice"
}
```

`invocation_source` is an OPTIONAL v0.3.0 addition recording which actor caused the call to be emitted (§4.7). v0.2.x tool calls without this field remain valid v0.3.0 tool calls.

### 4.2 Event addressing

For event-range annotations (§6.1) and any other cross-event reference, events are addressed by a stable identifier:

| Event type | Identifier |
|---|---|
| Turn | The turn's `index` integer, addressed as the string `turn-<index>` (e.g. `turn-3`). The index itself is not modified; the `turn-` prefix is only used in cross-event references (for example `event_range` endpoints, §6.1, and `event` redaction targets, §8.1). A `turn`-scope annotation's `target_id` is the bare index (§6.1). In both forms `<index>` is written in plain decimal digits, without sign, fraction or exponent (the RFC 8785 rendering of the integer), so the turn at position 3 is `turn-3` and `3` however its `index` is written (§3.4). |
| Tool call | The tool call's `id` field, used verbatim. |
| Segment marker | The marker's `id` field, used verbatim. |
| Policy trigger | The trigger's `id` field, used verbatim. |

Tool-call, segment-marker, and policy-trigger IDs are author-provided; if the author does not supply one, producers SHOULD synthesise stable IDs (`tc-<index>`, `seg-<index>`, `trig-<index>`) and MUST keep them consistent across re-serialisation of the same trace.

Identifiers used in references MUST be globally unique within the trace. Producers MUST NOT reuse a tool-call ID as a segment-marker ID, even though the namespaces are formally separate.

### 4.3 Event ordering

Cross-array operations (event-range annotation, timeline analysis, judge-panel sweeps) require a deterministic order across the four event arrays. The order is derived on read; nothing is stored.

**Ordering key.** Every event has the key below. Events are ordered by comparing keys component by component; a later component decides only when all earlier ones are equal.

| # | Component | Value |
|---|---|---|
| 1 | Timestamp presence | events with a non-null `timestamp` before events whose `timestamp` is null or absent |
| 2 | `timestamp` | ascending, by bytes (§3.5); for valid timestamps in the §3.5 form this is instant order |
| 3 | `ordering` presence | events with an integer `ordering` before events without one |
| 4 | `ordering` | ascending |
| 5 | Kind | 0: a turn, or a tool call whose `emitting_turn_index` names a turn of the trace; 1: any other tool call; 2: a segment marker; 3: a policy trigger |
| 6 | Anchor | kind 0: the turn's `index`, or the tool call's `emitting_turn_index`; kinds 1 to 3: the event's position in its array |
| 7 | Position | a turn: 0; a kind-0 tool call: 1 plus its position in `tool_calls[]` |

Array positions count from 0. Turn indices are unique (§4.1), so no two events share a key.

Because the key is compared component by component, the order is total and needs no identifiers. A turn precedes its own tool calls whenever their timestamps and `ordering` are equal (components 5 to 7). Tool calls with no `emitting_turn_index`, or one naming no turn of the trace (shell-first frameworks where tool events are the primitive), are siblings of turns and follow them at equal timestamps and `ordering`. When no turn or tool call has a timestamp or `ordering` and the trace has no segment markers or policy triggers, the order is the turns by `index`, each followed by its own tool calls, then the other tool calls in array order. Events without a timestamp follow every timed event, even when they are turns and the timed events are tool calls, segment markers or policy triggers: in a trace whose turns carry no timestamp, every segment marker and policy trigger precedes every turn (their timestamps are required, §4.5, §4.6), so every turn falls in the last segment of each `segment_type` (§4.5). Producers whose turns lack timestamps can use the §3.5.1 synthetic monotonic fallback (strategy 3), on the same clock as the trace's segment markers and policy triggers (§3.6), where segment spans matter.

The OPTIONAL `ordering` field (components 3 and 4) is an integer, or null, that MAY appear on any event object (turn, tool call, segment marker, policy trigger); a null `ordering` is read as an absent one. It is a tie-break hint among events with equal timestamps, or with none: lower values sort first, and an event with an integer `ordering` precedes one without. It does not establish order across differing timestamps, and it overrides the turn-before-own-tool-call order when set. Producers needing semantic ordering at equal timestamps SHOULD set it; producers that do not need explicit control SHOULD omit it.

A tool call's timestamp may be later than its parent turn's timestamp; in that case the tool call appears later in the derived order, even past sibling turns. Conversely, a tool call with an earlier timestamp than its parent turn (for example a turn stamped at completion) precedes it.

A conformant producer MUST derive this order deterministically for any given input, given conformance to §3.5 (timestamp normalisation) and §3.6 (single clock authority); the derived order is then reproducible by any conformant reader. Without §3.5 and §3.6, two readers may agree on event order yet disagree on byte representation.

### 4.4 Migration from v0.2.x

The canonical event arrays are unchanged from v0.2.x for `turns` and `tool_calls`. v0.3.0 does not require turn IDs to be synthesised, does not move tool calls under turns, and does not introduce a `turn_id` back-reference: the v0.2.x `emitting_turn_index` convention is retained. v0.2.x tool calls without `emitting_turn_index` (shell-first frameworks) remain valid; the ordering key in §4.3 (kind 1) covers their ordering.

`segment_markers[]` and `policy_triggers[]` are absent in v0.2.x and migrate as empty arrays.

### 4.5 Segment markers

A segment marker delimits a span of turns sharing a mode or focus. Fields:

| Field | Type |
|---|---|
| `id` | String |
| `timestamp` | ISO 8601 |
| `segment_type` | Enumeration: `mode_shift`, `focus_shift`, `context_reset`, `operator_change`, `x-*` |
| `segment_label` | Free text |

A segment is the span from one marker to the next marker of the same `segment_type`, or to the end of the trace. Analysis tools derive segment spans by scanning `segment_markers[]` in event order (§4.3); segments are not explicitly opened and closed.

### 4.6 Policy triggers

Runtime signals from the harness or in-flight classifiers. Distinct from post-hoc harm annotation (§9.3).

| Field | Type |
|---|---|
| `id` | String |
| `timestamp` | ISO 8601 |
| `trigger_name` | String identifier |
| `threshold_proximity` | Optional numeric in [0, 1]; closeness to the trigger's firing threshold, rising as the measurement nears it: `0` is the classifier's furthest measurable distance from firing, `1` is at or past the threshold |
| `fired` | Boolean |

The `threshold_proximity` field here describes a runtime classifier measurement. `fired` records the harness's decision and `threshold_proximity` the measurement; the two are not required to agree (a harness may suppress a firing, or fire on other signals), and how far a measurement lies past the threshold is not recorded. `threshold_proximity` is distinct from the post-hoc near-miss proximity in §9.4, which is a retrospective assessment at trace level.

### 4.7 Tool call invocation source

The OPTIONAL `invocation_source` field on a tool call records which actor caused the call to be emitted. It is distinct from `operator.type` (§5.1), which describes the principal directing the entire session. The two axes are orthogonal: an `operator.type: human` session can contain individual tool calls with any `invocation_source` value.

| Value | Semantics |
|---|---|
| `operator_explicit` | The operator's most recent input contains a literal trigger for this call (slash command name, tool name, or direct instruction string matched by adapter rule). |
| `agent_choice` | The call was emitted in the agent's token stream without an explicit operator trigger string. |
| `harness_injected` | The call was emitted as a side effect of harness configuration (hook, runtime policy, or equivalent), not from the agent's token stream. |
| `unknown` | The adapter cannot determine the source from the harness log. |

Adapters MUST populate this field when the harness log permits unambiguous determination. When the field is omitted or null, consumers SHOULD treat it as `unknown`; the equivalence does not extend to `content_hash` (§3.4), since the three forms serialise differently.

Detection rules are harness-specific and live in adapter documentation, not this specification. Conformance requires only that emitted values are drawn from the enumeration.

The enumeration is closed: it admits no `x-` values. Granular subdivisions of `harness_injected` (e.g. distinguishing hook-driven from policy-driven calls) and refinements of `agent_choice` that distinguish goal-serving inference from unsolicited proactive action are carried as annotations on the tool call (scope `tool_call`, §6.1) with `claim_type: classification`, while the field keeps the parent value and a subdivision is not written into it; a subdivision observed in production MAY be added to the enumeration in a later revision that changes `Y` (§3.1).

### 4.8 Tool call argument shape, identity and output origin

This subsection consolidates two normative constraints on tool calls surfaced by four-adapter validation: argument shape (`input.arguments`) and identifier preservation (`id`). §4.8.3 restates the closed value set of the v0.2.x `output.content_origin` member and adds one accepted value to it.

#### 4.8.1 Argument shape

The `input.arguments` field on a tool call MUST be an object (JSON object / dict), or null when the source carries no arguments for the call or none that can be read as an object (the v0.2.x default for an object field). Adapters whose source data carries tool-call arguments as JSON-encoded strings (e.g. OpenAI Codex's `function_call.arguments`) MUST parse the string into an object before emission. Producers MUST NOT emit `input.arguments` as a string.

The companion `minitrace-adapter-dsl-spec` (§9.1) provides a `json_parse(s)` builtin for this normalisation. Adapter authors targeting corpora with string-encoded arguments use `arguments: "json_parse($record.payload.arguments)"` to materialise the object.

Downstream consumers expect uniform shape regardless of source encoding. Source variance in argument shape (Claw: object; Codex: string) is the adapter's responsibility to normalise, not the consumer's.

#### 4.8.2 Tool-call identifier preservation

When the source data carries explicit tool-call identifiers (e.g. OpenAI `function_call.call_id`, OpenInference `tool_call.id`, Anthropic Messages API `tool_use.id`), adapters MUST preserve those identifiers as `tool_calls[].id` in the emitted minitrace trace. A tool call's result, when there is one, is carried on the tool call itself (`tool_calls[].output`, v0.2.x), so the call and its result need no separate join key; a turn that delivers a tool result as a conversation turn is marked by its v0.2.x `input_channel`, and v0.2.x gives it no field naming a tool call.

Adapters MUST NOT synthesise fresh identifiers when the source provides one. Synthesised identifiers are appropriate only for source data that lacks per-call identifiers (e.g. ATBench-Claw's OpenClaw v3 events, where the adapter must derive `atbench-claw-tc-${container_index}-${seq}` strings).

Preserving producer-supplied identifiers enables:

- Cross-trace tool-call lineage when calls span trace boundaries.
- Debugging by correlating minitrace output with raw producer logs.
- Consumer ability to check a tool call and its result against the producer's own records by the same identifier.

The synthesis-when-absent rule is consistent with the broader principle of §4.2: identifiers are author-provided when authoritative; producers synthesise stable IDs only when the source does not supply them.

#### 4.8.3 Tool-call output origin

`tool_calls[].output.content_origin` (v0.2.x) records what the tool accessed. Its values are the closed set v0.2.x lists for it (`local_file`, `local_exec`, `web`, `mcp_server`, `database`, `sub_agent`, `model_echo`, `user_provided`) and null, with one exception that this specification makes: `tool_output` is accepted as a legacy value, and a trace that carries it is conformant. A trace carrying any other value there is not conformant, as v0.2.x already held it a validation error; the base schema enumerates the set. `tool_output` is a value of a turn's `input_channel`; on a tool call's output it says nothing about origin, and a consumer SHOULD read it as v0.2.x reads null, unknown or not classified. The value itself is preserved as written (§3.1). A producer that does not classify the origin SHOULD write null or omit the member, as v0.2.x allows, and SHOULD NOT write `tool_output`.

---

## 5. Operator

### 5.1 Operator typology

The `operator.type` field is drawn from:

| Type | Privacy implication |
|---|---|
| `human` | GDPR / labour law applies; default redaction |
| `ai_overseer` | Different model observing; usually represented via lineage edge rather than operator content |
| `scripted` | Automated input; no privacy concern |
| `delegated` | Pseudonymous / bot / CI; identity structure explicit |

AI subagents within the same framework are NOT a separate operator type. They are represented via `delegates-to` lineage edges (§7.1) from the parent trace.

Operator type is a session-level fact about who directs the work. The `invocation_source` field on individual tool calls (§4.7) is a per-call fact about which actor caused each specific call to be emitted. The two are orthogonal and MUST NOT be conflated.

### 5.2 Operator capture mode

The `operator.capture_mode` field is REQUIRED when `operator.type` is `human` or `delegated`, and MUST NOT be null: a trace always states its capture mode, and a consumer never infers one.

| Mode | Semantics |
|---|---|
| `full` | Complete operator content captured |
| `pseudonymised` | Identity replaced with stable pseudonym |
| `scrubbed` | Content retained, identifiers removed |
| `reference_only` | Only reference to operator; content excluded |

The value a producer writes when no explicit choice has been made and type is `human`:

- **At production time** (a runtime capture pipeline emitting a fresh trace): the pipeline writes `scrubbed` and applies an implicit `operator_privacy` redaction (§8) to operator-identifiable fields. The pipeline knows what it captured and can choose this value unilaterally.
- **At migration time** (converting a v0.2.x trace whose original capture context is not authoritatively known to the migrating party): there is no value the migrating party may write unilaterally. The migrating party MUST explicitly accept `scrubbed` per §12.1.3, or choose `full` with `capture_purpose`, or apply redaction (§8) and write the mode that describes the result (`scrubbed`, `pseudonymised` or `reference_only`).

When type is `delegated` there is no such value: whoever produces or migrates the trace states the mode, and none is written by default.

### 5.3 Capture purpose

The `operator.capture_purpose` field is REQUIRED when `capture_mode` is `full` AND `operator.type` is `human`. It names the legitimate basis for full capture of a human operator's content.

When `operator.type` is `scripted`, `ai_overseer`, or `delegated` — i.e. the operator is not a real human with privacy interests — `capture_purpose` is NOT REQUIRED and SHOULD be absent or null. The field's purpose is human-operator privacy disclosure; non-human operators have no operator-privacy concern to cite.

Wherever `capture_purpose` is not required (every case other than a `human` operator with `capture_mode: full`), an absent field and a null one mean the same: no capture purpose is stated. The equivalence does not extend to `content_hash` (§3.4): the two serialise differently, so adding or dropping a null `capture_purpose` changes the hash.

| Field | Type |
|---|---|
| `legal_basis` | Enumeration: `gdpr_art6_consent`, `gdpr_art6_contract`, `gdpr_art6_legitimate_interest`, `gdpr_art88_employment_safeguards`, `works_council_agreement`, `research_ethics_approval`, `x-*` |
| `reference` | Free text: document ID, agreement reference, or approval identifier |
| `authorised_at` | ISO 8601 |

A well-formed `capture_purpose` is an object carrying all three fields of the table above: `legal_basis` one of the listed values or `x-*`, `reference` a non-empty string, and `authorised_at` a timestamp in the §3.5 form. Well-formedness concerns these three fields only: a `capture_purpose` object MAY carry other members, which do not make it ill-formed; such a member is treated like any member this specification does not name (§3.1): it is preserved, and its value remains subject to §3.4. For a `human` operator, a non-null `capture_purpose` MUST be well-formed whatever the `capture_mode`: a stated legal basis is a disclosure, and a disclosure that is stated has to be valid.

Validators MUST reject traces with `capture_mode: full` AND `operator.type: human` lacking a well-formed `capture_purpose`, and traces with `operator.type: human` carrying a non-null `capture_purpose` that is not well-formed. Validators MUST NOT reject a trace with `operator.type` ∈ {`scripted`, `ai_overseer`, `delegated`} because its `capture_purpose` is absent or not well-formed. A `capture_purpose` that is present still has these types, whatever the operator: it is an object or null, `legal_basis` and `reference` are strings (any string: the enumeration and a non-empty `reference` belong to well-formedness), and `authorised_at` is a timestamp in the §3.5 form.

The schema enforces these rules: the if/then conditionals in the Operator `$defs` block require a `capture_purpose` object in the `human` + `full` case and a well-formed one wherever a `human` operator carries an object, while the field's own definition there gives the types above for every operator, the `authorised_at` form included. Sentinel `x-synthetic` `legal_basis` values used by adapters targeting synthetic-operator corpora (e.g. ATBench-Claw, ATBench-Codex, mast-full, mast-human, otel-halo) stop being needed: an omitted `capture_purpose` on a non-human operator is normatively conformant.

### 5.4 Transparency flag

`operator.operator_aware` is a REQUIRED boolean on the operator object when `operator.type` is `human`. It states whether the operator was informed that the session was captured. Validators treat this as a factual assertion by the trace producer.

This field is NOT derivable from harness logs. A migration tool, runtime adapter, or production capture pipeline cannot infer the operator's awareness state from the session content alone. The value MUST come from the trace producer (the entity authoring or migrating the trace), who is in a position to know the operator's actual state of consent. A migration tool that cannot obtain this value MUST refuse to emit a valid v0.3.0 trace with `operator.type: human`.

---

## 6. Annotations

This section EXTENDS the v0.2.x annotation layer. All v0.2.x annotations remain valid under v0.3.0 without modification, subject to the migration rules in §12.1.

### 6.1 Scopes

| Scope | Status | Target identifier |
|---|---|---|
| `session` | v0.2.x baseline | Implicit (the trace itself); `target_id` MAY be omitted, and when present identifies this trace |
| `trace` | New in v0.3.0 | The same scope as `session`, under the same target rule; a consumer treats the two values alike |
| `turn` | v0.2.x baseline | The turn's `index` as a decimal string, the v0.2.x form (e.g. `7`) |
| `tool_call` | v0.2.x baseline | Tool call ID |
| `event_range` | New in v0.3.0 | `start_id` and `end_id` instead of `target_id`, both event identifiers per §4.2; see below |
| `segment` | New in v0.3.0 | Segment marker ID |

Besides the values in this table, `scope.type` may be the v0.2.x value `handover` (§12.1.5) or an `x-*` value; no other value is valid.

An `event_range` scope carries `start_id` and `end_id` and no `target_id`. Both are §4.2 event identifiers (so a turn is `turn-<index>`) naming events of the trace, and `start_id` does not come after `end_id` in the event order of §4.3. The range is inclusive: it covers both endpoints and every event between them in that order. An `event_range` scope whose endpoint names no event of the trace, or whose `start_id` comes after its `end_id`, is not conformant.

Turn and tool-call `target_id` forms are unchanged from v0.2.x, so v0.2.x annotations address correctly without modification.

### 6.2 Sources

The v0.2.x `annotator` free-text field is RETAINED. v0.3.0 ADDS a structured `source` field carrying epistemological status.

| `source.type` | Typical `annotator` | Semantics |
|---|---|---|
| `runtime` | Harness component name | Emitted during session; integrity tied to event stream |
| `reviewer` | Human identifier | Post-hoc human review |
| `judge` | Judge identifier | Panel judgment, typically from a `judge-session` trace |
| `overseer` | Overseer trace reference | Observation from an overseer session |
| `aggregator` | Tool identifier | Automated analysis across traces |

Both `source.type` and `annotator` MAY be present. `annotator` identifies who; `source.type` classifies what kind of claim it is.

**Optional enrichment of v0.2.x annotations:** v0.2.x `annotator` is a free-text field carrying values like `user`, `model`, `automated`, named human reviewers, or harness component names. `source.type` is OPTIONAL on v0.3.0 annotations; v0.2.x annotations without it remain valid. v0.3.0 does NOT default `source.type` to a single value, because that would silently misclassify annotations whose `annotator` indicates a different epistemic status.

For producers that wish to derive `source.type` from `annotator`, the following mapping is recommended:

| v0.2.x `annotator` (case-insensitive) | Recommended `source.type` |
|---|---|
| Named human (free-text identifier not in this table) | `reviewer` |
| `user` | `reviewer` |
| `model`, `automated` | `aggregator` |
| Harness component name (matches a known runtime emitter) | `runtime` |
| Overseer trace reference | `overseer` |
| Judge identifier (from a `judge-session` trace) | `judge` |

When the `annotator` value is ambiguous, a producer applying this mapping SHOULD surface the choice to the trace owner rather than guess. Producers that do not enrich SHOULD leave `source.type` unset; consumers MUST treat absent `source.type` as "unclassified", not as any default value.

The `model` / `automated` → `aggregator` recommendation assumes post-hoc analytical classifiers. If a v0.2.x deployment used `model` or `automated` for an in-flight runtime classifier emitting during the session, the correct target is `runtime`, not `aggregator`. Producers SHOULD review `model` / `automated` annotations for runtime-emission origin before accepting the default; mis-mapping a runtime signal as an aggregator artefact loses the runtime/post-hoc distinction in §9.3.

### 6.3 Claim types

The `claim_type` field distinguishes what kind of assertion an annotation makes.

| Claim type | Meaning |
|---|---|
| `observation` | Descriptive note without classification |
| `classification` | Category assignment from a named vocabulary |
| `harm_linkage` | Event contributed to a harm outcome |
| `segment_marker_note` | Comment on a segment boundary |
| `disposition` | Reviewer disposition (`accepted` / `rejected` / `deferred`) |
| `x-*` | Other experimental |

Optional enrichment of v0.2.x annotations: `claim_type` is OPTIONAL. v0.2.x annotations without it remain valid. For producers that wish to derive `claim_type` from the existing `category` value, the recommended mapping is:

| v0.2.x category | Recommended `claim_type` |
|---|---|
| `observation` | `observation` |
| `ai-failure`, `user-error`, `environment-issue`, `success` | `classification` |
| `question`, `to-discuss`, `to-improve` | `observation` |

The original `category` is preserved; `claim_type` is a derived field. Producers that do not enrich SHOULD leave `claim_type` unset.

### 6.4 Baseline categories

Two sources contribute to the v0.3.0 baseline category vocabulary:

**From v0.2.x specification text** (Annotation `category` field): `observation`, `pattern`, `ai-failure`, `recommendation`.

**Promoted from operational practice** (used in deployed traces but not formalised in the v0.2.x spec): `user-error`, `environment-issue`, `success`, `question`, `to-discuss`, `to-improve`.

**Added in v0.3.0:** `near_miss`, used as §9.4 prescribes.

The full v0.3.0 baseline vocabulary is the union: `observation`, `pattern`, `ai-failure`, `recommendation`, `user-error`, `environment-issue`, `success`, `question`, `to-discuss`, `to-improve`, `near_miss`.

### 6.5 Classification levels

The v0.2.x classification values are RETAINED: `public`, `internal`, `confidential`, `customer-confidential`. In v0.3.0, classification is treated as access-control metadata distinct from redaction (§8). Classification describes who may see content; redaction describes what was removed.

### 6.6 Disagreement preservation

When multiple annotations exist on the same target, v0.3.0 implementations MUST preserve all of them. Consensus is NOT a first-class field; it is a derived artefact produced by downstream analysis. This preserves disagreement information that judge-panel methodologies depend on, and avoids collapsing reviewer divergence at storage time.

### 6.7 Taxonomy mapping

v0.2.x carries taxonomy codes in a `taxonomy_mappings` object whose subfields name the source taxonomy (e.g. `minitrace`, `mast`, `toolemu`). v0.3.0 ADDS an alternative single `taxonomy[]` array of strings with explicit namespace prefixes. Both forms are valid; producers MAY emit either or both, and consumers MUST accept either or both. Flattening `taxonomy_mappings` into `taxonomy[]` (§6.7.4) is the producer's choice (§12.1.2); a consumer re-serialising a trace does not flatten (§3.1).

#### 6.7.1 Single-claim multi-namespace tagging

The `taxonomy[]` array within a single annotation represents multi-namespace tagging of *the same claim*. For example, an annotation classifying a failure under both MAST and OWASP-LLM frameworks emits one annotation with `claim_type: classification` and `taxonomy: ["mast/F-AUT", "owasp-llm/LLM08"]`. Both entries refer to the same underlying classification.

#### 6.7.2 Orthogonal claim axes

When a corpus assigns multiple orthogonal classifications to a single trace (e.g. ATBench-Claw's three-axis labelling: `risk_source` × `failure_mode` × `harm_type`), each axis MUST be emitted as a separate annotation. A single annotation MUST NOT carry orthogonal classifications — the `taxonomy[]` field is for multi-namespace tagging, not for compound claims.

**Why the distinction.** Orthogonal axes are independently meaningful — a producer or consumer may revise one axis without affecting the others; a judge may disagree on one axis while agreeing on another. Storing them as separate annotations preserves disagreement-preservation per §6.6 across axes; collapsing them into one annotation flattens that information.

#### 6.7.3 Domain-qualified namespace prefix

Taxonomy entries SHOULD use a domain-qualified namespace prefix when the source taxonomy is variant-specific. The recommended shape:

```
<framework>/<variant>/<axis>/<value>
```

where:

- `<framework>` names the parent taxonomy (e.g. `atbench`, `mast`, `owasp-llm`)
- `<variant>` names the corpus or domain customisation (e.g. `claw`, `codex`, `m4`, `llm08-2025-edition`); MAY be omitted when the framework has no variant axis
- `<axis>` names the orthogonal classification axis (e.g. `risk-source`, `failure-mode`, `harm`, `defense-type`)
- `<value>` is the taxonomy value, formatted in the source corpus's idiom (snake_case for ATBench, KEBAB-CASE for OWASP, etc.)

Adapters targeting variant-specific corpora MUST NOT collapse the variant qualifier; an entry like `atbench/risk-source/skill_plugin_supply_chain_compromise` is ambiguous because that risk-source value exists only in ATBench-Claw, not in parent ATBench. The variant qualifier preserves cross-variant semantic distinctness.

Multi-namespace entries (single annotation tagged across multiple frameworks) follow §6.7.1: separate strings in the same `taxonomy[]`, e.g. `["mast/F-AUT", "owasp-llm/LLM08-2025"]`.

#### 6.7.4 General properties

- Taxonomy codes are external to this specification.
- A single annotation MAY carry multiple taxonomy mappings across either form (e.g. `minitrace/F-AUT` and `owasp-llm/LLM08` together in one `taxonomy[]`).
- The format does not mandate a specific taxonomy.

**Flattening:** In v0.2.x (§1.4), `taxonomy_mappings` has the members `minitrace`, `mast` and `toolemu`, each an array of code strings. Flattening maps each string `c` in the array of member `<name>` to the `taxonomy[]` entry `"<name>/<c>"`, duplicates included. The entries follow the members in the order RFC 8785 sorts their names (`mast`, `minitrace`, `toolemu`), and within a member the array order. None of these member names contains `/`, so distinct (name, code) pairs give distinct entries. A null `taxonomy_mappings`, an absent member, or a member whose array is empty yields no entry. The resulting entries keep every code and represent no empty member; the mapping is defined in this direction only. See §12.1.2.

**Meaning:** An annotation is tagged with every entry in its `taxonomy[]` or in the flattening of its `taxonomy_mappings`, whichever forms it carries. An entry is a tag on the annotation's claim (§6.7.1). In any annotation, an entry repeated within one form or across the two adds nothing to the annotation's meaning; a consumer re-serialising the trace still keeps it (§3.1).

#### 6.7.5 Version qualifier

When a source corpus uses different taxonomy versions across its release history, OR when a producer evolves its event-shape vocabulary across versions during a continuously-running session, adapters SHOULD encode the version into the taxonomy namespace path:

```
<framework>/<version>/<axis>/<value>
```

instead of `<framework>/<axis>/<value>`. Versions are producer- or corpus-specific identifiers (e.g. `mast/v0` for Round 1, `mast/v1` for Rounds 2-3, `mast/v2` for Generlazability + published; `claude-code/2.1.131` for that harness patch version).

The version qualifier preserves cross-version semantic distinctness: the same code (e.g. `1.4`) MAY map to different failure modes across versions, OR the same event-shape MAY emit different attachment subtypes across producer patch versions. Consumers performing cross-version analysis MUST use both version and code as the joint identifier.

**Per-event resolution.** The version qualifier is resolved **per event**, not per trace. A single agent-session that survives a producer upgrade mid-session can carry annotations qualified under two distinct version namespaces. Adapters MUST resolve the version from the source record carrying each event, not from a session-level constant. A session-level qualifier is incorrect for any session whose underlying records span multiple producer versions.

**Reading the qualifier.** Higher version numbers are NOT a quality or maturity progression. The qualifier reflects chronological round (for annotation-version corpora like MAST) or producer patch version (for moving-target corpora like claude-code), not refinement. Cross-version comparison without the per-version semantics in mind will mislead. Consumers SHOULD treat each `<framework>/<version>` namespace as semantically distinct from sibling versions and resolve cross-version equivalence (where claimed) via documented mapping, not by string identity of `<axis>/<value>` across versions.

**Compatibility.** A producer that does NOT carry version-shifting taxonomy MAY continue to emit `<framework>/<axis>/<value>` (omitting the version slot) per §6.7.3. This subsection's `<version>` slot is additive and SHOULD be present whenever the source distinguishes versions; it is not retroactively required for single-version corpora.

### 6.8 Annotation integrity

Annotations MAY carry `authored_at` and `author_signature` fields. `authored_at` is the time the annotation was written into this trace; the annotation's `timestamp` keeps its v0.2.x meaning, the time the annotation was made. The two coincide for an annotation made and written at once, and differ where an annotation made elsewhere is carried into the trace, as an imported review is, or where one is appended some time after it was made. Both are timestamps in the §3.5 form, `authored_at` when present. Whoever writes an annotation into a trace, the trace's producer or a consumer appending the annotation (§3.1), SHOULD set `authored_at` when writing it, and MAY set `author_signature` (§14.4). Both are set with the annotation: a consumer re-serialising a trace does not add either to an annotation already in it, since that is not among the additions §3.1 permits, and a migrating party adds neither to a v0.2.x annotation it carries over (§12.1.2). This enables post-hoc review of annotation provenance without conflating it with runtime event integrity.

---

## 7. Trace-of-trace Lineage

### 7.1 Edge types

Baseline edges in v0.3.0:

| Edge type | Meaning | Integrity requirement | Redaction propagation |
|---|---|---|---|
| `derived-from` | B is analytical output over A | None | MUST NOT propagate A's operator capture to B |
| `judges` | B is a panel verdict on A | A's `content_hash` REQUIRED at reference time | MUST preserve A's redaction decisions |
| `reviews` | B is a review of A | A's `content_hash` REQUIRED at reference time | MUST preserve A's redaction decisions |
| `audits` | B is an audit of A (e.g. red-team or safety evaluation) | A's `content_hash` REQUIRED at reference time | MUST preserve A's redaction decisions |
| `delegates-to` / `delegated-by` | Multi-agent handoff | None | Independent |
| `wraps` / `wrapped-by` | Harness wraps wrapped agent (see §7.4) | `wraps`: target's `content_hash` REQUIRED at reference time. `wrapped-by`: omitted (mirrors `delegated-by` per §7.3 to avoid a cross-trace reference cycle, DSL §13.2) | Independent |

An edge requiring integrity MUST NOT be created when the referenced trace has no `content_hash`. Producers of such edges are responsible for ensuring the hash exists before authoring the edge; a producer may set it on the referenced trace where it is absent or null (§3.1).

**Redaction propagation across edges.** "Preserving A's redaction decisions" (column above) means: an edge from B to A references A by `target_content_hash`, which covers A's content as redacted at the time the hash was computed. If A changes later (a redaction, or an addition §3.1 permits other than setting `content_hash`), A's hash changes, and B's reference is to the prior snapshot, not the new one. B does not reconstruct A's pre-redaction content. B does not author its own redactions over A. The cross-trace integrity contract is that B references a specific historical state of A, including whatever redactions A had at that moment.

This means redactions are trace-local in storage (§8) but propagate operationally across edges via the hash reference. A consumer following B's edge to A, and verifying the hash, sees only the redacted A.

Reserved for future baseline promotion in a revision that changes `Y` (§3.1; use as `x-*` until adopted):

| Reserved name | Intended meaning |
|---|---|
| `replays` | B re-executes A's scenario |
| `aggregates` | B summarises A and siblings |
| `overseen-by` / `observes` | Overseer-observer pair |
| `supersedes` | B corrects or replaces A |

### 7.2 Edge shape

```json
{
  "type": "judges",
  "target_trace_id": "<id>",
  "target_content_hash": "sha256:...",
  "authored_by": "<principal>",
  "authored_at": "<ISO 8601>"
}
```

`authored_by` is REQUIRED for edge types `judges`, `reviews`, and `audits` (where authorship is evidentiary). RECOMMENDED for others. For the same three types `authored_at` is REQUIRED and MUST NOT be null; where the source records no authoring time, the §3.5.1 fallback applies.

### 7.3 Multi-agent chains

Multi-agent workflows are represented as graphs of traces connected by `delegates-to`/`delegated-by` edges. Each agent produces its own trace with its own integrity and redaction. A consumer assembles the chain by traversing edges.

**Edge direction:** `delegates-to` is authored on the parent (delegating) trace and points at the child (delegated-to) trace. `delegated-by` is the inverse, authored on the child and pointing at the parent. The two are paired views of the same logical relationship; producers SHOULD author whichever side they own and SHOULD NOT author both for a single relationship. Consumers traversing the graph treat the pair as equivalent and MUST de-duplicate when both directions are present.

Neither edge requires integrity (§7.1). A `delegated-by` edge usually carries no `target_content_hash`, and §7.1 has a `wrapped-by` edge omit it, for the same reason: a child holding its parent's hash while the parent holds the child's would form a reference cycle that cannot be hashed (DSL §13.2 R3).

### 7.4 Harness-wrap shape

A **harness** producer's primary function is to invoke another agent (binary CLI, subprocess, or in-process agent) and capture its observation of the run alongside, optionally, the wrapped agent's native trace. Harness producers MUST select between two structural variants based on whether the wrapped agent's native trace is reachable from the harness's structured output:

- **Double-trace.** When the harness exposes the wrapped agent's native trace through a structured surface the adapter can read (e.g. a debug store entry populated when an instrumentation flag is set), the harness produces TWO sibling traces in one container: an outer harness-view trace with `lineage.type: wraps` (integrity-bearing, carries `target_content_hash` of the inner trace per §3.4), and an inner wrapped-agent-view trace with `lineage.type: wrapped-by` (discovery-only, no `target_content_hash` — mirrors the `delegated-by` precedent in §7.3, avoiding a cross-trace reference cycle per DSL §13.2).
- **Outer-only.** When the harness routes the wrapped agent's stdout through a free-form facility (logging stream, sandbox-internal file, plain-prose output) that the adapter cannot reach as structured content, the harness produces ONE outer trace with empty/absent `lineage[]` and `flags.wraps_inner: false` set explicitly on the outer trace.

The variant choice is **upstream-determined**: it tracks the wrapped agent's instrumentation contract with the harness, not adapter-author discretion. Either variant's outer trace is independently usable; the double-trace variant additionally enables cross-validation of the wrapped agent's self-reported behaviour against the harness's bridge-observed view.

### 7.5 Outer-trace `flags.wraps_inner`

When a producer follows the harness-wrap shape (§7.4), the outer trace MUST set `flags.wraps_inner` to a definite boolean: `true` if the harness produces a paired inner trace (double-trace variant), `false` if the harness produces no inner trace (outer-only variant). The flag MUST NOT be omitted on outer traces of harness producers; explicit denial (`false`) is the structural marker that distinguishes an intentional outer-only emission from an incomplete double-trace one.

The flag's queryable surface lets downstream tooling filter for outer traces by variant without traversing the `lineage[]` array.

---

## 8. Redaction

### 8.1 Redaction record

A redaction is an auditable typed operation, not string substitution. Each redaction record carries:

| Field | Type |
|---|---|
| `id` | String; unique within the trace |
| `target_kind` | Enumeration: `event` / `annotation` / `field` |
| `target` | Identifier whose form depends on `target_kind`, per the table below |
| `operation` | Enumeration (§8.2) |
| `reason_code` | Enumeration (§8.3) |
| `authorised_by` | Principal identifier |
| `authorised_at` | ISO 8601 timestamp |
| `original_content_hash` | `sha256:` and 64 lowercase hex digits: SHA-256 over the RFC 8785 serialisation of the target's JSON value before the operation (optional) |

`target_kind` selects how `target` is interpreted:

| `target_kind` | `target` form | Meaning |
|---|---|---|
| `event` | Event identifier per §4.2 (`turn-<index>` for a turn; the `id` of a tool call, segment marker or policy trigger) | The entire event is redacted; its members are replaced as §8.2 (Result of an operation) states. |
| `annotation` | Annotation ID | The entire annotation is redacted. |
| `field` | RFC 6901 JSON pointer rooted at the top-level trace object | A specific subfield is redacted; the surrounding event or annotation remains visible. |

The value `original_content_hash` covers is the JSON value at the pointer for a `field` target, the whole event object for an `event` target, and the whole annotation object for an `annotation` target, serialised as RFC 8785 serialises it for `content_hash` (§3.4).

Producers MUST set `target_kind` explicitly; it is not inferred from the shape of `target`. This avoids ambiguity when an annotation ID and an event ID happen to be lexically similar.

Redaction records live in a top-level `redactions[]` array on the trace. They are themselves NOT redactable in v0.3.0.

### 8.2 Operations

| Operation | Semantics |
|---|---|
| `remove` | Content replaced by null or the empty value (below); for a whole event or annotation, a member that admits neither is kept |
| `pseudonymise` | Content replaced with stable token |
| `truncate` | Content shortened |
| `mask` | Content replaced with fixed mask |
| `generalise` | Content replaced with less specific form |

**Result of an operation.** Redaction records apply in the order of `redactions[]`, and `original_content_hash` covers the target's value immediately before that record's operation. A redaction keeps the trace conformant (§13), and it does not change the members that identify or order an event or annotation object: `id`, `index`, `timestamp`, `ordering` and `emitting_turn_index` (§4.2, §4.3), any member whose value is an event identifier or a list of them (for example the v0.2.x `tool_calls_in_turn`), and an annotation's `scope`. A `field` target MUST resolve when its record is applied, and MUST NOT name or lie inside one of those members. A `field` target also MUST NOT name `/turns`, an item of it (`/turns/<n>`), or `/profile`, which selects the v0.2.x minimum fields a trace must carry (§3.1); a whole turn is redacted by an `event` target, member by member. `remove` replaces the targeted value with null; where neither the schemas nor this specification admit null at that position, with the empty value of the value's current JSON type (`""`, `{}` or `[]`); where neither is admitted (booleans and numbers have no empty value), the value is kept. That fallback serves only the members of an `event` or `annotation` target: a `field` target MUST NOT use `remove` where the value would be kept, and uses `mask`, `pseudonymise` or `generalise` instead. For an `event` or `annotation` target, every operation applies member by member to each member other than those named above. The other operations replace a targeted value with a value of the producer's choice (a token, a shortened or masked string, a less specific form) within the same limits. No placeholder object is defined: the redaction record, not the redacted value, says what was done. Redaction cannot hide those identifying and ordering members (§15.1).

### 8.3 Reason codes

| Code | Applies to |
|---|---|
| `operator_privacy` | Human operator identifiable content |
| `pii_detected` | PII within session content |
| `confidentiality` | Subject to NDA or confidentiality level |
| `legal_hold` | Pending legal process |
| `submitter_anonymity` | Third-party submission to regulator |
| `gdpr_erasure` | Data subject request |
| `x-*` | Experimental |

### 8.4 Default behaviours

§5.2 specifies the `capture_mode` a producer writes at production and migration time when no explicit choice has been made. The implicit `operator_privacy` redaction associated with the production-time `scrubbed` value is recorded in the trace's `redactions[]` array per §8.1, with `target_kind: field` pointing at operator-identifiable fields and `reason_code: operator_privacy`. The validation rule for the `full` case is in §5.3 and is not repeated here.

---

## 9. Harm Annotation

### 9.1 Anchoring

v0.3.0 does NOT define a novel harm taxonomy. Harm classifications anchor to external categories via the `harm_category` field on annotations with `claim_type: harm_linkage`:

| `harm_category` value | Source |
|---|---|
| `aia_3_49_a` | AI Act Art. 3(49)(a) |
| `aia_3_49_b` | AI Act Art. 3(49)(b) |
| `aia_3_49_c` | AI Act Art. 3(49)(c) |
| `aia_3_49_d` | AI Act Art. 3(49)(d) |
| `gpai_cop_cybersecurity` | GPAI Code of Practice — cybersecurity |
| `x-*` | Experimental / non-AI-Act contexts |

The cited instruments are authoritative for these category definitions; this specification does not restate or interpret them.

Optional secondary taxonomies attach via the `taxonomy` field (§6.7).

### 9.2 Three-level structure

Harm-relevant signals attach at three levels, each answering a different question:

| Scope | Question answered | Typical source |
|---|---|---|
| `session` or `trace` | Did the session lead to a serious incident? | Post-hoc reviewer |
| `turn` or `tool_call` or `event_range` | Which specific events contributed? | Post-hoc reviewer |
| no annotation scope: `policy_trigger` events in `policy_triggers[]` (§4.6); retrospectively, a near-miss annotation (§9.4) | How close did the session get to a threshold? | Runtime; post-hoc reviewer for a near-miss annotation |

No separate `harm_level` field is needed. An annotation's `scope` separates the first two levels, both carried by `harm_linkage` annotations (§9.3); the third is carried at runtime by `policy_trigger` events, which are not annotations (§9.3), and a retrospective near-miss annotation, although `session`- or `trace`-scoped like the first, is told apart from it by `content.category` `near_miss` and its `near_miss_*` members (§9.4).

### 9.3 Runtime versus post-hoc distinction

Runtime signals (policy triggers, classifier hits, threshold proximity) are events of type `policy_trigger` (§4.6). Post-hoc harm classifications are annotations with `claim_type: harm_linkage` (§6.3). These MUST NOT be conflated; they carry different epistemological status.

### 9.4 Near-miss representation

A near-miss is a session that approached but did not trigger a harm outcome. Near-miss signals are `session`- or `trace`-scoped annotations with:

| Field | Value |
|---|---|
| `claim_type` | `observation` |
| `content.category` | `near_miss` |
| `near_miss_proximity` | Numeric in [0, 1]; retrospective assessment of closeness to the threshold named in `near_miss_threshold_name`, in the direction of `threshold_proximity` (§4.6): `0` is furthest from it, `1` marks the threshold; a session past the threshold is not a near miss |
| `near_miss_threshold_name` | String identifier |

The `near_miss_proximity` field is retrospective and distinct from `policy_trigger.threshold_proximity` in §4.6, which is a runtime measurement. The two may be related in practice but MUST NOT share a field name.

Near-miss aggregation across traces is typically represented by annotations on a trace of reserved type `x-aggregate-session` (future baseline `aggregate-session`).

### 9.5 Defense-mechanism classification

When a corpus annotates defensive behaviour orthogonal to risk classification (e.g. ATBench-Claw and ATBench-Codex `defense_type` field), the annotation MUST be emitted as a separate annotation with `claim_type: classification` and a domain-qualified taxonomy entry under the framework's `defense-type` axis (e.g. `atbench/claw/defense-type/detected_with_warning`, `atbench/codex/defense-type/partial_refusal`), per §6.7.3.

Defense-mechanism classifications describe **observed behaviour of the defense layer** (e.g. "agent detected and warned", "agent refused", "agent partially refused"), independent of whether the trajectory was overall safe or unsafe. Adapters MUST emit the annotation whenever the source provides a non-null defense classification, regardless of whether the trajectory's overall `is_safe` status is true or false. The semantic interpretation is:

> "What did the defense mechanism do during this trajectory?"

NOT:

> "How did this trajectory remain safe?"

The latter framing was a possible reading from ATBench-Claw alone (where `defense_type` co-occurs only with `is_safe=true`). ATBench-Codex's release confirms the broader reading: 37 of 250 unsafe trajectories carry non-null `defense_type`, indicating defense-mechanism activation that did not prevent harm.

Defense-type taxonomy values are corpus-specific. Adapters MUST NOT remap source values to a hypothetical canonical defense-type vocabulary; the source vocabulary is preserved under the variant qualifier per §6.7.3.

This subsection is distinct from §9.1 anchoring: defense-mechanism classifications do NOT use the `harm_category` field. They are observations of defense-layer behaviour, not harm-linkage claims. An annotation MAY carry both kinds of information about the same trajectory (one annotation with `claim_type: harm_linkage` and `harm_category: aia_3_49_*`, another with `claim_type: classification` and a `defense-type` taxonomy entry); the two answer different questions.

---

## 10. Extension Mechanisms

### 10.1 Experimental namespace

Any field name starting with `x-` is experimental. Implementations MUST accept and preserve `x-` fields without validating their content against any shape; the value remains subject to §3.4 (numeric domain, JSON form, nesting depth), as any other member's is (§3.1), and a trace whose `x-` value breaks it is not conformant.

### 10.2 Vendor namespace

Vendor-scoped extensions use `ext:<vendor>:<name>`. Example: `ext:anthropic:reasoning_hash`.

Harnesses commonly emit per-call telemetry that has no canonical home in the v0.3.0 tool call shape (for example per-call duration, input byte size, or harness-internal call identifiers). Adapters SHOULD carry such fields via vendor-namespaced extensions on the relevant event, e.g. `ext:<harness>:duration_ms`. Future revisions MAY promote widely observed telemetry fields to baseline. Until promotion, consumers MUST treat such fields as adapter-specific and MUST NOT rely on their presence.

**Extension annotations.** An annotation that carries an extension MUST name it in `content.ext_namespace` with the full `ext:<vendor>:<name>` string (for example `"ext:bridge:model_event"`) and MUST carry its payload in other members of `content`. Every annotation that §10.5 to §10.7 direct a producer to emit (`ext:inspect_ai:event:*`, `ext:bridge:*`, `ext:mcp:*`) is such an annotation, as is any other annotation a producer emits for an `ext:` extension. A consumer finds every extension annotation by that one member.

### 10.3 Reserved namespaces

| Namespace | Reserved for |
|---|---|
| `x-aggregate-*` | Future aggregate-session trace type |

Additional reserved namespaces MAY be added in a revision that changes only `Z` (§3.1; for example v0.3.1) without breaking compatibility. Reservation is by specification amendment, not by ad hoc claim, and a reservation added in a revision that changes only `Z` binds only traces that declare that revision or a later one (§3.1).

### 10.4 Schema URI

Any trace whose `trace_type` is neither `agent-session` nor `judge-session` MUST carry a non-null `schema_uri` field pointing to a JSON Schema document. See §3.2 for validation behaviour.

### 10.5 Inspect-AI event taxonomy

When a producer is based on `inspect_ai`'s event module, the adapter SHOULD walk all declared event types in the inspect-ai event module (not just the corpus-visible subset), passing unknown event types through as `ext:inspect_ai:event:<type>` annotations per the vendor namespace pattern of §10.2. The canonical envelope is OPEN to future inspect-ai event additions; new types fall through to the extension surface rather than being silently dropped.

### 10.6 Bridge-intercepted model events

When a producer is a harness that intercepts the wrapped agent's outbound model-provider API calls (via env-var-redirected base URL pointing at a local proxy, in-process request interceptor, or SDK-level instrumentation), the adapter SHOULD emit bridge-observed interactions as `ext:bridge:model_event` annotations on the outer trace at per-turn scope, carrying the verbatim request/response/usage payload from the wire.

The namespace `ext:bridge:*` is reserved as a cross-producer convention for bridge-intercepted observations: producer-specific bridge annotations SHOULD use this namespace rather than a producer-scoped variant (`ext:<producer>:bridge:*`). The cross-producer namespace reflects that the bridge surface — wire-level observation independent of the wrapped agent's own reporting — is structurally identical across producers.

The bridge view sits on the outer (harness) trace per §7.4; the wrapped agent's self-reported model events, when reachable, live on the inner trace. Holding both views enables cross-validation of producer-self-reported behaviour against wire-level observation. Realisability of the inner-side view is upstream-determined: it depends on the wrapped agent's stdout-shape contract with its harness and may not exist for every wrapped agent.

Bridge annotations are subject to §8 redaction like any other annotation. The verbatim wire payload often contains the most direct path to model-input content and warrants explicit redaction policy at producer-deployment time.

### 10.7 MCP-protocol observations

When a producer observes Model Context Protocol (MCP) traffic — characteristically from the server or peer vantage, where the observer sees the wire conversation but not the host agent's reasoning about it — MCP-protocol-specific observations SHOULD be emitted under the reserved namespace `ext:mcp:*`. Observed examples: `ext:mcp:capability_negotiation` (the `initialize` handshake, offered protocol version, and advertised capabilities) and `ext:mcp:advertised_vs_serviced` (a capability advertised at negotiation but not honoured when the peer exercises it).

`ext:mcp:*` is reserved as a cross-producer convention, parallel to `ext:inspect_ai:event:*` (§10.5) and `ext:bridge:*` (§10.6): the namespace reflects MCP-protocol semantics that are structurally identical across MCP-observing producers rather than specific to any one host or fixture. It is complementary to, and distinct from, `ext:bridge:*`. The bridge namespace carries vantage-class wire observations independent of protocol — a server-initiated request that received no response is an `ext:bridge:mcp:server_request` annotation under the already-reserved bridge namespace — while `ext:mcp:*` carries the MCP-protocol-specific interpretation. A server-vantage observer is structurally blind to the actor that caused a tool call; calls it records therefore carry `invocation_source: unknown` (§4.7). This blindness is the defining property of the vantage, not a defect.

Baseline promotion of any `ext:mcp:*` scope or trace type is deferred until a second, independent MCP-observing producer corroborates the shape, and then needs a revision that changes `Y` (§3.1). Reservation here signals the convention without promoting it on a single deployment.

---

## 11. Reference Architecture (non-normative)

A reference storage and query-command architecture — a fast working annotation store synced to the canonical `.minitrace.json`, plus parameterised analysis queries shipped across installations — is non-normative implementation guidance. It is documented separately and is not part of this specification; v0.3.0 mandates none of it.

---

## 12. Backwards Compatibility

v0.3.0 is an additive layer over the v0.2.x trace shape. The full v0.2.x top-level structure is preserved. Migration is mechanical for everything except the new `operator` object, which carries fields whose values cannot be inferred from v0.2.x content.

### 12.1 v0.2.x to v0.3.0 migration

#### 12.1.1 Mechanical reformatting

Performed automatically:

| Element | Transformation |
|---|---|
| `schema_version` | Reformat the value from `minitrace-v0.2.0` to `0.3.0`. The field already exists in v0.2.x; only the value format changes. |
| `trace_type` | Introduce with value `agent-session`. Required new top-level field; the baseline default is unambiguous for any v0.2.x agent session. |

#### 12.1.2 Annotation extensions (additive, no transformation required)

v0.3.0 adds fields to the annotation object: `source.type`, `claim_type`, and the `event_range` / `segment` scopes (§6). These are OPTIONAL on existing v0.2.x annotations; absence is valid.

For v0.2.x annotations carried into v0.3.0:

| Element | Treatment |
|---|---|
| `annotator` free-text values | Retained verbatim. The mapping table in §6.2 is provided for tools that wish to derive `source.type`; it is not required by migration. |
| `category` value (existing v0.2.x field) | Retained verbatim. The mapping table in §6.3 is provided for tools that wish to derive `claim_type`; it is not required by migration. |
| `taxonomy_mappings` | Retained, or flattened by the producer of the v0.3.0 trace, a migrating party included, to `taxonomy[]` entries `"<name>/<code>"` as §6.7.4 defines, either in place of `taxonomy_mappings` or beside it, as the producer chooses. A consumer re-serialising a trace does not flatten (§3.1); a consumer that wants the `taxonomy[]` form applies §6.7.4 when reading. v0.3.0 producers MAY emit either form or both; v0.3.0 consumers MUST accept either or both. The resulting entries keep every code and represent no empty member (§6.7.4). |

#### 12.1.3 The `operator` object (REQUIRES user input)

v0.2.x has no `operator` object. v0.3.0 makes it REQUIRED at top level (§5). It cannot be inferred from v0.2.x content; values come from the trace owner.

A migration tool MUST refuse to emit a valid v0.3.0 trace without these:

| Field | Requirement |
|---|---|
| `operator.type` | The default value `human` SHOULD be confirmed by the trace owner rather than assumed. |
| `operator.operator_aware` (boolean, REQUIRED when `operator.type` is `human`) | No safe automated default. The trace owner MUST declare whether the operator was informed of capture. |
| `operator.capture_mode` (REQUIRED when `operator.type` is `human` or `delegated`, §5.2) | For `human`: `scrubbed` only if the trace owner explicitly accepts it. If the v0.2.x trace contains identifiable operator content, the owner MUST either authorise `full` (which requires `capture_purpose`) or apply redaction (§8) and write the mode that describes the result (`scrubbed`, `pseudonymised` or `reference_only`). For `delegated`: the owner states the mode; there is no default. |

#### 12.1.4 New top-level arrays (empty by default)

These v0.3.0 top-level fields are absent in v0.2.x and migrate as empty:

`segment_markers[]`, `policy_triggers[]`, `lineage[]`, `redactions[]`. The exception is `redactions[]`, which holds the records (§8.1) of any redaction applied at migration under §5.2 or §12.1.3. Optional scalars `content_hash`, `schema_uri` remain unset, except that the migrating party MAY set `content_hash`, computed last over the v0.3.0 output (§3.4).

#### 12.1.5 Field preservation

All other v0.2.x top-level fields (`profile`, `scenario_id`, `quality`, `title`, `summary`, `classification`, `provenance`, `flags`, `environment`, `operational_context`, `timing`, `condition`, `coordination`, `handover`, `outcome`, `metrics`, plus their nested structure) pass through unchanged, within the exceptions §3.1 lists (for example, second-precision timestamps are padded, §3.5). v0.3.0 producers MUST NOT strip them; v0.3.0 consumers MUST preserve them on re-serialisation. Members of the items of `turns[]`, `tool_calls[]` and the other arrays pass through the same way (§3.1), within the same exceptions.

The v0.2.x `Annotation.scope.type` value `handover` and the v0.2.x `ToolCall.spawned_agent` payload likewise pass through unchanged. v0.3.0 introduces `delegates-to` / `delegated-by` lineage edges (§7.1) as an OPTIONAL way to represent multi-agent relationships at trace-graph level; producers MAY use them in addition to or instead of `spawned_agent`, but v0.3.0 does not deprecate the v0.2.x form. A migrating party is not such a producer: in the output of a migration `spawned_agent` passes through unchanged and `lineage[]` is empty (§12.1.4); a consumer can append an edge to the migrated trace afterwards (§3.1).

#### 12.1.6 Migration completion criterion

A v0.2.x trace becomes a valid v0.3.0 `agent-session` trace when:

1. The mechanical reformatting in §12.1.1 has been applied.
2. The `operator` object in §12.1.3 has been supplied by the trace owner.
3. The migrating party has brought the copy within §3.5 (timestamp form, precision and leap seconds), §3.4 (numeric domain, depth limit, JSON form) and §4.8.1 (`tool_calls[].input.arguments` an object or null).

A v0.2.x trace carrying a value outside a v0.2.x enumeration that v0.3.0 keeps closed (v0.2.0 treats such values as validation errors) is not made conformant by these steps, which change no such value, nor is one missing a v0.2.x minimum field (§3.1), which these steps do not add, nor is one carrying a timestamp that is not calendar-valid other than a source leap second, `23:59:60` (§3.5), which these steps do not replace.

The original v0.2.x input is preserved unchanged. The v0.3.0 output is the result of the above, of any change §12.1.2, §12.1.3, §12.1.4 or §12.1.5 permits, and of any redaction §5.2 permits at migration time, applied to a copy.

---

## 13. JSON Schema

The machine-readable schema is published alongside this specification as three files:

- `minitrace-v0.3.0.base.schema.json` — common fields for all trace types
- `minitrace-v0.3.0.agent-session.schema.json` — agent-session extensions
- `minitrace-v0.3.0.judge-session.schema.json` — judge-session extensions

The type-specific schemas carry requirements of their own: the agent-session schema requires at least one turn, and the judge-session schema requires a `lineage` array containing at least one `judges` edge.

A trace is conformant if and only if it validates against the base schema plus the appropriate type-specific schema and meets every requirement this text places on a trace. The schemas check a subset of those requirements: a trace that validates against them can still be non-conformant, for example through a human operator whose `operator_aware` is not a boolean (§5.4), a string-valued `input.arguments` (§4.8.1), a number outside the numeric domain (§3.4) or a missing v0.2.x minimum field (§3.1). Validators MUST accept every conformant trace. For an unknown `trace_type` (§3.2), the appropriate type-specific schema is the one at its `schema_uri`; a validator that does not apply that schema checks the base schema only, a subset in the sense above.

---

## 14. Security Considerations

This section identifies security properties consumers and producers should be aware of regarding minitrace traces. The trace format itself is data, not code; the risks below arise when traces are loaded, validated, displayed, or used as input to other systems.

### 14.1 Untrusted content in trace fields

All string fields in a minitrace trace are untrusted data. Trace content originates from arbitrary harness output, operator input, and tool output, none of which is controlled by the trace consumer. Consumers MUST sanitise field values for their rendering context before display or processing:

- HTML-escape for web dashboards.
- Parameterised queries for SQL (including DuckDB).
- Safe escaping for shell or terminal contexts.

Fields including (but not limited to) `tool_call.input`, `tool_call.output`, turn content, annotation text, and `operator_input` may contain shell commands, code, markdown, or terminal control sequences that could trigger injection or UI spoofing if rendered unsafely.

`tool_call.input` content MUST NOT be executed by consumers without explicit user consent and sandboxing. Path-like fields (file references, source paths) are inert strings and MUST NOT be dereferenced automatically by consumers.

Open-ended object fields (vendor extensions under `ext:*`, experimental fields under `x-*`, and free-form metadata containers) may contain arbitrary nested data within the depth limit of §3.4. Consumers processing traces from untrusted sources SHOULD validate or strip such fields before use; a consumer that re-serialises the trace still preserves them (§3.1).

### 14.2 Schema URI fetch behaviour

§3.2 permits validators to fetch a `schema_uri` referenced by traces with unknown `trace_type`, and instructs them to accept the trace as valid-but-uninterpreted with a warning if the URI is unreachable. This is pragmatic for interoperability but introduces two risks:

- **Server-side request forgery (SSRF) and side-channel signalling.** A malicious producer can emit any URI; a validator that fetches it leaks its own network position and may be coerced into requesting attacker-chosen internal hosts.
- **Type-validation downgrade.** A producer can deliberately point to an unreachable URI and have its trace accepted as base-valid only, bypassing type-specific constraints.

Consumers needing strong type validation MUST configure offline schema sources and MUST NOT rely on URI fetch as a security control. Consumers performing schema fetch SHOULD restrict the fetcher to an allowlist of known schema hosts, time-box requests, and refuse private-network destinations.

### 14.3 Hash and integrity assumptions

`content_hash` (§3.4) provides integrity over the trace's canonical form at hash-computation time. It does not provide:

- **Authentication of the producer.** Anyone with the canonical content can compute a matching hash. Producer authentication is an envelope concern.
- **Freshness.** A hash matching an old snapshot does not prove the consumer is seeing the current state.
- **Tamper-evidence against a colluding producer.** A producer who modifies the trace and recomputes the hash leaves no in-trace evidence.
- **Cross-implementation reproducibility on out-of-range numbers.** §3.4's canonical-form reproducibility rests on the RFC 8785 / ECMA-404 IEEE 754 binary64 numeric model. Integers outside the inclusive range [−(2^53−1), 2^53−1] are not all exactly representable in binary64, and conformant RFC 8785 implementations diverge legitimately on such inputs (e.g. Python `rfc8785` 0.1.4 raises `ValueError`; Rust `serde_json_canonicalizer` 0.3.2 silently quantises through f64). §3.4 therefore excludes every number of that magnitude as a JSON number, whatever its notation: producers MUST string-encode them, and a trace carrying one as a number is not conformant. Because such traces exist in the wild, consumers SHOULD NOT treat a `content_hash` mismatch on traces carrying out-of-range numbers as evidence of tampering without first ruling out verifier-implementation divergence.

Consumers verifying a `content_hash` MUST recompute it themselves; trusting a producer-supplied hash without recomputation provides no integrity guarantee. Cross-trace hash references (§7.1) inherit these limitations.

### 14.4 Annotation signatures (placeholder)

§6.8 permits annotations to carry an `author_signature` field. v0.3.0 does NOT normatively profile a signature scheme, key format, or verification procedure. Until such a profile exists, `author_signature` is a free-form opaque string and MUST NOT be relied upon as proof of annotation authorship by any consumer claiming evidence-grade properties. Whoever writes an annotation (§6.8) SHOULD NOT set `author_signature` in v0.3.0 traces unless they are operating under a private profile understood by all consumers in their deployment.

A normative signature profile is deferred to a future revision.

### 14.5 Redaction integrity

The `redactions[]` array (§8.1) is itself NOT redactable in v0.3.0. This is deliberate: the redaction log is integrity-relevant content forever, because removing or altering a redaction record after the fact would conceal what was removed and why. Producers and intermediate processors MUST treat `redactions[]` as append-only. A redaction record, once appended, is final: no later record neutralises, amends or reverses it, and no later record restores content an earlier one changed. A later record MAY redact an already-redacted target further (§8.2); it does not thereby neutralise the earlier record. An error in a record, or in the redaction it records (a wrong `reason_code`, authorisation or target, for example), is corrected by producing a new trace from the unredacted source, not by changing `redactions[]`; the new trace MAY carry a lineage edge of type `x-supersedes` to the old one (the reserved name `supersedes`, §7.1, in its `x-` form until adopted). Where the unredacted source is no longer held, the record stands.

The OPTIONAL `original_content_hash` field on a redaction record (§8.1) names the pre-redaction content. When present, it provides a recovery vector against low-entropy redacted content: a party with the hash and a candidate plaintext can verify a guess. Producers SHOULD omit `original_content_hash` for redactions covering predictable, low-entropy content (short strings, enumerated values, well-known identifiers); it is appropriate where the redacted content is high-entropy and the recovery threat is acceptable.

### 14.6 Trace producer is in the trust boundary

A consumer of a minitrace trace inherits the trust assumptions of its producer. The format provides structure and integrity primitives but not a way to detect a fabricating, omitting, or selectively-emitting producer. Deployments requiring stronger guarantees MUST place the producer inside their trust boundary or rely on the envelope layer (sibling spec) for producer attestation.

---

## 15. Privacy Considerations

§5 covers operator-side privacy: consent, capture mode, legal basis, and the operator-aware flag. This section addresses privacy concerns that arise from data inside session content rather than from operator identification.

### 15.1 PII embedded inside session content

A trace can contain personal data unrelated to the operator's identity: pasted email threads, customer names in tool input, addresses in retrieved documents, identifiers in API responses. The `pii_detected` reason code in §8.3 names this category but the specification is silent on detection responsibility.

For v0.3.0:

- **Detection is a producer-side responsibility.** No consumer can be expected to detect PII inside arbitrary content reliably; producers are responsible for deciding whether to emit content that may contain PII; if they do, they MUST either apply redaction (§8) before emission or set the trace's classification (§6.5) accordingly.
- **Detection timing is unspecified.** A producer MAY perform detection at capture time, post-capture before storage, or both. The trace records only the resulting redactions; detection method and timing are out of scope.
- **Residual risk after redaction is a property of the redaction operation chosen.** `pseudonymise` and `mask` (§8.2) leave structure visible; `remove` replaces content with null or an empty value, leaving the identifying and ordering members, and in a whole event or annotation any member that admits neither, in place. Producers selecting the operation SHOULD weigh re-identification risk for the specific content category.
- **Redaction cannot hide event times or identifiers.** The members that identify or order an event or annotation stay unchanged under every redaction (§8.2), so a redacted trace still shows when each event or annotation happened and what each identifier is, which for a human operator can itself be sensitive (§15.4). Producers SHOULD NOT put personal data in identifiers, and SHOULD coarsen timestamps at production, within the §3.5 form, where the timing itself is sensitive, setting `ordering` (§4.3) where coarsening would tie events whose order matters.

Consumers receiving traces SHOULD treat the absence of `pii_detected` redactions as "the producer did not detect or did not redact", not as "no PII is present".

### 15.2 Scope of the format's privacy primitives

This specification provides privacy-relevant primitives — typed redaction (§8), classification (§6.5), and the operator capture-mode and capture-purpose fields (§5) — but does not define a compliance regime and makes no determination about any legal obligation. The operator capture and legal-basis fields (§5.3) cover operator data collection; they do not by themselves address downstream processing of PII embedded in session content (§15.1). Whether and how these primitives are used to satisfy any given legal or regulatory requirement is a deployment responsibility, outside the scope of this specification.

### 15.3 Redaction and withdrawal propagation

Redacting or withdrawing a trace after it has been referenced has cross-trace consequences. If a trace referenced by lineage edges (§7.1) is redacted, extended by an addition §3.1 permits other than setting `content_hash`, or withdrawn, downstream traces that reference its `target_content_hash` no longer match its current state: the reference points at the prior hashed snapshot, not the new one. The format provides the references but not a propagation mechanism, so operators of multi-trace deployments need to account for this when redacting or removing a referenced trace. A trace bundled in an envelope is in the same position towards the envelope's manifest: once the copy in the envelope is redacted or extended by an addition §3.1 permits, its stored bytes no longer match the hashes the manifest lists for it, and, unless the addition set `content_hash`, neither does its `content_hash` once recomputed. What follows for the manifest and its signature is the envelope specification's matter (envelope §9.3).

Within an immutable storage layer, removing content requires either copy-on-write republication (withdrawing the prior trace) or content-addressed deletion propagated through downstream caches; v0.3.0 does not mandate either approach. The `gdpr_erasure` reason code (§8.3) is available for recording that a redaction was made in response to an erasure request; performing the redaction is a producer or operator responsibility, not defined here.

### 15.4 Operator-aware deployments

The `operator.operator_aware` flag (§5.4) is a factual assertion by the producer about whether the operator was informed of capture. Its value constrains which downstream uses are defensible: deployments SHOULD treat `operator_aware: false` traces as restricted to internal incident analysis, not as inputs to systems that affect the operator's terms of work, evaluation, or compensation. This is the format's main worker-monitoring-adjacent surface; it is named explicitly to make the dual-use risk visible at the schema level rather than leaving it to policy.

---

## Appendix E: Changes from v0.2.x

### E.1 Breaking changes

The only structural addition that requires user input during migration is the `operator` object. Everything else is mechanical, additive, or brought in by the migrating party (§12.1.6, step 3).

| Change | Migration |
|---|---|
| `schema_version` value format changes from `minitrace-v0.2.0` to `0.3.0` (field already required in v0.2.x) | Automated reformat |
| `trace_type` field added and required | Automated: default to `agent-session` |
| `operator` object added and required at top level | REQUIRES user input: `operator.type`, `operator.operator_aware` (when type is `human`), and `operator.capture_mode` decision per §12.1.3 |
| Human operators with `capture_mode: full` require `operator.capture_purpose` | REQUIRES user input |
| Unknown `trace_type` values require a non-null `schema_uri` | Only affects types other than `agent-session` and `judge-session`; those two do not require it |
| All timestamp fields MUST emit microsecond precision (§3.5) | v0.2.x traces with second-precision timestamps MUST be padded with `.000000` during migration; truncation MUST be used for sub-microsecond sources; a timestamp that is not calendar-valid, other than a source leap second, is not made conformant (§12.1.6) |
| Every tool call's `input.arguments` is an object, or null when the source carries none that can be read as one, as v0.2.x typed and defaulted it, now enforced (§4.8.1) | The migrating party brings the copy within it (§12.1.6, step 3) |
| Values lie within §3.4's numeric domain, JSON form and depth limit | The migrating party brings the copy within them (§12.1.6, step 3) |

### E.2 Additive and relaxing changes (no migration required)

| Addition | Backwards compatible |
|---|---|
| `segment_markers[]` top-level array (§4.5) | Yes; new array, empty by default |
| `policy_triggers[]` top-level array (§4.6) | Yes; new array, empty by default |
| `lineage[]` top-level array (§7) | Yes; new array, empty by default |
| `redactions[]` top-level array (§8) | Yes; new array, empty by default |
| `content_hash` top-level field (§3.4) | Yes; optional unless required by edge integrity |
| `schema_uri` top-level field (§3.2) | Yes; conditional on unknown `trace_type` |
| `trace`, `event_range` and `segment` annotation scopes (§6.1) | Yes; new scope values |
| `source.type` field on annotations (§6.2) | Yes; optional, coexists with `annotator` |
| `claim_type` field on annotations (§6.3) | Yes; optional |
| `taxonomy[]` flattened form (§6.7) | Yes; producers MAY emit v0.2.x `taxonomy_mappings`, v0.3.0 `taxonomy[]` or both; consumers MUST accept either or both |
| `invocation_source` field on tool calls (§4.7) | Yes; optional |
| Cross-array event ordering rule (§4.3) | Yes; derived on read; does not alter storage |
| Reserved namespaces (`x-aggregate-*`) | Yes; previously unreserved |
| `ordering` field on events (§4.3) | Yes; optional tie-break hint |
| `authored_at` and `author_signature` fields on annotations (§6.8) | Yes; optional, set when an annotation is written |
| `flags.wraps_inner` (§7.5) | Yes; required only of producers following the harness-wrap shape |
| `tool_output` accepted as a legacy `tool_calls[].output.content_origin` value (§4.8.3) | Yes; a value v0.2.x rejected is now accepted |
| No `metrics` key is required (§3.1) | Yes; the v0.2.x Required status of `metrics.turn_count` and `metrics.tool_call_count` is withdrawn |
| `provenance.converted_at`, `provenance.converter_version`, `timing.duration_seconds` no longer required (§3.1) | Yes; a producer SHOULD carry them; consumers MUST NOT assume them |
| `profile` an open value set (§3.1) | Yes; `controlled` and `organic` keep their meaning; other values were validation errors in v0.2.0 |
| `timing.privacy_level` an open value set (§3.1) | Yes; `full`, `anonymous` and `minimal` keep their meaning; other values were validation errors in v0.2.0 |
| `coordination.human_attention` and `quality` open value sets (§3.1) | Yes; `focused`, `divided`, `unknown` and `A` to `D` keep their meaning; other values were validation errors in v0.2.0 |
| `harm_category` on `harm_linkage` annotations (§9.1) | Yes; new member |
| Near-miss vocabulary: the `near_miss` category and its `near_miss_*` members (§9.4) | Yes; new values and members |
| Extension namespaces: `x-` field names (§10.1) and `ext:<vendor>:<name>` (§10.2) | Yes; new naming rules |

### E.3 Items NOT changed from v0.2.x

To make the additive nature of v0.3.0 explicit, the following v0.2.x elements remain in their v0.2.x shape and semantics, except as §3.1, §4.8, E.1 and E.2 state:

- All top-level fields not listed above as new: `id`, `profile` (an open value set, §3.1), `scenario_id`, `quality` (an open value set, §3.1), `title`, `summary`, `classification`, `provenance` (`converted_at` and `converter_version` no longer required, §3.1), `flags`, `environment`, `operational_context`, `timing` (`duration_seconds` no longer required; `privacy_level` an open value set, §3.1), `condition`, `coordination` (`human_attention` an open value set, §3.1), `handover`, `outcome`, `metrics` (its Required keys withdrawn, §3.1).
- The `turns[]` array shape, including the `index`-based addressing.
- The `tool_calls[]` array at top level, including `emitting_turn_index`, with §4.8.1's argument shape and §4.8.3's accepted value.
- The v0.2.x `Annotation.scope.type` enum values (including `handover`), `Annotation.annotator`, `Annotation.content.category`, and `Annotation.taxonomy_mappings` (alternative form to v0.3.0 `taxonomy[]`).
- The `ToolCall.spawned_agent` payload.

v0.3.0 producers MUST NOT strip these on output, except that a migrating party MAY replace an annotation's `taxonomy_mappings` by its flattening (§3.1); v0.3.0 consumers MUST preserve them on re-serialisation.

---

*End of v0.3.0.*
