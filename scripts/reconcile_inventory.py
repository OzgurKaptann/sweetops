#!/usr/bin/env python
"""
Read-only inventory reconciliation.

Cross-checks the three independent records of what stock should be, per
ingredient:

    1. the SUMMARY      ingredient_stock.on_hand_quantity / reserved_quantity
    2. the LEDGER       SUM(ingredient_stock_movements.quantity_delta_on_hand)
    3. the ORDER LINES  SUM(reserved - consumed - released) over
                        order_inventory_lines — i.e. reservations still outstanding

The summary is a fast-query mirror; the ledger and the order lines are the
sources of truth it is derived from. If they disagree, something wrote stock
outside the inventory service and the summary can no longer be trusted.

It NEVER writes. A reconciliation that "fixes" drift by overwriting the summary
would destroy the very evidence needed to find the bug that caused it.

Usage:
    python scripts/reconcile_inventory.py                 # all ingredients
    python scripts/reconcile_inventory.py --ingredient 3  # one ingredient
    python scripts/reconcile_inventory.py --json          # machine-readable
    python scripts/reconcile_inventory.py --all           # include matching rows

Exit code:
    0  every ingredient's summary matches the ledger AND the order lines
    1  at least one mismatch (or a usage error)

Single-store note: inventory is GLOBAL in this schema (no store_id), so this
reports across the whole installation. Store-scoped reconciliation arrives with
refactor/store-scoped-inventory.

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


def reconcile(ingredient_id: int | None = None) -> list[dict]:
    """Return one row per ingredient with stored vs computed quantities."""
    db = SessionLocal()
    try:
        where = "WHERE s.ingredient_id = :iid" if ingredient_id is not None else ""
        params = {"iid": ingredient_id} if ingredient_id is not None else {}
        rows = db.execute(
            text(
                f"""
                SELECT
                    s.ingredient_id                        AS ingredient_id,
                    i.name                                 AS ingredient_name,
                    s.unit                                 AS unit,
                    COALESCE(s.on_hand_quantity, 0)        AS stored_on_hand,
                    COALESCE(s.reserved_quantity, 0)       AS stored_reserved,
                    COALESCE((
                        SELECT SUM(m.quantity_delta_on_hand)
                        FROM ingredient_stock_movements m
                        WHERE m.ingredient_id = s.ingredient_id
                    ), 0)                                  AS ledger_on_hand,
                    COALESCE((
                        SELECT SUM(l.reserved_quantity
                                   - l.consumed_quantity
                                   - l.released_quantity)
                        FROM order_inventory_lines l
                        WHERE l.ingredient_id = s.ingredient_id
                    ), 0)                                  AS lines_reserved
                FROM ingredient_stock s
                JOIN ingredients i ON i.id = s.ingredient_id
                {where}
                ORDER BY s.ingredient_id
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only inventory reconciliation.")
    parser.add_argument("--ingredient", type=int, default=None,
                        help="Restrict to one ingredient id.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--all", action="store_true",
                        help="Include ingredients that reconcile cleanly.")
    args = parser.parse_args()

    rows = reconcile(args.ingredient)
    mismatches = [r for r in rows if r["mismatch"]]
    reported = rows if args.all else mismatches

    if args.json:
        print(json.dumps({
            "checked_count": len(rows),
            "mismatch_count": len(mismatches),
            "results": reported,
        }, indent=2))
    else:
        scope = (
            f"ingredient {args.ingredient}" if args.ingredient is not None
            else "all ingredients"
        )
        if not mismatches:
            print(
                f"Inventory reconciliation OK ({scope}): "
                f"{len(rows)} ingredient(s); every summary matches the ledger "
                f"and the order inventory lines."
            )
            if args.all:
                for r in reported:
                    print(
                        f"  ingredient {r['ingredient_id']} ({r['ingredient_name']}): "
                        f"on-hand {r['stored_on_hand_quantity']} {r['unit']}, "
                        f"reserved {r['stored_reserved_quantity']} {r['unit']}"
                    )
        else:
            print(
                f"Inventory reconciliation FOUND {len(mismatches)} mismatch(es) "
                f"of {len(rows)} ingredient(s) ({scope}):"
            )
            for r in mismatches:
                print(f"  ingredient {r['ingredient_id']} ({r['ingredient_name']}):")
                if r["on_hand_mismatch"]:
                    print(
                        f"    on-hand  stored={r['stored_on_hand_quantity']} "
                        f"ledger={r['computed_on_hand_from_ledger']} "
                        f"drift={r['on_hand_mismatch_amount']} {r['unit']}"
                    )
                if r["reserved_mismatch"]:
                    print(
                        f"    reserved stored={r['stored_reserved_quantity']} "
                        f"order_lines={r['computed_reserved_from_order_lines']} "
                        f"drift={r['reserved_mismatch_amount']} {r['unit']}"
                    )

    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
