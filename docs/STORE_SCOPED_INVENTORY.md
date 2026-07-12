# Store-Scoped Inventory

**Branch:** `refactor/store-scoped-inventory`
**Migration:** `e2c9a4b16d38` (revises `c3b7e01f9a24`, the inventory lifecycle)

> A jar of chocolate in Kadıköy is not a jar of chocolate in Beşiktaş.
> Everything below follows from that one sentence.

This document explains **whose** stock a number belongs to.
[INVENTORY_LIFECYCLE.md](INVENTORY_LIFECYCLE.md) explains **what happens** to it
(reservation ≠ consumption, exactly-once settlement, the append-only ledger).
Store scoping added a dimension; it did not change the lifecycle.

---

## 1. The original limitation, and why it was dangerous

Before this branch, `ingredient_stock` had one row per ingredient — for the
entire company. `ingredient_stock_movements` and `order_inventory_lines` had no
store at all. The schema asserted, structurally, that **every branch shares one
jar of Nutella**.

For a single-shop business that is merely untrue-but-harmless. For a real
multi-branch waffle chain it is a data-integrity failure with six faces:

| Failure | What actually happens |
|---|---|
| Cross-store consumption | A Kadıköy order reserves and burns stock that is physically on a shelf in Beşiktaş. |
| Cross-store disclosure | The Beşiktaş manager opens `/inventory/stock` and sees — and can *write off* — Kadıköy's inventory. |
| Wrong stockout risk | Available quantity from one branch is divided by a burn rate from another. The result is a plausible number about nothing. |
| Mixed analytics | Waste, consumption velocity and slow-moving capital pool across branches, so no manager can act on their own shop. |
| Hidden reconciliation drift | Kadıköy 40 g short + Beşiktaş 40 g over = **zero**. The report prints "OK" while both branches are broken. |
| Untraceable receipts | A purchase receipt records a quantity and no branch. Nobody can say where the goods went. |

Note the shape of every one of these: **nothing raises an exception.** The
database stays "valid", the dashboards render, the numbers look reasonable. That
is precisely what makes global stock dangerous, and why the fixes below are
enforced by the database rather than by remembering to write `WHERE store_id = ?`.

The lifecycle branch knew this and mitigated it the only way it could: a
**fail-closed guard** that returned a Turkish `409` from every inventory endpoint
as soon as a second operational store existed. Safe, but it meant SweetOps could
not actually run two branches. This branch removes the need for it.

---

## 2. Catalog versus physical stock

The single most important distinction in the model:

| | Catalog — stays GLOBAL | Physical stock — is STORE-SCOPED |
|---|---|---|
| Tables | `ingredients`, `products`, recipe metadata (`standard_quantity`, `unit`, `price`) | `ingredient_stock`, `ingredient_stock_movements`, `order_inventory_lines` |
| Meaning | The *definition* of a thing the chain sells | Jars actually sitting on a shelf in one named branch |
| Why | Every branch sells the same Nutella. Duplicating the recipe per store would make a menu change a per-branch migration. | A jar cannot be in two places. |

The governing rule:

```
inventory data must never be global unless it is static recipe/catalog data
```

`ingredients` therefore keeps **no quantity column of any kind**. Physical
quantity lives only in `ingredient_stock`, and only ever for one store.

These dimensions stay separate and are never overloaded onto one another:
catalog ingredient · store stock · order reservation · physical consumption ·
payment state · preparation state.

---

## 3. The grain

```
ingredient_stock             one row per (store_id, ingredient_id)
ingredient_stock_movements   append-only ledger, every row carries store_id
order_inventory_lines        one row per (order_item, ingredient), carries store_id
```

**`ingredient_stock`** — the fast-query summary of one branch's slice of the
ledger.

```
id
store_id            FK stores.id                      NOT NULL
ingredient_id       FK ingredients.id                 NOT NULL
on_hand_quantity    physical stock in THIS store
reserved_quantity   promised to THIS store's accepted orders
available_quantity  GENERATED ALWAYS AS (on_hand - reserved) STORED
unit, reorder_level, last_restocked, updated_at

UNIQUE (store_id, ingredient_id)          ← the feature, in one line
CHECK  on_hand_quantity  >= 0
CHECK  reserved_quantity >= 0
CHECK  reserved_quantity <= on_hand_quantity     (no backorders)
```

