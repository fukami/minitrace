# minitrace

minitrace is a vendor-neutral interchange format for agent traces. It defines a
portable, canonical shape for a session — turns, tool
calls, annotations, lineage between traces, redactions, and provenance — so that
sessions produced by different harnesses can be stored, compared, and analysed
with one set of tools.

Converting sessions from different harnesses into one shape lets you compare
how they handle the same task, query a folder of traces as plain JSON (with jq
or DuckDB, for example), and hand a session to someone else in a form their
tools already read.

> **Converted traces are not scrubbed.** Conversion replaces a configuration
> value only when its key name looks like a secret (`token`, `apiKey`,
> `Authorization`, ...). A secret inside a prompt, a tool call or a tool's output
> is copied into the trace unchanged. Read a trace before you share it.

This repository holds the **format specifications** and the **adapters** that
convert native harness output into the format. If you want to *build or extend*
minitrace (write an adapter, propose a spec change), read
[`AGENTS.md`](AGENTS.md) first. This README is for *using* the format.

## The specifications

minitrace v0.3 is four specifications released together as one bundle. They
reference each other; treat them as a set. Each is versioned independently (see
[`VERSIONING.md`](VERSIONING.md)).

| Spec | File | Defines |
|---|---|---|
| **Trace** | [`spec/minitrace-spec-v0.3.0.md`](spec/minitrace-spec-v0.3.0.md) | The on-disk shape of a trace: turns, tool calls, operator, annotations, lineage, redactions. |
| **Adapter DSL** | [`spec/minitrace-adapter-dsl-spec-v0.1.0.md`](spec/minitrace-adapter-dsl-spec-v0.1.0.md) | A declarative YAML language for mapping one harness's native output to a trace. |
| **Envelope** | [`spec/minitrace-envelope-spec-v0.1.1.md`](spec/minitrace-envelope-spec-v0.1.1.md) | A container that bundles traces with attestation, witnesses, and provenance. |
| **Scenario** | [`spec/minitrace-scenario-spec-v0.1.0.md`](spec/minitrace-scenario-spec-v0.1.0.md) | A framework- and model-agnostic scenario definition, and the binding record that pins a run to its `(model, harness, scenario)` triple. |

The specifications define a **format**, not an implementation. Conformance is a
property of a document (does this trace match the shape?), not of any particular
tool.

## Using the format

- **Read a trace.** A trace is a single JSON document. The trace spec is the
  authoritative description of every field; start at its §3 (trace structure).
- **Convert a session.** An adapter under `adapters/<harness>/` maps a specific
  harness version's output to a trace. Each adapter is normally a DSL file (plus
  an optional Python preprocessor for native quirks; the exceptions are noted
  below). `adapters/verify_run.py` runs one adapter over one session and checks the
  result (`AGENTS.md`, "How to build a new adapter", step 5); it does not yet
  write the trace to a file, which is planned.
- **Not every adapter is regression-gated.** The adapters with a `regression/`
  directory are frozen against captured inputs and checked on every sweep. The
  agentdojo, claude-ai, gemini-cli, mcpuniverse, minimcp, inspect-swe and otel-openinference
  adapters are included only because tests under `adapters/tests/` exercise them;
  they have no regression rung and no format reference. Of these, gemini-cli,
  inspect-swe and otel-openinference ship their preprocessor only; their DSL files
  are not part of this release. Where the validation evidence below cites one of
  them, or an adapter this release does not ship, that result comes from a corpus
  run that is not part of this repository, and the sweep does not re-check it.
- **Bundle for distribution.** The envelope spec describes how to wrap one or
  more traces with provenance and integrity material for redistribution.
- **Bind a run to a scenario.** The scenario spec defines a portable scenario
  format and a binding record that pins a run to the exact `(model, harness,
  scenario)` it was produced for — the reproducibility surface for evaluation
  and comparison.

The empirical grounding of the specifications — which elements have been
exercised by which adapters, with what evidence — is recorded in
[`docs/minitrace-v0.3.0-validation-evidence.md`](docs/minitrace-v0.3.0-validation-evidence.md).

## Adapter behaviour notes

