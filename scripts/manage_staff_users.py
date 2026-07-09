#!/usr/bin/env python
"""
Controlled CLI for staff-user administration.

This is the ONLY supported way to create or modify staff accounts until an
authenticated staff-management UI exists. No unauthenticated web API creates or
edits users. Passwords are read through getpass and never taken from argv, so
they do not land in shell history. Password hashes and token hashes are never
printed.

Usage (from the repository root):

    python scripts/manage_staff_users.py ensure-roles
    python scripts/manage_staff_users.py create --username kitchen01 --role KITCHEN --store-id 1
    python scripts/manage_staff_users.py list
    python scripts/manage_staff_users.py disable --user-id 3
    python scripts/manage_staff_users.py enable  --user-id 3
    python scripts/manage_staff_users.py reset-password --user-id 3
    python scripts/manage_staff_users.py revoke-sessions --user-id 3

Database connection and transactions come from the application configuration
(app.core.db.SessionLocal), so this uses exactly the same DB as the API.
"""
import argparse
import getpass
import os
import sys
from datetime import datetime, timezone

# Make the API package importable when run from the repo root.
_CURRENT = os.path.dirname(os.path.abspath(__file__))
_API_DIR = os.path.join(_CURRENT, "..", "apps", "api")
sys.path.insert(0, _API_DIR)

from sqlalchemy import func  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402
from app.core.permissions import CANONICAL_ROLES, is_operational_role  # noqa: E402
from app.core.security import hash_password, validate_password  # noqa: E402
from app.models.auth_session import AuthSession  # noqa: E402
from app.models.role import Role  # noqa: E402
from app.models.store import Store  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.auth_service import revoke_all_sessions  # noqa: E402


def _now():
    return datetime.now(timezone.utc)


def _prompt_password() -> str:
    """Read a password twice via getpass and validate the policy."""
    pw1 = getpass.getpass("New password: ")
    pw2 = getpass.getpass("Confirm password: ")
    if pw1 != pw2:
        raise ValueError("Passwords do not match.")
    validate_password(pw1)
    return pw1


# ── ensure-roles ─────────────────────────────────────────────────────────────

