<div align="center">

# Qazterion

**An autonomous AI coding agent for your terminal — plans, writes, tests, and commits code on its own.**

*A free, open-source alternative to Claude Code, Cursor CLI, and Aider — with built-in multi-provider key rotation across Google Gemini, Groq, Mistral, DeepSeek, and OpenRouter.*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Test Suite](https://github.com/Abdullah-qazi-1/qazterion-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Abdullah-qazi-1/qazterion-agent/actions)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#quick-start)

[Quick Start](#quick-start) • [Features](#key-highlights) • [Commands](#terminal-commands--shortcuts) • [Architecture](#architecture--codebase-layout) • [FAQ](#faq)

</div>

---

## What is Qazterion?

**Qazterion is an autonomous AI coding agent and CLI tool** — a terminal-based assistant that understands a plain-English task, plans it, edits your codebase, runs your tests, fixes what fails, and commits the verified result to Git, with little to no manual intervention. It runs directly in your terminal (VS Code Terminal, Windows PowerShell, Command Prompt, macOS/Linux Bash/Zsh) and is designed to feel like Claude Code, but open-source and provider-agnostic.

Unlike tools locked to a single AI provider, Qazterion is built around **automatic multi-provider key rotation and failover** — configure keys for Google Gemini, Groq, Mistral, DeepSeek, and OpenRouter (including multiple keys per provider), and Qazterion routes around rate limits and outages automatically.

```text
╭──────────────────────────────────────────────────────────────────────╮
│                                                                      │
│   ██████╗  █████╗ ███████╗████████╗███████╗██████╗ ██╗ ██████╗ ███╗   ██╗│
│  ██╔═══██╗██╔══██╗╚══███╔╝╚══██╔══╝██╔════╝██╔══██╗██║██╔═══██╗████╗  ██║│
│  ██║   ██║███████║  ███╔╝    ██║   █████╗  ██████╔╝██║██║   ██║██╔██╗ ██║│
│  ██║▄▄ ██║██╔══██║ ███╔╝     ██║   ██╔══╝  ██╔══██╗██║██║   ██║██║╚██╗██║│
│  ╚██████╔╝██║  ██║███████╗   ██║   ███████╗██║  ██║██║╚██████╔╝██║ ╚████║│
│   ╚══▀▀═╝ ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝│
│                                                                      │
│                    AI SOFTWARE ENGINEERING AGENT                     │
╰──────────────────────────────────────────────────────────────────────╯
  Qazterion v2.0.0
  Model      gemini-3.6-flash
  Directory  D:\Projects\my-app
  Git        main ✓
────────────────────────────────────────────────────────────────────────
```

---

## Why Qazterion?

| | Qazterion | Claude Code | Cursor CLI | Aider |
|---|---|---|---|---|
| Open source | ✅ | ❌ | ❌ | ✅ |
| Multi-provider (Gemini, Groq, Mistral, DeepSeek, OpenRouter) | ✅ | ❌ (Claude only) | ❌ | Partial |
| Multiple keys per provider + auto-failover | ✅ | ❌ | ❌ | ❌ |
| Automated Git checkpoints per task | ✅ | ✅ | Partial | ✅ |
| Self-repair loop on failing tests | ✅ | ✅ | ❌ | Partial |
| Hardware-level key encryption (DPAPI/Fernet) | ✅ | N/A | N/A | ❌ |
| Free & self-hosted | ✅ | ❌ | ❌ | ✅ |

---

- **Interactive REPL**: Claude Code-style terminal with command history, autocompletion, status trees, and syntax-highlighted diffs.
- **Autonomous Execution Loop**:
  `Understand` → `Plan` → `Permission Approval` → `Inspect` → `Act` → `Observe` → `Test` → `Self-Repair` → `Git Checkpoint`.
- **Multi-Provider Key Rotation & Failover**: Automatic failover across Google Gemini, Groq, Mistral, DeepSeek, and OpenRouter with physical key injection, concurrency limits, and immediate 429 rotation.
- **Instruction Hierarchy & Trust Boundaries**: Immutable system prompt at index 0; external tool results, repository reads, and summaries are sandboxed as untrusted user content.
- **Hardware-Level Encryption & Secret Isolation**: API keys encrypted at rest via Windows DPAPI (`CryptProtectData`) on Windows and Fernet on Unix; credentials accessed strictly in-memory and excluded from global `os.environ`.
- **Subprocess Environment Sanitization**: Child processes, host tools, and Git commands execute in sanitized environments stripped of sensitive host tokens.
- **SQLite Persistence & Telemetry**: Comprehensive persistent tracking of sessions, tasks, events, checkpoints, model calls, token consumption, and latency metrics in `%LOCALAPPDATA%\Qazterion\qazterion.db`.

---

## Quick Start

### 1. Installation

Clone the repository and install in editable mode:

```powershell
git clone https://github.com/Abdullah-qazi-1/qazterion-agent.git
cd qazterion-agent
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e .
```

> A published PyPI package (`pip install qazterion`) is on the roadmap — for now, install from source as shown above.

### 2. Configure API Keys

Add your provider keys to the encrypted keystore:

```powershell
# Add Google Gemini Key
qazterion /keys add gemini 1 AIzaSyYourGeminiKey...

# Add multiple keys for the same provider (auto-failover on rate limits)
qazterion /keys add gemini 2 AIzaSyYourSecondGeminiKey...

# Add Groq LPU Key (Optional)
qazterion /keys add groq 1 gsk_yourGroqKey...
```

Verify your keys and connectivity:

```powershell
qazterion /keys list
qazterion /keys test gemini 1
qazterion /status
```

### 3. Launch Interactive Session

```powershell
# In current folder
qazterion

# Or target a specific project workspace
qazterion --workspace "C:\Projects\my-app"
```

---

## Terminal Commands & Shortcuts

Inside the Qazterion interactive session or directly from terminal arguments:

| Command | Description |
|---|---|
| `/help` | Display manual of available commands and usage guidelines |
| `/status` | View provider routing matrix, model health, and telemetry stats |
| `/keys` \| `/model` | List, add (with masked input), remove, and test provider keys |
| `/diff` \| `/files` | Inspect uncommitted changes and unified colored diffs |
| `/rollback` \| `/undo` | View recent git checkpoints and revert changes safely |
| `/history` | Show list of completed autonomous tasks, duration, and token usage |
| `/rules` | View active security and workspace constraints (`.qazterion/rules.md`) |
| `/clear` | Clear terminal screen and redraw header banner |
| `/exit` \| `/quit` | Exit session gracefully |

---

## Autonomous Execution Workflow

When you type a prompt:

```text
You
› Build a FastAPI authentication system with JWT.

Qazterion
● Analyzing project...

  ├─ Reading project structure
  ├─ Inspecting requirements.txt
  ├─ Inspecting existing routes
  └─ Planning implementation

Qazterion
● Plan

  1. Create auth module
  2. Add User model
  3. Add JWT authentication
  4. Add login/register endpoints
  5. Add tests

  Proceed? [Y/n] › y

Qazterion
● Implementing...

  ✓ Created app/auth/models.py
  ✓ Created app/auth/security.py
  ✓ Created app/auth/routes.py
  ✓ Updated app/main.py

● Running tests...

  $ pytest

  ✓ 18 passed

────────────────────────────────────────────────────────────────────────
  ✓ Completed   4 files changed   18 tests passed
────────────────────────────────────────────────────────────────────────
```

---

## Architecture & Codebase Layout

```text
qazterion-agent/
├── qz_cli/                 # Terminal UI, Claude Code theme, banner, REPL, formatters
│   ├── app.py              # Main CLI entry point & prompt loop
│   ├── banner.py           # ASCII header banner & workspace card
│   ├── formatters.py       # Rich unified diff, plan tree & status tables
│   └── commands/           # Slash command handlers (/keys, /status, /diff, etc.)
├── qz_core/                # Pure autonomous core engine
│   ├── autonomous_loop.py  # End-to-end execution runner
│   ├── event_bus.py        # Central Pub/Sub event dispatcher
│   ├── planner.py          # Plan generation & DAG decomposition
│   ├── classifier.py       # Task complexity & mode analyzer
│   ├── dag_executor.py     # Hard DAG sequential node executor
│   ├── client.py           # Multi-provider LiteLLM client & key rotation
│   └── git_ops.py          # Atomic git checkpoints & rollbacks
├── qz_storage/             # Persistent SQLite database engine
│   └── db.py               # Sessions, tasks, events, checkpoints, telemetry
├── qz_security/             # Security sandbox, path traversal & rules engine
├── qz_indexer.py            # Incremental AST parser & symbol dependency graph
├── qz_context.py            # Hybrid retrieval (exact, symbol, semantic) & budgets
├── qz_keystore.py           # OS DPAPI hardware encryption for secrets
├── qz_repair.py              # Self-healing diagnostic repair loop
└── qz_tools.py               # File system, diffing, and subprocess tools
```

---

## Testing

Run the automated test suite across all 38 test modules:

```powershell
pytest -v
```

Full suite results: **306 passed, 1 skipped (Docker daemon detection), 0 failed**.

For detailed audit history, threat models, and architectural specifications, refer to [USER_GUIDE.md](USER_GUIDE.md) and [SECURITY.md](SECURITY.md).

---

## FAQ

**What is Qazterion?**
Qazterion is a free, open-source, autonomous AI coding agent that runs in your terminal. It plans, edits, tests, self-repairs, and commits code changes with minimal manual intervention — similar in spirit to Claude Code, but provider-agnostic.

**Is Qazterion free and open source?**
Yes. Qazterion is released under the MIT License and is free to self-host. You only pay for the underlying model API usage (Gemini, Groq, Mistral, DeepSeek, or OpenRouter).

**How is Qazterion different from Claude Code or Cursor?**
Qazterion is not locked to one AI provider. It supports Google Gemini, Groq, Mistral, DeepSeek, and OpenRouter simultaneously, with automatic key rotation and failover across multiple keys of the same provider — so a single rate limit never blocks your workflow.

**Can I use multiple API keys from the same provider (e.g. two Gemini keys)?**
Yes — Qazterion is built for this. Add as many keys as you want per provider family (`qazterion /keys add gemini 1 ...`, `qazterion /keys add gemini 2 ...`) and the router automatically load-balances and fails over between them.

**Does Qazterion work on Windows, macOS, and Linux?**
Yes. It's tested via CI on `windows-latest` and `ubuntu-latest` across Python 3.10–3.13.

**Does Qazterion store my API keys safely?**
Yes. Keys are encrypted at rest — via Windows DPAPI (`CryptProtectData`) on Windows, and Fernet encryption on macOS/Linux. Keys are never logged or printed in full.

---

## Contributing

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## Security

Found a vulnerability? Please see [SECURITY.md](SECURITY.md) for responsible disclosure instructions — do not open a public issue.

## Author & Maintainer

- **Author**: Abdullah
- **Email**: [abdullahizaq321@gmail.com](mailto:abdullahizaq321@gmail.com)
- **Repository**: [github.com/Abdullah-qazi-1/qazterion-agent](https://github.com/Abdullah-qazi-1/qazterion-agent)

## License

MIT License. See [LICENSE](LICENSE) for details.