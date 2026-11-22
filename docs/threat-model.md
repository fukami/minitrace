# minitrace threat model (v0.3.0)

> **Status: non-normative.** The normative security and privacy text is in
> `minitrace-spec` v0.3.0 §14 and §15, and in `minitrace-envelope-spec`
> v0.1.1 §5.2, §9 and §11. This document states what the release treats
> as hostile and as trusted, what its checks establish, and what the
> shipped tooling assumes. It describes the tooling as released; where a
> limitation is listed as known, it is not yet fixed.

## Scope

The release consists of four specifications (trace format, adapter DSL,
envelope, scenario) and tooling in three groups:

- **Converters.** Per-harness preprocessors (Python), declarative adapters
  written in the adapter DSL (YAML), and the DSL runner with its shared
  source reader, which turn native session stores and exports into traces.
- **Verifiers.** The JSON Schemas with `schemas/validate.py`, the Python
  envelope verifier, the pipeline driver `adapters/verify_run.py`, the Rust
  verifier `minitrace-verify`, and the Rust canonicalisation reference
  `jcs-oracle`.
- **Helpers.** Key-name redaction for copied configuration, timestamp
  normalisation, cross-trace hashing.

Compared with v0.2.0, the parsing surface has grown. v0.2.0 adapters were
Python scripts only. In v0.3.0 an adapter is a DSL file that the runner
loads as input, usually paired with a preprocessor, and the envelope adds a
bundle format with its own manifest, witness and signature files.

## What is hostile and what is trusted

| Input | Treated as | Reason |
|---|---|---|
| Native session stores and exports | Hostile | Their content comes from models, tools, web pages and users, none of which the analyst controls. |
| Traces received from others | Hostile content; claims trusted only as far as the producer is | Every string field is untrusted (trace §14.1), and the format cannot detect a fabricating or omitting producer (trace §14.6). |
| Envelopes received from others | Same as traces | Manifest hashes give integrity over what the producer bundled; authenticity is out of their reach (envelope §5.2, §11.2). |
| Adapter DSL files | Input to the runner | The runner loads them with a YAML safe loader and evaluates only the functions the DSL specification defines; an unknown function, root or construct rejects the adapter before any input is read (DSL §8.3, §9.1). An adapter still decides what the trace says, so its output is only as trustworthy as its author. |
| Preprocessors | Code | A preprocessor is a Python program. Running one is running its author's code, with the analyst's permissions. |
| The analyst, the local file system, the output directory | Trusted | The tooling runs as the analyst and does not defend against them. |

## Properties of the converter boundary

The trust boundary is where native data is read. At this release:

- **No network access and no execution of content.** None of the shipped
  Python modules opens a network connection, and none calls `eval`, `exec`,
  `os.system` or a shell. Tool commands and file paths in session data are
  stored as data. Subprocesses are started only by the pipeline drivers,
  the regression scripts and the tests, which run the preprocessor, the
  runner and the Rust verifier as fixed commands.
- **Source data is read, never changed.** SQLite session stores are copied
  into a private temporary directory, and the copy is opened read-only for
  queries and with `trusted_schema=OFF`, so views and triggers in a hostile
  database cannot call functions with side effects. A store that changes
  while it is copied is refused.
- **Symlinks.** A file found by walking an input directory is read only if
  no path component below the walked root is a symlink and the file
  resolves inside the root; skipped files are reported. A path the analyst
  names on the command line is read as given. Hard links are not detected:
  a regular file in the tree that is a hard link to a file stored elsewhere
  on the same file system passes these checks and is read as part of the
  input, so whoever can create entries in the input tree can have the
  converter read any file they can link to. Files with more than one link
  are not refused, because legitimate trees contain them, for example a git
  clone made with hard links.
