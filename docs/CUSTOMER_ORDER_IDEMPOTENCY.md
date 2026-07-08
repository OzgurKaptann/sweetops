# Customer Order Idempotency

How the SweetOps QR ordering flow guarantees that **one logical checkout
attempt creates at most one order**, even under double-clicks, slow mobile
connections, browser retries, or a customer retrying after an uncertain result.

## 1. Original duplicate-order risk

The customer app submitted orders with a plain `POST /public/orders/` that sent
**no** `Idempotency-Key` header. The only protection against duplicates was a
React `submitting` state flag. Because React state updates are asynchronous, a
fast second tap could fire the handler before the flag re-rendered, and a
network timeout or lost response gave the customer no safe way to retry: each
retry was a brand-new order. Result: a single logical checkout could create
multiple orders — deducting stock and spawning kitchen tickets more than once.

## 2. Existing backend capability

The backend already supports idempotency and needed **no changes**:

- `POST /public/orders/` reads an optional `Idempotency-Key` request header
  (`apps/api/app/routers/public_orders.py`).
- `create_order` (`apps/api/app/services/order_service.py`) looks the key up
  against the unique `orders.idempotency_key` column. On a hit it returns the
  **existing** order (HTTP 200) without touching stock, movements, order items
  or ingredient-consumption rows.
- The column has a `UNIQUE` constraint and index
  (`alembic/versions/c9f1d3e8a042_production_hardening.py`), so a duplicate key
  can never create a second order.

Verified by `apps/api/tests/test_rollback.py::TestStockDeduction::test_idempotent_order_does_not_double_deduct`
and the movement tests in the same file.

## 3. Missing customer-side behavior

The server could de-duplicate, but only if the client sent **the same key**
across retries of the same order and a **new key** once the order changed. The
client did neither — it sent no key at all. This branch supplies that missing
client behavior.

## 4. Logical attempt definition

One *logical order attempt* is one specific order payload. It is identified by a
deterministic **fingerprint** (§5). All submissions of the same fingerprint —
double-clicks, in-flight duplicate handlers, and retries after network
uncertainty — belong to the same logical attempt and share one idempotency key.
Any material change to the payload starts a **new** logical attempt with a new
key.

## 5. Payload fingerprint design

`fingerprintOrder()` (`apps/customer-web/src/lib/order-idempotency.ts`) produces
a deterministic string from only the fields that affect what the backend
persists:

- `store_id`
- `table_id` (`null`/`undefined` normalized to `null`)
- each item's `product_id` and `quantity`
- each item's ingredients: `ingredient_id` and `quantity`

Ordering is normalized before serialization so equivalent orders never diverge:

- ingredients are sorted by `ingredient_id`, then `quantity`;
- items are sorted by `product_id`, then `quantity`, then a stable secondary
  representation of their normalized ingredient list;
- the normalized structure is serialized with `JSON.stringify`.

Transient UI state (toasts, upsell suggestions, selection widgets) is excluded.

## 6. Key generation

`generateIdempotencyKey()` prefers `crypto.randomUUID()`, falling back to a
UUIDv4 built from `crypto.getRandomValues`. It never uses timestamps, counters,
or `Math.random()` on their own; if no secure random source exists it throws
rather than mint a weak, guessable key.

## 7. Retry behavior

`getOrCreateKey(fingerprint)`:

- returns the **existing** key if the stored attempt's fingerprint matches
  (retries, double-clicks, network-uncertainty retries) — the backend then
  returns the already-created order instead of duplicating it;
- mints, stores, and returns a **new** key when the fingerprint changed or no
  attempt exists.

## 8. Double-click protection

Two layers:

1. A synchronous `submittingRef` guard in `CustomerMenuPageClient` returns
   immediately on re-entry, closing the async-state-update race.
2. The submit button is `disabled` while `submitting` is true and shows the
   Turkish `Gönderiliyor…` state.

The UI guard is a convenience; the idempotency header remains the real
guarantee. Even if two requests reach the server, they carry the same key and
resolve to one order.

## 9. Success behavior

On a confirmed successful response (a newly created order **or** the same order
returned for the key):

