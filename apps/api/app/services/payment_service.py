"""
Payment Service — append-only financial ledger for cashier collection & refunds.

Guarantees
----------
1. Source of truth is the ledger: PaymentSettlement → PaymentAllocation, and
   PaymentRefund. Order.paid_amount / refunded_amount / payment_status /
   refund_status are a summary mirror maintained inside the SAME transaction.
2. Money is Decimal end-to-end, quantised to 2 places. Never float.
3. The order total settled against is the persisted checkout snapshot
   (orders.total_amount) — never recalculated, never client-supplied.
4. Store and actor come only from the authenticated session.
5. Concurrency: selected orders are locked FOR UPDATE in ascending id order, so
   two cashiers can never both collect the same outstanding balance and two
   refunds can never exceed the refundable balance.
6. Idempotency: every command requires an Idempotency-Key. Only
   SHA-256(key) and SHA-256(canonical payload) are stored. Same key + same
   payload replays the original result; same key + different payload → 409.
7. Only non-sensitive data is stored — never card PAN/CVV/track data.
"""
from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import messages
from app.models.order import Order
from app.models.payment_allocation import PaymentAllocation
from app.models.payment_refund import PaymentRefund
from app.models.payment_settlement import PaymentSettlement
from app.models.table import Table
from app.models.user import User
from app.schemas.payment import (
    AllocationReceipt,
    OrderPaymentRequest,
    RefundCreateRequest,
    RefundReceipt,
    SettlementCreateRequest,
    SettlementReceipt,
)
from app.services.audit_service import audit
from app.services.auth_service import CurrentStaff

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")
VALID_METHODS = {"CASH", "CARD", "OTHER"}
DEFAULT_CURRENCY = "TRY"


# ── Small helpers ─────────────────────────────────────────────────────────────

def order_code(order_id: int) -> str:
    """Staff-facing order code derived from the database id (no new column)."""
    return f"SIP-{order_id:06d}"


def parse_order_code(raw: str) -> Optional[int]:
    """Accept 'SIP-000123', 'sip123', or a plain '123'; return the numeric id."""
    if raw is None:
        return None
    s = raw.strip().upper().replace(" ", "")
    if s.startswith("SIP-"):
        s = s[4:]
    elif s.startswith("SIP"):
        s = s[3:]
    s = s.lstrip("0") or "0"
    try:
        val = int(s)
    except ValueError:
        return None
    return val if val > 0 else None


