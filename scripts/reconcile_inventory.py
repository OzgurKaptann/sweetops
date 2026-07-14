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

...and, since the transfer workflow, a fourth check that is about PAIRS of rows
rather than totals:

    4. TRANSFER PAIRING every completed transfer has exactly one TRANSFER_OUT in
                        its source store and exactly one TRANSFER_IN in its
                        destination store, for its ingredient and its quantity

A transfer's two legs are ordinary ledger deltas, so check 1 already accounts for
them store by store: the outbound leg lowers the source's on-hand, the inbound
leg raises the destination's, and each store reconciles on its own. What check 1
cannot see is a HALF transfer. Stock that left Kadıköy and arrived nowhere leaves
Kadıköy's ledger and summary in perfect agreement with each other — both are
simply 2 kg short of physical reality, and nothing in a per-store total is wrong.
Only comparing the transfer against its legs finds it. The database refuses to
create one (a deferred constraint trigger checks the pairing at COMMIT), so this
check is here to catch what got in some OTHER way: a manual SQL edit, a restore
from an inconsistent backup, a future migration bug.

...and, since the physical stock count workflow, a fifth check of the same shape:

    5. COUNT / MOVEMENT every stock count has exactly the ledger movement its own
                        delta demands — ONE matching STOCK_COUNT_ADJUSTMENT when the
                        delta is non-zero, and NONE when it is zero

A count's correction is an ORDINARY on-hand delta, so check 1 already accounts for
it: the STOCK_COUNT_ADJUSTMENT is summed with every other movement and the store
reconciles on its own. It is deliberately NOT special-cased or excluded — a count
saying the shelf holds 350 g less really does mean the shelf holds 350 g less.

What check 1 cannot see is a count whose movement is MISSING or WRONG. A count row
claiming the shelf was corrected, with no movement behind it, leaves the ledger and
the summary in perfect agreement with each other — both simply still hold the old
figure, and nothing in a per-store total is wrong. The count sheet and the ledger
then tell different stories and each looks internally consistent. Only comparing the
count against its movement finds it.

Zero-delta counts are the documented policy and are NOT drift: the shelf agreed with
the system, nothing physical happened, and no movement was written. Such a count
contributes nothing to the ledger and must never be reported as a mismatch — it is
evidence that the shelf was checked, which is exactly what it is for.

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