- **Configuration is redacted by key name.** vibe's configuration snapshot, droid's
  settings sidecar and claude-code's environment snapshots are carried into the trace
  with every value under a key whose last word (ignoring a trailing `value`, `header` and
  the like) names a secret (`token`, `apiKey`, `client_secret`, `Authorization`, ...)
  replaced by `[redacted]` (booleans, nulls and empty strings are kept); the trace
  records how many (`vibe_config_redacted`, `_raw_redacted`, `environment_redacted`). The rule is in
  `adapters/redact.py`. Plurals count like the singular (`secrets`, `api_keys`), a
  trailing `pem` like `value` (`private_key_pem`), and an environment-style `*_KEY` or
  `*_PAT` name is a secret (`OPENAI_KEY`, `GH_PAT`). `tokens` is redacted only over an
  object or list, since as a number it is a count (`max_tokens`). It does not see a
  secret under an innocuous key name, a bare `key` or a
  scalar `tokens`, nor one in a prompt, a tool call or its output. codex's configuration annotation carries a few posture fields only and is not
  redacted.
- **vibe tool success is inferred.** vibe records no status field. A bash call's
  success is read from the final `returncode: N` line the harness appends (no such line
  means not successful), and a text result the harness wraps in `<tool_error>` is a
  failure; any other vibe tool call with a result, an empty one included, is recorded as
  successful, because the spec (v0.2.0, unchanged in v0.3.0) expects a boolean. A call
  with no result is recorded as not successful; `framework_metadata.vibe_has_result:
  false` tells it apart from a recorded failure.

## Running the checks

The commands below reproduce the release checks from the root of a clone of this
repository. They need Python 3.13 or later with its venv module, git, and a Rust
toolchain with cargo 1.78 or later; no particular operating system or image is
assumed. Run them as a user other than root, since some of the tests check file
permissions, which root bypasses. The first block fetches the Python packages
(the four direct dependencies are pinned in `adapters/requirements.txt`; the
eight packages they pull in are not, so pip installs whatever versions PyPI
serves that day) and the Rust crates and needs network access;
the second runs without it.

```sh
python3 -m venv .venv
.venv/bin/pip install -r adapters/requirements.txt
cargo fetch --locked --manifest-path adapters/minitrace-verify/Cargo.toml
cargo fetch --locked --manifest-path adapters/jcs-oracle/Cargo.toml
```

```sh
cargo build --release --offline --locked --manifest-path adapters/minitrace-verify/Cargo.toml
cargo build --release --offline --locked --manifest-path adapters/jcs-oracle/Cargo.toml
cargo test --release --offline --locked --manifest-path adapters/minitrace-verify/Cargo.toml
cargo test --release --offline --locked --manifest-path adapters/jcs-oracle/Cargo.toml
.venv/bin/python adapters/run_fleet_sweep.py
```

The checks pass when every command exits 0. Each `cargo test` reports `0 failed`;
jcs-oracle has no unit tests of its own, and the sweep checks it against the
canonical vectors. The fleet sweep runs every regression rung, every test under
`adapters/tests/` (the canonical vectors among them), a lint and the coverage
declarations, and ends with a line beginning `fleet sweep: N/N pass`.

## Reference implementation

[`go-minitrace`](https://github.com/go-go-golems/go-minitrace), by Manuel
(go-go-golems), is an independent Go implementation of the v0.2.0 format and
the reference for a working storage and query architecture:

- canonical `.minitrace.json` as the portable interchange format;
- a SQLite working store for fast annotation mutation;
- a DuckDB analytical read layer over the working store; and
- parameterised SQL query commands for shipping analysis patterns across
  installations.

That storage split and the query-command pattern are **reference, not mandated**:
the format specifications do not require them. go-minitrace is a separate project
— its author is not a contributor to these specifications, and the
specifications do not depend on it. It is credited here as an independent
implementation and a worked reference for tooling around the format.

## Status

v0.3 is released. The four specifications are versioned independently (trace
v0.3.0, adapter DSL v0.1.0, envelope v0.1.1, scenario v0.1.0) and ship as a
coherent bundle.
