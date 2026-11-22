# minitrace-verify

Trace-aware verifier for minitrace v0.3.0 §3.4 `content_hash`.

This is the **Scope B extension** of the Path B JCS oracle work. Where
`jcs-oracle` (sibling crate) proves the JCS + SHA-256 *floor* matches the
Python reference with no trace-shape knowledge, this CLI is the
trace-aware verifier built on that floor. It has two modes:

**Single-trace** (one `--file`, or stdin):

1. Read one minitrace JSON document.
2. Strip the `content_hash` field.
3. Canonicalise the remainder per RFC 8785 (JCS).
4. SHA-256 the canonical bytes.
5. Compare the recomputed digest to the trace's stored `content_hash`.

**Set / edge-consistency** (multiple `--file`, or `--dir`):

1. Load every trace in the set.
2. Self-verify each one that carries a non-null `content_hash` (as above).
3. For every `lineage[]` edge carrying a resolved `target_content_hash`
   (a cross-trace integrity edge, §7.1), confirm the referenced in-set
   trace carries a `content_hash` (§7.1) and that it equals the
   edge's `target_content_hash`. Edges whose referent is not in the set
   are reported but not failed (external referent).

**`--strict`** (set mode only): a set passes only if everything in it was
verified. A trace with a null `content_hash`, an edge to a trace outside
the set, and an edge whose hash differs from its referent's (another state
of it, trace §7.1) each count as a problem instead of being reported only. Without it, a set of unhashed
traces reports `ok` although nothing was checked. `--strict` needs `--file`
or `--dir` (stdin is single-trace mode, exit 2), and a `--dir` holding one
trace stays in set mode.

In either mode, a trace holding a number outside the §3.4 numeric domain is
reported as not conformant (see "Out-of-domain numbers" below).

## What this is, what this isn't

This is:

- A trace-shape verifier for a single minitrace document, or for a set
  with cross-trace integrity edges between members.
- The operational realisation of minitrace-spec v0.3.0 §14.3: on
  mismatch over a trace containing integer literals outside the
  JCS-safe range [−(2^53−1), 2^53−1], the output carries an
  explanatory note pointing at §14.3, so a consumer doesn't treat the
  mismatch as tampering without first ruling out
  verifier-implementation divergence.
- The verifier-side counterpart of `cross_trace.py`'s producer-side
  resolver: it *checks* resolved cross-trace edges, the resolver
  *produces* them.
- Test-grade tooling — `publish = false`; the artefact is the bin.

This is **not**:

- A placeholder resolver. `${trace:<id>:content_hash}` placeholders are
  a producer-side authoring construct that `cross_trace.py` resolves
  *before* stamping; published traces do not carry them. Per DSL spec v0.1.0
  §13.1 the only placeholder-bearing field is `lineage[].target_content_hash`:
  an unresolved placeholder there, or any value that is neither null nor a
  well-formed `sha256:<64 hex>` hash, exits 2. Placeholder-shaped text in any
  other field is content and is not inspected, so a session that quotes the
  syntax verifies. This CLI verifies already-resolved edges; it does not
  back-fill them.
- A JCS oracle. For raw JCS+SHA-256 parity against `rfc8785.dumps()`,
  use the sibling `jcs-oracle` crate (it has no trace-shape
  knowledge by design).
- A signature verifier. `content_hash` is integrity-against-storage,
  not authentication — see §14.3 for the property delta.

## Build

```sh
cd adapters/minitrace-verify
cargo build --release
# binary at: target/release/minitrace-verify
```

## Usage

```sh
# single trace, from a file:
target/release/minitrace-verify --file path/to/trace.minitrace.json

# single trace, from stdin:
target/release/minitrace-verify < path/to/trace.minitrace.json

# a set (edge-consistency mode), explicit files:
target/release/minitrace-verify --file a.minitrace.json --file b.minitrace.json

# a set, every *.minitrace.json in a directory:
target/release/minitrace-verify --dir examples/v0.3.0

# a set that passes only if everything in it was verified:
target/release/minitrace-verify --strict --dir out/
```

Exit codes:

