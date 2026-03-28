# Contributing to minitrace

minitrace is an early-stage research format. Contributions are welcome.

## Ways to contribute

**New adapters.** If you use an AI framework not listed here, writing an adapter is the most valuable contribution. See the existing adapters for the pattern: import from [`minitrace_common.py`](adapters/minitrace_common.py), use the builder functions. Agent adapters (CLI tools with local session stores) should implement `--discover` mode. Web export adapters (data export ZIPs) follow a different pattern -- see `adapters/chatgpt/` or `adapters/claude-ai/` for examples. The [adapter guide](docs/adapter-guide.md) covers the full process.

**Format discovery.** When frameworks update their native session format, adapters may break silently. If you notice a format change, report it with a sample (sanitized) session file. See [format-discovery.md](docs/format-discovery.md) for the current documented formats.

**Scenario definitions.** New scenarios that test interesting behavioral dimensions (multi-file edits, error recovery, ambiguous instructions) are welcome as YAML files in [`scenarios/definitions/`](scenarios/definitions/). The [experiment guide](docs/experiment-guide.md) covers scenario design.

**DuckDB queries.** Useful analytical queries belong in [`queries/`](queries/).

**Security review.** The adapters parse untrusted session data. See the [threat model](docs/threat-model.md) for the review checklist.

## Guidelines

- Python 3.9+, stdlib only (no external dependencies)
- Each adapter is a standalone script, not a library
- Agent adapters: use `--discover` mode to document native formats before writing conversion logic
- Web export adapters: document the export format structure in the adapter docstring
- Run `python3 adapters/validate-minitrace.py` on your output before submitting
- Populate v0.2.0 fields (`input_channel`, `content_origin`, `platform_type`) where the source format provides sufficient signal

## Reporting issues

Open a [GitHub issue](https://github.com/fukami/minitrace/issues). Include:

- Framework name and version
- What you expected vs what happened
- Sample session data (sanitized, no API keys, personal paths, or credentials)

## Acknowledgements

minitrace was developed as part of research into human-AI coding interaction patterns at the intersection of conformity assessment, AI safety, and operational practice.

**[CrabNebula](https://crabnebula.dev/).** For supporting this research and providing feedback throughout development.

**[Gadi Evron](https://github.com/gadievron/raptor), [Prompt || GTFO](https://www.knostic.ai/blog/prompt-gtfo-season-1), and [[un]prompted](https://unpromptedcon.org/).** Gadi's [Raptor](https://github.com/gadievron/raptor) is the pattern miniraptor builds on. The Prompt || GTFO and [un]prompted community shaped how this project approaches real-world agent failures.

**[FIRST AI Security SIG](https://www.first.org/global/sigs/ai-security/).** Inspiration from the intersection of AI security and incident response practice.

**[The Assistant Axis](https://arxiv.org/abs/2601.10387).** The observation that models maintain a "helpful assistant" persona along a specific activation direction informed the over-autonomy/excessive-deference failure axis in the minitrace taxonomy.

**Research context.** The failure taxonomy draws on [MAST](https://arxiv.org/abs/2503.13657) for multi-agent failure categories, [MITRE ATLAS](https://atlas.mitre.org/) for adversarial technique mapping, [ToolEmu](https://github.com/ryoungj/toolemu) for safety evaluation framing, and [OWASP LLM Top 10](https://genai.owasp.org/) (2025) for threat-to-evidence mapping.

**Frameworks.** The adapter collection exists because these tools store session data in accessible formats. Thanks to the teams behind Claude Code (Anthropic), Codex (OpenAI), Goose (Block), Pi (Mario Zechner), OpenCode, Droid (Factory), Gemini CLI (Google), Vibe (Mistral), OpenClaw (AbanteAI), ChatGPT (OpenAI), and claude.ai (Anthropic).

**Tools.** [DuckDB](https://duckdb.org/) for making JSON queryable with SQL. [Ollama](https://ollama.com/) for enabling same-model cross-framework comparison.

**[Anthropic](https://www.anthropic.com/).** minitrace was developed inside miniraptor, an adversarial review framework running on Claude Code. The spec, adapters, failure taxonomy, and analysis tooling were written in human-AI collaboration sessions that are themselves minitrace-capturable.
