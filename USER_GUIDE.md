# Qazterion — User & Engineering Guide

> **Notice:** This document provides a complete guide and technical reference for using, configuring, and extending the **Qazterion Autonomous Coding Agent CLI**. See [README.md](README.md) for a quick overview and installation, and [CONTRIBUTING.md](CONTRIBUTING.md) if you want to develop Qazterion itself.

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

---

## 6. Technical Audit Remediations & Correctness Matrix

Comprehensive independent audits (Part 1 and Part 2) identified critical architectural, security, and runtime correctness gaps. The table below documents each confirmed finding, the architectural change implemented, files modified, and verifying regression test suites.

| Issue ID / Area | Problem Identified | Remediation Implemented | Modified Components / Files | Verifying Tests | Verification Result |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **AQ-02 / Boundary** | History summarizer promoted LLM summaries to `role: "system"`, violating instruction hierarchy and enabling prompt injection. | Rolled summaries are demoted to `role: "user"` wrapped in untrusted markers (`wrap_untrusted_content`), preserving root system instructions immutable at index 0. | `qz_core/executor.py`<br>`qz_agent.py` | `test_qz_part2_audit_fixes.py`<br>`test_qz_tools.py` | **Passed (100%)** |
| **AQ-03 / Tool Parsing** | Malformed JSON tool arguments were loosely caught, risking unintended execution of parameterless tools with default `{}`. | Structured `TOOL_ARGUMENT_ERROR` emitted upon `JSONDecodeError` or non-dict payloads, completely bypassing tool execution. | `qz_core/dag_executor.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **AQ-07 / Metrics** | CLI displayed artificial non-zero minimum floors (`max(1, files_modified)` and `tests_passed or 1`). | Removed all false metric minimums to report exact truthful modification and test numbers. | `qz_cli/app.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **AQ-08 / Status** | `AutonomousRunner` marked tasks as `completed` even when subtask DAG execution failed. | Propagated true DAG terminal status (`failed`, `cancelled`, `completed`) to task records. | `qz_core/autonomous_loop.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **AQ-10 / Sanitization** | `FallbackCompletions.create` only sanitized messages on fallback paths, bypassing proxy calls. | Enforced `sanitize_messages_for_llm` unconditionally across both primary proxy and direct fallback paths. | `qz_core/client.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **TOOL-01 / Bounding** | `read_file` allowed unbounded file reads, risking LLM context blowout on large files. | Bounded `read_file` to 2,000 lines max with 1-indexed pagination (`start_line`, `end_line`) and truncation markers. | `qz_tools.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **TOOL-02 / Bounding** | `list_files` allowed unbounded directory tree outputs in large repositories. | Bounded `list_files` to 200 items max with total file count indicators. | `qz_tools.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **TOOL-03 / Diff Parsing** | `_parse_hunks` did not validate declared hunk line counts against actual diff content. | Parsed and enforced declared hunk line counts during unified diff processing. | `qz_tools.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **SAFE-03 / Rollback** | Rollback allowed reverting to arbitrary commits outside the task branch history. | Added `git merge-base --is-ancestor` verification in `ResumeManager.preview_rollback` to block non-ancestor targets. | `qz_recovery/resume_manager.py` | `test_qz_part2_audit_fixes.py` | **Passed (100%)** |
| **REC-02 / CLI Undo** | Interactive `/rollback` command bypassed safe checkpoint recovery logic. | Routed CLI rollback handling through `ResumeManager.safe_rollback_to_checkpoint()`. | `qz_cli/commands/handlers.py` | `test_qz_audit_fixes.py` | **Passed (100%)** |
| **SEC-01 / Secret Isolation** | KeyStore credentials could be dumped into global `os.environ`. | Credential access restricted to in-memory on demand; global `os.environ` is never modified with decrypted keys. | `qz_agent.py`<br>`qz_keystore.py` | `test_qz_phase1_security.py` | **Passed (100%)** |
| **SEC-02 / Subprocesses** | Subprocesses could inherit environment variables containing credentials. | Scrubbed child process environments via `sanitize_subprocess_env` across all host and Git commands. | `qz_sandbox/backend.py`<br>`qz_tools.py`<br>`qz_core/git_ops.py`<br>`qz_recovery/resume_manager.py`<br>`qz_desktop_bridge.py`<br>`qz_cli/banner.py` | `test_qz_phase1_security.py` | **Passed (100%)** |
| **ROUT-01 / Key Transport** | Router selected physical API key, but transport omitted passing it to completions. | Added `get_key_value` in `qz_pool/registry.py` and passed `api_key` explicitly into completion transport. | `qz_pool/registry.py`<br>`qz_core/client.py`<br>`qz_core/executor.py` | `test_qz_phase2_routing.py`<br>`test_qz_agent_routing.py` | **Passed (100%)** |
| **ROUT-02 / Telemetry** | Telemetry logged requested alias rather than actual provider-returned `response.model`. | Recorded actual provider model string from response metadata in telemetry and cost tracking. | `qz_core/executor.py`<br>`qz_storage/db.py` | `test_qz_phase2_routing.py` | **Passed (100%)** |
| **ROUT-03 / Rate Limits** | Rate limits (429) performed redundant backoff on exhausted keys instead of rotating. | Immediate candidate route rotation on 429 errors; concurrency bounded via `limiter.slot()`. | `qz_core/executor.py`<br>`qz_router/limiter.py` | `test_qz_phase2_routing.py` | **Passed (100%)** |
| **ROUT-04 / R1 Metadata** | OpenRouter adapter declared unsupported tool-calling for DeepSeek R1. | Explicitly set `supports_tools=False` for DeepSeek R1 in OpenRouter adapter. | `qz_providers/adapters/openrouter.py` | `test_qz_phase2_routing.py` | **Passed (100%)** |
| **REPR-01 / Anti-Loop** | Repair loops could cycle repeatedly across identical invalid patches. | `RepairHistory` diff tracking in `qz_core/dag_executor.py` detects cycles and triggers `git checkout HEAD -- <files>` rollback. | `qz_core/dag_executor.py`<br>`qz_repair.py` | `test_qz_phase3_repair.py` | **Passed (100%)** |
| **BUDG-01 / Pre-Admission** | Outbound requests could exceed task or lifetime spending limits. | Enforced `UsageTracker.admit_request()` pre-flight gate before making API calls. | `qz_core/executor.py`<br>`qz_usage_tracker.py` | `test_qz_phase4_context_state.py` | **Passed (100%)** |
| **MEM-01 / State Isolation** | Fallback memory files collided across different project workspaces. | Partitioned fallback memory files by workspace SHA-256 hash (`project-memory-{ws_hash}.jsonl`). | `qz_memory.py` | `test_qz_phase4_context_state.py` | **Passed (100%)** |

---

## 7. Layered Security Architecture & Defenses

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          User Request / CLI REPL                        │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Instruction Boundary Guard                         │
│  - System Prompt fixed at index 0 (never overwritten or appended)       │
│  - Tool results wrapped in [UNTRUSTED_CONTENT:label] tags               │
│  - Compressed conversation summaries tagged as untrusted user messages  │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                  Context Optimizer & Budget Gatekeeper                  │
│  - UsageTracker.admit_request() pre-flight token & cost budget check    │
│  - Token-aware context window fitting (fit_request_context)             │
│  - SHA-256 workspace-isolated memory persistence                        │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                 Multi-Key Provider Router & Transport                   │
│  - Hardware DPAPI / Fernet encrypted KeyStore (in-memory access only)   │
│  - Concurrency limiter per physical key slot                            │
│  - Explicit physical API key injection into provider transport          │
│  - Immediate candidate route rotation on HTTP 429                       │
└────────────────────────────────────┬────────────────────────────────────┘
                                     │
                                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Subtask DAG Execution Engine                       │
│  - Strict JSON argument decoding (malformed payloads rejected cleanly)  │
│  - Workspace canonical path containment & traversal blocking            │
│  - Subprocess environment scrubbed of all credentials and secrets       │
│  - Cyclic repair loop detection with checkpoint checkout rollback       │
│  - Checkpoint restoration verified via git merge-base --is-ancestor     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 8. Test Verification & Empirical Results

The complete repository test suite consists of 38 test modules and 307 test cases:

```powershell
pytest
```
```text
================= 306 passed, 1 skipped in 153.66s (0:02:33) =================
```

### Dedicated Remediation Test Suites
- **`test_qz_part2_audit_fixes.py` (5 passed)**: Verifies history summary role demotion, malformed JSON tool rejection, bounded `read_file`/`list_files`, ancestor-scoped rollback, and proxy message sanitization.
- **`test_qz_phase1_security.py` (6 passed)**: Verifies KeyStore isolation from global `os.environ` and child process environment sanitization.
- **`test_qz_phase2_routing.py` (5 passed)**: Verifies physical API key transport injection, provider model telemetry attribution, concurrency limits, and 429 rotation.
- **`test_qz_phase3_repair.py` (4 passed)**: Verifies anti-loop repair detection and task-scoped checkpoint rollback.
- **`test_qz_phase4_context_state.py` (4 passed)**: Verifies budget pre-admission, context fitting, and workspace SHA-256 memory partitioning.
- **`test_qz_phase5_adversarial.py` (3 passed)**: Verifies prompt injection boundaries and path traversal blocking.
- **`test_qz_audit_fixes.py` (11 passed)**: Verifies regression coverage across legacy audit fixes.
- **`test_qz_agent_routing.py` (5 passed)**: Verifies multi-key agent routing and key value extraction.
- **`test_qz_tools.py` (16 passed)**: Verifies file operations, diff parsing, and tool execution.

---

## 9. Known Operational Constraints & Remaining Risk Factors

While all confirmed architectural and security defects have been remediated and verified, production deployments should account for the following operational characteristics:

1. **Docker Daemon Availability (1 Skipped Test)**
   - `test_docker_sandbox_isolated_execution` is skipped when Docker Desktop or the local Docker daemon is not active.
   - *Impact*: In non-containerized environments, Qazterion falls back to the host sandbox with `sanitize_subprocess_env` and workspace boundary enforcement.

2. **Physical API Key Exhaustion at High Concurrency**
   - If all configured API keys for a provider family hit concurrent rate limits simultaneously, failover cascades across secondary configured providers.
   - *Impact*: If all configured providers are exhausted, the execution pauses with a `PROVIDER_QUOTA_EXHAUSTED` status. Ensure multiple keys or providers are registered in the KeyStore for high-volume workflows.

3. **Complex Multi-File Fuzzy Merging**
   - `apply_patch` enforces strict line count and hunk context validation.
   - *Impact*: Heavily drifted files with non-unique surrounding lines will safely reject the patch rather than performing ambiguous fuzzy modifications, triggering diagnostic repair.

---

## 10. Recommended Next Steps for Production Workloads

1. **Populate KeyStore**: Configure at least two API keys per provider family (e.g. Gemini, Groq, Mistral) using `qazterion /keys add` to maximize uptime and bypass rate limits.
2. **Enable Plan Confirmation**: For mission-critical repositories, retain interactive plan approval (`Proceed? [Y/n]`) before subtask execution.
3. **Initialize Clean Git Repositories**: Ensure target workspaces have an initialized Git repository with an established `HEAD` commit so atomic checkpoints and ancestor-validated rollbacks operate with full fidelity.