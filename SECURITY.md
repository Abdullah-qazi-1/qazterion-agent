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

1. **Credential & Secret Protection**
   - Provider API keys are encrypted at rest with Windows DPAPI (`CryptProtectData`, current user) on Windows and an owner-restricted Fernet keyfile elsewhere. Keystore writes are atomic.
   - Keys are decrypted in memory only when a request is sent; they are never written to logs, the usage log, the health file, or returned to the desktop UI (the old `get_enabled_env` RPC was removed).
   - The CLI never accepts a key on the command line and never records `/keys` lines in its history file.
   - Tool output and error messages pass through `qz_security.redaction.redact()`; the agent may not write `[REDACTED:...]` placeholders back into files.

2. **Command Execution (no Docker required)**
   - Commands run as child processes on your machine, inside the workspace, with every `*_KEY`, `*_TOKEN`, `*_SECRET`, `*_PASSWORD` (and similar) variable removed, a timeout, and termination of the whole process tree on timeout.
   - Before a command runs, `qz_security.command_risk` classifies it: destructive or system-level commands (file deletion, network downloads, registry, credential paths, `git reset --hard`, `git clean -f`, `git checkout -- …`, force-push, history rewrites, nested shells) are refused; package installs and other medium-risk commands need your explicit approval. With no one to ask (headless), medium-risk commands are refused unless `QAZTERION_HEADLESS_MEDIUM=allow`.
   - Paths in tool arguments and commands are resolved (including symlinks/junctions) and must stay inside the workspace.
   - This is a guard-rail, not an isolation boundary: code the agent writes and then runs (e.g. tests) runs with your user's permissions. Review plans/diffs for untrusted repositories.

3. **Instruction Hierarchy & Trust Boundaries**
   - The system prompt stays at message index 0. Tool output, repository content and history summaries are wrapped as untrusted content and scanned for injection markers.

4. **Git Safety**
   - The agent commits only files it read or wrote in the current task, never `.env`/key files, and marks its commits with a `Committed-by: Qazterion` trailer.
   - Rollback only ever discards Qazterion commits, refuses when tracked files have uncommitted changes or a user commit would be lost, never overwrites untracked files, and never falls back to `HEAD~1`. `force` cannot bypass these checks.
   - When a repair loop rolls back a failed subtask it restores only the files the agent changed in that subtask, and skips any file you modified meanwhile.
   - Qazterion refuses to `git init` your home directory or a drive root.
