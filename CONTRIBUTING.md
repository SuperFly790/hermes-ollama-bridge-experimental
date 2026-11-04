# Contributing

Before submitting a change:

1. Run `python -m unittest discover -s tests -v` offline.
2. Keep the loopback/no-tools defaults fail-closed.
3. Provide a synthetic regression test; never include production requests, local credentials or raw logs.
4. Clearly distinguish model behavior from bridge transformations.
5. Do not represent this project as an official Hermes Agent component.

Changes affecting streaming, tool calls, budgeting or data persistence need explicit security review and a description of failure handling.
