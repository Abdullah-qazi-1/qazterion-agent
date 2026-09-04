# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

We take the security of Qazterion seriously. If you discover a security vulnerability, please do NOT open a public issue.

Instead, please report security concerns via email or private security advisory:
- **Email**: abdullahizaq321@gmail.com (Abdullah)
- Include details, reproduction steps, and potential impact.

### Security Guarantees in Qazterion

1. **Credential Protection**:
   - Provider API keys and LiteLLM master secrets are never stored in plaintext.
   - On Windows, secrets are encrypted using Windows Data Protection API (DPAPI).
   - On non-Windows platforms, secrets are encrypted with owner-restricted keyfiles.
   - Credentials are never logged in full and only masked representations are exposed in the CLI or logs.

2. **Isolated Execution**:
   - Execution of filesystem, terminal, and AI tools is guarded by permission checks and safe sandboxing.
