"""Mask secret_scanner findings in text sent to the model or logs."""

from __future__ import annotations

from qz_security.secret_scanner import Finding, scan


def redact(text: str, findings: list[Finding] | None = None) -> str:
    if not text:
        return text
    hits = findings if findings is not None else scan(text)
    if not hits:
        return text
    pieces: list[str] = []
    cursor = 0
    for hit in hits:
        pieces.append(text[cursor:hit.start])
        pieces.append(f"[REDACTED:{hit.kind}]")
        cursor = hit.end
    pieces.append(text[cursor:])
    return "".join(pieces)
