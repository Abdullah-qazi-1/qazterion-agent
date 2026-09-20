from __future__ import annotations
import os
import sys
from typing import Any
from openai import OpenAI

PROXY_URL = os.environ.get("LITELLM_PROXY_URL", "http://localhost:4000")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-my-local-master-key")


def sanitize_messages_for_llm(messages: list[dict]) -> list[dict]:
    """Sanitize message turns for Gemini / strict multi-turn function calling providers."""
    if not messages:
        return []
    
    clean_list = []
    pending_tool_ids = set()
    
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")
        
        # System messages in middle of conversation -> convert to user message
        if role == "system" and len(clean_list) > 0:
            role = "user"
            content = f"[System Note]: {content}"

        clean_entry = {"role": role}
        if content is not None:
            clean_entry["content"] = str(content)
        elif not tool_calls:
            clean_entry["content"] = ""
            
        if tool_calls:
            clean_entry["tool_calls"] = tool_calls
            for tc in tool_calls:
                if isinstance(tc, dict) and tc.get("id"):
                    pending_tool_ids.add(tc["id"])
                elif hasattr(tc, "id"):
                    pending_tool_ids.add(getattr(tc, "id"))
                    
        if role == "tool":
            clean_entry["tool_call_id"] = str(tool_call_id or "call_0")
            if clean_entry["tool_call_id"] in pending_tool_ids:
                pending_tool_ids.remove(clean_entry["tool_call_id"])
            if "content" not in clean_entry:
                clean_entry["content"] = str(content or "")

        clean_list.append(clean_entry)
        
    return clean_list


class FallbackCompletions:
    def __init__(self, raw_client: OpenAI):
        self._raw_client = raw_client

    def create(self, **kwargs):
        call_kwargs = dict(kwargs)
        if "messages" in call_kwargs:
            call_kwargs["messages"] = sanitize_messages_for_llm(call_kwargs["messages"])

        # 1. Try local LiteLLM proxy if running
        try:
            return self._raw_client.chat.completions.create(**call_kwargs)
        except Exception as proxy_err:
            err_name = type(proxy_err).__name__.lower()
            err_str = (str(proxy_err) + " " + err_name).lower()
            # If not a connection error, re-raise original API error
            if "connection" not in err_str and "refused" not in err_str and "connect" not in err_str:
                raise

        # 2. Direct fallback transport to LiteLLM for the *requested* model/alias only
        try:
            import litellm
            litellm.suppress_debug_info = True

            req_model = kwargs.get("model", "gemini-3.6-flash")
            prov_name = None
            litellm_model = req_model

            if "/" in req_model:
                prov_name = req_model.split("/")[0].lower()
            else:
                try:
                    from qz_providers.model_registry import get_model_registry, DEFAULT_ALIAS_MAP
                    model_reg = get_model_registry()
                    meta = model_reg.get_model_by_alias(req_model)
                    if meta:
                        prov_name = meta.provider.lower()
                        litellm_model = f"{meta.provider}/{meta.model_id}"
                    elif req_model in DEFAULT_ALIAS_MAP:
                        p, m = DEFAULT_ALIAS_MAP[req_model]
                        prov_name = p.lower()
                        litellm_model = f"{p}/{m}"
                except Exception:
                    pass

            if not prov_name:
                prov_name = "gemini" if "gemini" in req_model else ("groq" if "groq" in req_model else "mistral")

            # Resolve API key from kwargs if provided, else keystore
            api_key = kwargs.get("api_key")
            if not api_key:
                try:
                    from qz_keystore import KeyStore
                    ks = KeyStore()
                    for entry in ks.list_entries(provider=prov_name):
                        if entry.enabled:
                            key_val = ks.get_key(entry.provider, entry.index)
                            if key_val:
                                api_key = key_val
                                break
                except Exception:
                    pass

            if not api_key:
                api_key = (
                    os.environ.get(f"{prov_name.upper()}_KEY_1")
                    or os.environ.get(f"{prov_name.upper()}_API_KEY")
                )

            # Sanitize messages for provider
            raw_messages = kwargs.get("messages", [])
            clean_messages = sanitize_messages_for_llm(raw_messages)

            call_kwargs = dict(kwargs)
            call_kwargs["model"] = litellm_model
            call_kwargs["messages"] = clean_messages
            if api_key:
                call_kwargs["api_key"] = api_key

            if "gemini" in litellm_model:
                call_kwargs.pop("top_k", None)

            return litellm.completion(**call_kwargs)
        except Exception as direct_err:
            raise direct_err


class RobustOpenAIClient:
    def __init__(self, raw_client: OpenAI):
        self._raw_client = raw_client
        self.chat = type("Chat", (), {"completions": FallbackCompletions(raw_client)})()

    def __getattr__(self, name):
        return getattr(self._raw_client, name)


_raw = OpenAI(base_url=PROXY_URL, api_key=MASTER_KEY, timeout=45.0, max_retries=0)
client = RobustOpenAIClient(_raw)


def get_client(client_override: OpenAI | None = None) -> Any:
    if client_override is not None:
        return client_override
    agent_mod = sys.modules.get("qz_agent")
    if agent_mod and hasattr(agent_mod, "client"):
        return getattr(agent_mod, "client")
    return client
