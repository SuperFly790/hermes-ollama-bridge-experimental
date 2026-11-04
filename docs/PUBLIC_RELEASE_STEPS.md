# Direct public release procedure (no private GitHub repository)

**Stage 0:** Ask an independent reviewer to read the single-file audit bundle. Treat code as a *public candidate*, not an approved release.

**Release gates:** no P0 security issues or hardcoded secrets/PII, source-license provenance confirmed, offline mock CI passes, README limitations accurate, no claim of Hermes production support. An independent review does not replace a user decision to publish.

**Once approved:** create a new **Public** repository named `hermes-ollama-bridge-experimental` under your chosen GitHub account. Use an empty repo (no auto-created README/license, because they are in the archive). Extract the sanitized ZIP to an empty local folder, with no files from the Windows Hermes installation.

From that clean folder:

```powershell
py -3.12 -m unittest discover -s tests -v
git init -b main
git add .gitignore .github/workflows/ci.yml LICENSE README.md CHANGELOG.md CONTRIBUTING.md SECURITY.md pyproject.toml src/bridge.py src/__init__.py tests/test_bridge.py examples/bridge.env.example docs/*.md SHA256SUMS.txt
git diff --cached --stat
git diff --cached --check
git commit -m "Experimental public release v0.1.0-rc1"
git remote add origin https://github.com/OWNER/hermes-ollama-bridge-experimental.git
git push -u origin main
```

Replace `OWNER` with the GitHub account that owns the empty repository. Review the staged file list **before** committing. Never use `git add .` on the original Hermes folder. Creating a public repository exposes its entire Git history permanently; do not add sensitive files and then delete them later.

After publishing, inspect repository visibility, GitHub Actions status and public file tree. Do not enable automated fallback into the main agent.
