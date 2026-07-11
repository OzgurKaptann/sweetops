# Inventory Lifecycle

Branch: `refactor/inventory-lifecycle`
Migration: `c3b7e01f9a24` (revises `b8c4d1e6f207`, the payment settlement ledger)

---

## 1. The original problem

SweetOps treated **placing an order** and **using up ingredients** as the same
event. `order_service.create_order()` validated stock and then immediately did:

```python
stock.stock_quantity -= needed        # physical stock, gone
IngredientStockMovement(movement_type="ORDER_DEDUCTION", quantity_delta=-needed)
```

and cancellation did the mirror image, adding the quantity straight back.

There was exactly **one** quantity column, `ingredient_stock.stock_quantity`, and
it silently meant two incompatible things at once: *what is physically in the
shop* and *what is still sellable*. That produces a set of concrete, everyday
failures in a waffle shop:

* **Cancelling a cooked order invented ingredients.** An order that reached
  `IN_PREP` — batter poured, banana sliced, iron closed — could be cancelled, and
  the old `_return_stock_for_order()` would happily add every gram back to
  stock. The system then believed in 150 g of batter that was physically in the
  bin. Every downstream number (reorder point, cost, waste, margin) inherited
  that lie.
* **Physical stock dropped for waffles nobody had started.** Ten open orders on
  the board meant ten waffles' worth of ingredients had already been deducted,
  even though every one of them was still sitting in the queue. A physical count
  never matched the system.
* **There was no way to say "we threw this away".** Waste, a supplier delivery,
  and a stock-count correction all had to be squeezed into the same
  `quantity_delta` column with a sign and no required reason or actor. A `-500`
  was indistinguishable from theft, spillage, or a typo.
* **The ledger was not a ledger.** `ingredient_stock_movements` had no
  constraints, no actor, no reason, and nothing stopped an `UPDATE` or `DELETE`
  rewriting history.

The fix is not to rename the movement types. It is to stop conflating a
**promise** with a **fact**.

> **An order is a promise. Cooking is a fact.**

---

## 2. Preparation ≠ payment ≠ inventory

Three independent dimensions, which must never be overloaded onto one another:

| Dimension | States | Owned by |
|---|---|---|
| Preparation | `NEW` → `IN_PREP` → `READY` → `DELIVERED`, or `CANCELLED` | `kitchen_service` |
| Payment | `UNPAID` / `PARTIALLY_PAID` / `PAID` (+ refund status) | `payment_service` |
| Inventory | reserved / consumed / released / waste / returned / adjusted / received | `inventory_service` |

The rules:

* **Payment changes never move stock.** Collecting cash does not cook a waffle.
* **Stock changes never move payment.** Writing off spoiled cream does not
  refund anybody.
* **Preparation changes move stock only through one explicit, documented rule** —
  entering `IN_PREP` consumes; `CANCELLED` releases whatever is still merely
  promised. Nothing is implicit.

---

## 3. The inventory state model

```
                  order created
                        │
                        ▼
                 ┌─────────────┐
                 │  RESERVED   │  reserved↑   on-hand unchanged
                 └──────┬──────┘
          kitchen       │        cancel before
          starts        │        the kitchen starts
            ┌───────────┴───────────┐
            ▼                       ▼
     ┌─────────────┐         ┌──────────────┐
     │  CONSUMED   │         │   RELEASED   │
     │ reserved↓   │         │  reserved↓   │
     │ on-hand ↓   │         │  on-hand  =  │
     └─────────────┘         └──────────────┘
            │
            │  cancel AFTER cooking → nothing is restored.
            ▼  The batter was really poured.
      (stays consumed)
```

Independent of any order, physical stock also moves through:

| Movement | Meaning | on-hand | reserved |
|---|---|---|---|
| `PURCHASE_RECEIPT` | goods arrived from a supplier | ↑ | – |
| `MANUAL_ADJUSTMENT` | correction to match a real physical count (signed) | ↑ or ↓ | – |
| `WASTE` | burnt, dropped, spoiled — a **cost**, kept visible | ↓ | – |
| `RETURNED` | usable stock deliberately put back | ↑ | – |

Waste is deliberately **not** folded into consumption. If burnt batter were
recorded as CONSUMPTION, the owner could never see what the shop is throwing
away — which is exactly the number that decides whether the business is viable.

