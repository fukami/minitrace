# minitrace-adapter-dsl Specification v0.1.0

**Status:** Release.
**Date:** 2026-09-25 (RC 3); last amended 2026-10-29.
**Predecessor:** v0.1.0 RC 2.
**Companion to:** `minitrace-spec` v0.3.0 (trace format), `minitrace-envelope-spec` v0.1.1 (envelope format).
**Revision note:** RC 3 defines cross-trace placeholders by location instead of by shape. A placeholder may appear only as the whole value of `lineage[].target_content_hash`; placeholder-shaped text anywhere else is content, and a non-null `target_content_hash` must be a placeholder (before publication) or a well-formed hash (§13.1). The producer-side token scan of §14.7 no longer licenses validators to reject traces. The placeholder change leaves every RC 2 adapter valid. An RC 2 adapter that calls `parse_iso`, relies on a YAML merge key or on YAML 1.1 typing, or uses any construct RC 3 makes structural is rejected.
**Approach:** This DSL spec is written against shipped harness adapters. It was first written to express exactly the four adapters that had shipped then (atbench-claw, atbench-codex, mast-full + mast-human, otel-openinference/halo), and it defines no more than shipped adapters need. The DSL grows only when a new adapter cannot be expressed in the existing vocabulary (convention absorption, §16).

---

## 0. Summary

An adapter is a declarative, versioned mapping from one specific harness output format to canonical minitrace v0.3.0. v0.1.0 adapters are expressed as YAML files with a fixed top-level structure, content selectors over input records, and an ordered emit-rule list that produces canonical events.

v0.1.0 covers:

- A single input format per adapter (JSONL or JSON).
- Per-record dispatch on a discriminator field or shape predicate.
- Per-block iteration inside multi-block records.
- Stateful accumulation across records (current-model, tool-call ID map, last-turn index).
- Direct emission of turns and tool calls; attachment of subsequent records to prior tool calls.
- Static and computed scalar fields on the resulting trace.
- Final-pass derivation of metrics and timing from accumulated events.
- **Multi-trace emission**: each container (or the whole input) builds one trace per declared shape (typically agent-session + judge-session pairs; §14.2 states when one is not built or not written) linked by cross-trace placeholders, with deterministic post-emission resolution (§13, §14).
- **Container-mode iteration**: each top-level container builds one trace per declared shape (§14.2); record-level iteration runs inside the container. Deep-path `record_path` and the `$self` sentinel cover atomic-record corpora (§4.1).
- **Producer-side discipline checks**: emitted traces MUST NOT contain unresolved DSL interpolation tokens, apart from tokens that come from the corpus itself; producers SHOULD run a post-emission scan for the forbidden token classes (§14.7).

v0.1.0 does NOT cover:

- Multiple input files combined into one trace (deferred to a future revision).
- Binary or proprietary input formats requiring a parsing module.
- Conditional output schema selection (declared trace shapes are statically determined; every declared shape is built for every container, §14.2).
- Imports or inheritance between adapter DSL files.
- Schema validation rules — those are the trace JSON Schema's job, applied to the output.

---

## 1. Scope

### 1.1 What an adapter DSL file is

A YAML file declaring how a specific version of one harness's output is mapped to a v0.3.0 minitrace trace. Filename convention: `<harness>/<harness-version>.dsl.yaml`. Example: `pi/0.58.1.dsl.yaml`.

### 1.2 What it is not

- Not a runtime configuration. The DSL describes a transformation; a separate interpreter executes it.
- Not a rewriter for v0.2.x → v0.3.0 traces. That migration is governed by `minitrace-spec` §12 and is mechanical without a DSL.
- Not a place to express envelope assembly. The DSL produces a trace; envelope assembly is a separate step (per `minitrace-envelope-spec`).

### 1.3 Three-layer architecture (cross-reference)

Per `minitrace-spec` §1.3, adapters are one of the three layers (trace, envelope, adapter). This DSL specification is the binding language for the adapter layer.

**Conventions.** The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in BCP 14 (RFC 2119, RFC 8174) when, and only when, they appear in all capitals, as shown here.

---

## 2. Top-level structure

A DSL file is a YAML 1.2 document, read with the YAML 1.2 core schema: plain scalars are typed by that schema alone, so `no`, `on` and `off` are strings, `2.10` is the number 2.1 only when unquoted, and nothing is read as a date. A DSL file with a duplicate key in any mapping, a non-string mapping key, or an explicit tag (`!!binary`, `!!set`, `!!timestamp` and the like, and the non-specific tag `!`) is invalid: a runner MUST reject it at load, as a structural failure (§8.3). The same holds for a DSL file that uses an anchor or an alias, or that is a stream of more than one document. A `<<` key is an ordinary mapping key: the YAML 1.2 core schema has no merge keys, and a runner MUST NOT merge on it. A number anywhere in a DSL file, and a number constant inside an expression, MUST lie in the numeric domain of `minitrace-spec` §3.4. That domain excludes a number whose binary64 value lies outside ±(2^53−1), in any notation (`1e16` as well as `9007199254740992`), `.inf`, `.nan`, and a number that overflows binary64 such as `1e400`. A number outside the domain is a structural failure at load. A plain scalar that the core schema reads as a number MUST be written as a JSON number (RFC 8259 §6: an optional leading `-`, an integer part without leading zeros, an optional fraction with at least one digit after the point, an optional exponent), which is the notation §8.3 requires of a number constant with an optional leading `-`; `0x10`, `0o7`, `+1`, `.5`, `1.` and `007` are structural failures at load. Likewise a string anywhere in a DSL file, a mapping key included, MUST be one an I-JSON message can hold (`minitrace-spec` §3.4): a string holding an unpaired surrogate or a noncharacter, which a YAML escape such as `"\uFFFF"` can write, is a structural failure at load.

A DSL file is a YAML document with the following top-level keys:

```yaml
adapter:          # REQUIRED. Identity, versioning, source-format binding.
input:            # REQUIRED. Input format and record-level shape.
state:            # OPTIONAL. Named accumulators initialised before scanning.
constants:        # OPTIONAL. Static values referenced from emit rules.
session:          # REQUIRED unless `sessions` is present. Static and per-record session-level field map.
sessions:         # Multi-trace alternative to `session` (§14.2); exactly one of the two is present.
operation_types:  # OPTIONAL. Tool-name to operation-type table (§10).
emit:             # REQUIRED. Ordered list of emit rules applied per input record.
finalize:         # OPTIONAL. Post-scan computation of derived fields.
```

Order is significant for `emit`: rules are applied in order, and every rule whose `when` predicate matches fires (§8). The order of the top-level keys among themselves is not significant; §7 and §12 state the order within `session`, each `sessions` shape and `finalize`, and §14.5 that the order of the shapes is not significant. This version defines no other top-level key; a runner MAY warn about one and MUST NOT let it affect the output. The YAML rules of this section and the placeholder-location rule of §8.4 still apply to the whole file, such keys included.

---

## 3. `adapter` block

```yaml
adapter:
  name: <string>                 # e.g. "pi"
  harness_version: <string>      # e.g. "0.58.1"
  dsl_version: "0.1.0"           # which DSL version this file conforms to
  source_format: <string>        # opaque identifier, available as $adapter.source_format
  converter_version: <string>    # opaque identifier, available as $adapter.converter_version
  produces_schema: "0.3.0"
```

`dsl_version` MUST be present. Interpreters MUST refuse to load a file whose `dsl_version` they do not understand.

`source_format` and `converter_version` are available to mappings as `$adapter.source_format` and `$adapter.converter_version`; a runner writes neither into the produced trace on its own, and an adapter maps the `provenance` fields it wants (for example the `derived` key `provenance.converter_version: "$adapter.converter_version"`).

---

## 4. `input` block

```yaml
input:
  type: jsonl | json
  # For type: jsonl — input is one record per line.
  # For type: json — input is one root document; if the root is an array, each element is a record.
  encoding: utf-8                 # OPTIONAL; utf-8 is the only value and the default
  container:                      # OPTIONAL; see §4.1
    record_path: <field name>
```

**Encoding.** An input is read as UTF-8. `encoding`, where present, MUST be `utf-8`; any other value is a structural failure at load (§15). An input in another encoding is converted to UTF-8 by a pre-processor before the runner reads it, as an input whose structure the DSL cannot walk is first restructured by one (the ChatGPT export, §4.1). A UTF-8 byte-order mark (the bytes `EF BB BF`) at the very start of the input is not part of it: the runner ignores it and does not report it, and reads a `json` input, or the first `jsonl` line, as if the mark were absent. Anywhere else U+FEFF is an ordinary character: inside a string or a member name it is kept, and outside one it is not JSON whitespace, so the line or the input that holds it is not well-formed and is treated as §15 states (Unreadable and empty input).

**Numbers in the input.** A number in the input that lies in the numeric domain of `minitrace-spec` §3.4 is read as its nearest binary64 value. A number the trace could not hold as a number is read as a string holding its source text exactly as written, so `12345678901234567890` is read as `"12345678901234567890"`. Such a number is either a number whose binary64 value lies outside ±(2^53−1), in any notation (so `1e16` is read as `"1e16"`), or a number whose nearest binary64 value is infinite. Nothing is lost in reading, and the trace carries such a number as a string, as `minitrace-spec` §3.4 requires, except where it lands in a field the `minitrace-spec` base schema types as a number or an integer (see "Numeric fields in a built trace" below). One such number neither makes a record unreadable nor makes the input unreadable. Arithmetic on the resulting string is an input failure, like arithmetic on any string (§8.3). A pre-processor that prepares input for the runner MUST read numbers the same way.

**Nesting depth.** The depth of a JSON value is 0 for a scalar and, for an array or object, one more than the deepest of its members, so `[]` and `{}` have depth 1 and `{"a": [1]}` has depth 2. The *level* of a value inside a root value is the number of arrays and objects that enclose it, so the root is at level 0. Depth is limited by truncation, not by dropping: to bring a root value within depth 64, a runner replaces every array or object at level 64 with null, and reports each replacement as an input failure (§8.3) naming where it was found and the path: for a value in a record, the record as §8.3 names one (in container mode, §4.1, with its container); in container mode, for any other value of a top-level element, its container; for a `json_parse` value, the input its expression was evaluated on (§8.3); and for a trace it builds, that trace. The runner applies this to every record (in container mode, to every top-level element), to every value `json_parse` returns, and to every trace it builds, before cross-trace resolution. A record within the limit is used unchanged, and no record, container or trace is dropped for its depth, so one deep value costs only itself. Every trace a runner emits therefore has depth at most 64 (`minitrace-spec` §3.4). To truncate, a runner must be able to read the value first: it MUST read records, top-level elements and `json_parse` values up to depth 512. One deeper than 512 is unreadable in every runner: such a record or top-level element is skipped and reported, the other records are converted, and such a `json_parse` value yields null and is reported. A runner reports each replacement and each skipped record, element or `record_path` item, except that past an implementation-defined bound, per source or per record, it MAY report the remaining ones as one summary naming their count and the first affected input, named as a replacement is. The first affected input is the earliest in input order among the inputs of all the findings the summary stands for, of whatever kind and whichever part of the runner made them; it is not the input of whichever finding the runner happened to leave unreported first. Input order is the order of the top-level elements (§4.1) and, within one, of its records (in container mode by their position in the `record_path` list, §4.1.1). For a finding with no such position (one named by the input as a whole, by a container apart from its records, or by a built trace), and for which path is named when several of those findings lie in the earliest input, this specification fixes no order. The summary is itself an input failure, whose loss class §15 states. A duplicate key in the input as written, and bytes not valid UTF-8, are named otherwise than a replacement, whatever value they lie in: in a `json` input, which is read as one document, by the input; in a `jsonl` input, whose lines are read one at a time, by the line, as its record or, in container mode, its container.

**Numeric fields in a built trace.** A value that a field of the `minitrace-spec` base schema typed as a number or an integer cannot hold can reach that field, for example a number the reader kept as its source text. After the depth pass, and before R5 and cross-trace resolution (§13.2), the runner checks every trace it builds:

- If a turn's `index` is not its position in `turns[]` (`minitrace-spec` §4.1), whatever the value (text, a boolean, a number outside the numeric domain, a gap or a repeat, or no `index` at all), or if an item of `turns[]` is not an object, the trace is not emitted. The runner reports it as a set-level loss (§15), naming the trace and the path `/turns/<i>/index`. Such a trace still counts for R5's unique-id rule (§13.2), so a trace sharing its id is not emitted either, and a placeholder naming that id is unresolvable (R4).
- Any other such field whose value is not a number, is a number outside the numeric domain, is a number whose value is not whole in a field typed as an integer, or lies outside the bounds the base schema gives that field (a negative count, a proximity above 1) is set to null where the base schema admits null for that field; where it does not (the `metrics` counts typed as integers only), the member is omitted. Each is an input failure (§8.3) naming the trace and the path. A null in such a field that does not admit null is omitted and reported as an input failure naming the trace and the path.

