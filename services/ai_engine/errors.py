"""services/ai_engine/errors.py
Normalized error model, categories, and classifier for AI providers and search tools.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class ErrorCategory(str, Enum):
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH_ERROR = "AUTH_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    SERVER_ERROR = "SERVER_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    UNKNOWN = "UNKNOWN"


@dataclass
class ProviderError:
    """Normalized provider error carrying capability context, status code, and retry semantics."""
    provider: str
    capability: str  # "generation" or "search"
    category: ErrorCategory
    status_code: Optional[int] = None
    retryable: bool = False
    retry_after: Optional[float] = None
    message: str = ""

    def __str__(self) -> str:
        code_str = f" HTTP {self.status_code}" if self.status_code else ""
        return f"[{self.provider}:{self.capability}] {self.category.value}{code_str}: {self.message}"


def classify_provider_error(
    error: Any,
    status_code: Optional[int] = None,
    provider: str = "unknown",
    capability: str = "general"
) -> ProviderError:
    """
    Central error classification function parsing exceptions, HTTP responses,
    Google GenAI SDK errors, and error strings into a normalized ProviderError.
    """
    err_str = str(error) if error else ""
    code = status_code

    # 1. Extract status code from exception attributes if available
    if code is None:
        for attr in ("status_code", "code", "http_status"):
            val = getattr(error, attr, None)
            if isinstance(val, int):
                code = val
                break
            elif isinstance(val, str) and val.isdigit():
                code = int(val)
                break

    # 2. Extract from response attribute
    if code is None and hasattr(error, "response"):
        resp = getattr(error, "response")
        val = getattr(resp, "status_code", None)
        if isinstance(val, int):
            code = val

    # 3. Extract status code from error string via regex if still not found
    if code is None and err_str:
        match = re.search(r"\b(429|403|401|400|404|500|502|503|504)\b", err_str)
        if match:
            code = int(match.group(1))

    err_lower = err_str.lower()

    # ── RATE_LIMITED (429 / RESOURCE_EXHAUSTED) ───────────────
    if (
        code == 429
        or "resource_exhausted" in err_lower
        or "too many requests" in err_lower
        or "rate limit" in err_lower
        or "ratelimit" in err_lower
    ):
        # Extract Retry-After if present
        retry_after = 60.0  # default adaptive cooldown
        ra_match = re.search(r"retry[-_]after[:=\s]+(\d+)", err_lower)
        if ra_match:
            retry_after = float(ra_match.group(1))

        return ProviderError(
            provider=provider,
            capability=capability,
            category=ErrorCategory.RATE_LIMITED,
            status_code=429,
            retryable=False,  # FAIL FAST: do not immediately retry
            retry_after=retry_after,
            message=err_str or "Rate limit exceeded (429 Too Many Requests)"
        )

    # ── QUOTA_EXHAUSTED ────────────────────────────────────────
    if (
        (code == 403 and any(w in err_lower for w in ("quota", "exceeded", "allowance", "credit", "billing")))
        or "quota exceeded" in err_lower
        or "daily limit" in err_lower
    ):
        return ProviderError(
            provider=provider,
            capability=capability,
            category=ErrorCategory.QUOTA_EXHAUSTED,
            status_code=code or 403,
            retryable=False,
            retry_after=300.0,
            message=err_str or "API quota exhausted"
        )

    # ── AUTH_ERROR ─────────────────────────────────────────────
    if (
        code in (401, 403)
        or any(w in err_lower for w in ("unauthorized", "api key not valid", "invalid api key", "forbidden", "permission_denied"))
    ):
        return ProviderError(
            provider=provider,
            capability=capability,
            category=ErrorCategory.AUTH_ERROR,
            status_code=code or 401,
            retryable=False,
            retry_after=600.0,
            message=err_str or "Authentication or permissions failed"
        )

    # ── BAD_REQUEST ────────────────────────────────────────────
    if code in (400, 404) or "invalid_argument" in err_lower or "bad request" in err_lower:
        return ProviderError(
            provider=provider,
            capability=capability,
            category=ErrorCategory.BAD_REQUEST,
            status_code=code or 400,
            retryable=False,
            message=err_str or "Bad request"
        )

    # ── TIMEOUT ────────────────────────────────────────────────
    if (
        isinstance(error, (TimeoutError, Exception)) and "timeout" in type(error).__name__.lower()
        or "timed out" in err_lower
        or "timeout" in err_lower
    ):
        return ProviderError(
            provider=provider,
            capability=capability,
            category=ErrorCategory.TIMEOUT,
            status_code=504,
            retryable=True,
            retry_after=5.0,
            message=err_str or "Request timed out"
        )

    # ── NETWORK_ERROR ──────────────────────────────────────────
    if any(w in err_lower for w in ("connecterror", "connection refused", "dns", "network", "socket", "connection reset")):
        return ProviderError(
            provider=provider,
            capability=capability,
            category=ErrorCategory.NETWORK_ERROR,
            status_code=None,
            retryable=True,
            retry_after=10.0,
            message=err_str or "Network connection failure"
        )

    # ── SERVER_ERROR (500 / 502 / 503) ─────────────────────────
    if code in (500, 502, 503) or any(w in err_lower for w in ("internal server error", "service unavailable", "bad gateway")):
        return ProviderError(
            provider=provider,
            capability=capability,
            category=ErrorCategory.SERVER_ERROR,
            status_code=code or 500,
            retryable=True,
            retry_after=30.0,
            message=err_str or f"Server error ({code})"
        )

    return ProviderError(
        provider=provider,
        capability=capability,
        category=ErrorCategory.UNKNOWN,
        status_code=code,
        retryable=False,
        message=err_str or "Unknown error"
    )
