# Customer Menu Scoping

**Status:** implemented on `fix/customer-menu-scope-and-selection`
**Migration:** `a9e4c7b25d13` — `products.is_active` + `store_products`
**Fixes:** [RUNTIME_PRODUCT_GAP_REVIEW.md](RUNTIME_PRODUCT_GAP_REVIEW.md) F-02, F-23,
and the product-selection/quantity half of F-01
**Related:** [SECURE_QR_TABLE_CONTEXT.md](SECURE_QR_TABLE_CONTEXT.md) ·
[STORE_SCOPED_INVENTORY.md](STORE_SCOPED_INVENTORY.md)

---

## 1. The problem

The customer menu was the whole `products` table:

```python
# apps/api/app/services/menu_service.py, before
products = db.query(Product).all()
```

No store filter and no activation filter — and neither was possible, because
`products` had only `id`, `name`, `category`, `base_price`, `created_at`. There
was nothing to filter on.

Two consequences, one of which was already true in the live database:

* **Test debris was customer-facing.** Eight `TestWaffle_<hex>` rows at ₺100.00,
  left behind by interrupted test runs, sat in the same table the public menu
  endpoint served. They were invisible only because the customer screen rendered
  `products[0]` and nothing else — a coincidence, not a control.
* **Two branches could not have two menus.** A seasonal item, a branch that does
  not sell drinks, a product being trialled in one shop: none of it was
  expressible.

And on the client:

```tsx
// apps/customer-web/src/components/CustomerMenuPageClient.tsx, before
const product = menu?.products[0] ?? null;
items: [{ product_id: product.id, quantity: 1, … }]
```

The guest never chose a product and never chose how many. Whatever sat first in
the array was ordered, one of it, at that price.

---

## 2. The model

Two orthogonal boundaries, mirroring the catalog/physical split that
`ingredients` + `ingredient_stock` already use.

| Column / table | Grain | Means |
| --- | --- | --- |
| `products.is_active` | catalog, chain-wide | The chain still sells this at all. Retiring an item switches it off once, everywhere. |
| `store_products` | (branch, product) | **This branch publishes this product to guests.** No row ⇒ not on anybody's menu. |
| `store_products.is_available` | (branch, product) | Published, but off the board today (sold out). The publication decision survives. |
| `store_products.sort_order` | (branch, product) | Menu order within the branch, ties broken by name so the list never reshuffles between two loads. |

A product reaches a guest through a **relationship**, never through a name
filter. That is the whole point: matching on `TestWaffle_%` would be a filter
over a symptom, and the next stray row would be called something else. Debris is
excluded because nobody ever published it.

There is deliberately **no price column** on `store_products`. A branch
publishes the chain's product at the chain's price. Per-branch pricing is P1-B —
named and deferred.

### Why the migration exists at all

`REAL_USE_READINESS_ROADMAP` P0-D required this decision to be taken explicitly,
with a migration, or the branch deferred. The existing data model genuinely
could not express it: no `store_id`, no `is_active`, no join table, and nothing
else in the schema that meant "on the menu".

### Why nothing was backfilled

The obvious backfill — one row per (store × product) — would, on the very first
upgrade, republish exactly the debris the boundary exists to contain. It would
also be a lie: no explicit publication decision was ever taken for any existing
row, so none can be inferred from one.

So the customer catalog **fails closed**: after this migration every store's menu
is empty until something is offered. `scripts/seed_demo_data.py` publishes the
demo menus (Kadıköy sells all five products, Moda only the three waffles — the
demo data now exercises store scoping rather than merely permitting it). A real
shop is provisioned the same way until the authenticated onboarding surface
(P0-E) exists; **that surface is not part of this branch.**

> **Since resolved.** `feat/store-setup-and-menu-provisioning` adds the owner-facing
> half: an OWNER/MANAGER can now publish, withdraw, switch off for the day and
> reorder their own branch's menu from `/setup` in owner-web, and a readiness
> checklist explains *why* a menu is empty rather than leaving a shop to guess. See
> [STORE_SETUP_AND_MENU_PROVISIONING.md](STORE_SETUP_AND_MENU_PROVISIONING.md). The
> seed script remains the only way to create a *store*, and staff accounts, per-store
> pricing and a printable QR sheet are all still unbuilt.

---

## 3. Where it is enforced

### Menu read — `menu_service.list_menu_products`

```sql
products JOIN store_products ON store_products.product_id = products.id
WHERE store_products.store_id = :resolved_store
  AND store_products.is_available
  AND products.is_active
ORDER BY store_products.sort_order, products.name, products.id
```

