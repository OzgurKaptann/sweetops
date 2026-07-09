"""
Staff authentication API.

  POST /auth/login       — verify credentials, open a session, set cookies.
  GET  /auth/me          — current staff profile (no-store).
  POST /auth/logout      — revoke current session, clear cookies (idempotent).
  POST /auth/logout-all  — revoke every session for the current user.

Cookie-based auth: all responses that expose identity set Cache-Control:
no-store. State-changing routes require CSRF + trusted origin (see deps).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import HTTPException
from sqlalchemy.orm import Session

from app.core import messages
from app.core.config import settings
from app.core.cookies import clear_auth_cookies, set_auth_cookies
from app.core.db import get_db
from app.core.deps import enforce_csrf, enforce_origin, get_current_staff
from app.models.store import Store
from app.schemas.auth import LoginRequest, LogoutResponse, StaffProfile, StoreSummary
from app.services import auth_service
from app.services.audit_service import audit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _profile(db: Session, staff) -> StaffProfile:
    store = None
    if staff.store_id is not None:
        row = db.get(Store, staff.store_id)
        if row is not None:
            store = StoreSummary(id=row.id, name=row.name)
    return StaffProfile(
        id=staff.user_id,
        username=staff.username,
        role=staff.role,
        store=store,
        permissions=list(staff.permissions),
    )


@router.post("/login", response_model=StaffProfile)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> StaffProfile:
    _no_store(response)

    # Login-CSRF reduction: reject untrusted browser origins before touching
    # credentials.
    enforce_origin(request)

    if not body.username or not body.username.strip() or not body.password:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_fields", "message": messages.AUTH_MISSING_FIELDS},
        )

    try:
        user = auth_service.authenticate(db, body.username, body.password)
    except auth_service.AccountLocked as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "account_locked", "message": exc.message},
        )
    except auth_service.LoginError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_credentials", "message": exc.message},
        )

    user_agent = request.headers.get("user-agent")
    session, raw_token, raw_csrf = auth_service.create_session(db, user, user_agent)
    set_auth_cookies(response, raw_token, raw_csrf)

    audit(
        db,
        entity_type="user",
        entity_id=user.id,
        action="login",
        actor_type="STAFF",
        actor_id=str(user.id),
        payload_after={"session_id": session.id, "role_id": user.role_id},
    )
    db.commit()

    resolved = auth_service.resolve_session(db, raw_token)
    staff = auth_service.build_context(*resolved)
    return _profile(db, staff)


@router.get("/me", response_model=StaffProfile)
def me(
    response: Response,
    staff=Depends(get_current_staff),
    db: Session = Depends(get_db),
) -> StaffProfile:
    _no_store(response)
    return _profile(db, staff)


@router.post("/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LogoutResponse:
    """
    Idempotent logout. If a valid session exists, its CSRF token must be
    presented and the session is revoked. Cookies are always cleared.
    """
    _no_store(response)

    raw_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    resolved = auth_service.resolve_session(db, raw_token) if raw_token else None

    if resolved is not None:
        session, user, role = resolved
        staff = auth_service.build_context(session, user, role)
        enforce_origin(request)
        enforce_csrf(request, staff)
        auth_service.revoke_session(db, session, reason="logout")
        audit(
            db,
            entity_type="user",
            entity_id=user.id,
            action="logout",
            actor_type="STAFF",
            actor_id=str(user.id),
            payload_after={"session_id": session.id},
        )
        db.commit()

    clear_auth_cookies(response)
    return LogoutResponse(ok=True)


@router.post("/logout-all", response_model=LogoutResponse)
def logout_all(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> LogoutResponse:
    _no_store(response)

    raw_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    resolved = auth_service.resolve_session(db, raw_token) if raw_token else None
    if resolved is None:
        # Not authenticated — clear cookies and behave idempotently.
        clear_auth_cookies(response)
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": messages.AUTH_SESSION_EXPIRED},
        )

    session, user, role = resolved
    staff = auth_service.build_context(session, user, role)
    enforce_origin(request)
    enforce_csrf(request, staff)

    count = auth_service.revoke_all_sessions(db, user.id, reason="logout_all")
    audit(
        db,
        entity_type="user",
        entity_id=user.id,
        action="logout_all",
        actor_type="STAFF",
        actor_id=str(user.id),
        payload_after={"revoked_count": count},
    )
    db.commit()

    clear_auth_cookies(response)
    return LogoutResponse(ok=True)
