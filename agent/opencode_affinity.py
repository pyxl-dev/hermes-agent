"""Stable OpenCode relay session affinity.

OpenCode Go/Zen route requests by the ``x-opencode-session`` header. Keep the
value stable for one Hermes conversation so provider fallback and context
compression do not lose backend affinity or fail on routes that require it.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

OPENCODE_SESSION_HEADER = "x-opencode-session"


def is_opencode_target(provider: Optional[str], base_url: Optional[str]) -> bool:
    provider_name = str(provider or "").strip().lower()
    if provider_name.startswith("opencode"):
        return True

    try:
        hostname = (urlparse(str(base_url or "")).hostname or "").lower()
    except Exception:
        hostname = ""
    return hostname == "opencode.ai" or hostname.endswith(".opencode.ai")


def _ambient_session_id() -> str:
    try:
        from gateway.session_context import get_session_env

        return (
            get_session_env("HERMES_SESSION_ID", "")
            or get_session_env("HERMES_SESSION_KEY", "")
            or ""
        ).strip()
    except Exception:
        return ""


def opencode_session_headers(
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, str]:
    if not is_opencode_target(provider, base_url):
        return {}

    key = str(session_id or "").strip() or _ambient_session_id()
    return {OPENCODE_SESSION_HEADER: key} if key else {}


def merge_opencode_session_headers(
    kwargs: dict[str, Any],
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    headers = opencode_session_headers(provider, base_url, session_id)
    if not headers:
        return kwargs

    existing = kwargs.get("extra_headers")
    merged = dict(existing) if isinstance(existing, dict) else {}
    for key, value in headers.items():
        merged.setdefault(key, value)
    kwargs["extra_headers"] = merged
    return kwargs