- the active attempt is cleared (`orderIdempotency.clear()`);
- the cart is reset (`setSelected(new Set())`);
- the button stays disabled through navigation to `/success`, so the completed
  order cannot be resubmitted;
- a subsequent new order computes a new fingerprint and receives a new key.

The success page continues to receive `order_id` and `amount` via query string,
unchanged.

## 10. Network uncertainty behavior

`createOrder` classifies failures via `OrderRequestError`:

- **network** (fetch rejected — offline/timeout/lost response) and **server**
  (HTTP 5xx) are *uncertain* (`isUncertain === true`). The order may already
  exist, so the key and cart are **preserved** and the customer sees:

  > Sipariş sonucu doğrulanamadı. Tekrar deneyebilirsin; siparişin iki kez oluşturulmayacak.

  A retry reuses the same key and is therefore safe.
- **validation** (HTTP 4xx, e.g. out of stock) is a deterministic rejection: the
  cart is kept so the customer can adjust; changing the selection yields a new
  key automatically.

## 11. Storage behavior

The active attempt is stored in `sessionStorage` under
`sweetops.pendingOrderAttempt`, so one logical attempt survives a component
rerender, route transition, accidental refresh, or uncertain network result.

Storage is accessed only through a guarded provider that:

- returns `null` during SSR (`typeof window === "undefined"`);
- probes writability (private-mode browsers expose but reject
  `sessionStorage`);
- resolves lazily on every operation, so a store constructed during SSR still
  uses real storage in the browser;
- falls back to an in-memory copy when storage is unavailable or throws.

Only a fingerprint and a random key are stored — **no** customer-identifying or
sensitive data. Completed attempts are cleared on success, never persisted
indefinitely.

## 12. Turkish user-facing messages

All new/changed customer-facing text is Turkish:

| Situation | Message |
| --- | --- |
| Submitting | `Gönderiliyor…` (existing) |
| Network/server uncertainty | `Sipariş sonucu doğrulanamadı. Tekrar deneyebilirsin; siparişin iki kez oluşturulmayacak.` |
| Deterministic rejection | `Sipariş oluşturulamadı. Lütfen seçimlerini kontrol et.` |

No new user-facing English text was introduced.

## 13. Tests and verification

Pure-TypeScript tests run with Node's built-in test runner (no new framework
added). `npm run test --workspace=customer-web`, or directly:

```bash
node --test apps/customer-web/src/lib/order-idempotency.test.ts \
            apps/customer-web/src/lib/api.test.ts
```

Covered scenarios:

- **1 — Header sent**: `createOrder` sends a non-empty `Idempotency-Key`.
- **2 — Double click**: two rapid `getOrCreateKey` calls for one payload yield
  one key (plus the synchronous `submittingRef` guard in the component).
- **3 — Network retry**: an unchanged payload reuses the same key.
- **4 — Payload change**: store, table, product, quantity, ingredient, or
  ingredient-quantity changes produce a new key / fingerprint.
- **5 — Successful completion**: `clear()` retires the attempt; the next order
  gets a different key.
- **6 — Deterministic normalization**: reordered-but-equivalent payloads share a
  fingerprint.
- **7 — Different payload**: materially different orders differ.
- **8 — Storage unavailable**: in-memory fallback works, including a provider
  that throws; a persisted attempt survives a fresh store instance.

Also classified in tests: network vs. 5xx (uncertain) vs. 4xx (validation).

Backend idempotency regression is covered by the existing API suite
(`test_rollback.py`, `test_order_quantity_accounting.py`) — no backend behavior
was altered.

## 14. Deferred: server-side payload mismatch protection

The backend currently returns the existing order for a known key **without
comparing the payload**. If a client reused a key with a *different* payload, it
would silently receive the first order. The customer app never does this — a
changed payload always mints a new key — so the risk is not reachable through
the UI. Adding a server-side fingerprint/mismatch check (returning HTTP 409 on
conflicting reuse) is deferred; it would require a schema/migration change that
is out of scope for this branch.

## 15. Deferred: payment settlement workflow

Payment settlement is explicitly **out of scope**. This work covers order
creation idempotency only. Order preparation statuses (`NEW`, `IN_PREP`,
`READY`, `DELIVERED`, `CANCELLED`) must **not** be overloaded with payment
state; a separate payment-status model is future work.
