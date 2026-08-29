"""로그인 / 로그아웃 / 세션 확인."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from .. import auth_ratelimit as rl
from ..auth import clear_session, get_current_user, issue_session, validate_credentials
from ..config import settings
from ..schemas import LoginIn, SessionOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return (request.client.host if request.client else "") or "unknown"


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    if not settings.auth_enabled:
        issue_session(response, "anonymous")
        return {"ok": True, "auth_disabled": True, "user": "anonymous"}

    ip = _client_ip(request)
    ok, retry = rl.check(ip)
    if not ok:
        raise HTTPException(
            429,
            f"로그인 시도가 너무 많습니다. {retry}초 후 다시 시도하세요.",
            headers={"Retry-After": str(retry)},
        )

    if not validate_credentials(body.username, body.password):
        locked, lock_retry = rl.record_failure(ip)
        detail = "사용자명 또는 비밀번호가 잘못되었습니다"
        if locked:
            detail += f" — {lock_retry}초간 차단됩니다"
        raise HTTPException(401, detail)

    rl.record_success(ip)
    issue_session(response, body.username)
    return {"ok": True, "user": body.username}


@router.post("/logout")
def logout(response: Response) -> dict:
    clear_session(response)
    return {"ok": True}


@router.get("/me", response_model=SessionOut)
def me(request: Request) -> SessionOut:
    u = get_current_user(request)
    return SessionOut(
        authenticated=bool(u),
        auth_enabled=settings.auth_enabled,
        user=(u or {}).get("user"),
        exp=(u or {}).get("exp"),
    )


@router.get("/ratelimit")
def ratelimit_state() -> dict:
    return rl.state()