Two rows for the same ingredient in one store would let two concurrent orders
lock *different rows* and each believe it had the last 200 g of pistachio. The
uniqueness constraint is what makes the row lock meaningful.

**`ingredient_stock_movements`** — every row states what happened, how it moved
on-hand and reserved, and **which branch it happened in**. Order-driven rows also
carry `order_id`, `order_item_id`, `order_inventory_line_id`; manual rows carry
`actor_user_id` and `reason`.

**`order_inventory_lines`** — the per-order lifecycle (`reserved`, `consumed`,
`released`, `waste`, `returned`), now stamped with the order's store.

---

## 4. Cross-store integrity is enforced by the database

"Remember to add `WHERE store_id = ?`" is not an invariant, it is a habit. One
forgotten filter in one analytics query and Store A's order is eating Store B's
chocolate — silently, with a plausible number on the dashboard, and no test
necessarily catching it.

So the invariants are **composite foreign keys**, which a query cannot forget.
Each makes a class of corruption *unrepresentable* rather than merely unwritten:

| # | Requirement | Enforced by |
|---|---|---|
| 1 | Stock cannot duplicate `(store_id, ingredient_id)` | `uq_stock_store_ingredient` |
| 2 | Movement store must match its stock row's store | `fk_movement_stock_store` → `ingredient_stock(store_id, ingredient_id)` |
| 3 | Order inventory line store must match its order's store | `fk_oil_order_store` → `orders(store_id, id)` |
| 4 | Order inventory line store must match its stock row's store | `fk_oil_stock_store` → `ingredient_stock(store_id, ingredient_id)` |
| 5 | A movement for an order must match that order's store | `fk_movement_order_store` → `orders(store_id, id)` |
| 6 | A movement for an inventory line must match that line's store | `fk_movement_line_store` → `order_inventory_lines(store_id, id)` |
| 7 | A manual movement's actor must belong to the movement's store | `fk_movement_actor_store` → `users(store_id, id)` |
| 8 | Reservation / consumption / release cannot touch another store's summary | Service locks `(store_id, ingredient_id)` only; #2 and #4 make a cross-store write impossible anyway |
| 9 | The ledger is append-only | `trg_ingredient_stock_movements_immutable` (UPDATE/DELETE refused; no runtime bypass) |
| 10 | Idempotency uniqueness is store-scoped | `uq_movement_store_idem` on `(store_id, idempotency_key_hash)` |

Supporting unique constraints exist purely as FK targets: `orders(store_id, id)`,
`users(store_id, id)`, `order_inventory_lines(id, store_id)`. They are redundant
against the primary keys, but PostgreSQL requires a unique constraint on exactly
the referenced pair.

All composite FKs are `MATCH SIMPLE`: when the nullable half is NULL (a manual
movement has no order; an order movement has no actor) the constraint does not
apply. When it is present, the referenced row **must** be in the movement's
store. A side effect worth stating: a user with `store_id IS NULL` can never be
the actor of a stock movement — which is the correct answer, not an accident.

**Indexes:** `ix_ingredient_stock_store_id`, `ix_ingredient_stock_ingredient_id`,
`ix_ingredient_stock_movements_store_id`, `ix_movement_store_ingredient_created`,
`ix_oil_store_order_ingredient`.

**Append-only:** the immutability trigger is dropped *only* for the duration of
the migration's backfill `UPDATE` (which it would otherwise refuse) and
reinstalled before the migration commits — long before any application traffic
can reach the table. A correction to stock history is a new compensating
movement, never an edit.

---

## 5. Migration `e2c9a4b16d38`

### Two classes of row

**DERIVABLE — exact, no assumption, correct with any number of stores:**

```
order_inventory_lines.store_id     ← its order's store_id
movements.store_id (order rows)    ← its order's store_id
```

An order already knows its store. Its inventory is that store's, necessarily.

**AMBIGUOUS — needs an assumption:**

```
ingredient_stock rows              — a global stock row names no store
movements with order_id IS NULL    — opening balances, manual adjustments,
                                     waste, purchase receipts
```

