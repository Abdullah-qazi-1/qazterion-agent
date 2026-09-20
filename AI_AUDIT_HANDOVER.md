# Qazterion (v2.2.0) — Technical Architecture & Audit Handover Brief

> **Purpose:** This document is a complete, self-contained technical briefing for any AI assistant, auditor, or engineer inspecting, reviewing, or continuing development on **Qazterion**. It contains the system architecture, audit history, implemented remediations, security guarantees, test results, and operational context.

---

## 1. System Overview

**Qazterion** is an open-source, provider-agnostic, terminal-based autonomous AI software engineering agent. It understands user instructions in natural language, plans multi-step DAG implementations, inspects code, applies targeted diff patches, runs tests, self-repairs failed tests, and creates atomic Git checkpoints.

### Key Capabilities & Philosophy
- **Multi-Provider Key Pooling & Auto-Failover**: Supports Google Gemini, Groq, Mistral, DeepSeek, and OpenRouter with multi-key round-robin rotation (e.g. GEMINI_KEY_1, GEMINI_KEY_2) and immediate failover on HTTP 429 rate limits.
- **Hardware-Level Encryption & In-Memory Secrets**: Keys are encrypted at rest using Windows DPAPI (CryptProtectData) on Windows and Fernet on Unix. Credentials are never written to global os.environ.
- **Instruction Hierarchy & Trust Boundaries**: Immutable system prompt fixed at message index 0. All external tool results, repository reads, and history summaries are sandboxed as untrusted user content ([UNTRUSTED_CONTENT:label]).
- **Subprocess Isolation**: Host tools and Git executions run with sanitized environment variables (sanitize_subprocess_env).
- **Task-Scoped Checkpoints & Rollback**: Checkpoints are created per subtask and validated via git merge-base --is-ancestor before reverting.

---

## 2. Codebase Architecture & File Map

`
qazterion/
├── qz_agent.py              # High-level agent orchestration, REPL integration, rolling summary
├── qz_tools.py              # Filesystem tools (read_file, apply_patch, list_files, make_directory, run_command)
├── qz_context.py            # Token budgeting, context compaction (fit_request_context), chunk retriever
├── qz_indexer.py            # AST symbol extraction and incremental codebase dependency indexer
├── qz_keystore.py           # OS DPAPI & Fernet hardware-encrypted key vault
├── qz_memory.py             # SHA-256 workspace-isolated persistent project memory
├── qz_repair.py             # Diagnostic test error classification and repair suggestion engine
├── qz_usage_tracker.py      # Pre-flight request admission and lifetime cost/token tracking
├── qz_desktop_bridge.py     # Desktop UI / IPC bridge with subprocess sanitization
├── qz_environment.py        # System environment inspection and tool discovery
├── qz_cli/                  # Terminal UI, Claude Code theme, banner, REPL
│   ├── app.py               # Main CLI interactive entry point & truthful metric reporting
│   ├── banner.py            # Terminal ASCII banner and workspace status card
│   ├── formatters.py        # Unified colored diff, plan tree, and status renderers
│   └── commands/            # Slash command handlers (/keys, /status, /diff, /rollback, etc.)
├── qz_core/                 # Core autonomous execution engine
│   ├── autonomous_loop.py   # AutonomousRunner loop & status propagation
│   ├── client.py            # FallbackCompletions transport & universal message sanitization
│   ├── dag_executor.py      # HardDAGExecutor, strict JSON tool parsing, repair anti-loop
│   ├── event_bus.py         # Pub/Sub event dispatcher for UI and telemetry
│   ├── executor.py          # Provider completion dispatcher, limiter slot acquisition, rolling summary
│   ├── git_ops.py           # Atomic Git commit creation and repository checkpointing
│   ├── planner.py           # Multi-step task decomposition and DAG planning
│   └── classifier.py        # Task complexity analyzer (quick, standard, complex)
├── qz_pool/                 # Physical key registry, health tracking, and slot pooling
│   ├── registry.py          # KeyRegistry with get_key_value lookup & normalization
│   ├── health_manager.py    # Per-key error tracking, exponential backoff, circuit breaker
│   └── pool.py              # Provider key pool allocator
├── qz_providers/            # Provider configurations and adapters
│   ├── model_registry.py    # Model metadata, context windows, pricing, alias persistence
│   ├── registry.py          # Provider definitions (Gemini, Groq, Mistral, DeepSeek, OpenRouter)
│   └── adapters/            # Custom provider adapters (e.g. OpenRouter tool support flags)
├── qz_recovery/             # Crash recovery, state resumption, and rollback
│   └── resume_manager.py    # ResumeManager, ancestor-validated checkpoint rollback
├── qz_router/               # Model routing and concurrency management
│   ├── router.py            # Cost/latency-optimized provider and model selection
│   └── limiter.py           # ConcurrencyLimiter per physical key slot
├── qz_sandbox/              # Sandboxed command execution
│   └── backend.py           # Docker containerized execution and host fallback
├── qz_security/             # Security gatekeeper, path containment, and redactor
│   ├── gateway.py           # Tool execution authorization gatekeeper
│   ├── injection_guard.py   # Untrusted content delimiter wrapping ([UNTRUSTED_CONTENT])
│   ├── secret_scanner.py    # Regex secret redactor for logs and output
│   └── workspace_guard.py   # Canonical path resolution preventing ../ traversal
├── qz_storage/              # SQLite persistence engine
│   └── db.py                # Sessions, tasks, subtasks, events, checkpoints, telemetry
├── qz_validation/           # Objective validation pipeline (tests, lint, typecheck, build)
└── tests/ & test_qz_*.py    # Comprehensive test suite (38 test modules, 307 tests)
`

