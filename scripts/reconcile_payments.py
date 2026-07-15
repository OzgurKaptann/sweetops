#!/usr/bin/env python
"""
Read-only payment reconciliation.

Two independent checks, both READ-ONLY — reconciliation must never rewrite
financial history:

  1. ORDER SUMMARY vs LEDGER
     Each order's stored payment-summary fields (orders.paid_amount /
     orders.refunded_amount) against the append-only ledger (sum of completed
     allocations / refunds).

  2. CLOSED SHIFT SNAPSHOT vs LEDGER
     Each closed cashier shift's frozen snapshot against a fresh re-derivation of
     the ledger for the shift's own window (store + cashier + opened_at..closed_at,
     the exact attribution rule the shift service used). Verifies:
       * cash/card payment and refund totals,
       * gross = Σ payments, total refunds = Σ refunds, net = gross − refunds,
       * expected cash = opening + cash payments − cash refunds,
       * discrepancy = counted − expected.
     A mismatch means a snapshot was corrupted after the fact — the shift table's
     trigger makes that unrepresentable through the app, so this is a defence in
     depth check for direct-SQL tampering or a maths regression.

Usage:
    python scripts/reconcile_payments.py            # all stores
    python scripts/reconcile_payments.py --store 1  # one store
    python scripts/reconcile_payments.py --json     # machine-readable output

Exit code:
    0  every order summary AND every closed shift snapshot matches the ledger
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


def _q2(v) -> Decimal:
    return Decimal(str(v if v is not None else "0")).quantize(TWO)


def reconcile(store_id: int | None = None) -> list[dict]:
    """Return a list of per-order reconciliation rows that MISMATCH."""
    db = SessionLocal()
    try:
        where = "WHERE o.store_id = :sid" if store_id is not None else ""
        params = {"sid": store_id} if store_id is not None else {}
        rows = db.execute(
            text(
                f"""
                SELECT
                    o.id AS order_id,
                    o.store_id AS store_id,
                    COALESCE(o.paid_amount, 0)     AS stored_paid,
                    COALESCE(o.refunded_amount, 0) AS stored_refunded,
                    COALESCE((
                        SELECT SUM(a.amount)
                        FROM payment_allocations a
                        JOIN payment_settlements s ON s.id = a.settlement_id
                        WHERE a.order_id = o.id AND s.status = 'COMPLETED'
                    ), 0) AS ledger_paid,
                    COALESCE((
                        SELECT SUM(r.amount)
                        FROM payment_refunds r
                        WHERE r.order_id = o.id
                    ), 0) AS ledger_refunded
                FROM orders o
                {where}
                ORDER BY o.id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    mismatches: list[dict] = []
    for r in rows:
        stored_paid = _q2(r.stored_paid)
        stored_ref = _q2(r.stored_refunded)
        ledger_paid = _q2(r.ledger_paid)
        ledger_ref = _q2(r.ledger_refunded)
        if stored_paid != ledger_paid or stored_ref != ledger_ref:
            mismatches.append({
                "order_id": r.order_id,
                "store_id": r.store_id,
                "stored_paid_amount": str(stored_paid),
                "ledger_paid_amount": str(ledger_paid),
                "stored_refunded_amount": str(stored_ref),
                "ledger_refunded_amount": str(ledger_ref),
                "paid_mismatch": stored_paid != ledger_paid,
                "refunded_mismatch": stored_ref != ledger_ref,
            })
    return mismatches


