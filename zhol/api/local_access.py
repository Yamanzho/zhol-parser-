"""Localhost session for the macOS panel. Production stays on the bearer token."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from zhol.api.admin_actions import require_action_header

COOKIE = "zhol_local_session"
_TTL_SECONDS = 12 * 60 * 60
_MAX_SESSIONS = 32
_sessions: dict[str, float] = {}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "api"}


def local_access_enabled() -> bool:
    return os.environ.get("ZHOL_LOCAL_ACCESS", "").strip() == "1" and bool(_gate())


def _gate() -> str:
    return os.environ.get("ZHOL_LOCAL_GATE", "").strip()


def _same(supplied: str, expected: str) -> bool:
    if not supplied or not expected or len(supplied) > 400:
        return False
    return hmac.compare_digest(
        hashlib.sha256(supplied.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _peer(request: Request) -> str:
    if request.client is None:
        return ""
    return request.client.host


def _private_or_loopback(peer: str) -> bool:
    if peer in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return True
    return peer.startswith(("10.", "192.168.", "172."))


def host_is_local(request: Request) -> bool:
    raw = request.headers.get("host", "")
    name = raw.split(":")[0].strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    peer = _peer(request)
    if name == "testserver":
        return peer == "testclient"
    if name not in _LOCAL_HOSTS:
        return False
    return _private_or_loopback(peer)


def gate_matches(request: Request) -> bool:
    return _same(request.headers.get("x-zhol-local-gate", ""), _gate())


def reset_local_sessions() -> None:
    _sessions.clear()


def _prune(now: float) -> None:
    expired = [key for key, seen in _sessions.items() if now - seen > _TTL_SECONDS]
    for key in expired:
        _sessions.pop(key, None)
    while len(_sessions) >= _MAX_SESSIONS:
        oldest = min(_sessions, key=_sessions.get)
        _sessions.pop(oldest, None)


def issue_session() -> str:
    now = time.time()
    _prune(now)
    session_id = secrets.token_urlsafe(32)
    _sessions[session_id] = now
    return session_id


def drop_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def session_valid(session_id: str) -> bool:
    seen = _sessions.get(session_id)
    if seen is None:
        return False
    if time.time() - seen > _TTL_SECONDS:
        _sessions.pop(session_id, None)
        return False
    return True


def cookie_authenticates(request: Request | None) -> bool:
    if request is None or not local_access_enabled() or not host_is_local(request):
        return False
    raw = request.cookies.get(COOKIE, "")
    return bool(raw) and session_valid(raw)


def require_local_mode() -> None:
    if not local_access_enabled():
        raise HTTPException(status_code=404, detail="not found")


def open_local_session(
    request: Request,
    _mode: None = Depends(require_local_mode),
    _header: None = Depends(require_action_header),
) -> Response:
    if not os.environ.get("ZHOL_ADMIN_TOKEN", "").strip():
        raise HTTPException(status_code=503, detail="admin api is disabled")
    if not gate_matches(request) or not host_is_local(request):
        raise HTTPException(status_code=403, detail="remote access denied")
    session_id = issue_session()
    response = JSONResponse({"local_access": True})
    response.set_cookie(
        COOKIE,
        session_id,
        max_age=_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


def close_local_session(
    request: Request,
    _mode: None = Depends(require_local_mode),
    _header: None = Depends(require_action_header),
) -> Response:
    if not host_is_local(request):
        raise HTTPException(status_code=403, detail="remote access denied")
    drop_session(request.cookies.get(COOKIE, ""))
    response = JSONResponse({"local_access": False})
    response.delete_cookie(COOKIE, path="/")
    return response


router = APIRouter(prefix="/api/v1/admin", tags=["admin-local"])
router.add_api_route("/local-session", open_local_session, methods=["POST"])
router.add_api_route("/local-session/logout", close_local_session, methods=["POST"])
