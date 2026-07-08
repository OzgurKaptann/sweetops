# Order-Item Quantity Accounting Fix

**Branch:** `fix/order-quantity-accounting` (based on `main`, commit `f84a921`)
**Scope:** Order creation pricing and ingredient stock accounting only. No changes to
QR security, authentication/RBAC, frontend behaviour, analytics/forecasting formulas,
dbt models, inventory lifecycle semantics, cancellation policy, dependency versions, or
Alembic migrations.
**Primary file:** `apps/api/app/services/order_service.py`

---

## 1. The bug

An order line carries an **item quantity** (how many of a product were ordered) and, per
ingredient, a **selected quantity** (portions of that modifier per product). Before the fix
the item quantity was applied to the **base product price only**. It was *not* applied to:

- ingredient modifier pricing, and
- physical ingredient consumption (`consumed_quantity`).

Because stock validation, stock deduction, the stock-movement record and cancellation
restoration all derive from `consumed_quantity`, every one of those figures was wrong for
any order with `item_quantity > 1`.

### Concrete failure

Order: 3 waffles, each with 1× banana modifier. Banana: `price = 10`, `standard_quantity = 50 g`.

| Quantity | Pre-fix | Correct |
|---|---|---|
| Base price charged | 100 × 3 = 300 | 300 |
| Banana modifier charged | 10 × **1** = 10 | 10 × 1 × **3** = 30 |
| Banana grams consumed | 50 × **1** = 50 | 50 × 1 × **3** = 150 |

The customer was **under-charged** by 20 and stock was **silently over-reported** by 100 g
per order — the physical shelf ran out long before the system said it should.

---

## 2. The fix

### Canonical formula (single source of truth)

A single helper is the ONE place the physical quantity is computed:

```python
def calculate_consumed_quantity(
    standard_quantity: Decimal,
    selected_quantity: int,
    item_quantity: int,
) -> Decimal:
    return standard_quantity * selected_quantity * item_quantity
```

Every downstream figure is derived from this one value, so they can never drift apart:

| Concern | Location | How item quantity is applied |
|---|---|---|
| Stock validation (`required` map) | `order_service.py` §3 | `calculate_consumed_quantity(...)` |
| Persisted `consumed_quantity` | `order_service.py` §6 | `calculate_consumed_quantity(...)` |
| Stock deduction | `order_service.py` `_deduct_stock` | reuses `required[ing_id]` |
| Stock-movement `quantity_delta` | `order_service.py` `_deduct_stock` | reuses `required[ing_id]` (`-needed`) |
| Ingredient modifier pricing | `order_service.py` §6 | `ing.price * ing_data.quantity * item_data.quantity` |
| Cancellation restoration | `kitchen_service.py` `_return_stock_for_order` | reuses persisted `consumed_quantity` |

Validation, deduction and movement all consume the same `required` map; the persisted
`consumed_quantity` uses the identical formula; cancellation reads back the persisted value.
There is exactly one multiplication of item quantity into consumption, and one into pricing.

### Why cancellation needed no change

`_return_stock_for_order` already restores exactly `oii.consumed_quantity`. Because that
persisted value now correctly includes item quantity, restoration is correct automatically —
no formula change, preserving the existing cancellation policy and idempotency guard
(`CANCELLATION_RETURN` written once, only when an `ORDER_DEDUCTION` exists).

---

## 3. Tests added

New regression suite: `apps/api/tests/test_order_quantity_accounting.py`. Every scenario
**fails on the pre-fix code** (item quantity omitted) and **passes after the fix**.

| # | Scenario | Guards |
|---|---|---|
| 1 | Single product, single portion (`qty=1`) | No regression to existing behaviour |
| 2 | 3 products × 1 portion | Price, `consumed_quantity`, stock level, movement all ×3 |
| 3 | 2 products × 3 portions (multiplier 6) | item_qty × selected_qty compose correctly |
| 4/5 | Multiple order items + ingredient shared across items | Per-line and aggregated deduction agree |
| 6 | Stock enough for 1 but not 3 | Item quantity drives `out_of_stock` (422); nothing committed |
| 7 | Idempotent retry with `qty=3` | Deducts once; retry returns same order/total |
| 8 | Cancellation with `qty=3` | Restores exactly the deducted 150 g, once (double-cancel → 409) |
| 9 | 5 concurrent `qty=3` orders, stock fits exactly 2 | `FOR UPDATE` locking; stock never negative |

---

## 4. Verification

See the final review report for exact command output. Commands run:

```bash
# apps/api
python -m pytest -q tests/test_order_quantity_accounting.py
python -m pytest -q tests/test_rollback.py
python -m pytest -q tests/test_concurrency.py
python -m pytest -q tests/test_state_machine.py
python -m pytest -q --collect-only
python -m pytest -q            # full suite (PostgreSQL available)

# repo root
python -m compileall apps/api/app
git diff --check
npm run build:types
npm run build:ui
npm run build --workspace=customer-web
npm run build --workspace=kitchen-web
npm run build --workspace=owner-web

# apps/api
alembic heads   # expected: 4299b615f7aa (head)
```

---

## 5. Product-language constraint

No new user-facing English text was introduced (SweetOps targets a Turkish waffle shop).
The change is confined to internal calculation logic; test names, code and this document
are technical English, which is permitted.
