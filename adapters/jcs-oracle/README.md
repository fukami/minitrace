# jcs-oracle

Independent JCS (RFC 8785) + SHA-256 oracle for cross-language `content_hash`
parity tests against `adapters/cross_trace.py`.

This is **Path B** of the v0.3.0 §3.4 cross-implementation verification
work: the Python reference (`cross_trace.py`) computes a trace's
`content_hash` via `rfc8785.dumps()` + `hashlib.sha256`. The spec claims
the canonical form is byte-identical regardless of programming language.
This oracle is the falsifier: a Rust implementation of the same
JCS+SHA-256 pipeline, against which we can feed edge cases (numeric
formatting around 1e-7 / 1e16, non-BMP key sort, deep nesting, etc.) and
assert equal output.

## What this is, what this isn't

This is:

- A CLI that reads one JSON document from stdin and writes either the
  JCS canonical form or `sha256:<hex>` of those bytes.
- Test-grade tooling, used by the Python-side parity driver at
  `adapters/tests/test_jcs_parity.py` which runs the same fixtures
  through both `rfc8785.dumps()` and this binary and asserts
  byte-equal canonical output + equal `sha256:<hex>` digest.

This is **not**:

- A trace verifier. It does not strip a `content_hash` field, does not
  resolve `${trace:...:content_hash}` placeholders, does not compare to
  a stored hash on a trace. Those are trace-shape concerns belonging
  to a downstream verifier; the oracle only proves the JCS+SHA-256
  floor matches Python.
- A library. The crate has `publish = false`; the artefact is the bin.

## Build

```sh
cd adapters/jcs-oracle
cargo build --release
# binary at: target/release/jcs-oracle
```

## Usage

```sh
echo '{"b":2,"a":1}' | target/release/jcs-oracle
# → {"a":1,"b":2}

echo '{"b":2,"a":1}' | target/release/jcs-oracle --hash
# → sha256:<64 hex>
```

Stdin is mandatory; the binary does not accept a file path argument
(parity drivers always pipe).

## Numbers and `arbitrary_precision`

`serde_json_canonicalizer` explicitly normalises numeric tokens through
the IEEE 754 binary64 representation, matching RFC 8785's prescription
that JSON numbers are doubles. `serde_json` (without
`arbitrary_precision`) parses numeric tokens to `f64`. Python's
`json.loads` likewise produces a `float` for non-integer tokens, which
`rfc8785.dumps()` then re-emits per the same RFC 8785 numeric rules.

If a parity test surfaces a mismatch on a value that both languages
should be parsing identically, the cause is the JCS implementation, not
this scaffold. That is the bug the parity test exists to catch.

## Status

v0.1.0 scaffold + parity driver (`adapters/tests/test_jcs_parity.py`,
10 byte-equal fixtures + 1 documented-divergence fixture covering the
2^53 + 1 safe-integer guard). All 11 currently green against
`rfc8785` 0.1.4 + `serde_json_canonicalizer` 0.3.2.

The documented divergence is the first finding from Path B: Python
`rfc8785` REJECTS integers > 2^53 - 1 (guards against silent f64
quantisation), Rust `serde_json_canonicalizer` ACCEPTS and silently
quantises through f64. Both are defensible against RFC 8785; the
implication for minitrace v0.3.0 §3.4 is that a trace containing a
bigint value would hash inconsistently between Python and Rust
verifiers.

**Spec-side resolution:** minitrace-spec v0.3.0 constrains the numeric
domain in §3.4: a number whose binary64 value lies outside the inclusive
range [−(2^53−1), 2^53−1] MUST be carried as a JSON string, and a trace
carrying one as a JSON number is not conformant. §14.3 records what
remains: because such traces exist in the wild, consumers SHOULD NOT
treat a `content_hash` mismatch on traces carrying out-of-range numbers
as tampering without first ruling out verifier-implementation divergence.

## See also

- `adapters/cross_trace.py` — Python reference (§3.4 implementation).
- `adapters/minitrace-verify/` — trace-aware verifier built on this
  oracle's JCS+SHA-256 floor; operationalises §14.3 by emitting a
  divergence note on mismatch over bigint-bearing traces.
- minitrace-spec v0.3.0 §3.4 — pins JCS for `content_hash`.
- minitrace-spec v0.3.0 §14.3 — bigint cross-implementation reproducibility caveat.
- RFC 8785 — JSON Canonicalization Scheme.