**Objects in the input.** JSON objects are unordered (RFC 8259 §4), and the DSL does not depend on the order in which an object's members are written: the one construct that turns an object into a sequence, `dict_items` (§9.1), orders by key. If an object in the input, or in a value `json_parse` reads, names the same key more than once, the last value wins. The runner keeps the record and reports the duplicate in the conversion's diagnostics, naming, for a duplicate in the input as written, the input or the line as stated at the end of "Nesting depth" above, and, for one in a value `json_parse` reads, where it was found as a depth replacement is named. A pre-processor MUST read objects the same way.

**Strings in the input.** An emitted trace is an I-JSON message (`minitrace-spec` §3.4), so it holds no unpaired surrogate and no noncharacter. A runner replaces each unpaired surrogate and each noncharacter in an input string, in an input member name, and in a value `json_parse` reads, with U+FFFD; the rest of the string is kept. It reports each affected string or member name once, as an input failure (§8.3) naming where it was found as a depth replacement is named (above), the path (for a member name, the path to the member under its repaired name), whether the member name or its value was repaired, and the number of replacements in it: a path cannot tell characters within one string apart. If replacement makes two member names of one object equal, the last value wins and the runner reports it, named as the repair is. The reporting bound stated under nesting depth above applies to these reports as it does to depth replacements, counting affected strings and member names. A pre-processor MUST read strings the same way.

**Bytes in the input.** Bytes that are not valid UTF-8 do not make the input, or a line of it, unreadable (§15). A runner replaces them with U+FFFD and keeps the rest. Each maximal subpart of an ill-formed subsequence becomes one U+FFFD (the Unicode Standard, section 3.9, "U+FFFD Substitution of Maximal Subparts"; the UTF-8 decoder of the WHATWG Encoding Standard does the same), so `FF FE` becomes two, a sequence cut short such as `E2 82` becomes one, and a surrogate encoded as bytes, `ED A0 80`, becomes three. The replacement is made before the text is parsed, and the result is parsed as any other text: where the bytes lay inside a string or a member name, it holds the U+FFFD (two member names made equal this way are a duplicate key, "Objects in the input"), and where the result is not well-formed JSON, the line or the input is treated as §15 states. A runner reports each replacement as an input failure (§8.3), named by the input or the line as stated at the end of "Nesting depth" above; the reporting bound stated there applies, counting replacements. Where the input as a whole is then unreadable (§15), reporting it as unreadable is enough. A surrogate written as a JSON escape (`\ud800`) is not such bytes: it is an unpaired surrogate under "Strings in the input". A pre-processor that reads UTF-8 input MUST read its bytes the same way, a byte-order mark at the very start of that input included ("Encoding", above). How a pre-processor reports what it reads the same way as the runner (numbers, objects, strings and bytes), and what a pre-processor owes that converts from another encoding or reads a source that is not JSON, are outside this specification; a source-fidelity profile may define them. Until one does, a loss a pre-processor causes, whether it repairs or drops what it read, does not appear in the conversion's diagnostics, and §15's set-level signal covers only what the runner itself reads.

v0.1.0 supports exactly these two input types. Adapter authors needing other formats (SQLite, ZIP-of-JSON, multi-file bundles) MUST wait for a future revision.

### 4.1 `input.container`

A *top-level element* is an element of a `json` input's root array, or a line of a `jsonl` input that is not blank (§15). When present, each top-level element of the input that is not skipped as unreadable (§15) becomes one trace per declared trace shape (§14.2); the records to scan for that trace come from the named field (`record_path`) within the element.

This is container mode. Examples:

- claude.ai web export: top-level array of conversations, each conversation has `chat_messages` to scan as records.
- ChatGPT export: top-level array of conversations, each with `mapping`, an object whose members form a parent/children tree rather than a list in conversation order, so an adapter for it needs a pre-processor that flattens it to a list first.

```yaml
input:
  type: json
  container:
    record_path: chat_messages
```

Behaviour:

- Top-level input MUST be a JSON array (or a JSONL where each line that is not blank is one container — equivalent semantics). A `json` input whose root is not an array cannot be read in container mode: the conversion fails, writing no trace, and reports the input as unreadable, naming the JSON type of its root as written (§15, Unreadable and empty input).
- For each container in the array that is not skipped as unreadable (§15), the runner produces one trace per declared trace shape.
- Inside each trace's emit context, expressions can reference `$container` (the whole container object, e.g. one conversation) in addition to `$record` (the current message inside `chat_messages`).
- `$record_first` resolves to the first record within the current container, NOT the first container.
- `$container_first` resolves to the first container in the input (rare; useful for input-wide metadata).

When `container` is absent, the input produces exactly one trace per declared shape from the input's records, or none in the cases §14.2 names.

#### 4.1.1 Deep-path `record_path`

When `input.container.record_path` is a dotted path (e.g. `"trajectory.events"`), the runner performs a deep lookup with dot-separated keys. Each segment is applied to the value reached so far: at an object it is a key, including a segment of digits; at a list it is an index if it is the canonical form of a non-negative integer (ASCII digits, no sign, no leading zero, as an array index in a JSON Pointer, RFC 6901 §4), counting from 0. A missing key, an index past the end, any other segment at a list, and a segment applied to a string, a number, a boolean or null produce `null`, unreported, which the runner treats as an empty record set. This generalises the single-key form (`record_path: "chat_messages"`) used by single-level container formats. A value at `record_path` (other than `$self`, §4.1.2) that is neither a list nor null (an object, a string, a number or a boolean) is an input failure (§8.3) for that container: as for a null, the container yields no records and its traces are built from none, and the runner reports it, naming the container and the `record_path`. An item of the list at `record_path` that is not an object (a string, a number, a boolean, a list or null) is skipped as unreadable (§15), and the other items are the container's records. A container whose every item was skipped yields no records, and its traces are built from none. A skipped item keeps its place in the numbering of records: every report that names a record of a container by its position, whatever it reports, gives the record's index in the `record_path` list, counting from 0 and counting skipped items. That is the number a report gives a record, not which record `$record_first` is: a skipped item is never `$record_first` (§7). The runner reports each skipped item, naming that position and the container. These skips count toward the per-source reporting bound of §4 together with the reader's other findings; past the bound they are counted in a summary, which is then a set-level loss (§15).

```yaml
input:
  type: jsonl
  container:
    record_path: trajectory.events    # deep lookup: container["trajectory"]["events"]
```

#### 4.1.2 Atomic containers — `record_path: "$self"`

When `input.container.record_path` is the string `"$self"`, the runner iterates once per container with the container itself as the singleton inner record. This is required for adapters where each top-level array element is a unit (no inner events list) — for example, MAST records carrying a single `trace.trajectory` string and a flat `mast_annotation` block, or per-trace containers carrying a `conversation_messages` list rebuilt by a pre-processor.

The `$self` sentinel is distinct from a dotted path; runners MUST treat it as a literal string match, not a deep-get lookup.

```yaml
input:
  type: jsonl
  container:
    record_path: "$self"     # the container IS the record; iterate once
```

---

## 5. `state` block (OPTIONAL)

```yaml
state:
  current_model: null
  next_turn_index: 0
  pending_tool_calls: {}
  ...
```

State is a named map of values mutated as input is scanned. Each key has an initial value. Values are ordinary YAML scalars/lists/maps. State is initialised afresh from this block for every trace the runner builds: for every container, or once for the whole input when there is no container, and for every declared trace shape (§14.2). Only the rules that target that shape see and change it, and nothing carries over from one trace to another.

State values are referenced from emit rules as `$state.<key>` and may be mutated by the `set` action (§8.2.1).

---

## 6. `constants` block (OPTIONAL)

```yaml
constants:
  default_capture_mode: scrubbed
  pii_in_paths_indicator: "/private/"
```

Constants are immutable references for use in emit rules. They are referenced as `$constants.<key>`.

---

## 7. `session` block

The session block populates the trace's top-level fields. It is split into three subsections.

```yaml
session:
  static:
    classification: "internal"
    flags.for_research: true
    schema_version: "0.3.0"
    trace_type: "agent-session"

  derived:
    # field path: expression
    id: "$record_first.id"
    title: "$record_first.id"
    operational_context.working_directory: "$record_first.cwd"
    timing.started_at: "$record_first.timestamp"
    environment.agent_framework: "pi"
    environment.agent_version: "$adapter.harness_version"

  passthrough:
    # Keys here are written to the trace as-is. Useful for non-trivial nested
    # default structures whose values are not scanned from input.
    handover: { received: null, produced: null }
    coordination: { project_id: null, predecessor_session: null, concurrent_sessions: null, human_attention: "active" }
```

`static` field values are literal. `derived` field values are expressions evaluated once during processing — typically against the first input record. `passthrough` values are inserted verbatim. In `session` and in each `sessions` shape alike, the three maps are written in this order: `passthrough`, then `static`, then `derived`. A key of a later map replaces what an earlier map wrote at or beneath its path, and a later key beneath an earlier object is written inside it (§8.2.1): a `static` key beneath a `passthrough` object is kept inside that object, and a `static` object replaces whatever `passthrough` wrote beneath it. Within one map, keys are written in the order written, as `set` keys are (§8.2.1), and the same two rules hold between them. After `passthrough` and `static` and before `derived`, each of `turns`, `tool_calls`, `segment_markers`, `policy_triggers`, `annotations`, `lineage` and `redactions` not yet present is written as an empty list, and each of `metrics`, `flags`, `environment`, `operational_context`, `timing`, `provenance` and `operator` as an empty object (`operator` is then replaced, §7.1); later writes fill or replace them, and the built trace carries them even when empty, since an omitted member and an empty list or object hash differently (`minitrace-spec` §3.1). A `derived` key whose path, before its last segment, runs at run time through a string, a number, a boolean or a list is an input failure as §8.2.1 states for `set`: nothing is written, the prior value is kept, it is reported and named as there, and the path is checked first, so the expression is not evaluated. Because `static` and `passthrough` values are literal, a `static`, `derived`, `passthrough` or `finalize.computed` key that lies beneath a `static` or `passthrough` value that is a string, a number, a boolean or a list is a structural failure at load (§15), whatever the order of the keys, in `session` and in each `sessions` shape alike; a nested literal object counts by its own keys, and a null literal there is created as an object, as §8.2.1 has it.

The trace `id` is subject to §13.2 R5: it must evaluate to a usable, unique non-empty string, or the trace is not emitted.

Deep field paths (e.g. `operational_context.working_directory`) are dot-separated; the interpreter creates intermediate objects as needed.

`$record_first` is a built-in reference to the first record of the trace's record set (within the current container in container mode, §4.1). It is the only built-in record reference in the `session` block. Records and containers that the reader skipped (§4) do not count for `$record_first`, `$container_first` or `$container_index`. In `finalize.computed`, only `state`, `adapter`, `constants`, `trace` and `container` are bound; the other context variables are unbound there and evaluate to null (§8.3).

### 7.1 Consent-claim fields are runtime-supplied, not adapter-declared

The `operator` object in `minitrace-spec` v0.3.0 §5 carries assertions about who directed the session and on what consent basis. Per `minitrace-spec` §5.4, the value of `operator.operator_aware` cannot be inferred from harness output and MUST come from the trace producer in a position to know the operator's actual state.

A DSL adapter file is a publishable artefact intended to be read as a template across deployments. If consent-claim values are baked into the adapter's `session.static`, every consumer of that adapter inherits the claim regardless of their actual circumstances.

Therefore: an adapter DSL file MUST NOT write the trace's `operator` object or anything beneath it, the consent-claim fields `operator.type`, `operator.operator_aware`, `operator.capture_mode` and `operator.capture_purpose` included. That holds wherever an adapter can write a session field: a key `operator`, or one beneath it (`operator.type`, `operator.capture_purpose.reference`, `operator.notes`), in the `static`, `derived` or `passthrough` map of `session` or of a `sessions` shape or in `finalize.computed`, and a `set` key `session.operator` or one beneath it. An adapter that has such a key is invalid, a structural failure a runner MUST detect at load (§15), whatever the key's value, an expression or null included. The whole object is refused, not only the four fields, because the runtime replaces it whole (below): nothing an adapter writes there reaches the trace. The `operator` object, these fields in it, is supplied by the runtime invocation of the DSL interpreter (the entity actually performing the conversion), per the rules below.

The interpreter:

- MUST require `operator.type` at invocation; refuses to emit a trace if not provided.
- MUST require `operator.operator_aware` (boolean) when `operator.type` is `human`; refuses to emit otherwise.
- MUST require `operator.capture_mode`, one of `full`, `pseudonymised`, `scrubbed` or `reference_only`, when `operator.type` is `human` or `delegated` (`minitrace-spec` §5.2); refuses to emit otherwise.
- MUST require a well-formed `operator.capture_purpose` (legal_basis, reference, authorised_at; well-formed as `minitrace-spec` §5.3 defines it) when `operator.type` is `human` and `capture_mode` is `full`, and MUST NOT emit a non-well-formed non-null `capture_purpose` for a `human` operator in any mode (`minitrace-spec` §5.3); refuses to emit otherwise.
- MUST require every runtime-supplied operator value, whatever interface supplies it (a command line or a library call), to be one an I-JSON message can hold (`minitrace-spec` §3.4) and to meet `minitrace-spec` §5.2 to §5.4; refuses to emit otherwise.
- The runtime-supplied `operator` object is written into the trace AFTER all DSL processing, replacing the whole `operator` object any rule may have produced, not merging with it (this is enforcement, not collaboration).

