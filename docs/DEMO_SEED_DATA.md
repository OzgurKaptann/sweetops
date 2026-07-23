# Demo Seed Data

`scripts/seed_demo_data.py` populates SweetOps with a **deterministic, idempotent,
demo-scoped** dataset so that every operational surface is meaningful the moment a
reviewer opens it. On a fresh database the product is a set of empty screens; after
one command it tells a coherent small Turkish waffle-shop story.

> **Development / demo only.** This seed creates local demo accounts with a shared,
> published password. Never run it against a production database.

---

## 1. Purpose

SweetOps now spans many operational surfaces — customer QR ordering, the kitchen
board and its prep-timing cards, cashier payments/refunds/shifts, order issues, the
store-scoped inventory lifecycle with threshold alerts, transfers and physical
counts, and the owner operational dashboard. Without realistic data a reviewer sees
nothing and cannot understand the product. This seed makes all of it legible in one
command.

## 2. Command

Run after migrations, from the repository root:

```bash
python scripts/seed_demo_data.py
# or, equivalently:
npm run seed:demo
```

Migrate first if needed:

```bash
cd apps/api && python -m alembic upgrade head
```

> ⚠️ **Run the backend test suite _before_ seeding.** The tests and local
> development share one database. With demo data resident, roughly two dozen
> tests fail for reasons that are not regressions — the migration downgrade
> tests correctly refuse to downgrade store-scoped inventory while a second
> store holds stock. Correct order: **migrate → test → seed → demo**. See
> [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) §9 and
> [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md) §10.

The script prints a create/reuse summary, the demo stores, and the local login
credentials.

## 3. Safety & idempotency

The seed is built to be run repeatedly and to be trusted around real data:

- **Deterministic.** No random values. The only "now" it uses is deliberate:
  kitchen-timing timestamps and today's dashboard metrics are relative to the
  moment you seed, exactly as a live shop's would be. Re-running does not create
  new randomised history.
- **Idempotent.** Safe to run any number of times. Catalog / store / table / user
  / stock rows are upserted by natural key. Orders carry deterministic idempotency
  keys. **Every** money, stock, shift and issue mutation is driven through the same
  idempotent services the API uses, keyed by stable idempotency strings — so a
  rerun *replays* rather than duplicates. Two consecutive runs leave identical row
  counts (asserted by the tests).
- **Demo-scoped.** Everything lives inside the two demo stores (below). Store 1 and
  any other non-demo store are never read for writing, never mutated, never
  deleted.
- **Non-destructive.** The script only creates or upserts. It deletes nothing,
  wipes no table, drops no volume, and recreates no container. There is no
  "replace old demo data" path.
- **Ledger-honest.** All stock is built through the inventory service (every
  on-hand change has a matching ledger movement) and all money through the payment
  ledger, so `reconcile_payments`, `reconcile_inventory`, `reconcile_order_issues`
  and `reconcile_kitchen_timing` all stay green after seeding.
- **Fails safely.** If the schema is not migrated, the script aborts before
  writing anything with an actionable message pointing at `alembic upgrade head`.

## 4. Demo stores

| Store | Role |
|-------|------|
| **SweetOps Demo - Kadıköy** | Primary store — where all orders, payments, issues, shifts and threshold states live. |
| **SweetOps Demo - Moda** | Secondary store — exists only as the destination of a stock transfer, to demonstrate the multi-store transfer flow. |

Tables (Kadıköy): **Masa 1**, **Masa 2**, **Masa 3**, **Masa 4**, **Paket Servis**.

## 5. Demo users (local only)

All demo accounts share the password **`demo1234`** (printed at the end of every
run). These are **local demo credentials only** — never real accounts, never
secrets.

