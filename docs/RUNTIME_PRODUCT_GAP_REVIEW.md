# SweetOps — Real-Use Readiness and Product Gap Review

**Date:** 2026-07-23
**Branch:** `audit/runtime-product-gap-review`
**Scope:** audit and documentation only. No application logic, schema, migration,
dependency, test, or UI change was made in the branch that carries this document.
**Companion:** [REAL_USE_READINESS_ROADMAP.md](REAL_USE_READINESS_ROADMAP.md)

---

## 0. What was and was not verified

This is the honesty contract for everything below. Read it before quoting any
finding.

### Verified (commands actually run on this machine, 2026-07-23)

| Check | Result |
| --- | --- |
| `git diff --check` | clean, no whitespace errors |
| `python scripts/verify_release_readiness.py` | **OK — all 9 checks passed** (16 docs, 8 scripts, 2 env examples, 7 package scripts, single Alembic head `e7f2a9c04d18` over 17 revisions, 125 doc links resolve, no merge markers, no committed secrets in 375 files) |
| `python scripts/reconcile_kitchen_timing.py` | **OK (all stores)** — every order's lifecycle events internally consistent |
| `python scripts/reconcile_payments.py` | **OK (all stores)** — every order summary matches the ledger; every closed shift snapshot matches its window |
| `python scripts/reconcile_inventory.py` | **OK (all stores)** — 39 (store, ingredient) rows across 3 stores; transfers, stock counts and thresholds all coherent |
| `python scripts/reconcile_order_issues.py` | **OK (all stores)** — every resolved issue matches the refund ledger |
| Read-only SQL against the running `sweetops_db` container | See §10 for the row counts quoted throughout |
| Source reading across `apps/api`, all four frontends, `packages/`, `data/dbt`, `scripts/`, `docs/` | Every file reference below is a real line in this repository |

### NEEDS_MANUAL_BROWSER_CONFIRMATION

**No browser flow was driven in this audit.** The API was not started, `npm install`
was not run, no frontend dev server was started, and no screen was opened. There is
no browser automation in this repository, so every claim about what a screen *looks
like* or *feels like under a rush* is derived from source and is explicitly marked:

> **NEEDS_MANUAL_BROWSER_CONFIRMATION**

Findings marked that way describe what the code will do. They are not observations.
Nothing in this document should be read as "we watched it happen" unless it appears
in the Verified table above.

Also **not** run in this audit: `pytest`, the frontend test suites, the frontend
builds, `alembic upgrade head`, and the demo seed. Those belong to the
`release-verification` sweep, not to a product audit.

### Severity and effort keys

- **Severity** — `blocker` (stops a paid pilot), `major` (a real shop works around
  it daily), `minor` (annoyance or future debt).
- **Effort** — S (< 1 day), M (1–3 days), L (a week or more).
- **Lens** — CTO · PM · DataEng · Analyst · UI.

---

## 1. What is genuinely good

An audit that only lists faults misprices the work. These are the parts a buyer
should pay for, and they are not common at this stage.

1. **The money and stock ledgers are real, and they reconcile.** All four
   reconcilers are green against a database that holds demo data, live orders,
   refunds, transfers, counts and a discrepant shift close. Payments are a
   settlement/allocation/refund ledger, not a `total_amount` column, and
   `docs/PAYMENT_SETTLEMENT_WORKFLOW.md` states the rules.
