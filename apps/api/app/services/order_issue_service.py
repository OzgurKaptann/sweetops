"""
Order Issue Service — record a problem against an order, then resolve it with a
controlled cancellation and/or refund decision.

An order issue COORDINATES the systems that already exist; it never bypasses them:

  * money moves ONLY through the append-only payment refund ledger
    (payment_service.create_issue_refunds),
  * stock moves ONLY through the existing inventory lifecycle primitive
    (inventory_service.release_order_reservation) — which releases outstanding
    reservation and NEVER restores already-consumed stock,
  * the cashier shift snapshot is untouched: a refund created here lands in the
    ledger, so an OPEN shift's close picks it up by the existing window rule and a
    CLOSED shift's frozen snapshot is unaffected,
  * every mutation is audit-logged, and every command is store-scoped and idempotent.

Guarantees
----------
1. Store and actor come only from the authenticated session, never the body.
2. Creation records the problem only — it moves no money and no stock.
3. Resolution is atomic: refund creation, cancellation and the issue update commit
   in ONE transaction, or none of them do.
4. Idempotent: creation is store-scoped by Idempotency-Key; resolution writes onto
   the issue's own row. A replay returns the original result and never creates a
   second refund; the same key with a different payload → 409.
5. Money is Decimal end-to-end. Only SHA-256 hashes of the key/payload are stored.
6. A refunding resolution (FULL/PARTIAL) requires payments:refund — a cashier may
   record an issue and close it without money, but a refund is a supervisor control.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import messages
from app.core.permissions import PERM_PAYMENTS_REFUND
from app.models.order import Order
from app.models.order_issue import (
    ISSUE_STATUS_OPEN,
    ISSUE_STATUS_RESOLVED,
    REFUNDING_RESOLUTIONS,
    RESOLUTION_CANCEL_ONLY,
    RESOLUTION_FULL_REFUND,
    RESOLUTION_NO_REFUND,
    RESOLUTION_PARTIAL_REFUND,
    OrderIssue,
)
from app.models.order_status_event import OrderStatusEvent
from app.models.user import User
from app.schemas.order_issue import (
    IssueCreateRequest,
    IssueResolveRequest,
    OrderIssueListResponse,
    OrderIssueResponse,
)
from app.services import inventory_service, payment_service
from app.services.audit_service import audit
from app.services.auth_service import CurrentStaff

logger = logging.getLogger(__name__)

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")

AUDIT_CREATED = "ORDER_ISSUE_CREATED"
AUDIT_RESOLVED = "ORDER_ISSUE_RESOLVED"


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
            detail={"error": "idempotency_required", "message": messages.ISSUE_IDEMPOTENCY_REQUIRED},
        )
    return idempotency_key.strip()


def _require_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise HTTPException(
            status_code=422,
            detail={"error": "reason_required", "message": messages.ISSUE_REASON_REQUIRED},
        )
    return text


def _conflict(message: str, error: str = "conflict") -> HTTPException:
    return HTTPException(status_code=409, detail={"error": error, "message": message})


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404, detail={"error": "not_found", "message": messages.ISSUE_NOT_FOUND}
    )


def _forbidden(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"error": "forbidden", "message": message})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _display(db: Session, user_id: int | None) -> Optional[str]:
    if user_id is None:
        return None
    user = db.get(User, user_id)
    return user.username if user else f"user:{user_id}"


# ── Response builder ──────────────────────────────────────────────────────────

def build_issue_response(
    db: Session, issue: OrderIssue, *, replay: bool = False
) -> OrderIssueResponse:
    order = db.get(Order, issue.order_id)
    refundable = payment_service.order_refundable(order) if order else ZERO
    return OrderIssueResponse(
        id=issue.id,
        store_id=issue.store_id,
        order_id=issue.order_id,
        order_code=payment_service.order_code(issue.order_id),
        issue_type=issue.issue_type,
        status=issue.status,
        resolution_type=issue.resolution_type,
        requested_refund_amount=(
            q2(issue.requested_refund_amount)
            if issue.requested_refund_amount is not None else None
        ),
        approved_refund_amount=(
            q2(issue.approved_refund_amount)
            if issue.approved_refund_amount is not None else None
        ),
        refund_id=issue.refund_id,
        reason=issue.reason,
        note=issue.note,
        created_by_user_id=issue.created_by_user_id,
        created_by_display=_display(db, issue.created_by_user_id) or "",
        resolved_by_user_id=issue.resolved_by_user_id,
        resolved_by_display=_display(db, issue.resolved_by_user_id),
        created_at=issue.created_at,
        resolved_at=issue.resolved_at,
        order_refundable_amount=q2(refundable),
        idempotent_replay=replay,
    )


# ── Lookups ───────────────────────────────────────────────────────────────────

def _find_by_create_key(db: Session, store_id: int, key_hash: str) -> Optional[OrderIssue]:
    return (
        db.query(OrderIssue)
        .filter(
            OrderIssue.store_id == store_id,
            OrderIssue.created_idempotency_key_hash == key_hash,
        )
        .first()
    )


# ── Create ────────────────────────────────────────────────────────────────────

def create_issue(
    db: Session,
    staff: CurrentStaff,
    order_id: int,
    req: IssueCreateRequest,
    *,
    idempotency_key: Optional[str],
    ip_address: Optional[str] = None,
) -> OrderIssueResponse:
    """Record a problem against an order. Moves no money and no stock."""
    key = _require_key(idempotency_key)
    issue_type = req.issue_type.value
    reason = _require_reason(req.reason)
    note = (req.note or "").strip() or None
    requested = q2(req.requested_refund_amount) if req.requested_refund_amount is not None else None
    if requested is not None and requested < 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_amount", "message": messages.ISSUE_REQUESTED_OVER_REFUNDABLE},
        )

    canonical = _canonical({
        "cmd": "issue_create",
        "order_id": order_id,
        "issue_type": issue_type,
        "requested": str(requested) if requested is not None else "none",
        "reason": reason,
        "note": note or "",
    })
    key_hash = _sha256(key)
    request_hash = _sha256(canonical)

    existing = _find_by_create_key(db, staff.store_id, key_hash)
    if existing is not None:
        return _resolve_create_replay(db, existing, request_hash)

    try:
        order = db.get(Order, order_id)
        if order is None or order.store_id != staff.store_id:
            raise _not_found()

        # The requested refund cannot exceed what is still refundable on the order.
        if requested is not None and requested > payment_service.order_refundable(order):
            raise _conflict(
                messages.ISSUE_REQUESTED_OVER_REFUNDABLE, error="requested_over_refundable"
            )

        issue = OrderIssue(
            store_id=staff.store_id,
            order_id=order_id,
            issue_type=issue_type,
            status=ISSUE_STATUS_OPEN,
            requested_refund_amount=requested,
            reason=reason,
            note=note,
            created_by_user_id=staff.user_id,
            created_idempotency_key_hash=key_hash,
            created_request_hash=request_hash,
        )
        db.add(issue)
        db.flush()  # issue.id, created_at

        audit(
            db,
            entity_type="order_issue",
            entity_id=issue.id,
            action=AUDIT_CREATED,
            actor_type="STAFF",
            actor_id=str(staff.user_id),
            ip_address=ip_address,
            payload_after={
                "issue_id": issue.id,
                "order_id": order_id,
                "store_id": staff.store_id,
                "issue_type": issue_type,
                "requested_refund_amount": str(requested) if requested is not None else None,
                "created_by_user_id": staff.user_id,
                "reason": reason,
            },
        )

        db.commit()
        db.refresh(issue)
        logger.info(
            "order_issue_created issue=%s order=%s store=%s type=%s actor=%s",
            issue.id, order_id, staff.store_id, issue_type, staff.user_id,
        )
        return build_issue_response(db, issue, replay=False)

    except IntegrityError:
        db.rollback()
        existing = _find_by_create_key(db, staff.store_id, key_hash)
        if existing is not None:
            return _resolve_create_replay(db, existing, request_hash)
        raise
    except HTTPException:
        db.rollback()
        raise


def _resolve_create_replay(
    db: Session, existing: OrderIssue, request_hash: str
) -> OrderIssueResponse:
    if existing.created_request_hash != request_hash:
        raise _conflict(messages.ISSUE_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch")
    return build_issue_response(db, existing, replay=True)


# ── Resolve ───────────────────────────────────────────────────────────────────

def resolve_issue(
    db: Session,
    staff: CurrentStaff,
    issue_id: int,
    req: IssueResolveRequest,
    *,
    idempotency_key: Optional[str],
    ip_address: Optional[str] = None,
) -> OrderIssueResponse:
    """
    Resolve an OPEN issue. Refund creation, cancellation and the issue update are
    one atomic transaction; a replay never creates a second refund.
    """
    key = _require_key(idempotency_key)
    resolution = req.resolution_type.value
    reason = _require_reason(req.reason)
    note = (req.note or "").strip() or None
    approved_in = q2(req.approved_refund_amount) if req.approved_refund_amount is not None else None

    canonical = _canonical({
        "cmd": "issue_resolve",
        "issue_id": issue_id,
        "resolution_type": resolution,
        "approved": str(approved_in) if approved_in is not None else "none",
        "reason": reason,
        "note": note or "",
    })
    key_hash = _sha256(key)
    request_hash = _sha256(canonical)

    # Store-scoped load (a cross-store issue is a plain 404).
    issue = db.get(OrderIssue, issue_id)
    if issue is None or issue.store_id != staff.store_id:
        raise _not_found()

    # Fast idempotency path for an already-resolved issue.
    if issue.status != ISSUE_STATUS_OPEN:
        return _resolve_resolve_replay(db, issue, key_hash, request_hash)

    # A refunding resolution is a supervisor control; a cashier cannot refund.
    if resolution in REFUNDING_RESOLUTIONS and not staff.has_permission(PERM_PAYMENTS_REFUND):
        raise _forbidden(messages.ISSUE_REFUND_FORBIDDEN)

    try:
        # Lock the issue row (serialises concurrent resolves) and the order row
        # (serialises refunds + cancellation against the till).
        locked_issue = db.execute(
            select(OrderIssue)
            .where(OrderIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if locked_issue is None or locked_issue.store_id != staff.store_id:
            raise _not_found()
        if locked_issue.status != ISSUE_STATUS_OPEN:
            db.rollback()
            return _resolve_resolve_replay(db, locked_issue, key_hash, request_hash)

        order = db.execute(
            select(Order)
            .where(Order.id == locked_issue.order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if order is None or order.store_id != staff.store_id:
            raise _not_found()

        refundable = payment_service.order_refundable(order)
        approved_final = ZERO
        primary_refund_id: Optional[int] = None

        if resolution == RESOLUTION_NO_REFUND:
            approved_final = ZERO

        elif resolution == RESOLUTION_CANCEL_ONLY:
            # Cancelling an order that still holds collected money would silently
            # discard cash; the operator must refund it (FULL_REFUND) instead.
            if payment_service.net_paid(order) > ZERO:
                raise _conflict(messages.ISSUE_CANCEL_BLOCKED_PAID, error="cancel_blocked_paid")
            approved_final = ZERO
            _cancel_order(db, order, staff, ip_address)

        elif resolution == RESOLUTION_FULL_REFUND:
            if refundable <= ZERO:
                raise _conflict(messages.ISSUE_NOTHING_REFUNDABLE, error="nothing_refundable")
            approved_final = refundable
            refunds = payment_service.create_issue_refunds(
                db, staff=staff, order=order, issue_id=locked_issue.id,
                total_amount=approved_final, reason=reason, base_key=key, ip_address=ip_address,
            )
            primary_refund_id = refunds[0].id
            # A full refund fully reverses the sale — cancel the order and release
            # any outstanding reservation. Net paid is now zero, so the cancel is
            # allowed and consumed stock is (correctly) NOT restored.
            _cancel_order(db, order, staff, ip_address)

        elif resolution == RESOLUTION_PARTIAL_REFUND:
            if refundable <= ZERO:
                raise _conflict(messages.ISSUE_NOTHING_REFUNDABLE, error="nothing_refundable")
            if approved_in is None or approved_in <= ZERO:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "amount_required", "message": messages.ISSUE_PARTIAL_AMOUNT_REQUIRED},
                )
            if approved_in > refundable:
                raise _conflict(messages.ISSUE_APPROVED_OVER_REFUNDABLE, error="approved_over_refundable")
            approved_final = approved_in
            refunds = payment_service.create_issue_refunds(
                db, staff=staff, order=order, issue_id=locked_issue.id,
                total_amount=approved_final, reason=reason, base_key=key, ip_address=ip_address,
            )
            primary_refund_id = refunds[0].id
            # A partial refund leaves the order active — no cancellation, no stock move.

        now = _now()
        locked_issue.status = ISSUE_STATUS_RESOLVED
        locked_issue.resolution_type = resolution
        locked_issue.approved_refund_amount = approved_final
        locked_issue.refund_id = primary_refund_id
        locked_issue.resolved_by_user_id = staff.user_id
        locked_issue.resolved_at = now
        locked_issue.resolved_idempotency_key_hash = key_hash
        locked_issue.resolved_request_hash = request_hash

        audit(
            db,
            entity_type="order_issue",
            entity_id=locked_issue.id,
            action=AUDIT_RESOLVED,
            actor_type="STAFF",
            actor_id=str(staff.user_id),
            ip_address=ip_address,
            payload_after={
                "issue_id": locked_issue.id,
                "order_id": order.id,
                "store_id": staff.store_id,
                "resolution_type": resolution,
                "approved_refund_amount": str(approved_final),
                "refund_id": primary_refund_id,
                "resolved_by_user_id": staff.user_id,
                "reason": reason,
            },
        )

        db.commit()
        db.refresh(locked_issue)
        logger.info(
            "order_issue_resolved issue=%s order=%s store=%s resolution=%s approved=%s refund=%s actor=%s",
            locked_issue.id, order.id, staff.store_id, resolution, approved_final,
            primary_refund_id, staff.user_id,
        )
        return build_issue_response(db, locked_issue, replay=False)

    except HTTPException:
        db.rollback()
        raise


def _cancel_order(
    db: Session, order: Order, staff: CurrentStaff, ip_address: Optional[str]
) -> None:
    """
    Cancel an order as part of an issue resolution, reusing the existing inventory
    lifecycle primitive.

    ``release_order_reservation`` releases only what is still merely reserved and
    NEVER restores already-consumed stock — the batter the kitchen really poured
    stays spent. A returned-stock workflow is a deliberate, separate, actor-
    attributed movement and is out of scope here (see docs).

    Cancellation goes through this controlled path rather than the kitchen state
    machine because an operational void/refund can happen at any point in the
    order's life (even after DELIVERED), whereas the kitchen state machine models
    only cook-time transitions. The status change and the reservation release are
    part of the caller's single resolution transaction.
    """
    inventory_service.release_order_reservation(
        db, order, actor_type="STAFF", actor_user_id=staff.user_id, ip_address=ip_address
    )
    if order.status != "CANCELLED":
        old_status = order.status
        order.status = "CANCELLED"
        db.add(OrderStatusEvent(
            order_id=order.id,
            status_from=old_status,
            status_to="CANCELLED",
            actor_type="STAFF",
            actor_id=str(staff.user_id),
        ))


def _resolve_resolve_replay(
    db: Session, issue: OrderIssue, key_hash: str, request_hash: str
) -> OrderIssueResponse:
    """
    An already-resolved issue. The SAME resolve key + SAME payload replays the
    original result (no second refund); the same key + a different payload → 409;
    a different key against a resolved issue is simply "already resolved".
    """
    if issue.resolved_idempotency_key_hash == key_hash:
        if issue.resolved_request_hash == request_hash:
            return build_issue_response(db, issue, replay=True)
        raise _conflict(messages.ISSUE_IDEMPOTENCY_MISMATCH, error="idempotency_mismatch")
    raise _conflict(messages.ISSUE_ALREADY_RESOLVED, error="already_resolved")


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_issues(
    db: Session,
    staff: CurrentStaff,
    *,
    status: Optional[str] = None,
    issue_type: Optional[str] = None,
    order_id: Optional[int] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 100,
) -> OrderIssueListResponse:
    """Store-scoped issue history with optional filters."""
    q = db.query(OrderIssue).filter(OrderIssue.store_id == staff.store_id)

    if status:
        q = q.filter(OrderIssue.status == status)
    if issue_type:
        q = q.filter(OrderIssue.issue_type == issue_type)
    if order_id is not None:
        q = q.filter(OrderIssue.order_id == order_id)
    if date_from is not None:
        q = q.filter(OrderIssue.created_at >= date_from)
    if date_to is not None:
        q = q.filter(OrderIssue.created_at < date_to)

    limit = max(1, min(limit, 500))
    rows = q.order_by(OrderIssue.created_at.desc()).limit(limit).all()
    return OrderIssueListResponse(issues=[build_issue_response(db, r) for r in rows])


def list_order_issues(
    db: Session, staff: CurrentStaff, order_id: int
) -> OrderIssueListResponse:
    """Every issue raised against one order, store-scoped."""
    order = db.get(Order, order_id)
    if order is None or order.store_id != staff.store_id:
        # Non-disclosing: an order in another branch reads as having no issues.
        return OrderIssueListResponse(issues=[])
    rows = (
        db.query(OrderIssue)
        .filter(
            OrderIssue.store_id == staff.store_id,
            OrderIssue.order_id == order_id,
        )
        .order_by(OrderIssue.created_at.desc())
        .all()
    )
    return OrderIssueListResponse(issues=[build_issue_response(db, r) for r in rows])


def get_issue(db: Session, staff: CurrentStaff, issue_id: int) -> Optional[OrderIssueResponse]:
    issue = db.get(OrderIssue, issue_id)
    if issue is None or issue.store_id != staff.store_id:
        return None
    return build_issue_response(db, issue)
