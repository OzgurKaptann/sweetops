#!/usr/bin/env python
"""
Read-only payment reconciliation.

Compares each order's stored payment-summary fields (orders.paid_amount /
orders.refunded_amount) against the append-only ledger (sum of completed
allocations / refunds). It NEVER writes — normal reconciliation must not
rewrite financial history.

Usage:
    python scripts/reconcile_payments.py            # all stores
    python scripts/reconcile_payments.py --store 1  # one store
    python scripts/reconcile_payments.py --json     # machine-readable output

Exit code:
    0  every order's summary matches the ledger
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only payment reconciliation.")
    parser.add_argument("--store", type=int, default=None, help="Restrict to one store id.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    mismatches = reconcile(args.store)

    if args.json:
        print(json.dumps({"mismatch_count": len(mismatches), "mismatches": mismatches}, indent=2))
    else:
        scope = f"store {args.store}" if args.store is not None else "all stores"
        if not mismatches:
            print(f"Reconciliation OK ({scope}): every order summary matches the ledger.")
        else:
            print(f"Reconciliation FOUND {len(mismatches)} mismatch(es) ({scope}):")
            for m in mismatches:
                print(
                    f"  order {m['order_id']} (store {m['store_id']}): "
                    f"paid stored={m['stored_paid_amount']} ledger={m['ledger_paid_amount']} | "
                    f"refunded stored={m['stored_refunded_amount']} ledger={m['ledger_refunded_amount']}"
                )

    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