---

## 4. Stock summary model

`ingredient_stock`, one row per ingredient:

```
on_hand_quantity     NUMERIC(12,3)  physical stock in the shop, right now
reserved_quantity    NUMERIC(12,3)  promised to accepted, not-yet-cooked orders
available_quantity   NUMERIC(12,3)  GENERATED ALWAYS AS (on_hand - reserved) STORED
```

`available_quantity` is generated **by PostgreSQL**, so the identity
`available = on_hand − reserved` can never drift from its inputs — the
application cannot even write it.

Everything is `NUMERIC`, never binary floating point: 0.1 g of vanilla paste
added a thousand times must be exactly 100 g.

Database constraints:

| Constraint | Why |
|---|---|
| `ck_stock_on_hand_nonneg` | you cannot physically hold negative batter |
| `ck_stock_reserved_nonneg` | ditto for promises |
| `ck_stock_reserved_le_on_hand` | **backorders are disabled** — the shop may not promise batter it does not physically have |

### Naming note

`stock_quantity` was **renamed** to `on_hand_quantity` rather than kept as an
alias. The old name is the bug: any code still reading it is code that has not
been thought through, and a rename forces every read site to be re-examined.

`OrderItemIngredient.consumed_quantity` **keeps** its name. It is the persisted
per-line *requirement* snapshot produced by the canonical quantity formula, and
it is a stable contract used by pricing and kitchen display. The authoritative
lifecycle state lives in `order_inventory_lines`; this column is just the input.

---

## 5. Order reservation flow

`POST /public/orders/` (customer, QR-authenticated):

1. Resolve trusted store/table from the QR token.
2. Compute the requirement for every ingredient with the **canonical formula**:

   ```
   required = standard_quantity × selected_ingredient_quantity × order_item_quantity
   ```

   This is unchanged from `fix/order-quantity-accounting` and still the only
   place the arithmetic lives.
3. Lock every `ingredient_stock` row `FOR UPDATE`, **in ascending `ingredient_id`
   order**.
4. Validate against **`available`**, never `on_hand`. Stock already promised to
   the order two tables over is not stock this order may claim.
   Shortfall → `422 {"error": "out_of_stock", "items": [...]}`, nothing written.
5. Create the order, items, and one `order_inventory_lines` row per
   `(order_item, ingredient)` carrying `reserved_quantity`.
6. `reserved_quantity += required`. **`on_hand_quantity` is not touched.**
7. Append one `RESERVATION_CREATED` movement per line.
8. Audit `INVENTORY_RESERVED` (actor: `CUSTOMER` — there is no staff member).

All in one transaction.

---

## 6. Kitchen consumption flow

`PATCH /kitchen/orders/{id}/status` → `IN_PREP` is the single bridge from
preparation state to physical stock.

1. Lock the order row `FOR UPDATE` (this serialises a concurrent cancel).
2. Lock the order's inventory lines, then the stock rows (ascending
   `ingredient_id`).
3. For each line compute:

   ```
   outstanding = reserved − consumed − released
   ```

4. `consumed += outstanding`; `on_hand −= outstanding`; `reserved −= outstanding`.
5. Append a `CONSUMPTION` movement, attributed to the authenticated staff user.
6. Audit `INVENTORY_CONSUMED`.

The status change and the stock movement commit in **one transaction** — an
order can never be marked `IN_PREP` without its consumption, nor consumed
without being marked.

### Why it can only happen once

Consumption settles `outstanding`, which is **zero on any replay**. So:

* a repeated mutation consumes nothing further;
* `READY` and `DELIVERED` consume nothing;
* an undo (`IN_PREP → NEW`) does **not** un-consume — the batter really was
  poured — and a subsequent restart finds `outstanding = 0` and consumes nothing
  a second time.

And beneath the service, the database enforces
`consumed + released <= reserved` (`ck_oil_settled_le_reserved`), so
double-consumption is not merely unreachable through the happy path — it is
**unrepresentable**.

---

## 7. Cancellation before preparation

Release, don't "return":

* `reserved −= outstanding`, `released += outstanding`
* `on_hand` is **unchanged** — releasing a promise puts nothing back on a shelf,
  because nothing ever left it
* one `RESERVATION_RELEASED` movement, `delta_on_hand = 0`
* audit `INVENTORY_RESERVATION_RELEASED`

