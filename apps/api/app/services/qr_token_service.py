"""
QR token service — the single trusted source of store/table context.

Security guarantees:
  * Raw tokens are generated with `secrets.token_urlsafe` (CSPRNG, 256 bits of
    entropy) and are NEVER stored. Only the SHA-256 hash is persisted.
  * Resolution is a constant-shape indexed lookup on the hash. Invalid, unknown,
    malformed and revoked tokens all fail the same way (return None) so callers
    can emit one indistinguishable public response.
  * The raw token is returned to the caller ONLY at issuance/rotation time so it
    can be printed onto a physical sticker exactly once.

Nothing in this module logs a raw token. Callers must log only `token_prefix`.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.store import Store
from app.models.table import Table
from app.models.table_qr_token import (
    TableQrToken,
    QR_TOKEN_STATUS_ACTIVE,
    QR_TOKEN_STATUS_REVOKED,
)

# Number of raw-token bytes of entropy. 32 bytes = 256 bits.
_TOKEN_NBYTES = 32
# Length of the non-secret prefix stored for operational support.
TOKEN_PREFIX_LEN = 8


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------

def generate_raw_token() -> str:
    """Cryptographically secure, opaque, URL-safe token. Never stored."""
    return secrets.token_urlsafe(_TOKEN_NBYTES)


def hash_token(raw_token: str) -> str:
    """Deterministic SHA-256 hex digest (64 chars) of a raw token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def token_prefix(raw_token: str) -> str:
    """Short, non-secret leading fragment used for support/listing."""
    return raw_token[:TOKEN_PREFIX_LEN]


def table_display_name(table: Table) -> str:
    """Turkish, human-facing table label derived from the table number."""
    number = (table.table_number or "").strip()
    return f"Masa {number}" if number else f"Masa #{table.id}"


# ---------------------------------------------------------------------------
# Resolved context (safe, public shape)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedQrContext:
    """Server-derived, trustworthy context. Contains no token material."""

    store_id: int
    store_name: str
    table_id: int
    table_name: str
    context_version: int = 1


# ---------------------------------------------------------------------------
# Issuance / rotation / revocation (staff-side, via CLI)
# ---------------------------------------------------------------------------

class ActiveTokenExists(Exception):
    """
    A table already has an ACTIVE token and `issue` refuses to mint a second.

    The invariant is "at most one ACTIVE token per table" (one physical table =
    one current trusted sticker). Callers should use `rotate` to replace the
    existing token instead of silently creating a competing one.
    """

    def __init__(self, table_id: int, existing_token_id: int):
        self.table_id = table_id
        self.existing_token_id = existing_token_id
        super().__init__(
            f"table_id {table_id} already has an ACTIVE token "
            f"(id={existing_token_id}); use rotate to replace it"
        )


def _lock_table(db: Session, table_id: int) -> Table:
    """
    Row-lock the parent table (SELECT … FOR UPDATE) and return it.

    All issue/rotate operations for a table serialize on this lock, so two
    concurrent issue/rotate calls can never race past the one-active-token
    check. The partial unique index is the ultimate backstop; this lock turns a
    would-be IntegrityError into deterministic, ordered behaviour.
    """
    table = db.execute(
        select(Table).where(Table.id == table_id).with_for_update()
    ).scalar_one_or_none()
    if table is None:
        raise ValueError(f"table_id {table_id} does not exist")
    return table


def _active_tokens_for_table(db: Session, table_id: int) -> list[TableQrToken]:
    return list(
        db.execute(
            select(TableQrToken).where(
                TableQrToken.table_id == table_id,
                TableQrToken.status == QR_TOKEN_STATUS_ACTIVE,
            )
        ).scalars()
    )


def _insert_active_token(
    db: Session, table_id: int, created_reason: Optional[str]
) -> tuple[TableQrToken, str]:
    """Create and flush one ACTIVE token row. Assumes the caller enforces the
    one-active-token invariant (table already locked, prior active revoked)."""
    raw = generate_raw_token()
    record = TableQrToken(
        table_id=table_id,
        token_hash=hash_token(raw),
        token_prefix=token_prefix(raw),
        status=QR_TOKEN_STATUS_ACTIVE,
        created_reason=created_reason,
    )
    db.add(record)
    db.flush()
    return record, raw


def issue_token(
    db: Session,
    table_id: int,
    *,
    created_reason: Optional[str] = None,
    commit: bool = True,
) -> tuple[TableQrToken, str]:
    """
    Mint one new ACTIVE token for a table.

    Enforces the one-active-token invariant: if the table already has an ACTIVE
    token this raises `ActiveTokenExists` (use `rotate` instead). Verifies the
    table and its store exist. Returns the persisted record and the raw token —
    the raw token is the caller's only chance to capture it.
    """
    table = _lock_table(db, table_id)
    store = db.get(Store, table.store_id)
    if store is None:
        raise ValueError(
            f"table_id {table_id} has no valid store (store_id={table.store_id})"
        )

    existing = _active_tokens_for_table(db, table_id)
    if existing:
        raise ActiveTokenExists(table_id, existing[0].id)

    record, raw = _insert_active_token(db, table_id, created_reason)
    if commit:
        db.commit()
        db.refresh(record)
    return record, raw