def reconcile_shifts(store_id: int | None = None) -> list[dict]:
    """
    Return per-closed-shift reconciliation rows that MISMATCH their snapshot.

    Re-derives each closed shift's totals from the ledger using the SAME rule the
    shift service used: settlements this cashier collected in the window, and
    refunds of this cashier's money in the window (joined through the settlement).
    """
    db = SessionLocal()
    try:
        where = "AND cs.store_id = :sid" if store_id is not None else ""
        params = {"sid": store_id} if store_id is not None else {}
        rows = db.execute(
            text(
                f"""
                SELECT
                    cs.id AS shift_id,
                    cs.store_id AS store_id,
                    cs.cashier_user_id AS cashier_user_id,
                    cs.opening_cash_amount,
                    cs.counted_closing_cash_amount,
                    cs.cash_payments_amount,
                    cs.cash_refunds_amount,
                    cs.card_payments_amount,
                    cs.card_refunds_amount,
                    cs.gross_payments_amount,
                    cs.total_refunds_amount,
                    cs.net_collected_amount,
                    cs.expected_closing_cash_amount,
                    cs.cash_discrepancy_amount,
                    COALESCE((SELECT SUM(s.gross_amount) FROM payment_settlements s
                        WHERE s.store_id = cs.store_id AND s.cashier_user_id = cs.cashier_user_id
                          AND s.status = 'COMPLETED' AND s.payment_method = 'CASH'
                          AND s.completed_at >= cs.opened_at AND s.completed_at < cs.closed_at), 0) AS l_cash_pay,
                    COALESCE((SELECT SUM(s.gross_amount) FROM payment_settlements s
                        WHERE s.store_id = cs.store_id AND s.cashier_user_id = cs.cashier_user_id
                          AND s.status = 'COMPLETED' AND s.payment_method = 'CARD'
                          AND s.completed_at >= cs.opened_at AND s.completed_at < cs.closed_at), 0) AS l_card_pay,
                    COALESCE((SELECT SUM(s.gross_amount) FROM payment_settlements s
                        WHERE s.store_id = cs.store_id AND s.cashier_user_id = cs.cashier_user_id
                          AND s.status = 'COMPLETED'
                          AND s.completed_at >= cs.opened_at AND s.completed_at < cs.closed_at), 0) AS l_gross,
                    COALESCE((SELECT SUM(r.amount) FROM payment_refunds r
                        JOIN payment_settlements s ON s.id = r.settlement_id
                        WHERE r.store_id = cs.store_id AND s.cashier_user_id = cs.cashier_user_id
                          AND s.payment_method = 'CASH'
                          AND r.created_at >= cs.opened_at AND r.created_at < cs.closed_at), 0) AS l_cash_ref,
                    COALESCE((SELECT SUM(r.amount) FROM payment_refunds r
                        JOIN payment_settlements s ON s.id = r.settlement_id
                        WHERE r.store_id = cs.store_id AND s.cashier_user_id = cs.cashier_user_id
                          AND s.payment_method = 'CARD'
                          AND r.created_at >= cs.opened_at AND r.created_at < cs.closed_at), 0) AS l_card_ref,
                    COALESCE((SELECT SUM(r.amount) FROM payment_refunds r
                        JOIN payment_settlements s ON s.id = r.settlement_id
                        WHERE r.store_id = cs.store_id AND s.cashier_user_id = cs.cashier_user_id
                          AND r.created_at >= cs.opened_at AND r.created_at < cs.closed_at), 0) AS l_total_ref
                FROM cashier_shifts cs
                WHERE cs.status = 'CLOSED' {where}
                ORDER BY cs.id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    mismatches: list[dict] = []
    for r in rows:
        l_cash_pay = _q2(r.l_cash_pay)
        l_card_pay = _q2(r.l_card_pay)
        l_gross = _q2(r.l_gross)
        l_cash_ref = _q2(r.l_cash_ref)
        l_card_ref = _q2(r.l_card_ref)
        l_total_ref = _q2(r.l_total_ref)
        l_net = _q2(l_gross - l_total_ref)
        l_expected = _q2(_q2(r.opening_cash_amount) + l_cash_pay - l_cash_ref)
        l_discrepancy = _q2(_q2(r.counted_closing_cash_amount) - l_expected)

        problems = []
        if _q2(r.cash_payments_amount) != l_cash_pay:
            problems.append("cash_payments")
        if _q2(r.card_payments_amount) != l_card_pay:
            problems.append("card_payments")
        if _q2(r.cash_refunds_amount) != l_cash_ref:
            problems.append("cash_refunds")
        if _q2(r.card_refunds_amount) != l_card_ref:
            problems.append("card_refunds")
        if _q2(r.gross_payments_amount) != l_gross:
            problems.append("gross_payments")
        if _q2(r.total_refunds_amount) != l_total_ref:
            problems.append("total_refunds")
        if _q2(r.net_collected_amount) != l_net:
            problems.append("net_collected")
        if _q2(r.expected_closing_cash_amount) != l_expected:
            problems.append("expected_closing_cash")
        if _q2(r.cash_discrepancy_amount) != l_discrepancy:
            problems.append("cash_discrepancy")

        if problems:
            mismatches.append({
                "shift_id": r.shift_id,
                "store_id": r.store_id,
                "cashier_user_id": r.cashier_user_id,
                "fields": problems,
                "snapshot_expected_closing_cash": str(_q2(r.expected_closing_cash_amount)),
                "ledger_expected_closing_cash": str(l_expected),
                "snapshot_discrepancy": str(_q2(r.cash_discrepancy_amount)),
                "ledger_discrepancy": str(l_discrepancy),
            })
    return mismatches


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only payment reconciliation.")
    parser.add_argument("--store", type=int, default=None, help="Restrict to one store id.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    mismatches = reconcile(args.store)
    shift_mismatches = reconcile_shifts(args.store)
    total = len(mismatches) + len(shift_mismatches)

    if args.json:
        print(json.dumps({
            "mismatch_count": total,
            "order_mismatches": mismatches,
            "shift_mismatches": shift_mismatches,
        }, indent=2))
    else:
        scope = f"store {args.store}" if args.store is not None else "all stores"
        if total == 0:
            print(
                f"Reconciliation OK ({scope}): every order summary matches the ledger "
                "and every closed shift snapshot matches its window."
            )
        else:
            if mismatches:
                print(f"Reconciliation FOUND {len(mismatches)} order mismatch(es) ({scope}):")
                for m in mismatches:
                    print(
                        f"  order {m['order_id']} (store {m['store_id']}): "
                        f"paid stored={m['stored_paid_amount']} ledger={m['ledger_paid_amount']} | "
                        f"refunded stored={m['stored_refunded_amount']} ledger={m['ledger_refunded_amount']}"
                    )
            if shift_mismatches:
                print(f"Reconciliation FOUND {len(shift_mismatches)} shift mismatch(es) ({scope}):")
                for m in shift_mismatches:
                    print(
                        f"  shift {m['shift_id']} (store {m['store_id']}, cashier {m['cashier_user_id']}): "
                        f"fields={','.join(m['fields'])} | expected snapshot={m['snapshot_expected_closing_cash']} "
                        f"ledger={m['ledger_expected_closing_cash']}"
                    )

    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
