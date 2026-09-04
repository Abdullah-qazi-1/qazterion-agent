from __future__ import annotations

from openai import OpenAI
from qz_core.client import get_client


# ============================================================
# PHASE 6: Self-review pass
# ============================================================

def self_review(changes: list[dict], client: OpenAI | None = None) -> str | None:
    """Review only files changed during this task, returning actionable issues.

    Patch diffs are preferred because they show exactly what changed. New-file
    content is bounded so the review remains focused even if a generated file
    is large. A review outage must never discard completed work.
    """
    if not changes:
        return None

    items = []
    for change in changes:
        detail = str(change.get("detail", ""))[:12_000]
        items.append(f"File: {change['path']}\nChange type: {change['kind']}\nChanged content:\n{detail}")
    review_input = "\n\n---\n\n".join(items)
    c = get_client(client)
    try:
        response = c.chat.completions.create(
            model="reasoner",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a strict senior code reviewer. Review only the supplied changed code. "
                        "Find concrete bugs, security concerns, edge cases, regressions, or missing tests. "
                        "If clean, reply exactly NO_ISSUES. Otherwise give concise, actionable findings."
                    ),
                },
                {"role": "user", "content": review_input},
            ],
            temperature=0,
            max_tokens=900,
        )
        findings = (response.choices[0].message.content or "").strip()
    except Exception as error:
        print(f"\033[90m[review] self-review unavailable ({error}); continuing without it\033[0m")
        return None

    return None if findings.upper() == "NO_ISSUES" else findings or None
