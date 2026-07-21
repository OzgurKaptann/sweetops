#!/usr/bin/env python
"""
Read-only order-issue reconciliation.

Verifies that every resolved order issue tells the same story as the payment refund
ledger it drove. READ-ONLY — reconciliation must never rewrite history; it only
re-derives and compares.

Checks (per issue):
  1. LINK VALID          a FULL/PARTIAL refund resolution with a positive approved
                         amount has refund_id set, and it points at a refund that
                         is itself linked back to this issue.
  2. AMOUNT MATCHES      Σ(payment_refunds linked to this issue) == approved amount.
  3. SAME CONTEXT        every linked refund is the SAME store AND the SAME order as
                         the issue.
  4. STORE MATCHES       the issue's store == its order's store.
  5. NO STRAY REFUND     a NO_REFUND / CANCEL_ONLY resolution (and any OPEN issue)
                         has NO linked refunds and no refund_id.
  6. NO DUPLICATE        the linked refunds never sum to MORE than the approved
                         amount (a double refund for one issue).

Also, once across the ledger:
  7. WITHIN ORDER        no order's total refunds exceed what was paid on it.

Usage:
    python scripts/reconcile_order_issues.py            # all stores
    python scripts/reconcile_order_issues.py --store 1  # one store
    python scripts/reconcile_order_issues.py --json     # machine-readable output

Exit code:
    0  every resolved issue matches the refund ledger
    1  at least one mismatch (or a usage error)

No credentials, tokens or card data are ever printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

# Make ``app`` importable when run from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from sqlalchemy import text  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402

TWO = Decimal("0.01")
REFUNDING = ("FULL_REFUND", "PARTIAL_REFUND")


def _q2(v) -> Decimal:
    return Decimal(str(v if v is not None else "0")).quantize(TWO)


def reconcile_issues(store_id: int | None = None) -> list[dict]:
    """Return per-issue reconciliation rows that MISMATCH the refund ledger."""
    db = SessionLocal()
    try:
        where = "WHERE i.store_id = :sid" if store_id is not None else ""
        params = {"sid": store_id} if store_id is not None else {}
        rows = db.execute(
            text(
                f"""
                SELECT
                    i.id               AS issue_id,
                    i.store_id         AS issue_store_id,
                    i.order_id         AS issue_order_id,
                    i.status           AS status,
                    i.resolution_type  AS resolution_type,
                    i.refund_id        AS refund_id,
                    COALESCE(i.approved_refund_amount, 0) AS approved,
                    o.store_id         AS order_store_id,
                    COALESCE((
                        SELECT SUM(r.amount) FROM payment_refunds r
                        WHERE r.order_issue_id = i.id
                    ), 0)              AS linked_sum,
                    (
                        SELECT COUNT(*) FROM payment_refunds r
                        WHERE r.order_issue_id = i.id
                    )                  AS linked_count,
                    (
                        SELECT COUNT(*) FROM payment_refunds r
                        WHERE r.order_issue_id = i.id
                          AND (r.store_id <> i.store_id OR r.order_id <> i.order_id)
                    )                  AS wrong_context_count,
                    (
                        SELECT COUNT(*) FROM payment_refunds r
                        WHERE r.id = i.refund_id AND r.order_issue_id = i.id
                    )                  AS primary_link_ok
                FROM order_issues i
                JOIN orders o ON o.id = i.order_id
                {where}
                ORDER BY i.id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    mismatches: list[dict] = []
    for r in rows:
        approved = _q2(r.approved)
        linked_sum = _q2(r.linked_sum)
        problems: list[str] = []

        # 4. STORE MATCHES.
        if r.issue_store_id != r.order_store_id:
            problems.append("store_mismatch")

        # 3. SAME CONTEXT (store + order) for every linked refund.
        if r.wrong_context_count:
            problems.append("refund_context_mismatch")

        is_refunding = r.status in ("RESOLVED", "VOIDED") and r.resolution_type in REFUNDING

        if is_refunding and approved > 0:
            # 1. LINK VALID.
            if r.refund_id is None or not r.primary_link_ok:
                problems.append("missing_or_broken_refund_link")
            # 2. AMOUNT MATCHES / 6. NO DUPLICATE.
            if linked_sum != approved:
                problems.append(
                    "refund_amount_over" if linked_sum > approved else "refund_amount_under"
                )
        else:
            # 5. NO STRAY REFUND for non-refunding / open issues.
            if r.linked_count or r.refund_id is not None:
                problems.append("unexpected_refund_link")

        if problems:
            mismatches.append({
                "issue_id": r.issue_id,
                "store_id": r.issue_store_id,
                "order_id": r.issue_order_id,
                "status": r.status,
                "resolution_type": r.resolution_type,
                "approved_refund_amount": str(approved),
                "linked_refund_sum": str(linked_sum),
                "refund_id": r.refund_id,
                "problems": problems,
            })
    return mismatches


def reconcile_order_refunds(store_id: int | None = None) -> list[dict]:
    """Check 7: no order's total refunds exceed what was paid on it."""
    db = SessionLocal()
    try:
        where = "WHERE o.store_id = :sid" if store_id is not None else ""
        params = {"sid": store_id} if store_id is not None else {}
        rows = db.execute(
            text(
                f"""
                SELECT o.id AS order_id, o.store_id AS store_id,
                       COALESCE(o.paid_amount, 0) AS paid,
                       COALESCE((SELECT SUM(r.amount) FROM payment_refunds r
                                 WHERE r.order_id = o.id), 0) AS refunded
                FROM orders o
                {where}
                ORDER BY o.id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    over: list[dict] = []
    for r in rows:
        if _q2(r.refunded) > _q2(r.paid):
            over.append({
                "order_id": r.order_id,
                "store_id": r.store_id,
                "paid_amount": str(_q2(r.paid)),
                "refunded_amount": str(_q2(r.refunded)),
            })
    return over


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only order-issue reconciliation.")
    parser.add_argument("--store", type=int, default=None, help="Restrict to one store id.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    issue_mismatches = reconcile_issues(args.store)
    refund_overruns = reconcile_order_refunds(args.store)
    total = len(issue_mismatches) + len(refund_overruns)

    if args.json:
        print(json.dumps({
            "mismatch_count": total,
            "issue_mismatches": issue_mismatches,
            "order_refund_overruns": refund_overruns,
        }, indent=2))
    else:
        scope = f"store {args.store}" if args.store is not None else "all stores"
        if total == 0:
            print(
                f"Order-issue reconciliation OK ({scope}): every resolved issue "
                "matches the refund ledger."
            )
        else:
            if issue_mismatches:
                print(f"FOUND {len(issue_mismatches)} issue mismatch(es) ({scope}):")
                for m in issue_mismatches:
                    print(
                        f"  issue {m['issue_id']} (store {m['store_id']}, order {m['order_id']}): "
                        f"{','.join(m['problems'])} | approved={m['approved_refund_amount']} "
                        f"linked={m['linked_refund_sum']}"
                    )
            if refund_overruns:
                print(f"FOUND {len(refund_overruns)} order refund overrun(s) ({scope}):")
                for m in refund_overruns:
                    print(
                        f"  order {m['order_id']} (store {m['store_id']}): "
                        f"refunded={m['refunded_amount']} paid={m['paid_amount']}"
                    )

    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
