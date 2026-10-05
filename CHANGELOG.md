# Changelog

All notable changes to **Qazterion** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.3.0] — 2026-10-05 (Multi-provider gateway, safety and reliability overhaul)

### Architecture
* **In-process provider gateway** (`qz_providers`): roles → provider/model/key selection, key rotation, per-(key, model) rate-limit/quota cooldowns, invalid-key disabling, retired-model benching, cross-provider failover, bounded attempts and short waits for cooldowns. Health is persisted and shared between processes.
* **One provider catalog** (`default_providers.yaml` + user `providers.yaml`) replaces `config.yaml`, the alias map, router fallbacks, executor fallbacks and the scoring table. Adding a provider is configuration only.
* **LiteLLM proxy removed** (no background process, no port 4000, no master key); `litellm` is no longer a dependency. Removed `qz_pool`, `qz_router`, `qz_storage`, `qz_telemetry`, `qz_proxy_manager`, `generate_config`, the Docker sandbox and `setup.py`.
* One task database (`qz_tasks`) in the user data dir; index cache and project memory moved out of the project.

### Fixed
* The CLI authenticated with a hard-coded proxy key and model calls with stored keys crashed (`api_key` passed to the SDK).
* Desktop bridge could lose all RPC replies under concurrent requests (`redirect_stdout` race); decrypted keys were exposed over RPC.
* Destructive git commands ran without approval; repair rollback could wipe user edits; checkpoint rollback could fall back to `HEAD~1` or discard user commits; `force` skipped safety checks.
* Non-ASCII files were corrupted and CRLF files rewritten as LF on Windows; zero-length diff hunks inserted at the wrong line.
* Permission prompts crashed in the CLI and treated "deny" as approval; plan approval never happened in the CLI and edited plans were ignored by the desktop app; EOF auto-approved plans.
* Tasks in repositories with pre-existing failing tests could never complete; validation commands could run in the wrong directory.
* Tool exceptions aborted whole tasks; cancellation still validated and committed; checkpoint ordering, missing `get_checkpoint`, Rich markup in help, `formatters` missing `import sys`.
* Dashboard usage never refreshed; proxy status fields were missing; `pip install requests` was blocked while `git reset --hard` was allowed.

### Tests
* Hermetic suite (temp data dir, no `.env`, offline gateway). New tests for the catalog, keys, error classification, health, gateway failover/rotation, adapters over a real local HTTP server, file safety, rollback safety against real git, DAG resilience, the bridge protocol and a full end-to-end task through the real pipeline.

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
