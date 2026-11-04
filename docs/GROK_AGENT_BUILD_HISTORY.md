# Grok bot / on-device build history (historical provenance)

> This page documents the role of a Grok-controlled agent on the MSI development host. It is not a claim that Grok authored every line of this repository, that any model is an independently authenticated contributor, or that the historical build is identical to the public release candidate.

## Who did what

- **Project owner:** commissioned a local DeepSeek-to-Ollama fallback for Hermes, set safety boundaries and approved limited tests. The owner later authorized shutdown of the live fallback and requested preparation of a publicly shareable research release.
- **Grok bot / on-device engineering agent:** according to the owner's contemporaneous reports, operated on the MSI host, prepared the local sandbox v5 build, generated patch artifacts, ran offline and live validation, collected results, and performed the operational rollback/disablement of fallback. *Its exact prompting, internal delegation and machine command transcript were not supplied for independent verification.*
- **ChatGPT:** reviewed the reports, provided safety constraints, generated independent audit briefs, and prepared **this separate public-release candidate** from the saved v5 code, with additional defensive modifications and standalone offline tests.
- **Gemini:** performed external review of audit materials; its findings were used as hypotheses for verification, not automatically treated as proof. The final public candidate still requires independent review.

## On-device build sequence reported for v5 (2026-11-03 to 2026-11-04)

1. A pre-existing 821-line legacy bridge was present in the local Hermes working directory; a byte-preserving baseline was recorded (SHA-256 prefix `3a5d7ee2`). Production bridge was **not** overwritten during tests.
2. Earlier patch iterations led to a v4 sandbox (reported digest prefix `9575c2b8`). The v5 builder, `build_sandbox_v5.py`, imported a local `build_sandbox.py` and used `v5_spec.py` to apply the additional changes. The v5 output included `sandbox_v5/bridge.py`, `bridge_budget_toolpolicy_v5.patch` against the baseline, and `supplemental_v5.patch` against v4.
3. The on-device agent reported **18/18** offline bridge checks, **24** gate self-checks and **13/13** offline gate tests. These numbers come from historical reports; they are not the test results of this public release.
4. After explicit permission, the agent started local Ollama and probed the Qwen3.5:9B model. An isolated live gate recorded a short-query success and failures of history recall after lossy condensation. The owner's independent Gemini audit later found that the canary test design and the source/results synchronization were confounded, so a broad "model alone is responsible" conclusion is **not established**.
5. The owner approved removal of the risk from the running system. On the development host, `fallback_providers` became `[]`; the gateway was restarted, the primary DeepSeek and Telegram were checked, Ollama stopped, and the legacy bridge remained untouched. This repository does **not** reactivate fallback.

## Public release is a different build

This repository contains an extracted, independently edited bridge under `src/bridge.py` and self-contained tests under `tests/test_bridge.py`. It **does not include** the original Grok/on-device patch chain, the original 821-line bridge, on-device gateway code, logs, API keys, personal configurations or operational state. The local builder needed intermediate files missing from the audit archive; the public candidate therefore ships *a full source snapshot*, not a non-reproducible patch series.

Changes to the release candidate (as opposed to the Grok/on-device build): empty-by-default tool allowlist, isolated test port and Windows mutex, bounded HTTP body and local upstream checks, thread-safe JSONL writes, neutralized condensation notice, limited middle-history identifier extraction, additional budget accounting, no full-prompt dumping on HTTP error, and a new offline mock test suite.

## Verification levels

| Evidence | What can be checked here | What cannot be concluded |
| --- | --- | --- |
| Owner's operational reports | Historical milestones and numbers | Exact identity of the program issuing each change |
| Included current source and tests | Actual implementation and mock test behavior | That code was committed or executed verbatim on the MSI |
| Historical SHA-256 prefixes in reports | Documented version labels | Independent byte-level verification without original files |
| External review summaries | Review findings, including disputed canary explanation | Certification of security or model reliability |

## Attribution and licensing gate

The owner must confirm rights to distribute all original/custom code and any code copied from third-party projects before issuing the public release. The labels "built by Grok bot" and "prepared with ChatGPT" describe development assistance, not a copyright assignment, a blanket license grant, or an endorsement by xAI, OpenAI, Gemini or NousResearch. See `docs/PROVENANCE.md` and `LICENSE`.
