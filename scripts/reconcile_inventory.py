#!/usr/bin/env python
"""
Read-only, STORE-SCOPED inventory reconciliation.

Cross-checks the three independent records of what stock should be, per
(store, ingredient):

    1. the SUMMARY      ingredient_stock.on_hand_quantity / reserved_quantity
    2. the LEDGER       SUM(ingredient_stock_movements.quantity_delta_on_hand)
    3. the ORDER LINES  SUM(reserved - consumed - released) over
                        order_inventory_lines — i.e. reservations still outstanding

The summary is a fast-query mirror; the ledger and the order lines are the
sources of truth it is derived from. If they disagree, something wrote stock
outside the inventory service and the summary can no longer be trusted.

Why the store is part of the grain
----------------------------------
Reconciling across stores would be worse than not reconciling at all. Suppose
Kadıköy is 500 g of pistachio SHORT and Beşiktaş is 500 g OVER — two real,
serious, opposite faults. Summed into one global figure they are zero, and the
report says everything is fine. Mismatches must never be allowed to cancel each
other out across branches, so every total here is computed per store, and every
mismatch names the store it belongs to.

It NEVER writes. A reconciliation that "fixes" drift by overwriting the summary
would destroy the very evidence needed to find the bug that caused it.

Usage:
    python scripts/reconcile_inventory.py                    # every store, grouped by store
    python scripts/reconcile_inventory.py --store-id 2       # one store
    python scripts/reconcile_inventory.py --ingredient 3     # one ingredient, all stores
    python scripts/reconcile_inventory.py --json             # machine-readable
    python scripts/reconcile_inventory.py --all              # include matching rows

Exit code:
    0  every (store, ingredient) summary matches its ledger AND its order lines
    1  at least one mismatch in ANY store (or a usage error)

No credentials, tokens or idempotency keys are ever printed.
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

THREE = Decimal("0.001")


def _q3(v) -> Decimal:
    return Decimal(str(v if v is not None else "0")).quantize(THREE)


def reconcile(
    store_id: int | None = None,
    ingredient_id: int | None = None,
) -> list[dict]:
    """
    Return one row per (store, ingredient) with stored vs computed quantities.

    Every correlated subquery is keyed on BOTH s.store_id and s.ingredient_id.
    Dropping the store from either one would silently pool another branch's
    movements into this branch's expected total — which is exactly the class of
    bug this script exists to catch, so it must not commit it itself.
    """
    db = SessionLocal()
    try:
        filters = []
        params: dict = {}
        if store_id is not None:
            filters.append("s.store_id = :sid")
            params["sid"] = store_id
        if ingredient_id is not None:
            filters.append("s.ingredient_id = :iid")
            params["iid"] = ingredient_id
        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        rows = db.execute(
            text(
                f"""
                SELECT
                    s.store_id                             AS store_id,
                    st.name                                AS store_name,
                    s.ingredient_id                        AS ingredient_id,
                    i.name                                 AS ingredient_name,
                    s.unit                                 AS unit,
                    COALESCE(s.on_hand_quantity, 0)        AS stored_on_hand,
                    COALESCE(s.reserved_quantity, 0)       AS stored_reserved,
                    COALESCE((
                        SELECT SUM(m.quantity_delta_on_hand)
                        FROM ingredient_stock_movements m
                        WHERE m.store_id      = s.store_id
                          AND m.ingredient_id = s.ingredient_id
                    ), 0)                                  AS ledger_on_hand,
                    COALESCE((
                        SELECT SUM(l.reserved_quantity
                                   - l.consumed_quantity
                                   - l.released_quantity)
                        FROM order_inventory_lines l
                        WHERE l.store_id      = s.store_id
                          AND l.ingredient_id = s.ingredient_id
                    ), 0)                                  AS lines_reserved
                FROM ingredient_stock s
                JOIN ingredients i ON i.id = s.ingredient_id
                JOIN stores      st ON st.id = s.store_id
                {where}
                ORDER BY s.store_id, s.ingredient_id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    results: list[dict] = []
    for r in rows:
        stored_on_hand = _q3(r.stored_on_hand)
        stored_reserved = _q3(r.stored_reserved)
        ledger_on_hand = _q3(r.ledger_on_hand)
        lines_reserved = _q3(r.lines_reserved)

        on_hand_diff = stored_on_hand - ledger_on_hand
        reserved_diff = stored_reserved - lines_reserved

        results.append({
            "store_id": r.store_id,
            "store_name": r.store_name,
            "ingredient_id": r.ingredient_id,
            "ingredient_name": r.ingredient_name,
            "unit": r.unit,
            "stored_on_hand_quantity": str(stored_on_hand),
            "computed_on_hand_from_ledger": str(ledger_on_hand),
            "on_hand_mismatch_amount": str(on_hand_diff),
            "stored_reserved_quantity": str(stored_reserved),
            "computed_reserved_from_order_lines": str(lines_reserved),
            "reserved_mismatch_amount": str(reserved_diff),
            "on_hand_mismatch": on_hand_diff != 0,
            "reserved_mismatch": reserved_diff != 0,
            "mismatch": on_hand_diff != 0 or reserved_diff != 0,
        })
    return results


