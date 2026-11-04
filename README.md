# Experimental Ollama Bridge for OpenAI-Compatible Agents

**Status:** research prototype / **NOT production-ready**. Windows-only runtime, Python 3.12+. This repository is an independent experiment and is **not** an official Hermes Agent component or a maintained failover solution.

A small local HTTP gateway translates OpenAI-style `POST /v1/chat/completions` requests into Ollama `/api/chat` requests. It implements a best-effort token budget, tool-call filtering, and **lossy** middle-history summarization. It was created while studying whether a 9B local model can take over long autonomous-agent sessions. The observed answer was **no**: automatic fallback for long sessions is not endorsed.

## Safety defaults

- Binds **only to loopback**, default `127.0.0.1:11599`. It refuses a non-loopback HTTP upstream (default `http://127.0.0.1:11434`).
- **No tool names are allowed by default.** To experiment with read-only tools, opt in explicitly through `BRIDGE_ALLOWED_TOOLS`. This is only a *name-based* filter, not an OS sandbox or guarantee of privacy.
- `BRIDGE_TOOL_POLICY=enforce` by default. Never use `observe` with an untrusted agent.
- Limits JSON request bodies to 8 MiB by default. Full history spill files are **off** by default; upstream HTTP errors do not dump prompts. Diagnostic JSONL files can still contain metadata; keep them private.
- Does not support automatic safe replay of non-idempotent actions. An old agent may still retry independently.
- Streaming errors use a custom SSE error frame. Hermes v0.22.5 CLI has been tested against interrupted streams in an isolated environment; error attribution and end-of-turn behavior still have caveats. This is not a general OpenAI-client compatibility guarantee.

## Quick start (Windows, *separate test environment*)

Install Ollama and a model yourself; this project **never installs** or launches it automatically. Avoid running it on the same port as an active production agent.

```powershell
py -3.12 -m unittest discover -s tests -v
$env:BRIDGE_HOST="127.0.0.1"
$env:BRIDGE_PORT="11599"
$env:BRIDGE_UPSTREAM="http://127.0.0.1:11434"
$env:BRIDGE_NUM_CTX="16384"
$env:BRIDGE_ALLOWED_TOOLS=""
$env:BRIDGE_TOOL_POLICY="enforce"
$env:BRIDGE_SPILL_MODE="off"
py -3.12 src\bridge.py
```

A separate terminal can query `http://127.0.0.1:11599/health`. No real Ollama request is required to run the unit tests. **Do not configure this experimental service as an automatic fallback for a live agent.**

### Key environment variables

| Variable | Default | Notes |
| --- | --- | --- |
| `BRIDGE_HOST` | `127.0.0.1` | Loopback only |
| `BRIDGE_PORT` | `11599` | Avoid collisions with existing services |
| `BRIDGE_UPSTREAM` | `http://127.0.0.1:11434` | HTTP loopback only |
| `BRIDGE_NUM_CTX` | `16384` | Must match the upstream model's tested runtime context |
| `BRIDGE_ALLOWED_TOOLS` | *(none)* | Explicit opt-in; no tools by default |
| `BRIDGE_ALLOW_NETWORK` | `0` | Unsafe; network-capable names become eligible only when explicitly enabled |
| `BRIDGE_TOOL_POLICY` | `enforce` | `observe` is unsafe diagnostic mode |
| `BRIDGE_MAX_BODY_BYTES` | `8388608` | Reject oversized request before reading it |
| `BRIDGE_SPILL_MODE` | `off` | Do not store full prompts by default |
| `BRIDGE_LOG_DIR` | `src/logs` | Never commit logs |

See [Known limitations](docs/KNOWN_LIMITATIONS.md), [Architecture](docs/ARCHITECTURE.md), [Security](SECURITY.md), and [Public release steps](docs/PUBLIC_RELEASE_STEPS.md) before running.

## Tests

```powershell
py -3.12 -m unittest discover -s tests -v
```

The offline unit test suite covers safety defaults, oversized inputs, bounded summaries, context budgeting, SSE completion and truncation, forbidden tool calls, and concurrent JSONL logging. The offline unit tests use a **mock upstream** and cannot, by themselves, establish compatibility with Hermes Gateway or real Qwen. GitHub Actions successfully ran the rc.2 checks on Ubuntu and Windows with Python 3.12 and 3.13 (run [41825132154](https://github.com/SuperFly790/hermes-ollama-bridge-experimental/actions/runs/41825132154)). The operator also reported separate live MSI testing, summarized below.

## Subsequent on-device validation of rc.2 (operator-reported, 2026-11-04)

The Windows MSI test operator reported the following **isolated** results with the public rc.2 source at commit `b865567348f4f99cd81b6ab481f9f9be8e2fcb0e`:

- Real Ollama/Qwen 3.5:9B HTTP probe: 8/8 checks passed; separate synthetic-stress probe: 9/9 passed. No tools were allowed.
- Hermes v0.22.5 CLI via the bridge with `num_ctx=65536`: 30/30 dialog turns completed; three test identifiers were recalled after lossy history condensation. One real-model condensation took **196.6 seconds**; accuracy beyond those canaries is not established.
- With a synthetic failing upstream, the Hermes configuration `agent.api_max_retries=2` and `agent.auto_recovery_cycles=0` terminated refused-connection attempts itself and recovered after two HTTP 503 responses. This is a Hermes-side setting, **not** a bridge fix or proof of a universal deadline.
- SSE client abort, upstream interruption, and same-session resume were exercised in an isolated test profile, with caveats: an upstream interruption produced a misleading context-reset diagnostic; an interrupted user turn remained without a completed assistant response.
- A further compression experiment was **inconclusive (setup failure, no new measurements)** due to Windows file locks / session title collisions. Safe replay or exactly-once execution of state-changing tools remains **untested**.

These observations are based on reports from the MSI operator; the raw host logs and profile database remain private and are **not present in this repository**. They are not an independent replication, an audited production failover benchmark, or authorization to enable fallback.

## Observed research results (historical sandbox, not a benchmark of this revision)

Previous sandbox v5 was exercised on a compact AMD UMA computer with local 9B Qwen. The recorded single-probe run reported `prompt_eval_count=482`, `context_length=65536`, and `14.24 s` cold start. A long synthetic JSON input reported 15,851 prompt tokens and 91.55 seconds. A synthetic canary experiment failed to reproduce middle-history identifiers and one tail identifier. Later independent analysis identified **prompt-construction confounders**; a clean A/B test has not been run. Those historical sandbox observations precede the rc.2 on-device tests summarized above.

These numbers are **historical observations reported by the test agent**, not independently reproduced source artifacts and not a quality guarantee.

## Licensing and provenance

Distributed under the MIT License for this repository's code, subject to final provenance review. This project derives from a standalone, user-supplied prototype; it does **not** bundle Hermes Agent source code, production configuration, private logs, credentials, browser profiles or the unprotected legacy bridge. See [Provenance](docs/PROVENANCE.md) and [Grok bot / MSI build history](docs/GROK_AGENT_BUILD_HISTORY.md).

## Release policy

This repository is a **public-release candidate pending external review**. Passing the offline test suite is not a substitute for review of source origin, data handling, SSE client behavior, model-memory reliability, and runtime hardening. **Do not deploy it as an automatic failover.**

For independent publication review, see [Release review notes](docs/RELEASE_REVIEW_NOTES.md).