- **Bounds.** The shared source reader stops a file larger than 512 MiB or
  with more than 5,000,000 records, skips and reports a line longer than
  32 MiB, and truncates nesting at the depth limit of trace §3.4 (64),
  reporting each truncation. The claude-ai, aider, cursor and agentdojo
  preprocessors read their input whole but apply the same 512 MiB limit:
  the first three refuse a larger file, and agentdojo skips it with a
  warning. SQLite stores are refused before they are
  copied if the store and its write-ahead log together exceed 512 MiB, or
  if the store path is a symlink, FIFO or device; a store is also refused
  if a table the adapter reads is a view, a virtual or shadow table, or has
  a generated column, or if a string or blob it reads exceeds 32 MiB. A
  refusal by table shape applies to the whole store, however small, so a
  harness version that introduces such a table needs an adapter update
  before its stores convert. The
  preprocessors report a refusal as an error and write no output. The
  goose and opencode regression drift tools run the preprocessor and
  report its refusal as a failed adapter run; they resolve the store path
  first, so they read a store reached through a symlink instead of
  refusing it. The bounds are set generously and exist to stop unbounded
  memory growth and query cost on a hostile or corrupt file.
- **Corruption is reported.** The shared source reader replaces bytes that
  are not valid UTF-8 with U+FFFD, reports each replacement and keeps the
  record (DSL §4). A line that is not JSON after that is skipped and
  recorded as a finding; the conversion continues.
- **Output.** The runner refuses to write over anything at its output path
  that is not a regular file or a symlink; an earlier file there, or a link
  to anything other than a directory, is removed, and such a link is not
  followed. A link to a directory is followed: the runner takes the path as
  directory output and writes into the directory the link names, and the
  link stays. In directory output the runner
  names a file after its trace id only if the id is 1 to 200 characters of
  `[A-Za-z0-9._-]`, does not start with a dot and is not a Windows device
  name; a trace whose id fails that check is not written. The pipeline
  driver names its per-trace files by position.

**Known limitations at this release.**

- Four preprocessors for benchmark and observability sources
  (inspect-swe, mcpuniverse, minimcp, otel-openinference) parse their
  primary JSON input with Python's own parser rather than the shared
  source reader, so they do not read numbers as DSL §4 requires of a
  pre-processor, and the 512 MiB limit under **Bounds** does not apply to
  them. On hostile input they can end with a Python traceback: minimcp
  does on an integer of more than 4,300 digits, which Python's parser
  refuses. The shared reader reads such a number, like any number outside
  the numeric domain of trace §3.4, as a string holding its source text,
  and skips nothing for it. These four preprocessors also do not install
  the output sanitiser of `adapters/input_tree.py` that the other
  preprocessors use, so what they print from their input reaches the
  terminal unescaped, as with the Python validators below: mcpuniverse
  prints an input path holding an escape sequence as it is.
- The agentdojo preprocessor ends with a Python traceback on a run file
  that is valid JSON of another shape, such as one whose `messages` member
  is a string or whose top level is an array; `adapters/verify_run.py`
  reports the run failed, with exit status 1.
- `adapters/verify_run.py` always runs `adapters/dsl_runner.py` with
  `--coverage`, and building the coverage report ends the runner with a
  Python traceback when an input record's `type` member is a non-empty
  object or array. The runner has written the traces by then, but no
  report, and `adapters/verify_run.py` reports the run failed, with exit
  status 1. Run without `--coverage`, `adapters/dsl_runner.py` converts
  the same input with exit status 0. This was measured on the pi adapter,
  which has no preprocessor. The claude-code preprocessor turns a `type`
  that is not a string into an unknown event, and
  `adapters/verify_run.py` reports the same input complete.
- A lone surrogate written as a JSON escape (`\ud800`) ends several
  preprocessors with a Python traceback, where DSL §4 requires a
  pre-processor to replace it with U+FFFD and report it, as the shared
  reader does: droid in its settings sidecar, vibe in `meta.json`,
  mcpuniverse in its trace file and in the fields of `raw/output.json`
  and `run-record.json` that it carries into its output (measured on the
  question, the run id and the model; a field it does not carry, such as
  the seed, leaves the outcome unchanged), and the otel-openinference
  (halo) preprocessor in its span file. The aider, codex and cursor
  preprocessors read a `--framework-meta` file the same way, which
  matters where the session leaves a field empty that the file fills.
- The agentdojo preprocessor decodes its input files with bytes that are
  not valid UTF-8 replaced by U+FFFD, but does not report the
  replacement, where DSL §4 requires a pre-processor to report it as the
  shared reader does (**Corruption is reported** above). The conversion
  ends with exit status 0 and no warning.
