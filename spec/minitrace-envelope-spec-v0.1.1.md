# minitrace-envelope Specification v0.1.1

**Status:** Release. v0.1.0 RC 1 remains unchanged.
**Date:** 2026-10-03 (RC 1); last amended 2026-10-28.
**Companion to:** `minitrace-spec` v0.3.0 (trace format), `minitrace-scenario-spec` v0.1.0 (scenario format and binding record), `minitrace-adapter-dsl-spec` v0.1.0 (adapter binding language).
**Predecessor:** v0.1.0 RC 1.

---

## 0. Summary

The envelope is a sibling to the trace format. A minitrace trace describes session behaviour; an envelope packages one or more traces with everything else needed to treat the bundle as evidence: pre-conversion harness logs, witness artefacts (network captures, screenshots, environment snapshots, inference probes), clock attestation, and producer attestation.

v0.1.0 establishes:

- A directory-based container with a single `manifest.json` at root and content-addressed sidecar files for binary and large artefacts.
- A typed witness model with extensibility under `x-*` and `ext:<vendor>:*`.
- A `bindings[]` listing (§4.11) for checksummed sidecars that bind external identity/provenance — the adapter that produced a trace, and the `(model, harness, scenario)` triple a run was for — recorded in this manifest. (The same sidecars may also be sealed at production time into the producing apparatus's own integrity manifest; that manifest is a separate artefact, per §4.11.)
- A flexible clock attestation framework that admits multiple clocks per envelope, including deliberately manipulated clocks for adversarial-condition research.
- A signature framework with a placeholder cryptographic profile and an explicit redaction policy (§9).
- Cross-references to `minitrace-spec` v0.3.0 for trace `content_hash`, lineage, redaction, and security/privacy considerations.

The envelope is designed so that adding a new witness kind, recording a manipulated clock, or carrying a previously unmodelled artefact does not require a new envelope version. v0.1.0 is intentionally a small core with strong extension points.

---

## 1. Scope

### 1.1 What an envelope is

An envelope is a bundle of artefacts about one or more sessions. It carries:

- One or more minitrace traces (the canonical behavioural record).
- Pre-conversion harness output (raw logs, source files, intermediate state) — preserved for evidentiary chain of custody.
- Witnesses: typed observational artefacts that did not fit into the trace itself (packet captures, screenshots, environment snapshots, inference probes, future modalities).
- Clock attestation: which clocks the bundle's timestamps were produced against.
- Producer attestation: who built the envelope, optionally signed.
- A manifest that lists every artefact with its content hash.

### 1.2 What an envelope is not

- Not a transport format. Compression and packaging for transport (zip, tar, OCI image) are out of scope; the spec defines a directory layout that can be packed into any of those.
- Not a behavioural record. Behaviour lives in the trace (§1 of `minitrace-spec`); the envelope's job is to make traces verifiable as evidence.
- Not a signature scheme. v0.1.0 names a signature field and defines what it covers, but does not normatively pick a cryptographic algorithm. See §9.
- Not a research data format. Research-specific witness payloads (transformer-lens activations, attention captures, probe outputs) are carried as opaque content addressable by the witness model; their internal format is governed elsewhere.

### 1.3 Three-layer architecture

| Layer | Specification | Scope |
|---|---|---|
| Trace | `minitrace-spec` v0.3.0 | Behavioural session content |
| Envelope | This document | Operational integrity, manifest, witnesses, clocks, attestation |
| Adapter | Per-harness DSL files | Mapping from harness-native output to canonical trace |

A trace exists independently of an envelope (per `minitrace-spec` §1.3). An envelope MUST reference at least one trace.

### 1.4 Instrumented and assembled envelopes

The artefacts an envelope binds may be produced by a single instrumented run, or gathered afterwards from material that already existed. Both are envelopes, and the difference between them is provenance rather than kind.

In the **instrumented** case a capture pipeline runs the session and its observers together. Every artefact shares one run identity, the observers declare what they captured, and correlation keys are present by construction. This is the case with the most complete evidence, and it is the case the reference tooling produces.

In the **assembled** case the artefacts are collected after the session ended, possibly from several hosts, possibly incomplete, and possibly with no record of what was not collected. Reconstruction and forensic use fall here, as does any bundle built from logs that were retained for some other purpose. `producer.kind` (§4.5) distinguishes the two: `automated_pipeline` for a bundle a tool emitted, `human_assembled` for one a person curated, and `hybrid` where a tool emitted and a person revised.

An assembled envelope is not a degraded instrumented one. It is the ordinary case for evidence that was not collected under observation, and the format's obligation is the same in both: bind the artefacts to one session and record what is known about each of them, including that something is not known. §6.1 applies unchanged, so material that has not yet been reconstructed into a trace is not yet an envelope.

#### 1.4.1 Declared status rather than inferred completeness

Several rules in this specification exist so that a consumer can distinguish an absence of evidence from evidence of absence. They were written for different layers and they share one shape, which this section names so that the next such rule is not invented independently.

| Mechanism | Section | The ambiguity it removes |
|---|---|---|
| `producer.kind` | §4.5 | A curated bundle read as an observed one |
| Raw artefact present, withheld with `original_content_hash`, or absent | §7.4, §10.4 | A withheld artefact read as one that never existed |
| `tool_call_id_attribution`, one of `always`, `when_known`, `never` | §7.6.1 | An event the module never tags read as one it failed to correlate |
| `psi_available`, and a declared witness kind whose module never captured | §7.6.9, §7.6.1 | A module that could not attach read as a module that saw nothing |
| Adversarial-condition declaration | §11.3 | A fault-injected environment read as production-grade evidence |

Where the absence of a field would otherwise be ambiguous between "not observed" and "observed to be nothing", this specification SHOULD carry the declaration that separates the two, and a producer SHOULD populate it.

#### 1.4.2 What this does not extend

The boundary in §11.2 is unchanged, and it binds an assembled envelope more tightly than an instrumented one. The envelope provides integrity over what the producer chose to bundle and does not validate independent reality. Where the producer did not observe the session, the bundle attests to what was collected and to its internal consistency, and never to its completeness or to the authenticity of the material at its source. Signing under §9 addresses the authenticity of the envelope, not of the artefacts gathered into it.

---

## 2. Terminology

**Conventions.** The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "NOT RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in BCP 14 (RFC 2119, RFC 8174) when, and only when, they appear in all capitals, as shown here.

| Term | Definition |
|---|---|
| Envelope | A directory containing a manifest and one or more bundled artefacts. The unit of evidence. |
| Manifest | The single file (`manifest.json`) at envelope root that enumerates and hashes every artefact. |
| Artefact | Any file inside the envelope listed in the manifest. Includes traces, raw logs, witnesses, clock attestation files, signature data. |
| Trace | A minitrace JSON document conforming to `minitrace-spec` v0.3.0 or a later revision of the v0.3.x line (in an envelope declaring `0.1.0`, also a v0.2.x trace, §12). |
| Witness | A typed observational artefact attesting to some aspect of the session beyond what the trace captures. |
| Clock | A logical time source against which timestamps in the bundle were produced. An envelope may declare multiple clocks. |
| Producer | The entity that authored the envelope (the system or person that assembled the manifest and bundled the artefacts). |
| Content hash | A SHA-256 over the literal byte contents of a file. Distinct from `minitrace-spec` §3.4 trace `content_hash`, which is over canonicalised JSON. The envelope manifest carries both kinds where appropriate. |

---

## 3. Container Layout

### 3.1 Directory structure

An envelope is a directory containing:

```
<envelope-root>/
  manifest.json                       # REQUIRED: §4
  signature.json                      # OPTIONAL: §9
  traces/
    <trace-id-or-name>.minitrace.json
    ...
  raw/
    <harness>/
      <preserved-source-files>
  witnesses/
    <witness-id>/
      <witness-payload-files>
  clocks/
    <clock-id>.json                   # OPTIONAL: §8
```

Implementations MAY pack this directory into any container format (zip, tar, OCI image) for transport. The packed form MUST preserve relative paths exactly as listed in the manifest.

The directory is a logical layout, not a normative filesystem hierarchy. The manifest is authoritative; subdirectory names other than `manifest.json` and `signature.json` are conventions and MAY be flattened or rearranged, provided every artefact's path in the manifest matches its actual location.

### 3.2 Roots and paths

All paths in the manifest MUST be relative to the envelope root, use forward slashes regardless of platform, and MUST NOT contain `..`, `.`, leading slashes, or symbolic-link traversals. Implementations MUST refuse to load an envelope whose manifest references a path outside the envelope root.

A single envelope root contains exactly one manifest. Nested envelopes (an envelope inside another envelope's directory) are NOT supported in v0.1.0; cross-envelope relationships are expressed via lineage edges in the contained traces, not by nesting.

### 3.3 Encoding

All text artefacts (manifest, traces, JSON witness payloads, signatures) MUST be UTF-8. Binary artefacts (packet captures, screenshots, captured executables, inference tensors) are stored raw. The manifest's content-hash entry covers raw bytes, so binary integrity is checked by byte-exact recomputation.

In an envelope that declares `0.1.1` or a later revision, `manifest.json` as a whole MUST meet the numeric domain and JSON form rules `minitrace-spec` §3.4 states for a trace. A number whose IEEE 754 binary64 value lies outside the inclusive range [−(2^53−1), 2^53−1] is carried as a JSON string, whatever its notation, and the manifest is an I-JSON message (RFC 7493): among other things, no object in it has two members with the same name, and no string, member name included, contains an unpaired surrogate or a noncharacter. The manifest is hashed and signed as literal bytes (§9) and is never canonicalised; these rules make every consumer parse the same values from those bytes, since parsers disagree on input outside them (one keeps the last of two members with the same name, another refuses the input). A manifest in which an object has two members of the same name, or a number's nearest binary64 value is infinite, has no single reading, and a consumer MUST refuse to load it, whatever revision the envelope declares; this binds the consumer, not the envelope, so §12's rule that an envelope is held to no requirement of a later revision does not lift it, and accepting a declared version (§12) is not accepting a manifest that has no single reading. A finite number outside that range carried as a JSON number, or a string or member name containing an unpaired surrogate or a noncharacter, is a failed check in an envelope that declares `0.1.1` or later, and a consumer MUST treat the envelope as informational only, not as evidence, as for a failed check of §6.2; in an envelope that declares `0.1.0`, which states neither the numeric domain rule nor the JSON form rule, a consumer notes it. Nesting depth is not extended to the manifest as a whole. Within the manifest, the witness records are held to §7.1's rules instead, nesting depth included: a departure inside one is a matter for §6.2 step 4 (§7.1), not for this paragraph, except that a record with two members of the same name or an infinite number is refused with its manifest, as above.

---

## 4. Manifest

### 4.1 Top-level shape

```json
{
  "envelope_version": "0.1.1",
  "envelope_id": "<id>",
  "created_at": "<ISO 8601 UTC at microsecond precision>",
  "producer": { ... },
  "clocks": [ ... ],
  "traces": [ ... ],
  "raw_artefacts": [ ... ],
  "witnesses": [ ... ],
  "annotations": [ ... ],
  "bindings": [ ... ],
  "extensions": { ... }
}
```

Required fields: `envelope_version`, `envelope_id`, `created_at`, `producer`, `traces`. All other top-level arrays and objects are OPTIONAL; absent equals empty array or null. (`bindings` is listed before `extensions` for readability; its subsection is §4.11, appended after `extensions` so the amendment renumbers nothing.)

### 4.2 Versioning

`envelope_version` follows the version policy of `minitrace-spec` (§3.1 of the trace spec), which states what a revision that changes `Y` or only `Z` of a version `0.Y.Z` may contain. For the envelope it reads as follows. A witness kind (§7.2) or raw-artefact kind (§4.7) in the `x-*` namespace is admitted by its pattern and is not a value of the baseline kinds, so a revision that changes only `Z` may document a new `x-*` kind (§7.6); listing it among the documented provisional kinds, in §7.6 or in the schema's conditions on them, is not a value added to an enumeration in the sense of that policy. A revision that adds a member with its own value set (`target_kind`, §7.4; `capture_status`, §7.6.1) adds a member, not a value to an existing enumeration. Promoting an `x-*` kind to baseline (§7.2, §7.6.12) adds a value to the baseline kinds and waits for a revision that changes `Y`.

### 4.3 Identity

`envelope_id` MUST be unique within any consumer's working set. Producers SHOULD use UUIDv4 or UUIDv7. The id is an identifier, not an access token.

### 4.4 Created-at

`created_at` records when the envelope was assembled. Microsecond precision per `minitrace-spec` §3.5. The clock used for `created_at` SHOULD be one of the clocks declared in §8, as §8.6 states; absent an explicit clock declaration, the envelope is assumed to use its `producer.host_clock_id` (§4.5).

### 4.5 Producer

```json
{
  "id": "<producer identifier>",
  "kind": "automated_pipeline" | "human_assembled" | "hybrid",
  "tool": "<tool name and version, if automated>",
  "host_clock_id": "<clock id from §8>",
  "notes": "<free text>"
}
```

`producer.id` is a stable identifier for the entity that assembled this envelope. `kind` distinguishes a fully automated pipeline (where the producer is a tool) from a human-assembled bundle (curated by a person) and the hybrid case (tool emits, human reviews).

### 4.6 Trace listing

```json
{
  "trace_id": "<id from trace.id>",
  "path": "traces/<file>.minitrace.json",
  "trace_content_hash": "sha256:...",
  "file_content_hash": "sha256:...",
  "trace_type": "agent-session",
  "schema_version": "0.3.0",
  "primary": true
}
```

`trace_content_hash` is the canonical-JSON hash defined by `minitrace-spec` §3.4 (over the trace's content excluding the `content_hash` field).

In an envelope that declares `0.1.1` or a later revision, every bundled trace MUST carry its own `content_hash` (`minitrace-spec` §3.4, whose table requires the hash on a trace included in an envelope integrity chain and leaves this specification to govern it). The only trace exempt from the internal `content_hash` check of §6.2 step 2 is a v0.2.x trace bundled in an envelope that declares `0.1.0`, since v0.2.x defines no `content_hash`; one such a trace carries is not checked (§6.2, §12).

`file_content_hash` is the SHA-256 over the literal bytes of the trace file as stored in the envelope. It is independent of the canonical-JSON hash; the two will differ unless the producer wrote the trace in canonical form.

`schema_version` records the version the trace's own `schema_version` declares; where the two differ, the trace's decides (§12), and a consumer notes the difference and does not fail §6.2 for it.

In an envelope that declares `0.1.1` or a later revision, a trace listing's `trace_id` MUST equal the `id` of the trace it lists. In an envelope of any revision, a consumer fails §6.2 step 2 for a trace listing whose `trace_id` differs from that trace's `id`, since the listing shape defines `trace_id` as `<id from trace.id>`, here and in v0.1.0 §4.6.

In an envelope that declares `0.1.1` or a later revision, two trace listings MUST NOT carry the same `trace_id`. In an envelope of any revision, a consumer fails §6.2 step 2 for every trace listing whose `trace_id` another trace listing also carries, as `minitrace-spec` §3.3 requires duplicate ids to be treated as an error at load time, and reports none of them as verified; a `produces_trace_id` or witness `scope.target_trace_id` naming that id names no bundled trace, and a lineage edge naming it is not compared with any bundled trace (§6.3).

`primary: true` MAY be set on at most one trace, naming the trace this envelope is "about" if there is a single such trace. Envelopes bundling judge-sessions and the sessions they judge typically mark the agent-session as primary.

### 4.7 Raw artefact listing

Pre-conversion harness output, source files used during the session, intermediate state. Each entry:

```json
{
  "id": "<id>",
  "path": "raw/<harness>/<file>",
  "kind": "harness_log" | "source_file" | "config" | "x-<custom>",
  "harness": "<harness identifier, if applicable>",
  "content_hash": "sha256:...",
  "size_bytes": <int>,
  "produces_trace_id": "<trace id, if this artefact is the source for a trace>",
  "redaction": null | { ... }
}
```

`redaction` carries the §7.4 record when this artefact has been redacted or withheld; §10.4 states what it means for a raw artefact specifically. `produces_trace_id` links a raw artefact to the trace produced from it. Multiple raw artefacts MAY produce one trace (a JSONL log plus a config file, for example); a single raw artefact MAY contribute to multiple traces.

In an envelope that declares `0.1.1` or a later revision, a `produces_trace_id`, here or on a binding (§4.11), MUST equal the `trace_id` of exactly one trace listing (§4.6). In an envelope of any revision, a consumer that finds a `produces_trace_id` equal to no trace listing's `trace_id`, or to the `trace_id` of two or more, notes it and applies the raw artefact or binding to no trace, and not to the bundle as a whole; it does not fail §6.2 step 3 for it, since that step checks the file's bytes.

### 4.8 Witness listing

See §7 for witness types and shape.

### 4.9 Annotation listing

Envelope-level annotations attach to the envelope itself or to specific artefacts within it. They do not duplicate trace-level annotations (which live in the trace); envelope annotations describe assembly notes, chain-of-custody context, evidence-grade assertions about the bundle as a whole, or anything else not properly inside any single trace.

```json
{
  "id": "<id>",
  "scope": {
    "type": "envelope" | "trace" | "raw_artefact" | "witness",
    "target_id": "<id>"
  },
  "annotator": "<principal>",
  "authored_at": "<ISO 8601>",
  "content": { "category": "...", "title": "...", "detail": "..." }
}
```

### 4.10 Extensions

`extensions` is an object whose keys are vendor-scoped (`ext:<vendor>:<name>`) or experimental (`x-<name>`) and whose values are arbitrary JSON. Consumers MUST preserve unknown extensions on re-serialisation.

### 4.11 Binding listing

`bindings` is an OPTIONAL array listing **binding sidecars**: checksummed companion files that bind external identity or input-provenance to the bundle without being traces, witnesses, or raw artefacts. A binding sidecar is a separate file whose hash this envelope manifest records in `bindings[]`; recording the hash here is what makes the binding tamper-evident under the envelope's integrity model, mirroring the sidecar pattern of an adapter's `adapter.json`. Where the same sidecar is written and sealed at production time by the producing apparatus, that seal is into the apparatus's own integrity manifest — a manifest distinct from this one. `bindings[]` is the envelope-level listing of such sidecars, not a duplicate or competing seal step; an envelope is not required to be the production-time seal locus. Each entry:

```json
{
  "kind": "scenario-binding" | "adapter-identity" | "x-<custom>",
  "path": "<file path within the envelope, e.g. run-binding.json>",
  "content_hash": "sha256:...",
  "produces_trace_id": "<trace id this binding pertains to, if any>"
}
```

- `kind` is the binding's type. Two kinds are defined; the `x-<custom>` escape admits others without an envelope-version bump.
- `path` is the sidecar file's location within the envelope. `content_hash` is the SHA-256 over its literal bytes; recording this hash in the manifest is what makes the binding tamper-evident under the envelope's integrity model.
- `produces_trace_id` optionally scopes a binding to one trace (a bundle with several traces from different runs may carry one binding per trace). Absent means the binding applies to the bundle as a whole (§4.7 states what a consumer does with one that names no single listed trace).

The manifest **lists and checksums** binding sidecars; it does not define their internal content. Each `kind` names the document that owns the sidecar's field shape:

| `kind` | Sidecar content owned by | Binds |
|---|---|---|
| `scenario-binding` | `minitrace-scenario-spec` §11 | the `(model, harness, scenario)` triple: scenario id/version/content-hash, model id+digest, harness name+version, run-spec hash |
| `adapter-identity` | the producing apparatus (not specified in this release) | the adapter that produced a trace: adapter binary hash, version, input/output schema version |

This keeps the boundary clean: the envelope spec owns the **manifest listing** (presence, path, checksum, trace scope); the **record formats** live where their producers are specified. A consumer verifies a binding by recomputing `content_hash` over the named file and then interpreting the file per the document or apparatus that owns its `kind`. A gate MAY assert that a required `kind` (e.g. `scenario-binding`) is present and well-formed as an admissibility precondition; defining that predicate is a gate/apparatus concern, not this spec's.

Unknown `kind` values (including unrecognised `x-<custom>`) MUST be preserved on re-serialisation and MUST NOT be treated as a verification failure; a consumer that does not understand a binding kind ignores it.

---

## 5. Producer Attestation

### 5.1 Identity

`producer` (§4.5) names the entity that assembled the envelope. The identity is opaque to the envelope format; its meaning is established outside the envelope (organisation directory, key registry, deployment-specific identity service).

### 5.2 Trust model

A consumer reading an envelope inherits the trust assumptions of the envelope's producer. The envelope provides integrity over the bundle's contents (every artefact hashed in the manifest) and, if signed (§9), authenticity of the producer's claim that this is the bundle they assembled. It does NOT provide:

- Independent witness against a fabricating producer. A producer who fabricates a trace and bundles it in an envelope leaves no in-envelope evidence of fabrication.
- Authentication of the operators or agents recorded inside the bundled traces. Those identities are governed by `minitrace-spec` §5 and §7.1.
- Verification that the witnesses in §7 actually correspond to the session described in the trace; a producer is trusted to bundle witnesses honestly.

Deployments needing stronger guarantees place the producer inside their trust boundary, or chain envelopes via signatures from independent attestors, or rely on out-of-band evidence.

### 5.3 Multi-attestor envelopes

An envelope with multiple independent attestors (e.g. a producer signature plus a separate auditor counter-signature) is supported by carrying multiple signature blocks under §9. v0.1.0 specifies the shape of one signature block; bundling several is mechanical.

---

## 6. Trace Bundling and Integrity

### 6.1 At least one trace

Every envelope MUST list at least one trace in `traces[]`. An envelope with zero traces is rejected at load time.

### 6.2 Hash recomputation

Consumers loading an envelope SHOULD:

1. Verify each trace listing's `file_content_hash` (§4.6) against the literal bytes of the corresponding trace file.
2. Parse each trace, recompute the canonical-JSON hash of its content (per `minitrace-spec` §3.4), and verify that the trace listing's `trace_content_hash` (§4.6) equals it and that the trace's internal `content_hash` equals it too. A trace without `content_hash` fails this step (§4.6), except a v0.2.x trace bundled in an envelope that declares `0.1.0`, which has only its listing hash checked (§12): v0.2.x defines no `content_hash`, so a `content_hash` member such a trace carries is not checked, and a consumer notes its presence. A trace that is not v0.3.x fails this step, unless it is a v0.2.x trace bundled in an envelope that declares `0.1.0` (§12).
3. Verify every other byte-hash the manifest lists against the literal bytes of the file it names: each raw artefact's `content_hash` (§4.7), each binding sidecar's `content_hash` (§4.11), and each entry of every witness's `path_hashes` (§7.1). In an envelope declaring `0.1.1` or later, a witness not removed as a whole also fails this step if its `path_hashes` is absent, lacks a key for a path the record lists, or has a key naming a path the record does not list (§7.1). A raw artefact or witness removed as a whole (§7.4, §10.4) lists no file, so there is nothing to verify for it, and a consumer reports it as removed, not as verified.
4. Verify each witness's own `content_hash` (§7.1) against the canonical form of that witness record.
5. If a signature is present (§9), verify the signature over the manifest.

A consumer that skips any of these MUST treat the envelope as informational only, not as evidence. A check in steps 1 to 4 that fails is treated at least as strictly as one skipped: the consumer MUST NOT report the trace, file or witness it concerns as verified, and MUST treat the envelope as informational only, not as evidence. A failed `path_hashes` entry in step 3 concerns the witness that lists the file as well as the file: that witness MUST NOT be reported as verified, whatever step 4 finds for its record, so a failed payload file is treated no more leniently than the omitted `path_hashes` of the next paragraph. Whether the consumer also rejects the envelope is for its evidence policy to decide. A signature that fails in step 5 is treated at least as strictly as a skipped step 5, whether or not the consumer's evidence policy requires it: the consumer MUST treat the envelope as informational only, not as evidence, and §9.5 states when it MUST also reject it.

Step 3 covers every listed file uniformly, on the principle that a hash recorded in the manifest and never recomputed establishes nothing. It is stated as one step rather than three because the operation is identical in each case and the set of listings is expected to grow. In an envelope declaring `0.1.0`, a witness that omits `path_hashes` (permitted there, §7.1) and is not removed as a whole leaves its payload files unverifiable: a consumer MUST NOT report such a witness as verified, and MUST treat the envelope as informational only where that witness is load-bearing for its evidence policy. In an envelope declaring `0.1.1` or later, whose producer MUST emit `path_hashes` (§7.1), such a witness fails step 3 instead: it is not reported as verified, and the envelope is informational only whether or not the witness is load-bearing, as for any failed check.

### 6.3 Multiple traces and lineage

When an envelope contains multiple traces, the cross-trace relationships are expressed through `lineage` edges in the traces themselves (per `minitrace-spec` §7). The envelope manifest does not duplicate the lineage graph; it lists the traces, and consumers traverse `lineage[]` edges within them.

When a lineage edge inside a trace requires `target_content_hash` (per `minitrace-spec` §7.1), and the target trace is bundled in the same envelope, the manifest's `trace_content_hash` for the target trace and the lineage edge's `target_content_hash` MUST match when the producer assembles the envelope, unless the bundled target is a later state of the one the edge refers to, reached by one or more redactions or additions `minitrace-spec` §3.1 permits, as when an assembled envelope (§1.4) collects a target already redacted. The requirement binds the producer not to bundle any other trace, or any other state of the target, in its place. The two can also stop matching after assembly without any check of §6.2 failing: when the target is redacted or extended by an addition `minitrace-spec` §3.1 permits and the manifest is updated for it (§9.3), the edge keeps referring to the earlier state of the target (`minitrace-spec` §7.1, §15.3), and `minitrace-spec` §3.1 gives no party re-serialising the trace licence to rewrite it. A consumer that finds the two differing reports the edge as referring to a state of the target other than the one bundled, and MUST NOT report that edge as verified against the bundled target. Such a difference is not a failed check under §6.2: steps 1 to 5 check each listed file against the manifest and, where a signature is present, the manifest against its signer, and an edge referring to an earlier state of its target is consistent with both; a consumer cannot tell a permitted change to the target, made before or after assembly, from a producer that did not meet this requirement, so the exception cannot be tested. In an envelope without a verified signature the difference may be the only sign that the target changed after the edge was written (§14.1); what weight it carries is for the consumer's evidence policy.

### 6.4 Cross-envelope references

A lineage edge in a trace MAY reference a trace not bundled in this envelope. The reference carries `target_trace_id` and `target_content_hash`; resolving the reference requires locating the target envelope through deployment-specific means (registry, content-addressed storage, prior-art context). The envelope format does not specify resolution.

---

## 7. Witnesses

### 7.1 Witness model

A witness is a typed observational artefact bundled alongside traces. It records something about the session that did not fit in the trace's structured shape.

```json
{
  "id": "<id>",
  "kind": "<witness kind>",
  "scope": {
    "type": "envelope" | "trace" | "turn" | "tool_call" | "policy_trigger" | "segment",
    "target_id": "<event id, when scope is below trace level>",
    "target_trace_id": "<trace id, if not envelope-scoped>"
  },
  "captured_at": "<ISO 8601>",
  "captured_by": "<capture mechanism>",
  "content_hash": "sha256:...",
  "size_bytes": <int>,
  "primary_path": "witnesses/<witness-id>/<file>",
  "auxiliary_paths": ["witnesses/<witness-id>/<other-file>", ...],
  "path_hashes": { "witnesses/<witness-id>/<file>": "sha256:...", ... },
  "metadata": { ... },
  "redaction": null | { ... }
}
```

`primary_path` names the main artefact for this witness. `auxiliary_paths` covers cases where one witness has multiple files (a packet capture plus its key log; a screenshot plus the OCR transcript).

`path_hashes` maps every path this record lists, `primary_path` and each entry of `auxiliary_paths`, to the SHA-256 over that file's literal bytes as stored in the envelope. Producers of an envelope that declares `envelope_version` `0.1.1` or a later revision MUST emit it, every listed path MUST appear as a key, and a key MUST NOT name a path the record does not list; an envelope that declares `0.1.0` MAY omit it, as v0.1.0 has no such field (§12). This is what places witness payload files inside the integrity chain: §6.2 requires consumers to recompute it. The field is OPTIONAL in the schema, which does not condition it on the declared `envelope_version`; a witness without it has unverifiable payload files in an envelope declaring `0.1.0` and fails §6.2 step 3 in one declaring `0.1.1` or later, and §6.2 states what a consumer must do with one. **Stated plainly, because the two halves diverge: the producer MUST above is not enforced by the normative schema.** A producer that declares `0.1.1` or later and emits no `path_hashes` violates this section while validating clean against `minitrace-envelope-v0.1.1.schema.json`, and the key-set correspondence is not expressible in JSON Schema in any case (§6.2 step 3 is where it is checked). Because the MUST is scoped by declared version, presence could be required conditionally on `envelope_version` without touching v0.1.0 envelopes; the v0.1.1 schema does not require it, and the gap is a known property of this revision. When the witness is removed as a whole (`redaction.operation` `remove`, `target_kind` `witness` or absent), the payload files are absent and unlisted (§7.4), so `path_hashes` is correspondingly empty.

The witness's own `content_hash` covers the witness record, NOT the bytes of its files. It is the SHA-256 over the canonical-JSON form of the record **excluding the `content_hash` field itself**, canonicalised per RFC 8785 (JCS). No other transformation is applied before hashing: in particular, every timestamp in the record (`captured_at`, `redaction.authorised_at` and any inside `metadata`) is hashed as stored. The serialisation, the removal of the hash field and the absence of any other transformation are those `minitrace-spec` applies to a trace's `content_hash` (§3.4), stated here rather than incorporated by reference because the two hashes cover different objects. Excluding the field from its own input is what keeps the definition non-circular; naming the canonicalisation is what lets an independently-implemented verifier reproduce the bytes.

That holds only for a record whose canonical form every verifier can reproduce, so in an envelope that declares `0.1.1` or a later revision a witness record, its `metadata` included, MUST meet the three rules `minitrace-spec` §3.4 states for a trace. A number whose IEEE 754 binary64 value lies outside the inclusive range [−(2^53−1), 2^53−1] is carried as a JSON string, whatever its notation (Numeric domain). The record, taken on its own, is an I-JSON message (RFC 7493): among other things, no object in it has two members with the same name, and no string, member name included, contains an unpaired surrogate or a noncharacter (JSON form). Its depth, counted as `minitrace-spec` §3.4 counts it, is at most 64, and a verifier MUST NOT reject a record for its depth when that depth is at most 64 (Nesting depth). A record outside these rules has no canonical form that every verifier can reproduce: in an envelope that declares `0.1.1` or later its `content_hash` fails §6.2 step 4; in one that declares `0.1.0`, which states none of these rules, a verifier reports that hash as unverifiable, which §6.2 treats as a skipped check, not as a failed one. Whatever revision the envelope declares, a record with two members of the same name, or a number whose nearest binary64 value is infinite, is instead refused with its manifest (§3.3), so §6.2 is not reached for it.

`size_bytes` describes the primary payload file, not the record; a witness removed as a whole has neither (§7.4).

### 7.2 Baseline witness kinds

v0.1.0 defines the following kinds:

| Kind | Purpose | Typical primary payload |
|---|---|---|
| `network_capture` | Packet capture or flow log of session network activity | `.pcap` or `.pcapng` |
| `screenshot` | Visual capture of UI state at a moment in the session | `.png`, `.webp` |
| `screen_recording` | Continuous visual capture of UI over a span | `.mp4`, `.mkv` |
| `environment_snapshot` | Captured state of environment variables, mount points, process tree | `.json` or `.tar` |
| `process_tree` | Process-tree dump (timestamped) | `.json` |
| `filesystem_snapshot` | File-system state at a moment (typically pre/post session) | `.tar` or `.tar.zst` |
| `harness_artefact` | Output that is harness-specific but does not fit the raw_artefacts category | varies |
| `inference_capture` | Model-internal data captured during inference (activations, attention, probe outputs) | varies; typically tensor formats |
| `external_log` | Log from an external system referenced by the session (e.g. firewall logs, server logs) | `.log`, `.json` |
| `binary_capture` | Captured executable, library, or other binary referenced or executed during the session | raw bytes |

Kinds outside this list use the `x-<name>` or `ext:<vendor>:<name>` namespaces. A revision that changes `Y` MAY promote widely-observed `x-*` kinds to baseline (§4.2, §7.6.12).

### 7.3 Witness scope

A witness MAY scope to:

- The envelope as a whole (`scope.type: "envelope"`).
- A specific trace (`scope.type: "trace"`, `scope.target_trace_id`).
- A specific event within a trace (`scope.type` in `turn` / `tool_call` / `policy_trigger` / `segment`, with `scope.target_id` naming the event identifier per `minitrace-spec` §4.2 and `scope.target_trace_id` naming the trace).

Sub-trace scopes require the target trace to be bundled in this envelope: `scope.target_trace_id` equals the `trace_id` of exactly one trace listing (§4.6), and `scope.target_id` names exactly one event of the scope's type in that trace by its `minitrace-spec` §4.2 identifier, a turn only as `turn-<index>`; §4.2 addressing applies to a bundled v0.2.x trace as well, which has no segment markers or policy triggers. In an envelope of any revision, as v0.1.0 §7.3 required bundling and named the event by `minitrace-spec` §4.2, a consumer resolves a sub-trace scope against the bundled trace even when that trace failed §6.2 step 2, while a trace whose file is absent or cannot be parsed resolves no event; a sub-trace scope that does not resolve is a failed check, and the consumer MUST NOT report the witness as verified and MUST treat the envelope as informational only, not as evidence, as for a failed check of §6.2. A `trace` scope whose `target_trace_id` equals no trace listing's `trace_id`, or that of two or more, is noted, and the consumer applies the witness to no bundled trace; it is not a failed check.

### 7.4 Witness redaction

Witnesses MAY be redacted. A redacted witness has its `redaction` field populated:

```json
{
  "operation": "remove" | "pseudonymise" | "truncate" | "mask" | "generalise",
  "target_kind": "witness" | "payload_keys",
  "targets": ["<key name or dotted key path>", ...],
  "reason_code": "<reason code per minitrace-spec §8.3>",
  "authorised_by": "<principal>",
  "authorised_at": "<ISO 8601>",
  "original_content_hash": "sha256:..." | null,
  "original_path_hashes": { "<listed path>": "sha256:...", ... }
}
```

When `operation: remove` applies to the witness as a whole (`target_kind` `witness` or absent), the witness record remains in the manifest but the payload files are absent and not listed: the record carries no `primary_path`, `auxiliary_paths` or `size_bytes`, and its `path_hashes`, where present, is empty (§7.1). The witness record itself is integrity-relevant content per the same logic as `minitrace-spec` §8.1: redaction logs are not themselves redactable.

`target_kind` names what the operation applied to. `witness` means the record and its payload files as wholes, which is the only behaviour v0.1.0 could express and remains the default: a record that omits `target_kind` is read as `witness`, which is what keeps this addition additive (§12). `payload_keys` means the operation applied to the keys named in `targets[]`, within every record of every payload file this record lists. `targets[]` is REQUIRED when `target_kind` is `payload_keys`, and MUST NOT be present otherwise.

A key set rather than a pointer, because a pointer does not address this case. `minitrace-spec` §8.1 identifies a redacted field with an RFC 6901 pointer, which works because a trace is one JSON document with one root. A witness payload is NDJSON, and a raw harness log (§10.4) is typically JSONL: many documents in one file, so no pointer addresses "the `user.email` attribute on every record here". The declaration is therefore a key set, and it is the same key set the producer already applies at capture.

Hashes describe the bytes as distributed. `content_hash` and `path_hashes` (§7.1), and a raw artefact's `content_hash` (§4.7), always cover the artefact as it exists in this envelope, redacted or not. A consumer therefore verifies a redacted envelope under §6.2 with no special case and needs no knowledge of the redaction to check integrity: a file that is present is checked as it stands, and an entry removed as a whole lists none. The pre-redaction values are recorded separately: `original_content_hash` for the record, and `original_path_hashes` for the payload files, keyed exactly as `path_hashes` is. Both are OPTIONAL. Where present, they let a holder of the unredacted artefact demonstrate that the distributed one derives from it; where absent, the redaction is visible but its input is not attestable. Where the withheld content is guessable (a short value, one of few possibilities) and the rest of the original can be rebuilt from what is distributed, these hashes let anyone holding a candidate confirm it; a producer that must keep such content confidential SHOULD omit them.

### 7.5 Inference capture

`inference_capture` is a placeholder kind for model-internal data: per-layer activations, attention scores, probe outputs, transformer-lens captures, similar. v0.1.0 does NOT specify a format for the captured data — implementations carry tensor data in whatever native format the capture tool emits, with `metadata` describing layout, shape, and provenance. A future revision MAY introduce a typed sub-shape once the most common capture pipelines stabilise.

### 7.6 Provisional `x-*` witness kinds for kernel-side observation

v0.1.1 enumerates ten provisional `x-*` witness kinds for kernel-side, userspace-hook, and in-path control observation:

- `x-process-exec-trace` — process lifecycle (exec, exit, fork).
- `x-file-access-trace` — file-system operations attributed to a process.
- `x-network-flow-trace` — connection-level network records identifying outbound connections by peer address and port (full 5-tuple available via optional extensions).
- `x-syscall-trace` — per-syscall events with optional argument and return-value capture.
- `x-process-hook-trace` — userspace function-call interception (Frida, LD_PRELOAD).
- `x-tls-keylog-trace` — pre-encryption TLS payload capture via libssl uprobe.
- `x-lsm-event-trace` — security-boundary events observed at the LSM dispatch points (process exec, capability checks, BPF loads, inode unlinks, socket connects), discriminated by `lsm_event`.
- `x-resource-event-trace` — observation-only performance-attribution events from the kernel memory-management paths (OOM kills, PSI memory-stall episodes), discriminated by `resource_event`.
- `x-dns-trace` — DNS-resolution records from an in-path policy resolver (one observed query plus the policy `decision` and action taken). The first **in-path control** kind: the producer is a resolver in the data path that acts on each query (forward / sink), not a passive observer.
- `x-http-catch` — HTTP(S) requests reaching an in-path sink-catcher (one request plus the policy `decision`, the captured request, and — for TLS connections — an optional `tls` block). The second **in-path control** kind, the HTTP-layer complement to `x-dns-trace`: it records what the agent tried to send over HTTP and what the platform did with it.

The shapes specified below are normative within the v0.1.x line: modules emitting any of these kinds MUST conform to the minimum-guaranteed fields for that kind, and consumers MAY rely on those fields being present. Optional extensions are non-normative; consumers MUST treat their absence as "this capture mode did not record it", not "this did not happen".

This subsection is provisional: kinds remain under the `x-` namespace until they meet the §7.6.12 promotion criteria. Promotion moves a kind to §7.2 baseline.

#### 7.6.1 Cross-cutting requirements

The following requirements apply to every `x-*` kind in this subsection. They are stated once here rather than repeated per kind.

**Event payload.** Each `x-*` witness's `primary_path` typically references an NDJSON payload (one JSON object per line), one event per record. The witness record's `metadata` block carries kind-level declarations; per-event data lives in the payload.

**`tool_call_id` correlation.** Every event in any `x-*` kind below MUST include a `tool_call_id` key referencing a trace-level tool-call identifier per `minitrace-spec` §4.2. The value MAY be `null` when the emitting module has no correlation entry for the event; the key MUST NOT be absent. Consumers joining witness records to trace events SHOULD use `tool_call_id` when its value is non-null; `(timestamp, pid)` joins are brittle under tool-pool concurrency and SHOULD be treated as best-effort fallback only.

**`tool_call_id_attribution` declaration.** Each witness record MUST declare its module's correlation discipline in the record's `metadata` block:

```json
"metadata": {
  "tool_call_id_attribution": "always" | "when_known" | "never",
  ...
}
```

The three values have distinct consumer semantics:

- **`always`** — the module tags every event it emits. A null `tool_call_id` on a record of this kind is a data anomaly.
- **`when_known`** — the module tags events when it has a correlation map entry available. A null `tool_call_id` is expected when the module lacked an entry.
- **`never`** — the module does not perform correlation. Consumers MUST NOT attempt `tool_call_id` joins on events from this module.

This declaration is mandatory because a null `tool_call_id` value alone does not let a consumer distinguish "tagged but unmatched" from "module does not tag". Consumers MUST honour the declared discipline.

**Capture status.** A producer declares, per `x-*` kind, whether its capture ran, as `capture_status` with the closed v0.1.x value set `"captured" | "unsupported_platform" | "attach_failed"` (`captured` = the capture path ran on a supported platform, so an empty payload is a genuine quiet run; `unsupported_platform` = the module has no capture implementation for this platform, so nothing was or could have been observed; `attach_failed` = a capture path exists but did not attach, so nothing was observed). A witness record of an `x-*` kind asserts that its capture ran. An assembler MUST NOT emit a witness record of an `x-*` kind for a capture whose declared status is present and anything other than `captured`, including a value outside the set or a non-string; the payload MAY still be carried as an untyped artefact, which asserts nothing about the kind. An absent declaration is read as `captured`, so producers that predate the declaration keep their records. `event_count: 0` alone does not distinguish a quiet run from a capture that never ran; the declaration does. Where carried in the envelope, the declaration is the witness record's `metadata.capture_status`; a conformant manifest therefore holds only `captured` there.

**`comm` truncation.** Where a kind below specifies a `comm` field, the field MUST carry at most 16 characters (Linux `TASK_COMM_LEN`). Modules on non-Linux kernels MUST truncate longer process basenames to 16 characters for the minimum-guaranteed `comm` field and MAY carry the full basename in an optional `comm_full` extension.

**Timestamps.** Per-event `timestamp` fields MUST be ISO 8601 strings. Resolution SHOULD be nanosecond when the module's clock source supports it (typically kernel monotonic) and MAY be coarser (microsecond, millisecond) when the source is wall-clock or coarser. The witness record's outer `captured_at` per §7.1 is independent of per-event timestamps and reflects the module's wall-clock at write time. Modules SHOULD declare the per-event clock source in `metadata.clock_source`; `minitrace-spec` defines no vocabulary for it.

#### 7.6.2 `x-process-exec-trace`

Process lifecycle events: exec, exit, fork. One event per kernel transition.

**Minimum guaranteed fields (all events):**

```json
{
  "timestamp": "<ISO 8601>",
  "event_type": "exec" | "exit" | "fork",
  "pid": <int>,
  "tool_call_id": <string or null>
}
```

**Additional minimum guaranteed fields per `event_type`:**

- `exec`: `parent_pid` (int), `comm` (string, ≤ 16 chars), `filename` (string).
- `exit`: `exit_code` (int; -1 when killed by signal), `exit_signal` (int or null).
- `fork`: `parent_pid` (int), `child_pid` (int).

**Optional extensions:**

- `argv` (array of strings) — present when the module captures argument vectors; modules MAY apply a length bound and SHOULD document it in `metadata`.
- `parent_comm` (string) — on `fork` events.
- `cgroup_path` (string).
- `uid` / `gid` (int).
- `executable_hash` (string) — content hash of the binary at the time of `exec`.

#### 7.6.3 `x-file-access-trace`

File-system operations attributed to a process.

**Minimum guaranteed fields:**

```json
{
  "timestamp": "<ISO 8601>",
  "pid": <int>,
  "comm": "<string, ≤ 16 chars>",
  "resolved_path": "<string>",
  "mode": "r" | "w" | "rw" | "x",
  "tool_call_id": <string or null>
}
```

`mode` is a four-value summary covering the common open intents. Modules MAY emit richer encoding under the `open_flags` extension; consumers reading the minimum-guaranteed shape see `mode` only.

`resolved_path` is the kernel's canonical view of the path at capture time. Modules using `bpf_d_path()` or equivalent SHOULD note in `metadata` that bind mounts, overlay filesystems, and namespaced paths may yield paths requiring downstream normalisation.

**Optional extensions:**

- `decision`: `"allow"` | `"deny"` — MUST NOT be emitted unless the capture mechanism observes an enforcement decision (e.g. LSM hook with policy attached). Observation-only modes therefore omit this field.
- `open_flags` — full `O_*` flag encoding as integer bitmask or array of flag names.
- `inode` (int), `device` (string).
- `umask` (int).
- `parent_pid` (int).

#### 7.6.4 `x-network-flow-trace`

Connection-level network records identifying outbound connections by peer address and port, with optional local-side and protocol-level extensions. This kind is connection-level metadata; the `network_capture` baseline kind (§7.2) is the packet-level equivalent. A producer MAY emit both kinds for the same session.

Modules that hook `tcp_connect` or equivalent and have access to the full 5-tuple (local address, local port, peer address, peer port, protocol) SHOULD emit the additional fields under the optional extensions below; the minimum-guaranteed shape carries only the peer-side identification.

**Minimum guaranteed fields:**

```json
{
  "timestamp": "<ISO 8601>",
  "pid": <int>,
  "comm": "<string, ≤ 16 chars>",
  "family": "ipv4" | "ipv6",
  "peer_addr": "<string>",
  "peer_port": <int>,
  "tool_call_id": <string or null>
}
```

**Optional extensions:**

- `local_addr` (string) — local source address. Modules with access to the bound local address SHOULD emit this alongside `sport` to provide the full local-side identification.
- `sport` (int) — local source port.
- `protocol` (string) — `tcp`, `udp`, `icmp`, etc. Modules SHOULD emit when known.
- `state_transitions` — array of `{state: <string>, at: <ISO 8601>}` records. Modules hooking `tcp_set_state` or equivalent populate; others MUST NOT emit.
- `sni` (string) — extracted from TLS ClientHello when visible.
- `bytes_sent` / `bytes_received` (int) — totals when the module aggregates.

#### 7.6.5 `x-syscall-trace`

Per-syscall events with optional argument and return-value capture. Filter-map gating expected; producers SHOULD document the captured-syscall subset in `metadata`.

**Minimum guaranteed fields:**

```json
{
  "timestamp": "<ISO 8601>",
  "pid": <int>,
  "comm": "<string, ≤ 16 chars>",
  "syscall_name": "<string>",
  "syscall_number": <int>,
  "tool_call_id": <string or null>
}
```

Both `syscall_name` and `syscall_number` are mandatory: `syscall_name` is portable across architectures; `syscall_number` is required for kernel-level joins (syscall numbers are architecture-dependent).

**Optional extensions:**

- `args` — array of type-tagged arguments. Modules emitting `args` MUST declare in `metadata` which syscalls have argument capture enabled.
- `return_value` (int or structured).
- `duration_ns` (int) — when the module hooks both `sys_enter` and `sys_exit`.
- `category` (string) — module-defined classification (`file`, `network`, `process`, `privilege`, etc.).

#### 7.6.6 `x-process-hook-trace`

Userspace function-call interception. Frida and LD_PRELOAD-style hooks are the canonical capture mechanisms.

**Minimum guaranteed fields:**

```json
{
  "timestamp": "<ISO 8601>",
  "pid": <int>,
  "process_image": "<string>",
  "hook_name": "<string>",
  "hook_target": "<string>",
  "tool_call_id": <string or null>
}
```

`hook_name` is the agent-script identifier and SHOULD be versioned with the script; `hook_target` names the function or method intercepted.

This kind specifies `process_image` (full path to the executable) in place of the `comm` field used by the kernel-mode kinds above. `process_image` provides the resolution useful for userspace hook attribution (typically a specific interpreter binary path or a target executable); modules MAY additionally emit `comm` as an optional extension when known.

**Optional extensions:**

- `args_serialised` (string or structured).
- `return_value_serialised` (string or structured).
- `script_hash` (string) — content hash of the hook script.
- `redaction_class`: `"green"` | `"amber"` | `"red"` per the producer's egress classification.

**Egress requirement.** Records under this kind may capture data requiring redaction (TLS keys, pre-redaction user prompts, captured argument values). Modules MUST classify per-hook in `metadata` and apply the §7.4 redaction discipline; consumers MUST honour `redaction_class` when present on a record.

#### 7.6.7 `x-tls-keylog-trace`

Pre-encryption TLS payload capture (typically via uprobe on libssl `SSL_write` / `SSL_read` or LD_PRELOAD interposition on libssl symbols). Investigation-only; sensitive by construction.

**Minimum guaranteed fields:**

```json
{
  "timestamp": "<ISO 8601>",
  "pid": <int>,
  "comm": "<string, ≤ 16 chars>",
  "direction": "send" | "recv",
  "payload_hash": "sha256:...",
  "redaction_class": "amber" | "red",
  "tool_call_id": <string or null>
}
```

`payload_hash` is mandatory because the raw payload MAY be redacted per §7.4; the hash preserves integrity discipline independent of payload presence.

`redaction_class` is mandatory for this kind (unlike its optional appearance under `x-process-hook-trace`). Only `"amber"` and `"red"` are valid; `"green"` MUST be rejected because pre-encryption TLS payload is never publishable-by-default. A module unable to classify a record MUST fail closed (refuse to emit the record).

**Consumer validation.** Consumers MUST validate that every `x-tls-keylog-trace` record carries `redaction_class` set to `"amber"` or `"red"`. A record missing `redaction_class`, or carrying `"green"`, MUST be rejected by the consumer (treated as a malformed record), not silently ignored or processed as unclassified. This makes the module-side fail-closed requirement observable at the envelope level: a non-conforming module's output cannot pass an envelope-level consumer check.

**Optional extensions:**

- `payload` (base64 or hex string) — raw bytes; emission subject to the `redaction_class` discipline above.
- `tls_version` (string).
- `peer_sni` (string) — used for joining to `x-network-flow-trace` records.
- `connection_id` (string) — module-attached identifier for grouping send/recv pairs.

**Egress requirement.** Raw `payload` is sensitive by construction. Modules SHOULD emit raw `payload` only when the operator has explicitly enabled raw capture for the session; emission MUST honour the record's `redaction_class`.

#### 7.6.8 `x-lsm-event-trace`

Security-boundary events observed at the kernel's LSM (Linux Security Module) dispatch points. A single witness kind carrying multiple event classes, discriminated by an `lsm_event` field. This kind observes where the kernel consults its security hooks; it complements `x-syscall-trace` (the raw-syscall stream) and overlaps the more specific `x-process-exec-trace` and `x-network-flow-trace` kinds at the security-decision layer rather than replacing them.

**Discriminator field.** This kind uses `lsm_event` as its event-class discriminator, **not** the `event_type` field used by `x-process-exec-trace` (§7.6.2). The divergence is deliberate and normative: `x-process-exec-trace`'s classes share a uniform lifecycle shape, whereas this kind's classes observe structurally different hooks with disjoint per-class payloads. Consumers MUST switch on `lsm_event` to determine which class-specific fields are present. The closed value set for v0.1.x is `"exec" | "capability" | "bpf_load" | "unlink" | "connect"`.

**Minimum guaranteed fields (all events):**

```json
{
  "timestamp": "<ISO 8601>",
  "pid": <int>,
  "comm": "<string, ≤ 16 chars>",
  "lsm_event": "exec" | "capability" | "bpf_load" | "unlink" | "connect",
  "tool_call_id": <string or null>
}
```

**Additional minimum guaranteed fields per `lsm_event`:**

- `exec`: `filename` (string) — the executable path observed at the LSM bprm check.
- `capability`: `capability` (string) — the POSIX capability name being checked (e.g. `CAP_NET_ADMIN`); `audit` (bool) — whether the check requested auditing (distinguishes an audited capability use from a silent probe).
- `bpf_load`: `prog_type` (string) — the `enum bpf_prog_type` name; `prog_name` (string) — the program name, which MAY be empty for an unnamed program.
- `unlink`: `path` (string) — the deleted file's **basename only**, not a full path. The inode-unlink dispatch point carries no path structure, so full-path resolution is not available at this hook; consumers MUST treat this field as a basename.
- `connect`: `family` (`"ipv4"` | `"ipv6"`); `peer_addr` (string); `peer_port` (int, host byte order).

**Optional extensions:** none defined in v0.1.x. Consumers MUST treat the absence of any field beyond the per-class minimum as "this capture mode did not record it".

**Decision field.** This kind MUST NOT emit the `decision` field defined for `x-file-access-trace` (§7.6.3) unless the capture mechanism observes an enforcement decision. Observation-only LSM-dispatch capture (read-only `fentry`, not an enforcing BPF-LSM attachment) does not observe a verdict and therefore omits `decision`.

#### 7.6.9 `x-resource-event-trace`

Resource-exhaustion and resource-contention events observed at the kernel's memory-management paths, with process attribution (`pid`, `comm`). A single witness kind carrying multiple event classes, discriminated by a `resource_event` field. This kind captures performance-attribution signals — why a process was slow, stalled, or killed — that are otherwise invisible in the trace: an OOM-killed model process surfaces only as "the framework crashed", and a process slowed under memory pressure is indistinguishable from one that is simply slow. It complements the security-decision view of `x-lsm-event-trace` (§7.6.8) and the raw-syscall stream of `x-syscall-trace` (§7.6.5) at the resource layer rather than replacing them.

**Observation-only.** This kind is produced by read-only probes on the kernel resource paths (`fentry`-where-attachable / `kprobe`-fallback, `BPF_PROG_TYPE_TRACING`). The module observes resource events and takes **no control action**: it does not kill, throttle, or set limits. No record under this kind carries an enforcement or control verdict.

**Discriminator field.** This kind uses `resource_event` as its event-class discriminator, **not** the `event_type` field used by `x-process-exec-trace` (§7.6.2), consistent with `x-lsm-event-trace`'s `lsm_event` (§7.6.8). The divergence is deliberate and normative: this kind's classes observe structurally different kernel paths with disjoint per-class payloads — an OOM victim-attribution record versus a memory-stall duration record — so the divergent field name signals the divergent contract rather than implying interchangeability. Consumers MUST switch on `resource_event` to determine which class-specific fields are present. The closed value set for v0.1.x is `"oom_kill" | "memory_pressure"`.

**Minimum guaranteed fields (all events):**

```json
{
  "timestamp": "<ISO 8601>",
  "pid": <int>,
  "comm": "<string, ≤ 16 chars>",
  "resource_event": "oom_kill" | "memory_pressure",
  "tool_call_id": <string or null>
}
```

**Actor versus subject (`oom_kill`).** On an `oom_kill` record the top-level `pid` / `comm` is the *allocating context* that tripped the OOM killer — **not** the task that was killed. The killed task (the OOM killer's chosen victim, `oom_control->chosen`) is carried in `victim_pid` / `victim_comm`. Both are recorded so a consumer can distinguish who triggered the kill from who was killed; the two are frequently different processes (a memory-hungry allocator trips the kill, but the killer's heuristic may choose a different victim).

**Additional minimum guaranteed fields per `resource_event`:**

- `oom_kill`: `victim_pid` (int) — the PID of the task the OOM killer chose (`oom_control->chosen`), distinct from the record's top-level `pid`; `victim_comm` (string, ≤ 16 chars) — the chosen victim's `comm`.
- `memory_pressure`: `stall_ns` (int) — the duration in nanoseconds of one per-task memory-stall episode, measured `psi_memstall_leave − psi_memstall_enter`. The record's `timestamp` is anchored to the **episode end** (the `psi_memstall_leave` ktime), the point at which `stall_ns` becomes known; consumers MUST NOT read `timestamp` as the stall's start.

**Optional extensions:** none defined in v0.1.x. Consumers MUST treat the absence of any field beyond the per-class minimum as "this capture mode did not record it". CPU-throttling (cgroup CPU-bandwidth) and block-I/O-pressure classes are anticipated future additions to the `resource_event` value set; they are not specified in v0.1.x, and a consumer MUST treat any `resource_event` value outside the closed v0.1.x set as a record from a newer producer it does not understand.

**PSI availability (kind-level metadata).** The `memory_pressure` class requires a `CONFIG_PSI=y` kernel with PSI enabled. Where PSI is unavailable the producer degrades to OOM-only capture and MUST declare the degrade as a `psi_available` boolean in the witness record's `metadata` block (the kind-level declaration mechanism of §7.6.1). A consumer MUST read `psi_available` before interpreting an absence of `memory_pressure` events: `psi_available: false` means the class could not attach on this run, **not** that no memory stalls occurred. This is the disambiguation the §7.6 absence-semantics rule ("absence means this capture mode did not record it, not that this did not happen") requires for the PSI class.

#### 7.6.10 `x-dns-trace`

DNS-resolution records from an in-path policy resolver: one record per observed DNS query, carrying the queried name and type, the policy `decision`, and the action the resolver took. This kind observes the agent's DNS name-resolution and the egress-control decision applied to it. It is the DNS-name-visibility layer; the packet-level `network_capture` (§7.2) and the connection-level `x-network-flow-trace` (§7.6.4) see flows and addresses but not the resolved name, and a producer MAY emit all three for the same session.

**Control, not observation-only.** Unlike the observation-only kinds of this subsection, and like `x-http-catch` (§7.6.11), the producer of `x-dns-trace` is a resolver **in the data path** that *acts* on each query: an allow-listed name is forwarded to the real upstream, everything else is sinked to a fakeip (leak-prevention). The `decision` field therefore records an **enforcement action the producer took**, not a verdict it passively observed — distinguishing it from the observed `decision` of `x-file-access-trace` (§7.6.3) and the explicitly observation-only kinds (`x-resource-event-trace` §7.6.9, the read-only-`fentry` kinds). A consumer reading an `x-dns-trace` record learns both what the agent tried to resolve and what the platform did about it.

**Coverage boundary.** `x-dns-trace` records only queries that reached this resolver. An agent speaking DoH/DoT directly to a hardcoded resolver bypasses it; absence of a record is "this resolver did not see the query", not "no DNS egress occurred". This is the §7.6 absence-semantics rule applied to an in-path control kind.

**Minimum guaranteed fields:**

```json
{
  "timestamp": "<ISO 8601>",
  "qname": "<string>",
  "qtype": "<string>",
  "decision": "forward" | "sink" | "sink-oob",
  "tool_call_id": <string or null>
}
```

**`decision` value set.** The closed value set for v0.1.x is `"forward" | "sink" | "sink-oob"` (`forward` = relayed to the configured upstream and its answer returned; `sink` = answered with the sink fakeip; `sink-oob` = answered with the out-of-band catcher address). A consumer MUST treat any `decision` value outside this set as a record from a newer producer it does not understand; `"block"` (answer withheld / refused) and `"nxdomain"` are anticipated future additions and are not specified in v0.1.x.

**Optional extensions:**

- `client` (string) — source `ip:port` of the querying datagram.
- `matched_rule` (string) — the policy rule that produced the decision (e.g. an allow-list match label or a wildcard/catch-all label). Free-form provenance, not a closed set.
- `seq` (int) — producer-local monotonic per-query sequence number.
- `qclass` (string) — DNS query class when not `IN`.
- `upstream` (string) — the upstream resolver a `forward` was relayed to, when recorded.
- `answer` (string or array) — the answer returned (the synthesised sink address, or the upstream answer for a `forward`), when recorded.
- `latency_ms` (number) — resolution latency for a `forward`, when measured.

#### 7.6.11 `x-http-catch`

HTTP(S)-request records from an in-path sink-catcher: one record per HTTP request reaching the catcher, carrying the request line (method, host, path), the policy `decision`, the captured request (headers and body), and — for a TLS connection — an optional `tls` block. This kind is the HTTP-layer complement to `x-dns-trace` (§7.6.10): `x-dns-trace` records the name the agent tried to resolve and the egress-control decision on it; `x-http-catch` records the HTTP request the agent tried to send and the egress-control decision on it. A producer MAY emit both for the same session (a sinked name resolves to the catcher, whose HTTP request is then recorded here).

**Control, not observation-only.** Like `x-dns-trace`, the producer of `x-http-catch` is **in the data path** and *acts*: a request to an allow-listed host is forwarded, everything else is answered by the sink-catcher (the request is captured, not relayed). The `decision` field therefore records an **enforcement action the producer took**, not a passively-observed verdict. It shares the closed `decision` value set with `x-dns-trace`.

**Capture-anyway, flag-anomalous.** The catcher captures every request that reaches it, *including* one whose `Host` is allow-listed — because an allow-listed `Host` arriving at the sink catcher (rather than being forwarded upstream by the DNS layer) is itself the anomaly: a host-header spoof attempting to evade the sink. The optional `allowlisted_host_at_sink` flag marks this case. The don't-double-capture-the-legitimate-channel property is enforced upstream by DNS forwarding, not by an HTTP-layer `Host` gate.

**TLS depth.** A plaintext request carries `tls: null`. A TLS connection carries a `tls` object: `sni`, the ClientHello record byte count, the handshake stage observed, and whether the catcher MITM-**terminated** the connection. When `terminated` is true the captured `body` is the decrypted plaintext; when false the connection was observed passively (SNI + byte count) and no plaintext body is available. `tls != null` is the structural discriminator a consumer uses to tell an encrypted connection from a plaintext one.

**Coverage boundary.** `x-http-catch` records only requests that reached this catcher. An agent that connects directly to a hardcoded address, pins a certificate that rejects the MITM CA, or speaks a non-HTTP protocol on the port bypasses it; absence of a record is "this catcher did not see the request", not "no HTTP egress occurred". This is the §7.6 absence-semantics rule applied to an in-path control kind.

**Minimum guaranteed fields:**

```json
{
  "timestamp": "<ISO 8601>",
  "host": <string or null>,
  "method": <string or null>,
  "path": <string or null>,
  "decision": "forward" | "sink" | "sink-oob",
  "tls": null | { "sni": <string or null>, "record_bytes": <int>, "handshake": "<string>", "terminated": <bool> },
  "tool_call_id": <string or null>
}
```

`host`, `method`, and `path` are `null` when the request line or `Host` header could not be parsed (the line is still emitted, recording that a connection reached the catcher). `tls` is `null` for a plaintext connection and the object shape above for a TLS connection.

**`decision` value set.** Identical to `x-dns-trace` (§7.6.10): the closed v0.1.x set is `"forward" | "sink" | "sink-oob"`. A consumer MUST treat any value outside this set as a record from a newer producer it does not understand.

**`tls` object fields** (present when `tls` is non-null): `sni` (string or null — the ClientHello server name); `record_bytes` (int — bytes of the first TLS record observed); `handshake` (string — the handshake stage, e.g. `"client_hello"`); `terminated` (bool — whether the catcher MITM-terminated and decrypted the connection, in which case `body` is plaintext).

**Optional extensions:**

- `client` (string) — source `ip:port` of the connecting socket.
- `seq` (int) — producer-local monotonic per-connection sequence number.
- `matched_rule` (string) — the policy rule that produced the decision (same provenance labels as `x-dns-trace`). Free-form, not a closed set.
- `allowlisted_host_at_sink` (bool) — true when an allow-listed `Host` arrived at the sink catcher (the host-spoof-to-evade anomaly described above).
- `headers` (array) — captured request headers, as `[name, value]` pairs.
- `content_length` (int or null) — the declared `Content-Length`, when present.
- `body` (string or null) — the captured request body, up to the producer's capture cap (plaintext for a terminated TLS connection; absent for a non-terminated one).
- `body_bytes` (int) — number of body bytes captured.
- `body_truncated` (bool) — true when the body was cut off at the capture cap or the connection closed mid-body.

#### 7.6.12 Promotion path

A `x-*` kind in this subsection becomes a candidate for §7.2 baseline promotion when:

1. The kind has shipped in production traces from at least one producer.
2. At least one consumer (detector framework, trace reader, replay tool) consumes the kind in a non-trivial code path.
3. The minimum-guaranteed shape has been stable across the most recent revision in the v0.1.x line (no contract-breaking changes).

Promotion moves the kind to §7.2 baseline and is made only by a revision that changes `Y` (§4.2), in which the `x-` prefix MAY drop; the §7.6 entry SHOULD then be retained as an alias through at least one further revision, to give modules transition time.

#### 7.6.13 Rationale and capture mechanisms

Per-kind field-shape rationale, capture-mechanism mapping (e.g. which eBPF probes or userspace hooks emit which kind), and the structural reasoning behind the minimum-guaranteed-plus-extensions pattern are recorded outside this specification. The spec records the normative contract; the rationale, recorded separately, says why each contract is shaped as it is.

---

## 8. Clock Attestation

### 8.1 Why clocks are first-class

The trace spec (`minitrace-spec` §3.6) requires a single logical clock per trace and punts skew handling and clock attestation to the envelope. This section defines that attestation.

Several deployment patterns in scope make clocks adversarial or experimentally manipulated:

- **Y2038 scenarios.** Sessions deliberately run with a fast-forwarded clock to test agent behaviour near the 32-bit signed-time-t boundary.
- **Replay studies.** A session re-run against a frozen clock to make timing reproducible.
- **Distributed harnesses.** A session whose timestamps come from multiple physical hosts that may drift relative to each other.
- **Untrusted harness clocks.** A session captured from a system whose clock is not under the producer's control; the producer wishes to record this fact.

A simple "trusted timestamp" model does not cover any of these. v0.1.0 instead lets the envelope declare what clocks were used and asserts properties (or non-properties) of each.

### 8.2 Clock declaration

Every envelope SHOULD declare at least one clock. An envelope with no `clocks[]` declaration is treated as "all timestamps are from the producer's host clock at envelope creation time, unattested".

```json
{
  "id": "<clock-id>",
  "kind": "host_system" | "external_authority" | "simulated" | "frozen" | "manipulated" | "x-<custom>",
  "description": "<free text>",
  "authority": "<URI or identifier of trusted time source, if any>",
  "drift_attestation": null | { ... },
  "manipulated": true | false,
  "manipulation_notes": "<required when manipulated is true>"
}
```

| Kind | Meaning |
|---|---|
| `host_system` | The capturing host's system clock. No external attestation. |
| `external_authority` | A clock whose values are obtained from an external time source (NTP, RFC 3161 timestamping authority, blockchain timestamp). `authority` names the source. |
| `simulated` | A clock provided by a simulation framework whose values do not correspond to wall time (e.g. discrete-event simulator). |
| `frozen` | A clock held at a fixed value for the duration of the session (e.g. replay studies). |
| `manipulated` | A clock deliberately set to an unusual value (e.g. Y2038 fast-forward). The session-relevant fact is being signalled, not concealed. |

### 8.3 Manipulated clocks

When a session is run against a manipulated clock, the envelope MUST declare `manipulated: true` and populate `manipulation_notes` with at minimum: target wall-clock state at session start, manipulation method (system clock override, library shim, time virtualisation), and intended research purpose. This makes the manipulation visible to consumers; concealing it would defeat the integrity claim of the envelope.

A manipulated-clock envelope is still a valid envelope. Consumers performing temporal analysis MUST treat manipulated clocks as untrusted for wall-time inferences and MUST NOT presume them monotonic: a system clock override or library shim can move a clock backward. An ordering inference within the session then rests on another source: for trace events, the `minitrace-spec` §4.3 order is then timestamp order, not necessarily the order of occurrence, and a turn's `index` and array position give the producer's order; within one witness stream, a producer-local sequence number where a record carries one (§7.6.10, §7.6.11).

### 8.4 Drift attestation

For external-authority and host-system clocks, a producer MAY include a `drift_attestation` block recording offset measurements against a reference clock at session start and end:

```json
{
  "reference_authority": "<URI>",
  "offset_at_start_ms": <int>,
  "offset_at_end_ms": <int>,
  "measurement_method": "ntp_offset" | "rfc3161_roundtrip" | "x-<custom>"
}
```

Drift attestation is informational. A consumer can use it to bound timestamp accuracy but it is not a normative integrity claim.

### 8.5 Per-trace clock binding

Each trace bundled in the envelope has timestamps that come from one logical clock per `minitrace-spec` §3.6. The envelope manifest binds traces to clocks:

```json
{
  "trace_id": "<id>",
  "clock_id": "<clock id from clocks[]>"
}
```

This binding is OPTIONAL when the envelope declares exactly one clock and all traces use it. When multiple clocks are declared, the binding becomes REQUIRED for unambiguous interpretation.

### 8.6 Clock for envelope's own timestamps

The envelope's `created_at` and any envelope-level annotation timestamps reference the clock named by `producer.host_clock_id`. This clock SHOULD be one of the declared clocks; if it is not declared, consumers MUST treat envelope timestamps as `host_system` unattested.

---

## 9. Signature

### 9.1 Signature framework

An envelope MAY be signed by its producer (or counter-signed by attestors). Signature data lives in `signature.json` at envelope root:

```json
{
  "signatures": [
    {
      "id": "<signature id>",
      "signer": "<principal>",
      "algorithm": "<algorithm identifier>",
      "key_id": "<key identifier>",
      "manifest_hash": "sha256:...",
      "signed_at": "<ISO 8601>",
      "signature": "<base64-encoded signature bytes>",
      "covers": ["manifest"] 
    }
  ]
}
```

`manifest_hash` is the SHA-256 over the literal byte contents of `manifest.json` at signing time. Consumers verify by hashing the manifest file and comparing.

### 9.2 Cryptographic profile (deferred)

v0.1.0 does NOT normatively profile a signature algorithm. The `algorithm` field is opaque in v0.1.0; deployments using signatures MUST agree on an algorithm out of band. Candidate profiles for v0.2.0 include Ed25519 with raw signatures, COSE-Sign1, and PASETO v4.public; the choice depends on key-management and transport requirements that have not been resolved.

A consumer that requires evidence-grade signatures MUST NOT rely on an unprofiled `algorithm` value; it MUST be configured with a specific algorithm and key set.

### 9.3 Signature and redaction

Signing covers the manifest's content hash at signing time. Any change to the manifest (adding a witness, updating a clock, or changing a hash because content was redacted or because a bundled trace was extended by an addition `minitrace-spec` §3.1 permits, such as an appended annotation) invalidates the signature. A bundled trace that is redacted or extended without the manifest being changed no longer matches the `file_content_hash` the manifest lists for it (§6.2, step 1) nor, once its `content_hash` is recomputed, the listed `trace_content_hash` (§4.6). What this section says of redaction after signing holds for such an addition after signing as well, a co-signed record then naming what was added. v0.1.0 does NOT support redaction-tolerant signatures; redaction-after-signing requires re-signing the new manifest, with the prior signature invalidated. Every signature in `signatures[]` that covers `manifest` and was made over the earlier manifest, an attestor's counter-signature included, then fails §6.2 step 5. An envelope that keeps one is treated as informational only by every consumer (§6.2), so a re-signing meant to restore evidence value replaces or removes each of them; a counter-signature that cannot be renewed is lost.

For deployments where redaction-after-signing is essential and re-signing is not possible (signer no longer available, integrity claim must persist across redactions), v0.1.0 offers two patterns; the second keeps the redaction auditable but, within this specification, not the evidence value the signature gave the envelope:

- **Supersession.** Issue a new envelope referencing the redacted state, with a `derived-from` lineage edge inside its primary trace pointing at the original envelope's primary trace. The original envelope's signature remains valid for its prior state.
- **Co-signed redaction record.** Have the redactor sign a separate detached record naming what was redacted, why, and the new manifest hash. This is not part of the envelope's `signatures[]` array; it is an auditable artefact bundled as a witness or external attestation. While the envelope's original signature stays in `signatures[]`, it fails against the changed manifest and every consumer treats the envelope as informational only (§6.2). Removed, it leaves an unsigned envelope whose weight is for the consumer's evidence policy. This specification defines no verification of the record; a deployment may check it by its own means.

A future revision will introduce Merkle-tree-based signatures permitting in-place leaf removal, deferring scheme selection until concrete redaction-frequency data is available.

### 9.4 Signature scope

`covers` enumerates what the signature attests. v0.1.0 defines one value, `manifest`. Future values may include `producer_identity_binding` (signature covers a producer-identity claim independent of the manifest) and `clock_attestation` (signature attests to a specific clock state).

### 9.5 Verification

A consumer verifying an envelope:

1. Reads `manifest.json` and computes its byte hash.
2. Reads `signature.json`.
3. For each signature whose `covers` includes `manifest`: verifies the signature against the configured key for `key_id`, comparing `manifest_hash` to the recomputed hash.
4. If any signature in `signatures[]` is required for the consumer's evidence policy and verification fails, the envelope MUST be rejected.
5. Consumers MUST NOT silently downgrade a failed signature verification to "valid but unsigned"; failure is failure.

---

## 10. Pre-Conversion Artefacts (Raw Logs)

### 10.1 Why preserved

Raw harness output is preserved in the envelope so the trace's adapter mapping is verifiable: a consumer can run the adapter against the preserved raw input and confirm the trace matches. This supports chain-of-custody claims and adapter regression testing.

### 10.2 Listing

Each preserved raw artefact appears in `raw_artefacts[]` per §4.7. Files live under `raw/<harness>/...` by convention.

### 10.3 Adapter pinning

When raw artefacts are bundled, the producer SHOULD also bundle the adapter version that produced the trace. Where adapters are expressed as DSL files (per the adapter DSL spec, separate document), the DSL file is itself a raw artefact of `kind: "x-adapter-dsl"` (§4.7). `harness_artefact` is a witness kind (§7.2), not a raw-artefact kind, so it does not apply here; a dedicated raw-artefact kind for DSL files waits for a revision that changes `Y` (§4.2).

### 10.4 Redaction of raw logs

Raw artefacts MAY be redacted via the same model as witnesses (§7.4), carried in the raw artefact's own `redaction` field (§4.7). Redacting a raw artefact while keeping the trace it produced is acceptable; the chain-of-custody claim is correspondingly weakened (the trace's mapping is no longer reproducible from the bundled raw input). The redaction record makes this state visible.

Raw harness logs are commonly line-delimited, so reducing one for distribution uses `target_kind: "payload_keys"` with the withheld key names in `targets[]`, and §4.7's `content_hash` covers the reduced bytes as bundled. Dropping an artefact entirely uses `operation: "remove"` (with `target_kind` `witness` or absent), which keeps the entry, and its `original_content_hash` where one is recorded, while the file itself is absent; the entry then carries none of §4.7's file fields (`path`, `content_hash`, `size_bytes`).

This is what separates a withheld artefact from one that never existed. An entry carrying a redaction record states that a source existed, names its pre-redaction hash where one is recorded, and says who authorised the reduction and why. An artefact absent from `raw_artefacts[]` states only that the trace stands as the only record (§10.5), which is equally the correct condition for a natively-emitted trace that never had a source log. A producer distributing a reduced bundle SHOULD therefore list the artefact with a redaction record rather than drop the entry, so that a consumer holding only the reduced bundle can still tell the two apart.

### 10.5 Producing-trace link

`raw_artefacts[].produces_trace_id` is the bidirectional link from raw input to derived trace. A trace MAY be produced from multiple raw artefacts (the link is many-to-one from raw to trace). When this link is present, the consumer can attempt adapter re-run for verification; when it is absent, the trace stands as the only record.

---

## 11. Trust and Threat Model

### 11.1 In scope

The envelope's threat model covers:

- A consumer receiving a corrupted or partially modified envelope. Hash verification (§6.2) detects modification.
- A consumer receiving an envelope from an unknown producer when an evidence policy requires producer attestation. Signature verification (§9) addresses this within the limits of §9.2.
- A producer making honest mistakes (truncated witness, missing hash entry, version mismatch). Manifest validation surfaces these.

### 11.2 Out of scope (or partially addressed)

- A producer fabricating traces and witnesses. The envelope provides integrity over what the producer chose to bundle; it does not validate independent reality.
- A producer deliberately concealing manipulated clocks. §8.3 makes manipulation declaration normative, but a malicious producer can lie about it. Detecting clock-manipulation lies is out of scope.
- A producer redacting selectively to alter the bundle's narrative. Redaction is auditable in-bundle (§7.4, §10.4) but only at the level of "something was removed for reason X"; the integrity claim is on the redaction record's existence, not on the appropriateness of the choice.
- Chain-of-custody during transport. Once an envelope leaves the producer, custody is enforced by signing (§9) plus deployment-specific transport guarantees, not by the envelope format.
- Regulatory evidence-grade properties. The envelope provides primitives. Whether those primitives meet a specific evidence-grade threshold for any regulatory regime is a question for that regime's analysis, not for this specification.

### 11.3 Adversarial-condition envelopes

Envelopes recording deliberate adversarial conditions (manipulated clocks, fault-injection harnesses, compromised-environment tests) are valid and supported. The envelope makes the adversarial conditions explicit (clock manipulation, witness types describing the injected conditions) so consumers can reason about them rather than mistaking them for production-grade evidence. Deployments treating envelopes as evidence MUST inspect clock attestation and witness-kind sets before accepting.

---

## 12. Backwards Compatibility

This is v0.1.1, an additive revision of v0.1.0 per §4.2: every conformant v0.1.0 envelope that does not itself use a name this revision defines (`path_hashes` and `original_path_hashes`, §7.1 and §7.4; `target_kind` and `targets`, §7.4; `capture_status`, §7.6.1; the witness kinds `x-dns-trace` and `x-http-catch`, §7.6.10 and §7.6.11) and whose witness `content_hash` and `size_bytes` follow the readings §7.1 now fixes for them, which v0.1.0 left unstated, and whose raw artefacts and witnesses removed as a whole carry no file fields (§7.4, §10.4), and that bundles only v0.3.x traces (below), each carrying its own `content_hash` (§4.6), and whose witness records meet §7.1's numeric domain, JSON form and nesting depth rules, and whose manifest meets §3.3's numeric domain and JSON form rules, and whose trace listings each carry the `id` of the trace they list and a `trace_id` no other trace listing carries, and whose `produces_trace_id` values each equal the `trace_id` of exactly one trace listing (§4.6, §4.7), is a conformant v0.1.1 envelope.

The envelope is consistent with `minitrace-spec` v0.3.0. In an envelope that declares `0.1.1` or a later revision, bundled traces MUST conform to v0.3.0 or any later revision in the v0.3.x line; a v0.2.x trace is migrated (`minitrace-spec` §12) before it is bundled. v0.1.0 also admitted v0.2.x traces as a transitional case, the trace's own `schema_version` declaring the version and the trace listing's `schema_version` recording it, and an envelope that declares `0.1.0` is held to no requirement of this revision (below). In this specification a v0.3.x trace is one whose own `schema_version` is `0.3.<n>` (`minitrace-spec` §3.1), and a v0.2.x trace one whose own `schema_version` is `minitrace-v0.2.0`, the value v0.2.0 defines (`minitrace-spec` §1.4, §12.1.1); a trace declaring `minitrace-v0.1.0` is not a v0.2.x trace, although v0.2.0 holds a valid v0.1.0 trace to be a valid v0.2.0 trace and directs its validators to accept that value; the v0.2.x traces that v0.1.0 admits (v0.1.0 §12) are read the same way, so in an envelope that declares `0.1.0` a trace declaring `minitrace-v0.1.0` fails §6.2 step 2. A consumer MUST NOT assume v0.3.0 features in a v0.2.x trace bundled that way, and §6.2 step 2 verifies no internal `content_hash` in it, since v0.2.x defines none: a `content_hash` member such a trace carries is not checked (§6.2), while the listing's `trace_content_hash`, which excludes the member, and `file_content_hash`, which covers it (§4.6), still are.

**Understanding a version.** The revisions of one line, `0.1.0`, `0.1.1` and any later `0.1.<n>`, are additive, so a consumer understands every `envelope_version` in the line it implements. A consumer implementing any v0.1.x revision MUST accept an envelope declaring `0.1.<n>` for every `<n>`, and MUST ignore top-level fields and record fields it does not recognise; an envelope from a later revision of the line therefore verifies under §6.2 with the fields this consumer knows. Producers SHOULD declare the revision whose fields they emit. An envelope is held to no requirement of a revision later than the one it declares, so a consumer can rely on a requirement that a revision adds over an earlier one, such as §7.1's `path_hashes`, only for an envelope that declares that revision or a later one (§6.2 says what a consumer does with a witness that has no `path_hashes`). A future v0.2.0 MAY introduce changes that break v0.1.x envelopes; a consumer SHOULD reject an envelope whose `envelope_version` lies outside the line it implements.

---

## 13. JSON Schema

A normative JSON Schema for the manifest (§4) is published alongside this specification as `minitrace-envelope-v0.1.1.schema.json`; the schema for v0.1.0 RC 1 is published with it as `minitrace-envelope-v0.1.0.schema.json`. Which v0.1.0 envelopes are conformant v0.1.1 envelopes is stated in §12. Each version ships one schema file; multi-file splitting is deferred until a second envelope variant exists.

---

## 14. Security Considerations

The envelope inherits the security considerations in `minitrace-spec` §14 for any bundled traces. The following considerations are envelope-specific.

### 14.1 Untrusted manifest entries

A manifest declares paths and hashes. A malicious or malformed manifest can:

- Reference paths outside the envelope root (`..` traversal). §3.2 forbids this; consumers MUST enforce the rule at load time.
- Reference very long paths or unusual encodings. Consumers SHOULD normalise paths and reject those that fail Unicode normalisation or exceed reasonable length.
- Declare hashes that do not match the file. §6.2 requires recomputation; consumers that skip recomputation accept any claim.
- Reference paths that exist but were not authored by the producer (planted files). Hash-recomputation defends against this only if the producer's signature covers the manifest.

### 14.2 Witness binary content

Binary witnesses (packet captures, captured executables, screenshots) are arbitrary bytes from a session that may have run untrusted code. Consumers loading binary witnesses for further analysis MUST treat them as fully untrusted: never auto-execute, never auto-parse with format-specific tools without sandboxing, never feed to third-party services without sanitisation.

### 14.3 Inference capture sensitivity

Inference captures (`inference_capture` kind) may contain model-internal data that, depending on the model and capture method, can include partial training-data reconstructions, embedding-space information about other prompts, or other sensitive intermediate state. Producers bundling inference captures SHOULD evaluate the residual sensitivity of those captures against deployment policy; consumers receiving them inherit the sensitivity.

### 14.4 Signature verification timing

A consumer MUST NOT load and act on traces or witnesses before signature verification (when signatures are required by evidence policy). Acting first and verifying later opens a window where a bad envelope can affect downstream state.

### 14.5 Clock manipulation visibility

§8.3 requires manipulated clocks to be declared. Consumers MUST surface manipulated-clock state to any human or system using the envelope as evidence; failing to surface this is equivalent to misrepresenting the envelope's content.

### 14.6 Cross-envelope reference resolution

Resolving a cross-envelope reference (§6.4) is a fetch operation against external storage. The same SSRF and side-channel considerations as `minitrace-spec` §14.2 (schema URI fetch) apply. Consumers SHOULD restrict cross-envelope resolution to allowlisted storage backends.

---

## 15. Privacy Considerations

The envelope inherits `minitrace-spec` §15 for bundled traces. Envelope-specific concerns:

### 15.1 Witnesses contain unstructured data

Many witness kinds (screenshots, network captures, environment snapshots) capture data the trace did not. PII detection inside witnesses is harder than inside traces (no structured field set to scan). Producers bundling witnesses SHOULD apply PII detection to witness payloads at production time and either redact (§7.4) or label classification accordingly. v0.1.0 does NOT mandate a detection method.

### 15.2 Raw harness logs

Raw logs (§10) typically contain everything the harness saw: full operator input including any pasted PII, full tool output including any retrieved customer content. They are usually higher-risk than the corresponding trace. Producers SHOULD treat raw-log inclusion as an explicit decision per envelope; default deployments SHOULD NOT bundle raw logs unless evidence policy requires them.

### 15.3 Cross-trace PII propagation

When an envelope contains multiple traces (e.g. a session and its judges, plus witnesses), PII present in one artefact may need redaction across all of them. The envelope provides the manifest as a place to enforce consistency; producers SHOULD apply redaction sweeps across the full bundle, not just one artefact at a time.

### 15.4 Inference captures and operator privacy

Inference captures may encode operator content in ways the operator was not made aware of (an activation tensor partly determined by a pasted CV section). Operators consenting to session capture per `minitrace-spec` §5 have not necessarily consented to inference-internal capture. Producers bundling inference captures from sessions with `operator.type: "human"` SHOULD treat the inference capture as a separate consent boundary and document this in `producer.notes`.

---

## 16. Open Questions

Items deferred from v0.1.0 to a future revision.

| Question | Deferred because |
|---|---|
| Cryptographic profile for §9 signatures | Choice depends on key management not yet resolved; placeholder framework allows deployment-specific selection |
| Redaction-tolerant signature scheme | Real-world redaction frequency and patterns needed before committing to a scheme |
| Typed sub-shape for `inference_capture` | Capture pipelines still evolving; premature schema would lock in choices that bind upcoming research |
| Container packing format | Directory layout works; choice between zip / tar / OCI / git-style content-addressed store is a deployment concern, not a format concern |
| Cross-envelope resolution mechanism | Pending experience with multi-envelope deployments |
| Producer-identity binding scheme | Depends on identity service choice (organisation directory, decentralised identity, key registry) |
| Multi-attestor coordination protocol | One signer is sufficient for v0.1.0; coordination becomes interesting only at scale |

---

## Appendix A: Minimal Worked Example

A minimal envelope bundling one agent-session trace, one raw harness log, and one screenshot witness:

```
example-envelope/
  manifest.json
  traces/
    agent-session-example.minitrace.json
  raw/
    pi/
      output.log
  witnesses/
    w-screenshot-001/
      session-end.png
```

Manifest excerpt:

```json
{
  "envelope_version": "0.1.1",
  "envelope_id": "0193d4a0-aaaa-7890-abcd-ef0123456789",
  "created_at": "2026-05-03T10:01:00.000000Z",
  "producer": {
    "id": "spec-example-pipeline",
    "kind": "automated_pipeline",
    "tool": "minitrace-envelope-builder/0.1.0",
    "host_clock_id": "host-clock-0"
  },
  "clocks": [
    {
      "id": "host-clock-0",
      "kind": "host_system",
      "manipulated": false
    }
  ],
  "traces": [
    {
      "trace_id": "0193d4a0-1234-7890-abcd-ef0123456789",
      "path": "traces/agent-session-example.minitrace.json",
      "trace_content_hash": "sha256:9d67dbcd9141f00c4b39418be2c8d5e9a78ab740d6afdf76deeaf1fafca3b016",
      "file_content_hash": "sha256:...",
      "trace_type": "agent-session",
      "schema_version": "0.3.0",
      "primary": true,
      "clock_id": "host-clock-0"
    }
  ],
  "raw_artefacts": [
    {
      "id": "raw-001",
      "path": "raw/pi/output.log",
      "kind": "harness_log",
      "harness": "pi",
      "content_hash": "sha256:...",
      "size_bytes": 1234,
      "produces_trace_id": "0193d4a0-1234-7890-abcd-ef0123456789"
    }
  ],
  "witnesses": [
    {
      "id": "w-screenshot-001",
      "kind": "screenshot",
      "scope": {
        "type": "trace",
        "target_trace_id": "0193d4a0-1234-7890-abcd-ef0123456789"
      },
      "captured_at": "2026-05-03T10:00:01.500000Z",
      "captured_by": "pi-screenshot-tool",
      "content_hash": "sha256:...",
      "size_bytes": 48213,
      "primary_path": "witnesses/w-screenshot-001/session-end.png",
      "auxiliary_paths": [],
      "path_hashes": { "witnesses/w-screenshot-001/session-end.png": "sha256:..." },
      "redaction": null
    }
  ],
  "annotations": [],
  "extensions": {}
}
```

This envelope is unsigned. Adding signing requires writing `signature.json` per §9.

---

## Appendix B: Adversarial-Condition Envelope Sketch

A Y2038-scenario envelope with a manipulated clock:

```json
{
  "clocks": [
    {
      "id": "y2038-fast-forward",
      "kind": "manipulated",
      "description": "System clock fast-forwarded to 2038-01-19T03:14:06Z minus 30s at session start to test agent behaviour at the 32-bit signed time_t boundary.",
      "manipulated": true,
      "manipulation_notes": "faketime LD_PRELOAD wrapper; offset target 2038-01-19T03:14:06Z; method: faketime '@2038-01-19 03:14:06'; intended purpose: behavioural research on time-handling edge cases."
    }
  ]
}
```

Consumers encountering this envelope MUST surface the `manipulated: true` clock when presenting the envelope as evidence; the entire bundle is implicitly tagged as a research artefact, not as a record of real-time activity.

---

*End of v0.1.1.*
