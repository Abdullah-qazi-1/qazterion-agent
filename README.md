# Qazterion

**Qazterion** is a production-grade, cross-platform autonomous AI coding agent and CLI tool (Claude Code style) designed for professional software engineering. It operates directly in your terminal (VS Code Terminal, Windows PowerShell, Command Prompt, macOS/Linux Bash/Zsh) with natural-language interactive tasks, multi-provider model routing, permission gates, incremental AST retrieval, sandboxed execution, and automated Git checkpoints.

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

## Key Highlights

- **Interactive REPL**: Claude Code-style terminal with command history, autocompletion, status trees, and syntax-highlighted diffs.
- **Autonomous Execution Loop**:
  `Understand` $\rightarrow$ `Plan` $\rightarrow$ `Permission Approval` $\rightarrow$ `Inspect` $\rightarrow$ `Act` $\rightarrow$ `Observe` $\rightarrow$ `Test` $\rightarrow$ `Self-Repair` $\rightarrow$ `Git Checkpoint`.
- **Multi-Provider Key Rotation & Failover**: Automatic failover across Google Gemini, Groq, Mistral, DeepSeek, and OpenRouter with round-robin support for multiple keys of the same provider family.
- **Hardware-Level Encryption**: API keys encrypted at rest via Windows DPAPI (`CryptProtectData`) on Windows and Fernet on Unix.
- **SQLite Persistence & Telemetry**: Comprehensive persistent tracking of sessions, tasks, events, checkpoints, model calls, token consumption, and latency metrics in `%LOCALAPPDATA%\Qazterion\qazterion.db`.

---

## Quick Start

### 1. Installation

Clone the repository and install in editable mode:

```powershell
git clone https://github.com/your-username/Qazterion.git
cd Qazterion
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -e .
```

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
Qazterion/
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
├── qz_security/            # Security sandbox, path traversal & rules engine
├── qz_indexer.py           # Incremental AST parser & symbol dependency graph
├── qz_context.py           # Hybrid retrieval (exact, symbol, semantic) & budgets
├── qz_keystore.py          # OS DPAPI hardware encryption for secrets
├── qz_repair.py            # Self-healing diagnostic repair loop
└── qz_tools.py             # File system, diffing, and subprocess tools
```

---

## Testing

Run the automated test suite:

```powershell
pytest test_qz_cli_and_loop.py -v
```

---

## Author & Maintainer

- **Author**: Abdullah
- **Email**: [abdullahizaq321@gmail.com](mailto:abdullahizaq321@gmail.com)

---

## License

MIT License. See `LICENSE` for details.
