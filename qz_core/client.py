"""OpenAI-style client facade over the in-process :class:`ModelGateway`.

Agent code calls ``get_client().chat.completions.create(model=<role>, ...)`` where
``model`` is a role name ("coder", "planner", ...) or ``provider/model``. The
gateway picks the provider, model and API key and fails over transparently.
"""

from __future__ import annotations

import sys
from typing import Any


class _GatewayCompletions:
    def create(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        task_id: str | None = None,
        timeout: float | None = None,
        **_ignored: Any,
    ) -> Any:
        from qz_providers.gateway import get_gateway

        if task_id is None:
            # Attribute planner/classifier/review calls to the running task too.
            from qz_core.common import get_task_context

            ctx = get_task_context()
            if ctx is not None and ctx.task_id not in ("rpc", "validation"):
                task_id = ctx.task_id
        return get_gateway().complete(
            model,
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            task_id=task_id,
            timeout=timeout,
        )


class _Chat:
    def __init__(self) -> None:
        self.completions = _GatewayCompletions()


class GatewayClient:
    """Drop-in for the subset of the OpenAI client the agent uses."""

    def __init__(self) -> None:
        self.chat = _Chat()


client = GatewayClient()


def get_client(client_override: Any | None = None) -> Any:
    """Return ``client_override`` if given, else the module-level gateway client.

    Tests may replace ``qz_agent.client`` to intercept every model call.
    """
    if client_override is not None:
        return client_override
    agent_mod = sys.modules.get("qz_agent")
    if agent_mod is not None and getattr(agent_mod, "client", None) is not None:
        return agent_mod.client
    return client


def accepts_task_id(target: Any) -> bool:
    """True when ``target`` is the gateway client (which understands ``task_id``)."""
    return isinstance(target, GatewayClient)