A global row saying *"4 kg of pistachio"* cannot be split across branches by any
rule the database knows. 4 kg in Kadıköy? 2 and 2? **Nobody can tell from the
data.**

### Backfill rule

* **Exactly one operational store** → every ambiguous row is assigned to it. This
  is not a guess; it is the only physical possibility.
* **More than one operational store, and ambiguous rows exist** → **the migration
  aborts** with `AmbiguousInventoryStore` and a clear operator message. Nothing is
  committed; the schema stays global.

*"Operational"* = a store with **at least one staff user or at least one order** —
evidence it is actually being run. A `Store` row created ahead of an opening (or
by a test fixture) has neither, and cannot be where four kilos of pistachio have
been sitting. If no store is operational but exactly one `Store` row exists, that
one is used (a freshly seeded shop that has not hired staff yet).

### Why fail closed rather than pick something

Guessing here would not throw an error. It would produce a database that looks
completely fine and is **quietly wrong about where the physical stock is** — the
worst outcome available for inventory. The three tempting shortcuts are all
wrong:

* *Assign everything to store 1* — a coin flip dressed up as a default.
* *Duplicate the stock into every store* — **fabricates inventory that exists on
  no shelf.**
* *Split it evenly* — invents a physical fact.

Splitting real stock across real branches is a **physical-count decision for the
owner**, not an inference for a migration. So the migration stops and says so.

To resolve it: perform a physical count per store, decide the per-store split,
pre-create the per-store rows (or reduce the installation to one operational
store), then re-run.

### Materialised zero rows

The composite FKs point **at** the summary row, so it must exist. If a movement
or line references an ingredient whose store has no summary row — a latent
inconsistency inherited from before the lifecycle migration, where a stock row
could be deleted while its ledger history remained — the migration materialises
it at **zero**. That neither creates nor destroys stock, and reconciliation will
now correctly *report* the pre-existing drift instead of hiding it behind a
missing row.

### New stores after the migration

A new branch starts with **no stock** and never inherits another branch's.
Inheriting would fabricate inventory that does not physically exist. Stock is
initialised explicitly, via **purchase receipt**, **manual adjustment**, or
**seed/demo data**. (Inventory *transfer* between stores is deliberately not
implemented — see § Deferred.)

### Downgrade

`downgrade()` removes only this branch's schema. Orders, payments, the movement
ledger and the entire inventory *lifecycle* (reservations, lines, the
append-only trigger) are left untouched.

It **refuses to run when more than one store holds stock.** Collapsing two
branches' shelves into one global row would have to merge, pick or discard real
per-store quantities — destroying the record of which branch owned what, with no
way to reconstruct it. A lossy downgrade is worse than no downgrade. Reduce the
data to a single store first.

Re-upgrade after a downgrade is a normal one-store backfill and succeeds.

Alembic remains **single-head**.

---

## 6. Order creation — the store comes from the QR token

Customer order context is derived server-side from the secure QR token; a
client-supplied `store_id` is never trusted. On order creation:

1. Resolve **store + table** from the QR token (row-locked, inside the txn).
2. `lock_stock_rows(db, store_id, ingredient_ids)` — locks **only that store's**
   rows, `FOR UPDATE`, in ascending `ingredient_id` order.
3. Validate **available** (`on_hand − reserved`) **in that store only**.
4. Create `order_inventory_lines` with that `store_id`.
5. Create `RESERVATION_CREATED` movements with that `store_id`.
6. Raise `reserved_quantity` on **that store's** summary rows only.

The store filter in the lock is not an optimisation — it *is* the isolation
boundary. It is why a Kadıköy order waits only on other Kadıköy orders, and why
it can never lock, read or spend a gram of Beşiktaş's chocolate.

**Consequences, all tested:**

* A Store A QR order can never reserve Store B inventory.
* The same ingredient has **independent availability per store**: Store A can be
  sold out of the very pistachio Store B has 5 kg of, and Store A's order is
  correctly rejected `422 out_of_stock` while Store B's succeeds.
* A store with **no stock row** for an ingredient is short of it. That is never
  satisfied from another store's shelf.
* Idempotent replay returns the original order and does **not** double-reserve.
* Quantity accounting (`standard_quantity × selected × item_quantity`) is
  unchanged.

