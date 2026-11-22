# A complete envelope: pi 0.58.1

This directory is a complete `minitrace-envelope-spec` v0.1.1 envelope that verifies under §6.2:

```sh
python adapters/verify_envelope.py examples/envelope-pi/manifest.json
```

It reports every check passed and the verdict "integrity only": the envelope is unsigned, so producer authenticity is not established (§5.2).

The envelope bundles one session and the inputs it was converted from. `raw/pi/s1-config.jsonl` is the native pi 0.58.1 session log of the pi regression rung (`adapters/pi/regression/0.58.1/inputs/s1-config.jsonl`, the same bytes), listed as the `harness_log` that produces the trace. `raw/adapter/pi-0.58.1.dsl.yaml` is the adapter that converted it, listed as an `x-adapter-dsl` raw artefact (§10.3). `traces/` holds the trace that `adapters/dsl_runner.py` produced from those two files, run from this directory with `--operator-type scripted`, with its `content_hash` stamped by `adapters/cross_trace.py`.

The one witness is synthetic. Its kind is `x-synthetic-example` and its payload says that it records no observation; it is there so that the witness checks of §6.2 (the payload against `path_hashes`, the record against its own `content_hash`) have something to verify.

Converting the raw input again gives a different `provenance.converted_at`, and therefore a different trace `content_hash` and file hash; the manifest records the conversion that is bundled here.