---

## 8. Cancellation after consumption

**Stock is not restored.** This is the central product decision of this branch.

The ingredients are physically gone — poured, cooked, thrown away. A cancellation
is an accounting event; it cannot un-pour batter. Automatically crediting the
stock back would make the system's numbers diverge from the shop's shelves,
which is precisely the bug this branch exists to fix.

If some of it really is salvageable, that is a **deliberate human decision**, and
it must be recorded as one: an explicit `RETURNED` or `MANUAL_ADJUSTMENT`
movement with an authenticated actor and a reason. Never an implicit side effect
of pressing Cancel.

---

## 9. Paid / refunded order interaction

The payment-settlement rule is preserved exactly: an order with **net paid > 0**
(paid − refunded) cannot be cancelled until the money is refunded.

Crucially, that guard runs **before any inventory mutation**. Otherwise a
blocked cancellation would still have released the reservation on its way to
returning `409` — money safe, stock corrupted.

| Payment state | Consumed? | Cancellation |
|---|---|---|
| Unpaid | no | allowed → releases reservation |
| Unpaid | yes | allowed → **no stock restored** |
| Fully refunded (net 0) | no | allowed → releases reservation |
| Fully refunded (net 0) | yes | allowed → **no stock restored** |
| Paid / partially paid (net > 0) | either | **blocked, 409, before any stock mutation** |

---

## 10–12. Manual operations: adjustment, receipt, waste

| Endpoint | Movement | Effect |
|---|---|---|
| `POST /inventory/purchase-receipts` | `PURCHASE_RECEIPT` | on-hand ↑ |
| `POST /inventory/manual-adjustments` | `MANUAL_ADJUSTMENT` | on-hand ↑ or ↓ (signed `delta`) |
| `POST /inventory/waste` | `WASTE` | on-hand ↓, stays visible as waste |

Every one of them requires an **authenticated actor**, and adjustment and waste
additionally require a **reason** — enforced by the database, not just the
service. An unexplained stock correction is indistinguishable from theft.

A negative adjustment or waste that would push `on_hand` below `reserved` is
refused with `409 insufficient_on_hand`: the shop cannot write off batter that a
waiting customer's accepted order is already counting on.

### Permissions

| Role | `inventory:read` | `inventory:adjust` |
|---|---|---|
| OWNER | ✅ | ✅ |
| MANAGER | ✅ | ✅ |
| KITCHEN | ✅ | ❌ |
| CASHIER | ❌ | ❌ |

A cook may see what is left, so they can flag a shortage — but a cook correcting
the count is exactly the unaccountable adjustment this lifecycle exists to
prevent. Kitchen waste reporting is deliberately **deferred**, not quietly
enabled. A cashier handles money, not stock; the two authorities stay separate.

---

## 13. The movement ledger

`ingredient_stock_movements` is the **append-only source of truth**.

```
id, ingredient_id, movement_type
quantity                  always the POSITIVE magnitude
quantity_delta_on_hand    ) direction lives here…
quantity_delta_reserved   ) …and the movement type constrains both
unit
order_id, order_item_id, order_inventory_line_id   (lineage; null for manual)
reason, actor_user_id
idempotency_key_hash, request_hash                 (hashes only, never raw)
legacy_backfill
created_at
```

There are **no ambiguous bare signs**. A `-500` means nothing on its own;
`CONSUMPTION` with `delta_on_hand = −500, delta_reserved = −500` is unambiguous.
`ck_movement_delta_matches_type` enforces the mapping:

| Type | `delta_on_hand` | `delta_reserved` |
|---|---|---|
| `RESERVATION_CREATED` | 0 | +quantity |
| `RESERVATION_RELEASED` | 0 | −quantity |
| `CONSUMPTION` | −quantity | −quantity |
| `WASTE` | −quantity | 0 |
| `RETURNED` | +quantity | 0 |
| `PURCHASE_RECEIPT` | +quantity | 0 |
| `MANUAL_ADJUSTMENT` | ±quantity | 0 |

A row cannot claim to be a `CONSUMPTION` while *adding* to physical stock.

