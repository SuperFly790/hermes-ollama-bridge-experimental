# Security policy

**This is experimental code, not a supported production integration.**

Never expose the HTTP server outside loopback, never attach privileged tools by default, and never put user data or API keys into public issues. It does not authenticate local callers. `BRIDGE_TOOL_POLICY=observe` and `BRIDGE_ALLOW_NETWORK=1` are unsafe diagnostic settings.

If you discover a vulnerability, **do not publish a weaponized reproduction containing private data or instructions for accessing someone else's files**. Use the repository's GitHub *Report a vulnerability* / private security advisory channel if enabled; otherwise contact the repository owner via GitHub before creating a public issue. For ordinary bugs, provide a minimal, synthetic local test case.

No support or vulnerability response SLA is guaranteed.