def q2(value) -> Decimal:
    """Quantise any numeric to 2 decimal places (money)."""
    return Decimal(str(value if value is not None else "0")).quantize(
        TWO_PLACES, rounding=ROUND_HALF_UP
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(payload: dict) -> str:
    """Deterministic JSON for request-payload hashing (sorted keys, no spaces)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _require_key(idempotency_key: Optional[str]) -> str:
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "idempotency_required", "message": messages.PAY_IDEMPOTENCY_REQUIRED},
        )
    return idempotency_key.strip()


def _conflict(message: str, error: str = "conflict") -> HTTPException:
    return HTTPException(status_code=409, detail={"error": error, "message": message})


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"error": "not_found", "message": messages.PAY_NOT_FOUND})


# ── Order summary maths ───────────────────────────────────────────────────────

def net_paid(order: Order) -> Decimal:
    return q2(order.paid_amount) - q2(order.refunded_amount)


def outstanding(order: Order) -> Decimal:
    rem = q2(order.total_amount) - net_paid(order)
    return rem if rem > 0 else Decimal("0.00")


def is_payable(order: Order) -> bool:
    return order.status != "CANCELLED" and outstanding(order) > 0


def recompute_order_summary(order: Order) -> None:
    """Derive payment_status / refund_status from paid & refunded amounts."""
    total = q2(order.total_amount)
    paid = q2(order.paid_amount)
    refunded = q2(order.refunded_amount)
    net = paid - refunded

    if net <= 0:
        order.payment_status = "UNPAID"
    elif net >= total:
        # net can only equal total in normal operation (overpayment is blocked).
        order.payment_status = "PAID"
    else:
        order.payment_status = "PARTIALLY_PAID"

    if refunded <= 0:
        order.refund_status = "NONE"
    elif refunded >= paid:
        order.refund_status = "REFUNDED"
    else:
        order.refund_status = "PARTIALLY_REFUNDED"


# ── Cashier display ───────────────────────────────────────────────────────────

def _cashier_display(db: Session, user_id: int) -> str:
    user = db.get(User, user_id)
    return user.username if user else f"user:{user_id}"


# ── Idempotency lookups ───────────────────────────────────────────────────────

def _find_settlement_by_key(db: Session, store_id: int, key_hash: str) -> Optional[PaymentSettlement]:
    return (
        db.query(PaymentSettlement)
        .filter(
            PaymentSettlement.store_id == store_id,
            PaymentSettlement.idempotency_key_hash == key_hash,
        )
        .first()
    )


def _find_refund_by_key(db: Session, store_id: int, key_hash: str) -> Optional[PaymentRefund]:
    return (
        db.query(PaymentRefund)
        .filter(
            PaymentRefund.store_id == store_id,
            PaymentRefund.idempotency_key_hash == key_hash,
        )
        .first()
    )


# ── Receipts ──────────────────────────────────────────────────────────────────

def build_settlement_receipt(
    db: Session, settlement: PaymentSettlement, *, replay: bool = False
) -> SettlementReceipt:
    table_number = None
    if settlement.table_id is not None:
        tbl = db.get(Table, settlement.table_id)
        table_number = tbl.table_number if tbl else None

    allocations = [
        AllocationReceipt(
            id=a.id,
            order_id=a.order_id,
            order_code=order_code(a.order_id),
            amount=q2(a.amount),
        )
        for a in sorted(settlement.allocations, key=lambda x: x.id)
    ]
    return SettlementReceipt(
        settlement_id=settlement.id,
        table_id=settlement.table_id,
        table_number=table_number,
        payment_method=settlement.payment_method,
        currency=settlement.currency,
        gross_amount=q2(settlement.gross_amount),
        status=settlement.status,
        cashier_display=_cashier_display(db, settlement.cashier_user_id),
        completed_at=settlement.completed_at,
        allocations=allocations,
        idempotent_replay=replay,
    )


def build_refund_receipt(
    db: Session, refund: PaymentRefund, *, replay: bool = False
) -> RefundReceipt:
    return RefundReceipt(
        refund_id=refund.id,
        settlement_id=refund.settlement_id,
        allocation_id=refund.allocation_id,
        order_id=refund.order_id,
        order_code=order_code(refund.order_id),
        amount=q2(refund.amount),
        currency=refund.currency,
        reason=refund.reason,
        refunded_by_display=_cashier_display(db, refund.refunded_by_user_id),
        created_at=refund.created_at,
        idempotent_replay=replay,
    )


def _audit_replay(db: Session, entity_type: str, entity_id: int, staff: CurrentStaff, ip: Optional[str]) -> None:
    """Record a non-financial replay marker (never a second money mutation)."""
    audit(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        action="PAYMENT_IDEMPOTENT_REPLAY",
        actor_type="STAFF",
        actor_id=str(staff.user_id),
        ip_address=ip,
        payload_after={"entity_id": entity_id, "store_id": staff.store_id},
    )
    db.commit()


# ── Collection ────────────────────────────────────────────────────────────────

def _validate_method(method: str) -> str:
    if method not in VALID_METHODS:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_method", "message": messages.PAY_METHOD_INVALID},
        )
    return method


def _lock_orders(db: Session, order_ids: Iterable[int]) -> list[Order]:
    ids = sorted(set(int(i) for i in order_ids))
    if not ids:
        return []
    # populate_existing() overwrites any stale identity-map attributes with the
    # freshly-locked DB row — essential for correct concurrent balance checks:
    # without it the ORM would return a copy read before a competing cashier's
    # commit, and both settlements would "see" the same outstanding balance.
    rows = db.execute(
        select(Order)
        .where(Order.id.in_(ids))
        .order_by(Order.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()
    return list(rows)


def collect_settlement(
    db: Session,
    staff: CurrentStaff,
    req: SettlementCreateRequest,
    *,
    idempotency_key: Optional[str],
    ip_address: Optional[str] = None,
) -> SettlementReceipt:
    """Settle the exact outstanding balance of the selected table orders."""
    key = _require_key(idempotency_key)
    method = _validate_method(req.payment_method.value)
    # Currency is server-controlled: never read from the request. SweetOps is
    # single-currency (TRY); the value comes only from server configuration.
    currency = DEFAULT_CURRENCY

    canonical = _canonical({
        "cmd": "settlement",
        "table_id": req.table_id,
        "order_ids": sorted(set(req.order_ids)),
        "method": method,
        "currency": currency,
        "note": req.note or "",
    })
    key_hash = _sha256(key)
    request_hash = _sha256(canonical)

    # Fast idempotency path (definitive check happens again under lock).
    existing = _find_settlement_by_key(db, staff.store_id, key_hash)
    if existing is not None:
        return _resolve_settlement_replay(db, existing, request_hash, staff, ip_address)

    return _do_collect(
        db,
        staff,
        table_id=req.table_id,
        order_ids=req.order_ids,
        method=method,
        currency=currency,
        note=req.note,
        terminal_reference=req.terminal_reference,
        per_order_amount=None,          # pay full outstanding of each order
        key_hash=key_hash,
        request_hash=request_hash,
        ip_address=ip_address,
        # The whole-table settle is the generic one-click flow. An order that was
        # previously refunded still shows an outstanding balance; recollecting it
        # here would be a silent side effect, so it is refused and must be done
        # through the explicit per-order endpoint instead.
        guard_refunded_recollect=True,
    )


def collect_order_payment(
    db: Session,
    staff: CurrentStaff,
    order_id: int,
    req: OrderPaymentRequest,
    *,
    idempotency_key: Optional[str],
    ip_address: Optional[str] = None,
) -> SettlementReceipt:
    """Collect full or partial payment for a single order."""
    key = _require_key(idempotency_key)
    method = _validate_method(req.payment_method.value)
    # Currency is server-controlled (see collect_settlement) — always TRY.
    currency = DEFAULT_CURRENCY

    amount = None
    if req.amount is not None:
        amount = q2(req.amount)
        if amount <= 0:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_amount", "message": messages.PAY_AMOUNT_INVALID},
            )

    canonical = _canonical({
        "cmd": "order_payment",
        "order_id": order_id,
        "amount": str(amount) if amount is not None else "full",
        "method": method,
        "currency": currency,
        "note": req.note or "",
    })
    key_hash = _sha256(key)
    request_hash = _sha256(canonical)

    existing = _find_settlement_by_key(db, staff.store_id, key_hash)
    if existing is not None:
        return _resolve_settlement_replay(db, existing, request_hash, staff, ip_address)

    # Resolve the order's table so the settlement is anchored to it.
    order = db.get(Order, order_id)
    if order is None or order.store_id != staff.store_id:
        raise _not_found()

    return _do_collect(
        db,
        staff,
        table_id=order.table_id,
        order_ids=[order_id],
        method=method,
        currency=currency,
        note=req.note,
        terminal_reference=req.terminal_reference,
        per_order_amount=amount,        # None → full outstanding; else exact partial
        key_hash=key_hash,
        request_hash=request_hash,
        ip_address=ip_address,
        # Explicit, single-order collection IS the confirmed path for
        # recollecting a previously-refunded order — allow it here.
        guard_refunded_recollect=False,
    )


def _resolve_settlement_replay(
    db: Session,
    existing: PaymentSettlement,
    request_hash: str,
    staff: CurrentStaff,
    ip_address: Optional[str],
) -> SettlementReceipt:
    if existing.request_hash != request_hash:
        raise _conflict(messages.PAY_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch")
    _audit_replay(db, "payment_settlement", existing.id, staff, ip_address)
    return build_settlement_receipt(db, existing, replay=True)


def _do_collect(
    db: Session,
    staff: CurrentStaff,
    *,
    table_id: Optional[int],
    order_ids: list[int],
    method: str,
    currency: str,
    note: Optional[str],
    terminal_reference: Optional[str],
    per_order_amount: Optional[Decimal],
    key_hash: str,
    request_hash: str,
    ip_address: Optional[str],
    guard_refunded_recollect: bool = False,
) -> SettlementReceipt:
    try:
        # 1. Lock all selected orders in deterministic id order.
        orders = _lock_orders(db, order_ids)

        # 2. Definitive idempotency re-check now that we hold the locks: a
        #    concurrent identical command may have committed while we waited.
        existing = _find_settlement_by_key(db, staff.store_id, key_hash)
        if existing is not None:
            db.rollback()
            return _resolve_settlement_replay(db, existing, request_hash, staff, ip_address)

        found_ids = {o.id for o in orders}
        missing = [i for i in set(order_ids) if i not in found_ids]
        if missing:
            raise _not_found()

        # 3. Store + table isolation (all derived from the session store).
        for o in orders:
            if o.store_id != staff.store_id:
                raise _not_found()
            if o.status == "CANCELLED":
                raise _conflict(messages.PAY_ORDER_CANCELLED, error="order_cancelled")

        if table_id is not None:
            tbl = db.get(Table, table_id)
            if tbl is None or tbl.store_id != staff.store_id:
                raise _not_found()
            for o in orders:
                if o.table_id != table_id:
                    raise _conflict(messages.PAY_TABLE_MISMATCH, error="table_mismatch")

        # 4. Compute allocations from LOCKED state.
        allocations: list[tuple[Order, Decimal]] = []
        if per_order_amount is not None:
            # Single-order partial/full payment.
            order = orders[0]
            out = outstanding(order)
            if out <= 0:
                raise _conflict(messages.PAY_NO_BALANCE, error="no_balance")
            if per_order_amount > out:
                raise _conflict(messages.PAY_OVERPAYMENT, error="overpayment")
            allocations.append((order, per_order_amount))
        else:
            # Pay full outstanding of each selected order.
            for order in orders:
                out = outstanding(order)
                if out <= 0:
                    continue
                # A previously-refunded order still carries an outstanding
                # balance. In the generic whole-table settle flow, refuse to
                # silently recollect it — this must be an explicit per-order
                # action (see collect_order_payment) so the operator confirms.
                if guard_refunded_recollect and order.refund_status != "NONE":
                    raise _conflict(
                        messages.PAY_REFUNDED_RECOLLECT, error="refunded_recollect"
                    )
                allocations.append((order, out))

        if not allocations:
            raise _conflict(messages.PAY_NO_BALANCE, error="no_balance")

        gross = sum((amt for _, amt in allocations), Decimal("0.00"))
        gross = q2(gross)

        # 5. Create the settlement + allocations, update order summaries.
        settlement = PaymentSettlement(
            store_id=staff.store_id,
            table_id=table_id,
            cashier_user_id=staff.user_id,
            payment_method=method,
            currency=currency,
            gross_amount=gross,
            status="COMPLETED",
            note=note,
            terminal_reference=terminal_reference,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        db.add(settlement)
        db.flush()  # settlement.id

        for order, amt in allocations:
            db.add(PaymentAllocation(
                settlement_id=settlement.id,
                order_id=order.id,
                amount=amt,
            ))
            order.paid_amount = q2(order.paid_amount) + amt
            # Guard invariant: never exceed the order total.
            if net_paid(order) > q2(order.total_amount):
                raise _conflict(messages.PAY_OVERPAYMENT, error="overpayment")
            recompute_order_summary(order)

        # 6. Audit (single financial mutation entry).
        audit(
            db,
            entity_type="payment_settlement",
            entity_id=settlement.id,
            action="PAYMENT_COLLECTED",
            actor_type="STAFF",
            actor_id=str(staff.user_id),
            ip_address=ip_address,
            payload_after={
                "settlement_id": settlement.id,
                "store_id": staff.store_id,
                "table_id": table_id,
                "order_ids": [o.id for o, _ in allocations],
                "method": method,
                "currency": currency,
                "gross_amount": gross,
            },
        )

        db.commit()
        db.refresh(settlement)
        logger.info(
            "payment_collected settlement=%s store=%s cashier=%s method=%s gross=%s",
            settlement.id, staff.store_id, staff.user_id, method, gross,
        )
        return build_settlement_receipt(db, settlement, replay=False)

    except IntegrityError:
        # A concurrent identical key committed between our re-check and insert.
        db.rollback()
        existing = _find_settlement_by_key(db, staff.store_id, key_hash)
        if existing is not None:
            return _resolve_settlement_replay(db, existing, request_hash, staff, ip_address)
        raise
    except HTTPException:
        db.rollback()
        raise


# ── Refund ────────────────────────────────────────────────────────────────────

def allocation_refundable(db: Session, allocation: PaymentAllocation) -> Decimal:
    already = db.query(func.coalesce(func.sum(PaymentRefund.amount), 0)).filter(
        PaymentRefund.allocation_id == allocation.id
    ).scalar()
    return q2(allocation.amount) - q2(already)


def refund_allocation(
    db: Session,
    staff: CurrentStaff,
    allocation_id: int,
    req: RefundCreateRequest,
    *,
    idempotency_key: Optional[str],
    ip_address: Optional[str] = None,
) -> RefundReceipt:
    key = _require_key(idempotency_key)

    reason = (req.reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=422,
            detail={"error": "reason_required", "message": messages.REFUND_REASON_REQUIRED},
        )
    amount = q2(req.amount)
    if amount <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_amount", "message": messages.REFUND_AMOUNT_INVALID},
        )

    canonical = _canonical({
        "cmd": "refund",
        "allocation_id": allocation_id,
        "amount": str(amount),
        "reason": reason,
    })
    key_hash = _sha256(key)
    request_hash = _sha256(canonical)

    existing = _find_refund_by_key(db, staff.store_id, key_hash)
    if existing is not None:
        return _resolve_refund_replay(db, existing, request_hash, staff, ip_address)

    try:
        # Load allocation → settlement for store isolation.
        allocation = db.get(PaymentAllocation, allocation_id)
        if allocation is None:
            raise _not_found()
        settlement = db.get(PaymentSettlement, allocation.settlement_id)
        if settlement is None or settlement.store_id != staff.store_id:
            raise _not_found()

        # Lock the order row to serialise concurrent refunds for this order.
        # populate_existing() refreshes stale in-memory attributes with the
        # locked row so the refundable check reflects committed state.
        order = db.execute(
            select(Order)
            .where(Order.id == allocation.order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if order is None or order.store_id != staff.store_id:
            raise _not_found()

        # Definitive idempotency re-check under lock.
        existing = _find_refund_by_key(db, staff.store_id, key_hash)
        if existing is not None:
            db.rollback()
            return _resolve_refund_replay(db, existing, request_hash, staff, ip_address)

        refundable = allocation_refundable(db, allocation)
        if refundable <= 0 or amount > refundable:
            raise _conflict(messages.REFUND_OVER_BALANCE, error="refund_over_balance")

        refund = PaymentRefund(
            store_id=staff.store_id,
            settlement_id=settlement.id,
            allocation_id=allocation.id,
            order_id=order.id,
            amount=amount,
            currency=settlement.currency,
            reason=reason,
            refunded_by_user_id=staff.user_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
        )
        db.add(refund)

        order.refunded_amount = q2(order.refunded_amount) + amount
        recompute_order_summary(order)

        audit(
            db,
            entity_type="payment_refund",
            entity_id=allocation.id,
            action="PAYMENT_REFUNDED",
            actor_type="STAFF",
            actor_id=str(staff.user_id),
            ip_address=ip_address,
            payload_after={
                "settlement_id": settlement.id,
                "allocation_id": allocation.id,
                "order_id": order.id,
                "store_id": staff.store_id,
                "amount": amount,
                "currency": settlement.currency,
            },
        )

        db.commit()
        db.refresh(refund)
        logger.info(
            "payment_refunded refund=%s allocation=%s store=%s actor=%s amount=%s",
            refund.id, allocation.id, staff.store_id, staff.user_id, amount,
        )
        return build_refund_receipt(db, refund, replay=False)

    except IntegrityError:
        db.rollback()
        existing = _find_refund_by_key(db, staff.store_id, key_hash)
        if existing is not None:
            return _resolve_refund_replay(db, existing, request_hash, staff, ip_address)
        raise
    except HTTPException:
        db.rollback()
        raise


def _resolve_refund_replay(
    db: Session,
    existing: PaymentRefund,
    request_hash: str,
    staff: CurrentStaff,
    ip_address: Optional[str],
) -> RefundReceipt:
    if existing.request_hash != request_hash:
        raise _conflict(messages.PAY_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch")
    _audit_replay(db, "payment_refund", existing.allocation_id, staff, ip_address)
    return build_refund_receipt(db, existing, replay=True)


# ── Order-level refund for an issue resolution ────────────────────────────────

def order_refundable(order: Order) -> Decimal:
    """
    The order's remaining refundable amount = net paid (paid − refunded).

    Equal to the sum of every allocation's refundable balance, because
    paid = Σ allocation amounts and refunded = Σ refunds. This is the ceiling on
    what an issue resolution may refund.
    """
    rem = net_paid(order)
    return rem if rem > 0 else Decimal("0.00")


def create_issue_refunds(
    db: Session,
    *,
    staff: CurrentStaff,
    order: Order,
    issue_id: int,
    total_amount: Decimal,
    reason: str,
    base_key: str,
    ip_address: Optional[str] = None,
) -> list[PaymentRefund]:
    """
    Refund ``total_amount`` for ``order`` as part of resolving order issue
    ``issue_id``, distributing it across the order's allocations (ascending id),
    filling each allocation's remaining refundable balance in turn.

    This is the ONE place an issue resolution touches money, and it reuses the
    existing append-only refund ledger — it never restates a refunded amount. Each
    created PaymentRefund carries ``order_issue_id`` so the resolution's refunds
    can be summed back for reconciliation, and the order summary mirror is updated
    in the SAME transaction.

    Caller responsibilities (this function does NOT commit):
      * ``order`` must already be locked FOR UPDATE by the caller,
      * ``total_amount`` must be > 0 and <= order_refundable(order).

    Per-refund idempotency keys are derived deterministically from the issue's own
    Idempotency-Key, so the store-scoped uniqueness of the refund ledger holds and a
    stray replay that reached here would collide rather than double-refund. Returns
    the created refunds in allocation order (the first is the issue's primary link).
    """
    total = q2(total_amount)
    if total <= 0:
        raise _conflict(messages.REFUND_OVER_BALANCE, error="refund_over_balance")

    allocations = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.order_id == order.id)
        .order_by(PaymentAllocation.id)
        .all()
    )

    remaining = total
    created: list[PaymentRefund] = []
    for alloc in allocations:
        if remaining <= 0:
            break
        refundable = allocation_refundable(db, alloc)
        if refundable <= 0:
            continue
        take = remaining if remaining < refundable else refundable
        take = q2(take)
        settlement = db.get(PaymentSettlement, alloc.settlement_id)

        key_hash = _sha256(f"{base_key}|issue-refund|{issue_id}|{alloc.id}")
        request_hash = _sha256(_canonical({
            "cmd": "issue_refund",
            "issue_id": issue_id,
            "allocation_id": alloc.id,
            "amount": str(take),
            "reason": reason,
        }))

        refund = PaymentRefund(
            store_id=order.store_id,
            settlement_id=settlement.id,
            allocation_id=alloc.id,
            order_id=order.id,
            amount=take,
            currency=settlement.currency,
            reason=reason,
            refunded_by_user_id=staff.user_id,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
            order_issue_id=issue_id,
        )
        db.add(refund)
        order.refunded_amount = q2(order.refunded_amount) + take
        remaining -= take
        created.append(refund)

    if remaining > 0 or not created:
        # Asked to refund more than the ledger can give back. The caller validated
        # this against order_refundable, so reaching here means a concurrent refund
        # consumed the balance under our lock — refuse rather than under-refund.
        raise _conflict(messages.REFUND_OVER_BALANCE, error="refund_over_balance")

    recompute_order_summary(order)
    db.flush()  # assign refund ids for the issue's primary link

    for refund in created:
        audit(
            db,
            entity_type="payment_refund",
            entity_id=refund.allocation_id,
            action="PAYMENT_REFUNDED",
            actor_type="STAFF",
            actor_id=str(staff.user_id),
            ip_address=ip_address,
            payload_after={
                "settlement_id": refund.settlement_id,
                "allocation_id": refund.allocation_id,
                "order_id": order.id,
                "store_id": order.store_id,
                "amount": q2(refund.amount),
                "currency": refund.currency,
                "order_issue_id": issue_id,
            },
        )
    return created
