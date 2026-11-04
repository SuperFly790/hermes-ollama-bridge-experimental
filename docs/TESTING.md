# Test scope

## Offline verification

Command: `python -m unittest discover -s tests -v`

The tests run without connecting to Ollama, DeepSeek, Gemini or Hermes. They cover:

- Loopback binding and upstream restriction
- Empty tool allowlist by default and named filtering
- Generation/token reserve validation
- Oversized tool schema budget and noncompressible-message margin
- Middle-history structured-identifier extraction and explicit lossiness
- Synthetic local responses and usage
- SSE terminal `[DONE]` exactly once on success
- SSE upstream EOF without `[DONE]` on truncation
- Forbidden tool calls filtered from SSE
- Concurrent writes producing parseable JSONL records
- Request byte limit before read and rejection of wrong JSON shapes

## Evidence boundaries

These unit tests are **not** model-evaluation tests, not a security audit and not proof of correct client integration. Earlier real-Ollama measurements were made on a different sandbox revision and cannot be attributed to this public candidate. The old `ollama_real_gate_v4.py` is intentionally excluded due to unsynchronized revisions and test defects.

Required before any production consideration: clean A/B recall study, tests against a real Ollama instance with a known model digest, test for client handling of partial SSE, input fuzzing, and an application-level action replay policy.

## Gemini audit follow-up (2026-11-04)

Regression coverage includes nested JSON shape rejection, fixed num_ctx in native and observe/non-native paths, exactly-once safety margin, handoff notice and last user preservation, and reasoning token estimates. CI tests both Python 3.12 and 3.13 on Windows and Ubuntu.