def _group_by_store(rows: list[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["store_id"], []).append(row)
    return dict(sorted(grouped.items()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only, store-scoped inventory reconciliation."
    )
    parser.add_argument("--store-id", type=int, default=None,
                        help="Restrict to one store. Omit to reconcile every store, "
                             "each grouped and totalled separately.")
    parser.add_argument("--ingredient", type=int, default=None,
                        help="Restrict to one ingredient id.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--all", action="store_true",
                        help="Include rows that reconcile cleanly.")
    args = parser.parse_args()

    rows = reconcile(store_id=args.store_id, ingredient_id=args.ingredient)
    mismatches = [r for r in rows if r["mismatch"]]
    by_store = _group_by_store(rows)

    if args.json:
        # Per-store counts as well as the overall count: a caller must be able to
        # see that store 1 is clean and store 2 is not, without those two facts
        # ever having been added together.
        print(json.dumps({
            "checked_count": len(rows),
            "mismatch_count": len(mismatches),
            "stores": [
                {
                    "store_id": sid,
                    "store_name": store_rows[0]["store_name"],
                    "checked_count": len(store_rows),
                    "mismatch_count": sum(1 for r in store_rows if r["mismatch"]),
                    "results": [
                        r for r in store_rows if args.all or r["mismatch"]
                    ],
                }
                for sid, store_rows in by_store.items()
            ],
        }, indent=2))
        return 1 if mismatches else 0

    scope = "all stores" if args.store_id is None else f"store {args.store_id}"
    if args.ingredient is not None:
        scope += f", ingredient {args.ingredient}"

    if not rows:
        print(f"Inventory reconciliation: no stock rows found ({scope}).")
        return 0

    if not mismatches:
        print(
            f"Inventory reconciliation OK ({scope}): {len(rows)} (store, ingredient) "
            f"row(s) across {len(by_store)} store(s); every summary matches its "
            f"ledger and its order inventory lines."
        )
    else:
        print(
            f"Inventory reconciliation FOUND {len(mismatches)} mismatch(es) of "
            f"{len(rows)} (store, ingredient) row(s) ({scope}):"
        )

    for sid, store_rows in by_store.items():
        store_mismatches = [r for r in store_rows if r["mismatch"]]
        reported = store_rows if args.all else store_mismatches
        if not reported:
            continue

        name = store_rows[0]["store_name"]
        print(f"\n  store {sid} ({name}) — "
              f"{len(store_mismatches)} mismatch(es) of {len(store_rows)} row(s)")

        for r in reported:
            if not r["mismatch"]:
                print(
                    f"    ok  ingredient {r['ingredient_id']} ({r['ingredient_name']}): "
                    f"on-hand {r['stored_on_hand_quantity']} {r['unit']}, "
                    f"reserved {r['stored_reserved_quantity']} {r['unit']}"
                )
                continue
            print(f"    MISMATCH ingredient {r['ingredient_id']} ({r['ingredient_name']}):")
            if r["on_hand_mismatch"]:
                print(
                    f"      on-hand  stored={r['stored_on_hand_quantity']} "
                    f"ledger={r['computed_on_hand_from_ledger']} "
                    f"drift={r['on_hand_mismatch_amount']} {r['unit']}"
                )
            if r["reserved_mismatch"]:
                print(
                    f"      reserved stored={r['stored_reserved_quantity']} "
                    f"order_lines={r['computed_reserved_from_order_lines']} "
                    f"drift={r['reserved_mismatch_amount']} {r['unit']}"
                )

    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