def cmd_ensure_roles(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        created = []
        for name in CANONICAL_ROLES:
            existing = db.query(Role).filter(Role.name == name).first()
            if existing is None:
                db.add(Role(name=name))
                created.append(name)
        db.commit()
        if created:
            print(f"created roles: {', '.join(created)}")
        else:
            print("all canonical roles already exist")
        print(f"canonical roles: {', '.join(CANONICAL_ROLES)}")
        return 0
    finally:
        db.close()


# ── create ───────────────────────────────────────────────────────────────────

def cmd_create(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        username = args.username.strip()
        if not username:
            print("error: username must be non-empty", file=sys.stderr)
            return 2

        role = db.query(Role).filter(Role.name == args.role).first()
        if role is None:
            print(
                f"error: role '{args.role}' does not exist. Run 'ensure-roles' first.",
                file=sys.stderr,
            )
            return 2

        if is_operational_role(role.name) and args.store_id is None:
            print(
                f"error: role '{role.name}' is operational and requires --store-id",
                file=sys.stderr,
            )
            return 2

        if args.store_id is not None:
            store = db.get(Store, args.store_id)
            if store is None:
                print(f"error: store id {args.store_id} does not exist", file=sys.stderr)
                return 2

        # Case-insensitive uniqueness.
        clash = db.query(User).filter(func.lower(User.username) == username.lower()).first()
        if clash is not None:
            print(
                f"error: a user with username '{clash.username}' already exists "
                "(case-insensitive match)",
                file=sys.stderr,
            )
            return 2

        try:
            password = _prompt_password()
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        now = _now()
        user = User(
            username=username,
            password_hash=hash_password(password),
            role_id=role.id,
            store_id=args.store_id,
            is_active=True,
            failed_login_count=0,
            password_changed_at=now,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"created user id={user.id} username={user.username} role={role.name} store_id={user.store_id}")
        return 0
    finally:
        db.close()


# ── list ─────────────────────────────────────────────────────────────────────

def cmd_list(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id).all()
        if not users:
            print("(no users)")
            return 0

        header = (
            f"{'ID':>4}  {'USERNAME':<20} {'ROLE':<9} {'STORE':>6} "
            f"{'ACTIVE':<7} {'LOCKED':<7} {'LAST_LOGIN':<26} {'SESSIONS':>8}"
        )
        print(header)
        print("-" * len(header))
        now = _now()
        for u in users:
            role = db.get(Role, u.role_id) if u.role_id else None
            active_sessions = (
                db.query(func.count(AuthSession.id))
                .filter(
                    AuthSession.user_id == u.id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
                .scalar()
                or 0
            )
            locked = bool(u.locked_until and u.locked_until.replace(tzinfo=timezone.utc) > now) \
                if u.locked_until else False
            print(
                f"{u.id:>4}  "
                f"{(u.username or '')[:20]:<20} "
                f"{(role.name if role else '?')[:9]:<9} "
                f"{str(u.store_id if u.store_id is not None else '-'):>6} "
                f"{('yes' if u.is_active else 'no'):<7} "
                f"{('yes' if locked else 'no'):<7} "
                f"{str(u.last_login_at or '-'):<26} "
                f"{active_sessions:>8}"
            )
        return 0
    finally:
        db.close()


# ── disable / enable ─────────────────────────────────────────────────────────

def cmd_disable(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        user = db.get(User, args.user_id)
        if user is None:
            print(f"error: user id {args.user_id} not found", file=sys.stderr)
            return 1
        user.is_active = False
        db.flush()
        revoked = revoke_all_sessions(db, user.id, reason="user_disabled")
        db.commit()
        print(f"disabled user id={user.id}; revoked {revoked} active session(s)")
        return 0
    finally:
        db.close()


def cmd_enable(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        user = db.get(User, args.user_id)
        if user is None:
            print(f"error: user id {args.user_id} not found", file=sys.stderr)
            return 1
        user.is_active = True
        user.failed_login_count = 0
        user.locked_until = None
        db.commit()
        print(f"enabled user id={user.id}")
        return 0
    finally:
        db.close()


# ── reset-password ───────────────────────────────────────────────────────────

def cmd_reset_password(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        user = db.get(User, args.user_id)
        if user is None:
            print(f"error: user id {args.user_id} not found", file=sys.stderr)
            return 1
        try:
            password = _prompt_password()
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        user.password_hash = hash_password(password)
        user.password_changed_at = _now()
        user.failed_login_count = 0
        user.locked_until = None
        db.flush()
        revoked = revoke_all_sessions(db, user.id, reason="password_reset")
        db.commit()
        print(f"reset password for user id={user.id}; revoked {revoked} active session(s)")
        return 0
    finally:
        db.close()


# ── revoke-sessions ──────────────────────────────────────────────────────────

def cmd_revoke_sessions(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        user = db.get(User, args.user_id)
        if user is None:
            print(f"error: user id {args.user_id} not found", file=sys.stderr)
            return 1
        revoked = revoke_all_sessions(db, user.id, reason="manual_revoke")
        print(f"revoked {revoked} active session(s) for user id={user.id}")
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage SweetOps staff users.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ensure-roles", help="Create missing canonical roles idempotently")
    p.set_defaults(func=cmd_ensure_roles)

    p = sub.add_parser("create", help="Create a staff user (password read via getpass)")
    p.add_argument("--username", required=True)
    p.add_argument("--role", required=True, choices=CANONICAL_ROLES)
    p.add_argument("--store-id", type=int, default=None)
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("list", help="List staff users (safe fields only)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("disable", help="Disable a user and revoke all their sessions")
    p.add_argument("--user-id", type=int, required=True)
    p.set_defaults(func=cmd_disable)

    p = sub.add_parser("enable", help="Re-enable a disabled user")
    p.add_argument("--user-id", type=int, required=True)
    p.set_defaults(func=cmd_enable)

    p = sub.add_parser("reset-password", help="Reset a password and revoke all sessions")
    p.add_argument("--user-id", type=int, required=True)
    p.set_defaults(func=cmd_reset_password)

    p = sub.add_parser("revoke-sessions", help="Revoke all active sessions for a user")
    p.add_argument("--user-id", type=int, required=True)
    p.set_defaults(func=cmd_revoke_sessions)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
