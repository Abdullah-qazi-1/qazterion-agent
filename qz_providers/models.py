"""Data models for provider abstraction, model metadata, capabilities, and lifecycle states."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ModelCapability(str, Enum):
    """Core capabilities supported by LLM models."""
    CHAT = "chat"
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    STRUCTURED_OUTPUT = "structured_output"
    VISION = "vision"
    REASONING = "reasoning"
    FAST = "fast"
    LARGE_CONTEXT = "large_context"


class ModelLifecycleState(str, Enum):
    """Lifecycle availability states for models."""
    ACTIVE = "active"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DEPRECATED = "deprecated"
    REMOVED = "removed"


@dataclass
class ModelMetadata:
    """Rich metadata describing an LLM model, its capabilities, limits, and status."""
    provider: str
    model_id: str
    display_name: str
    capabilities: list[str] = field(default_factory=list)
    context_window: int = 32768
    max_output_tokens: int | None = 4096
    input_modalities: list[str] = field(default_factory=lambda: ["text"])
    output_modalities: list[str] = field(default_factory=lambda: ["text"])
    supports_tools: bool = True
    supports_vision: bool = False
    supports_streaming: bool = True
    supports_reasoning: bool = False
    supports_structured_output: bool = True
    state: ModelLifecycleState = ModelLifecycleState.ACTIVE
    consecutive_failures: int = 0
    last_discovery_time: float | None = None
    pricing_input_per_1m: float | None = None
    pricing_output_per_1m: float | None = None
    source: str = "static"  # "discovery" | "static" | "user_config"
    historical: bool = False

    def is_available(self) -> bool:
        """Return True if model is eligible for routing."""
        return self.state in (ModelLifecycleState.ACTIVE, ModelLifecycleState.DEGRADED)

    def has_capability(self, cap: str | ModelCapability) -> bool:
        cap_val = cap.value if isinstance(cap, ModelCapability) else str(cap).lower()
        if cap_val in [c.lower() for c in self.capabilities]:
            return True
        if cap_val == "tool_calling" and self.supports_tools:
            return True
        if cap_val == "vision" and self.supports_vision:
            return True
        if cap_val == "streaming" and self.supports_streaming:
            return True
        if cap_val == "reasoning" and self.supports_reasoning:
            return True
        if cap_val == "structured_output" and self.supports_structured_output:
            return True
        if cap_val == "large_context" and self.context_window >= 65536:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "capabilities": list(self.capabilities),
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_streaming": self.supports_streaming,
            "supports_reasoning": self.supports_reasoning,
            "supports_structured_output": self.supports_structured_output,
            "state": self.state.value if isinstance(self.state, ModelLifecycleState) else str(self.state),
            "consecutive_failures": self.consecutive_failures,
            "last_discovery_time": self.last_discovery_time,
            "pricing_input_per_1m": self.pricing_input_per_1m,
            "pricing_output_per_1m": self.pricing_output_per_1m,
            "source": self.source,
            "historical": self.historical,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelMetadata:
        state_raw = data.get("state", "active")
        try:
            state = ModelLifecycleState(state_raw)
        except ValueError:
            state = ModelLifecycleState.ACTIVE
        return cls(
            provider=str(data.get("provider", "")),
            model_id=str(data.get("model_id", "")),
            display_name=str(data.get("display_name", data.get("model_id", ""))),
            capabilities=list(data.get("capabilities", [])),
            context_window=int(data.get("context_window", 32768)),
            max_output_tokens=data.get("max_output_tokens"),
            input_modalities=list(data.get("input_modalities", ["text"])),
            output_modalities=list(data.get("output_modalities", ["text"])),
            supports_tools=bool(data.get("supports_tools", True)),
            supports_vision=bool(data.get("supports_vision", False)),
            supports_streaming=bool(data.get("supports_streaming", True)),
            supports_reasoning=bool(data.get("supports_reasoning", False)),
            supports_structured_output=bool(data.get("supports_structured_output", True)),
            state=state,
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            last_discovery_time=data.get("last_discovery_time"),
            pricing_input_per_1m=data.get("pricing_input_per_1m"),
            pricing_output_per_1m=data.get("pricing_output_per_1m"),
            source=str(data.get("source", "static")),
            historical=bool(data.get("historical", False)),
        )


@dataclass
class ProviderInfo:
    """Information and health summary for a registered provider."""
    provider_id: str
    display_name: str
    enabled: bool = True
    adapter_type: str = "openai_compatible"
    base_url: str | None = None
    default_concurrency: int = 2
    supported_capabilities: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    health_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "adapter_type": self.adapter_type,
            "base_url": self.base_url,
            "default_concurrency": self.default_concurrency,
            "supported_capabilities": list(self.supported_capabilities),
            "models": list(self.models),
            "health_summary": dict(self.health_summary),
        }