---

## 7. Kitchen and cancellation

The kitchen staff's store comes from their authenticated session; the **stock**
store comes from `order.store_id`. Both must agree, and RBAC already guarantees
staff can only touch their own store's orders — a cross-store kitchen mutation is
a safe `403/404` before any stock moves.

**Start of preparation** (`consume_order`) settles
`outstanding = reserved − consumed − released` on that order's lines, under a row
lock, in that order's store:

* on-hand and reserved fall in **that store only**;
* `CONSUMPTION` movements carry that store;
* a replay finds `outstanding = 0` and is a **no-op** — the DB `CHECK
  (consumed + released <= reserved)` makes double-consumption structurally
  impossible, so repeated `IN_PREP → READY → DELIVERED` deducts exactly once.

**Cancellation** is unchanged in semantics, now store-scoped:

| Situation | Behaviour |
|---|---|
| Order has net paid money (`paid − refunded > 0`) | **Blocked** (`409`) *before any stock mutation* |
| Unconsumed | Releases **that store's** reservation only; on-hand untouched |
| Already consumed | **No stock restored** — the batter was really poured, and it certainly cannot be un-poured into another branch |
| Fully refunded, unconsumed | Cancels and releases that store's reservation |
| Fully refunded, consumed | Cancels, restores nothing |

The cancel/start-prep race stays safe: both drive the *same* `outstanding`
expression under the same row lock, so whichever commits first leaves nothing for
the other to settle.

WebSocket store partitioning is unchanged.

---

## 8. Manual inventory operations

```
GET  /inventory/stock              inventory:read
GET  /inventory/movements          inventory:read
POST /inventory/purchase-receipts  inventory:adjust
POST /inventory/manual-adjustments inventory:adjust
POST /inventory/waste              inventory:adjust
```

For every one of them:

```
store_id      = current_staff.store_id     ← from the session, always
actor_user_id = current_staff.user_id
```

**`store_id` is never read from the request body, query string, header, or any
frontend state.** There is no `store_id` field on any request schema and no
store parameter to tamper with — *the absence of the parameter is the security
property*. A body that smuggles in `"store_id": 2` is simply not read: the
receipt still lands in the caller's own store. (Tested.)

A staff account with **no store** is refused (`403 no_store_assigned`). There is
no chain-wide inventory view; inventory is physical, and physical stock sits in a
named branch.

An ingredient the branch has never stocked returns **`404 stock_not_configured`**
— deliberately distinct from "ingredient not found". The ingredient exists in the
shared catalog; this branch simply has no stock row for it. Another store's stock
is **not** used as a fallback; the branch must receive or count stock in
explicitly.

Mutations additionally require (unchanged): an authenticated session, a trusted
Origin, a valid CSRF token, the `inventory:adjust` permission, and an
`Idempotency-Key` header.

### Store-scoped idempotency

Uniqueness is `(store_id, idempotency_key_hash)`.

* Same store + same key + same payload → **replays** the original movement.
* Same store + same key + different payload → **`409 idempotency_mismatch`**.
* **Different store + same key → completely independent.**

That last line matters operationally. Two branch managers working from the same
printed run-book will legitimately send the same `Idempotency-Key` on the same
day. That is a *coincidence, not a replay* — and if it were treated as one,
Beşiktaş's 40 kg delivery would silently return Kadıköy's receipt and record no
stock at all.

Only `SHA-256(key)` and `SHA-256(canonical payload)` are ever stored. The raw key
is never persisted.

### Permissions (unchanged)

| Role | Inventory access |
|---|---|
| OWNER / MANAGER | read + adjust — **own store only** |
| KITCHEN | read — own store only |
| CASHIER | **none** |

No cross-store access. No super-admin inventory feature.

---

## 9. Analytics

Every inventory-derived signal is scoped to one store, on **both** sides of the
calculation. That pairing is the point: divide Kadıköy's available quantity by
Beşiktaş's burn rate and you get a plausible-looking number of hours that is
simply *about nothing*.

