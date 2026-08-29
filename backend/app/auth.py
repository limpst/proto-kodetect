"""쿠키 기반 세션 인증 — HMAC-SHA256 서명 토큰.

토큰 포맷: base64url(json_payload) "." base64url(HMAC_SHA256(secret, body))
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request, Response, status

from .config import settings

COOKIE_NAME = "kodetect_session"


def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=")
    sig = hmac.new(settings.session_secret.encode(), body, hashlib.sha256).digest()
    return f"{body.decode()}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def _verify(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    try:
        body_b, sig_b = token.split(".", 1)
        expected = hmac.new(
            settings.session_secret.encode(), body_b.encode(), hashlib.sha256
        ).digest()
        got = base64.urlsafe_b64decode(sig_b + "=" * (-len(sig_b) % 4))
        if not hmac.compare_digest(got, expected):
            return None
        payload = json.loads(
            base64.urlsafe_b64decode(body_b + "=" * (-len(body_b) % 4)).decode()
        )
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def validate_credentials(user: str, password: str) -> bool:
    if not settings.auth_password:
        return False
    ok_u = hmac.compare_digest((user or "").encode(), settings.auth_user.encode())
    ok_p = hmac.compare_digest(
        (password or "").encode(), settings.auth_password.encode()
    )
    return ok_u and ok_p


def issue_session(response: Response, user: str) -> str:
    now = int(time.time())
    token = _sign({"user": user, "iat": now, "exp": now + settings.session_max_age_sec})
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.session_max_age_sec,
        path="/",
    )
    return token


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def get_current_user(request: Request) -> dict | None:
    if not settings.auth_enabled:
        return {"user": "anonymous", "auth_disabled": True}
    return _verify(request.cookies.get(COOKIE_NAME, ""))


def require_user(request: Request) -> dict:
    u = get_current_user(request)
    if not u:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="로그인이 필요합니다",
            headers={"X-Auth-Redirect": "/login"},
        )
    return u


PUBLIC_PREFIXES = (
    "/login",
    "/api/auth/",
    "/static/",
    "/healthz",
    "/docs",
    "/openapi.json",
    "/redoc",
    "/favicon.ico",
)


def is_public(path: str) -> bool:
    return not settings.auth_enabled or path.startswith(PUBLIC_PREFIXES)