**Append-only** is enforced by `trg_ingredient_stock_movements_immutable`, which
refuses `UPDATE` and `DELETE` outright. Same hardening as the payment ledger:
`SECURITY INVOKER`, pinned `search_path`, schema-qualified references, no dynamic
SQL, `EXECUTE` revoked from `PUBLIC`, and **no runtime bypass** — no GUC or
session variable can switch it off, because any role (including through an
injection path) could set one. History is corrected with a new compensating
movement, never an edit.

---

## 14. Idempotency

**Customer orders** — unchanged: `orders.idempotency_key` is unique. A replay
returns the existing order and reserves nothing further. A *concurrent* retry
that races past the pre-check is caught by the unique constraint and resolved to
the winner's order, so even a retry storm reserves exactly once.

**Manual inventory commands** — every mutation requires an `Idempotency-Key`:

* same key + same payload → replays the original movement (`idempotent_replay:
  true`), no second ledger row;
* same key + **different** payload → `409 idempotency_mismatch`. Replaying it
  under the original's result would silently discard the new intent;
* only `SHA-256(key)` and `SHA-256(canonical payload)` are stored — never the raw
  key, never the raw body;
* a partial unique index on `idempotency_key_hash` makes the guarantee
  concurrency-safe, not just check-then-act.

---

## 15. Concurrency and locking

* Stock rows are locked `SELECT … FOR UPDATE` **in ascending `ingredient_id`
  order, always**. Two orders that both need chocolate (id 3) and banana (id 7)
  take id 3 first, so one waits for the other instead of deadlocking
  head-to-head.
* `populate_existing=True` on every locking read, so a caller can never validate
  availability against a stale identity-map copy read before a competing
  transaction committed.
* The order row is locked for the whole kitchen transition, which serialises a
  start-prep against a concurrent cancel of the same order.
* Nothing depends on the frontend disabling a button.

Proven by real threaded tests against real PostgreSQL — see `§23`.

---

## 16. Reconciliation

`scripts/reconcile_inventory.py` — **read-only**.

It cross-checks the three independent records of what stock should be:

```
   SUMMARY        ingredient_stock.on_hand_quantity / reserved_quantity
       vs
   LEDGER         SUM(ingredient_stock_movements.quantity_delta_on_hand)
       vs
   ORDER LINES    SUM(reserved − consumed − released) over order_inventory_lines
```

Reports per ingredient: stored on-hand, computed on-hand from the ledger, stored
reserved, computed reserved from the order lines, and the mismatch amount.
Supports `--json`, `--ingredient N`, `--all`. Exits **non-zero** on any mismatch.

It **never writes**. A reconciler that "repairs" drift by overwriting the summary
destroys the evidence needed to find the bug that caused it.

```bash
python scripts/reconcile_inventory.py            # all ingredients
python scripts/reconcile_inventory.py --json     # machine-readable, exit 1 on drift
```

---

## 17. Analytics definitions

| Term | Definition |
|---|---|
| `on_hand_quantity` | physical stock in the shop right now |
| `reserved_quantity` | promised to accepted, not-yet-cooked orders |
| `available_quantity` | `on_hand − reserved` — what can still be **sold** |
| `consumed_quantity` | physically used by the kitchen (`CONSUMPTION` movements) |
| `waste_quantity` | physically thrown away (`WASTE` movements) |
| `manual_adjustment_quantity` | count corrections (`MANUAL_ADJUSTMENT`) |
| `purchase_receipt_quantity` | goods received (`PURCHASE_RECEIPT`) |
| `stockout_risk` | computed from **`available`**, with velocity from **`CONSUMPTION`** |

Two corrections were needed, and both are now enforced by tests:

* **Stockout risk runs on `available`, not `on_hand`.** 100 g of pistachio on the
  shelf, all of it promised to open orders, is a shop that can sell no pistachio.
  That is a stockout, and judging on on-hand alone would hide it. The owner
  endpoint reports all three quantities, and distinguishes *"Stok tükendi!"* from
  *"Kalan stok bekleyen siparişler için ayrıldı"* — those need opposite responses.
* **Velocity is burn rate, from `CONSUMPTION` movements only.** Counting
  reservations would inflate the rate with waffles that were never cooked (and
  may yet be cancelled), and cry stockout too early.

The customer menu's `in_stock` / `low_stock` / `out_of_stock` status is likewise
driven by `available`, so the menu never offers an ingredient that is already
promised away and would be rejected at checkout.