| Signal | Store-scoped how |
|---|---|
| Stockout risk | This store's `available_quantity` ÷ this store's `CONSUMPTION` velocity |
| Consumption velocity | This store's `CONSUMPTION` movements only |
| Slow-moving capital | This store's on-hand, against this store's movements |
| Waste metrics | This store's `WASTE` movements only |
| Owner insights / critical alerts | Demand from this store's orders, runway from this store's shelves (`LEFT JOIN … ON s.ingredient_id = i.id AND s.store_id = :store_id`) |
| Decision engine | All six evaluators now take `(db, store_id)` |
| Conversion engine (menu, upsell, validate) | `stock_status` from this store's rows |

Invariants:

* Reserved stock is **never** counted as consumed.
* **No global fallback.** If a store has no stock row for an ingredient, the
  answer for that store is "not stocked here" — never another store's figure.

An ingredient that Beşiktaş sells briskly can still be dead stock in Kadıköy, and
the Kadıköy manager is the one who has to run the promotion. Store scoping is
what lets the system say so.

### The old fail-closed guard

Removed from staff inventory, owner analytics, owner insights and the decision
engine. **Multi-store operation is no longer an error condition.** Concretely,
the decision engine used to *skip* its two inventory evaluators once a second
store existed (`signals_evaluated` silently dropping from 6 to 4); now all six
run for every store.

### Remaining limitation — stated honestly

Three endpoints still have **no store context at all**, because they carry
neither a QR token nor a session:

```
GET  /public/menu/            (ungated)
GET  /public/menu/upsell      (ungated)
POST /public/menu/validate    (when no qr_token is supplied)
```

They report `stock_status`, which is physical and therefore belongs to exactly
one branch. With two branches stocked, *"is pistachio in stock?"* has two
different true answers and nothing in the request says which was asked. So they
resolve the **single stocked store** and **fail closed** with a Turkish `409`
otherwise. Refusing beats guessing; picking "store 1" would quietly show one
branch's shelves to another branch's customers.

This is a limitation of *those endpoints*, not of the inventory model. The
QR-gated paths the customer app actually uses are fully store-scoped:

```
POST /public/menu/resolve     store derived from the scanned token
POST /public/menu/upsell      store derived from the scanned token
POST /public/menu/validate    store derived from the supplied qr_token
```

`customer-web` was moved onto `POST /public/menu/upsell` in this branch, so the
customer experience is store-correct in a multi-branch shop.

---

## 10. Reconciliation

```bash
python scripts/reconcile_inventory.py                 # every store, grouped by store
python scripts/reconcile_inventory.py --store-id 2    # one store
python scripts/reconcile_inventory.py --ingredient 3  # one ingredient, all stores
python scripts/reconcile_inventory.py --json --all
```

Cross-checks three independent records, **per `(store, ingredient)`**:

```
SUMMARY       ingredient_stock.on_hand_quantity / reserved_quantity
    vs
LEDGER        SUM(ingredient_stock_movements.quantity_delta_on_hand)
    vs
ORDER LINES   SUM(reserved − consumed − released) over order_inventory_lines
```

Every correlated subquery is keyed on **both** `store_id` and `ingredient_id`.

**Why the store is part of the grain.** Reconciling across stores would be worse
than not reconciling at all. Kadıköy 500 g short + Beşiktaş 500 g over — two
real, serious, *opposite* faults — sum to **zero**, and the report says everything
is fine. Mismatches must never cancel out across branches.

Guarantees:

* Every total is computed **per store**; nothing is ever summed across stores.
* Every mismatch **names its store** (id and name), so it is actionable by a
  specific branch manager.
* Exit **non-zero if any store mismatches** — never averaged away.
* A store-filtered run lets an operator isolate which branch is actually broken.
* It **never writes.** A reconciler that "repairs" drift by overwriting the
  summary destroys the only evidence of whatever wrote stock outside the
  inventory service.

---

## 11. Seed and demo data

**`apps/api/seed.py`** — creates one store and stocks **that store** explicitly.
No store ever inherits another's shelves.

**`scripts/demo_seed.py`** — now multi-store:

* `ensure_stock_rows_for_all_stores()` gives **every** store its own row for every
  active ingredient. Cloning quantities across stores is fine *here* precisely
  because it is synthetic. The production migration does the **opposite** — it
  refuses to duplicate real stock into a second store, because that would
  fabricate inventory that exists on no shelf. **Seed cloning is never production
  migration logic.**
