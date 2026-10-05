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
  Qazterion v2.3.0
  Coder      gemini/gemini-flash-latest (+4 fallback)
  Accounts   4 key(s) across gemini, groq, openrouter
  Directory  D:\Projects\my-app
  Git        main
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
- **Multi-Provider, Multi-Account Routing**: The agent asks for a *role* (coder, planner, fast, reasoner, classify); an in-process gateway picks a provider, model and API key, rotates keys on rate limits, disables rejected keys, benches retired models, and fails over to other providers. No proxy server, no Docker.
- **Instruction Hierarchy & Trust Boundaries**: Immutable system prompt at index 0; external tool results, repository reads, and summaries are sandboxed as untrusted user content.
- **Hardware-Level Encryption & Secret Isolation**: API keys encrypted at rest via Windows DPAPI (`CryptProtectData`) on Windows and Fernet on Unix; credentials accessed strictly in-memory and excluded from global `os.environ`.
- **Subprocess Environment Sanitization**: Child processes, host tools, and Git commands execute in sanitized environments stripped of sensitive host tokens.
- **Local Persistence**: Tasks, events and checkpoints in one SQLite file; usage in a small JSONL log; key/model health in a JSON file — all under `%LOCALAPPDATA%\Qazterion\`, never inside your project.

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

Either store keys in the encrypted keystore (the key is typed at a hidden prompt, never on the command line):

```powershell
qazterion /keys add gemini        # stored as GEMINI_KEY_1
qazterion /keys add gemini        # a second account: GEMINI_KEY_2
qazterion /keys add groq
```

…or put them in a `.env` file (see `.env.example`): `GEMINI_KEY_1=...`, `GEMINI_KEY_2=...`, `GROQ_KEY_1=...`.

Check them (lists models only — costs no tokens):

```powershell
qazterion /keys test
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
| `/keys` | `list`, `add <provider>` (hidden input), `delete`/`enable`/`disable <provider> <n>`, `test`, `reset` |
| `/model` | Show roles → models; `/model <role> <provider/model>` to prefer a model; `/model strategy balanced\|priority` |
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

## Providers, Keys & Routing

All provider configuration lives in one YAML catalog: built-in defaults in
`qz_providers/default_providers.yaml`, overridable in `%LOCALAPPDATA%\Qazterion\providers.yaml`.

```yaml
providers:
  gemini:
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    key_prefix: GEMINI_KEY            # keys: GEMINI_KEY_1, GEMINI_KEY_2, ...
    models:
      gemini-flash-latest: {context_window: 1048576, tools: true}
roles:
  coder: [gemini/gemini-flash-latest, groq/llama-3.3-70b-versatile, mistral/codestral-latest]
```

* **Adding a provider** = adding a `providers:` entry with an OpenAI-compatible `base_url` (or `/keys` + the desktop app's "custom provider"). No code changes.
* **Per request**, the gateway walks the role's models in order, skipping disabled providers, models without tool support (when tools are needed), models with no key, and models/keys that are cooling down.
* **Keys rotate**: `balanced` (default) spreads requests across a provider's healthy keys; `priority` sticks to the first healthy one.
* **Failures are classified once** and drive the next step: invalid/expired key → key disabled until replaced; rate limit / quota → that key cools down *for that model* (Retry-After honoured); retired model → benched for 6 h; timeouts/5xx → another key, then the next model; everything cooling briefly → wait up to 30 s.
* Health state is shared between the CLI and the desktop app through `provider_health.json`, so a failing key is not retried by the next process.

---

## Architecture & Codebase Layout

```text
qazterion-agent/
├── qz_cli/                  # Terminal UI: REPL, banner, formatters, slash commands
├── qz_core/
│   ├── autonomous_loop.py   # Task pipeline: env → plan → approval → DAG → execute
│   ├── planner.py           # Plan, architecture, subtask DAG
│   ├── classifier.py        # Task mode / complexity
│   ├── dag_executor.py      # Per-subtask tool loop, validation gate, repair
│   ├── executor.py          # request_completion(), system prompt, test baseline
│   └── client.py            # OpenAI-style facade over the provider gateway
├── qz_providers/            # Multi-provider access
│   ├── default_providers.yaml  # Providers, models, roles (single source of truth)
│   ├── catalog.py           # Loads/merges/edits the catalog
│   ├── keys.py              # Keys per provider (keystore + environment)
│   ├── health.py            # Key / model cooldowns (persisted)
│   ├── gateway.py           # Role → provider/model/key selection with failover
│   └── adapters/            # OpenAI-compatible + OpenRouter wire protocols
├── qz_tasks/                # SQLite task/event/checkpoint store
├── qz_security/             # Command risk, path guard, approvals, secret redaction
├── qz_sandbox/              # Host command execution (scrubbed env, timeouts)
├── qz_validation/           # Tests/lint/typecheck/build/security gate
├── qz_recovery/             # Resume and safe checkpoint rollback
├── qz_keystore.py           # DPAPI (Windows) / Fernet encrypted key storage
├── qz_tools.py              # File, patch, command and git tools for the agent
└── qz_desktop_bridge.py     # JSON-RPC backend for the desktop app
```

---

## Testing

```powershell
pip install -e ".[dev]"
pytest
```

The suite is hermetic: it never reads your real keys or calls real providers (provider HTTP is
simulated with a local server and scripted adapters). Docker is not used or required.

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
Yes — Qazterion is built for this. Run `qazterion /keys add gemini` once per account (or set `GEMINI_KEY_1`, `GEMINI_KEY_2`, … in `.env`); requests rotate across healthy keys and skip ones that are rate limited or rejected.

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