**Deferred:** owner-web charts still render the existing fields (`stock_quantity`
is kept as an alias of on-hand for contract compatibility). Backend definitions
are correct now; the UI surfacing of reserved-vs-available is a follow-up.

---

## 18. Audit behaviour

Append-only audit events, in the same transaction as the mutation:

`INVENTORY_RESERVED`, `INVENTORY_RESERVATION_RELEASED`, `INVENTORY_CONSUMED`,
`INVENTORY_WASTE_RECORDED`, `INVENTORY_ADJUSTED`, `INVENTORY_RECEIVED`.

Staff-triggered actions (kitchen transitions, all manual commands) carry the
authenticated `actor_id`. Customer order creation has no staff actor and is
recorded as `CUSTOMER`.

**Never logged:** session tokens, CSRF tokens, raw idempotency keys, raw QR
tokens. (The order-creation audit payload previously included the raw
idempotency key; it no longer does — an audit trail is not a place to keep a
replay credential.)

---

## 19. Migration and backfill assumptions

The old code deducted physical stock **at order creation** and restored it on
cancellation. The backfill reproduces exactly the world that left behind — it
does not invent a tidier history.

For every `order_item_ingredient` with a persisted `consumed_quantity`:

| Existing order status | Backfilled line |
|---|---|
| `CANCELLED` | `reserved = consumed = returned = q` — the old code deducted `q` and added it back; net zero, nothing outstanding |
| `NEW`, `IN_PREP`, `READY`, `DELIVERED` | `reserved = consumed = q`, `released = 0` — the stock was **already** physically deducted, whatever the prep status |

Historical `NEW` orders are recorded as **consumed**, not merely reserved. The
tidier story would be a lie about the database we inherited: that stock is
already gone. Recording it as reserved would double-count (reserved rising
against an on-hand figure already reduced) and then consume it a *second* time
when the kitchen started it. Consequence, stated plainly: **historical `NEW`
orders carry no outstanding reservation** — starting one consumes nothing
further, and cancelling one releases nothing. Orders placed after the migration
get the full lifecycle.

`reserved_quantity` therefore backfills to `0` for every ingredient, and
`on_hand_quantity` keeps the exact value `stock_quantity` held.

**Opening balance.** The old ledger only ever recorded deltas, never the stock
the shop started with, so a naive sum would not equal the stored on-hand and
*every* pre-existing ingredient would reconcile as "drifted" forever. The
migration therefore reconstructs one opening movement per ingredient:

```
opening = on_hand_quantity − SUM(existing ledger deltas)
```

positive → `PURCHASE_RECEIPT`, negative → `MANUAL_ADJUSTMENT`, zero → no row.
Reconciliation is meaningful from the first run. (Verified: 0 drifted
ingredients immediately after migrating.)

**Movement type mapping:** `ORDER_DEDUCTION → CONSUMPTION`,
`CANCELLATION_RETURN → RETURNED`, `RESTOCK → PURCHASE_RECEIPT`,
`MANUAL_ADJUST → MANUAL_ADJUSTMENT`, `WASTE → WASTE`.
Zero-delta rows carry no information and are dropped.

**`legacy_backfill`.** Every backfilled row is flagged. The actor, reason and
delta-consistency constraints exempt those rows **and only those rows**: the old
ledger never captured an actor or a reason, and inventing one after the fact
would be fabricating an audit trail. Everything written from now on carries the
full constraints.

**Downgrade** restores `stock_quantity` and `quantity_delta`, drops the lifecycle
schema, and leaves **every order and payment row intact** — asserted by a test
that collects real money, downgrades, and checks the order and its settlement
survived. Alembic remains single-head.

---

## 20. Single-store inventory limitation — read this

**Inventory is GLOBAL. It is not store-scoped, and this branch does not claim
otherwise.**

`ingredients`, `ingredient_stock` and `ingredient_stock_movements` have **no
`store_id`**. The schema physically cannot tell one shop's pistachio from
another's. This branch deliberately did **not** add store scoping — that is a
schema change with its own migration, backfill and isolation-testing burden, and
mixing it into a lifecycle refactor would make both harder to review.

The existing **fail-closed** guard is preserved and extended to every new
endpoint: when more than one operational store exists,
`assert_single_operational_store()` returns a Turkish `409` rather than showing —
or worse, letting a manager *write off* — another store's stock.

