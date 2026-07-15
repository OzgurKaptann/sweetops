"""
Cashier Shift Service — open a till, close it, reconcile it against the ledger.

A shift is a RECONCILIATION over the append-only payment ledger. This service
NEVER mutates a settlement, a refund, an order or inventory. At close time it
takes a read-only SNAPSHOT of what the ledger says happened during the shift
window and freezes it onto the shift row.

Guarantees
----------
1. Store and cashier come only from the authenticated session, never the body.
2. Money is Decimal end-to-end, quantised to 2 places. Never float.
3. Opening and closing each require an Idempotency-Key. Only SHA-256(key) and
   SHA-256(canonical payload) are stored. Same key + same payload replays the
   original result; same key + different payload → 409.
4. Attribution: store_id + cashier_user_id + window ``opened_at <= t < closed_at``.
   Payments are the settlements this cashier collected; refunds are refunds of
   money this cashier collected (join through the settlement) — the figure the
   physical drawer actually loses, regardless of who pressed the refund button.
5. A CLOSED shift is frozen by a DB trigger, so a payment recorded after the close
   can never change the snapshot and a closed shift can never be reopened.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import messages
from app.core.permissions import PERM_OWNER_READ
from app.models.cashier_shift import CashierShift, SHIFT_CLOSED, SHIFT_OPEN
from app.models.payment_refund import PaymentRefund
from app.models.payment_settlement import PaymentSettlement
from app.models.user import User
from app.schemas.cashier_shift import (
    ShiftCloseRequest,
    ShiftListResponse,
    ShiftOpenRequest,
    ShiftResponse,
)
from app.services.audit_service import audit
from app.services.auth_service import CurrentStaff

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")


# ── Small helpers ─────────────────────────────────────────────────────────────

def q2(value) -> Decimal:
    return Decimal(str(value if value is not None else "0")).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _require_key(idempotency_key: Optional[str]) -> str:
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "idempotency_required", "message": messages.SHIFT_IDEMPOTENCY_REQUIRED},
        )
    return idempotency_key.strip()


def _conflict(message: str, error: str = "conflict") -> HTTPException:
    return HTTPException(status_code=409, detail={"error": error, "message": message})


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": "not_found", "message": messages.SHIFT_NOT_FOUND}
    )


def _forbidden() -> HTTPException:
    return HTTPException(
        status_code=403, detail={"error": "forbidden", "message": messages.AUTH_FORBIDDEN}
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cashier_display(db: Session, user_id: int) -> str:
    user = db.get(User, user_id)
    return user.username if user else f"user:{user_id}"


# ── Response builder ──────────────────────────────────────────────────────────

def build_shift_response(db: Session, shift: CashierShift, *, replay: bool = False) -> ShiftResponse:
    return ShiftResponse(
        id=shift.id,
        store_id=shift.store_id,
        cashier_user_id=shift.cashier_user_id,
        cashier_display=_cashier_display(db, shift.cashier_user_id),
        status=shift.status,
        opened_at=shift.opened_at,
        closed_at=shift.closed_at,
        opening_cash_amount=q2(shift.opening_cash_amount),
        open_note=shift.open_note,
        close_note=shift.close_note,
        counted_closing_cash_amount=(
            q2(shift.counted_closing_cash_amount)
            if shift.counted_closing_cash_amount is not None else None
        ),
        cash_payments_amount=_opt(shift.cash_payments_amount),
        cash_refunds_amount=_opt(shift.cash_refunds_amount),
        expected_closing_cash_amount=_opt(shift.expected_closing_cash_amount),
        cash_discrepancy_amount=_opt(shift.cash_discrepancy_amount),
        card_payments_amount=_opt(shift.card_payments_amount),
        card_refunds_amount=_opt(shift.card_refunds_amount),
        gross_payments_amount=_opt(shift.gross_payments_amount),
        total_refunds_amount=_opt(shift.total_refunds_amount),
        net_collected_amount=_opt(shift.net_collected_amount),
        idempotent_replay=replay,
    )


def _opt(v) -> Optional[Decimal]:
    return q2(v) if v is not None else None


# ── Ledger snapshot ───────────────────────────────────────────────────────────

def compute_shift_totals(
    db: Session, *, store_id: int, cashier_user_id: int, opened_at: datetime, closed_at: datetime
) -> dict:
    """
    Read-only snapshot of the ledger for one shift window. See module docstring
    for the attribution rule. scripts/reconcile_payments.py re-derives these with
    the SAME query, so the two must never drift.
    """
    # Payments this cashier collected in the window, grouped by method.
    pay_rows = db.execute(
        select(
            PaymentSettlement.payment_method,
            func.coalesce(func.sum(PaymentSettlement.gross_amount), 0),
        )
        .where(
            PaymentSettlement.store_id == store_id,
            PaymentSettlement.cashier_user_id == cashier_user_id,
            PaymentSettlement.status == "COMPLETED",
            PaymentSettlement.completed_at >= opened_at,
            PaymentSettlement.completed_at < closed_at,
        )
        .group_by(PaymentSettlement.payment_method)
    ).all()

    # Refunds of THIS cashier's money, in the window, grouped by the original
    # settlement's method (the drawer loses cash for a cash sale reversed).
    ref_rows = db.execute(
        select(
            PaymentSettlement.payment_method,
            func.coalesce(func.sum(PaymentRefund.amount), 0),
        )
        .join(PaymentSettlement, PaymentSettlement.id == PaymentRefund.settlement_id)
        .where(
            PaymentRefund.store_id == store_id,
            PaymentSettlement.cashier_user_id == cashier_user_id,
            PaymentRefund.created_at >= opened_at,
            PaymentRefund.created_at < closed_at,
        )
        .group_by(PaymentSettlement.payment_method)
    ).all()

    payments = {method: q2(total) for method, total in pay_rows}
    refunds = {method: q2(total) for method, total in ref_rows}

    cash_payments = payments.get("CASH", ZERO)
    card_payments = payments.get("CARD", ZERO)
    cash_refunds = refunds.get("CASH", ZERO)
    card_refunds = refunds.get("CARD", ZERO)

    # Gross / total include EVERY method (CASH, CARD and any OTHER), so a future
    # payment method flows into the money totals without inventing a UI label.
    gross_payments = q2(sum(payments.values(), ZERO))
    total_refunds = q2(sum(refunds.values(), ZERO))

    return {
        "cash_payments_amount": cash_payments,
        "card_payments_amount": card_payments,
        "cash_refunds_amount": cash_refunds,
        "card_refunds_amount": card_refunds,
        "gross_payments_amount": gross_payments,
        "total_refunds_amount": total_refunds,
        "net_collected_amount": q2(gross_payments - total_refunds),
    }


# ── Lookups ───────────────────────────────────────────────────────────────────

def _find_open_shift(db: Session, store_id: int, cashier_user_id: int) -> Optional[CashierShift]:
    return (
        db.query(CashierShift)
        .filter(
            CashierShift.store_id == store_id,
            CashierShift.cashier_user_id == cashier_user_id,
            CashierShift.status == SHIFT_OPEN,
        )
        .first()
    )


def _find_by_open_key(db: Session, store_id: int, key_hash: str) -> Optional[CashierShift]:
    return (
        db.query(CashierShift)
        .filter(
            CashierShift.store_id == store_id,
            CashierShift.opened_idempotency_key_hash == key_hash,
        )
        .first()
    )


# ── Open ──────────────────────────────────────────────────────────────────────

def open_shift(
    db: Session,
    staff: CurrentStaff,
    req: ShiftOpenRequest,
    *,
    idempotency_key: Optional[str],
    ip_address: Optional[str] = None,
) -> ShiftResponse:
    key = _require_key(idempotency_key)

    opening = q2(req.opening_cash_amount)
    if opening < 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "opening_cash_invalid", "message": messages.SHIFT_OPENING_CASH_INVALID},
        )
    open_note = (req.open_note or "").strip() or None

    canonical = _canonical({
        "cmd": "shift_open",
        "opening_cash": str(opening),
        "open_note": open_note or "",
    })
    key_hash = _sha256(key)
    request_hash = _sha256(canonical)

    # Fast idempotency path: same opening key already used in this store.
    existing = _find_by_open_key(db, staff.store_id, key_hash)
    if existing is not None:
        return _resolve_open_replay(db, existing, request_hash)

    # An open shift already exists for this cashier at this store: return it rather
    # than creating a second one (which the partial unique index would refuse).
    already_open = _find_open_shift(db, staff.store_id, staff.user_id)
    if already_open is not None:
        return build_shift_response(db, already_open, replay=False)

    try:
        shift = CashierShift(
            store_id=staff.store_id,
            cashier_user_id=staff.user_id,
            status=SHIFT_OPEN,
            opening_cash_amount=opening,
            open_note=open_note,
            opened_idempotency_key_hash=key_hash,
            opened_request_hash=request_hash,
        )
        db.add(shift)
        db.flush()  # shift.id, opened_at

        audit(
            db,
            entity_type="cashier_shift",
            entity_id=shift.id,
            action="CASHIER_SHIFT_OPENED",
            actor_type="STAFF",
            actor_id=str(staff.user_id),
            ip_address=ip_address,
            payload_after={
                "shift_id": shift.id,
                "store_id": shift.store_id,
                "cashier_user_id": shift.cashier_user_id,
                "opening_cash_amount": opening,
                "opened_at": shift.opened_at,
            },
        )

        db.commit()
        db.refresh(shift)
        logger.info(
            "cashier_shift_opened shift=%s store=%s cashier=%s opening=%s",
            shift.id, staff.store_id, staff.user_id, opening,
        )
        return build_shift_response(db, shift, replay=False)

    except IntegrityError:
        # Either a concurrent identical open key, or a concurrent open shift for
        # this cashier committed between our checks and the insert.
        db.rollback()
        existing = _find_by_open_key(db, staff.store_id, key_hash)
        if existing is not None:
            return _resolve_open_replay(db, existing, request_hash)
        already_open = _find_open_shift(db, staff.store_id, staff.user_id)
        if already_open is not None:
            return build_shift_response(db, already_open, replay=False)
        raise
    except HTTPException:
        db.rollback()
        raise


def _resolve_open_replay(db: Session, existing: CashierShift, request_hash: str) -> ShiftResponse:
    if existing.opened_request_hash != request_hash:
        raise _conflict(messages.SHIFT_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch")
    return build_shift_response(db, existing, replay=True)


# ── Current ───────────────────────────────────────────────────────────────────

def get_current_shift(db: Session, staff: CurrentStaff) -> Optional[ShiftResponse]:
    shift = _find_open_shift(db, staff.store_id, staff.user_id)
    if shift is None:
        return None
    return build_shift_response(db, shift, replay=False)


# ── Close ─────────────────────────────────────────────────────────────────────

def close_shift(
    db: Session,
    staff: CurrentStaff,
    shift_id: int,
    req: ShiftCloseRequest,
    *,
    idempotency_key: Optional[str],
    ip_address: Optional[str] = None,
) -> ShiftResponse:
    key = _require_key(idempotency_key)

    counted = q2(req.counted_closing_cash_amount)
    if counted < 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "counted_cash_invalid", "message": messages.SHIFT_COUNTED_CASH_INVALID},
        )
    close_note = (req.close_note or "").strip() or None

    canonical = _canonical({
        "cmd": "shift_close",
        "shift_id": shift_id,
        "counted": str(counted),
        "close_note": close_note or "",
    })
    key_hash = _sha256(key)
    request_hash = _sha256(canonical)

    # Store-scoped load + authorization (a cross-store shift is a plain 404).
    shift = db.get(CashierShift, shift_id)
    if shift is None or shift.store_id != staff.store_id:
        raise _not_found()
    _authorize_close(staff, shift)

    # Fast idempotency path for an already-closed shift.
    if shift.status == SHIFT_CLOSED:
        return _resolve_close_replay(db, shift, key_hash, request_hash)

    try:
        # Lock the shift row to serialise concurrent closes.
        locked = db.execute(
            select(CashierShift)
            .where(CashierShift.id == shift_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if locked is None or locked.store_id != staff.store_id:
            raise _not_found()

        # Definitive re-check under lock: a concurrent close may have committed.
        if locked.status == SHIFT_CLOSED:
            db.rollback()
            return _resolve_close_replay(db, locked, key_hash, request_hash)

        closed_at = _now()
        totals = compute_shift_totals(
            db,
            store_id=locked.store_id,
            cashier_user_id=locked.cashier_user_id,
            opened_at=locked.opened_at,
            closed_at=closed_at,
        )
        expected_cash = q2(
            q2(locked.opening_cash_amount)
            + totals["cash_payments_amount"]
            - totals["cash_refunds_amount"]
        )
        discrepancy = q2(counted - expected_cash)

        locked.status = SHIFT_CLOSED
        locked.closed_at = closed_at
        locked.counted_closing_cash_amount = counted
        locked.close_note = close_note
        locked.cash_payments_amount = totals["cash_payments_amount"]
        locked.cash_refunds_amount = totals["cash_refunds_amount"]
        locked.card_payments_amount = totals["card_payments_amount"]
        locked.card_refunds_amount = totals["card_refunds_amount"]
        locked.gross_payments_amount = totals["gross_payments_amount"]
        locked.total_refunds_amount = totals["total_refunds_amount"]
        locked.net_collected_amount = totals["net_collected_amount"]
        locked.expected_closing_cash_amount = expected_cash
        locked.cash_discrepancy_amount = discrepancy
        locked.closed_idempotency_key_hash = key_hash
        locked.closed_request_hash = request_hash

        audit(
            db,
            entity_type="cashier_shift",
            entity_id=locked.id,
            action="CASHIER_SHIFT_CLOSED",
            actor_type="STAFF",
            actor_id=str(staff.user_id),
            ip_address=ip_address,
            payload_after={
                "shift_id": locked.id,
                "store_id": locked.store_id,
                "cashier_user_id": locked.cashier_user_id,
                "opened_at": locked.opened_at,
                "closed_at": closed_at,
                "opening_cash_amount": q2(locked.opening_cash_amount),
                "cash_payments_amount": totals["cash_payments_amount"],
                "cash_refunds_amount": totals["cash_refunds_amount"],
                "expected_closing_cash_amount": expected_cash,
                "counted_closing_cash_amount": counted,
                "cash_discrepancy_amount": discrepancy,
                "card_payments_amount": totals["card_payments_amount"],
                "card_refunds_amount": totals["card_refunds_amount"],
                "gross_payments_amount": totals["gross_payments_amount"],
                "total_refunds_amount": totals["total_refunds_amount"],
                "net_collected_amount": totals["net_collected_amount"],
            },
        )

        db.commit()
        db.refresh(locked)
        logger.info(
            "cashier_shift_closed shift=%s store=%s cashier=%s expected=%s counted=%s discrepancy=%s",
            locked.id, locked.store_id, locked.cashier_user_id, expected_cash, counted, discrepancy,
        )
        return build_shift_response(db, locked, replay=False)

    except HTTPException:
        db.rollback()
        raise


def _authorize_close(staff: CurrentStaff, shift: CashierShift) -> None:
    """
    A cashier may close only their OWN shift. A supervisor (owner:read — held by
    OWNER/MANAGER, never by CASHIER) may close any open shift in their own store.
    """
    if shift.cashier_user_id == staff.user_id:
        return
    if staff.has_permission(PERM_OWNER_READ):
        return
    raise _forbidden()


def _resolve_close_replay(
    db: Session, shift: CashierShift, key_hash: str, request_hash: str
) -> ShiftResponse:
    if shift.closed_idempotency_key_hash == key_hash:
        if shift.closed_request_hash == request_hash:
            return build_shift_response(db, shift, replay=True)
        raise _conflict(messages.SHIFT_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch")
    # A different key against an already-closed shift is not a replay.
    raise _conflict(messages.SHIFT_ALREADY_CLOSED, error="already_closed")


# ── Reads ─────────────────────────────────────────────────────────────────────

def _can_view_all(staff: CurrentStaff) -> bool:
    """OWNER/MANAGER (owner:read) see every shift in the store; a cashier sees own."""
    return staff.has_permission(PERM_OWNER_READ)


def list_shifts(
    db: Session,
    staff: CurrentStaff,
    *,
    status: Optional[str] = None,
    cashier_user_id: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 100,
) -> ShiftListResponse:
    q = db.query(CashierShift).filter(CashierShift.store_id == staff.store_id)

    if not _can_view_all(staff):
        # A cashier sees only their own shifts.
        q = q.filter(CashierShift.cashier_user_id == staff.user_id)
    elif cashier_user_id is not None:
        q = q.filter(CashierShift.cashier_user_id == cashier_user_id)

    if status in (SHIFT_OPEN, SHIFT_CLOSED):
        q = q.filter(CashierShift.status == status)
    if date_from is not None:
        q = q.filter(CashierShift.opened_at >= date_from)
    if date_to is not None:
        q = q.filter(CashierShift.opened_at < date_to)

    limit = max(1, min(limit, 500))
    rows = q.order_by(CashierShift.opened_at.desc()).limit(limit).all()
    return ShiftListResponse(shifts=[build_shift_response(db, s) for s in rows])


def get_shift(db: Session, staff: CurrentStaff, shift_id: int) -> Optional[ShiftResponse]:
    shift = db.get(CashierShift, shift_id)
    if shift is None or shift.store_id != staff.store_id:
        return None
    if not _can_view_all(staff) and shift.cashier_user_id != staff.user_id:
        # A cashier cannot read another cashier's shift — non-disclosing 404.
        return None
    return build_shift_response(db, shift)
