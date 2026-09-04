"""Normalized provider exceptions and error mapping."""

from __future__ import annotations

from typing import Any


class ProviderError(Exception):
    """Base exception for all provider and model execution errors."""
    retryable: bool = False
    status_code: int | None = None

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        self.details = details


class AuthenticationError(ProviderError):
    """Raised on 401/403 or invalid API credentials (permanent failure, do not blindly retry)."""
    retryable = False


class RateLimitError(ProviderError):
    """Raised on 429 or quota limit exhaustion."""
    retryable = True

    def __init__(self, message: str, *, cooldown_suggested_s: float = 15.0, status_code: int | None = 429, **kwargs: Any) -> None:
        super().__init__(message, status_code=status_code, retryable=True, **kwargs)
        self.cooldown_suggested_s = cooldown_suggested_s


class TimeoutError(ProviderError):
    """Raised on request timeout or deadline exceeded."""
    retryable = True


class ServerError(ProviderError):
    """Raised on 5xx upstream server errors (500, 502, 503, 504)."""
    retryable = True


class ContextLengthExceededError(ProviderError):
    """Raised when prompt/context exceeds model's context window."""
    retryable = False


class ModelNotFoundError(ProviderError):
    """Raised when requested model ID does not exist on provider."""
    retryable = False


class ModelUnavailableError(ProviderError):
    """Raised when model is temporarily degraded or undergoing maintenance."""
    retryable = True


class InvalidRequestError(ProviderError):
    """Raised for malformed parameters, invalid schema, or bad payload."""
    retryable = False


def normalize_error(error: Exception | str | None, status_code: int | None = None) -> ProviderError:
    """Normalize any raw error, status code, or exception into a standard ProviderError subclass."""
    if isinstance(error, ProviderError):
        return error

    msg = str(error) if error is not None else "Unknown provider error"
    msg_lower = msg.lower()

    # 1. Status code checks
    if status_code == 401 or status_code == 403:
        return AuthenticationError(msg, status_code=status_code)
    if status_code == 429:
        return RateLimitError(msg, status_code=status_code)
    if status_code == 404:
        return ModelNotFoundError(msg, status_code=status_code)
    if status_code in (500, 502, 503, 504):
        return ServerError(msg, status_code=status_code)

    # 2. String pattern checks
    if any(m in msg_lower for m in ("401", "403", "unauthorized", "invalid_api_key", "invalid api key", "forbidden", "permission denied", "auth failed")):
        return AuthenticationError(msg, status_code=status_code or 401)

    if any(m in msg_lower for m in ("429", "rate limit", "ratelimit", "too many requests", "quota", "insufficient credits", "insufficient balance", "billing", "resource_exhausted")):
        return RateLimitError(msg, status_code=status_code or 429)

    if any(m in msg_lower for m in ("context_length_exceeded", "maximum context length", "too many tokens", "prompt is too long", "exceeds context")):
        return ContextLengthExceededError(msg, status_code=status_code or 400)

    if any(m in msg_lower for m in ("model not found", "does not exist", "model_not_found", "unknown model")):
        return ModelNotFoundError(msg, status_code=status_code or 404)

    if any(m in msg_lower for m in ("timeout", "timed out", "deadline", "timedout", "connection timeout", "read timeout")):
        return TimeoutError(msg, status_code=status_code or 408)

    if any(m in msg_lower for m in ("500", "502", "503", "504", "bad gateway", "service unavailable", "internal server error", "server error")):
        return ServerError(msg, status_code=status_code or 500)

    if any(m in msg_lower for m in ("unavailable", "overloaded", "capacity", "degraded")):
        return ModelUnavailableError(msg, status_code=status_code or 503)

    return ProviderError(msg, status_code=status_code, retryable=False)