- The agentdojo preprocessor reads a zoneless `evaluation_timestamp` as
  UTC, although AgentDojo writes it as the local time of the evaluating
  host without a zone. Every timestamp of an agentdojo trace is that one
  value (`timing.started_at` and the timestamps of its annotations and
  policy triggers), so all of them are off by the evaluating host's UTC
  offset, which the run file does not record. A run file without the
  member, as some AgentDojo runs are, or with the member null or blank,
  converts with `1970-01-01T00:00:00.000000Z` in those fields and no
  warning. The preprocessor's intermediate output records the absence
  (`event_ts_present` false), but the trace does not, and
  `adapters/verify_run.py` reports such a run complete.
- The aider preprocessor attributes roles and edits by line pattern in
  aider's Markdown chat history, where the harness writes model output
  verbatim: a line starting with `####` opens a user turn, and a line
  starting with `> Applied edit to` records an edit to the named file.
  Model output containing such a line produces a user turn or an edit that
  did not happen, and `adapters/verify_run.py` still reports the run as
  complete. Honest sessions lose and gain user turns as well. Each
  `####` line is its own user turn, so a prompt of three lines becomes
  three user turns (measured on the shipped s2-shell input), and a
  confirmation answer, where aider writes one as a `####` line, becomes
  a user turn. A reply that leaves a thinking block or a code fence
  open, or that closes its thinking block with `</think>` rather than
  `</thinking-content-…>`, hides the prompts that follow: a thinking
  block runs to the next `</thinking-content-…>` line outside a code
  fence, and an open fence shifts the pairing of every later fence. In
  measured histories of two and three prompts, one or all of the later
  prompts were lost. `adapters/verify_run.py` reports each of these runs
  complete, with exit status 0. Role and operation attribution in aider
  traces is therefore heuristic, in honest sessions as well as against
  hostile model output.
- The Python validators echo offending values in their error messages
  without a length bound: a 200 KB invalid value produces about 200 KB of
  output. They do not sanitise the values they print, so terminal control
  sequences are escaped only where the schema library happens to print a
  value with `repr()`. Consumers that display trace content follow trace
  §14.1 regardless.
- No shipped tool checks the v0.2.x minimum fields that trace §3.1
  requires of an agent-session trace by profile. Beyond the base schema,
  the agent-session schema requires only `trace_type` and `turns`, and
  neither the validators nor the verifiers test the per-profile fields, so
  a trace that passes `schemas/validate.py` can lack them. `classification`,
  required in both profiles, is one case: the base schema admits it as
  null or absent with no `remove` redaction recorded in `redactions[]`,
  although it holds any other value to the four values of trace §6.5.
- No shipped validator or verifier checks that a timestamp is
  calendar-valid, as trace §3.5 requires. `schemas/validate.py`, and
  through it the schema checks of `adapters/verify_run.py` and
  `adapters/verify_envelope.py`, test the timestamp's form only: a trace
  with a turn timestamped on 30 February, at hour 25 or at second 60
  passes. `adapters/verify_envelope.py` also accepts such values in a
  manifest's `created_at` and a witness's `captured_at`.
- No shipped tool checks a redaction record's target against trace §8.2.
  The base schema types `target` as any non-empty string, and
  `schemas/validate.py` accepts a `field` target naming `/profile`,
  `/turns`, an item of `/turns`, or a member §8.2 protects such as
  `/turns/0/timestamp`. `adapters/dsl_runner.py` writes the redaction
  records an adapter emits without checking their targets.
- The codex and cursor preprocessors record a shell command's operation
  type from its first word, and for git from its subcommand, unless the
  command is compound: `find . -delete`, `find . -exec rm {} +` and
  `git diff --ext-diff` are recorded as READ, although the first two remove
  files and the third runs a program the git configuration names.
  Operation types in these traces are heuristic.
- `adapters/verify_run.py` reads a missing fidelity record as a
  preprocessor that keeps none. When a preprocessor that keeps one leaves
  none beside its output, the skipped records and read findings it would
  have counted are not in the loss accounting, and the run can still be
  reported complete.
- The runner refuses the operator capture modes `pseudonymised`,
  `scrubbed` and `reference_only` and exits 1, because it does not perform
  their transformations. Only `full` converts, or no mode where the
  operator type allows that.
- `provenance.converter_version` carries a label written by hand in each
  adapter file, and the label is not changed with every change to the
  adapter or its preprocessor: codex's has stayed the same through four
  commits to its adapter file since it was last set, one of which changed
  its regression output.
  Two traces with the same value can come from converters that behave
  differently.