def reconcile_transfers(
    store_id: int | None = None,
    ingredient_id: int | None = None,
) -> list[dict]:
    """
    Return one row per BROKEN transfer — a transfer whose two legs do not match it.

    A healthy transfer produces exactly two ledger rows:

        TRANSFER_OUT  in source_store_id       on_hand -quantity, reserved 0
        TRANSFER_IN   in destination_store_id  on_hand +quantity, reserved 0

    both for the transfer's ingredient and its quantity. Anything else is a
    transfer that cannot be trusted, and this counts each side independently so
    the report can say WHICH half is wrong rather than merely that something is:

        out_count = 0  the stock left nowhere — the source never gave it up
        in_count  = 0  the stock arrived nowhere — it left the source and vanished
        out/in > 1     the same shipment was posted twice on one side

    A transfer is included when EITHER of its stores matches ``--store-id``. A
    one-sided transfer is exactly as much of a problem for the branch that did not
    get its crate as for the branch that shipped it, so filtering to one store must
    not hide it from the other.
    """
    db = SessionLocal()
    try:
        filters = []
        params: dict = {}
        if store_id is not None:
            filters.append("(t.source_store_id = :sid OR t.destination_store_id = :sid)")
            params["sid"] = store_id
        if ingredient_id is not None:
            filters.append("t.ingredient_id = :iid")
            params["iid"] = ingredient_id
        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        rows = db.execute(
            text(
                f"""
                SELECT
                    t.id                   AS transfer_id,
                    t.source_store_id      AS source_store_id,
                    t.destination_store_id AS destination_store_id,
                    t.ingredient_id        AS ingredient_id,
                    i.name                 AS ingredient_name,
                    t.quantity             AS quantity,
                    t.unit                 AS unit,
                    t.status               AS status,
                    COALESCE((
                        SELECT COUNT(*)
                        FROM ingredient_stock_movements m
                        WHERE m.transfer_id             = t.id
                          AND m.movement_type           = 'TRANSFER_OUT'
                          AND m.store_id                = t.source_store_id
                          AND m.ingredient_id           = t.ingredient_id
                          AND m.quantity                = t.quantity
                          AND m.quantity_delta_on_hand  = -t.quantity
                          AND m.quantity_delta_reserved = 0
                    ), 0)                  AS out_count,
                    COALESCE((
                        SELECT COUNT(*)
                        FROM ingredient_stock_movements m
                        WHERE m.transfer_id             = t.id
                          AND m.movement_type           = 'TRANSFER_IN'
                          AND m.store_id                = t.destination_store_id
                          AND m.ingredient_id           = t.ingredient_id
                          AND m.quantity                = t.quantity
                          AND m.quantity_delta_on_hand  = t.quantity
                          AND m.quantity_delta_reserved = 0
                    ), 0)                  AS in_count
                FROM inventory_transfers t
                JOIN ingredients i ON i.id = t.ingredient_id
                {where}
                ORDER BY t.id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    results: list[dict] = []
    for r in rows:
        out_count = int(r.out_count)
        in_count = int(r.in_count)
        if out_count == 1 and in_count == 1:
            continue  # healthy — every leg present, matching and correctly signed
        results.append({
            "transfer_id": r.transfer_id,
            "source_store_id": r.source_store_id,
            "destination_store_id": r.destination_store_id,
            "ingredient_id": r.ingredient_id,
            "ingredient_name": r.ingredient_name,
            "quantity": str(_q3(r.quantity)),
            "unit": r.unit,
            "status": r.status,
            "transfer_out_movements": out_count,
            "transfer_in_movements": in_count,
            "issue": _transfer_issue(out_count, in_count),
        })
    return results


def _transfer_issue(out_count: int, in_count: int) -> str:
    if out_count == 0 and in_count == 0:
        return "no ledger movements at all"
    if out_count == 0:
        return "missing TRANSFER_OUT — destination gained stock the source never gave up"
    if in_count == 0:
        return "missing TRANSFER_IN — stock left the source and arrived nowhere"
    return f"duplicated legs (out={out_count}, in={in_count})"


def reconcile_stock_counts(
    store_id: int | None = None,
    ingredient_id: int | None = None,
) -> list[dict]:
    """
    Return one row per BROKEN stock count — a count whose ledger movement does not
    match it.

    A healthy count produces exactly the movement its own delta demands:

        delta <> 0   ONE  STOCK_COUNT_ADJUSTMENT in the count's store, for its
                          ingredient, with quantity = abs(delta),
                          quantity_delta_on_hand = delta, quantity_delta_reserved = 0
        delta  = 0   NONE — the shelf agreed with the system, so nothing physical
                          happened and nothing belongs in the physical ledger

    Anything else is a count that cannot be trusted, and the two cases are reported
    apart because they mean opposite things:

        non-zero delta, no movement   the shelf was corrected on PAPER only. The
                                      system still believes the old figure, and the
                                      count sheet says otherwise.
        zero delta, a movement        stock was moved by a count that found nothing
                                      wrong — an unexplained correction wearing a
                                      count's clothes.
        wrong amount / direction      the ledger moved a different quantity from the
                                      one the count says it found.
        duplicated                    the same correction posted twice.

    A zero-delta count with no movement is HEALTHY and is never reported.
    """
    db = SessionLocal()
    try:
        filters = []
        params: dict = {}
        if store_id is not None:
            filters.append("c.store_id = :sid")
            params["sid"] = store_id
        if ingredient_id is not None:
            filters.append("c.ingredient_id = :iid")
            params["iid"] = ingredient_id
        where = f"WHERE {' AND '.join(filters)}" if filters else ""

        rows = db.execute(
            text(
                f"""
                SELECT
                    c.id                  AS stock_count_id,
                    c.store_id            AS store_id,
                    st.name               AS store_name,
                    c.ingredient_id       AS ingredient_id,
                    i.name                AS ingredient_name,
                    c.counted_quantity    AS counted_quantity,
                    c.system_on_hand_quantity AS system_on_hand_quantity,
                    c.delta_quantity      AS delta_quantity,
                    c.unit                AS unit,
                    c.status              AS status,
                    COALESCE((
                        SELECT COUNT(*)
                        FROM ingredient_stock_movements m
                        WHERE m.stock_count_id          = c.id
                          AND m.movement_type           = 'STOCK_COUNT_ADJUSTMENT'
                          AND m.store_id                = c.store_id
                          AND m.ingredient_id           = c.ingredient_id
                          AND m.quantity                = abs(c.delta_quantity)
                          AND m.quantity_delta_on_hand  = c.delta_quantity
                          AND m.quantity_delta_reserved = 0
                    ), 0)                 AS matching_count,
                    COALESCE((
                        SELECT COUNT(*)
                        FROM ingredient_stock_movements m
                        WHERE m.stock_count_id = c.id
                    ), 0)                 AS movement_count
                FROM inventory_stock_counts c
                JOIN ingredients i  ON i.id  = c.ingredient_id
                JOIN stores      st ON st.id = c.store_id
                {where}
                ORDER BY c.id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    results: list[dict] = []
    for r in rows:
        delta = _q3(r.delta_quantity)
        matching = int(r.matching_count)
        total = int(r.movement_count)

        if delta == 0 and total == 0:
            continue  # healthy: the shelf was checked and found correct
        if delta != 0 and matching == 1 and total == 1:
            continue  # healthy: exactly the movement the delta demands

        results.append({
            "stock_count_id": r.stock_count_id,
            "store_id": r.store_id,
            "store_name": r.store_name,
            "ingredient_id": r.ingredient_id,
            "ingredient_name": r.ingredient_name,
            "counted_quantity": str(_q3(r.counted_quantity)),
            "system_on_hand_quantity": str(_q3(r.system_on_hand_quantity)),
            "delta_quantity": str(delta),
            "unit": r.unit,
            "status": r.status,
            "matching_movements": matching,
            "total_movements": total,
            "issue": _stock_count_issue(delta, matching, total),
        })
    return results


def _stock_count_issue(delta: Decimal, matching: int, total: int) -> str:
    if delta == 0:
        return (
            f"zero-delta count has {total} ledger movement(s) — stock was moved by a "
            "count that found nothing wrong"
        )
    if total == 0:
        return (
            "missing STOCK_COUNT_ADJUSTMENT — the shelf was corrected on paper only, "
            "and the system still believes the pre-count figure"
        )
    if total > 1:
        return f"duplicated movements (total={total}, matching={matching})"
    return (
        "movement does not match the count — wrong quantity, wrong direction, wrong "
        "store/ingredient, or it moved reserved stock"
    )


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

    # Transfer pairing is a separate axis: a half-posted transfer leaves every
    # per-store total internally consistent and is invisible above.
    broken_transfers = reconcile_transfers(
        store_id=args.store_id, ingredient_id=args.ingredient
    )
    # ...and so is count/movement agreement, for exactly the same reason.
    broken_counts = reconcile_stock_counts(
        store_id=args.store_id, ingredient_id=args.ingredient
    )

    if args.json:
        # Per-store counts as well as the overall count: a caller must be able to
        # see that store 1 is clean and store 2 is not, without those two facts
        # ever having been added together.
        print(json.dumps({
            "checked_count": len(rows),
            "mismatch_count": len(mismatches),
            "broken_transfer_count": len(broken_transfers),
            "broken_stock_count_count": len(broken_counts),
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
            "broken_transfers": broken_transfers,
            "broken_stock_counts": broken_counts,
        }, indent=2))
        return 1 if (mismatches or broken_transfers or broken_counts) else 0

    scope = "all stores" if args.store_id is None else f"store {args.store_id}"
    if args.ingredient is not None:
        scope += f", ingredient {args.ingredient}"

    if not rows and not broken_transfers and not broken_counts:
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

    # ── Transfer pairing ──────────────────────────────────────────────────
    if not broken_transfers:
        print(
            f"\n  transfers: every completed transfer has exactly one TRANSFER_OUT "
            f"and one TRANSFER_IN matching it ({scope})."
        )
    else:
        print(f"\n  BROKEN TRANSFERS ({len(broken_transfers)}):")
        for t in broken_transfers:
            print(
                f"    transfer {t['transfer_id']}: "
                f"store {t['source_store_id']} → {t['destination_store_id']}, "
                f"ingredient {t['ingredient_id']} ({t['ingredient_name']}), "
                f"{t['quantity']} {t['unit']}"
            )
            print(
                f"      {t['issue']} "
                f"(TRANSFER_OUT rows={t['transfer_out_movements']}, "
                f"TRANSFER_IN rows={t['transfer_in_movements']})"
            )

    # ── Stock count / movement agreement ──────────────────────────────────
    if not broken_counts:
        print(
            f"\n  stock counts: every count has exactly the ledger movement its "
            f"delta demands ({scope})."
        )
    else:
        print(f"\n  BROKEN STOCK COUNTS ({len(broken_counts)}):")
        for c in broken_counts:
            print(
                f"    stock_count {c['stock_count_id']}: "
                f"store {c['store_id']} ({c['store_name']}), "
                f"ingredient {c['ingredient_id']} ({c['ingredient_name']}), "
                f"counted {c['counted_quantity']} {c['unit']} "
                f"vs system {c['system_on_hand_quantity']} {c['unit']} "
                f"(delta {c['delta_quantity']} {c['unit']})"
            )
            print(
                f"      {c['issue']} "
                f"(matching movements={c['matching_movements']}, "
                f"total={c['total_movements']})"
            )

    return 1 if (mismatches or broken_transfers or broken_counts) else 0


if __name__ == "__main__":
    raise SystemExit(main())