def rotate_token(
    db: Session,
    table_id: int,
    *,
    created_reason: Optional[str] = None,
    commit: bool = True,
) -> tuple[TableQrToken, str]:
    """
    Atomically replace a table's active token with a fresh one.

    Ordering matters because of the partial unique index (one ACTIVE per table):
      1. lock the parent table (serializes concurrent issue/rotate),
      2. flip the currently-active token(s) to REVOKED and flush — this frees
         the partial unique index *before* the new row is inserted,
      3. insert the replacement ACTIVE token,
      4. link lineage (old.replaced_by_id → new.id),
      5. commit once.

    Historical rows are preserved (never deleted). If there is no prior active
    token this still issues a new one.
    """
    from sqlalchemy.sql import func

    _lock_table(db, table_id)
    previous = _active_tokens_for_table(db, table_id)

    now = func.now()
    for old in previous:
        old.status = QR_TOKEN_STATUS_REVOKED
        old.revoked_at = now
    # Flush the revocations first so the partial unique index no longer sees an
    # ACTIVE row for this table when the replacement is inserted.
    db.flush()

    new_record, raw = _insert_active_token(
        db, table_id, created_reason or "rotate"
    )
    for old in previous:
        old.replaced_by_id = new_record.id
    db.flush()

    if commit:
        db.commit()
        db.refresh(new_record)
    return new_record, raw


def revoke_by_id(
    db: Session, token_id: int, *, commit: bool = True
) -> TableQrToken:
    """
    Revoke exactly one ACTIVE token by its database primary key.

    Destructive operations must target one unambiguous record — never a
    (possibly non-unique) display prefix. The record is never deleted; only its
    status/`revoked_at` change so history and lineage survive. Raises ValueError
    if the id does not exist or the token is not ACTIVE.
    """
    from sqlalchemy.sql import func

    token = db.execute(
        select(TableQrToken)
        .where(TableQrToken.id == token_id)
        .with_for_update()
    ).scalar_one_or_none()
    if token is None:
        raise ValueError(f"token id {token_id} does not exist")
    if token.status != QR_TOKEN_STATUS_ACTIVE:
        raise ValueError(
            f"token id {token_id} is not ACTIVE (status={token.status}); "
            "nothing to revoke"
        )
    token.status = QR_TOKEN_STATUS_REVOKED
    token.revoked_at = func.now()
    db.flush()
    if commit:
        db.commit()
    return token


def rotate_by_id(
    db: Session,
    token_id: int,
    *,
    created_reason: Optional[str] = None,
    commit: bool = True,
) -> tuple[TableQrToken, str]:
    """
    Rotate the token identified by its database primary key.

    The id must reference an ACTIVE token; its table is then rotated (the token
    is revoked and a replacement is issued with lineage). Targeting the exact
    record — not a display prefix — guarantees a rotate acts on one unambiguous
    token.
    """
    token = db.get(TableQrToken, token_id)
    if token is None:
        raise ValueError(f"token id {token_id} does not exist")
    if token.status != QR_TOKEN_STATUS_ACTIVE:
        raise ValueError(
            f"token id {token_id} is not ACTIVE (status={token.status}); "
            "nothing to rotate"
        )
    return rotate_token(
        db, token.table_id, created_reason=created_reason, commit=commit
    )


def list_tokens(db: Session) -> list[dict]:
    """
    Operational listing. Never includes raw tokens or hashes — only the
    non-secret prefix plus store/table/status/timestamps.
    """
    rows = db.execute(
        select(TableQrToken, Store, Table)
        .join(Table, TableQrToken.table_id == Table.id)
        .join(Store, Table.store_id == Store.id)
        .order_by(TableQrToken.id)
    ).all()

    out: list[dict] = []
    for token, store, table in rows:
        out.append(
            {
                "id": token.id,
                "store_id": store.id,
                "store_name": store.name,
                "table_id": table.id,
                "table_name": table_display_name(table),
                "token_prefix": token.token_prefix,
                "status": token.status,
                "created_at": token.created_at,
                "revoked_at": token.revoked_at,
                "last_used_at": token.last_used_at,
                "replaced_by_id": token.replaced_by_id,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Resolution (customer-side, request time)
# ---------------------------------------------------------------------------

def _table_is_active(table: Table) -> bool:
    # `is_active` does not exist on Table in the current schema; default True.
    # Coded defensively so adding the column later automatically takes effect.
    return bool(getattr(table, "is_active", True))


def _store_is_active(store: Store) -> bool:
    return bool(getattr(store, "is_active", True))


class QrTableUnavailable(Exception):
    """Token is valid but its table/store is not open to ordering."""


def resolve_token(
    db: Session,
    raw_token: Optional[str],
    *,
    touch: bool = True,
    for_update: bool = False,
) -> Optional[ResolvedQrContext]:
    """
    Resolve a raw token to trustworthy store/table context.

    Returns None for any invalid/unknown/malformed/revoked token (callers must
    not distinguish these publicly). Raises `QrTableUnavailable` when the token
    itself is valid and ACTIVE but the table or store is inactive.

    `for_update=True` locks the token row so a concurrent revoke/rotate during
    an in-flight order serializes behind this read (used by order creation).
    `touch=True` records `last_used_at` on a successful resolution.
    """
    from sqlalchemy.sql import func

    if not raw_token or not isinstance(raw_token, str):
        return None

    token_hash = hash_token(raw_token)

    stmt = select(TableQrToken).where(
        TableQrToken.token_hash == token_hash,
        TableQrToken.status == QR_TOKEN_STATUS_ACTIVE,
    )
    if for_update:
        stmt = stmt.with_for_update()

    token = db.execute(stmt).scalar_one_or_none()
    if token is None:
        return None

    table = db.get(Table, token.table_id)
    if table is None:
        # Relationship vanished (e.g. cascade). Fail safe — treat as invalid.
        return None
    store = db.get(Store, table.store_id)
    if store is None:
        return None

    if not _table_is_active(table) or not _store_is_active(store):
        raise QrTableUnavailable()

    if touch:
        token.last_used_at = func.now()
        db.flush()

    return ResolvedQrContext(
        store_id=store.id,
        store_name=store.name,
        table_id=table.id,
        table_name=table_display_name(table),
    )
