# Contributing to Qazterion

Thank you for your interest in contributing to Qazterion! We welcome contributions from the community to make Qazterion the best autonomous coding CLI.

---

## Development Setup

### Prerequisites
* **Python 3.10+**
* **Git**
* **Docker Desktop** *(Optional, for sandbox isolation)*

### Getting Started

1. **Clone the repository:**
   ```powershell
   git clone https://github.com/Abdullah-qazi-1/qazterion-agent.git
   cd qazterion-agent
   ```

2. **Set up the Python virtual environment:**
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -e .
   pip install pytest pytest-cov
   ```

3. **Run the CLI in development mode:**
   ```powershell
   qazterion
   ```

---

## Testing & Quality Assurance

Before submitting a pull request, ensure the full suite passes (this mirrors the CI workflow in `.github/workflows/`):

```powershell
pytest -v
```

---

## Code Architecture Guidelines

* **Pure Decoupled Engine:** Keep core agent loop, tool execution, and storage completely decoupled from presentation logic.
* **Non-Leaking Credentials:** Never log, print, or commit API keys or sensitive user data. Always use `mask_key()`, `qz_security.redact()`, and ensure secrets are never stored in global `os.environ`.
* **Instruction Hierarchy Integrity:** Never promote dynamic model outputs or tool results to system messages. System prompt must remain immutable at message index 0.
* **Subprocess Sanitization:** All child process invocations (Git, host tools, sandboxes) must use `sanitize_subprocess_env(workspace)` to prevent credential leakage.
* **Resilient Parsing:** Codebase indexers, prompt parsers, and command runners must gracefully handle malformed files, invalid JSON, and non-zero exit codes.
* **Cross-Platform Compatibility:** Ensure shell commands and path handling work consistently across Windows PowerShell, CMD, macOS, and Linux.

---

## Pull Request Checklist

* [ ] `pytest -v` passes locally (matches the `ubuntu-latest` and `windows-latest` CI matrix)
* [ ] No API keys, tokens, or secrets committed (see [SECURITY.md](SECURITY.md))
* [ ] New behavior has a corresponding test
* [ ] Public functions/classes have clear docstrings
* [ ] `CHANGELOG.md` updated for user-facing changes

## Reporting Issues

Found a bug or want to request a feature? Please [open an issue](https://github.com/Abdullah-qazi-1/qazterion-agent/issues) using the provided templates. For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.