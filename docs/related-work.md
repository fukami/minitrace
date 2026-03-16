# Related Work

minitrace is not a benchmark. It is a capture format that enables behavioral research on human-AI interaction, and a practice documentation format for operational session archival. This page summarizes how it relates to existing datasets, formats, and threat taxonomies.

## Comparison table

| Dataset/Format | What it provides | What minitrace adds |
|----------------|------------------|---------------------|
| [MAST](https://arxiv.org/abs/2503.13657) | Failure taxonomy, multi-agent traces | Reproducible scenario specs, human context, handover capture |
| [MITRE ATLAS](https://atlas.mitre.org/) | Adversarial technique knowledge base for AI systems | Evidence format. minitrace sessions capture technique execution traces, not just technique descriptions. Tool call sequences map to ATLAS techniques. |
| [ToolEmu](https://github.com/ryoungj/toolemu) | Synthetic scenarios, safety focus | Real operational traces, session-level capture, coordination |
| [OWASP LLM Top 10](https://genai.owasp.org/) (2025) | Threat taxonomy for LLM applications | Evidence format. minitrace sessions provide structured evidence for threats like LLM06 (Excessive Agency) and LLM09 (Misinformation). Failure codes map to OWASP threats. |
| [SWE-bench](https://www.swebench.com/) | Task definitions, code problems | Full interaction capture, not just outcomes |
| [HumanEval](https://github.com/openai/human-eval) | Code evaluation | Tool usage patterns, failure annotation |
| Native agent transcripts | Raw session data (JSONL, SQLite, JSON, OTEL spans) | Structured schema, cross-framework comparison, classification, annotation layer |

## MITRE ATLAS mapping

[ATLAS](https://atlas.mitre.org/) catalogues adversarial techniques against AI systems. minitrace sessions capture the observable traces of these techniques in coding agent contexts.

| ATLAS Technique | minitrace Evidence |
|-----------------|-------------------|
| AML.T0054 LLM Jailbreak | `Turn.source` distinguishing injected vs human content, prompt content in turns |
| AML.T0051 LLM Prompt Injection | `source: "framework"` turns containing injected instructions, tool call sequences following injection |
| AML.T0048 Exfiltration via AI | Tool calls with `operation_type: "EXECUTE"` or `"READ"`, `framework_metadata` capturing egress |
| AML.T0043 Craft Adversarial Data | Scenario definitions with `deception` field, session annotations flagging manipulated inputs |

## OWASP LLM Top 10 mapping

[OWASP LLM Top 10](https://genai.owasp.org/) describes what can go wrong. minitrace captures when and how it happens in practice.

| OWASP Threat | minitrace Evidence |
|--------------|-------------------|
| LLM06 Excessive Agency | F-AUT (over-autonomy), F-SCO (scope-creep), behavioral audit tags |
| LLM09 Misinformation | F-HAL (hallucination), F-STA (knowledge-stale), F-ASM (unverified-assumption) |
| LLM02 Sensitive Information Disclosure | `classification` field, `contains_pii` flag, path sanitization |
| LLM01 Prompt Injection | `source` field on turns distinguishes human input from framework-injected content |

These mappings are illustrative, not exhaustive. As operational observation data grows, more techniques and threats will have minitrace evidence patterns.
