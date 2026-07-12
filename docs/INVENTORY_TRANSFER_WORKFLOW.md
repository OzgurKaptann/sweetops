# Inventory Transfer Workflow

Moving stock between branches, as a first-class business event.

Status: **backend complete**. No UI. See [Limitations](#12-limitations).

---

## 1. Why a transfer is not a manual adjustment

Before this branch, shipping 2 kg of chocolate from Kadıköy to Beşiktaş could only
be typed into SweetOps as two manual adjustments:

```text
Kadıköy   MANUAL_ADJUSTMENT  -2000 g   "sent to Beşiktaş"
Beşiktaş  MANUAL_ADJUSTMENT  +2000 g   "came from Kadıköy"
```

The quantities are right. Everything else about it is wrong, because **nothing in
the database says those two rows are the same chocolate.** The reason text is
prose; the database cannot read it.

What that costs, concretely:

| Failure | What actually happens |
|---|---|
| **Half the transfer** | One adjustment commits, the other fails (a crash, a lost connection, a manager who got distracted). 2 kg of chocolate ceases to exist. Both branches' ledgers remain perfectly self-consistent — each agrees with its own summary — so **nothing raises and no per-store reconciliation reports anything wrong.** The stock is just gone. |
| **Reconciliation is blind** | Kadıköy is 2 kg short; Beşiktaş is 2 kg over. To a reconciler these are two unrelated faults — one looks like theft, the other like a miscount — and it has no way to know they are one shipment. |
| **Waste is a lie** | The outbound half looks exactly like WASTE: same sign, same magnitude, same store. If it is *booked* as waste, the owner's report accuses a branch of binning chocolate it actually shipped. |
| **Purchasing is a lie** | The inbound half looks exactly like a PURCHASE_RECEIPT. Booked that way, the chain counts 2 kg it never bought — every internal shipment inflating spend that was only ever incurred once. |
| **Reordering is a lie** | If either half touches consumption, velocity rises: the reorder engine sees a branch racing through chocolate and orders more — for a branch that simply put it on a van. |
| **Nobody owns it** | A manual adjustment names an actor and a store. It does not name a counterparty. "Who authorised this, and where did it go?" is unanswerable. |

None of these raise an exception. Every one produces a **plausible-looking number**,
which is the worst failure mode available for stock.

A convention ("always write the same reason text") cannot fix any of it, because
nothing enforces a convention. A table and a set of foreign keys can.

> **A transfer is ONE business event with TWO linked ledger movements.**
> The event is a row in `inventory_transfers`. The two movements point back at it.
> The database refuses to let them disagree with the event, with each other, or to
> exist alone.

---

## 2. Transfer data model

### `inventory_transfers` — the business event

| Column | Notes |
|---|---|
| `id` | BIGSERIAL. The shared identity both legs carry. |
| `source_store_id` | **From the session, never the request body.** |
| `destination_store_id` | Client-supplied — only the manager knows where the van is going — and validated server-side. |
| `ingredient_id` | Catalog ingredient (global). |
| `quantity` | `NUMERIC(12,3)`, always positive. Decimal end-to-end, never float. |
| `unit` | Copied from the catalog ingredient. |
| `status` | `COMPLETED`. The only value. See [§ Status](#status-has-exactly-one-value). |
| `reason` | **Mandatory.** An unexplained shipment out of a branch is indistinguishable from stock walking out of the door. |
| `note` | Optional free text. |
| `initiated_by_user_id` | Bound to `source_store_id` by a composite FK. |
| `idempotency_key_hash` | SHA-256. **The raw key is never stored.** |
| `request_hash` | SHA-256 of the canonical request body. |
| `created_at`, `completed_at` | Both legs post atomically, so these are the same instant. |

### `ingredient_stock_movements` — the two legs

Three new columns:

| Column | Notes |
|---|---|
| `transfer_id` | FK → `inventory_transfers`. NULL for every non-transfer movement. |
| `transfer_out_store_id` | **GENERATED** `CASE WHEN movement_type = 'TRANSFER_OUT' THEN store_id END` |
| `transfer_in_store_id` | **GENERATED** `CASE WHEN movement_type = 'TRANSFER_IN' THEN store_id END` |

The generated columns are not decoration. They turn *"the OUT leg is booked in the
transfer's source store, the IN leg in its destination store"* from an application
rule into a **foreign key**. A single FK cannot conditionally target two different
columns of `inventory_transfers`, so the direction is projected into its own column
and each FK is left `MATCH SIMPLE` — on a `TRANSFER_OUT` row `transfer_in_store_id`
is NULL and the inbound FK does not apply, and vice versa. They are
`GENERATED ALWAYS ... STORED`, so **the application cannot forge them.**

### Status has exactly one value

`status = 'COMPLETED'`, enforced by a CHECK constraint. Both legs post inside one
database transaction, so a transfer is never half-done, never in transit, never
pending approval. A mutable status column with no state machine behind it would be
a lie that invites one to be written later. When an approval or in-transit flow is
genuinely built, the status domain widens *then*.

---

## 3. Source and destination store rules

```text
source_store_id = current_staff.store_id        ← always, no exceptions
```

There is **no `source_store_id` field in the request schema**, and the schema
`forbid`s unknown fields, so a body carrying one is rejected with a 422 rather than
silently ignored. Shipping another branch's stock is therefore not a permission
check that could be got wrong — it is a request that **cannot be expressed**.

And if the service layer were wrong anyway, the database still refuses:

```sql
FOREIGN KEY (source_store_id, initiated_by_user_id)
    REFERENCES users (store_id, id)      -- fk_transfer_actor_source_store
```

A Store A manager cannot be recorded as having shipped Store B's stock. Not
forbidden — **unrepresentable**. (`users.store_id` is nullable, so a member of staff
with no store assignment can never initiate a transfer, which is the correct answer.)

The destination is validated:

| Rule | Behaviour |
|---|---|
| Must exist | `404 destination_store_not_found` |
| Must differ from the source | `422 same_store_transfer`. Shipping to yourself is a no-op that would leave a cancelling pair of movements behind. Also a DB CHECK (`ck_transfer_stores_differ`). |
| Must be able to receive the ingredient | Its stock row is **created at zero** if it has never held it — see below. |

### Destination stock row policy: **auto-create at zero**

A transfer into a branch that does not yet stock the ingredient **creates the stock
row** rather than returning 404.

This does not contradict *"a new branch never inherits another branch's stock"*.
Nothing is inherited and nothing is fabricated: the row starts at **zero**, and the
only thing that puts stock in it is a `TRANSFER_IN` exactly matched by a
`TRANSFER_OUT` somewhere else. **Chain-wide totals are unchanged to the gram.**

The alternative — refusing until someone books a purchase receipt — would force a
manager stocking a newly opened branch from the warehouse branch to **invent a
supplier delivery that never happened**, which is precisely the lie about physical
stock this module exists to prevent.

The *source* has no such policy: a branch that has never stocked an ingredient has
nothing to ship, and gets `404 stock_not_configured`. It is never satisfied from a
third store's shelf.

---

## 4. The available-stock rule

```text
available = on_hand - reserved

REFUSE the transfer if:   source.available < quantity
```

**Reserved stock is not transferable.** The gate is `available`, never raw
`on_hand`.

A branch with 100 g on the shelf and 10 g already reserved for an accepted order has
90 g it may ship. The other 10 g is batter this shop **has already promised to a
customer sitting at a table**. Putting it on a van would silently break that promise
— the order would be accepted, then fail in the kitchen with no stock to cook.

Refused with `409 insufficient_available`. Reserved quantity is **never modified by
a transfer** on either side: stock moved; nobody's promise did.

---

## 5. The transfer movement pair

One successful transfer creates **exactly two** ledger rows:

| | source leg | destination leg |
|---|---|---|
| `store_id` | `source_store_id` | `destination_store_id` |
| `movement_type` | `TRANSFER_OUT` | `TRANSFER_IN` |
| `quantity_delta_on_hand` | `-quantity` | `+quantity` |
| `quantity_delta_reserved` | `0` | `0` |
| `transfer_id` | the transfer | the same transfer |
| `actor_user_id` | the initiator | **NULL** — see below |
| `reason` | the transfer's reason | the transfer's reason |

### Why the inbound leg carries no actor

`fk_movement_actor_store` (from the store-scoped refactor) says **staff only move
stock in their own store**. The person who authorises a transfer works in the
*source* store, but the inbound movement lands in the *destination* store. Naming
them as its actor would break that constraint — and weakening the constraint to
allow it would re-open exactly the cross-store hole it was added to close.

So the inbound leg has `actor_user_id IS NULL` (enforced by
`ck_movement_transfer_in_no_actor`), and accountability lives on the transfer row's
`initiated_by_user_id`, which is itself bound to the source store. **Nothing is
lost:** the transfer is one event, and one event has one initiator.

### Atomicity

Both legs, both summary updates, the transfer row and the audit record are **one
database transaction**. There is no window in which the source has lost stock the
destination has not gained. If the transaction fails at any point — including on
`COMMIT` — nothing moved on either side.

---

## 6. Idempotency

`POST /inventory/transfers` **requires** an `Idempotency-Key` header (`400
idempotency_required` without one).

| Case | Behaviour |
|---|---|
| Same source store + same key + **same** payload | Replays the original transfer. Returns the original `transfer_id` and both original movement ids, with `idempotent_replay: true`. **No new movement rows. No further stock moves.** |
| Same source store + same key + **different** payload | `409 idempotency_mismatch`. Replaying the original's result would silently discard the new intent — a manager who meant to ship 5 kg would be told the 2 kg they shipped an hour ago succeeded. |
| **Different** source store + same key | Two independent transfers. Both succeed. |
| Concurrent retries of the same key | Exactly one transfer. The losers replay the winner. |

Uniqueness is **scoped to the source store**:

```sql
UNIQUE (source_store_id, idempotency_key_hash)      -- uq_transfer_source_idem
```

Two branch managers working from the same printed run-book will legitimately send
`Idempotency-Key: 1`. That collision is a **coincidence, not a replay**. With a
global key, Beşiktaş's transfer would silently return Kadıköy's result and ship
nothing at all.

**The raw key is never stored** — only its SHA-256 digest — and neither the digest
nor the request hash is ever returned in a response or written to the audit trail.

---

## 7. Permissions

Transfer requires **`inventory:adjust`** — the same physical-stock authority as
waste and manual adjustment, because it permanently changes what is on a branch's
shelves.

| Role | Transfer? | Why |
|---|---|---|
| OWNER | ✅ | Holds `inventory:adjust`. |
| MANAGER | ✅ | Holds `inventory:adjust`. |
| KITCHEN | ❌ | Has `inventory:read` so it can flag a shortage, but not `inventory:adjust`. A cook shipping a crate to another branch is exactly the unaccountable stock movement this lifecycle exists to prevent. |
| CASHIER | ❌ | No inventory permission at all. Money and stock are separate authorities. |

No new permission was added, and no role's permissions were changed. If
`inventory:adjust` is ever granted to KITCHEN, transfer comes with it — deliberately,
in one place (`app/core/permissions.py`), not silently.

Every state-changing call additionally requires an authenticated staff session, a
trusted `Origin`, a valid CSRF token, and an `Idempotency-Key`.

**There is no cross-store transfer authority and no super-admin override.** A user
ships out of their own store, or not at all.

**Limitation:** the receiving store does not approve. See [Limitations](#12-limitations).

---

## 8. Reconciliation

`scripts/reconcile_inventory.py` (read-only; it never writes).

A transfer's legs are **ordinary ledger deltas**, so the existing per-store on-hand
check already accounts for them with no special case: the outbound leg lowers the
source's total, the inbound raises the destination's, each branch reconciles on its
own.

What the per-store totals **cannot** see is a **half transfer** — and that is why a
fourth check exists:

> Stock that left Kadıköy and arrived nowhere leaves Kadıköy's ledger and summary in
> **perfect agreement with each other**. Both are simply 2 kg short of physical
> reality. No per-store total is wrong. Only comparing the transfer against its legs
> finds it.

So for every transfer the reconciler verifies:

- exactly **one** `TRANSFER_OUT`, in the transfer's source store,
- exactly **one** `TRANSFER_IN`, in the transfer's destination store,
- both for the transfer's **ingredient**, at the transfer's **quantity**, with the
  correct **signs**, and zero reserved delta.

A broken pair **fails the run** (exit 1) and is reported to **both** branches — the
one that shipped and the one that never got its crate.

```bash
python scripts/reconcile_inventory.py                 # every store
python scripts/reconcile_inventory.py --store-id 2    # one store, both directions
python scripts/reconcile_inventory.py --json
```

The database already refuses to create a broken pair (see § 11). This check exists
because a reconciler **must not assume the constraint protecting it was actually in
force** — it is there to catch a manual SQL edit, a restore from an inconsistent
backup, or a future migration bug.

---

## 9. Analytics definitions

| Metric | Transfer's effect | Why |
|---|---|---|
| **Waste** | **Excluded.** `TRANSFER_OUT` is not waste. | Same sign and magnitude as waste, but the branch shipped the chocolate — it did not bin it. |
| **Purchase receipts** | **Excluded.** `TRANSFER_IN` is not a purchase. | Nobody bought it. Counting it would inflate chain purchasing spend on every internal shipment. |
| **Consumption velocity** | **Excluded.** Neither leg is consumption. | Velocity is the rate a branch physically *burns* stock, and it drives reordering. A branch that shipped stock away did not consume it. |
| **`last_restocked`** | **Not touched.** | A PURCHASE_RECEIPT concept. The branch was not resupplied by a supplier. |
| **On-hand / available** | **Changed** — source down, destination up. | This is what a transfer *is*. |
| **Stockout risk** | **Changed**, on both sides. | Risk runs on `available`. A branch that ships away its last chocolate genuinely *is* about to run out; the branch that received it genuinely is not. This is the one thing a transfer legitimately moves. |
| **Movement history** | Shows `TRANSFER_OUT` / `TRANSFER_IN` **distinctly**. | A reader must never have to guess what a bare signed number meant. |

These hold **by construction**, not by a filter someone must remember: the analytics
queries in `decision_engine.py` select `movement_type == CONSUMPTION` explicitly, so
a new movement type is excluded from velocity unless someone deliberately adds it.

---

## 10. Audit

Every transfer writes one append-only audit record:

```text
entity_type  inventory_transfer
entity_id    <transfer id>
action       INVENTORY_TRANSFERRED
actor_type   STAFF
actor_id     <initiator user id>
payload_after
    transfer_id, source_store_id, destination_store_id,
    ingredient_id, quantity, unit, actor_user_id, reason, status
```

**Never logged:** session tokens, CSRF tokens, raw idempotency keys, request hashes.
An audit trail that leaks a replayable credential is a liability, not a control.

---

## 11. Database constraints

Everything below is enforced by **PostgreSQL**, not by application code.

| # | Guarantee | Mechanism |
|---|---|---|
| 1 | Quantity > 0 | `ck_transfer_quantity_positive` |
| 2 | Source ≠ destination | `ck_transfer_stores_differ` |
| 3 | Source store exists | `fk_transfer_source_store` |
| 4 | Destination store exists | `fk_transfer_destination_store` |
| 5 | Ingredient exists | `fk_transfer_ingredient` |
| 6 | Initiator belongs to the source store | `fk_transfer_actor_source_store` (composite) |
| 7 | Idempotency unique **per source store** | `uq_transfer_source_idem` |
| 8 | OUT leg matches the transfer's **source** store + ingredient | `fk_movement_transfer_source_leg` (composite, via generated column) |
| 9 | IN leg matches the transfer's **destination** store + ingredient | `fk_movement_transfer_destination_leg` |
| 10 | `TRANSFER_OUT` → negative on-hand delta, zero reserved | `ck_movement_delta_matches_type` |
| 11 | `TRANSFER_IN` → positive on-hand delta, zero reserved | `ck_movement_delta_matches_type` |
| 12 | Transfer movements are **append-only** | `trg_ingredient_stock_movements_immutable` (existing; UPDATE/DELETE refused) |
| 13 | **A transfer can never be one-sided** | `trg_inventory_transfers_paired` + `trg_transfer_movement_paired` — see below |
| — | `transfer_id` present ⟺ a transfer movement type | `ck_movement_transfer_link` |
| — | The inbound leg carries no actor | `ck_movement_transfer_in_no_actor` |
| — | At most one leg per direction | `uq_movement_transfer_direction` (partial unique index) |
| — | Reserved never goes above on-hand | `ck_stock_reserved_le_on_hand` (existing) |

### The pairing invariant (#13) — why a trigger

Every other constraint is **per row**. None of them can say *"this transfer has both
of its halves"*, because that is a statement about a **set** of rows — and a
one-sided transfer is the single worst outcome this feature can produce.

So: a **DEFERRED constraint trigger**, checked at `COMMIT`, on **both** tables:

- on `inventory_transfers` — catches a transfer whose legs were never written;
- on `ingredient_stock_movements` — catches a leg bolted on afterwards.

It must be deferred: the two legs cannot both exist at the instant the first is
inserted.

Written to the same rules as the append-only trigger beside it: `SECURITY INVOKER`,
a **pinned `search_path`**, **schema-qualified** references, **no dynamic SQL**,
`EXECUTE` revoked from `PUBLIC`, and **no GUC or session variable that can switch it
off** — any role, including one reached through an injection path, could set one.

Combined with `uq_movement_transfer_direction` (at most one of each) and
`ck_movement_transfer_link` (only transfer types may carry a `transfer_id`), a
transfer has **exactly two** movements. Never one. Never three.

---

## 12. Limitations

Deliberately **not** built. Each is a real feature, and none is a half-built stub:

- **No approval workflow.** The source store ships unilaterally; the receiving store
  does not accept, confirm, or reject. In a small chain where a manager phones ahead
  before loading a van, this matches the physical operation. A chain where it does
  not should build approval — and *then* the transfer status domain widens.
- **No in-transit status.** Stock lands in the destination the instant the transfer
  commits. There is no "on the van" state, so a transfer posted before the van
  actually leaves will briefly overstate the destination's shelf.
- **No transfer cancellation or reversal.** The ledger is append-only. A transfer
  sent in error is corrected by a **new transfer in the opposite direction**, which
  is honest: the chocolate physically travelled twice.
- **No supplier management.**
- **No purchase-order management.**
- **No lot / expiry tracking.** A transfer moves an ingredient quantity, not
  identified batches, so it cannot answer "which batch went to Beşiktaş?".
- **No barcode scanning.**
- **No UI.** Backend only — no owner-web transfer screen. The API is exercised
  through tests and can be driven with `curl`.
- **Single-ingredient transfers.** One transfer moves one ingredient. Shipping a
  crate with five things in it is five transfers, five business events. A multi-line
  transfer would be a natural extension of this table.

---

## 13. API

All routes are protected. `POST` additionally requires trusted Origin + CSRF +
`Idempotency-Key`.

### `POST /inventory/transfers` — `inventory:adjust`

```jsonc
// Request. Unknown fields are REJECTED (422), not ignored.
{
  "destination_store_id": 2,
  "ingredient_id": 7,
  "quantity": "2000.000",
  "reason": "Beşiktaş çikolatası bitmek üzere",
  "note": "sabah servisiyle gönderildi"     // optional
}
```

```jsonc
// 200
{
  "transfer_id": 41,
  "source_store_id": 1,
  "destination_store_id": 2,
  "ingredient_id": 7,
  "ingredient_name": "Çikolata",
  "quantity": "2000.000",
  "unit": "g",
  "status": "COMPLETED",
  "reason": "Beşiktaş çikolatası bitmek üzere",
  "note": "sabah servisiyle gönderildi",
  "initiated_by_user_id": 3,
  "source_movement_id": 918,
  "destination_movement_id": 919,
  "source_on_hand_quantity": "3000.000",
  "source_reserved_quantity": "250.000",
  "source_available_quantity": "2750.000",
  "created_at": "2026-07-12T09:14:22Z",
  "idempotent_replay": false
}
```

Errors: `400 idempotency_required` · `403` (CSRF / Origin / permission) ·
`404 destination_store_not_found` · `404 stock_not_configured` ·
`404 ingredient_not_found` · `409 insufficient_available` ·
`409 idempotency_mismatch` · `422 same_store_transfer` · `422` (validation).

### `GET /inventory/transfers` — `inventory:read`

Transfers this store **sent or received**, newest first. Optional
`?direction=OUTBOUND|INBOUND`, `?ingredient_id=`, `?limit=`.

Both directions are shown deliberately: a branch that could only see its outbound
shipments could not answer *"where did this crate come from?"*, which is half of what
traceability is for. Each item carries a `direction` field relative to the caller.

### `GET /inventory/transfers/{id}` — `inventory:read`

One transfer, **if the caller's store is one of its two sides**. A transfer between
two other branches returns **404, not 403** — a 403 would confirm it exists.

---

## 14. Migration

Revision **`f4b8c1d90e26`** (`inventory transfer workflow`), on top of
`e2c9a4b16d38` (store-scoped inventory).

**Purely additive.** No existing row of any table is read, rewritten or deleted —
there are no transfers to backfill, because before this migration a transfer was not
a thing that could be recorded. Every existing movement keeps `transfer_id = NULL`
and is untouched by every new constraint (each is vacuously true when `transfer_id`
is NULL). Orders, payments and stock quantities are not touched at all.

### Downgrade **refuses while transfers exist**

Dropping `inventory_transfers` would delete the only record that a shipment between
two branches ever happened — **while leaving the stock it moved exactly where it
moved it.** The ledger would keep a bare `-2 kg` in one store and `+2 kg` in another
with nothing to explain either, and the `TRANSFER_OUT` / `TRANSFER_IN` rows cannot
even be expressed in the movement-type domain being restored.

There is no correct reconstruction, so it **aborts loudly** (`TransfersExist`) rather
than producing a database that looks fine and is quietly wrong about where the stock
went. Export or reverse the transfers first.

With no transfers present, downgrade removes exactly this branch's schema — the
table, the three columns, the constraints, the indexes, the trigger — and restores
the previous movement type domain and delta rule verbatim.

---

## 15. Deployment

1. **Back up PostgreSQL.**
2. `alembic upgrade head`
3. Verify the transfer table: `\d inventory_transfers`
4. Verify the new movement types:
   ```sql
   SELECT pg_get_constraintdef(oid) FROM pg_constraint
   WHERE conname = 'ck_movement_type_domain';   -- must list TRANSFER_OUT, TRANSFER_IN
   ```
5. **Run reconciliation BEFORE enabling transfers** — a pre-existing drift must not
   be blamed on this feature:
   ```bash
   python scripts/reconcile_inventory.py
   ```
6. Create a **test transfer** between two real stores (a small quantity).
7. Confirm the **source** on-hand decreased by exactly that quantity.
8. Confirm the **destination** on-hand increased by exactly that quantity.
9. Confirm **waste and consumption analytics are unchanged** — the owner's waste
   report and consumption velocity must not have moved at all.
10. **Run reconciliation again.** It must pass, including the transfer-pair check.
11. **Train staff:** *branch transfers are never manual adjustments.* A manager who
    types a transfer in as two adjustments defeats every guarantee in this document,
    and the system cannot detect that they did.

---

## 16. See also

- [`STORE_SCOPED_INVENTORY.md`](STORE_SCOPED_INVENTORY.md) — why stock belongs to a branch
- [`INVENTORY_LIFECYCLE.md`](INVENTORY_LIFECYCLE.md) — reserve → consume → waste
- `apps/api/app/models/inventory_transfer.py`
- `apps/api/app/services/inventory_service.py` → `transfer_stock()`
- `apps/api/alembic/versions/f4b8c1d90e26_inventory_transfer_workflow.py`