* Demo orders are generated **per store**, and reserve/consume **only their own
  store**: the deduction map is keyed `(store_id, ingredient_id)`, and every line
  and ledger row is stamped with the order's store.
* The demo ledger is now **honest**: the opening balance is *derived* —
  `opening = consumed + ending target` — so the ledger sums exactly to the summary.
  (Previously the demo wrote on-hand directly with no ledger row, leaving every
  demo database permanently unreconciled and heavy-use ingredients clamped at zero
  while the ledger ran negative.)

**Reconciliation passes after seed and after demo seed**, for one store and for
two. QR/table/store and payment seeds are unchanged.

---

## 12. Tests

`apps/api/tests/test_store_scoped_inventory.py` (33) — constraints, order
creation, kitchen, cancellation, manual operations, idempotency, analytics.
`apps/api/tests/test_store_scoped_inventory_migration.py` (10) — schema, round
trip, backfill, fail-closed.
`apps/api/tests/test_store_scoped_reconciliation.py` (7) — per-store books.

Several deliberately bypass the service layer and assert that **PostgreSQL
itself** refuses the write — because the point is that the database rejects
cross-store rows even if the application is wrong.

Highlights:

* Same ingredient, two stores, two independent quantities.
* Store A sold out while Store B has plenty → A's order is rejected and **does not
  touch B's 500 g**.
* Direct-SQL cross-store movement / line / actor rows are **rejected by the DB**.
* Ledger `UPDATE`/`DELETE` still refused.
* Store A start-prep consumes A only; B unaffected in on-hand *and* reserved.
* Cross-store kitchen mutation blocked; nothing consumed anywhere.
* Client-supplied `store_id` in the body is **ignored**; the receipt lands in the
  session's store.
* The same idempotency key in two stores → **two independent receipts**, both
  applied.
* Store A's velocity excludes Store B's movements (A even reads as *slow-moving*
  while B burns through the same ingredient).
* Missing stock in one store never reads another store's stock.
* Migration **aborts** with two operational stores and global stock; schema stays
  global; nothing half-applied.
* Store A short 40 + Store B over 40 → **both reported**; the drifts sum to zero,
  which is exactly why they are never summed.

Full API suite: **644 passed, 0 failed.**

---

## 13. Deployment

1. **Back up PostgreSQL.**
2. **Confirm exactly one operational store** — or prepare a manual per-store stock
   split. The migration will abort otherwise; that is intended, not a bug.
3. `alembic upgrade head`.
4. Verify one stock row per `(store, ingredient)`; verify movements and order
   inventory lines all carry `store_id`.
5. `python scripts/reconcile_inventory.py` — expect exit 0 for every store.
6. For any **new** store, initialise stock explicitly (purchase receipt / manual
   adjustment). It inherits nothing.
7. Smoke-test: Store A QR order · Store B QR order · Store A kitchen start-prep ·
   Store B manual receipt · Store A owner dashboard · Store B owner dashboard.
8. Confirm **no cross-store stock leakage**.
9. **Perform a physical count per store.**

> Rollout is **not** complete without a physical count per store. Until a human
> has looked at the shelves, the store labels on historical stock are an
> assumption — a well-founded one, but an assumption.

---

## 14. Deferred — explicitly not in this branch

* **Inventory transfer between stores** — moving stock from Kadıköy to Beşiktaş
  needs its own movement types (`TRANSFER_OUT` / `TRANSFER_IN`), a two-sided
  atomic write and an approval flow. It is *the* obvious next feature, and it is
  deliberately absent: the whole point of this branch is that stock does not move
  between branches by accident, so making it move on purpose deserves its own
  design.
* **Supplier management**
* **Purchase-order management** (purchase *receipts* exist; purchase *orders* do not)
* **Recipe versioning**
* **Lot / expiry tracking** — batch-level granularity under the store level
* **Barcode scanning**
* **Full inventory UI** — manual operations remain protected backend endpoints
* **Advanced forecasting**
* **Turkish localisation of analytics** (staff-facing inventory errors are Turkish;
  the analytics layer is not)
