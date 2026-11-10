"""HTTP security headers and request guards shared by all dashboard routes."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

BASE_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

LOGIN_CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://t.me https://*.telegram.org; "
    "frame-src https://oauth.telegram.org; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

CSRF_HEADER = "x-liqguard-csrf"


def apply_security_headers(response, path: str) -> None:
    response.headers["Content-Security-Policy"] = LOGIN_CSP if path == "/login" else BASE_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"


def same_origin(origin: Optional[str], host: Optional[str]) -> bool:
    """True if the Origin header (when present) names the host that served the request."""
    if not origin:
        return True
    if origin == "null" or not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


def require_csrf(request: Request) -> None:
    """
    State-changing requests must carry a custom header (which cross-site
    forms cannot send and cross-site fetch cannot send without a CORS
    preflight we never grant) and, if present, a same-origin Origin.
    """
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail="Missing CSRF header")
    if not same_origin(request.headers.get("origin"), request.headers.get("host")):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")
