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
   git clone https://github.com/qazterion/qazterion.git
   cd qazterion
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

Before submitting a pull request, ensure all tests pass:

```powershell
pytest test_qz_cli_and_loop.py -v
```

---

## Code Architecture Guidelines

* **Pure Decoupled Engine:** Keep core agent loop, tool execution, and storage completely decoupled from presentation logic.
* **Non-Leaking Credentials:** Never log, print, or commit API keys or sensitive user data. Always use `mask_key()` and `qz_security.redact()`.
* **Resilient Parsing:** Codebase indexers, prompt parsers, and command runners must gracefully handle malformed files, invalid JSON, and non-zero exit codes.
* **Cross-Platform Compatibility:** Ensure shell commands and path handling work consistently across Windows PowerShell, CMD, macOS, and Linux.
