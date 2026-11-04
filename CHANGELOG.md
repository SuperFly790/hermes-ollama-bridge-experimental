# Changelog

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