| Code | Meaning |
|------|---------|
| `0` | all checks passed |
| `1` | a check ran and found a problem (self-hash mismatch; a cross-trace edge naming a referent with no hash; a trace outside the §3.4 numeric domain, reported as `not conformant` on its own line even when the hash verifies; a `judges`, `reviews`, `audits` or `wraps` edge without `target_content_hash`, which §7.1 makes REQUIRED; a `lineage[]` entry whose `type` or `target_trace_id` is not a non-empty string (absent, null, empty or another JSON type; the base schema requires both on every edge); each in either mode; with `--strict`, anything left unverified) |
| `2` | a check could not run (missing `content_hash` in single mode, parse failure, duplicate ids, unresolved placeholder or malformed hash in `lineage[].target_content_hash`, `--strict` without `--file` or `--dir`, I/O, a trace of more than 256 MiB) |

Sample output, match:

```
verified: sha256:7c92a3e8...
```

Sample output, mismatch:

```
mismatch:
  stored:     sha256:19717bbd3f4cc88ef0b9a570ee9922915b0b6d9806bb75b988b8ba9f776d50c5
  recomputed: sha256:9d67dbcd9141f00c4b39418be2c8d5e9a78ab740d6afdf76deeaf1fafca3b016
```

Sample output, a trace holding `9007199254740993` whose hash verifies
(exit 1):

```
verified: sha256:10156790...
not conformant: number outside [-(2^53-1), 2^53-1] by value; minitrace-spec
        v0.3.0 §3.4 requires such numbers as strings (the hash does not pin their values).
```

Sample output, the same trace with a wrong stored hash (exit 1; §14.3 note):

```
mismatch:
  stored:     sha256:1111...
  recomputed: sha256:10156790...
  note: trace contains numbers outside [-(2^53-1), 2^53-1];
        mismatch may be verifier-implementation divergence
        per minitrace-spec v0.3.0 §14.3.
not conformant: number outside [-(2^53-1), 2^53-1] by value; minitrace-spec
        v0.3.0 §3.4 requires such numbers as strings (the hash does not pin their values).
```

Sample output, malformed stored hash:

```
mismatch:
  stored:     <malformed content_hash: sha256:0000...\u{000d}\u{001b}[2K... [truncated, 171 chars]>
  recomputed: sha256:efd1560ad8fceb106fe67f8ab1de7d70c27860c0dd2e035b1ce0eec822d1720a
```

Everything this tool prints that came from a trace, a lineage edge, or a
filename is attacker-controlled, so hashes are checked against their own syntax
before being printed as hashes, and every other echoed value has its length
bounded and these characters escaped: control characters (Unicode category
Cc); the directional marks, embeddings, overrides and isolates U+200E, U+200F,
U+202A to U+202E and U+2066 to U+2069; the zero-width characters U+200B to
U+200D and U+FEFF; the word joiner and invisible operators, as the range
U+2060 to U+2065; and the line separators U+2028 and U+2029. Other format
characters pass through unescaped, among them U+061C ARABIC LETTER MARK,
U+00AD SOFT HYPHEN and the tag characters U+E0001 and U+E0020 to U+E007F.
A value that
verifies as well-formed prints unchanged, so output over valid traces is
byte-identical to earlier releases.

Sample output, unresolved placeholders:

```
verify: trace contains unresolved `${trace:<id>:content_hash}` placeholders; this CLI does not resolve them (they are producer-side — see adapters/cross_trace.py)
```
(exit 2; message on stderr)

Sample output, set mode (consistent pair):

```
minitrace-verify: set mode (2 traces)
  agent-session-example.minitrace.json: verified sha256:9d67dbcd...
  judge-session-example.minitrace.json: verified sha256:443b60e0...
  edge judge-session-example.minitrace.json -> agent-session-example.minitrace.json: consistent (sha256:9d67dbcd...)
ok: 2 self-verified, 1 edges consistent, 0 external edges, 0 edges to another state, 0 problems
```

An edge whose `target_content_hash` differs from its in-set referent's
stored hash refers to another state of the referent (minitrace-spec
§7.1; envelope §6.3): it is reported with both hashes, not verified, and
is no problem in set mode; with `--strict`, where anything left
unverified fails the set, it is one.

Sample output, set mode (edge referent has no hash — §7.1):

