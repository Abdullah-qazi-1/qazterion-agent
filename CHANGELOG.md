# Changelog

All notable changes to **Qazterion** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] — 2026-09-04 (Autonomous Coding CLI Release)

### Added
* **Claude Code-Style Terminal CLI Interface (`qz_cli/`):**
  * Interactive REPL with prompt-toolkit, multiline input, autocompletion, and command history.
  * ASCII header banner with live workspace path, active git branch, and model router status.
  * In-session slash commands: `/keys`, `/status`, `/diff`, `/rollback`, `/history`, `/rules`, `/clear`, `/help`, `/exit`.
  * Visual unified diff renderer with additions/deletions syntax highlighting.
  * Interactive plan approval and security permission gates.
* **Pure Decoupled Autonomous Core Engine (`qz_core/`):**
  * Multi-step autonomous execution loop: Understand $\rightarrow$ Plan $\rightarrow$ Approve $\rightarrow$ Inspect $\rightarrow$ Act $\rightarrow$ Observe $\rightarrow$ Test $\rightarrow$ Self-Repair $\rightarrow$ Checkpoint.
  * Real-time Pub/Sub Event Bus (`qz_core/event_bus.py`).
  * Strict multi-turn function call turn ordering and message sanitization (`sanitize_messages_for_llm`).
  * Direct LiteLLM and multi-provider failover routing with auto-rotation across multiple keys per family (`GEMINI_KEY_1`, `GEMINI_KEY_2`, etc.).
* **SQLite Persistent Storage (`qz_storage/`):**
  * Persistent tracking of sessions, tasks, events, checkpoints, model invocations, and telemetry in `qazterion.db`.
* **Packaging & Universal Scripts:**
  * Added `pyproject.toml` and `setup.py` registering `qazterion` and `qz` global CLI commands.
  * Universal cross-platform compatibility across Windows PowerShell, CMD, macOS, and Linux.

---

## [1.0.0] — 2026-09-02 (Foundation Release)

### Added
* Incremental multi-language AST symbol and dependency indexer (`qz_indexer.py`).
* Hybrid context retriever and token budget manager (`qz_context.py`).
* Diagnostic failure classifier and anti-looping repair loop (`qz_repair.py`).
* Encrypted OS Keystore using Windows DPAPI with multi-key pooling (`qz_keystore.py`).
* Tool gateway with workspace path containment, command risk classifier, and secret redaction (`qz_security/`).
* Objective validation gate for tests, lint, typecheck, build, and security (`qz_validation/`).