- Some preprocessors read a value of the wrong type as absent and report
  nothing. codex reads a tool name that is not a string as `unknown`, and
  a structured tool output (an object, not a string) as the empty string;
  `adapters/verify_run.py` reports both runs complete. gemini-cli reads a
  `messages` member that is not an array as no messages, without a
  warning; the run then fails on the schema for an empty `turns`, not on
  the input. codex in session mode also skips, without a warning, a
  `response_item` whose payload type is not `message`, `reasoning`,
  `function_call` or `function_call_output`.
- The cursor preprocessor records a shell command as failed only when its
  `exitCode` is a non-zero integer, and then writes the command's stderr,
  or `exit code N` when stderr is empty, as the error; the exit status
  itself is not kept. The stderr of a command that succeeded is not kept. A
  command with no exit code (null or absent, as for one killed by a signal)
  is recorded as a success, and so is one whose `exitCode` is not an
  integer (`true`, `1.0`, `"1"`). `adapters/verify_run.py` reports these
  runs complete, without a warning. A shell tool result whose `success`
  is null, the empty string or an empty object is recorded as a success
  with an empty result, without a warning, and the run is reported
  complete; one whose `success` is any other value that is not an
  object, such as `false`, `true`, a number, a string or a list, is
  recorded as a success with that value as its result text, with a
  warning, and the run is reported incomplete.
- The goose preprocessor records a tool result of the form
  `{"status": "error", "error": "..."}` as a success with an empty result,
  so the error text does not reach that tool call's output, nothing in
  the trace records that the call failed, and `adapters/verify_run.py`
  reports the run complete. A tool result whose value carries
  `isError: true` is recorded as a failure. Of the four goose inputs
  shipped with the regression tests, one holds a tool result of the
  first form; captures beyond those four were not measured.
- The cursor, claude-ai and agentdojo preprocessors parse their input
  with a helper that keeps the last of two members of one object with the
  same name and does not report the other, where DSL §4 requires a
  pre-processor to report it as the shared reader does. A cursor run that
  loses a duplicate member this way is reported complete.
- The minimcp and otel-openinference (halo) preprocessors end
  with a Python traceback and exit status 1 when their input file does
  not exist; the other preprocessors exit with an error line, cursor's
  with status 66.
- `adapters/verify_run.py` drops the validator's warning for a trace whose
  `trace_type` is not one the schemas define: such a trace is validated
  against the base schema only and its `schema_uri` is not fetched, and the
  run prints `schema=ok` without saying so. `schemas/validate.py` prints
  the warning. `adapters/verify_envelope.py` notes it for a bundled trace
  and counts the trace schema check as unsupported, but that check is not
  a step of envelope §6.2, so the verdict and the exit status are the ones
  the envelope would have without it: an otherwise intact unsigned
  envelope is reported as integrity only, with exit status 0.
- The minimcp adapter declares its traces as `trace_type`
  `ext:minitrace:bridge-session`, with a `schema_uri` naming a
  bridge-session schema under `https://minitrace.dev/schemas/` that this
  release does not ship. Trace §3.2 requires a trace of an unknown type to
  validate against the schema at its `schema_uri`; the shipped validators
  do not fetch it and check a minimcp trace against the base schema only,
  so its conformance cannot be established from the release.
- `adapters/verify_envelope.py` notes an `x-*` witness whose
  `metadata.capture_status` is other than `captured`, a record envelope
  §7.6.1 says an assembler must not emit, but does not count it as a
  problem: the exit status is the one the envelope would have without it.
- `adapters/verify_envelope.py` ends its text report on an envelope whose
  verdict is informational only, with exit status 3, with the line
  `ok: 0 problem(s)` that also ends the report on an envelope that passed;
  the verdict line and the exit status say the envelope is not evidence,
  the last line does not.
- Not every write error of the runner ends the conversion the way an
  error writing to an output path does. Written to standard output, a
  trace that cannot be written, for example into a closed pipe, ends the
  conversion with a Python traceback and exit status 120. When the input
  is unreadable and the report of it cannot be written, the runner exits
  1 with an error line, not with the unreadable-input status 4.

## Trace ids