2. **Store scoping is derived from the session, never from the client.** Every
   owner and cashier read takes `staff.store_id`; there is no `store_id` query
   parameter to tamper with. The kitchen WebSocket partitions by store in
   `websocket_manager.py:31` and derives the store from the authenticated session
   at [`ws.py:60`](../apps/api/app/routers/ws.py#L60).
3. **The QR table context is properly hardened.** The token travels in the URL
   *fragment*, is captured once, scrubbed from the address bar, cached in
   `sessionStorage`, and a definitively dead token is forgotten rather than
   retried ([`CustomerMenuPageClient.tsx:376-398`](../apps/customer-web/src/components/CustomerMenuPageClient.tsx#L376-L398)).
   Legacy `?store=`/`?table=` params are never read.
4. **Idempotency is designed, not bolted on.** Customer order submission, cashier
   collection, cashier refunds and inventory actions all carry fingerprinted keys,
   with a distinction between "rejected — mint a new key" and "outcome unknown —
   safe to retry the same key" ([`CashierPage.tsx:46-65`](../apps/cashier-web/src/components/CashierPage.tsx#L46-L65)).
5. **The timing layer refuses to fabricate.** `_nonneg_delta` returns `None`
   rather than a negative or a zero when the event log is impossible
   ([`kitchen_timing_service.py:102-116`](../apps/api/app/services/kitchen_timing_service.py#L102-L116)),
   and averages are `None` — not `0` — when nothing completed.
6. **The owner operational dashboard's money rules are correct and written down.**
   `docs/OWNER_OPERATIONAL_DASHBOARD.md` §4 explicitly forbids using an order's
   `total_amount` as revenue. This matters, because a different module on the same
   page does exactly that (finding **F-03**).
7. **The demo seed is deterministic, idempotent, demo-scoped and non-destructive**,
   and says so with a tested guarantee (`docs/DEMO_SEED_DATA.md` §3).
8. **`docs/PRODUCTION_READINESS.md` §14 is an honest gap list.** Thirteen
   limitations, including the ones that are embarrassing. That is rare and it
   raises the credibility of everything else in the repo.

---

## 2. Installation readiness

Fresh-clone rehearsal was performed **against the README text, not against a live
machine** — the stack was already running here, so a true cold install was not
re-executed. Findings are from reading `README.md` §12 against
`docker-compose.yml` and the two `.env.example` files.

### F-16 · `docker-compose up -d` starts more than the README says — and points Metabase at the operational database
**Lens:** CTO / DataEng · **Severity:** major · **Effort:** S

README §12 step 1 says the command starts "the backend stack (API + PostgreSQL)".
`docker-compose.yml` defines **five** services: `postgres`, `redis`, `api`,
`metabase` (port 3100) and `dbt`. Two of those are the legacy analytics stack that
`docs/PRODUCTION_READINESS.md` §14.10 already classes as unmaintained.

The sharper problem is the Metabase configuration:

```yaml
metabase:
  environment:
    - MB_DB_TYPE=postgres
    - MB_DB_DBNAME=sweetops_db     # ← the OPERATIONAL database
    - MB_DB_USER=sweetops
```

`MB_DB_*` is Metabase's **own application database**, not its data source. As
configured, a first `docker-compose up -d` lets Metabase create and own dozens of
its internal tables inside `sweetops_db`, alongside `orders` and
`payment_settlements`. That is a documented-nowhere side effect of the documented
first command, it pollutes the schema an operator inspects during an incident, and
it is the sort of thing that makes a `pg_dump` restore surprising.

**NEEDS_MANUAL_BROWSER_CONFIRMATION** for the exact table set Metabase creates —
this finding is read from the compose file, not from an observed run.

### F-27 · Redis is started and configured but has no consumer
**Lens:** CTO · **Severity:** minor · **Effort:** S

`REDIS_URL` is in both `.env.example` files and Compose starts a `redis:7`
container with a persistent volume. No application code reads it (no Redis client
appears in `apps/api/app`). It is either dead infrastructure or an undocumented
future dependency; either way it is a container an operator has to reason about.

### What is right about installation

- Both `.env.example` files contain placeholders only, are commented to a genuinely
  high standard, and explicitly flag the known `CUSTOMER_WEB_BASE_URL` port-3000
  vs 3001 mismatch instead of hiding it.
- `verify_release_readiness.py` is a real read-only repository check and passed 9/9
  here.
- The seed-before-test hazard is called out in the README itself, not buried.

---

## 3. Customer QR / table flow (customer-web, :3001)

**All runtime claims in this section: NEEDS_MANUAL_BROWSER_CONFIRMATION.**

### F-01 · The customer can only ever order one product, quantity 1
**Lens:** PM · **Severity:** blocker · **Effort:** M ·
**Status: PARTIALLY FIXED** — `fix/customer-menu-scope-and-selection`

> **Fixed:** the `products[0]` fallback is gone. The guest picks a product
> explicitly (nothing is pre-selected, not even on a one-product menu), the
> choice is named in the sticky bar before submit, and quantity is a visible
> stepper bounded 1–10 rather than a hard-coded `1`. Submit stays disabled until
> the selection is complete and still on the menu.
> **NOT fixed:** one product line per submission. A table wanting two waffles
> *and* a Türk Kahvesi still submits twice — the multi-item cart remains open
> under P0-D. See [CUSTOMER_MENU_SCOPING.md](CUSTOMER_MENU_SCOPING.md).

[`CustomerMenuPageClient.tsx:490`](../apps/customer-web/src/components/CustomerMenuPageClient.tsx#L490):

```ts
const product = menu?.products[0] ?? null;
```

The whole screen is built around `products[0]`. The submit payload is hard-coded to
a single line with `quantity: 1`
([`CustomerMenuPageClient.tsx:513-525`](../apps/customer-web/src/components/CustomerMenuPageClient.tsx#L513-L525)).

The database currently holds **14 products**, including the demo seed's
`Klasik Waffle` (₺90), `Çilekli Waffle` (₺110), `Muzlu Nutellalı Waffle` (₺130),
`Türk Kahvesi` (₺60) and `Limonata` (₺55) — see §10. A guest sees none of them.
They see product id 1, `Waffle`, ₺45, from the legacy `apps/api/seed.py`.

For a real waffle shop this means: no drinks, no second waffle, no "two of these",
no per-item note, no allergy field. A table of four either scans four times or the
staff take the rest of the order verbally — which is precisely the paper/WhatsApp
fallback this product exists to eliminate.

### F-02 · The product catalog is global, unfiltered, and currently contains test debris that is customer-facing
**Lens:** DataEng / CTO · **Severity:** blocker · **Effort:** M ·
**Status: FIXED** — `fix/customer-menu-scope-and-selection`

> Migration `a9e4c7b25d13` adds `products.is_active` (catalog retirement,
> chain-wide) and a `store_products` publication table (one row = "this branch
> offers this product"). The customer menu is now a join through that table
> against the store the QR token resolved to, and order creation re-checks the
> same relationship server-side before any stock is locked. A product nobody
> published is unreachable from every customer surface — no name matching
> anywhere. Nothing was backfilled: the catalog fails closed, so an
> unprovisioned branch returns an empty menu rather than the whole table. Full
> design, including what was deliberately deferred (per-store pricing, cart,
> onboarding): [CUSTOMER_MENU_SCOPING.md](CUSTOMER_MENU_SCOPING.md).

[`menu_service.py:38`](../apps/api/app/services/menu_service.py#L38):

```python
products = db.query(Product).all()
```

No store filter, no active filter — and `Product`
([`product.py`](../apps/api/app/models/product.py)) has neither a `store_id` nor an
`is_active` column to filter on. The docstring states the intent ("the catalog half
of the menu is global: every branch sells the same waffle"), which is a defensible
single-shop decision and an untenable multi-store one: two branches cannot have
different prices, different menus, or a seasonal item.

The immediate consequence is visible in the live database right now: **8 orphaned
`TestWaffle_<hex>` products at ₺100.00**, left by interrupted test runs, sit in the
same global catalog the public menu endpoint serves. `docs/PRODUCTION_READINESS.md`
§14.8 acknowledges test debris as "harmless"; this is the case where it is not —
the debris is in a customer-facing table with no filter between it and a guest's
phone. F-01 is currently masking it, because only `products[0]` is rendered.

### F-19 · "Bugün en çok seçilen" is neither today-based nor correctly sorted
**Lens:** PM / Analyst · **Severity:** minor · **Effort:** S

[`CustomerMenuPageClient.tsx:39-46`](../apps/customer-web/src/components/CustomerMenuPageClient.tsx#L39-L46):

```ts
const aScore = (a.popular_badge ? 1000 : 0) + b.ranking_score;
const bScore = (b.popular_badge ? 1000 : 0) + a.ranking_score;
```

The comparator mixes its operands — `aScore` is built from `b.ranking_score`. The
resulting order is not the intended popularity ranking. Separately, the label
promises *today's* most-chosen items while the input is `ranking_score` from the
menu enrichment, which is not a same-day count. Customer-facing copy should not
claim a time window the data does not have.

### F-21 · The guest's confirmation is built from URL parameters and then goes dark
**Lens:** PM / UI · **Severity:** minor · **Effort:** S

[`success/page.tsx:9-10`](../apps/customer-web/src/app/success/page.tsx#L9-L10) reads
`order_id` and `amount` from the query string rather than re-reading the created
order from the server. The total a guest sees is therefore editable in the address
bar (cosmetic only — no money moves), and more importantly the screen is terminal:
there is no "hazırlanıyor / hazır" status, so a guest who wants to know where their
waffle is has to ask a member of staff. The QR flow's job-to-be-done stops one step
short of finished.

### What is right about the customer flow

The QR gate itself is the strongest piece of code in the repository. Five explicit
phases (`loading` / `missing` / `invalid` / `unavailable` / `network`), each with
its own Turkish copy and the correct token-retention decision per phase; a
synchronous re-entrancy guard on submit that does not rely on React state
(`submittingRef`, line 336); and an idempotency key that is cleared on confirmed
success and *kept* on an uncertain outcome. Out-of-stock ingredients are shown
struck through with an alternative suggested rather than silently hidden — that is
a considered choice, not a default.

---

## 4. Kitchen live updates and history (kitchen-web, :3002)

**All runtime claims in this section: NEEDS_MANUAL_BROWSER_CONFIRMATION.**

### F-05 · The board never re-syncs after a dropped socket
**Lens:** CTO / PM · **Severity:** blocker · **Effort:** S ·
**Status: FIXED** on `fix/kitchen-live-resync`

> **Resolution.** The kitchen client's socket, reconnect and freshness rules now
> live in one framework-free controller,
> [`liveSync.ts`](../apps/kitchen-web/src/lib/liveSync.ts), bound to React by
> [`useKitchenLiveSync.ts`](../apps/kitchen-web/src/lib/useKitchenLiveSync.ts).
> `initial_state` is handled (it triggers a full refetch rather than a fragile
> partial hydrate); **every** socket open — first connect or reconnect —
> refetches orders and timing; a single 12-second interval polls whenever the
> link is not live and every 30 seconds even when it is; and waking the tablet or
> regaining the network resyncs immediately. No backend change was needed — the
> server already sent everything required.
>
> The badge is now derived from *when data last arrived*, not from what the
> socket claims: `live` requires an open socket **and** a successful refresh
> inside 60 seconds. The remaining states — `reconnecting`, `polling`, `stale`,
> `offline` — each say what is actually wrong, and "Yenile" is always visible
> rather than only while disconnected. Locked down by
> [`liveSync.test.ts`](../apps/kitchen-web/src/lib/liveSync.test.ts) (30 tests on
> a fake clock and a fake socket).
>
> The diagnosis below is kept as the record of what was wrong.

Two facts combine into a data-loss-shaped bug on the shop floor.

1. The server sends a full `initial_state` payload on every connection
   ([`ws.py:64-79`](../apps/api/app/routers/ws.py#L64-L79)).
2. The kitchen client's `onmessage` handles **only** `order_created` and
   `order_status_updated`
   ([`page.tsx:151-184`](../apps/kitchen-web/src/app/page.tsx#L151-L184)). The
   `initial_state` event is parsed and discarded.

The reconnect path is `ws.onclose → setTimeout(connectWS, 5000)`
([`page.tsx:186-190`](../apps/kitchen-web/src/app/page.tsx#L186-L190)) — it
reconnects the socket but never calls `loadOrders()` again, and there is no polling
interval anywhere in the file.

So: the kitchen tablet loses Wi-Fi for ninety seconds. Three orders are placed. The
socket reconnects, the badge turns green and says **Canlı**, and those three orders
are not on the board and will never appear until an unrelated *new* order arrives
and triggers a refetch. The screen looks healthy while it is lying. A kitchen
display that can silently drop tickets is not something to put in front of a paying
shop.

The "Yenile" button is rendered *only* when `connectionState !== 'connected'`
([`page.tsx:261`](../apps/kitchen-web/src/app/page.tsx#L261)) — precisely the state
the board is *not* in after a successful reconnect. The manual recovery is hidden
exactly when it is needed.

### F-08 · Timing cards and the tempo strip go stale between order creations
**Lens:** PM / Analyst · **Severity:** major · **Effort:** S ·
**Status: FIXED** on `fix/kitchen-live-resync`

> **Resolution.** The local-patch path is gone. `order_status_updated`,
> `order_created` and `initial_state` all funnel into the same coalesced refetch
> of `/kitchen/orders/` **and** `/kitchen/timing/orders` together, so tickets and
> delay badges can never come from different moments. The fallback poll refreshes
> both on the same cadence, so `delay_state` keeps advancing during a lull — the
> case where it mattered most. Concurrent events coalesce into at most one
> in-flight refetch plus one trailing refetch, so a rush cannot stampede the API.

`loadOrders()` fetches orders *and* timing together
([`page.tsx:113-135`](../apps/kitchen-web/src/app/page.tsx#L113-L135)) and is called
on mount and on `order_created`. An `order_status_updated` event takes the local-patch
path instead ([`page.tsx:160-179`](../apps/kitchen-web/src/app/page.tsx#L160-L179)),
which mutates `orders` but touches neither `timingById` nor `tempo`.

Consequence: "Mutfak temposu" and every per-card delay badge only advance when a
brand-new order arrives. During a lull — which is exactly when an order is most
likely to be forgotten — the delay warnings freeze. `delay_state` is computed
server-side against `now` (`kitchen_timing_service._delay_state`), so the numbers
are correct at fetch time and progressively wrong afterwards.

### F-06 · The kitchen cannot mark delivered, cannot cancel, and cannot undo
**Lens:** PM · **Severity:** major · **Effort:** M

The backend state machine
([`kitchen_service.py:45-63`](../apps/api/app/services/kitchen_service.py#L45-L63))
supports `NEW→IN_PREP→READY→DELIVERED`, cancellation from `NEW` and `IN_PREP`, and a
60-second backward undo window (`UNDO_TRANSITIONS`).

The kitchen UI exposes exactly one button with a two-step ladder
([`page.tsx:209-211`](../apps/kitchen-web/src/app/page.tsx#L209-L211)):

```ts
const nextStatus = currentStatus === "NEW" ? "IN_PREP" : "READY";
```

- **No DELIVERED.** A repo-wide search finds `READY: "DELIVERED"` only in
  [`owner-web/src/app/kitchen/page.tsx:56`](../apps/owner-web/src/app/kitchen/page.tsx#L56).
  Handing food to a customer is an owner/manager action performed on the owner
  console. In a real shop the owner is not standing at the pass, so orders will sit
  in `READY` — and the owner dashboard's `completed_today` (defined as orders in
  `DELIVERED`) will read near-zero regardless of how many waffles were sold.
- **No cancellation.** No frontend sends `CANCELLED`. A burnt or abandoned order
  has no path off the board except through the cashier's order-issue workflow,
  which is a *refund* mechanism, not a preparation-state mechanism.
- **No undo.** The 60-second undo window exists in the service and is reachable
  from no screen. A mis-tapped "Hazır ✓" is permanent from the kitchen's point of
  view, and it removes the card from the board (line 167), so the mistake is also
  invisible.

### F-07 · There is no kitchen history
**Lens:** PM · **Severity:** major · **Effort:** M

[`page.tsx:167`](../apps/kitchen-web/src/app/page.tsx#L167) drops any order reaching
`READY`, `DELIVERED` or `CANCELLED` from local state. The board is the live moment
and nothing else. A cook cannot answer "did we already make table 4's order?", a
shift lead cannot review what happened, and there is no end-of-shift recap. Since
`GET /kitchen/timing` and `order_status_events` already hold everything needed, this
is missing surface, not missing data.

### F-24 · Errors are delivered by `alert()`
**Lens:** UI · **Severity:** minor · **Effort:** S

[`page.tsx:222`](../apps/kitchen-web/src/app/page.tsx#L222) uses a blocking browser
modal for a failed status update. On a wall-mounted tablet with flour on the
operator's hands, a modal that must be dismissed before the board is usable again is
the wrong instrument. Every other surface in the repo uses inline Turkish status
copy; this is the one place that does not.

### F-26 · The reconnect timer outlives the component
**Lens:** CTO · **Severity:** minor · **Effort:** S ·
**Status: FIXED** on `fix/kitchen-live-resync`

> **Resolution.** `KitchenLiveSync.stop()` clears the reconnect timer and the
> poll interval, closes the socket with its handlers already detached (so a
> deliberate close is not reported back as a drop), and latches the controller
> inert — after `stop()` no timer fires, no socket callback lands, and no state
> is emitted, including from a fetch that was already in flight. Sockets carry an
> epoch, so a callback from a replaced socket cannot move the state either.

Cleanup closes the socket ([`page.tsx:201-205`](../apps/kitchen-web/src/app/page.tsx#L201-L205)),
which fires `onclose`, which schedules another `connectWS` five seconds later. On an
unmount or a fast-refresh cycle this leaves an orphaned reconnect chain. Harmless on
a kiosk that never navigates; noisy in development and a slow leak anywhere else.

---

## 5. Cashier real-use flow (cashier-web, :3004)

**All runtime claims in this section: NEEDS_MANUAL_BROWSER_CONFIRMATION.**

### F-09 · Partial payment is implemented in the backend and unreachable from the screen
**Lens:** PM · **Severity:** major · **Effort:** S

The API accepts a partial amount —
[`schemas/payment.py:75-84`](../apps/api/app/schemas/payment.py#L75-L84): *"Collect
payment against a single order. When `amount` is omitted the full [balance is
taken]"*. The typed client even declares it:
[`cashier-web/src/lib/api.ts:170-176`](../apps/cashier-web/src/lib/api.ts#L170-L176)
exposes `amount?: string | null`.

The screen never sends it. [`CashierPage.tsx:190`](../apps/cashier-web/src/components/CashierPage.tsx#L190):

```ts
const r = await payOrder(orderId, { payment_method: method }, key);
```

There is no amount field anywhere in the component. So "the customers want to split
this ₺240 bill three ways" — the single most common real cashier request in a
Turkish café — has no answer, despite the ledger being built to handle it. This is
the cheapest high-value fix in the audit: the capability exists, the wiring does not.

### F-10 · A refund is only possible on the settlement you just took, in this browser tab
**Lens:** PM · **Severity:** major · **Effort:** M

Refund controls live inside `<Receipt>`, which renders only when the in-memory
`receipt` state is set by a just-completed settlement
([`CashierPage.tsx:373-375`](../apps/cashier-web/src/components/CashierPage.tsx#L373-L375)).
`receipt` is not persisted and is cleared whenever another table is opened
([`CashierPage.tsx:110`](../apps/cashier-web/src/components/CashierPage.tsx#L110)).

A page reload, a shift change, or simply serving the next customer destroys the only
path to `refundAllocation`. The "İşlem geçmişi" list below is display-only — no row
is actionable. A customer returning ten minutes later with a complaint cannot be
refunded from the cashier screen at all; staff must route it through the order-issue
workflow (which is bounded and auditable, so the money stays correct) or through the
API directly. The mismatch between "we built a proper refund ledger" and "you can
only refund for the next thirty seconds" is a real-use gap, not a code defect.

### F-25 · Nothing on the cashier screen refreshes on its own
**Lens:** PM / UI · **Severity:** minor · **Effort:** S

`loadTables` and `loadRecent` run once on mount
([`CashierPage.tsx:103-106`](../apps/cashier-web/src/components/CashierPage.tsx#L103-L106))
and thereafter only after a successful payment. There is no interval and no socket.
"Açık masalar" is a snapshot from whenever the tab was opened; a table that ordered
five minutes ago is not there. During a rush the cashier's fallback is the search
box — which requires knowing the order code.

### F-28 · The refund form is a free-text box with no bound shown
**Lens:** UI · **Severity:** minor · **Effort:** S

[`CashierPage.tsx:490-501`](../apps/cashier-web/src/components/CashierPage.tsx#L490-L501)
renders the refund amount as a plain `<input>` with no `type="number"`, no
`inputMode="decimal"`, and no display of the maximum refundable amount. The server
enforces the bound correctly and returns a clear Turkish message
(`refund_over_balance` → *"Bu tahsilatın iade edilebilir bakiyesi kalmadı."*), so no
money is at risk — but the cashier discovers the limit by being rejected, and on a
tablet they get an alphabetic keyboard for a numeric field.

### What is right about the cashier flow

The error taxonomy is genuinely well thought through
([`CashierPage.tsx:46-65`](../apps/cashier-web/src/components/CashierPage.tsx#L46-L65)):
a deterministic rejection releases the idempotency attempt so the cashier can adjust
and retry, while a network-uncertain outcome keeps the key and tells the operator in
plain Turkish that retrying is safe. A double-click is blocked before React state
updates. Shift open/close never blocks collection — payments work without an open
shift, with a soft amber warning instead of a hard gate. That is the correct
priority for a cash desk.

---

## 6. Owner reporting and dashboard gaps (owner-web, :3003)

This is where the product's story and its screens diverge most.

### F-03 · Two different definitions of "revenue" render on the same page
**Lens:** Analyst · **Severity:** blocker · **Effort:** M

The owner landing page stacks the new operational dashboard and the legacy analytics
components in one scroll
([`owner-web/src/app/page.tsx:151-210`](../apps/owner-web/src/app/page.tsx#L151-L210)):
Zone 0 `OperationalDashboardPanel`, Zone 1 `KPICardGrid` + `MainAnalyticsChart`,
Zone 4 `HourlyDemandChart` + `IngredientForecastPanel`.

They do not agree:

| | Zone 0 · Günlük ciro | Zone 1 · KPI `gross_revenue` |
| --- | --- | --- |
| Source | payment ledger — Σ completed allocations, minus refunds | `SUM(orders.total_amount)` |
| Cancelled orders | excluded (never settled) | **included** |
| Unpaid orders | excluded, shown separately as *money owed* | **counted as revenue** |
| Refunds | subtracted | **ignored** |
| Defined in | `docs/OWNER_OPERATIONAL_DASHBOARD.md` §4 | nowhere |

Evidence for the legacy definition:
[`owner_analytics_service.py:32-40`](../apps/api/app/services/owner_analytics_service.py#L32-L40)
— `SELECT COUNT(id), COALESCE(SUM(total_amount),0), COALESCE(AVG(total_amount),0)
FROM orders WHERE created_at >= :today_start AND store_id = :store_id`, with no
status filter and no reference to the payment tables.

The dashboard doc explicitly forbids this: *"The dashboard does not use an order's
`total_amount` as money collected, does not count unpaid orders as revenue."* It
keeps that promise; the component two zones below it does not. The database
currently holds **8 CANCELLED orders out of 24** (§10) — a third of all orders would
be counted as revenue by the KPI card and correctly excluded by the panel above it.

An owner who sees two different "today's revenue" figures on one screen stops
trusting both. This is the finding that most directly blocks charging money for
reporting.

### F-04 · Every day boundary and every hour bucket is UTC; the shop is in Istanbul
**Lens:** Analyst / DataEng · **Severity:** blocker · **Effort:** M ·
**Status: FIXED** on `fix/business-timezone`

> **Resolution.** A single business-day definition now lives in
> [`app/core/business_time.py`](../apps/api/app/core/business_time.py), configured by
> `BUSINESS_TIMEZONE` (default `Europe/Istanbul`). Storage is unchanged — every
> column is still `timestamptz` holding a UTC instant — but every reporting window,
> daily bucket and hour bucket is now local: the owner dashboard, owner analytics
> (KPIs, `peak_hour`, Saatlik Talep, daily sales, ingredient forecast windows), the
> measurement layer (`metrics_service`), the kitchen timing summary, owner insights
> and the `/owner/metrics` future-date guard. Day windows are half-open UTC intervals
> `[start, end)` derived from the local day; hour and day *groupings* use
> `AT TIME ZONE`. Locked down by `apps/api/tests/test_business_timezone.py`.
> The four consequences below are what the fix removes; they are kept here as the
> record of why the change was made.

No timezone configuration exists anywhere in the repository — no `TZ`, no
`Europe/Istanbul`, no zone column. "Today" is computed as the UTC calendar day in at
least four independent places:

- [`owner_analytics_service.py:28`](../apps/api/app/services/owner_analytics_service.py#L28)
  — `now.replace(hour=0, ...)` on a UTC `now`.
- [`kitchen_timing_service.py:387-388`](../apps/api/app/services/kitchen_timing_service.py#L387-L388)
  — `today = datetime.now(timezone.utc).date()`, documented as UTC.
- `operational_dashboard_service.py` — `func.date(col) == today`, documented as UTC
  in `docs/OWNER_OPERATIONAL_DASHBOARD.md` §3.
- [`owner_metrics.py:108`](../apps/api/app/routers/owner_metrics.py#L108) — future-date
  validation against `datetime.now(timezone.utc).date()`.

Istanbul is UTC+3 year-round. Consequences a shop will actually hit:

1. **The business day ends at 03:00 local.** Everything sold between midnight and
   03:00 — normal hours for a dessert shop — lands on the *previous* day's report.
   The owner's "dün ne sattık?" and the till's cash never match.
2. **"Saatlik Talep" is shifted three hours.** `EXTRACT(HOUR FROM created_at)` on a
   UTC timestamp, labelled `"18:00"`
   ([`owner_analytics_service.py:149`](../apps/api/app/services/owner_analytics_service.py#L149)),
   is 21:00 in the shop. The chart's own caption — *"En yoğun saatler turuncu
   gösterilir"* — points at the wrong hours, and `peak_hour` in the KPI block is
   wrong by the same three hours.
3. **Staffing and prep decisions made from that chart are made against the wrong
   clock.** This is the one analytics defect that causes a shop to lose money by
   acting on the number.
4. **Forecasting inherits it.** Any future hourly or daily model built on these
   buckets learns a three-hour-displaced demand curve. See §8.

To be fair to the code: the UTC choice is *consistent* and *documented* — the
dashboard doc names it, and every module uses the same boundary, so the figures agree
with each other. That is why this is a definition problem to be decided once, not a
scattering of bugs. But no amount of consistency makes 03:00 the end of a waffle
shop's day.

### F-11 · The "30 Gün" button shows 7 days of data
**Lens:** Analyst · **Severity:** major · **Effort:** S

[`owner_analytics_service.py:158`](../apps/api/app/services/owner_analytics_service.py#L158)
hard-codes the window:

```python
seven_days_ago = get_current_utc() - timedelta(days=7)
```

`MainAnalyticsChart` offers **Bugün / 7 Gün / 30 Gün** tabs
([`MainAnalyticsChart.tsx:37-41`](../apps/owner-web/src/components/MainAnalyticsChart.tsx#L37-L41))
and implements 30 days as `points.slice(-30)` over a 7-element array
([`MainAnalyticsChart.tsx:191-198`](../apps/owner-web/src/components/MainAnalyticsChart.tsx#L191-L198)).
Selecting "30 Gün" therefore renders the identical 7 points with an identical
average line, under a label promising a month. There is no "insufficient history"
notice. An owner comparing "this month" against "this week" is comparing a series
with itself.

### F-12 · The "Bugün" tab silently substitutes a different metric, with the wrong axis unit
**Lens:** Analyst · **Severity:** major · **Effort:** S

[`MainAnalyticsChart.tsx:207-214`](../apps/owner-web/src/components/MainAnalyticsChart.tsx#L207-L214):

```ts
const chartData = isToday
  ? hourlyPoints.map((p) => ({ label: p.hour_bucket, value: p.order_count }))
  : dailyPoints.map(...)
```

With **Ciro** selected and **Bugün** clicked, the chart plots hourly *order counts*.
The italic caption admits it, but `cfg` is still `METRIC_CONFIG.revenue`, so:

- the Y axis renders `tickFormatter={(v) => \`₺${v}\`}` — order counts labelled as
  lira ([`MainAnalyticsChart.tsx:307`](../apps/owner-web/src/components/MainAnalyticsChart.tsx#L307));
- the reference line reads **"Ortalama: ₺7"** when the shop averaged 7 orders an
  hour ([`MainAnalyticsChart.tsx:322`](../apps/owner-web/src/components/MainAnalyticsChart.tsx#L322)).

A currency symbol on a count is not a caption problem. It is a wrong number on a
screen an owner makes decisions from.

### F-29 · The average reference line straddles a partial day
**Lens:** Analyst · **Severity:** minor · **Effort:** S

The "Ortalama" line averages every point in the window
([`MainAnalyticsChart.tsx:217-219`](../apps/owner-web/src/components/MainAnalyticsChart.tsx#L217-L219)),
including today — which is incomplete by construction. At 11:00 the current bar is a
third of a day and drags the mean down, so yesterday looks like an outperformance.
No point is marked partial and there is no "so far today" annotation. Same issue in
the daily series generally: `fetch_daily_sales` groups `DATE(created_at)` over
`created_at >= now - 7 days`, so the oldest bucket is also a partial day (from
whatever time of day it is now, seven days ago).

### F-14 · An owner account with no store silently sees all zeros
**Lens:** DataEng / CTO · **Severity:** major · **Effort:** S

[`owner_analytics.py:47`](../apps/api/app/routers/owner_analytics.py#L47) and its
siblings pass `staff.store_id` straight through:

```python
return service.fetch_kpis(db, staff.store_id)
```

When `store_id` is `NULL`, `WHERE store_id = :store_id` matches no rows, and the
owner gets a fully rendered dashboard reading `0 ₺`, `0 sipariş`, empty charts. The
same router already knows the honest answer — `_require_store`
([`owner_analytics.py:23-39`](../apps/api/app/routers/owner_analytics.py#L23-L39))
returns a 403 with a Turkish explanation, with an excellent docstring about why
inventing a chain-wide total would be wrong — but it is applied only to
`/stock-status`. Everywhere else, "you have no branch assigned" is rendered as "your
branch sold nothing today". Those must never look the same.

The same latent shape exists on the kitchen socket: `store_id = user.store_id`
([`ws.py:60`](../apps/api/app/routers/ws.py#L60)) registers the connection under a
`None` key, which no broadcast will ever match — a silently dead live feed rather
than a refusal.

### Reports the owner still cannot run

The operational dashboard is explicitly and correctly a *today* aggregate. Everything
below is missing surface. Each is named as a report, with the question it answers and
its grain — this is a naming exercise, not a design:

| Report | Question | Grain | Data exists? |
| --- | --- | --- | --- |
| **Günlük kapanış (daily close)** | "What did we sell, collect, refund and owe yesterday?" | store × day | Yes — ledger + orders |
| **Dün vs bugün** | "Are we ahead or behind the same point yesterday / last week's same weekday?" | store × day, partial-day aware | Yes |
| **Haftalık özet** | "Which days carry the week? Which weekday is weakest?" | store × ISO week | Yes |
| **Aylık özet** | "Is the month tracking to last month?" | store × month | Yes |
| **Ürün performansı** | "Which waffle actually earns? What sells and what sits?" | store × product × day | **No** — orders have no per-product revenue attribution surfaced, and F-01/F-02 mean only one product is ever ordered |
| **Malzeme tüketimi ve fire** | "What did we consume vs waste vs count-adjust?" | store × ingredient × day | Yes — the inventory ledger has all movement types |
| **Vardiya karşılaştırma** | "Which shift closes short? Which cashier is fast?" | store × shift × cashier | Yes — `cashier_shifts` freezes discrepancy at close |
| **Sorunlu sipariş trendi** | "Are complaints rising? Which type? Which product?" | store × issue_type × week | Yes — `order_issues` |
| **Mutfak SLA trendi** | "Is prep time drifting? At which hours?" | store × day × hour | Yes — `order_status_events` |
| **Marj / kârlılık** | "What do we make per waffle after ingredients?" | store × product | **No** — no ingredient cost price anywhere in the schema |

Nine of the ten questions are answerable from data already captured. Two are blocked
on product decisions (per-product ordering, ingredient cost), not on engineering.

---

## 7. Analytics chart quality

Audited per the checklist: is the definition stated, the time zone explicit, the
partial day handled, the axis honest, the comparison fair, the empty state useful?

| Chart / tile | Definition stated | Time zone | Partial day | Axis honest | Empty state | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Zone 0 · Operasyon Özeti cards | **Yes** — `OWNER_OPERATIONAL_DASHBOARD.md` §3 | Business local, documented (F-04 fixed) | N/A (today only, by design) | N/A | **Good** — `—` for no data, never a fake 0 | **Trustworthy** |
| Zone 0 · Dikkat gerektirenler | Yes — §9 rule table | N/A | N/A | N/A | Good Turkish empty line | **Trustworthy** |
| KPI · Günlük ciro (`gross_revenue`) | **No** | Business local (F-04 fixed) | No | N/A | Unknown | **F-03 — conflicts with Zone 0** |
| KPI · `peak_hour` | No | **Local hour** (F-04 fixed) | No | N/A | `null` handled | **Correct** |
| Ana grafik · Ciro/Sipariş/Sepet | No | Business local (F-04 fixed) | **No** — F-29 | **No** — F-12 (₺ on counts) | *"Yeterli veri yok."* — fine | **Not trustworthy** |
| Ana grafik · 30 Gün range | No | Business local (F-04 fixed) | No | **No** — F-11, still shows 7 days | — | **Misleading** — F-11 open |
| Ana grafik · Malzeme Kullanımı | Partly — "anlık görünüm" | None (all-time) | N/A | Share % in tooltip only | — | **Weak** — an all-time cumulative count labelled a snapshot, with no window |
| Saatlik Talep | No | **Local buckets, local labels** (F-04 fixed) | Today only | Counts, honest | *"Henüz saatlik veri yok."* — good | **Correct** |
| Malzeme Tahmini (`IngredientForecastPanel`) | Partly — `baseline_method` is on the wire | Business local (F-04 fixed) | No | — | — | **See §8 / F-17** |
| Mutfak temposu (kitchen strip) | Yes — `KITCHEN_PREP_TIMING_METRICS.md` | Business local (F-04 fixed) | Yes — `None` not `0` | Counts | Renders nothing when null | **Trustworthy** (F-08 staleness fixed) |

The pattern is clean: **everything built in the operational-dashboard and
kitchen-timing era is defined, documented and honest. Everything inherited from the
legacy analytics era is undefined and, in three places, wrong.** The two eras are
rendered side by side on one page.

### F-30 · `TopIngredients` has no time window at all
**Lens:** Analyst · **Severity:** minor · **Effort:** S

[`owner_analytics_service.py:92-117`](../apps/api/app/services/owner_analytics_service.py#L92-L117)
aggregates `order_item_ingredients` for the store with **no date filter** — it is an
all-time cumulative count, rendered under a caption that says *"anlık görünüm"*
(snapshot). As history accumulates this converges to a constant and stops carrying
information, while continuing to look like a current-state chart. It also counts
*rows*, not quantities (see F-17).

---

## 8. Forecasting readiness

Per the skill's boundary: **no forecasting is designed here.** This section only
answers whether the data would support it. Design work belongs to the
`forecasting-analytics-architect` skill, on its own branch, when the user asks for it.

### Verdict: **NOT YET READY — blocked on four specific things, in this order**

**1. There is no history. (blocking, and it is a matter of calendar time)**

Read-only SQL against the live database, 2026-07-23:

```
 store_id | orders | first_day  |  last_day  | distinct_days
----------+--------+------------+------------+---------------
        1 |      9 | 2026-07-15 | 2026-07-16 |             2
    12722 |     15 | 2026-07-23 | 2026-07-23 |             1
```

Twenty-four orders. Two distinct days in one store, one day in the other. Store
12723 has no orders at all. Of those 24, **8 are CANCELLED**. There is no seasonality
signal, no weekday effect, no holiday behaviour, and nothing to backtest against.
Any model fitted here would be fitting the demo seed. `docs/PROJECT_ROADMAP.md` is
right to defer, and the deferral reasoning is correct.

**2. The day and hour boundaries are wrong for the market. (blocking, fixable)**

F-04. A model trained on UTC buckets learns a demand curve displaced three hours from
the shop's reality, and its daily totals split the late-evening trade across two
dates. Fixing the timezone *before* accumulating history is far cheaper than
re-deriving it afterwards.

**3. Consumption is counted as rows, not quantities. (blocking, fixable)**

[`owner_analytics_service.py:200-201`](../apps/api/app/services/owner_analytics_service.py#L200-L201)
— `COUNT(CASE WHEN ... THEN oi.id END)` over `order_item_ingredients`. That counts
*how many times an ingredient appeared on a line*, ignoring `quantity` on both the
order item and the ingredient line. `order_item_ingredient` carries a quantity, and
`order_service.calculate_consumed_quantity` already does the real arithmetic for the
inventory ledger. A demand forecast that predicts "Nutella will appear on 14 lines
tomorrow" cannot be converted into "order 3 kg" without that quantity. The correct
consumption signal already exists in the inventory movement ledger and is not the one
the forecast reads.

**4. Cancellations are not excluded from demand. (fixable, small)**

Neither `fetch_ingredient_forecast` nor `fetch_daily_sales` nor `fetch_top_ingredients`
filters on order status. A cancelled order's ingredients count as demand. With a third
of current orders cancelled, that is not a rounding error. Note the contrast:
`kitchen_timing_service` handles this correctly and deliberately — a cancelled order
has no READY event, so it cannot inflate prep averages, and the docstring says so.

### What is already in place, and is genuinely useful

- **Timestamps are complete and append-only.** `order_status_events` records every
  transition with a server timestamp; `kitchen_timing_service` reduces it to first-entry
  times per state. The reconciler confirms internal consistency across all stores.
- **Recipe mapping exists.** `order_item_ingredients` links orders to ingredients with
  quantities, and `order_service.calculate_consumed_quantity` converts to stock units.
- **The inventory ledger is complete and reconciled** — receipts, consumption, waste,
  adjustments, transfers, counts. This is the *right* input for ingredient forecasting
  and is currently unused by the forecast code.
- **Store scoping is enforced on every operational path.**

### F-17 · The shipped "forecast" is a 7-day mean presented as a prediction
**Lens:** Analyst · **Severity:** major (as *presentation*) · **Effort:** S to relabel

[`owner_analytics_service.py:187-267`](../apps/api/app/services/owner_analytics_service.py#L187-L267).
The code is candid — `predicted = avg_daily * 1.0  # just use avg as prediction for
MVP` — and it correctly exposes `baseline_method: "7d_moving_avg"` and a
`confidence_level` on the wire. But `IngredientForecastPanel` sits in the owner page's
**Analiz** zone under the subtitle *"Talep örüntüsü · tahmin · malzeme dağılımı"*,
and the response declares `forecast_horizon_days: 7` while every horizon day carries
the same flat number. `confidence_level` is derived purely from row count
(`>=10 → high`), not from any error measure — so an ingredient with wildly volatile
usage reads "high confidence" as long as it appears often.

A flat mean is a perfectly respectable *baseline*. It should be labelled one. Right
now the product's own roadmap says forecasting is deferred while the owner's screen
shows a panel called "tahmin".

### F-18 · The legacy dbt marts are not store-scoped
**Lens:** DataEng · **Severity:** major (if ever run) · **Effort:** M

Across `data/dbt/models/`, `store_id` appears in exactly **two** files:
`staging/stg_orders.sql` and `marts/core/fact_orders.sql`. It appears in none of
`marts/owner/agg_daily_sales.sql`, `agg_hourly_orders.sql`,
`agg_daily_ingredient_demand.sql`, `agg_top_ingredients.sql`, nor in either forecast
model. `forecast_ingredient_daily_baseline.sql` cross-joins a 7-day horizon onto
trend signals that carry no store dimension — running it against a multi-store
database silently blends branches into one number.

The project is classed as legacy (`PRODUCTION_READINESS.md` §14.10) and is not on the
API's path, so nothing is wrong *today*. But it is the natural starting point someone
would reach for when forecasting work begins, and it would be the wrong starting
point. It should be either store-scoped as a deliberate first step of that work, or
removed.

### The honest sequence

Fix the day boundary (F-04) and the quantity/cancellation semantics (points 3 and 4)
**first**, then let a real pilot accumulate 8–12 weeks of trustworthy history, then
hand the design to `forecasting-analytics-architect`. Doing it in the other order
produces a model that is confidently wrong and, worse, a model whose errors are
indistinguishable from data-collection errors.

---

## 9. UI and theme quality (first pass)

Per the skill boundary this is a first-pass read; depth belongs to the
`ui-theme-review` skill. **Every visual claim here: NEEDS_MANUAL_BROWSER_CONFIRMATION.**

### F-15 · The dark-mode inheritance is a contrast hazard on kitchen and owner surfaces
**Lens:** UI · **Severity:** major · **Effort:** S · **NEEDS_MANUAL_BROWSER_CONFIRMATION**

Three facts that only combine on a device set to dark mode:

1. `kitchen-web` and `owner-web` `globals.css` both flip `--foreground` to `#ededed`
   under `@media (prefers-color-scheme: dark)`, and `body { color: var(--foreground) }`.
2. Every component in those apps is written in light-mode Tailwind utilities —
   `bg-white`, `bg-gray-100`, `text-gray-900` — with no `dark:` variants anywhere.
3. `kitchen-web` additionally hard-codes `<html lang="en" className="dark">`
   ([`kitchen-web/src/app/layout.tsx:18`](../apps/kitchen-web/src/app/layout.tsx#L18)).

Any element that does **not** carry an explicit Tailwind text colour inherits
`#ededed` — near-white — while sitting on an explicitly `bg-white` card. Concrete
instance: the kitchen order number,
[`kitchen-web/src/app/page.tsx:298`](../apps/kitchen-web/src/app/page.tsx#L298):

```tsx
<span className="text-xl font-bold font-mono">#{order.id}</span>
```

No colour class. On a dark-mode tablet this is white text on a white card — the
single most important identifier on the ticket. `cashier-web`'s `globals.css` has no
dark block at all (200 bytes vs 488) and is therefore immune, which is itself the
inconsistency: four surfaces, three different theme strategies.

This needs to be confirmed in a browser with the OS in dark mode before it is
actioned — but the CSS cascade says it will happen, and a kitchen tablet is exactly
the device most likely to be left on whatever theme it shipped with.

### F-31 · Two of four apps declare the wrong document language
**Lens:** UI · **Severity:** minor · **Effort:** S

`cashier-web` and `customer-web` correctly declare `<html lang="tr">`.
`kitchen-web` and `owner-web` declare `lang="en"` while rendering 100% Turkish copy.
That misinforms screen readers (Turkish read with English phonemes), breaks
hyphenation, and prompts browsers to offer a translation of text that is already in
the user's language. `docs/TURKISH_USER_FACING_LOCALIZATION.md` documents the copy
scope thoroughly; the document language was missed.

### F-20 · The shared UI package is dead, and its one exported badge speaks English
**Lens:** UI / CTO · **Severity:** minor · **Effort:** S

`packages/ui` exports `Button`, `Card` and `StatusBadge`. A repo-wide search finds
`@sweetops/ui` in exactly one place: the `dependencies` block of
`apps/customer-web/package.json`. **No source file imports it.** Every surface has
independently re-implemented buttons, cards and badges, which is why the four apps
look like four products.

Worse, `StatusBadge` renders `{status}` — the raw wire enum
([`packages/ui/src/index.tsx`](../packages/ui/src/index.tsx)) — so the one shared
component would print `IN_PREP` on a Turkish screen. Meanwhile each app maintains its
own well-tested `labels.ts` that does this correctly. The shared package is not just
unused; adopting it as-is would regress the localization guarantee.

### Cross-surface inconsistency (first-pass list)

| | customer | kitchen | cashier | owner |
| --- | --- | --- | --- | --- |
| `<html lang>` | `tr` ✅ | `en` ❌ | `tr` ✅ | `en` ❌ |
| Dark-mode block in `globals.css` | custom vars | yes ⚠️ | **none** | yes ⚠️ |
| Accent colour | amber | amber/green/blue | indigo/slate | amber/blue/indigo mixed |
| Login screen | n/a | own `AuthGate` | own `AuthGate` | own `AuthGate` |
| Error presentation | inline toast | **`alert()`** ❌ | inline status line | inline panel |
| Auth gating | n/a | per-page | per-page | layout-level ✅ |
| Font | custom | Geist + `font-family: Arial` on body | Arial | Geist + `font-family: Arial` on body |

Note the last row: all four `globals.css` files set `body { font-family: Arial,
Helvetica, sans-serif }`, which overrides the Geist fonts that `layout.tsx` loads
and injects as CSS variables. Two apps pay the cost of `next/font` and then render
Arial. **NEEDS_MANUAL_BROWSER_CONFIRMATION.**

Three near-identical `AuthGate` login screens exist (kitchen, cashier, owner) with
independently written markup and copy — *"İşletme Girişi"* in owner-web, differing
elsewhere. Focus rings are present on the login inputs (`focus:ring-2
focus:ring-indigo-500`) but absent on most operational controls; the kitchen's
primary action button is the exception and does it properly.

### Tablet / real-conditions read

- **Kitchen**: card grid goes to 4 columns at `xl`; the primary button is a
  full-width `py-3` target — good for gloves. But the timing block is `text-xs` /
  `text-[10px]` (lines 79–86), which is small for a wall-mounted screen read from a
  metre away, and the only error channel is a blocking `alert()` (F-24).
- **Cashier**: the bill is a 7-column `<table>` inside a `md:grid-cols-2` column, with
  `text-xs` action links (`Tahsilat Al`, `Sorun`) as the tap targets. On a tablet in
  portrait during a rush, those are small, adjacent, and one of them opens a refund
  path. **NEEDS_MANUAL_BROWSER_CONFIRMATION.**
- **Customer**: genuinely good — `max-w-md`, sticky bottom bar, `active:scale`
  feedback, one-handed reach, honest out-of-stock treatment.
- **Owner**: dense but organised into labelled zones with accent bars. The header
  packs five links plus a live dot plus a refresh button into a 56px bar — likely to
  crowd below `lg`.

---

## 10. Data-quality risks

### Verified state of the live database (read-only SQL, 2026-07-23)

```
stores:    3   (1 SweetOps Waffle, 12722 SweetOps Demo - Kadıköy, 12723 SweetOps Demo - Moda)
orders:   24   (8 CANCELLED, 8 DELIVERED, 5 READY, 2 NEW, 1 IN_PREP)
products: 14   (1 legacy "Waffle", 8 TestWaffle_<hex> debris, 5 demo-seed products)
ingredients: 29 · order_item_ingredients: 28 · payment_settlements: 11 · cashier_shifts: 3
```

All four reconcilers pass against exactly this state. That is the strongest single
signal in the audit: the ledgers are correct even with test debris, cancellations, a
discrepant shift and cross-store demo data resident.

### F-23 · Test debris reaches a customer-facing table
**Lens:** DataEng · **Severity:** major · **Effort:** S ·
**Status: FIXED (reachability)** — `fix/customer-menu-scope-and-selection`

> Debris can no longer *reach* a guest: a `products` row is customer-facing only
> through a `store_products` publication row, and a test run never writes one
> (F-02). The specific leak was also closed at source — the local helper in
> `test_order_quantity_accounting.py` that minted `TestWaffle_<hex>` and cleaned
> up only the product now delegates to a shared conftest helper that publishes
> the product and withdraws it again, so an interrupted run leaves at worst an
> unpublished row.
> **Still true:** the eight existing debris rows are still resident in the
> development database. They are now inert — unpublished, invisible on every
> menu, unorderable — and removing them is a database chore, not a code change.

`PRODUCTION_READINESS.md` §14.8 says interrupted test runs leave "orphaned
`user_<hex>` rows and orders behind. They are harmless but they accumulate." The live
database shows the debris also lands in **`products`** — 8 `TestWaffle_<hex>` rows at
₺100.00 — and `products` is read unfiltered by the public menu endpoint (F-02). The
documented assessment of "harmless" holds only because F-01 currently renders just
one product. Fix F-01 without fixing F-02 and the debris becomes visible to guests.

Demo store ids are 12722/12723 rather than 2/3, which suggests the sequence has been
advanced by repeated test-fixture creation and rollback. Cosmetic, but it is a
fingerprint of the same shared-database problem.

### F-32 · Nothing distinguishes demo data from real data at query time
**Lens:** DataEng · **Severity:** major · **Effort:** M

The seed is demo-*scoped* by store — a well-chosen boundary that keeps store 1
untouched — but there is no `is_demo` flag on any table and no store-level marker. A
future chain-wide report, a dbt run, or an accounting export has no way to exclude
demo activity except by hard-coding store ids. Right now demo stores hold **15 of 24
orders**. Since the reports that would trip over this do not exist yet (§6), this is
a trap being set rather than one being sprung.

### F-33 · Clock assumptions are implicit and single-sourced
**Lens:** DataEng · **Severity:** minor (today) · **Effort:** S

Every timestamp comes from the API process clock (`datetime.now(timezone.utc)`) or
the database `server_default=func.now()` — a mix. Both are UTC and both are correct
on one host, so nothing is wrong today. But `order_status_events` timing is derived
from application-clock rows compared against database-clock rows, and there is no NTP
or drift assumption written down anywhere. `PRODUCTION_READINESS.md` §14.12 already
notes single-node assumptions; clock sourcing belongs in that list too.

### F-34 · Cancellation semantics differ between subsystems, and only one is documented
**Lens:** DataEng / Analyst · **Severity:** major · **Effort:** S

- `kitchen_timing_service` — a cancelled order has no READY event, so it cannot
  produce a prep duration. **Documented and deliberate.**
- `kitchen_service` — cancellation releases outstanding reservations and never
  restores already-consumed stock. **Documented and deliberate.**
- `operational_dashboard_service` — `cancelled_today` is its own counter, excluded
  from money. **Documented.**
- `owner_analytics_service` — cancelled orders are **included** in `gross_revenue`,
  `total_orders`, `average_order_value`, hourly demand, daily sales, top ingredients
  and the ingredient forecast. **Undocumented, and almost certainly unintended.**

One codebase, two answers to "does a cancelled order count?", split cleanly along the
new-code/legacy-code line.

### What no reconciler covers

The four reconcilers verify *internal* consistency — ledger vs summary, event vs
event. Nothing verifies:

- that the analytics layer's definitions agree with the ledger's (F-03 would be
  caught by a reconciler that compared `SUM(orders.total_amount)` to collected
  allocations);
- that `products`/`ingredients` contain no test debris (F-23);
- that an order in `READY` for six hours is anomalous (no staleness check — and with
  no UI path to `DELIVERED`, this will be the steady state; see F-06);
- that stores hold consistent catalogs (there is only one catalog, F-02).

---

## 11. Commercial readiness

### F-13 · There is no way to onboard a shop
**Lens:** PM / CTO · **Severity:** blocker · **Effort:** L ·
**Status: PARTIALLY ADDRESSED** — `feat/store-setup-and-menu-provisioning`

> **Update (v1 store setup).** An authenticated, role-gated, store-scoped setup
> surface now exists: `/setup` in owner-web over `/owner/setup/status`,
> `/owner/menu/*` and `/owner/tables/*`. An OWNER/MANAGER can create and edit
> products, publish/withdraw them from **their own branch's** menu, mark an item
> sold out for the day, set menu order, add and rename tables, and issue or rotate a
> table's QR sticker — none of which required a developer to be reachable before. A
> readiness checklist explains why the customer menu is empty, which is the specific
> support call the fail-closed menu would otherwise generate. No migration, no new
> dependency; 35 backend + 48 owner-web tests. See
> [STORE_SETUP_AND_MENU_PROVISIONING.md](STORE_SETUP_AND_MENU_PROVISIONING.md).
>
> **The finding is not closed.** Creating a *store*, creating a staff account or
> resetting a password, authoring an ingredient/recipe, setting a per-store price,
> and printing a QR sheet all still require a script on the database host. The
> severity stays a blocker for full self-service onboarding; what changed is that the
> day-to-day acts a shop performs constantly — menu and tables — no longer need the
> vendor.

This is the finding that decides whether SweetOps can be sold, and it is worth
stating flatly: **a new customer cannot be set up without a developer editing Python
and running shell commands on the database host.**

| To create a… | Supported path today | After v1 store setup |
| --- | --- | --- |
| Store | **None.** `Store(...)` is constructed only in `apps/api/seed.py`, `scripts/seed_demo_data.py`, a migration, and test fixtures | unchanged |
| Table | **None** outside those same scripts | **owner-web `/setup`** (add, rename) |
| Product / price | **None** — no admin endpoint, no owner UI, no CLI | **owner-web `/setup`** (chain catalog + per-branch publication); per-*store* price still none (P1-B) |
| Menu (what a branch sells) | **None** outside `seed_demo_data.py` | **owner-web `/setup`** (publish / withdraw / sold-out / order) |
| Ingredient / recipe | **None** outside the seeds | unchanged |
| Staff account | `python scripts/manage_staff_users.py create --username … --role … --store-id …` | unchanged |
| QR token for a table | `python scripts/manage_qr_tokens.py issue --table-id 5` | **owner-web `/setup`** (issue + rotate; the CLI still owns revoke-without-replacement) |

The two CLIs that do exist are well built — `getpass` so passwords never reach shell
history, raw QR tokens printed exactly once and never recoverable, destructive
operations keyed on the primary key rather than a display prefix. Their own docstring
is honest: *"This is the ONLY supported way to create or modify staff accounts until
an authenticated staff-management UI exists."*

But that leaves the shop owner unable to add a product, change a price, add a table,
or reset a cashier's password without calling the vendor. For a pilot with a
hands-on founder that is survivable. For a second customer it is not, and it is the
single largest piece of unbuilt work in this audit.

### The rest of the commercial gap

Most of this is already stated honestly in `PRODUCTION_READINESS.md` §14 and is
repeated here only for completeness with a commercial reading:

| Area | State | Commercial consequence |
| --- | --- | --- |
| Hosting / TLS / domain | None — localhost Compose only (§14.1) | Nothing to sell access to |
| CI | None (§14.2) | Every release is a manual human sequence |
| Monitoring / alerting / logging | None (§14.3) | Nobody learns the shop is down before the shop calls |
| Backup / restore | None (§14.3) | **A disk failure loses the shop's takings.** For a system of record handling money, this is the second blocker after F-13 |
| Secret management | Local `.env` (§14.4) | Fails the lightest customer security review |
| Rate limiting | Account lockout only (§14.5) | `/auth/login` is exposed to credential stuffing |
| Multi-store admin | Store scoping is enforced, but there is no chain-level console | A two-branch owner logs in twice and compares by hand |
| Receipt / fiscal compliance | None — no printed receipt, no fiscal integration | Turkish retail food service has receipt obligations; the cashier surface produces an on-screen receipt only, and it disappears on reload (F-10) |
| Support path | None | No error reference, no diagnostic bundle, no in-app "something is wrong" |
| Load / capacity | Untested (§14.11) | No basis for a per-store price or an SLA |
| Multi-instance | WebSocket state is in process memory (§14.12) | Cannot scale past one API node without a broker |

### What is commercially strong

Store-scoped authorization derived from the session, signed QR tokens, an auditable
money ledger with bounded refunds, shift close with a frozen discrepancy snapshot,
full Turkish UX, four read-only reconcilers, and a genuinely honest readiness
document. That is a credible foundation. The gap is not in what was built — it is
that a shop cannot be *set up*, its data cannot be *backed up*, and its owner cannot
answer *"how was last week?"*.

---

## 12. Findings index

Ordered by severity, then by how directly they block a paying pilot.

| ID | Lens | Finding | Sev | Eff | § |
| --- | --- | --- | --- | --- | --- |
| **F-13** | PM/CTO | No way to onboard a store, catalog, table or price without editing Python — **PARTLY FIXED** on `feat/store-setup-and-menu-provisioning` (menu, tables and table QR are now owner-web actions; store creation, staff accounts, recipes, per-store prices and a printable QR sheet are not) | blocker | L | 11 |
| **F-01** | PM | ~~Customer can order only `products[0]`, quantity 1~~ **PARTLY FIXED** — `fix/customer-menu-scope-and-selection` (explicit product choice + bounded quantity; multi-item cart still open) | blocker | M | 3 |
| **F-03** | Analyst | Two conflicting "revenue" definitions on one owner page | blocker | M | 6 |
| **F-04** | Analyst/DataEng | ~~Every day boundary and hour bucket is UTC; the shop is UTC+3~~ **FIXED** — `fix/business-timezone` | blocker | M | 6 |
| **F-05** | CTO/PM | ~~Kitchen board never re-syncs after a dropped socket; shows "Canlı" while stale~~ **FIXED** — `fix/kitchen-live-resync` | blocker | S | 4 |
| **F-02** | DataEng/CTO | ~~Product catalog is global and unfiltered; test debris is customer-facing~~ **FIXED** — `fix/customer-menu-scope-and-selection` | blocker | M | 3 |
| **F-06** | PM | Kitchen cannot mark DELIVERED, cancel, or undo | major | M | 4 |
| **F-07** | PM | No kitchen history or shift recap | major | M | 4 |
| **F-08** | PM/Analyst | ~~Kitchen timing and tempo freeze between order creations~~ **FIXED** — `fix/kitchen-live-resync` | major | S | 4 |
| **F-09** | PM | Partial payment exists in the API, unreachable from the cashier screen | major | S | 5 |
| **F-10** | PM | Refund only possible on the just-taken settlement, in-tab | major | M | 5 |
| **F-11** | Analyst | "30 Gün" renders 7 days | major | S | 6 |
| **F-12** | Analyst | "Bugün" plots order counts with a ₺ axis and a ₺ average line | major | S | 6 |
| **F-14** | DataEng/CTO | Owner with no store sees silent zeros instead of a 403 | major | S | 6 |
| **F-15** | UI | Dark-mode inheritance ⇒ white-on-white text on kitchen/owner | major | S | 9 |
| **F-16** | CTO/DataEng | Compose starts Metabase pointed at the operational DB; README understates the command | major | S | 2 |
| **F-17** | Analyst | A 7-day mean is presented as a forecast with row-count "confidence" | major | S | 8 |
| **F-18** | DataEng | Legacy dbt owner/forecast marts are not store-scoped | major | M | 8 |
| **F-23** | DataEng | ~~Test debris (8 `TestWaffle_*` products) sits in a customer-facing table~~ **FIXED** — `fix/customer-menu-scope-and-selection` (rows now inert and unreachable; deleting them is a DB chore) | major | S | 10 |
| **F-32** | DataEng | No demo/real data marker outside store id | major | M | 10 |
| **F-34** | DataEng/Analyst | Cancellation counts in legacy analytics, excluded everywhere else | major | S | 10 |
| **F-19** | PM/Analyst | Customer combo comparator mixes operands; "today's" label is not today | minor | S | 3 |
| **F-20** | UI/CTO | `packages/ui` is dead; its badge renders raw English enums | minor | S | 9 |
| **F-21** | PM/UI | Guest confirmation reads from URL params; no live order status | minor | S | 3 |
| **F-24** | UI | Kitchen errors use blocking `alert()` | minor | S | 4 |
| **F-25** | PM/UI | Cashier open-tables list never auto-refreshes | minor | S | 5 |
| **F-26** | CTO | ~~Kitchen reconnect timer survives unmount~~ **FIXED** — `fix/kitchen-live-resync` | minor | S | 4 |
| **F-27** | CTO | Redis started and configured with no consumer | minor | S | 2 |
| **F-28** | UI | Refund amount is free text with no bound shown, no numeric keypad | minor | S | 5 |
| **F-29** | Analyst | Average reference line includes the partial current day | minor | S | 6 |
| **F-30** | Analyst | `TopIngredients` is all-time, labelled "anlık görünüm", counts rows | minor | S | 7 |
| **F-31** | UI | `lang="en"` on two fully-Turkish apps | minor | S | 9 |
| **F-33** | DataEng | Mixed application/database clock sourcing, undocumented | minor | S | 10 |

Deliberately **not** raised as findings, per the skill's boundary: forecasting design,
supplier management, purchase orders, schema redesign, new dependencies, payment
redesign, inventory redesign, shift redesign. Those become work only when asked for
by name.

---

## 13. Verdict

**SweetOps is ready today for a demo and for a supervised single-store pilot. It is
not ready for a paid pilot, and it is not ready for a second store.**

What is real: the money and stock ledgers are correct and reconcile under adversarial
data; store scoping is enforced from the session on every path including the
WebSocket; the QR context is properly signed and handled; idempotency is designed
rather than patched; the kitchen timing layer refuses to fabricate a number; the
operational dashboard's definitions are written down and kept; the Turkish UX is
consistent and thorough; and the readiness documentation is honest to the point of
listing thirteen of its own failings. That is a genuine operational core and it is
worth more than most of what gets called an MVP.

What stops it: a shop cannot be fully *set up* without a developer (F-13 — its menu,
tables and table QR codes now can be, on
`feat/store-setup-and-menu-provisioning`, but its store row and its staff accounts
still cannot); a guest can order
only one hard-coded product while the catalog holds fourteen including test debris
(F-01, F-02); ~~the kitchen display can silently drop tickets after a Wi-Fi blip while
still showing "Canlı"~~ (F-05, fixed on `fix/kitchen-live-resync`); the owner sees two
irreconcilable revenue figures on one
screen (F-03) with every date and hour shifted three hours from the shop's clock
(F-04); and the shop's takings have no backup. The pattern behind most of these is
singular and encouraging: **the code written in the operational-dashboard and
kitchen-timing era is defined, documented and honest, and it is rendered on the same
page as legacy analytics code that is none of those things.** The fastest route to a
sellable product is not to build more — it is to retire or correct the legacy layer
and finish three flows that already have working backends behind them.

**Shortest path to a paid pilot:** fix the timezone and the revenue definition, make
the kitchen board re-sync, wire the partial payment that already exists, and give the
customer a real menu. Those five are P0 in the companion roadmap and none of them is
speculative — each has its backend already built. Store onboarding (F-13) and backups
follow immediately behind, and they are what turn one pilot into a second customer.

Forecasting stays where the roadmap already put it: deferred, correctly, until the day
boundary is right and 8–12 weeks of trustworthy history exist. It is the highest-value
*future* track and the lowest-value *current* one, and the 24 orders in the database
say so without argument.
