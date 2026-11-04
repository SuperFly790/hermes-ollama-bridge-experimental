# Source provenance and publication boundaries

The code in this release candidate was adapted from a **standalone user-provided bridge prototype** created for local experiments on Windows. The input bundle contained a 821-line baseline bridge, a 1511-line sandbox v5 and a mix of local reports and diagnostic scripts. Only a cleaned, modified copy of the sandbox bridge is published here.

The MSI sandbox and its iterative patch assembly and testing were operated by a **Grok bot** according to the owner. The current GitHub-ready candidate was subsequently adapted and tested by **ChatGPT** in a separate environment, with **Gemini** used for independent critique. See [Grok bot build history](GROK_AGENT_BUILD_HISTORY.md) for the exact reported workflow and verification limits.

This repository deliberately excludes:

- The legacy baseline, whose tool policy and budgeting were insufficient.
- Hermes Agent upstream repository source, installation files and persistent `state.db`.
- Working `config.yaml`, API keys, model-provider secrets, machine-specific paths and Telegram identifiers.
- Raw request/response dumps, browser profiles, full histories, spill files, and operational logs.
- Build scripts requiring missing private intermediate versions and old live-gate scripts with unsynchronized test artifacts.

**Changes made for the release candidate:** default no-tools mode, isolated non-production default port, loopback upstream validation, HTTP body-size limit, malformed request rejection, bounded identifier preservation in lossy summaries, neutralized handoff wording, logging mutex, expanded message-field budget estimates, synthetic usage for locally generated responses, and offline regression tests.

Authorship and rights to any external snippets, if any, **must be independently confirmed before an actual public push**. The MIT license applies to code that contributors have the right to license. We do not assert that a security review or upstream endorsement has occurred.