This makes `minitrace-spec` §5.4 enforceable rather than aspirational. The DSL describes how to read the harness; the runtime invocation states the consent context.

---

## 8. `emit` block

The `emit` block is an ordered list of rules. For each input record (in order), the interpreter tries each rule in sequence, and every rule whose `when` predicate matches fires, in the order written; a non-matching rule is skipped. Rules are ordered actions, not pattern-match dispatch: two rules may match the same record, and a `{ has_any: true }` rule fires on every record. After every rule has been tried, the next record is processed. (Inside `on_block`, dispatch is first-match-wins per block; see below.)

Each rule has the shape:

```yaml
- name: <rule name>             # OPTIONAL; for diagnostics
  when: <predicate>             # see §8.1
  do:                           # ordered list of actions
    - <action>                  # see §8.2
    - <action>
    - ...
```

Some rules iterate over content inside the matched record:

```yaml
- name: <rule name>
  when: <predicate>
  foreach_block: <expression>   # iterate; each element bound as $block
  on_block:
    - when: <predicate>         # block-level predicate
      do:
        - <action>
```

A rule MAY combine `do` and `foreach_block`/`on_block`. When both are present, the rule's `do` block runs once for the matched record (typically to emit the parent event such as a turn), and then `foreach_block` iterates the named collection (typically to emit child events such as tool calls within the parent turn). Order is normative: parent-level `do` runs before any block-level `on_block` actions.

The `foreach_block` expression MUST evaluate to a list, iterated in list order. Null iterates nothing, whether written as the literal `null` or evaluated. A literal `foreach_block` value that is neither a string nor null (a number, a boolean, an object, or a literal list, which is not an expression) is decided by the adapter text and is a structural failure at load (§8.3, §15). Any other evaluated value (a string, an object, a number) is an input failure (§8.3): the runner reports it and skips the block iteration of that rule for that record; the rule's `do` actions have already run.