So: correct under the current single-operational-store assumption; safely refused
otherwise. Nothing here provides multi-store inventory isolation.

## 21. Future: store-scoped inventory

Deferred to `refactor/store-scoped-inventory`: add `store_id` to the ingredient,
stock and movement tables, backfill to the single operational store, scope every
query and lock by store, remove the fail-closed guard, and add cross-store
isolation tests.

## 22. Deferred inventory UI

No inventory management UI is built here. Manual operations are protected backend
endpoints, exercised by tests. Owner-web charts keep their existing data contract
(`stock_quantity` remains as an alias of on-hand). A UI that surfaces
reserved-vs-available, and any kitchen waste-reporting screen, are follow-ups.

---

## 23. Tests and verification

New suites (`apps/api/tests/`):

| File | Covers |
|---|---|
| `test_inventory_model.py` | DB-level integrity, bypassing the service: Decimal not float, non-negative and reserved ≤ on-hand, generated `available`, positive movement quantity, movement-type domain, delta/type agreement, actor+reason required, **append-only ledger**, `consumed + released ≤ reserved` |
| `test_inventory_lifecycle.py` | reserve-not-consume, availability gating, consume-exactly-once, undo/restart, READY/DELIVERED no-ops, invalid transition mutates nothing, atomicity, cancel before/after prep, paid-cancel blocked **before** stock mutation, refunded cases, payment never moves stock |
| `test_inventory_manual.py` | receipt / adjustment / waste, reservation-protecting write-off refusal, reason required, idempotency (replay, 409 on mismatch, raw key never stored), RBAC (cashier none, kitchen read-only), CSRF + Origin |
| `test_inventory_concurrency.py` | concurrent over-reservation impossible, concurrent idempotent retry reserves once, concurrent start-prep consumes once, cancel/start-prep race settles once, concurrent adjustments preserve the summary, deterministic lock order under deadlock pressure |
| `test_inventory_reconciliation.py` | clean books reconcile, summary drift detected, reserved-vs-order-lines drift detected, deleted movement detected, CLI exit codes, **reconciler never mutates** |
| `test_inventory_analytics.py` | `available = on_hand − reserved`, stockout risk fires on fully-reserved stock, reservations are not consumption, slow-moving ignores reservations, waste stays distinct, owner endpoint reports all three quantities |
| `test_inventory_migration.py` | single head, all columns/constraints/trigger present, **downgrade preserves orders and collected money**, re-upgrade reinstalls the guard, backfill leaves the ledger reconciled |

Existing suites were updated where they encoded the *old* behaviour — most
notably `test_state_machine.py`, whose `test_in_prep_cancellation_returns_stock`
asserted the exact bug this branch fixes and is now
`test_cancel_after_prep_does_not_restore_stock`.

---

## 24. Deployment workflow

1. **Back up PostgreSQL.** `pg_dump` before anything else.
2. `alembic upgrade head` (single head: `c3b7e01f9a24`).
3. Verify stock summary columns: `on_hand_quantity`, `reserved_quantity`,
   `available_quantity` (generated).
4. Verify `order_inventory_lines` exists and is populated for open orders.
5. Verify the movement ledger reshaped and the append-only trigger is installed.
6. **Run `python scripts/reconcile_inventory.py`** — must exit 0.
7. Smoke: place a customer order → confirm it RESERVES (on-hand unchanged).
8. Smoke: start preparation → confirm it CONSUMES exactly once.
9. Smoke: cancel before prep → confirm the reservation is released, on-hand unchanged.
10. Smoke: cancel after prep → confirm stock is **not** restored.
11. Verify the payment workflow is unchanged (collect, refund, paid-cancel blocked).
12. Verify owner stock analytics render.
13. Verify public customer QR ordering still works.
14. **Train staff**: an order on the board has *reserved* ingredients; only
    starting it *uses* them. Cancelling a started order does not give stock back.
15. **Document the manual adjustment policy**: who may adjust, what counts as a
    valid reason, and how often a physical count is taken.

> **A physical rollout is not complete until a real inventory count has been
> taken and reconciled against `on_hand_quantity`.** The migration preserves the
> numbers the old system believed; it cannot verify them against the shelves.
> Only a human with a scale can do that.
