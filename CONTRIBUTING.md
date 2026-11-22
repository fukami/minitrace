# Contributing to minitrace

minitrace is an early-stage research format. Contributions are welcome.

## Ways to contribute

**New adapters.** If you use an agent harness that has no adapter here, writing one is the most valuable contribution. An adapter is a declarative file in the adapter DSL ([`spec/minitrace-adapter-dsl-spec-v0.1.0.md`](spec/minitrace-adapter-dsl-spec-v0.1.0.md)), plus an optional Python preprocessor (`preprocess_<harness>.py`) for native quirks the DSL cannot express. The existing adapters under `adapters/<harness>/` show the pattern, and the [adapter-authoring guide](docs/adapter-authoring-guide.md) covers the process from the native-format deep dive to validation.

**Regression rungs.** An adapter is pinned to the harness versions it was checked against by regression rungs under `adapters/<harness>/regression/<version>/`: a captured input, a `manifest.json` freezing the expected content hashes and coverage, and a `verify.py` that re-runs the adapter. A rung for a harness version not yet covered, captured from a sanitized session, is a direct contribution to what the format is known to handle.

**Format changes.** When a harness updates its native session format, an adapter may break silently. If you notice a format change, report it with a sanitized sample session. Each adapter's `format-reference/` directory documents the native format it was written against.

**Specification review.** The four specifications under `spec/` (trace, adapter DSL, envelope, scenario) are released. Ambiguities, contradictions and cases the text does not decide are best reported with the section number and a minimal example.

**Security review.** The adapters parse untrusted session data. Inputs that crash a preprocessor or the DSL runner, or that make a trace say something the input did not, are worth reporting.

## Guidelines

- Python 3.13+, the oldest version on which the test suite passes. The adapter tooling depends on the packages in [`adapters/requirements.txt`](adapters/requirements.txt); install them into a virtual environment.
- An adapter's output must validate against the base schema and its type-specific schema in `schemas/`; `adapters/verify_run.py <adapter.dsl.yaml> <input> [<preprocess.py>|-] --operator-type <type>` runs the full pipeline and verifies the result; it exits 0 only when the conversion is complete (or with `--allow-incomplete`, which still reports it). A human operator also needs `--operator-aware`, `--operator-capture-mode full` and the three options `--operator-capture-purpose-legal-basis`, `--operator-capture-purpose-reference` and `--operator-capture-purpose-authorised-at`; a delegated operator needs `--operator-capture-mode full` (see `--help`). The reference runner implements only the capture mode `full` of the four the adapter DSL lists (§7.1) and refuses the others, since it performs no pseudonymisation, scrubbing or reference-only transformation.
- Before submitting, run the fleet sweep, `python adapters/run_fleet_sweep.py`, with the Rust tools built (`cargo build --release` in `adapters/minitrace-verify/` and `adapters/jcs-oracle/`). It re-runs every regression rung and every test in `adapters/tests/`.
- A change to an adapter that moves a rung's content hash is re-frozen in the same change, with the reason recorded in the rung's `manifest.json`.

## Reporting issues

Open a [GitHub issue](https://github.com/fukami/minitrace/issues). Include:

- Harness name and version
- What you expected vs what happened
- Sample session data (sanitized, no API keys, personal paths, or credentials)

## Acknowledgements

minitrace was developed as part of research into human-AI coding interaction patterns at the intersection of conformity assessment, AI safety, and operational practice.

**[CrabNebula](https://crabnebula.dev).** For supporting this research and providing feedback throughout development.

**[FIRST AI Security SIG](https://www.first.org/global/sigs/ai-security/).** Inspiration from the intersection of AI security and incident response practice.

**[Gadi Evron's Raptor](https://github.com/gadievron/raptor).** The pattern miniraptor builds on.

**[The Assistant Axis](https://arxiv.org/abs/2601.10387).** The observation that models maintain a "helpful assistant" persona along a specific activation direction informed the over-autonomy/excessive-deference failure axis in the minitrace taxonomy.

**Research context.** The failure taxonomy draws on [MAST](https://arxiv.org/abs/2503.13657) for multi-agent failure categories, [MITRE ATLAS](https://atlas.mitre.org/) for adversarial technique mapping, [ToolEmu](https://github.com/ryoungj/toolemu) for safety evaluation framing, and [OWASP LLM Top 10](https://genai.owasp.org/) (2025) for threat-to-evidence mapping.

**Frameworks.** The adapter collection exists because these tools store session data in accessible formats. Thanks to the teams behind Claude Code/claude.ai (Anthropic), Codex (OpenAI), Goose (Block), Pi (Mario Zechner), OpenCode, Droid (Factory), Gemini CLI (Google), Cursor (Anysphere), Aider, and Vibe (Mistral).

**Tools.** [DuckDB](https://duckdb.org/) for making JSON queryable with SQL. [Ollama](https://ollama.com/) for enabling same-model cross-framework comparison.

**[Anthropic](https://www.anthropic.com/).** minitrace was developed inside miniraptor, an adversarial review framework running on Claude Code. The spec, adapters and failure taxonomy were written in human-AI collaboration sessions that are themselves minitrace-capturable.