`on_block` dispatch is first-match-wins **per block**: within a single block iteration, the interpreter tries each `on_block` sub-rule in order and only the first sub-rule whose `when` predicate matches fires for that block. A `{ has_any: true }` terminal sub-rule MAY be used as a catch-all for unmatched blocks (see e.g. mast's `unknown_block` fallback). To emit multiple distinct events from the same block (e.g. a `tool_call` plus a per-call annotation), declare separate top-level rules each with its own `foreach_block` over the same collection.

Block iteration is one level deep; nested iteration is not supported in v0.1.0.

### 8.1 Predicates

Predicate forms supported:

| Form | Meaning | Example |
|---|---|---|
| `{ field: <path>, value: <value> }` | Record's value at dotted `<path>` equals `<value>` | `{ field: type, value: model_change }` |
| `{ field: <path>, present: <bool> }` | Record's value at `<path>` is non-null (`present: true`) or null (`present: false`) | `{ field: openai_message_id, present: true }` |
| `{ has: <field> }` | Record has the named field, regardless of value | `{ has: message }` |
| `{ block_type: <value> }` | Inside `on_block` only: current `$block.type` equals `<value>` | `{ block_type: text }` |
| `{ container_field: <path>, value: <v> }` | Container's value at dotted `<path>` equals `<v>` (Draft 2; container-mode adapters) | `{ container_field: labels.defense_type, value: detected_with_warning }` |
| `{ container_field: <path>, present: <bool> }` | Container's value at `<path>` is non-null (`present: true`) or null (`present: false`) | `{ container_field: defense_type, present: true }` |
| `{ block_field: <path>, value: <v> }` | Inside `on_block` only: current block's value at dotted `<path>` equals `<v>` | `{ block_field: value, value: 1 }` |
| `{ block_field: <path>, present: <bool> }` | Inside `on_block` only: block's value at `<path>` is non-null (`present: true`) or null (`present: false`) | `{ block_field: id, present: true }` |
| `{ has_any: true }` | Always matches: in `emit`, its rule fires on every record; in `on_block`, it is the terminal catch-all for blocks no earlier sub-rule matched | `{ has_any: true }` |

Predicates may be combined with `all_of` or `any_of`:

```yaml
when:
  all_of:
    - { has: message }
    - { has: timestamp }
```

`all_of` and `any_of` each take a non-empty list of predicates, and a combinator stands alone in its mapping. In every form, `<path>` and `<field>` are strings, `present` is `true` or `false`, and `has_any` is exactly `true`. Any other shape is a structural failure at load (§15).

The `container_field` predicate enables conditional emission scoped to container-level fields without introducing a `not:` operator at the predicate level. It is required for any rule that fires per-event but only on a subset of containers (e.g. a `defense_type` annotation rule that fires only on records whose container carries non-null `defense_type`). Earlier conventions of using post-emission filtering or duplicating per-record logic across rules are obsoleted by this predicate.

The `block_field` predicate is symmetric to `field:` (record-scoped) and `container_field:` (container-scoped). It applies inside `on_block` rules where `foreach_block` iterates over synthesised blocks (e.g. `dict_items` output, see §9.1) whose discriminator is in the block payload rather than in `record.type`.

Boolean combinators `all_of:` and `any_of:` accept lists of any predicate type (including nested combinators).

### 8.2 Actions

This section defines six actions; §14.3 adds five emit actions. Each is a YAML mapping with a single top-level key naming the action, plus action-specific fields. The one exception is `omit_if_none`, a second key permitted beside `emit_turn` (§8.2.2).

#### 8.2.1 `set`

Mutate state or set a session-level field.

```yaml
- set:
    state.current_model: "$record.modelId"
    state.next_turn_index: "$state.next_turn_index + 1"
    session.environment.model: "$record.modelId"
```

LHS paths under `state.*` mutate state; under `session.*` write into the trace's top-level fields, except `session.lineage`, which only `emit_lineage` writes (§13.1), `session.tool_calls`, which only `emit_tool_call` and `attach_tool_result` write (§8.2.3), and `session.operator`, which the runtime supplies and no adapter writes (§7.1). The keys of a `set` or `accumulate` mapping are applied in the order written, and each right-hand side sees the writes of the keys before it: `state.n: "$state.n + 1"` followed by `state.id: "x-${state.n}"` uses the new `n`. A `set` or `accumulate` key whose path, before its last segment, runs through a value that is a string, a number, a boolean or a list is an input failure (§8.3): nothing is written, so that value and the key's target are unchanged, and the runner reports it as §8.3 states for any input failure, naming the key as the field, its right-hand side as the expression though it is not evaluated, and the input as §8.3 names it from what the right-hand side references, and in addition the key's path up to that value and the value's type. The path is checked first: for such a key the right-hand side is not evaluated and cannot fail, as for a branch §8.3 does not select, so the blocked path is its one finding. Such a path does not index a list, unlike a `record_path` segment (§4.1.1). The same holds for a `derived` key of a session map and a `finalize.computed` key (§7, §9). A value before the last segment that is null or absent is created as an object, and the write goes on, unreported (an `accumulate` that writes nothing creates none, §8.2.5).

#### 8.2.2 `emit_turn`

Append a turn to the trace's `turns[]`.

```yaml
- emit_turn:
    index: "$state.next_turn_index"
    timestamp: "$record.message.timestamp"
    role: "$record.message.role"
    content: "$block.text"
    model: "$state.current_model"
    source: "model"
```

After emission, `$state.last_turn_index` is automatically updated to the just-emitted turn's `index`. Producers may also set `$state.next_turn_index` explicitly via `set`.

An `emit_turn` action MAY carry, beside the `emit_turn` key in the same mapping, a key `omit_if_none` whose value is a list of turn field names. Each listed field whose value evaluates to null is left out of the turn instead of being written as null; an unlisted field that evaluates to null is written as null. This lets a field that applies to few turns (for example `ordering` on the turn a segment marker opens) stay absent on the others. `omit_if_none` is defined for `emit_turn` only.

```yaml
- emit_turn:
    index: "$state.next_turn_index"
    ordering: "$state.pending_ordering"
  omit_if_none: [ordering]
```

#### 8.2.3 `emit_tool_call`

Append a tool call to the trace's `tool_calls[]`.

```yaml
- emit_tool_call:
    id: "$block.id"
    timestamp: "$record.message.timestamp"
    tool_name: "$block.name"
    # operation_type: OPTIONAL; when omitted, filled from operation_types (§10)
    emitting_turn_index: "$state.last_turn_index"
    input:
      arguments: "$block.arguments"
    invocation_source: "agent_choice"
```

The `id` is classified the way R5 (§13.2) classifies a part of a trace id: a non-empty string is used as it is, and a number is rendered as §8.3 renders an interpolated number (so `1` and `1.0` both give `"1"`), since source tool-call identifiers MUST be preserved (`minitrace-spec` §4.8.2) and many are numeric. An `id` that is null, the empty string, a boolean, an object or a list is unusable, and so is a template `id` any of whose `${...}` parts is unusable, as R5 holds for trace ids. For an unusable `id` the tool call is not emitted, and the runner reports it as an input failure (§8.3), a value-level loss (§15). A `tool_name` that is neither a string nor null is set to null and reported as an input failure.

After emission, `$state.last_tool_call_id` is the just-emitted tool call's id, always a string, and `$state.tool_calls_by_id[<id>]` is set to the call's reference for later attachment. A tool call that is not emitted sets `$state.last_tool_call_id` to null and leaves `$state.tool_calls_by_id` unchanged, so a result attached to it, by its source id or through `$state.last_tool_call_id`, finds no call, unless an earlier emitted call has the same id, which then takes it (not a duplicate under the next paragraph, since the call not emitted holds no id), and is reported (§8.2.4, §15) rather than replacing another call's result.

Tool-call ids are unique within a trace (`minitrace-spec` §4.2). Once a trace is built, before `finalize`, each id held by two or more of its emitted tool calls, after rendering (so a source `1` and `"1"` share an id), is reported once, and none of the calls holding it is kept, nor any result attached to them, a value-level loss (§15): keeping the first or the last would let one record shadow another's result, as R5 holds for traces. `$state` is not changed by the removal. A reference to a removed call elsewhere in the trace is not rewritten and may not resolve.

A trace's `tool_calls[]` is written only by `emit_tool_call` and `attach_tool_result`. An adapter that writes `tool_calls`, or a path beneath it, in any other way is a structural failure that a runner MUST detect at load: a `set` key `session.tool_calls` or one beneath it, or a key `tool_calls` or one beneath it in the `static`, `derived` or `passthrough` map of `session` or of a `sessions` shape, or in `finalize.computed`. `$state.tool_calls_by_id` is likewise the runner's: `emit_tool_call` sets its entries, and `attach_tool_result` updates the call an entry holds. A `set` or `accumulate` key `state.tool_calls_by_id` or one beneath it, or a key `tool_calls_by_id` or one beneath it in the `state` block (§5), is a structural failure that a runner MUST detect at load. An adapter may read it, whole or by entry, and a read yields an independent copy, at every depth, of what it reads, taken when it is read, so writing that copy into another state key, or beneath one, never changes an emitted call.

#### 8.2.4 `attach_tool_result`

Update an already-emitted tool call with output content. Used when toolResult records arrive separately from their toolCall.

```yaml
- attach_tool_result:
    target_id: "$block.toolUseId"
    output:
      success: "not $block.isError"
      result: "$block.content"
      error: "$block.isError ? $block.content : null"
```

Its body has `target_id`, which it requires, and optionally `output`, a mapping; any other key, or an `output` that is not a mapping, is a structural failure at load.

A `target_id` that is a number is rendered as §8.3 renders an interpolated number before it is matched, as an emitted tool-call `id` is (§8.2.3), so `target_id: 5` and `target_id: 5.0` both match the call emitted as `"5"`. Any other `target_id` that is not a string matches no call (§8.3).

If `target_id` does not match any emitted tool call, the action is a soft no-op recorded in the diagnostics output (not a fatal error). Producers SHOULD ensure ordering or use `pending_tool_calls` state to buffer.

#### 8.2.5 `accumulate`

Add a value into a numeric or list state accumulator.

```yaml
- accumulate:
    state.token_totals.input: "$record.message.usage.inputTokens"
    state.all_timestamps: "iso_normalise($record.message.timestamp)"
```

The LHS is a `state.` path. If the state value there is a list, the RHS is appended, except that a null RHS is not appended. Otherwise the state value is a counter: a null or absent value counts as 0, a numeric RHS is added (booleans are not numbers), and a null RHS adds nothing: into a null or absent counter it writes nothing, so the key stays as it was (absent or null) and no value before the last segment is created for it (§8.2.1). A RHS that is neither null nor a number, a counter that holds a value other than null or a number (whatever the RHS, a null one included), or a key whose path runs through a string, a number, a boolean or a list (§8.2.1, likewise whatever the RHS), is an input failure, and the state keeps its prior value; so does a sum outside the numeric domain (§8.3).

#### 8.2.6 `note`

Emit a diagnostic note into the conversion log without affecting the trace.

```yaml
- note:
    level: warning
    message: "Unexpected record type: ${record.type}"
```

Its body has at most `level`, a string, and `message`; any other key, or a `level` that is not a string, is a structural failure at load.

### 8.3 Expressions

Expressions are intentionally minimal.

**Evaluated positions.** A value is evaluated only where this specification says so: the values of `session.derived` and of each `sessions.*.derived`, the values of `finalize.computed`, the `foreach_block` expression, and every value in an action's body, recursively through nested maps and lists, except the `omit_if_none` list. Every other value is literal and is never a template: `session.static`, `session.passthrough` (written verbatim), `state` initial values, `constants`, predicate operands, rule `name` and `target`, and the `operation_types` table.

A value at an evaluated position is one of:

- A literal scalar (string, number, boolean, null). A literal's type is its YAML type: a quoted YAML string is a string even when it spells a number, boolean or null (`"1.28"`, `"true"`, `"null"`, `"2026-10"`), and only an unquoted YAML scalar is a number, boolean or null.
- A reference: `$record.path`, `$record_first.path`, `$state.path`, `$block.path`, `$container.path`, `$container_first.path`, `$container_index`, `$adapter.path`, `$constants.path`, `$trace.path`.
- A function call from the table in §9.1 (functions are available in `emit`, `on_block`, `finalize`, and any other expression position).
- A Python-style ternary: `<expr> if <predicate> else <expr>` (and the legacy form `<predicate> ? <expr> : <expr>`).
- A unary `not`, which yields the negation of its operand's truthiness (below).
- A `+` or `-` between two numeric expressions, and a unary `-` before a numeric expression, with the same failure rules.
- A comparison `==`, `!=`, `<` or `>`, with the operand rules below.

**Comparisons and truthiness.** `==` and `!=` are type-strict. Two values are equal only if they have the same JSON type and the same value, so `true != 1` and `"1" != 1`; numbers compare by value, so `1 == 1.0`. Two objects are equal when they have the same set of keys and equal values under each key, and two lists are equal when they have the same length and equal elements at each position, by this same rule. This equality also governs these matches: a predicate's `value:` or `block_type` (§8.1), the `kind` of `blocks_join_of_type` and `first_block_text_of_type` and the matching in `count_where` (§9.1), the `operation_types` lookup by tool name (§10), and the match of an `attach_tool_result` `target_id` against tool-call ids (§8.2.4). Table keys and emitted tool-call ids are strings (§2; `minitrace-spec` base schema), while a tool name or a `target_id` can be any value, so one that is not a string never matches, except that a numeric `target_id` is first rendered as a string (§8.2.4): an unmatched `target_id` is reported (§15), and an unmatched tool name falls to the `default` entry, else `OTHER` (§10). `<` and `>` take either two numbers (booleans are not numbers anywhere in this specification: not for `+`, `-`, unary `-`, `min`, `max`, `<` or `>`) or two timestamp strings that `iso_normalise` (§9.1) returns unchanged, that is valid date-times already in the `minitrace-spec` §3.5 form with seconds 00 to 59, which then compare chronologically. Any other pair of operands, null included, is an input failure. The condition of a ternary and the operand of `not` use truthiness: null, `false`, the number zero, the empty string, the empty list and the empty object are false, and every other value is true, including the string `"false"`.
- A safe-navigation chain: `$record.message?.usage?.inputTokens` (returns null if any segment is missing).

Path syntax follows the dot/bracket convention used by JMESPath for simple field access. Filters (`[?type=='text']`) are NOT supported — use `foreach_block` with a `when` predicate inside `on_block` instead.

String formatting `${expr}` is supported inside string values, e.g. `"Unexpected record type: ${record.type}"`; such a value is a *template* (below). Interpolation does not nest: a `${` inside a string constant within a `${...}` body is ordinary text, not interpolated. The one nested form is the trace-id portion of the cross-trace placeholder (§8.4).

**Templates.** A string value in an evaluated position that contains `${` is a template. This is decided first, before the literal and expression test below. Each `${...}` part, delimited by balanced braces in which every `{` and `}` counts, inside quotes too (so `${coalesce($a, '}')}` ends its body at the quoted `}`, and that body does not parse), holds a body that is evaluated as an expression whether or not it carries a marker, never used as a literal. A `$` before the body's leading identifier is optional (`${record.id}` is `${$record.id}`), so that identifier is a reference or a call, never a string. The optional `$` applies only when the body begins with an identifier that is not a keyword. A body that begins with `not`, `true`, `false`, `null`, `True`, `False`, `None`, a quote or a digit is evaluated as written, so `${not record.ok}` negates and `${true}` renders `true`. The one exception is the cross-trace form `${trace:...}` (§8.4, §13.1). The text outside the `${...}` parts is literal text. A template is a structural failure, which a runner MUST detect when it loads the adapter, if a `${` is never closed, or if its text outside the `${...}` parts, joined with the parts removed, would itself be an expression by the test below (`record${x}.id` leaves `record.id`) (for example `coalesce($a, 'x-${b}')`): a value is either an expression or a template, never both. A template whose `${...}` body names a root that is not a context variable (`"${TMPDIR}"`) is a structural failure too, rejected at load under the evaluation-failure rule below.

**Literals and expressions.** A string value in an evaluated position that is not a template is an *expression* if its raw text, before any rewriting, contains one of three markers. The first is a `$`-prefixed reference: a `$` immediately followed by an ASCII letter or `_`, so `$5` and `$é` are not one. The second is a call: an identifier (an ASCII letter or `_`, then Unicode letters or digits, general categories L and N, or `_`) immediately followed by `(`, whether or not it names a §9.1 function. A call to a name §9.1 does not define, or a method call, is therefore an expression and a structural failure (below), never a literal. The third is a dotted path: a context variable (§8.4), with or without the `$` prefix, followed by `.` and an ASCII letter or `_` (so `record.1` and `record?.id` carry no path marker). A call is a marker only where the character before its identifier is not a Unicode letter or digit (general categories L and N) or `_`; a `.` before it is allowed, so `x.total_seconds()` holds a call. A path is a marker only where the character before its name is not a Unicode letter or digit, `_` or `.`; for a `$`-prefixed path the name is the identifier after the `$`, so the `$` is the character before it. A `$`-prefixed reference is likewise a marker only where the character before the `$` is not a Unicode letter or digit (general categories L and N), `_` or `.`; in a string that is an expression by another marker, and in a `${...}` body, a `$` in that position followed by an ASCII letter or `_` is a structural failure, so `not$record.a` (whose path `record.a` is a marker), `len(a.$record)` and `${not$record.a}` are rejected at load, while a string with no other marker, such as `price a$b`, is a literal. So `filmax(1)` holds one call, to `filmax` and not to `max`, and is rejected at load as an undefined function; `naïve(x)` holds a call to `naïve`; `éfoo(1)` holds no call, since its first letter is not ASCII and the `foo` after it follows a letter; `see note (1)` holds none either, and `x.record.id` holds no path. Classification comes first: the rewrites a runner applies to expressions (removing `$`, reading `?.` as safe navigation, the C-style `?:` ternary) apply only to values already classed as expressions, and never decide the class. A value with a marker that does not parse under the expression syntax below is a structural failure, not a literal, so `notes in record.json` is rejected. Any value without a marker is a *literal* and is used exactly as written, for example `cost $5`, `a?.b` or `What? yes: no`. This includes such a value that would not parse as an expression, and a bare, hyphenated or slash-separated word whose roots are not context variables (e.g. `synthetic-test-judge`, `inspect_ai/span/agent`). A context-variable name standing alone is a literal without `$` and a reference with it: `scope.type: trace` emits the string `trace`, while `$record` emits the current record. The empty string is a literal like any other and is emitted as `""`, not as null.

**Expression syntax.** Inside an expression, and inside each `${...}` body:

- An identifier is an ASCII letter or `_`, then Unicode letters or digits (general categories L and N) that have the property XID_Continue (Unicode Standard Annex #31), or `_`, read exactly as written: a name that NFKC normalisation would change (`ﬁeld`, `a１`) is not one. Every name, with or without `$`, and every `.name` and `?.name` segment is an identifier; a name or segment that is not one is a structural failure (`$record.名`, `coalesce(éa, 1)`), and a key that is not an identifier is read with `['name']`. The markers above test general categories only, the call marker's identifier included, not the property XID_Continue or this reading-as-written condition: `xﬁeld(1)`, `xⸯ(1)` and `record.aﬁ` are expressions and structural failures, while a string with no marker (`éa`, `ﬁeld(1)`, `record.é`) stays a literal.
- An identifier written with `$` MUST name a context variable (§8.4) or a §9.1 function; any other `$`-prefixed name is a structural failure. A context variable not bound where the expression runs evaluates to null (for example `$block` outside `foreach_block`), except that `$container` outside container mode and `$record_first` over an empty record set are empty objects.
- A bare identifier that names a context variable is that variable; one that names a §9.1 function is that function and MUST appear only in call position. `true`, `false` and `null` are the boolean and null constants, and `True`, `False` and `None` are accepted as the same constants. A reserved keyword of the Python 3 expression grammar (for example `in`, `is`, `and`, `or`, `lambda`, `import`) other than `if`, `else` and `not` in their roles above is a structural failure. Any other bare identifier is a string equal to its name, so `count_where($trace.tool_calls, operation_type, EXECUTE)` compares field `operation_type` with the string `EXECUTE`.
- A string constant is enclosed in single or double quotes. It contains neither its own quote character, nor a backslash, nor a line break (U+000A, U+000D); there are no escape sequences. Two string constants with nothing or only whitespace between them are a structural failure, not one string (`'a' 'b'`, `'a''b'`, `'a'"b"`).
- Whitespace between tokens is space (U+0020) and tab (U+0009) only. There is no line continuation and no comment: a backslash, a `#`, or a line break (U+000A, U+000D) or form feed (U+000C) outside a string constant is a structural failure. Space, tab, line breaks and form feeds at either end of an expression or a `${...}` body are removed before it is read, so the final newline of a YAML block scalar is harmless; a character with the Unicode property White_Space other than these (U+00A0, U+000B, U+0085 or U+2028, for example) is not removed and, wherever it stands outside a string constant, is a structural failure; a `${...}` body that is empty after this removal is a structural failure (`${}`, `${ }`); the literal text of a template is not expression text. A `${` followed directly, as written, by `trace:` opens the cross-trace form (§8.4), which is not a `${...}` body: nothing is removed at its ends, its trace-id portion and its field name are literal text, and only the `${...}` parts inside its trace-id portion are bodies, read as expressions under this section. The form is valid only as the whole value §13.1 allows and is a structural failure at load anywhere else (§8.4); `${ trace:…}` is therefore an ordinary body, and as a `target_content_hash` a structural failure (§13.1).
- A number constant is written in the syntax of an unsigned JSON number (RFC 8259 §6: digits, optional fraction, optional exponent, no leading zero). Hexadecimal, octal and binary forms and digit separators are not part of the language.
- Path access is `.name`, `?.name`, `[n]` with a non-negative integer constant, and `['name']` with a string constant. `.name`, `?.name` and `['name']` read a key of an object; `[n]` reads element `n` of a list. A missing key or element gives null, as does a target that is not an object (for a key) or not a list (for `[n]`), so `[n]` on a string gives null. Any other index form (a computed, negative or boolean index) is a structural failure. A path is one lexical unit: whitespace MUST NOT appear after the `$` of its root, between its root and its first segment, between its segments, or inside a segment outside a string constant (`.name`, `?.name`, `[n]`, `['name']`), so `$record ?. a`, `$record . a`, `$record [0]` and `$record[ 0 ]` are structural failures. Whitespace elsewhere in an expression, around operators, between function arguments and before or after a whole path, is allowed. Parentheses group.
- `?.` is safe navigation, never part of a ternary. The legacy ternary `p ? a : b` is single-level: its condition and its branches MUST NOT contain another legacy ternary, and a nested conditional uses the Python form, which nests.

A chained comparison (`1 < x < 9`) is an undefined construct and a structural failure. So is an operand of `<` or `>` that is itself a comparison or a `not` expression, parenthesised or not (`(1 < x) < 9`, `(not x) > 0`): its value is always a boolean, which `<` and `>` never take. A comparison or `not` as an operand of `==` or `!=` stays valid (`(1 < x) == (y < 2)`). `not` binds more loosely than a comparison, as in Python, so `not x > 0` is `not (x > 0)` and is unaffected. A boolean constant as an operand (`true > 0`) stays an input failure, a failure a literal operand is to blame for (*Input failure* below). Anything else (a method call, a keyword argument, another operator such as `>=`, `and` or `in`, a collection literal, a lambda, a slice) is an undefined construct and a structural failure.

**Evaluation failure.** An expression can fail in two ways, and each has one required outcome.

- *Structural failure:* the expression uses a function, operator or construct this specification does not define (§8.3, §9.1), names a `$`-prefixed root that is neither a context variable nor a function, carries a marker but does not parse, or is a `$`-prefixed reference to a function name outside call position (e.g. `$len`; a bare `max` without `$` as a whole value is a literal). This is a defect in the adapter. A runner MUST reject the adapter at load, before reading any input, for every structural failure the adapter text decides: syntax, `$` roots, function names and arity, undefined constructs and operators, templates (above) and placeholder location (§8.4, §13.1). This holds whether or not the rule holding the expression ever fires, so conforming runners accept and reject the same adapters. A structural failure that shows only during evaluation, because it depends on the data, aborts the conversion of the whole input: the runner emits no trace from that input and reports the failure.
- *Input failure:* any other failure while evaluating, in particular a value whose type the expression cannot use, for example a `+` or `-` operand that is not numeric, a function argument of the wrong type, or a computed number outside the numeric domain of `minitrace-spec` §3.4 (a sum beyond ±(2^53−1), for instance, including one reached by `accumulate`, which then keeps its prior value). This includes such a failure where a literal operand is to blame: a runner is not required to tell the two apart, and MAY check adapters statically to find them earlier. On an input failure the expression evaluates to null, and the runner MUST report the failure in the conversion's diagnostics, not in the trace, naming the field, the expression and the input it was evaluated on. Inside the record loop that is the record, and in container mode (§4.1) its container as well. For a value computed outside it (`session.derived`, `sessions.*.derived`, `finalize.computed`), it is each record or container root the expression references among those bound where it runs (`$record_first`, `$container`, `$container_first`, `$container_index` in the session block; `$container` in `finalize`), a record being named with its container in container mode, or, where it references none of them, that it read no input. An input failure MUST NOT abort the conversion, since one hostile record would otherwise suppress every trace in it. Other sections also class as input failures findings that the runner makes while reading the input or checking a built trace, where there is no field or expression to name: those of §4 and §4.1.1. Such a finding takes from this section that it does not abort the conversion and is reported in the conversion's diagnostics, not in the trace; it is named as the section that defines it states, not by field and expression, and its loss class is as §15 states.

**Evaluation order and reach.** An input failure in any part of a field's expression makes the whole field value null; a failed part never becomes null while evaluation goes on. Which parts are evaluated is fixed. Operands and function arguments are evaluated left to right, and all of them are evaluated, except in two forms. A ternary (either form) evaluates its condition first and then only the branch it selects; the other branch is not evaluated and cannot fail. `coalesce` evaluates its arguments left to right and stops at the first non-null one; the arguments after it are not evaluated and cannot fail, so `coalesce($record.a, $record.b + 1)` with `a` present yields `a` whatever `b` holds. A template evaluates its `${...}` parts left to right, and the first part that fails makes the whole string null.

A path whose segment is missing, or whose intermediate value is not an object (or, for `[n]`, not a list) (e.g. `record.message.role` where `record.message` is a string), is neither: it evaluates to null under safe navigation, and a runner MAY report it.

In either failure case a runner MUST NOT emit the expression's source text, or text derived from it such as the source with its `$` prefixes removed, as a value. Inside a `${...}` interpolation, an inner expression that *fails* makes the enclosing string null rather than partially substituted; an inner expression that evaluates to null without failing interpolates as the empty string.

**Interpolated values.** An inner value that evaluates without failing renders as follows: a string as it is; a number in its RFC 8785 serialisation (so `1.0` renders as `1`); a boolean as `true` or `false`; null as the empty string. An object or a list is an input failure, so the enclosing string is null.

### 8.4 Context variables

Implicit context variables available to DSL expressions:

| Variable | Meaning |
|---|---|
| `record` | The current record (per emit-rule iteration). |
| `record_first` | The first record in the current container's record set (for header-vs-body adapters). |
| `block` | The current block when iterating inside `foreach_block`. |
| `container` | The current container object (in container-mode adapters). |
| `container_first` | The first top-level element not skipped as unreadable, in input order (§4, §15). |
| `container_index` | The 0-based position of the current container among the top-level elements not skipped as unreadable, in input order (§4, §15). |
| `state` | The adapter's mutable state object (initialised from `state:` block, updated by `set:` and `accumulate:` actions). |
| `adapter` | The adapter declaration object (from `adapter:` block). |
| `constants` | The adapter's `constants:` block. |
| `trace` | The current trace being built (read-only from expressions; mutated only by emit actions). |

Expressions reference these via dotted paths (e.g. `record.message.role`, `state.next_turn_index`) or `$`-prefixed forms (`$record.path`, `$container_index`), and templates via `${...}` (`${container_index}`). Whether a value is a literal, an expression or a template is decided by §8.3 alone: a context-variable name standing alone without `$` (e.g. `scope.type: "trace"`) is a literal string. A value quoted inside the YAML string is classified like any other: `"'literal-text'"` carries no marker, so it is a literal and is emitted with its quotes, while `"'record.id'"` carries a path marker and is an expression.

The cross-trace placeholder syntax `${trace:<trace-id>:<field>}` (see §13.1) is a special form recognised during string interpolation: it is not evaluated as an expression. The runner detects the `trace:` prefix, recursively interpolates any inner `${...}` in the trace-id portion (so `${trace:agent-${record.id}:content_hash}` resolves the inner `${record.id}` first), and then passes the result through to the cross-trace resolution pass for substitution after content-hash computation. The special form is valid only as the whole template of a placeholder-bearing field (§13.1). A runner MUST reject, when it loads the adapter, a DSL that uses it anywhere else. A predicate whose shape shows that it can never match (an empty combinator, §8.1) is refused at load as well (§15). A rule whose predicate is well formed but never meets matching input is not a failure at all: it only produces no output, while a misplaced placeholder produces wrong output (a reference nobody resolves, carried as content).

---

## 9. `finalize` block (OPTIONAL)

After all records of a trace are processed, the `finalize` block runs once for that trace, with `$trace` bound to it; in multi-trace mode it runs once for each shape's trace. It computes derived fields from that trace's accumulated state. A `computed` key whose path runs at run time through a string, a number, a boolean or a list is an input failure as §8.2.1 states for `set`, and one beneath a `static` or `passthrough` literal of those types is a structural failure at load (§7, §15).

```yaml
finalize:
  computed:
    timing.ended_at: "max($state.all_timestamps)"
    timing.duration_seconds: "duration_seconds(min($state.all_timestamps), max($state.all_timestamps))"
    metrics.turn_count: "len($trace.turns)"
    metrics.tool_call_count: "len($trace.tool_calls)"
    metrics.execute_count: "count_where($trace.tool_calls, operation_type, EXECUTE)"
    metrics.total_input_tokens: "$state.token_totals.input"
    timing.privacy_level: "$constants.default_capture_mode"
```

### 9.1 Built-in functions

The following functions are available in `finalize` expressions AND in `emit` / `on_block` / `session.derived` / any other expression position. The `finalize` block was the first place they were specified; they apply uniformly across the DSL's expression surface.

| Function | Signature | Purpose |
|---|---|---|
| `len(x)` | `Any → int` | Length-or-zero: a string's length in Unicode code points (`len("😀")` is 1), a list's or an object's number of elements, 0 for null. Any other value is an input failure (§8.3). |
| `min(x)`, `max(x)` | `list → Any` | Min / max ignoring None values. Numbers compare by value and strings by Unicode code points; a list holding any value other than numbers, strings and nulls (booleans included), or numbers together with strings, is an input failure. Null for null, for an empty list and for a list holding only nulls; a value that is not a list is an input failure. Over timestamp strings this is chronological order only when every string is normalised per `minitrace-spec` §3.5 (UTC `Z`, six fractional digits); an adapter MUST normalise a timestamp to that form before it takes part in `min`, `max` or any ordering it relies on, with `iso_normalise` or before the DSL runs. |
| `count_where(items, field, value)` | `list[dict], str, Any → int` | Count elements where `item[field]` equals `value`, by the equality of `==` (§8.3). Null gives 0; any other value that is not a list is an input failure; elements that are not objects are not counted. |
| `iso_normalise(s)` | `str|None → str|None` | Return the `minitrace-spec` §3.5 form (`YYYY-MM-DDTHH:MM:SS.ffffffZ`, UTC) of an offset-bearing ISO 8601 extended date-time `YYYY-MM-DDTHH:MM:SS[.fraction](Z|±HH:MM)`, with uppercase `T` and `Z`. The fraction, when present, has at least one digit and may have any number of them; it is truncated, not rounded, to six (`minitrace-spec` §3.5), and padded with zeros when shorter or absent. `-00:00` is read as UTC. The value MUST be a valid date-time: a date of the proleptic Gregorian calendar, hours 00 to 23 (no `24:00:00`), minutes 00 to 59, seconds 00 to 59, or 60 when the value after conversion to UTC is `23:59:60` with any fraction (a leap second, which gives `23:59:59.999999` of that UTC day as `minitrace-spec` §3.5 states; the offset is applied to hours and minutes and the seconds are carried unchanged), and an offset of less than 24 hours with minutes 00 to 59. After conversion to UTC, the result MUST lie in the years 0001 to 9999. Any other string is an input failure (§8.3); this includes a date-only value, a week date, a time without offset, the basic format, lowercase `t` or `z`, an invalid date such as `2026-02-30`, a second 60 at any other time, such as `12:00:60Z`, and a value that leaves the year range on conversion, such as `0001-01-01T00:00:00+01:00`. An input that is not such a date-time is never passed through unchanged; a valid date-time already in the `minitrace-spec` §3.5 form with seconds 00 to 59 is returned as it is. Null gives null; a value that is not a string is an input failure. |
| `duration_seconds(start, end)` | `str|None, str|None → number|None` | Seconds from `start` to `end`, each a string `iso_normalise` accepts and read as `iso_normalise` reads it (fraction truncated to six digits, a leap second read as `23:59:59.999999`). The result is the difference in whole microseconds divided by 10^6 and rounded to the nearest binary64 value, so that implementations agree to the last bit. Null if either argument is null; an argument `iso_normalise` rejects is an input failure. |
| `iso_from_ms(n)` | `number|None → str|None` | Convert a Unix-epoch millisecond value to a UTC string in exactly the `minitrace-spec` §3.5 form, `YYYY-MM-DDTHH:MM:SS.ffffffZ`. The number is taken at the decimal value of its RFC 8785 serialisation, the shortest decimal that reads back as the same binary64 value, the closest one where two are as short, not at that binary64 value's exact expansion, and is rounded down to a whole microsecond, toward the earlier instant, as `minitrace-spec` §3.5 truncates; it is never rounded to nearest. A result outside the years 0001 to 9999 is an input failure, as is a boolean or a string, numeric or not (§8.3). |
| `iso_from_seconds(n)` | `number|None → str|None` | Convert a Unix-epoch second value (integer or fraction) to a UTC string in exactly the `minitrace-spec` §3.5 form, rounding the decimal value of its RFC 8785 serialisation down to a whole microsecond as `iso_from_ms` does, so `1700000000.123457` gives `2023-11-14T22:13:20.123457Z`. A result outside the years 0001 to 9999 is an input failure, as is a boolean or a string, numeric or not (§8.3). |
| `now_microseconds()` | `() → str` | Current UTC time in exactly the `minitrace-spec` §3.5 form (typical use: `provenance.converted_at`) |
| `sha256(s)` | `str → str` | SHA-256 over the UTF-8 encoding of `s`, as 64 lowercase hexadecimal digits. Any value that is not a string, null included, is an input failure. |
| `text_blocks_join(content)` | `list[dict]|None → str|None` | Join the `.text` of every block of `type: "text"` in the array, in array order, with `"\n"` (one line feed) between consecutive blocks. A text block without a `text` field contributes the empty string; a `text` that is not a string, null included, is an input failure. Null if `content` is null, is not a list or holds no text block. |
| `blocks_join_of_type(content, kind)` | `list[dict]|None, str → str|None` | Join the payload of every block whose `type` equals `kind`, in array order, with `"\n"` between consecutive payloads. The payload is `.thinking` for `kind` `thinking` and `.text` for any other `kind`. Unlike `text_blocks_join`, a missing, null or empty-string payload is skipped and adds no separator. Null if `content` is null, is not a list or holds no non-empty payload. Any other payload that is not a string, `0` and `false` included, is an input failure. |
| `first_block_text(content)` | `list[dict]|None → str|None` | Return the `.text` of the first `type: "text"` block, or `None`. Null, or a value that is not a list, gives null; a matched block whose `.text` is null or absent gives null, and one whose `.text` is present but not a string is an input failure. |
| `first_block_text_of_type(content, kind)` | `list[dict]|None, str → str|None` | Return text of the first block whose `type` matches `kind`. For `type: thinking`, returns `.thinking`; for `type: text` and for any other `kind`, returns `.text`. Null, or a value that is not a list, gives null; a matched block whose payload is null or absent gives null, and one whose payload is present but not a string is an input failure. |
| `json_parse(s)` | `Any → Any` | Idempotent JSON-string→object parse; returns `None` on parse failure. A parsed value outside the numeric domain or deeper than the limit of §4 is read as §4 prescribes. |
| `coalesce(*args)` | `*Any → Any` | First non-`None` argument. Arguments after it are not evaluated (§8.3, evaluation order). |
| `dict_items(d)` | `dict|None → list[{code, value}]` | Iterate dict key-value pairs as a `foreach_block` source, in ascending order of the keys compared as sequences of Unicode code points, not in the order the input wrote them (§4). Null, or a value that is not an object, gives an empty list. |
| `prefix_token(s)` | `str|None → str|None` | First whitespace-delimited token of a string (e.g. `"1.4"` from `"1.4 Failure description\n..."`). Whitespace is every character with the Unicode `White_Space` property plus U+001C to U+001F. Null for a value that is not a string and for a string of whitespace only. |

The language has no date-time type: timestamps are strings. `iso_normalise` brings a timestamp into the §3.5 form, in which string order is chronological order (`min`, `max`, and `<`/`>` per §8.3), and `duration_seconds` expresses the interval between two timestamps. Datetime arithmetic and method calls such as `.total_seconds()` are not part of the language and are structural failures (§8.3), and so is a call to `parse_iso`, which earlier candidates defined.

`json_parse` is idempotent: applied to any value that is not a string (an object, a list, a number, a boolean), it returns the value unchanged. This keeps adapters that target corpora with mixed string-encoded / object-encoded fields simple to author.

`dict_items` is the bridge between dict-shaped source data (e.g. a flat annotation block whose keys name the categories) and the `foreach_block` iteration model. The synthetic blocks have `code` (the dict key) and `value` (the dict value) fields, addressable via the `block_field` predicate (§8.1).

Functions that are observed-needed by additional adapters MAY be added in a revision that changes only `Z` per the convention-absorption rule in §16.

---

## 10. Operation-type derivation (OPTIONAL helper)

Tool calls in v0.3.0 carry an `operation_type` field (READ / MODIFY / NEW / EXECUTE / DELEGATE / OTHER) used by metrics aggregation. The DSL allows declaring a tool-name → operation-type table to avoid spreading the mapping across emit rules:

```yaml
operation_types:
  bash: EXECUTE
  read: READ
  read_file: READ
  write: NEW
  write_file: NEW
  edit: MODIFY
  edit_file: MODIFY
  search: READ
  grep: READ
  glob: READ
  default: OTHER
```

When this block is present and not empty, an `emit_tool_call` action that does not set `operation_type` gets one from the table: the entry for the emitted call's `tool_name`, else the `default` entry, else `OTHER`. An action that names `operation_type` keeps the value it evaluates to, null included. The table is not visible to expressions; there is no `$op_type` context variable.

---

## 11. Field preservation

Per `minitrace-spec` §3.1, v0.3.0 producers MUST NOT strip unknown fields. The DSL respects this by:

- Writing only the fields named in `session.static`, `session.derived`, `session.passthrough`, the body of `emit_turn` / `emit_tool_call` / `attach_tool_result`, and `finalize.computed`, and the empty lists and objects §7 writes.
- Leaving every other v0.3.0 baseline field unset.

Adapters MAY use `session.passthrough` to carry harness-specific fields outside the trace baseline as `framework_metadata` blobs or under `ext:<vendor>:*` keys. The DSL does not validate that these are not in conflict with v0.3.0 reserved names; the JSON Schema does that on the output.

---

## 12. Determinism

Two runs of the same DSL file against the same input MUST produce byte-identical output, modulo timestamps written by `now_microseconds()`. This is required so that adapter changes can be diffed against a fixed corpus.

Determinism implications:

- `set` and `accumulate` keys are applied in the order written (§8.2.1). No output depends on the order of the members of an input object (§4).
- `attach_tool_result` matching by `target_id` MUST use the equality of §8.3.
- `finalize` runs once per trace built, after all records, in the order written.

`now_microseconds()` is the only non-deterministic input. A runner MUST provide a way to fix the value it returns for one run to a given timestamp in the `minitrace-spec` §3.5 form; with the value fixed, two runs of the same DSL file against the same input produce byte-identical output, `content_hash` values included. The mechanism (a command-line option, an environment variable) is implementation-defined.

---

## 13. Cross-trace references and trace-pair emission

Some adapters emit multiple traces from a single input record. A common case: an adapter that produces both an `agent-session` trace describing an execution AND a `judge-session` trace describing the human or LLM evaluation of that execution. The judge-session's lineage edge (per `minitrace-spec` §7.1) carries `target_content_hash`, which references the agent-session's `content_hash`. Because `content_hash` is computed from the trace's canonical body (per `minitrace-spec` §3.4) and is not known until the body is finalised, the cross-reference cannot be filled at emission time; it requires a post-emission resolution pass.

This section defines the rules a DSL runner MUST satisfy when adapters emit multiple traces with cross-references between them. The rules are independent of how the adapter declares its output structure; that is the subject of §14.

### 13.1 Cross-trace placeholder syntax

A producer signals an unresolved cross-trace reference by writing a placeholder string of the form:

```
${trace:<trace-id>:<field>}
```

where `<trace-id>` is the `id` of another trace in the same emitted set (§13.2) and `<field>` names which property of the referenced trace is being substituted. The syntax is structurally extensible to multiple field types; v0.3.0 defines exactly one valid `<field>` value:

| `<field>` | Substitutes |
|---|---|
| `content_hash` | the referenced trace's `content_hash` (per `minitrace-spec` §3.4), in `sha256:<hex>` form |

Future spec revisions MAY add new valid `<field>` values. A runner MUST reject at load, as a structural failure, an adapter whose cross-trace template names any other `<field>`.

Placeholders MUST appear only in *placeholder-bearing fields*. This version defines exactly one: `target_content_hash` of a `lineage[]` edge.

A placeholder is recognised by where the adapter writes it, not by the shape of a value. In an `emit_lineage` action, `target_content_hash` is either absent, the null literal, or a whole cross-trace template `${trace:<trace-id template>:content_hash}`, whose trace-id portion may contain inner `${...}` parts. Any other value is a structural failure that a runner MUST detect at load: an expression, a template with text outside the `${trace:...}` form, a literal hash, or any other literal. A value taken from source data therefore reaches this field only through the trace-id portion of an adapter-written template, and never decides by itself whether the field holds a placeholder. Hashes are not carried into this field from source data in this version; a later revision may allow it (§16). For the same reason, `lineage` is written only by `emit_lineage`. Any other write whose target is `lineage` or lies below it is a structural failure that a runner MUST detect at load: in `session.static`, `session.derived`, `session.passthrough` or `finalize.computed` (and their `sessions.*` forms), or a `set` to `session.lineage`. So every lineage edge in a trace comes from an `emit_lineage` action whose `target_content_hash` passed the check above.

A value written in that form is a placeholder if and only if its whole value, after interpolation of any inner `${...}` (§8.4), matches `^\$\{trace:(.*):([a-z_]+)\}$`. In this pattern `.` matches any character, line terminators included. The first group is the `<trace-id>`: everything between `${trace:` and the last `:` of the value, so trace ids are not restricted in the characters they may contain. It may be empty. A trace-id portion that holds an unusable part (R5) makes the placeholder unresolvable (R4), whatever it renders to. The second group is the `<field>`.

A non-null `target_content_hash` MUST hold either a placeholder (before publication only) or a hash matching the pattern the base schema already defines for this field, `^sha256:[0-9a-f]{64}$` (`minitrace-spec` §3.4). Any other value is invalid and verifiers MUST reject it.

A substring of placeholder shape in any other field is content. Runners, verifiers and consumers MUST NOT treat it as a reference and MUST NOT reject the trace on account of it. A future revision may add placeholder-bearing fields; producers MUST NOT use any other field for cross-trace references.

A trace is said to have *outgoing placeholders* if any of its placeholder-bearing fields holds a placeholder. The set of trace ids referenced by a trace's outgoing placeholders is its *outgoing reference set*.

The cross-trace placeholder is recognised as a special form during the runner's string-interpolation pass per §8.4: the `trace:` prefix is detected before the body is evaluated as an expression. Inner `${...}` interpolations within the trace-id portion (e.g. `${trace:agent-${record.id}:content_hash}`) are resolved first; the result is then carried through to the post-emission resolution pass below.

### 13.2 Normative rules

**Emitted set.** R5 checks id uniqueness across every trace a runner builds from one input (one file or stream, §4), across all of its containers, records and declared trace shapes. The *emitted set* is what remains of those traces once R5, the turn `index` check (§4), the I-JSON check and, where a runner applies one before resolution, its naming rule for output files (§15, Writing output) have removed what they remove; it is the scope of resolution (R1 to R4). A placeholder may name any trace in the emitted set, whichever record or container produced it: a parent session's trace may reference a subagent session that is another container of the same input. A trace built from another input is outside the set, so a placeholder naming it is unresolvable (R4). Combining several inputs into one set is not covered by this version (§0).

**Which traces carry `content_hash`.** Every trace emitted by an adapter that declares `sessions:` (multi-trace mode, §14.2) MUST carry `content_hash`, stamped after resolution as R1 and R2 describe, whether or not it takes part in any cross-trace reference. A trace emitted by an adapter that declares `session:` (single-trace mode) MUST NOT carry one from the runner: `content_hash` is optional in `minitrace-spec` (§3.4), and such a trace may be stamped later, for example when an envelope is assembled. Because single-trace mode has no resolution pass, a cross-trace placeholder template (§13.1) in an adapter that declares `session:` is a structural failure at load; an `emit_lineage` there may leave `target_content_hash` absent or null.

**R1 — Hash-after-resolution.** When a trace contains one or more outgoing cross-trace placeholders, the runner MUST resolve those placeholders BEFORE computing the trace's `content_hash`. The hash covers the trace's canonical body with all outgoing references already substituted to their resolved values. This guarantees the round-trip property: a verifier who canonicalises the published trace and computes SHA-256 obtains the same value the producer stamped.

**R2 — One-shot back-fill.** Cross-trace reference resolution is a one-shot operation per emission cycle. A trace is considered resolved once its `content_hash` field is populated. Runners MUST NOT re-apply resolution to already-resolved traces. The integrity-bearing `content_hash` is the value stamped at first resolution.

*Note (informative):* under R1 the original commitment is over the resolved body, so a correct re-application of the algorithm to an unmutated trace is idempotent (the hash matches). The rule is nonetheless tightened to forbid re-application: it eliminates a class of latent bugs where intermediate state changes between passes (mutation, partial re-resolution, future spec extensions adding non-idempotent steps) would produce drift that a re-application would silently overwrite. R2 is producer-side discipline; consumers are unaffected.

**R3 — Acyclic graph.** The cross-trace placeholder graph MUST be acyclic. Cycles of any length (A → B → A; A → B → C → A; longer chains) are unresolvable: each trace's hash depends on at least one other's, with no terminating leaf. Runners MUST detect cyclic reference graphs. Because trace ids and lineage targets can come from source data, a cycle can be built by input (a session listed as its own subagent, or two sessions listing each other), so a cycle does not abort the set. Instead, the runner computes the cyclic components of the placeholder graph: the strongly connected components with more than one trace, and any single trace with a placeholder edge to itself. The graph is taken after R4 and R5 have removed what they remove. The runner omits every placeholder edge whose source and target lie in the same cyclic component, and keeps edges that leave a component. One pass removes every cycle, since the edges that remain only join different components, so no recomputation is needed. The rule does not rank traces: neither trace ids nor input order decide which edge is lost, so data that builds a cycle cannot choose which trace in it loses an edge, and no trace keeps an integrity-bearing edge into its own cycle. Omitting an edge means what it means under R4: the lineage edge is removed from the trace, never kept with a null `target_content_hash`. The runner reports every omitted edge in the conversion's diagnostics and emits the remaining traces. The rule is deterministic, so the surviving edges and every `content_hash` do not depend on iteration order. The diagnostic mechanism is implementation-defined.

**R4 — Unresolvable placeholders degrade.** A lineage edge holding a placeholder is resolvable only if its `target_trace_id` is a string equal to the placeholder's rendered trace-id; one that differs, or is absent, null or not a string, claims to describe one trace while binding another's hash (or none), so it is unresolvable too, and the runner omits and reports it rather than aborting the set. A placeholder whose trace-id is empty, or names no trace in the emitted set (for example a peer missing from a partial capture), cannot be resolved. A runner MUST NOT abort the set on its account: it omits the lineage edge holding that placeholder, reports the edge in the conversion's diagnostics, and emits the remaining traces. It MUST NOT emit the edge with a null `target_content_hash`, because an edge requiring integrity is not created without the referenced hash (`minitrace-spec` §3.4, §7.1). A placeholder whose trace-id portion contains an unusable part (R5: null, the empty string, a boolean, an object or a list) is unresolvable and falls under this rule, whatever the rendered trace-id would be, so a missing source id cannot resolve to a trace whose id happens to equal the rendered text. So does a placeholder template whose inner part fails: under §8.3 the template then evaluates to null, and the runner omits the edge and reports it rather than emitting it with a null `target_content_hash`. A null literal written by the adapter (§13.1) is not a placeholder and is unaffected.

**R5 — Unusable and duplicate trace ids degrade.** Trace ids can come from source data, so an id that cannot be used does not abort the set. Before resolution, the runner applies two rules to every trace it built, in single-trace and multi-trace mode alike.
- *Usable id.* A trace's `id` MUST be a non-empty string (`minitrace-spec` base schema). A runner does not convert other values to strings: an adapter that needs a numeric source id as a string writes it as a template (`"${container.id}"`), which renders under §8.3. If the `id` value is a template and any of its `${...}` parts evaluates to an *unusable part*, the id is unusable too, whatever the rendered text would be. An unusable part is null, the empty string, a boolean, an object or a list; a non-empty string or a number is usable, so `${container_index}` builds ids. This is an exception, for the `id` field and for a placeholder's trace-id portion (R4) only, to §8.3's rendering of these values, so that `agent-${record.id}` with a missing or empty source id does not become `agent-`. A number and a string that render alike (`5` and `"5"`) give the same id; two such traces fall under the unique-id rule below. A trace whose id is null, empty, not a string, or unusable this way is not emitted: the runner reports it in the conversion's diagnostics, a set-level loss (§15). Such a trace takes no part in the unique-id rule below.
- *Unique id.* If two or more traces built from the input have the same id, the runner emits none of them and reports the id, a set-level loss (§15). A trace not emitted for its turn `index` (§4), or not written because it is not I-JSON or, under a naming rule applied before R5, because of its file name (§15), counts here too. A trace already not emitted or not written for one of those reasons is reported for that reason; the shared id is reported once, if a trace still to be emitted holds it. It does not keep the first: a record earlier in the input could otherwise plant a trace that shadows the genuine one and draw its lineage edges.

A trace removed under R5, not emitted for its turn `index` (§4), or not written because it is not I-JSON or, under a naming rule applied before resolution, because of its file name (§15), is not in the emitted set, so a placeholder naming its id is unresolvable and falls under R4. A trace removed under R5 never leads to the set being aborted.

### 13.3 Resolution algorithm (informative)

A runner satisfying R1 to R5 may use any algorithm. A topological resolution is one straightforward implementation:

1. Apply R5: remove every trace whose id is unusable and every trace whose id another trace shares, counting traces not emitted for their turn `index` (§4) or not written because they are not I-JSON (§15) when checking for a shared id and removing those too, reporting each trace whose id is unusable, and each shared id once if a trace still to be emitted holds it. Then, where a runner applies its naming rule for output files here (§15, Writing output), remove the traces it refuses, with a diagnostic for each. Index the remaining traces by `id`.
2. Build the dependency graph: for each trace, its outgoing reference set. A reference is unresolvable, and its edge is omitted per R4, if its trace id is empty or names no trace in the emitted set, if its trace-id portion holds an unusable part, or if the edge's `target_trace_id` does not equal its rendered trace-id.
3. Iterate: a trace is *ready* when its outgoing reference set is a subset of the already-resolved trace ids. For a ready trace: replace the whole value of each placeholder-bearing field that holds a placeholder with the corresponding peer hash, THEN compute its `content_hash` over the resolved canonical body, THEN add it to the resolved set.
4. Repeat until all traces are resolved. If at any iteration no trace is ready and the resolved set is incomplete, the graph contains a cycle. Apply R3's omission rule (every placeholder edge inside a cyclic component) and continue. Equivalently, apply R3 once before step 3.

This algorithm terminates for acyclic graphs (the topological order ensures progress) and detects cycles (the no-progress condition is the cycle signal).

### 13.4 Verifier contract

A consumer verifying integrity of a published trace MUST:

1. Receive the trace with all placeholders already resolved (a placeholder-bearing field of a published trace MUST NOT hold a placeholder; an emission that does is a producer-side bug — see §14.7). Verifiers MUST inspect placeholder-bearing fields only, and MUST reject a trace whose `target_content_hash` holds anything other than null or a well-formed hash (§13.1).
2. Canonicalise the trace's body (excluding `content_hash`) per `minitrace-spec` §3.4 canonicalisation rules.
3. Compute SHA-256 over the canonical form.
4. Compare to the trace's `content_hash`.

If R1 is satisfied at emission time, this verification round-trips successfully. If the trace's content has been mutated after emission, the recomputed hash diverges from the stored value — exactly the integrity contract `minitrace-spec` §3.4 specifies.

For cross-trace references: a consumer fetching a trace referenced by a lineage edge's `target_content_hash` MUST recompute that referenced trace's hash and compare. A mismatch means either the referenced trace has been mutated since the reference was authored, or the producer's emission did not satisfy R1.

If a referenced trace is not available to the verifier (the producer published trace B containing a reference to trace A's hash, but trace A is not in the consumer's working set), the cross-reference cannot be verified. Reporting of unverifiable references is implementation-defined: a consumer may treat unavailability as failure, as unverifiable-but-not-mismatched, or as a warning. Producers SHOULD make all referenced traces discoverable through the envelope (sibling spec) when redistributing trace bundles, so verifiers do not encounter unavailability in well-formed bundles.

---

## 14. Multi-trace adapter declaration

§13 defines the runtime rules for cross-trace references. This section defines how an adapter declares that it emits more than one trace from a single input record.

### 14.1 Single-trace adapters (baseline)

An adapter that declares one trace shape uses the singular `session:` block per §7; the runner builds one trace from each container, or one from the whole input when there is no container (§4.1; §14.2 names the cases in which none is built). This is the existing single-trace mode; multi-trace emission is an additive layer over it.

### 14.2 Multi-trace declaration

An adapter MAY declare more than one trace shape using a plural `sessions:` block whose top-level keys are *DSL-local handles* — names used within the adapter file to refer to each trace shape. The DSL-local handle is NOT the same as the runtime `id` field of the produced trace; it is a compile-time identifier within the adapter.

Each declared trace shape:

- Carries its own `static` / `derived` / `passthrough` field declarations (per §7).
- Produces, at runtime, one trace per container (or one for the whole input when there is no container; none in the cases named below), with its `id` computed like any other trace.

```yaml
sessions:
  agent:
    static: { trace_type: "agent-session", schema_version: "0.3.0" }
    derived: { id: "agent-${container.id}", title: "${container.id}" }
  judge:
    static: { trace_type: "judge-session", schema_version: "0.3.0" }
    derived: { id: "judge-${container.id}", title: "Judgment for ${container.id}" }
```

An adapter MUST NOT have both `session:` (singular) and `sessions:` (plural). The runner picks single- or multi-trace mode by which key is present.

The runner builds exactly one trace per declared trace shape for each container, or for the whole input when there is no container, whether or not any rule that targets the shape fires. In these cases of §15 (Unreadable and empty input) there is none: an input that cannot be read as a whole and an input with nothing to convert write no trace, and, when there is no container, no trace is built from an input whose every record or element was skipped. In container mode, no trace is built for a top-level element skipped as unreadable (§15), so an input whose every top-level element was skipped has none. Each trace that is built is emitted unless R5 (§13.2) removes it or a turn `index` that is not its position (§4) drops it; the depth limit (§4) truncates, it never removes. A built trace is also not written in the cases §15 states under Writing output. No trace from the input is written when the adapter is refused at load (§8.3, §15) or the runtime operator values are refused (§7.1), both before any trace is built, or when the conversion ends before output: on a structural failure that shows during evaluation (§8.3), or when the input proves unreadable as a whole or the output location is refused (§15, Unreadable and empty input).

### 14.3 Targeted emission

When an adapter declares more than one trace shape, every emit rule MUST identify which trace shape it targets via a `target:` field. An adapter using `sessions:` (plural) without a `target:` on every emit rule is invalid, and so is one with an emit rule whose `target:` names no shape declared under `sessions:` (names compared exactly, case included): it identifies no shape. The runner MUST refuse to run either, at load (§15).

Single-trace adapters using `session:` (singular) do not need `target:`, since there is only one shape. A `target:` on a rule of a single-trace adapter is permitted and has no effect.

```yaml
emit:
  - when: { ... }
    target: agent
    do:
      - emit_turn: { ... }

  - when: { ... }
    target: judge
    do:
      - emit_lineage:
          type: judges
          target_trace_id: "agent-${container.id}"
          target_content_hash: "${trace:agent-${container.id}:content_hash}"
          authored_by: "atbench-claw-auditor"
          authored_at: "2026-04-08T00:00:00.000000Z"
```

The following emit actions append to the v0.3.0 secondary event/record arrays. They were added in Draft 2 (the first four) and Draft 3 (`emit_segment_marker`); they are available to both multi-trace and single-trace adapters. (`emit_policy_trigger` and `emit_segment_marker` write primary top-level arrays of `minitrace-spec` §4 and are commonly used in single-trace adapters; they are grouped here with the other v0.3.0-array emit actions for one place to find them.)

| Action | Purpose |
|---|---|
| `emit_lineage` | Append a lineage edge to the targeted trace's `lineage[]` (per `minitrace-spec` §7). |
| `emit_annotation` | Append an annotation to the targeted trace's `annotations[]` (per `minitrace-spec` §6). |
| `emit_redaction` | Append a redaction record to the targeted trace's `redactions[]` (per `minitrace-spec` §8). |
| `emit_policy_trigger` | Append a policy trigger to the targeted trace's `policy_triggers[]` (per `minitrace-spec` §4.6). |
| `emit_segment_marker` | Append a segment marker to the targeted trace's `segment_markers[]` (per `minitrace-spec` §4.5). |

These actions follow the same shape as `emit_turn` and `emit_tool_call`: a YAML mapping with the action name as the top-level key and the field map below.

**`emit_segment_marker` field shape.** The materialised marker conforms to `minitrace-spec` §4.5 (`SegmentMarker`):

```yaml
- emit_segment_marker:
    id: "claude-code-sm-pm-${container.session_id}-${state.next_sm_id}"
    timestamp: "$record.timestamp"          # ISO 8601, or null where the source has no marker timestamp
    segment_type: "mode_shift"              # see enumeration below
    segment_label: "$record.permission_mode" # OPTIONAL free text; canonical field name is segment_label
```

Normative notes for `emit_segment_marker`:

- **`segment_type` is a closed enumeration with an `x-` escape hatch.** Per `minitrace-spec` §4.5 the permitted values are `mode_shift`, `focus_shift`, `context_reset`, `operator_change`, and any `x-`-prefixed extension value. An adapter that needs a segment class outside the closed set MUST use an `x-` value (e.g. `x-agent_plan_snapshot`); it MUST NOT introduce a bare value, and it MUST NOT alter the §4.5 enumeration (that is a format-spec change, not an adapter or DSL change).
- **`segment_label` is the canonical label field**, not `label`. The runner materialises emit-action fields verbatim, so emitting `label` produces a non-canonical `label` field and leaves `segment_label` unset. Adapters MUST emit `segment_label`.
- **Extension fields are permitted.** `SegmentMarker` carries `additionalProperties: true`, so adapter-specific fields (e.g. a `taxonomy` array or an `authored_by` provenance string) are valid. Such fields are descriptive only; analysis tools that derive segment spans (§4.5) rely solely on `segment_type`, `timestamp`, and event order.

### 14.4 Cross-trace references between declared shapes

To author a cross-trace reference between two trace shapes declared in the same adapter, the producer:

1. Computes the runtime `id` of the referenced trace from the input record using the existing expression mechanism (`${...}` interpolation).
2. Inserts a cross-trace placeholder per §13.1 as the whole value of a placeholder-bearing field of the referencing trace, with the computed runtime id as `<trace-id>`.

The DSL does not provide a special primitive for "the peer trace's id." This is a deliberate design choice: cross-trace references work the same way whether the two traces are emitted from the same record (a "trace pair") or from different records or containers of the same input (for example a parent session referencing a subagent session). Both are allowed; the scope is the emitted set (§13.2).

For trace-pair adapters specifically (one input record → one agent-session + one judge-session), the typical pattern is:

- Both trace shapes derive their runtime `id` from a container-level identifier such that the agent's id is `agent-X` and the judge's id is `judge-X` where `X` is that identifier.
- The judge's lineage edge contains a cross-trace placeholder whose `<trace-id>` is the agent's runtime id (`agent-X`).
- Evaluation against the same container makes both shapes' `id` derivations and the placeholder's `<trace-id>` resolve to the same `X`, so the cross-reference points at the correct peer.

### 14.5 Order-independence of declaration

The order in which trace shapes are declared in the adapter file is NOT significant. The runtime resolution order is determined by the cross-trace placeholder dependency graph (§13.3), not by declaration order. An adapter author MAY declare shapes in any order that aids readability.

### 14.6 Field preservation across shapes

The field-preservation rule in `minitrace-spec` §3.1 applies independently to each declared trace shape. A producer MUST NOT strip fields from one trace shape because they happen to appear in another shape; trace shapes are independent objects and field preservation is per-shape.

### 14.7 Producer-side discipline on emitted traces

Emitted traces MUST NOT contain unresolved DSL interpolation tokens in any string field. Specifically: any substring of the form `${...}` in an emitted trace's string fields (whether at the top level, inside `taxonomy[]` entries, inside `content.text`, or anywhere a string is published) MUST either be:

1. A cross-trace placeholder in a placeholder-bearing field (§13.1) — and these MUST be resolved by the cross-trace back-fill pass (§13.3) before publication. Post-resolution traces MUST hold no placeholder in any placeholder-bearing field.
2. A token whose source is the corpus content itself, not the adapter's DSL syntax (e.g. a synthetic trajectory whose tool output legitimately contains the literal string `${TMPDIR}` as part of a shell-script payload).

The same holds for expression source text that carries no `${...}` marker: an emitted trace MUST NOT contain, as a value, the source text of an expression that failed to evaluate (§8.3), with or without its `$` prefixes. A post-emission scan cannot reliably find such text, so this rule is enforced at evaluation, not after it.

Producers MUST NOT emit traces containing unresolved DSL-syntax tokens such as `${state.<x>}`, `${container.<x>}`, `${container_index}`, `${block.<x>}`, `${adapter.<x>}`, `${constants.<x>}`, `${record.<x>}`. Such tokens indicate a DSL interpolation that did not run; emitting them is a producer conformance failure.

Producers SHOULD run a post-emission scan over all emitted traces, flagging any `${...}` token that is not (1) a cross-trace placeholder still awaiting resolution (during pre-resolution checks only) or (2) recoverable from corpus content. The scan is implementation-cheap; absence of it has historically allowed structurally-valid traces with content-incorrect interpolation tokens to ship.

**Recommended forbidden-token scan.** A regex over emitted trace text:

```
\$\{(?:state|container|container_index|container_first|block|adapter|constants|trace|record|record_first)(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*\}
```

matches the DSL's interpolation syntax forms. Tokens matching this regex AND not preceded by `trace:` (i.e. not a cross-trace placeholder) are producer bugs. Allowlisted exceptions for specific corpora (e.g. the literal strings `${TMPDIR}`, `${MODEL_ID}` appearing in synthetic-agent tool output) are the producer's responsibility to document.

The scan is a producer-side check: only the producer knows whether a token originated in the adapter's DSL or in the corpus (item 2), and a trace does not record which. Validators MUST NOT reject a trace on the basis of this scan. The recommended regex deliberately does not match the `trace:` form, which §13.1 and §8.4 govern.

---

## 15. Errors and diagnostics

The interpreter MUST report:

- Unknown DSL versions (refuse to run).
- Unknown action types (refuse to run).
- Other shape errors in the DSL, which are structural failures at load (§8.3): an unknown key in a rule (other than `name`, `when`, `do`, `foreach_block`, `on_block`, `target`); an unknown key beside an action (other than `omit_if_none` beside `emit_turn`); a predicate whose form or operands §8.1 does not allow; a `block_type` or `block_field` predicate outside `on_block`; an `on_block` sub-rule with a key other than `name`, `when` and `do`; a literal `foreach_block` value that is neither a string nor null (§8); in an adapter using `sessions:`, an emit rule without `target:` or whose `target:` names no declared shape (§14.3); an `attach_tool_result` or `note` body key, or body value, other than §8.2.4 and §8.2.6 allow; an `accumulate` key that is not a `state.` path; a key with an empty segment (`state.a..b`, `state.c.`, `state.`, `flags..x`) in a `set` or `accumulate`, in `finalize.computed`, or in a `static`, `derived` or `passthrough` map of `session` or of a `sessions` shape; a key `operator`, or one beneath it, in such a map or in `finalize.computed`, or a `set` key `session.operator` or one beneath it (§7.1); both `session` and `sessions`, or neither (§2); an `input` that is not a mapping, or whose `type` is absent or other than `json` or `jsonl`, or whose `encoding` is present and other than `utf-8` (§4); a key in the `adapter` block other than the six §3 shows; a `set` key under neither `state.` nor `session.` (§8.2.1); a rule or an `on_block` sub-rule without `when`, and `foreach_block` without `on_block` or `on_block` without `foreach_block` (§8); a `state`, `constants`, `finalize` or `finalize.computed` that is not a mapping, or a `static`, `derived` or `passthrough` map of `session` or of a `sessions` shape that is not a mapping (§5, §6, §7, §9); a `session` or `sessions` that is not a mapping, a `sessions` that declares no shape, or a `sessions` shape that is not a mapping (§7, §14); an `adapter` block that is absent, not a mapping or without `dsl_version` (§3); an `input.container` that is not a mapping, whose `record_path` is absent or not a string, or with a key other than `record_path` (§4.1); a rule with `when` but neither `do` nor `foreach_block`, or an `on_block` sub-rule without `do` (§8); a `static`, `derived`, `passthrough` or `finalize.computed` key beneath a `static` or `passthrough` literal that is a string, a number, a boolean or a list, in `session` or in a `sessions` shape (§7).
- Unresolved references (e.g. `$record.foo` when `foo` does not exist): not a failure (§8.3). The value is null and a runner MAY report it; it never aborts the conversion, and no option makes it fatal, since that would change which adapters and inputs produce output.
- `attach_tool_result` against an unknown `target_id` — soft warning recorded in diagnostics.

Diagnostics are emitted alongside the produced trace in a sibling file (interpreter-defined location). They are not part of the trace.

**Losses.** Several rules in this specification degrade instead of aborting, so that one hostile or broken record cannot suppress the whole output. A runner MUST report every loss they cause, in two classes:

- *Set-level losses:* a trace not emitted (R5, §13.2; a turn `index` that is not its position, §4), a trace not written (Writing output, below), and a record, a top-level element or an item of a container's `record_path` list skipped as unreadable (for example deeper than the reading bound of §4, or not an object, Unreadable and empty input, below).
- *Value-level losses:* a lineage edge omitted (R3, R4, §13.2), and any input failure (§8.3) other than a summary that stands for a skipped record, element or `record_path` item (below), including a value that became null, a value truncated for depth (§4), a numeric field nulled or omitted (§4), a tool call not emitted for an unusable `id` (§8.2.3), tool calls removed for a shared id (§8.2.3), an `accumulate` that kept its prior value, a container whose `record_path` value is neither a list nor null (§4.1.1), whose traces are still built from no records, and a `foreach_block` iteration skipped.

A summary under the reporting bound of §4 is a value-level loss, unless any finding it stands for was a skipped record, element or `record_path` item, in which case it is a set-level loss: the bound never hides the set-level signal, and truncations alone never raise it.

A runner MUST let a caller tell whether any set-level loss occurred without parsing the text of diagnostics: for example through its exit status, or through a field in the diagnostics. The mechanism and the names of severity levels are implementation-defined. A conversion with set-level losses is therefore never indistinguishable from a complete one, because R5 lets a record that copies another's id remove both traces, and ids are often predictable.

**Unreadable and empty input.** An input cannot be read as a whole when the runner cannot obtain all of its records or elements and the reason belongs to no single record, element or line: for example an input that cannot be opened or read (it is missing, it is not a file, or the runner is not permitted to read it), a `json` input that is not well-formed JSON, in container mode a `json` input whose root is not an array (§4.1), an input the runner finds changed while it reads it, or an input that as a whole exceeds a limit the runner documents (on its size, or on the number of its records). Such an input ends the conversion: no trace is written, the runner reports why, and a caller MUST be able to tell this failure from a complete conversion and from one with set-level losses without parsing the text of diagnostics. An input with nothing to convert (without a container, no records: an empty root array, or a `jsonl` input that is empty or whose lines are all blank; in container mode, no top-level elements) is a complete conversion that writes no trace: the runner reports it as a warning, and it raises no set-level signal. A blank `jsonl` line (empty, or JSON whitespace only) is neither a record nor a top-level element and is ignored, unreported, wherever it stands. With or without a container, a `jsonl` line that is not blank is skipped as unreadable if it is not well-formed JSON or its value is not an object, and so is an element of a `json` input's root array that is not an object. In container mode, an item of a container's `record_path` list that is not an object is skipped in the same way (§4.1.1). Without a container, a `json` root that is neither an array nor an object is skipped in the same way; a root that is an object is the input's one record. Each such skip is a set-level loss (above), and the other records or elements are converted. An input whose every record or element was skipped is neither an input with nothing to convert nor one that cannot be read as a whole: each skip is a set-level loss, and without a container no trace is built, as none is in container mode for a top-level element that was skipped (§14.2) (without a container, a `json` root that is not an array and is deeper than the reading bound of §4 is such a skipped record, not an unreadable input). A container that is not skipped has its traces built even when every item of its `record_path` list was skipped (§4.1.1). Bytes not valid UTF-8 are named as §4 states and do not by themselves make an input or a line unreadable: they are replaced and reported (§4, Bytes in the input). Whatever the outcome, a runner MUST NOT leave, where a caller would read it as this conversion's output, a trace that an earlier conversion of the same input location with the same adapter wrote and this conversion did not write; a runner MAY instead refuse, before writing, an output location that already holds a trace, reporting the refusal as a failure a caller can tell apart, as for an unreadable input.

**Writing output.** How a runner stores traces (one file, a directory of files, a stream) is implementation-defined. A runner MUST NOT abort a conversion because of one trace's data when it writes output. A trace it cannot write is a set-level loss: it is not written, it is reported, and the set-level signal is raised. That covers an id that is not usable as a file name under the runner's own naming rule, and an id that collides with another under that rule (for example ids that differ only in letter case on a case-insensitive file system). In a collision none of the colliding traces is written, as under R5: keeping the first would let one record shadow another. Only traces still to be written take part in the collision check. An error of the output while writing that any naming rule the runner applies does not catch, such as a full file system, a file-size or path-length limit (even when one trace's size is what reaches it) or a permission error, is a failure of the output, not of one trace's data: it ends the conversion, reported as a failure a caller can tell apart from a complete conversion and from one with set-level losses (Unreadable and empty input); whether the traces this run already wrote are removed is implementation-defined. A runner SHOULD apply such a naming rule after R5 and before cross-trace resolution, so that no trace it writes references a trace it does not write; a trace not written for its file name has then taken part in R5's unique-id rule, and an id shared by two or more traces is reported under R5, not as a collision; a runner that applies it before R5 counts a trace not written for its file name for R5's unique-id rule, as it does a trace not written because it is not I-JSON. The same holds for a trace that still holds an unpaired surrogate or a noncharacter, a member name included, when it is about to be written: it is not written, a set-level loss. §4 repairs such characters in the input, §2 refuses them in the DSL and §7.1 in runtime operator values, so this check is a backstop. A runner MUST apply it before cross-trace resolution and before stamping any `content_hash`, since a trace that is not I-JSON has no canonical form to hash (`minitrace-spec` §3.4); a trace not written for this reason counts for R5's unique-id rule (§13.2), as a trace not emitted for its turn `index` does.

---

## 16. Versioning

`dsl_version` follows the version policy of `minitrace-spec` §3.1, which states what a revision that changes `Y` or only `Z` of a version `0.Y.Z` may contain; for the DSL, additions are new actions, built-ins, predicates and emit actions. A v0.1.x adapter works under a v0.1.y interpreter where y >= x. After a revision that changes `Y`, or the revision to `1.0.0`, adapters MUST update their `dsl_version` and re-test.

**Convention absorption.** The DSL vocabulary grows only when a new adapter cannot be expressed in the existing vocabulary. A primitive (action, built-in, predicate) is added in a revision that changes only `Z` when, and only when, an adapter author demonstrates that a target harness output cannot be mapped without it. The DSL does not add speculative primitives ahead of an adapter that needs them.

Draft 2 of v0.1.0 is additive over Draft 1: every Draft 1 adapter remains valid under Draft 2 without modification. Draft 2 expands the vocabulary (multi-trace declaration, deep-path `record_path`, `$self` sentinel, `container_field` and `block_field` predicates, additional builtins, `emit_lineage` / `emit_annotation` / `emit_redaction` / `emit_policy_trigger` actions) but does not redefine, drop, or restructure any Draft 1 element.

Draft 3 of v0.1.0 is additive over Draft 2: it documents the `emit_segment_marker` action (§14.3) already implemented in the runner and emitted by the claude-code and droid adapters. Every Draft 2 adapter remains valid under Draft 3 without modification; no element is redefined, dropped, or restructured.

---

## 17. JSON Schema

The DSL itself has a JSON Schema at `schemas/minitrace-adapter-dsl-v0.1.0.schema.json` (separate file), validating the YAML structure of an adapter file. Adapter files SHOULD validate against this schema before being loaded by the interpreter.

---

*End of v0.1.0.*
