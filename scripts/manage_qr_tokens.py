#!/usr/bin/env python
"""
Controlled CLI for issuing, rotating, revoking and listing QR table tokens.

Raw tokens cannot be recovered after hashing, so token issuance happens here —
never inside a database migration. Each raw token is printed EXACTLY ONCE, at
the moment it is created. Existing raw tokens can never be printed again; only
the non-secret prefix is ever shown afterwards.

Destructive operations (revoke / rotate) target the token record's database
primary key (`--token-id`), never the human-facing `token_prefix` — a prefix is
a support/display value and is not guaranteed unique, so it must never be the
sole selector for a destructive action.

Usage (from the repository root):

    python scripts/manage_qr_tokens.py issue  --table-id 5
    python scripts/manage_qr_tokens.py rotate --token-id 42
    python scripts/manage_qr_tokens.py revoke --token-id 42
    python scripts/manage_qr_tokens.py list

Use `list` to find a token's id (it shows id, store, table, prefix, status and
timestamps). Database connection and transactions come from the application
configuration (app.core.db.SessionLocal), so this uses exactly the same DB as
the API.
"""
import argparse
import os
import sys

# Make the API package importable when run from the repo root.
_CURRENT = os.path.dirname(os.path.abspath(__file__))
_API_DIR = os.path.join(_CURRENT, "..", "apps", "api")
sys.path.insert(0, _API_DIR)

from app.core.config import settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.services.qr_token_service import (  # noqa: E402
    issue_token,
    rotate_by_id,
    revoke_by_id,
    list_tokens,
    ActiveTokenExists,
)


def _customer_url(raw_token: str) -> str:
    # The raw token is delivered in the URL *fragment* (`#qr=…`), never a query
    # string. A fragment is not sent to the server on the initial request, so
    # this long-lived bearer token cannot leak into web-server, proxy, CDN or
    # platform access logs the way a `?qr=` query parameter would.
    base = settings.CUSTOMER_WEB_BASE_URL.rstrip("/")
    return f"{base}/#qr={raw_token}"


def _print_issued(record, raw_token: str, *, label: str) -> None:
    """Print a freshly-minted token exactly once."""
    print("=" * 64)
    print(f"{label} — SAVE THIS NOW. The raw token is shown only once.")
    print("=" * 64)
    print(f"  token record id : {record.id}")
    print(f"  table id        : {record.table_id}")
    print(f"  token prefix    : {record.token_prefix}")
    print(f"  raw token       : {raw_token}")
    print(f"  customer url     : {_customer_url(raw_token)}")
    print("=" * 64)


def cmd_issue(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        record, raw = issue_token(
            db, args.table_id, created_reason=args.reason or "issue"
        )
        _print_issued(record, raw, label="ISSUED QR TOKEN")
        return 0
    except ActiveTokenExists as exc:
        print(
            f"error: {exc}\n"
            f"       run: rotate --token-id {exc.existing_token_id}",
            file=sys.stderr,
        )
        db.rollback()
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        db.rollback()
        return 2
    finally:
        db.close()


def cmd_rotate(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        record, raw = rotate_by_id(
            db, args.token_id, created_reason=args.reason or "rotate"
        )
        _print_issued(record, raw, label="ROTATED QR TOKEN (previous revoked)")
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        db.rollback()
        return 2
    finally:
        db.close()


def cmd_revoke(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        row = revoke_by_id(db, args.token_id)
        print(
            f"revoked token id={row.id} table_id={row.table_id} "
            f"prefix={row.token_prefix}"
        )
        return 0
    except ValueError as exc:
        # e.g. unknown id, or the token is already REVOKED. A destructive op
        # that matched no exact record makes no change and reports why.
        print(f"error: {exc}", file=sys.stderr)
        db.rollback()
        return 1
    finally:
        db.close()


def cmd_list(args: argparse.Namespace) -> int:
    db = SessionLocal()
    try:
        rows = list_tokens(db)
        if not rows:
            print("(no tokens)")
            return 0
        header = (
            f"{'ID':>4}  {'STORE':<20} {'TABLE':<10} {'PREFIX':<10} "
            f"{'STATUS':<8} {'CREATED':<26} {'REVOKED':<26} {'LAST_USED':<26}"
        )
        print(header)
        print("-" * len(header))
        for r in rows:
            print(
                f"{r['id']:>4}  "
                f"{(r['store_name'] or '')[:20]:<20} "
                f"{r['table_name'][:10]:<10} "
                f"{r['token_prefix']:<10} "
                f"{r['status']:<8} "
                f"{str(r['created_at']):<26} "
                f"{str(r['revoked_at'] or '-'):<26} "
                f"{str(r['last_used_at'] or '-'):<26}"
            )
        return 0
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage revocable QR table tokens for SweetOps."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue", help="Issue a new token for a table")
    p_issue.add_argument("--table-id", type=int, required=True)
    p_issue.add_argument("--reason", type=str, default=None)
    p_issue.set_defaults(func=cmd_issue)

    p_rotate = sub.add_parser(
        "rotate",
        help="Rotate a token by its database id (revokes it, issues a replacement)",
    )
    p_rotate.add_argument("--token-id", type=int, required=True)
    p_rotate.add_argument("--reason", type=str, default=None)
    p_rotate.set_defaults(func=cmd_rotate)

    p_revoke = sub.add_parser(
        "revoke", help="Revoke exactly one ACTIVE token by its database id"
    )
    p_revoke.add_argument("--token-id", type=int, required=True)
    p_revoke.set_defaults(func=cmd_revoke)

    p_list = sub.add_parser("list", help="List tokens (never prints raw tokens)")
    p_list.set_defaults(func=cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
