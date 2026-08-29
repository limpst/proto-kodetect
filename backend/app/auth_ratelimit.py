"""로그인 브루트포스 방어 — IP 단위 실패 카운트 + 잠금."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import settings


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = 0.0
    history: list[float] = field(default_factory=list)


_LOCK = threading.Lock()
_ENTRIES: dict[str, _Entry] = {}


def check(ip: str) -> tuple[bool, int]:
    """(허용여부, 남은 잠금 초)."""
    now = time.time()
    with _LOCK:
        e = _ENTRIES.get(ip)
        if e and e.locked_until > now:
            return False, int(e.locked_until - now) + 1
    return True, 0


def record_failure(ip: str) -> tuple[bool, int]:
    """실패 1건 기록. (이번에 잠겼는지, 잠금 초)."""
    now = time.time()
    with _LOCK:
        e = _ENTRIES.setdefault(ip, _Entry())
        e.failures += 1
        e.history.append(now)
        e.history = [t for t in e.history if now - t < settings.login_lockout_sec]
        if e.failures >= settings.login_max_attempts:
            e.locked_until = now + settings.login_lockout_sec
            e.failures = 0
            return True, settings.login_lockout_sec
    return False, 0


def record_success(ip: str) -> None:
    with _LOCK:
        _ENTRIES.pop(ip, None)


def state() -> dict:
    now = time.time()
    with _LOCK:
        return {
            "max_attempts": settings.login_max_attempts,
            "lockout_sec": settings.login_lockout_sec,
            "locked": [
                {"ip": ip, "retry_after": int(e.locked_until - now) + 1}
                for ip, e in _ENTRIES.items()
                if e.locked_until > now
            ],
            "tracked": len(_ENTRIES),
        }
