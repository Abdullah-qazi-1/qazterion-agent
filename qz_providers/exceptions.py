"""Normalized provider errors.

Every adapter converts raw SDK/HTTP failures into one of these classes so the
gateway can decide, without provider-specific code, whether to retry, rotate to
another key, fail over to another model, or give up.
"""

from __future__ import annotations

import builtins
import re
from typing import Any


class ProviderError(Exception):
    """Base class for all provider failures."""

    kind: str = "error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        if retryable is not None:
            self.retryable = retryable
        self.retry_after = retry_after
        self.details = details


class AuthenticationError(ProviderError):
    """Invalid, revoked or expired key (401/403). The key should stop being used."""
    kind = "auth"


class RateLimitError(ProviderError):
    """Short-term rate limit (429). The key is fine; wait or rotate."""
    kind = "rate_limit"
    retryable = True

    def __init__(self, message: str, *, cooldown_suggested_s: float | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("status_code", 429)
        super().__init__(message, **kwargs)
        if cooldown_suggested_s is not None and self.retry_after is None:
            self.retry_after = cooldown_suggested_s

    @property
    def cooldown_suggested_s(self) -> float | None:
        return self.retry_after


class QuotaExhaustedError(RateLimitError):
    """Daily/monthly quota or billing exhausted. The key needs a long rest."""
    kind = "quota"


class TimeoutError(ProviderError):  # noqa: A001 - intentionally scoped to this module
    kind = "timeout"
    retryable = True


class ConnectionError(ProviderError):  # noqa: A001
    """Network failure before the provider answered."""
    kind = "connection"
    retryable = True


class ServerError(ProviderError):
    kind = "server"
    retryable = True


class ModelUnavailableError(ProviderError):
    """Model overloaded or temporarily unavailable on this provider."""
    kind = "model_unavailable"
    retryable = True


class ModelNotFoundError(ProviderError):
    """Model id does not exist (or is not enabled) on this provider."""
    kind = "model_not_found"


class ContextLengthExceededError(ProviderError):
    kind = "context_length"


class InvalidRequestError(ProviderError):
    """The request itself was rejected (bad parameters, unsupported tools, ...)."""
    kind = "invalid_request"


_QUOTA_MARKERS = (
    "quota", "insufficient credits", "insufficient balance", "insufficient_quota",
    "billing", "per day", "daily limit", "exceeded your current",
)
_RATE_MARKERS = ("429", "rate limit", "ratelimit", "rate_limit", "too many requests", "resource_exhausted")
_AUTH_MARKERS = (
    "invalid_api_key", "invalid api key", "api key not valid", "incorrect api key",
    "unauthorized", "unauthenticated", "permission denied", "forbidden", "api key expired",
    "authentication",
)
_CONTEXT_MARKERS = (
    "context_length_exceeded", "maximum context length", "context window", "prompt is too long",
    "exceeds context", "too many tokens", "request too large",
)
_NOT_FOUND_MARKERS = ("model not found", "model_not_found", "does not exist", "unknown model", "no such model", "is not found")
_TIMEOUT_MARKERS = ("timed out", "timeout", "deadline exceeded")
_CONNECTION_MARKERS = ("connection error", "connection refused", "connection reset", "name resolution", "getaddrinfo", "failed to establish")
_SERVER_MARKERS = ("internal server error", "bad gateway", "service unavailable", "gateway timeout")
_UNAVAILABLE_MARKERS = ("overloaded", "unavailable", "capacity", "try again later")


def _retry_after_from(error: Any) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        for name in ("retry-after", "Retry-After", "x-ratelimit-reset-requests"):
            try:
                value = headers.get(name)
            except Exception:
                value = None
            if value:
                parsed = _parse_duration(str(value))
                if parsed is not None:
                    return parsed
    match = re.search(r"(?:retry|try again) (?:after|in) ([\d.]+)\s*(ms|s|sec|seconds)?", str(error), re.IGNORECASE)
    if match:
        amount = float(match.group(1))
        return amount / 1000.0 if (match.group(2) or "").lower() == "ms" else amount
    return None


def _parse_duration(value: str) -> float | None:
    value = value.strip().lower()
    try:
        return float(value)
    except ValueError:
        pass
    total = 0.0
    found = False
    for amount, unit in re.findall(r"([\d.]+)\s*(ms|h|m|s)", value):
        found = True
        number = float(amount)
        total += {"ms": number / 1000.0, "s": number, "m": number * 60.0, "h": number * 3600.0}[unit]
    return total if found else None


def _status_from(error: Any) -> int | None:
    for attr in ("status_code", "status", "code"):
        value = getattr(error, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def normalize_error(error: BaseException | str | None, status_code: int | None = None) -> ProviderError:
    """Convert any raw failure into the matching :class:`ProviderError` subclass."""
    if isinstance(error, ProviderError):
        return error

    message = str(error) if error is not None else "Unknown provider error"
    lower = message.lower()
    status = status_code if status_code is not None else (_status_from(error) if error is not None and not isinstance(error, str) else None)
    retry_after = _retry_after_from(error) if error is not None else None
    type_name = type(error).__name__ if error is not None and not isinstance(error, str) else ""
    common = {"status_code": status, "retry_after": retry_after}

    # Typed SDK exceptions first (openai.APITimeoutError subclasses APIConnectionError).
    if type_name == "APITimeoutError" or isinstance(error, builtins.TimeoutError):
        return TimeoutError(message, **common)
    if type_name == "APIConnectionError" or isinstance(error, builtins.ConnectionError):
        return ConnectionError(message, **common)

    if status in (401, 403):
        return AuthenticationError(message, **common)
    if status == 429 or any(marker in lower for marker in _RATE_MARKERS):
        if any(marker in lower for marker in _QUOTA_MARKERS):
            return QuotaExhaustedError(message, **common)
        return RateLimitError(message, **common)
    if status == 402 or any(marker in lower for marker in ("insufficient credits", "insufficient balance", "billing")):
        return QuotaExhaustedError(message, **common)
    if any(marker in lower for marker in _CONTEXT_MARKERS):
        return ContextLengthExceededError(message, **common)
    if status == 404 or any(marker in lower for marker in _NOT_FOUND_MARKERS):
        return ModelNotFoundError(message, **common)
    if status in (408,) or any(marker in lower for marker in _TIMEOUT_MARKERS):
        return TimeoutError(message, **common)
    if status in (502, 503, 529) or any(marker in lower for marker in _UNAVAILABLE_MARKERS):
        return ModelUnavailableError(message, **common)
    if (status is not None and status >= 500) or any(marker in lower for marker in _SERVER_MARKERS):
        return ServerError(message, **common)
    if any(marker in lower for marker in _AUTH_MARKERS):
        return AuthenticationError(message, **common)
    if any(marker in lower for marker in _CONNECTION_MARKERS):
        return ConnectionError(message, **common)
    if status is not None and 400 <= status < 500:
        return InvalidRequestError(message, **common)
    return ProviderError(message, **common)
