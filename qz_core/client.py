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
        # 1. Try local LiteLLM proxy if running
        try:
            return self._raw_client.chat.completions.create(**kwargs)
        except Exception as proxy_err:
            err_name = type(proxy_err).__name__.lower()
            err_str = (str(proxy_err) + " " + err_name).lower()
            # If not a connection error, re-raise original API error
            if "connection" not in err_str and "refused" not in err_str and "connect" not in err_str:
                raise

        # 2. Direct fallback to LiteLLM with active keystore provider rotation
        try:
            import litellm
            litellm.suppress_debug_info = True

            # Load any available keys from keystore
            configured_keys = {}
            try:
                from qz_keystore import KeyStore
                ks = KeyStore()
                for k, v in ks.enabled_env().items():
                    os.environ[k] = v
                for entry in ks.list_entries():
                    if entry.enabled:
                        configured_keys.setdefault(entry.provider.lower(), []).append(ks.get_key(entry.provider, entry.index))
            except Exception:
                pass

            available_providers = list(configured_keys.keys())
            if not available_providers:
                if os.environ.get("GEMINI_KEY_1") or os.environ.get("GEMINI_API_KEY"):
                    available_providers.append("gemini")

            # Route model aliases strictly to available providers
            model_name = kwargs.get("model", "gemini-3.6-flash")

            if "gemini" in available_providers and len(available_providers) == 1:
                # User has only Gemini configured -> route all requests to gemini-3.6-flash
                candidates = ["gemini/gemini-3.6-flash"]
            elif "groq" in available_providers and "gemini" not in available_providers:
                candidates = ["groq/llama-3.3-70b-versatile", "groq/openai/gpt-oss-20b"]
            else:
                candidates = ["gemini/gemini-3.6-flash", "groq/llama-3.3-70b-versatile"]

            # Sanitize messages for provider
            raw_messages = kwargs.get("messages", [])
            clean_messages = sanitize_messages_for_llm(raw_messages)

            last_err = None
            for model_candidate in candidates:
                prov_name = "gemini" if "gemini" in model_candidate else ("groq" if "groq" in model_candidate else "mistral")
                keys_list = configured_keys.get(prov_name, [os.environ.get(f"{prov_name.upper()}_KEY_1") or os.environ.get(f"{prov_name.upper()}_API_KEY")])

                for api_key in keys_list:
                    if not api_key:
                        continue
                    try:
                        call_kwargs = dict(kwargs)
                        call_kwargs["model"] = model_candidate
                        call_kwargs["messages"] = clean_messages
                        call_kwargs["api_key"] = api_key
                        
                        # Remove parameters deprecated in Gemini 3+ if present
                        if "gemini" in model_candidate:
                            call_kwargs.pop("top_k", None)

                        return litellm.completion(**call_kwargs)
                    except Exception as attempt_err:
                        last_err = attempt_err
                        err_str = str(attempt_err)
                        if "429" in err_str or "quota" in err_str.lower() or "rate" in err_str.lower():
                            # Rate limited on this key -> continue to next key/candidate
                            continue
                        continue

            if last_err:
                raise last_err
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
