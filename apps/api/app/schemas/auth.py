from typing import List, Optional

from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class StoreSummary(BaseModel):
    id: int
    name: str


class StaffProfile(BaseModel):
    """Safe profile returned by /auth/login and /auth/me. Never includes any
    credential material (no password hash, no session/CSRF token)."""

    id: int
    username: str
    role: str
    store: Optional[StoreSummary] = None
    permissions: List[str] = []


class LogoutResponse(BaseModel):
    ok: bool = True