Most adapters derive a trace's `id` from an identifier in the source data,
such as a harness session id, which trace §3.3 permits. A hostile source can
therefore give its trace the id of another trace. Ids are not credentials
(trace §3.3), and a duplicate id in one working set is a load error, so the
effect is a collision, which the consumer rejects when it loads the traces.

An id taken from the source also links every trace derived from the same
session. The rule that keeps identifying members out of redaction's
reach (trace §8.2, §15.1) covers events and annotations, not the trace
`id`, so it does not bar a `field` redaction of the id; unless one is
applied, the id stays.

## What verification establishes, and what it does not

| Check | Establishes | Does not establish |
|---|---|---|
| Schema validation | The trace has the shape the schemas describe. | Conformance: the schemas check a subset of the requirements (trace §13). |
| `content_hash`, recomputed by the consumer | The canonical form is the one that was hashed. | Who produced the trace, when, or whether its content is true; a producer who changes a trace and recomputes the hash leaves no trace of it (trace §14.3). A hash not recomputed by the consumer establishes nothing. |
| Lineage hashes, recomputed by the consumer over the referenced trace it holds | That trace is in the state the edge names (trace §7.1). | When the edge was written, or when the referenced trace was in that state: the edge records a claimed snapshot. A hash the consumer cannot recompute against a trace it holds establishes nothing. That the referenced trace is still in that state (trace §15.3). |
| Envelope manifest hashes | The listed files are the bytes the manifest names. | Authenticity, or that witnesses belong to the session the trace describes (envelope §5.2). |
| Envelope signatures | Nothing at this release. | v0.1.1 profiles no signature algorithm (envelope §9.2), and the shipped envelope verifier reports a present signature as not verified. |
| A converter run that reports no loss | The converter detected no loss of what it read. | That the source was complete, or that the session happened as recorded. |

Two consequences follow. First, an envelope's hashes establish integrity
against careless edits and nothing about authenticity. Second, an envelope
has no revision model: an envelope that later receives new evidence has no
defined way to record the change, and any change to its manifest breaks
the hashes and signatures that covered the earlier state (envelope §9.3).

Annotation signatures are in the same position: `author_signature` is an
opaque string at this release and is not proof of authorship (trace §14.4).

## Redaction is not sanitisation

Redaction in the trace format (§8) records that the producer removed or
replaced content, and why. It does not make the remaining content safe to
render or process, which is the consumer's job under trace §14.1, and it does
not find personal data: the absence of `pii_detected` redactions means only
that the producer did not detect or did not redact any (trace §15.1).

Redaction also leaves information behind. The members that identify and
order events and annotations are never redacted, so a redacted trace still
shows when each event happened and what each identifier is (trace §15.1). An
`original_content_hash` on a redaction record lets anyone with a candidate
plaintext confirm a guess, which matters for short or predictable content
(trace §14.5). Redaction records are final; an error in one is corrected by
producing a new trace from the unredacted source (trace §14.5).

The tooling's own redaction is narrower still. Where an adapter copies a
harness's configuration into a trace, values under keys that name a secret
(`token`, `apiKey`, `client_secret`, `Authorization` and similar) are
replaced. Secrets under other key names, and secrets inside free text such
as prompts, tool output and command lines, are not found. A trace is
reviewed before it is shared.

## Resource and file-system assumptions

- The tools run locally, as the analyst, on a file system the analyst
  controls. Nothing is sandboxed; a deployment that converts hostile stores
  at scale runs the converters in an isolated environment of its own.
- SQLite stores are converted while their harness is stopped. A store that
  changes during the copy is refused.
- Input files do not change during a run. The shared source reader refuses
  a file whose bytes differ between hashing and parsing.
- The default bounds above suit a workstation. Larger inputs need explicit
  limits, and smaller environments need lower ones.

## If signatures arrive

A future revision that profiles an envelope signature algorithm (envelope
§9.2) would let a consumer with the right key establish that a named key
holder assembled a given manifest. It would not establish that the bundled
traces and witnesses are true, which stays out of scope (envelope §11.2),
and redaction after signing would still require re-signing or one of the
patterns of envelope §9.3. A profile for annotation signatures (trace §14.4)
would do the same for individual annotations.

## Reporting security issues

Report security issues through the issue tracker of the repository this
release is published from.