`:resolved_store` comes from the QR token (`POST /public/menu/resolve`), never
from a client parameter. The ungated `GET /public/menu/` has no token and
therefore resolves the single operational store, refusing with a Turkish 409
once a second branch is staffed — unchanged behaviour, now applied to products
as well as stock.

An unprovisioned branch returns `"products": []`. The customer app renders a
Turkish empty state. It never falls back to "everything".

### Order creation — `order_service._resolve_menu_products`

Every ordered `product_id` is re-checked against the store the token resolved
to, before any stock row is locked:

* the product exists;
* `products.is_active`;
* a `store_products` row for (resolved store, product) with `is_available`.

Anything else is one `422` with the machine code `product_unavailable` and one
Turkish message. The reasons are deliberately not distinguished: a guest can
only ever do the same thing about all of them, and a per-reason response would
let a probe map which product ids exist in other branches.

The rendered menu is **not** evidence. A real, active product id belonging to
another branch's menu is refused here, and so is a product withdrawn between the
menu load and the tap on "Sipariş ver".

### Quantity — `apps/api/app/schemas/order.py`

| Bound | Value | Why |
| --- | --- | --- |
| `MAX_ITEM_QUANTITY` | 20 | Portions of one product on one line. |
| `MAX_INGREDIENT_PORTIONS` | 5 | Portions of one ingredient on one product. |
| `MAX_ORDER_ITEMS` | 20 | Lines in one submission. |

All are `ge=1`, so `0` and negatives are refused. A negative used to multiply
straight through `calculate_consumed_quantity` into a **negative stock
requirement** — a "sale" that releases stock and reduces the bill. The customer
app offers a narrower range still (`MAX_QUANTITY = 10` in
`order-selection.ts`); the server is what makes it safe.

Reservation logic is untouched: quantities still flow through the same
`calculate_consumed_quantity` → `lock_stock_rows` → `reserve_for_order` path.

---

## 4. What the guest sees

`apps/customer-web/src/lib/order-selection.ts` holds the decision as pure
functions, so it is testable under `node --test` without a DOM:

* **Nothing is pre-selected** — not even when the branch sells exactly one
  product. `buildOrderSubmission` returns `null` rather than guessing; there is
  no default-to-first and no "if there is only one it must be that one".
* **The choice is visible before submit** — the sticky bar names the product and
  the topping count, and the button reads `Ürün seçin` → `Malzeme seçin` →
  `Sipariş ver — ₺…`.
* **Quantity is an explicit stepper**, bounded 1–10, shown next to the total.
  The default 1 is rendered, not implied.
* **A stale selection cannot be submitted** — the chosen id is resolved against
  the *current* product list on every render.
* **Empty menu and invalid/expired QR** both render calm Turkish states with no
  way to submit.

---

## 5. What this branch is NOT

Named so nobody reads a wider claim into it:

* **No multi-item cart.** One product line per submission. A table ordering two
  waffles *and* a coffee still submits twice. (Roadmap P0-D's cart half.)
* **No shop onboarding.** Publishing a product still needs the seed script or a
  developer. That is F-13 / P0-E, untouched. — **Addressed since**, in part, by
  `feat/store-setup-and-menu-provisioning`: publishing, withdrawing, availability
  and menu order are now owner-web actions. Creating a *store* and a staff account
  still needs a script.
* **No QR management surface.** Unchanged. — **Addressed since**: tables can be
  added and renamed, and a table's QR sticker issued or rotated, from `/setup`. The
  link is still shown exactly once, because the raw token is stored only as a hash.
* **No per-store pricing.** P1-B. — still true.
* **No payment, kitchen, cashier, inventory, shift or order-issue change.**

---

## 6. Test coverage

`apps/api/tests/test_customer_menu_scoping.py` (14 tests) — menu returns only
published products; unpublished rows reach no branch; inactive products excluded
even when published; `is_available=False` excluded and restorable; two branches
keep independent menus; an unprovisioned branch returns `[]` rather than the
catalog; ordering an unpublished / other-branch / retired / just-withdrawn
product is refused; non-positive and absurd quantities refused; empty order
refused; a valid order still succeeds.

`apps/customer-web/src/lib/order-selection.test.ts` (18 tests, taking the
customer-web suite from 41 to 59) — no
`products[0]` fallback, including the single-product case; the payload carries
the chosen product; a withdrawn selection cannot submit; submit gating per
precondition; quantity clamping, stepping and junk input; empty-menu state;
price multiplication.

None of them match on a product name.