---

## 3. Audit History & Implemented Remediations

Two major independent technical audits have been performed on Qazterion. All confirmed findings have been remediated, hardened, and verified with dedicated test suites.

### Part 1 Audit Remediations (Core & Routing)
1. **Physical API Key Transport (ROUT-01)**: KeyRegistry.get_key_value wires decrypted physical keys directly into LiteLLM/provider completion calls.
2. **Secret Isolation (SEC-01)**: Master keys and provider secrets are stored encrypted (DPAPI/Fernet) and never written to global os.environ.
3. **Accurate Provider Model Telemetry (ROUT-02)**: Telemetry records actual provider-returned response.model rather than requested alias strings.
4. **Concurrency Limiting & 429 Rotation (ROUT-03)**: Bounded concurrent requests per key via limiter.slot() and immediate failover to alternate candidate routes upon rate limit without redundant backoff.
5. **DeepSeek R1 Metadata (ROUT-04)**: Set supports_tools=False in OpenRouter adapter to prevent invalid tool schema dispatch.
6. **Self-Repair Anti-Loop (REPR-01)**: RepairHistory diff tracking in HardDAGExecutor detects cyclic modifications and performs git checkout HEAD -- <files> rollback.
7. **Hard Budget Admission (BUDG-01)**: Pre-flight UsageTracker.admit_request() blocks outbound calls if token or financial limits are reached.
8. **Workspace-Partitioned Memory (MEM-01)**: Fallback memory files partitioned by workspace SHA-256 hash (project-memory-{ws_hash}.jsonl).

### Part 2 Audit Remediations (Instruction Hierarchy, Tool Safety & Metrics)
9. **Instruction Hierarchy Demotion (AQ-02)**: Compressed conversation summaries in roll_conversation_summary are demoted from role: 'system' to role: 'user' with untrusted delimiters, preserving the immutable system prompt at index 0.
10. **Strict Tool Argument Decoding (AQ-03)**: HardDAGExecutor strictly parses JSON object arguments. Any JSONDecodeError or non-dict payload generates a structured TOOL_ARGUMENT_ERROR and aborts tool invocation.
11. **Truthful Status & Completion Metrics (AQ-07, AQ-08)**: Removed artificial CLI minimums (max(1, ...) / ... or 1) to report truthful metrics; propagated true DAG failure/cancellation in AutonomousRunner.
12. **Universal Message Sanitization (AQ-10)**: Applied sanitize_messages_for_llm across both the primary proxy route and direct fallback paths.
13. **Bounded Tool Outputs (TOOL-01, TOOL-02)**: Bounded read_file to 2,000 lines max with 1-indexed pagination (start_line/end_line) and list_files to 200 items.
14. **Hunk Line Count Validation (TOOL-03)**: Enforced declared unified diff hunk line count validation in _parse_hunks.
15. **Ancestor-Validated Rollback (SAFE-03, REC-02)**: Rollback verifies target commit is a strict Git ancestor (git merge-base --is-ancestor) and routed CLI /rollback through the safe recovery manager.
16. **Auxiliary Subprocess Sanitization**: Passed env=sanitize_subprocess_env(workspace) across all Git commands in recovery, CLI banner, and desktop bridge.

---

## 4. Test Verification & Empirical Results

The entire codebase is verified using pytest: **306 passed, 1 skipped (Docker daemon check), 0 failed** across all 38 test suites.

### Key Verification Suites
- test_qz_part2_audit_fixes.py (5 passed): Summaries demotion, strict tool parsing, bounded outputs, ancestor rollback, proxy sanitization.
- test_qz_phase1_security.py (6 passed): Secret isolation and subprocess environment sanitization.
- test_qz_phase2_routing.py (5 passed): Key transport, telemetry model attribution, concurrency limits, 429 rotation.
- test_qz_phase3_repair.py (4 passed): Anti-loop repair detection and task checkpoint rollback.
- test_qz_phase4_context_state.py (4 passed): Budget admission, context compaction, SHA-256 memory partitioning.
- test_qz_phase5_adversarial.py (3 passed): Prompt injection boundaries and path traversal containment.
- test_qz_audit_fixes.py (11 passed): Consolidated regression suite.
- test_qz_agent_routing.py (5 passed): Multi-key agent routing and physical key extraction.
- test_qz_tools.py (16 passed): Tool executions, diffing, and rolling summary assertions.

---

## 5. Known Operational Limitations

1. **Docker Daemon Presence**: If Docker Desktop is offline, execution falls back safely to the host sandbox with subprocess sanitization and canonical path containment.
2. **Provider Key Exhaustion**: If all registered keys for all configured providers hit simultaneous rate limits, execution pauses with PROVIDER_QUOTA_EXHAUSTED.
3. **Severe Multi-File Diff Drift**: apply_patch rejects patches when context lines are ambiguous to prevent corrupting code, safely passing control to the repair loop.

---

## 6. How to Run & Verify

`powershell
# 1. Activate virtual environment
.\venv\Scripts\Activate.ps1

# 2. Run full test suite
pytest -v

# 3. Launch interactive CLI
qazterion

# 4. Target a specific workspace
qazterion --workspace 'D:\Projects\my-app'
`
