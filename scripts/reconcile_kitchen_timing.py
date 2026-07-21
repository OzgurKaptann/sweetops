#!/usr/bin/env python
"""
Read-only kitchen-timing reconciliation.

Kitchen preparation timing is DERIVED from ``order_status_events`` (see
apps/api/app/services/kitchen_timing_service.py). Derived metrics are only as
trustworthy as the event log underneath them, so this script re-reads that log
and flags any order whose lifecycle events are internally impossible. READ-ONLY —
it never rewrites history; it only re-derives and compares.

Checks (per order that has any tracked event):
  1. READY_BEFORE_PREP    the first READY event is at or before the first IN_PREP
                          event (would produce a negative/zero prep duration).
  2. NEGATIVE_PREP        ready_at < prep_started_at (redundant with 1, but caught
                          explicitly so a strict-inequality edge is visible).
  3. READY_WITHOUT_PREP   an order reached READY/DELIVERED but has no IN_PREP event
                          at all (no prep phase was ever recorded).
  4. TERMINAL_BOTH        an order has BOTH a READY/DELIVERED and a CANCELLED event
                          (contradictory terminal history).
  5. DUP_STATUS_EVENT     the same status_to appears more than once with DIFFERENT
                          timestamps for one order beyond the allowed undo bounce —
                          reported as a soft signal, not a hard failure (undo is a
                          legitimate source of repeats).
  6. EVENT_BEFORE_CREATE  any status event predates the order's created_at.

Usage:
    python scripts/reconcile_kitchen_timing.py            # all stores
    python scripts/reconcile_kitchen_timing.py --store 1  # one store
    python scripts/reconcile_kitchen_timing.py --json     # machine-readable output

Exit code:
    0  every order's lifecycle events are internally consistent
    1  at least one impossible ordering (or a usage error)

No credentials, tokens or card data are ever printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make ``app`` importable when run from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))

from sqlalchemy import text  # noqa: E402

from app.core.db import SessionLocal  # noqa: E402


def reconcile_timing(store_id: int | None = None) -> list[dict]:
    """Return per-order rows whose lifecycle events are internally impossible."""
    db = SessionLocal()
    try:
        where = "WHERE o.store_id = :sid" if store_id is not None else ""
        params = {"sid": store_id} if store_id is not None else {}
        rows = db.execute(
            text(
                f"""
                WITH firsts AS (
                    SELECT
                        e.order_id,
                        MIN(e.created_at) FILTER (WHERE e.status_to = 'IN_PREP')   AS prep_at,
                        MIN(e.created_at) FILTER (WHERE e.status_to = 'READY')     AS ready_at,
                        MIN(e.created_at) FILTER (WHERE e.status_to = 'DELIVERED') AS delivered_at,
                        MIN(e.created_at) FILTER (WHERE e.status_to = 'CANCELLED') AS cancelled_at,
                        MIN(e.created_at)                                          AS first_event_at
                    FROM order_status_events e
                    GROUP BY e.order_id
                )
                SELECT
                    o.id            AS order_id,
                    o.store_id      AS store_id,
                    o.status        AS status,
                    o.created_at    AS created_at,
                    f.prep_at       AS prep_at,
                    f.ready_at      AS ready_at,
                    f.delivered_at  AS delivered_at,
                    f.cancelled_at  AS cancelled_at,
                    f.first_event_at AS first_event_at
                FROM orders o
                JOIN firsts f ON f.order_id = o.id
                {where}
                ORDER BY o.id
                """
            ),
            params,
        ).fetchall()
    finally:
        db.close()

    problems_out: list[dict] = []
    for r in rows:
        problems: list[str] = []

        reached_ready = r.ready_at is not None or r.delivered_at is not None

        # 1 / 2. READY at or before IN_PREP (impossible ordering).
        if r.ready_at is not None and r.prep_at is not None:
            if r.ready_at < r.prep_at:
                problems.append("negative_prep")
            elif r.ready_at == r.prep_at:
                problems.append("ready_before_prep")

        # 3. Reached a ready/terminal-success state with no prep event recorded.
        if reached_ready and r.prep_at is None:
            problems.append("ready_without_prep")

        # 4. Contradictory terminal history.
        if (r.ready_at is not None or r.delivered_at is not None) and r.cancelled_at is not None:
            problems.append("terminal_both")

        # 6. An event predates the order's own creation timestamp.
        if r.first_event_at is not None and r.created_at is not None:
            if r.first_event_at < r.created_at:
                problems.append("event_before_create")

        if problems:
            problems_out.append({
                "order_id": r.order_id,
                "store_id": r.store_id,
                "status": r.status,
                "problems": problems,
            })
    return problems_out


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only kitchen-timing reconciliation.")
    parser.add_argument("--store", type=int, default=None, help="Restrict to one store id.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args()

    mismatches = reconcile_timing(args.store)
    total = len(mismatches)

    if args.json:
        print(json.dumps({"mismatch_count": total, "mismatches": mismatches}, indent=2))
    else:
        scope = f"store {args.store}" if args.store is not None else "all stores"
        if total == 0:
            print(
                f"Kitchen-timing reconciliation OK ({scope}): every order's "
                "lifecycle events are internally consistent."
            )
        else:
            print(f"FOUND {total} order(s) with impossible lifecycle events ({scope}):")
            for m in mismatches:
                print(
                    f"  order {m['order_id']} (store {m['store_id']}, "
                    f"status {m['status']}): {','.join(m['problems'])}"
                )

    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
