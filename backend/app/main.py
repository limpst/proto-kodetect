"""KO-Detect 애플리케이션 진입점."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import get_current_user, is_public
from .config import settings
from .db import init_db
from .routers import auth as auth_router
from .routers import bhc as bhc_router
from .routers import buildings as buildings_router
from .routers import detect as detect_router
from .routers import live as live_router
from .routers import policy as policy_router
from .routers import reports as reports_router

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from .seed import seed_if_empty

    seed_if_empty()
    yield


app = FastAPI(
    title=f"{settings.app_name} — {settings.app_title_ko}",
    version=settings.version,
    lifespan=lifespan,
)


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """미인증 요청 차단 — 페이지는 로그인으로, API는 401 JSON."""
    path = request.url.path
    if is_public(path) or path.startswith("/ws"):
        return await call_next(request)
    if get_current_user(request):
        return await call_next(request)
    if path.startswith("/api/") or path.startswith("/media/"):
        return JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
    return RedirectResponse(f"/login?next={path}", status_code=302)


app.include_router(auth_router.router)
app.include_router(bhc_router.router)
app.include_router(buildings_router.router)
app.include_router(detect_router.router)
app.include_router(live_router.router)
app.include_router(policy_router.router)
app.include_router(reports_router.router)

app.mount("/static", StaticFiles(directory=FRONTEND / "static"), name="static")
app.mount(
    "/media/uploads", StaticFiles(directory=settings.uploads_dir), name="uploads"
)
app.mount(
    "/media/overlays", StaticFiles(directory=settings.overlays_dir), name="overlays"
)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "app": settings.app_name, "version": settings.version}


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(FRONTEND / "login.html")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND / "app.html")
