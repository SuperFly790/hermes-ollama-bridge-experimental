# Changelog

## On-device validation documentation update (2026-11-04)

- Document subsequently reported rc.2 Windows MSI Ollama/Qwen integration tests without publishing private logs or local configuration.
- Clarify that Hermes v0.22.5 has a configurable, finite auto-recovery ladder, and that synthetic-upstream `2/0` tests do not constitute a bridge code change.
- Record 196.6-second history compression, SSE diagnostic caveats, Stage 2 compression setup failures, and untested side-effect idempotency.
- Documentation only; no new binary release or promotion to production.

## 0.1.0-rc.2 — Gemini audit follow-up (2026-11-04)

- Reject malformed nested `tool_calls`, `tools.function` and `options` input with HTTP 400.
- Enforce bridge-side `num_ctx` even in unsafe observe/non-native compatibility mode.
- Fix the double-counted context safety margin on non-compressible history.
- Preserve handoff system notice and last user turn during emergency trimming.
- Include reasoning and legacy `function_call` fields in first-pass token estimates.
- Limit GitHub Actions token permissions and test Python 3.12/3.13 on Windows and Ubuntu.
- Ignore `.dpapi`, `.dmp`, and `handoff_*` diagnostics; add regression tests.
- At the time of this code commit, the rc.2 source had not yet been live-tested. Later operator-reported MSI tests are recorded in the public README; production automatic fallback remains disabled.


## 0.1.0-rc.1 — public review candidate (2026-11-04)

- Extracted a standalone bridge from a private v5 sandbox, without production configuration or raw logs.
- Changed tool allowlist to *none by default* and default HTTP binding port to `11599`.
- Added loopback-only upstream validation and request size/shape guards.
- Isolated the Windows named mutex from the production bridge and prevented automatic prompt dumps on upstream HTTP errors.
- Added a local HTTP round-trip test using a mock upstream (no Ollama process).
- Made JSONL log writes thread-safe and added request-ID header for generated SSE policy response.
- Removed duplicate usage dictionary fields; added synthetic zero-token usage to local blocked-repeat JSON responses.
- Counted `role`, `name`, `tool_call_id`, `reasoning_content` and legacy `function_call` fields in the conservative message estimate.
- Applied the configured safety factor to non-compressible system and last-user estimates.
- Added a bounded structured-ID extraction to lossy summaries and neutralized one handoff instruction that could confound recall testing.
- Added offline unit tests and public documentation.
- **No new live Ollama testing** was performed for this revision. Not for production.

- Documented the role and limitations of the Grok bot/on-device assembly, ChatGPT release adaptations, and Gemini review, without treating reported builds as independent proof.
