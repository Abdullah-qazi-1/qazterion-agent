# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 2.x.x   | :white_check_mark: |
| 1.x.x   | :x:                |

## Reporting a Vulnerability

We take the security of Qazterion seriously. If you discover a security vulnerability, please do NOT open a public issue.

Instead, please report security concerns via email or private security advisory:
- **Email**: abdullahizaq321@gmail.com (Abdullah)
- Include details, reproduction steps, and potential impact.

### Security Guarantees & Defenses in Qazterion

1. **Credential & Secret Protection**:
   - Provider API keys and master secrets are encrypted at rest using Windows DPAPI (`CryptProtectData`) on Windows and owner-restricted Fernet keyfiles on Unix/macOS.
   - Credentials are never stored in global `os.environ`. Key values are retrieved strictly in-memory on demand when dispatching requests to provider transports.
   - All logging, telemetry, and error tracebacks pass through `qz_security.secret_scanner.redact()` to sanitize API keys, passwords, and tokens before terminal display or disk persistence.

2. **Subprocess & Environment Sanitization**:
   - All host, tool, and Git subprocess executions run within a sanitized environment (`sanitize_subprocess_env`).
   - Unrelated environment variables and sensitive host credentials (`*_API_KEY`, `*_SECRET`, `*_TOKEN`, `*_PASSWORD`) are stripped from child processes.

3. **Instruction Hierarchy & Trust Boundaries**:
   - System instructions remain strictly immutable at message index 0 and are never overwritten, replaced, or dynamically demoted.
   - External tool outputs, repository files, and model-generated summaries are wrapped in `[UNTRUSTED_CONTENT:label]` delimiters and assigned `role: "user"`, preventing prompt injection or elevation of untrusted text into system instructions.

4. **Tool Argument Parsing & Sandboxing**:
   - Tool arguments are parsed strictly as JSON objects. Any malformed payload emits a structured `TOOL_ARGUMENT_ERROR` and aborts execution, preventing unintended parameterless tool invocation.
   - Filesystem tools are bound by `WorkspaceGuard` canonical path resolution, blocking directory traversal (`../`) and out-of-bounds file writes.

5. **Safe Checkpoint & Ancestor Rollback**:
   - Checkpoint rollbacks are verified via `git merge-base --is-ancestor` to prevent rolling back across detached or non-ancestor commits.
   - Repair loops track diff signatures (`RepairHistory`) to halt infinite cyclic modifications and safely revert to the last verified checkpoint.