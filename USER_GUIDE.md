# Qazterion — User & Engineering Guide

> **Notice:** This document provides a complete guide and technical reference for using, configuring, and extending the **Qazterion Autonomous Coding Agent CLI**.

---

## 1. System Overview

**Qazterion** is an open-source, terminal-based autonomous AI coding agent designed for software engineers. It operates directly inside your terminal (VS Code Terminal, PowerShell, CMD, Bash, Zsh) to autonomously plan, read, edit, execute, test, and checkpoint complex codebases.

---

## 2. Interactive Terminal Commands

Run `qazterion` to enter the interactive REPL. The following slash commands are available at any time:

### `/keys` | `/model` — API Key & Model Management
- **List Keys**: `/keys list` (shows configured providers, masked secrets, and encryption backend)
- **Add Key**: `/keys add <provider> [index] [key]`
  - Example: `/keys add gemini 1 AIzaSy...`
  - Example (Multiple Keys): `/keys add gemini 2 AIzaSySecondKey...`
- **Delete Key**: `/keys delete <provider> [index]`
- **Test Connectivity**: `/keys test <provider> [index]`

### `/status` — Router Matrix & Health
- Displays the live provider status table, discovered models, DPAPI encryption details, token counters, and latency telemetry.

### `/diff` | `/files` — Code Diff Inspector
- Shows colored git diff of all modifications made during the active session (`+` green additions, `-` red deletions).

### `/rollback` | `/undo` — Checkpoint Restoration
- Lists recent git checkpoints created by Qazterion.
- Rollback: `/rollback <checkpoint_id>` reverts the workspace safely to that exact state.

### `/history` — Task History & Metrics
- Displays previous autonomous tasks, status (COMPLETED/FAILED), duration, and total token usage.

### `/rules` — Project Rules
- Displays active security and coding guidelines loaded from `.qazterion/rules.md`.

### `/clear` — Clear Terminal
- Clears the terminal screen and redraws the Qazterion header banner.

### `/exit` | `/quit` — Exit CLI
- Gracefully terminates the session.

---

## 3. Autonomous Execution Pipeline

When given a prompt, Qazterion executes through a 9-stage autonomous loop:

1. **Understand & Classify (`qz_core/classifier.py`)**: Analyzes task intent and categorizes it (`quick`, `standard`, `complex`).
2. **Context Retrieval (`qz_context.py` & `qz_indexer.py`)**: Gathers relevant AST symbol definitions, imports, and file chunks while respecting model context token budgets.
3. **Plan & Decompose (`qz_core/planner.py`)**: Generates an actionable step-by-step breakdown.
4. **Approval Gate**: Renders the proposed plan and prompts user (`Proceed? [Y/n]`).
5. **Inspect & Edit (`qz_core/dag_executor.py`)**: Applies targeted diff patches or file creations using unified diff algorithms.
6. **Execute & Test (`qz_tools.py`)**: Runs unit test suites (`pytest`, `unittest`, `cargo test`, `npm test`).
7. **Self-Repair Loop (`qz_repair.py`)**: Extracts test tracebacks, classifies failures, and attempts anti-loop diagnostic fixes.
8. **Checkpoint Creation (`qz_core/git_ops.py`)**: Creates an atomic Git commit checkpoint for all verified changes.
9. **Complete & Telemetry (`qz_storage/db.py`)**: Records latency, token consumption, and success metrics to SQLite.

---

## 4. Multi-Key Rotation & Auto-Failover

Qazterion provides resilient multi-tier fallback:
- **Key Rotation**: Multiple keys under the same provider family (e.g. `GEMINI_KEY_1`, `GEMINI_KEY_2`, `GROQ_KEY_1`, `GROQ_KEY_2`) are auto-rotated. If one hits a rate limit (`429`), Qazterion automatically switches to the next available key.
- **Provider Cascade**: If all keys of a provider are exhausted, the engine falls back to the next available configured provider.
- **Message Turn Sanitization**: Ensures multi-turn tool calling strictly conforms to provider API message sequencing rules.

---

## 5. Security & Isolation

- **Workspace Boundary Guard (`qz_security/workspace_guard.py`)**: Enforces canonical path resolution preventing traversal (`../`) outside the target directory.
- **Command Risk Classifier (`qz_security/command_risk.py`)**: Categorizes commands into risk tiers and blocks destructive system calls.
- **Secret Redactor (`qz_security/secret_scanner.py`)**: Automatically scans and masks secrets and API keys from terminal output and logs.
- **DPAPI Keystore (`qz_keystore.py`)**: API keys are hardware-encrypted on disk and only decrypted in memory during model invocation.