```
minitrace-verify: set mode (2 traces)
  agent.minitrace.json: content_hash null (not self-verified)
  judge.minitrace.json: content_hash null (not self-verified)
  edge judge.minitrace.json -> agent.minitrace.json: REFERENT HAS NO HASH
      edge target: sha256:9d67dbcd...
      referent agent.minitrace.json carries no content_hash (§7.1)
FAIL: 0 self-verified, 0 edges consistent, 0 external edges, 0 edges to another state, 1 problems
```
(exit 1)

Sample output, set mode with an unhashed member, without and with
`--strict`:

```
minitrace-verify: set mode (2 traces)
  a.minitrace.json: verified sha256:9d67dbcd...
  u.minitrace.json: content_hash null (not self-verified)
ok: 1 self-verified, 0 edges consistent, 0 external edges, 0 edges to another state, 0 problems
```
(exit 0)

```
minitrace-verify: set mode (2 traces)
  a.minitrace.json: verified sha256:9d67dbcd...
  u.minitrace.json: content_hash null (not self-verified)
      --strict: an unhashed trace fails the set
FAIL: 1 self-verified, 0 edges consistent, 0 external edges, 0 edges to another state, 1 problems
```
(exit 1)

## Out-of-domain numbers

A number whose binary64 value lies outside `[−(2^53−1), 2^53−1]`, in any
notation (an integer literal such as `9007199254740993`, `1e16`,
`9007199254740992.0`, or an overflow such as `1e999`), puts the trace
outside the §3.4 numeric domain. Two things follow, in single and set mode:

- The trace is reported `not conformant` on its own line, whatever the
  hash verdict, and the run exits 1. (In single mode a trace with no
  `content_hash` stops earlier, at exit 2, before this check is reported.) A hash computed over f64-quantised
  values still matches (`9007199254740993` and `9007199254740992` hash
  alike), so `verified` alone would overstate what was checked.
- If its hash also mismatches, the mismatch carries the §14.3 note: the
  difference may be verifier-implementation divergence rather than
  tampering.

Detection runs on the **raw input bytes**, not the parsed `Value`,
because `serde_json` without the `arbitrary_precision` feature quantises
such tokens to f64 on parse and the lexical evidence is lost. A small
bytes-walker (`src/main.rs::detect_out_of_domain_number_in_raw`) tracks
string boundaries, so number-shaped text inside strings is skipped, and
checks each number token by value.

## Tests

```sh
cd adapters/minitrace-verify
cargo test
```

Rust-side unit tests cover the bigint detector, the whole-value placeholder
grammar and the lineage-hash check (including placeholder text in content and in
lineage extension fields), the edge extractor, and the stored-hash classifier.
Cross-language parity is covered separately by
`adapters/tests/test_verify.py`, which:

- computes a known-good `content_hash` via `cross_trace.compute_content_hash`,
- stamps it onto a temp file,
- runs the Rust CLI against the temp file,
- asserts exit 0 + the expected "verified:" line.

It also covers the mismatch path (mutate a field after stamping; expect
exit 1), the §14.3 hint path (synthesise a bigint-bearing trace with a
deliberately wrong stored hash; expect exit 1 + the §14.3 note), and set
mode: the example pair and `--dir` (exit 0), an orphaned integrity edge
(exit 1), an edge whose target hash differs from its referent's (exit 0,
and exit 1 with `--strict`), lineage entries that cannot be read as edges
(exit 1 in every mode), a line separator in a printed value that must not
split the output as Python's `str.splitlines()` reads it, and duplicate ids
(exit 2).

## Status

v0.2.0 (2026-06-03). Single-trace verification plus set / edge-consistency
mode (§7.1 cross-trace integrity edges). Does not resolve
`${trace:<id>:content_hash}` placeholders — those are producer-side
(`cross_trace.py`), absent from published traces, and out of scope by
design.

## See also

- `adapters/jcs-oracle/` — JCS+SHA-256 oracle (no trace-shape knowledge).
- `adapters/cross_trace.py` — Python reference; producer-side paired-
  trace placeholder resolver (`back_fill_cross_trace_references`).
- `adapters/tests/test_verify.py` — cross-language parity test for this CLI.
- minitrace-spec v0.3.0 §3.4 — pins JCS+SHA-256 for `content_hash`.
- minitrace-spec v0.3.0 §14.3 — bigint cross-implementation reproducibility caveat.
- RFC 8785 — JSON Canonicalization Scheme.
