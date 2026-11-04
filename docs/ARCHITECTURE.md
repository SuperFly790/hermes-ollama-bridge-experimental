# Architecture

```text
OpenAI-compatible local client
    |
    | POST /v1/chat/completions
    v
Loopback-only bridge (default 127.0.0.1:11599)
    |-- validate JSON shape and request size
    |-- enforce declared tool-name allowlist (empty by default)
    |-- budget messages, schemas, output reserve (heuristic)
    |-- optionally condense *middle* of long history (lossy)
    |-- repair orphaned tool-call/result pairs
    |-- convert to Ollama /api/chat (num_ctx, num_predict)
    v
Local Ollama (default 127.0.0.1:11434)
    |
    | response / SSE
    v
Bridge filters completed tool calls before forwarding
```

- **Source:** `src/bridge.py` is a single-file experimental server. Python standard library only; Windows-specific single-instance and DPAPI support; the experimental mutex is isolated from the legacy service.
- **Budget:** `plan_budget()` accounts for allowed tool schemas, conservative message estimates, chat-template margin and generation reserve. This is a heuristic, not an exact tokenizer.
- **Policy:** `filter_tools()` drops unspecified schema names before Ollama; `apply_tool_policy()`/`stream_filter_calls()` reject tools returned by Ollama that are not in the same explicit allowlist. Whole mixed batches are rejected (fail-closed).
- **Streaming:** `_stream_native()` buffers tool calls until upstream `done: true`; missing completion is represented by a non-standard SSE error frame without `[DONE]`.
- **Handoff:** `condense()` preserves system messages and a bounded recent tail, adds a visibly incomplete summary of earlier messages, and may shrink large results. It is not a trustworthy substitute for structured memory.
- **Persistence:** JSONL diagnostic logs use a write lock. Prompt spill is disabled by default and Windows DPAPI is available only as opt-in.

Out of scope: network exposure, multi-user auth, autonomous side-effect safety, guaranteed long-context fidelity, and compatibility certification with any specific agent.
