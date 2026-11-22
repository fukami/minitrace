# Envelope examples

`manifest-minimal.json` and `manifest-bindings-witness.json` illustrate the manifest format of `minitrace-envelope-spec`. The minimal manifest follows the worked example in the specification's Appendix A; the second adds binding records, vendor-extension witness kinds and a manipulated clock. Both declare `envelope_version` 0.1.0, and a v0.1.1 consumer reads them under the 0.1.0 rules (§12); they are not conformant v0.1.1 envelopes.

They are manifests, not complete envelopes. The trace, raw-artefact and witness files they list are not included, and most of their hashes are placeholders (`sha256:bbbb…`, `sha256:1111…` and similar). Both validate against the manifest schema, which `schemas/validate-envelope-scenario.py` checks, but `adapters/verify_envelope.py` reports them as failing: verification under §6.2 recomputes every listed hash over the listed files, and here the files are absent and the hashes are not real.

`signature.json` shows the shape of the detached signature file (§9). Its algorithm and signature values are placeholders, and it is not a signature over either manifest.

For a complete envelope that does verify, see `examples/envelope-pi/`.
