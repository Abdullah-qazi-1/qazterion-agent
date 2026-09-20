# Changelog

All notable changes to **Qazterion** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.2.0] — 2026-09-18 (Part 2 Independent Audit Remediation & Hardening)

### Security & Instruction Hierarchy
* **Instruction Hierarchy Integrity (AQ-02):** Demoted conversation history rollups to `role: "user"` wrapped in `[UNTRUSTED_CONTENT]` tags, preserving immutable system prompt at message index 0.
* **Strict Tool Argument Parsing (AQ-03):** Implemented strict JSON object decoding in `HardDAGExecutor` to reject malformed arguments with `TOOL_ARGUMENT_ERROR` without calling the tool.
* **Universal Message Sanitization (AQ-10):** Applied `sanitize_messages_for_llm` across both the primary proxy route and direct fallback paths in `FallbackCompletions.create`.
* **Universal Subprocess Sanitization:** Extended `sanitize_subprocess_env` to all auxiliary Git commands in `qz_recovery/resume_manager.py`, `qz_desktop_bridge.py`, and CLI handlers.

### Tool Bounding & Safety
* **Bounded Tool Outputs (TOOL-01, TOOL-02):** Bounded `read_file` to 2,000 lines max with 1-indexed pagination (`start_line`/`end_line`) and `list_files` to 200 items.
* **Hunk Line Count Validation (TOOL-03):** Validated declared unified diff hunk line counts against actual diff lines in `_parse_hunks`.
* **Ancestor-Scoped Rollback (SAFE-03, REC-02):** Enforced `git merge-base --is-ancestor` validation in `ResumeManager.preview_rollback` and unified CLI `/rollback` through the safe recovery engine.

### Reliability & Metrics
* **Truthful Completion Metrics (AQ-07, AQ-08):** Removed artificial minimum floors (`max(1, ...)` / `... or 1`) in CLI; propagated true DAG failure/cancellation status in `AutonomousRunner`.
* **Test Suite Expansion:** Added `test_qz_part2_audit_fixes.py` bringing full repository test coverage to 306 passing tests.

---

## [2.1.0] — 2026-09-17 (Part 1 Audit Remediation & Key Transport Architecture)

### Added & Fixed
* **Physical API Key Injection (ROUT-01):** Wired direct physical API key lookup via `KeyRegistry.get_key_value` and passed it into provider completion transports.
* **Secret Isolation (SEC-01):** Prevented KeyStore secrets from leaking into global `os.environ`; access is restricted to in-memory on demand.
* **Model Concurrency Limiter & 429 Rotation (ROUT-03):** Added per-key slot limits via `limiter.slot()` and immediate failover without redundant exponential backoff on exhausted keys.
* **Accurate Model Telemetry (ROUT-02):** Recorded provider-returned `response.model` in telemetry and cost tracking.
* **Budget Pre-Admission (BUDG-01):** Added `UsageTracker.admit_request()` pre-flight gate.
* **Multi-Workspace Fallback Isolation (MEM-01):** Partitioned fallback memory files by workspace SHA-256 hash.
* **Anti-Loop Repair Tracking (REPR-01):** Added diff signature tracking in `RepairHistory` with automatic checkpoint rollback.

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
