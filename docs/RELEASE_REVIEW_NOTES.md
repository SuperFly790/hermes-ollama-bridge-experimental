# Public release review notes — evidence and open gates

**Scope.** A standalone, locally bound research proxy derived from a Grok-bot-assembled v5 sandbox, then modified for a public review candidate. This is not a copy of Hermes core and is **not** the production bridge on the MSI.

## Verified in the current artifact

- Standalone `src/bridge.py` and complete `tests/test_bridge.py` are included.
- Offline unit suite: 19 PASS / 0 FAIL on Python 3.13.5 in the artifact preparation environment (2026-11-04). All HTTP traffic in the suite uses loopback mock upstream; no real Ollama or external API.
- `python -m compileall -q src tests` succeeded on Python 3.13.5.
- Static scan over included text files found no hard-coded known Windows username, private Telegram chat ID, personal email, recognizable API credential prefix or user-directory path. This is an intentionally scoped scan, not a proof that no secrets exist.
- The only RFC1918 address found is a hard-coded **synthetic negative test** (`192.168.0.12`) testing rejection of non-loopback binding.
- ZIP excludes `.git`, `__pycache__`, local operational data, old production bridge, all MSI runtime configurations and raw request logs.

## Claims requiring external audit

1. **Code origin / license.** Verify lawful right to redistribute the original user-supplied bridge, any third-party snippets, and Grok-bot-assembled patches under the proposed MIT license. Historical ownership and prompt attribution are reported, not independently established by git history.
2. **Safe startup and parser handling.** Review startup options, error paths, malformed JSON, request/body size limits, hostile tool schema payloads and local-only assertions.
3. **Tool policy.** Test both request/response enforcement routes, including streaming, mixed tool batches, `BRIDGE_ALLOWED_TOOLS`, `BRIDGE_TOOL_POLICY=observe`, `BRIDGE_ALLOW_NETWORK=1` and native API access.
4. **Data privacy.** Verify whether any JSONL log, DPAPI/plain spill, HTTP error, debug tracing or model output can reveal secrets; the no-tools-by-default policy cannot prevent a future client from transmitting private prompt contents.
5. **State and retries.** Identify the absence of durable side-effect journals and the risk of external agents replaying calls after interrupted responses. A name-based allowlist is not a sandbox.
6. **Context fidelity.** Bounded ID extraction in `build_summary` is an improvement over first-220-char-only behavior but is still **lossy** and cannot restore all events or semantics.
7. **SSE interoperability.** This source emits a custom error event and no successful `[DONE]` on upstream truncation; real OpenAI SDK and Hermes Gateway behavior are not verified.
8. **Context accounting.** Heuristics can overestimate or underestimate nonstandard data; reported previous Qwen metrics are from historical agent logs, not live tests of this changed code.
9. **Reproducibility.** On-device Grok build chain referenced an intermediate `build_sandbox.py` not contained in the saved ZIP. Therefore this release uses a full source snapshot instead of claiming an independently replayed v5 patch build.
10. **CI target.** CI lists Windows and Linux Python 3.12, but only the current preparation environment Python 3.13.5 offline run is recorded here. Do not assert CI has passed before GitHub Actions runs.

## Deployment boundary

The user explicitly disabled live automatic fallback on the MSI: `fallback_providers: []`, Ollama stopped, Hermes Gateway restarted successfully and DeepSeek + Telegram verified (reported 2026-11-04). The experimental code here must not change that state. A public release is for code review and manual local experimentation; no production-use endorsement.

## Outcome requested from Gemini

Produce prioritized P0/P1/P2 findings with exact file:line references, independent evaluation of the Grok bot build provenance and source licensing, precise code fixes (not just abstractions), publish / no-publish decision, and a minimal reproducible offline test plan. Do not reinterpret past canary as definitive evidence of model inability. All missing evidence must be named.