| Username | Role |
|----------|------|
| `owner.demo@sweetops.local` | OWNER |
| `manager.demo@sweetops.local` | MANAGER |
| `kitchen.demo@sweetops.local` | KITCHEN |
| `cashier.demo@sweetops.local` | CASHIER (collects today's payments; holds the open shift) |
| `cashier2.demo@sweetops.local` | CASHIER (runs the two closed shifts) |
| `manager.moda.demo@sweetops.local` | MANAGER (Moda store) |

## 6. Demo scenarios

### Orders (Kadıköy, today)
A full spread of lifecycle states with deterministic timings relative to seed time:
one waiting order under the warning threshold and one over it; one in-prep order
under warning and one over the critical threshold; a ready order; delivered orders
(paid cash / paid card / partially paid / unpaid); a cancelled order; and the
orders that back the refund and issue scenarios below.

### Kitchen timing
Because the active orders' timings are relative to seed time, the kitchen board
shows live cards in each state: queue-warning, queue/prep-critical (delayed), plus
completed orders that yield real prep-duration and time-to-ready averages for the
owner dashboard.

### Payments & refunds
Full cash and full card collections, a partial payment, and refunds — a **direct
partial refund** taken at the till and refunds created by **resolving order
issues** (full and partial). Revenue on the dashboard is the collected/refunded
ledger, never the order total.

### Order issues
An **open** issue (missing item), and resolved issues covering **no-refund**,
**partial-refund**, and **full-refund** resolutions. The full-refund order is left
in-prep so its resolution can cancel it without producing a contradictory
delivered-and-cancelled history.

### Cashier shifts
One **open** shift, one **closed shift with zero discrepancy**, and one **closed
shift with a small (−5 TL) discrepancy**. Closed snapshots are computed from the
payment ledger by the shift service, so they reconcile by construction.

### Inventory threshold states
Five ingredients land in the five distinct states the threshold engine reports:

| Ingredient | State |
|------------|-------|
| Nutella | HEALTHY |
| Çilek | LOW |
| Muz | CRITICAL |
| Lotus Biscoff | OUT_OF_STOCK (received, then wasted) |
| Oreo | NOT_CONFIGURED |

### Stock operations
Every manual inventory command is exercised, each on its own ingredient so the
threshold states above stay clean: a **purchase receipt**, a **waste/fire** write-
off, a **manual adjustment**, a **stock transfer** (Kadıköy → Moda), and a
**physical stock count** (with a non-zero correction and a follow-up zero-delta
"counted and correct" count).

### Owner dashboard attention list
The seeded conditions light up every attention rule the dashboard has:
`OUT_OF_STOCK`, `CRITICAL_STOCK`, `DELAYED_KITCHEN`, `OPEN_ISSUES`,
`SHIFT_DISCREPANCY`, `OPEN_SHIFTS`, and `UNPAID_ORDERS`.

## 7. What becomes meaningful

After seeding, log in as the relevant demo user and these surfaces are populated:

- **customer-web** — QR/table ordering context for the demo tables.
- **kitchen-web** — an active board with live timing warnings and criticals.
- **cashier-web** — payable orders, refunds, order issues, and shift open/close.
- **owner-web** — the operational dashboard (orders, money, kitchen tempo, issues,
  shifts, inventory, attention list), inventory management with all threshold
  states, order-issue history, and shift history.

## 8. How to re-run

Just run the command again. The seed is idempotent: existing demo records are found
and reused, missing ones are created, and nothing is duplicated. Re-running is the
supported way to top up a demo database after other activity.

## 9. How non-demo data is protected

- The seed resolves its stores by the exact demo names and only ever writes inside
  them.
- It performs no deletes, truncations, or schema changes.
- Stock, money, shifts and issues are created through the same services and
  composite-FK / store-scoping guarantees the application enforces, so a demo write
  can only ever land in a demo store.
- The tests assert that a non-demo store's user and order counts are unchanged
  across a seed run.

## 10. Deferred demo gaps

- **`BELOW_RESERVED` inventory state** is intentionally not seeded: it requires
  on-hand below reserved, which a database CHECK constraint makes unrepresentable
  (it "should never appear"). The other five threshold states are all covered.
- Demo orders carry menu selections for display but **do not reserve stock** — the
  inventory story is told independently through the explicit stock operations, so
  order and inventory concerns stay decoupled and the reconcilers are unaffected by
  the order set.
- The seed does not attempt browser automation or screenshots; it populates data
  only.
