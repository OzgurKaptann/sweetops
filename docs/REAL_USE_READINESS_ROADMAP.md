# SweetOps — Real-Use Readiness Roadmap

**Date:** 2026-07-23
**Branch:** `audit/runtime-product-gap-review`
**Source of findings:** [RUNTIME_PRODUCT_GAP_REVIEW.md](RUNTIME_PRODUCT_GAP_REVIEW.md)
**Status:** proposal. Nothing here is committed work. Each phase becomes a branch
only when asked for by name.

This roadmap sequences the audit's findings into candidate branches. Every phase
carries an explicit **scope** and an explicit **not in this branch** list, because
the failure mode for a repository at this maturity is a branch that starts as "fix
the timezone" and ends as "redesign reporting".

---

## 0. How this is ranked

A roadmap where everything is P1 is not a roadmap. Three tiers only:

- **P0 — blocks a paid pilot.** A shop cannot be set up, cannot be trusted, or is
  shown a wrong number it will act on. Six phases.
- **P1 — a shop works around it every day.** Real friction, real risk, but the shop
  can open. Five phases.
- **P2 — debt, polish and the future.** Worth doing, not worth delaying a pilot for.
  Four phases.

The ordering constraint that matters most: **P0-A (timezone) must land before any
history accumulates**, because every day of trading recorded against a UTC day
boundary is a day of history that has to be reinterpreted later.

Effort keys: S (< 1 day), M (1–3 days), L (a week or more).

---

## P0 — before a paying shop

### P0-A · `fix/business-timezone` — one business day boundary, defined once
**Fixes:** F-04 (and unblocks the forecasting track) · **Effort:** M ·
**Status: DONE** — branch `fix/business-timezone`

> **Delivered.** `BUSINESS_TIMEZONE` (default `Europe/Istanbul`) plus one shared
> helper module, [`app/core/business_time.py`](../apps/api/app/core/business_time.py).
> Storage is untouched and still UTC. Routed through it: `owner_analytics_service`
> (KPIs/`peak_hour`/hourly demand/daily sales/forecast windows),
> `operational_dashboard_service`, `kitchen_timing_service.get_timing_summary`,
> `metrics_service` (all seventeen daily predicates), `operational_context_service`,
> `owner_insights_service` (7/14-day windows and the `active_days` denominator) and
> the `/owner/metrics` future-date guard. Day windows are half-open UTC intervals;
> day and hour *groupings* use `AT TIME ZONE`. 48 tests in
> `apps/api/tests/test_business_timezone.py`; docs §3 of
> `OWNER_OPERATIONAL_DASHBOARD.md` and `KITCHEN_PREP_TIMING_METRICS.md` updated.
> Still open and deliberately out of scope: **F-11** (the "30 Gün" tab still renders
> the 7-day series) and **F-12**.

The single highest-leverage change in the audit. Istanbul is UTC+3; "today" currently
ends at 03:00 local, and every hour bucket on the owner's demand chart is labelled
three hours off.

**Scope**
- Introduce one configured business timezone and one shared "business day" helper.
- Route every existing day boundary through it: `owner_analytics_service`,
  `kitchen_timing_service.get_timing_summary`, `operational_dashboard_service`,
  `owner_metrics`'s future-date guard.
- Convert hourly bucketing to the business timezone so `hour_bucket` and `peak_hour`
  mean what their labels say.
- Update `docs/OWNER_OPERATIONAL_DASHBOARD.md` §3 and
  `docs/KITCHEN_PREP_TIMING_METRICS.md` where they currently document UTC.
- State the definition once, in one place, and link every consumer to it.

**Not in this branch:** no new reports, no chart redesign, no schema change (storage
stays UTC — this is a *presentation and boundary* change), no DST handling beyond
what the standard library gives (Türkiye has been on permanent UTC+3 since 2016),
no per-store timezone (one business timezone until a second country appears).

**Done when:** the same order appears on the same business day in the dashboard, the
kitchen summary and the daily sales series; and the hour labelled `21:00` is 21:00 in
the shop.

---

### P0-B · `fix/owner-revenue-single-definition` — one number for money
**Fixes:** F-03, F-34, F-14 · **Effort:** M

An owner who sees two different "today's revenue" figures on one screen stops
trusting both. `docs/OWNER_OPERATIONAL_DASHBOARD.md` §4 already contains the correct
definition; the legacy KPI card contradicts it.

**Scope**
- Make the ledger definition the only one: retire or re-source `fetch_kpis`'s
  `gross_revenue` / `average_order_value` so money comes from settlements and
  allocations, never `SUM(orders.total_amount)`.
- Exclude CANCELLED orders from every legacy analytics query (`fetch_kpis`,
  `fetch_daily_sales`, `fetch_hourly_demand`, `fetch_top_ingredients`) — matching the
  behaviour the timing and dashboard services already have.
- Apply `_require_store` (already written, already correct) to every owner analytics
  route so a storeless account gets the honest 403 instead of rendered zeros.
- Write the definitions down: one short metric-definition section that says what
  counts as revenue, what counts as an order, and what a cancellation does.

**Not in this branch:** no new reports, no new endpoints, no payment-ledger change,
no chart work (P1-D), no removal of the legacy analytics *panels* (P1-E decides
that).

**Done when:** every money figure on the owner page traces to the payment ledger, and
a storeless owner sees a refusal rather than a zero.

---

### P0-C · `fix/kitchen-live-resync` — the board must never lie
**Fixes:** F-05, F-08, F-26 · **Effort:** S · **Status: DONE**

A kitchen display that shows a green **Canlı** badge while missing three tickets is
worse than one that shows an error. The server already sends everything needed; the
client discards it.

**Scope**
- ✅ Handle the `initial_state` event the server already emits on every connect
  (`ws.py:64-79`), or refetch on every successful reconnect — *both* were done:
  `initial_state` triggers a refetch, and every socket open resyncs independently.
- ✅ Refresh timing and tempo on `order_status_updated`, not only on `order_created`.
  The local-patch path is gone; orders and timing are always refetched as a pair.
- ✅ Add a low-frequency safety poll so the board self-heals even if the socket lies.
  One interval: 12 s while degraded, 30 s while live.
- ✅ Always offer the manual refresh control, not only while disconnected.
- ✅ Clear the reconnect timer on unmount (and the poll interval, and any socket).

**Delivered:** the socket/reconnect/freshness rules moved into
[`liveSync.ts`](../apps/kitchen-web/src/lib/liveSync.ts), a framework-free
controller bound to React by
[`useKitchenLiveSync.ts`](../apps/kitchen-web/src/lib/useKitchenLiveSync.ts), with
30 deterministic tests on a fake clock and a fake socket. The badge is derived from
when data last *arrived*: `live` requires an open socket **and** a refresh inside
60 s; otherwise it reads `reconnecting`, `polling`, `stale` or `offline`, always
alongside a "son güncelleme" line. Waking the tablet or regaining the network
resyncs immediately. **No backend change was required.**

**Not in this branch:** no board redesign, no history surface (P1-A), no new
transitions (P1-A), no WebSocket protocol change, no broker.

**Done when:** disconnecting the network for ninety seconds, placing orders, and
reconnecting leaves the board correct without a human touching it. — Met in the
unit suite; **still NEEDS_MANUAL_BROWSER_CONFIRMATION** on a real tablet.

---

### P0-D · `feat/customer-real-menu` — a menu a shop can actually sell from
**Fixes:** F-01, F-02, F-23 · **Effort:** M · **Status: PARTIALLY DONE**
(scoping + selection delivered on `fix/customer-menu-scope-and-selection`;
the multi-item cart is still open)

Fourteen products exist; a guest can order one. Eight of the fourteen are test
debris in a customer-facing table. These must be fixed together — fixing the first
alone exposes the second to guests.

**Scope**
- ⚠️ Render the real product list: product selection ✅, quantity ✅, and multiple
  lines per order ❌ — the order API accepts an items array, but the customer
  screen still submits exactly one line. **This is the remaining half of P0-D.**
- ✅ Give the catalog an activation and ownership boundary so test debris and
  retired items cannot reach a guest. **This requires a schema decision** — take it
  explicitly, in this branch, with a migration, or defer the branch. Do not smuggle
  it in. — Taken explicitly: migration `a9e4c7b25d13` adds `products.is_active`
  (chain-wide retirement) and a `store_products` publication table. The menu and
  order creation are both joins through it, scoped to the store the QR token
  resolved to. Nothing was backfilled, so the catalog fails closed.
- ⚠️ Clean the existing `TestWaffle_*` debris and document how it got there. — How
  it got there is documented (a test helper that created products and cleaned up
  only the product; it now delegates to a publishing/withdrawing conftest helper),
  and the rows are inert and unreachable. **The eight rows are still in the
  development database** — deleting them is a database chore, deliberately not
  done from application code.

**Delivered:** [CUSTOMER_MENU_SCOPING.md](CUSTOMER_MENU_SCOPING.md) — the model,
where it is enforced, the quantity bounds (server `ge=1` with explicit maxima;
a negative quantity previously produced a *negative* stock requirement), and 14
backend + 18 customer-web tests, none of which match on a product name.

**Not in this branch:** no per-store pricing (name it, defer it to P1-B), no cart
persistence across sessions, no order notes/allergens (P1-C), no upsell rework, no
combo redesign beyond fixing the comparator (F-19 rides along, it is three lines).
The delivered branch was narrower still: **no cart, no onboarding surface, no QR
management, and F-19 was left alone** to keep the diff to scoping and selection.

**Done when:** a guest at a table can order two waffles and a Türk Kahvesi in one
submission, and no `TestWaffle` is reachable from any surface. — **Half met:** no
`TestWaffle` is reachable from any surface; the single submission still cannot
carry both items.

---

### P0-E · `feat/store-onboarding` — a shop can be set up without a developer
**Fixes:** F-13 · **Effort:** L ·
**Status: PARTIALLY DONE** — branch `feat/store-setup-and-menu-provisioning` (v1)

> **Delivered (v1).** An authenticated, role-gated, store-scoped setup surface:
> `/owner/setup/status`, `/owner/menu/*` and `/owner/tables/*` in the API
> ([`owner_setup.py`](../apps/api/app/routers/owner_setup.py) +
> [`store_setup_service.py`](../apps/api/app/services/store_setup_service.py)), and
> `/setup` in owner-web. An OWNER/MANAGER can now create and edit catalog products,
> publish and withdraw them from **their own branch's** menu, switch an item off for
> the day, set menu order, add and rename tables, and issue or rotate a table's QR
> sticker. Two new permissions (`setup:read`, `setup:manage`) held by OWNER/MANAGER
> only. A readiness checklist answers the question a fail-closed customer menu
> cannot: *why is my menu empty?* **No migration, no new dependency.** 35 backend +
> 48 owner-web tests. See
> [STORE_SETUP_AND_MENU_PROVISIONING.md](STORE_SETUP_AND_MENU_PROVISIONING.md).
>
> **Still open in this phase:** creating a **store** (still a seed/psql act); staff
> accounts and password resets (`manage_staff_users.py` remains the only supported
> path); a printable QR sheet; ingredient/recipe authoring; per-store pricing (P1-B);
> closing/retiring a table (`tables` has no `is_active` column); and any guided
> onboarding wizard. The two CLIs still exist and are still the supported path for
> what they cover.

The largest single piece of unbuilt work, and the one that decides whether there can
be a second customer. Today: creating a store, a table, a product or a price requires
editing Python on the database host.

**Scope**
- ⚠️ An authenticated, role-gated administration surface for store, tables, products and
  prices, and ingredients/recipes. — **Partly delivered:** tables ✅, products ✅ (name,
  category, price, active state, per-branch publication/availability/order),
  store ❌, per-store prices ❌ (P1-B), ingredients/recipes ❌.
- ⚠️ Bring the two existing CLIs (`manage_staff_users.py`, `manage_qr_tokens.py`) into
  the same authenticated surface — staff creation, password reset, session revoke,
  QR issue/rotate/revoke. Preserve their existing safety properties exactly:
  passwords never in argv, raw QR tokens printed once and never recoverable,
  destructive operations keyed on primary key not display prefix. — **QR issue and
  rotate are now in the authenticated surface**, with the safety properties intact
  (the raw link is returned exactly once and is unrecoverable, so there is
  deliberately no "show QR link" endpoint). Revoke-without-replacement, and all of
  `manage_staff_users.py`, are untouched.
- ❌ A printable QR sheet per table, since the current path prints a raw token to a
  terminal. — Not built; the link is copyable text in a one-time dialog.

**Not in this branch:** no chain-level multi-store console (P1-B), no supplier or
purchase-order concepts, no billing, no self-service signup — this is *vendor-assisted
onboarding made possible without a developer*, not a SaaS signup funnel.

**Done when:** a new waffle shop is fully operational — store, tables, QR codes,
menu, prices, staff accounts — without anyone opening an editor or a psql prompt.
— **Not yet met.** Tables, QR codes and the menu are now self-service; the store row
and the staff accounts are not.

---

### P0-F · `ops/backup-and-restore` — the shop's takings survive a disk
**Fixes:** the §11 backup gap (`PRODUCTION_READINESS.md` §14.3) · **Effort:** M

SweetOps is a system of record for money. There is no backup. Charging a shop for a
system whose data cannot be restored is not defensible, and this is infrastructure
work that does not touch product scope at all.

**Scope**
- Automated `pg_dump` on a schedule, with retention.
- A **tested** restore procedure in `docs/OPERATIONS_RUNBOOK.md` — a backup nobody
  has restored is a hope.
- A documented recovery-point objective, honestly stated.
- Resolve F-16 first or alongside: Metabase must not own tables inside the
  operational database that a restore then has to reason about.

**Not in this branch:** no hosting, no TLS, no CI, no monitoring — each is its own
piece of work and none of them are this one.

**Done when:** a full restore has been performed from an automated backup into a
clean database and verified with all four reconcilers.

---

## P1 — a shop works around these daily

### P1-A · `feat/kitchen-complete-flow` — the flow the state machine already supports
**Fixes:** F-06, F-07, F-24 · **Effort:** M

The backend supports `READY→DELIVERED`, cancellation from `NEW`/`IN_PREP`, and a
60-second undo window. The kitchen surface exposes a two-step ladder and nothing
else, so handing food over is currently an owner-console action and a mis-tap is
permanent.

**Scope:** surface DELIVERED, cancellation with a reason, and the existing undo
window on the kitchen board; a recent-orders / shift-recap view from
`order_status_events` and the timing endpoint; replace `alert()` with the inline
Turkish status treatment every other surface uses.

**Not in this branch:** no new statuses, no change to `VALID_TRANSITIONS`, no change
to the undo window length, no inventory-consumption change (cancellation semantics
stay exactly as documented in `kitchen_service`), no board redesign.

---

### P1-B · `feat/multi-store-catalog` — a second branch becomes possible
**Fixes:** the multi-store half of F-02, the §11 multi-store-admin gap · **Effort:** L

`menu_service` reads `db.query(Product).all()` and its docstring states the
single-catalog assumption plainly. Two branches cannot have different prices or a
seasonal item. `compute_operational_context` is also called without a store on the
public menu path (F-22 in the review), so customer-facing ranking uses chain-wide
metrics.

**Scope:** store-scoped catalog and pricing; store-scoped menu ranking context; a
chain-level owner view that compares branches instead of forcing two logins.

**Not in this branch:** no per-store recipes, no franchise or tenant model, no
cross-store transfers work (that already exists and is reconciled), no pricing
strategy features.

**Depends on:** P0-D (catalog boundary) and P0-E (onboarding) — doing this first
would mean building multi-store administration for a catalog nobody can edit.

---

### P1-C · `feat/cashier-real-rush` — the desk survives a queue
**Fixes:** F-09, F-10, F-25, F-28 · **Effort:** M

The ledger supports partial payment; the screen never sends an amount. The refund
path exists only on the settlement just taken, in the current tab.

**Scope:** an amount field on collection so a bill can be split (the API field
already exists — `schemas/payment.py:84`); refund reachable from transaction history,
not only from in-memory receipt state; a numeric input with the refundable bound
shown before the server rejects it; auto-refresh of open tables.

**Not in this branch:** no payment redesign, no new payment methods, no split-by-item,
no shift redesign, no receipt printing (P2-D), no change to refund bounds or the
order-issue workflow.

---

### P1-D · `feat/owner-period-reports` — beyond today
**Fixes:** F-11, F-12, F-29, F-30 and the §6 report table · **Effort:** L

The operational dashboard is correctly and deliberately a *today* aggregate. Ten
reports were named in the review; nine are answerable from data already captured.

**Scope:** daily close, day-over-day and week-over-week comparison, weekly and
monthly summaries, shift comparison, issue trend, kitchen SLA trend. Real date ranges
replacing the hard-coded 7 days. Partial-day marking on every series that includes
today. Correct axis units and honest comparisons.

**Not in this branch:** no forecasting (P2-A), no margin or profitability report
(blocked — there is no ingredient cost price in the schema; name it, do not build it
here), no BI tool, no data warehouse, no export.

**Depends on:** P0-A and P0-B. Building period reports on a UTC day boundary and a
contested revenue definition would mean building them twice.

---

### P1-E · `chore/retire-legacy-analytics` — stop shipping two eras on one page
**Fixes:** F-17, F-18, F-20, F-27, and the second half of F-16 · **Effort:** M

The owner landing page renders the operational dashboard (defined, documented,
honest) directly above legacy analytics panels (undefined, and wrong in three
places). `PRODUCTION_READINESS.md` §14.10 already classes the legacy surface as
unmaintained — the page does not reflect that.

**Scope:** decide, per legacy surface, retire or adopt — the `ingredient-forecast`
endpoint and panel, the dbt project under `data/`, the Metabase and Redis containers,
and the unused `packages/ui`. Whatever is kept gets a definition, store scoping and
an owner; whatever is not gets removed. If the forecast panel stays, relabel it as
the baseline it is (`baseline_method` is already on the wire) and stop deriving
"confidence" from row count.

**Not in this branch:** no forecasting design or implementation, no new analytics, no
dashboard redesign — this branch *subtracts*.

---

## P2 — debt, polish, and the future

### P2-A · `feat/forecasting-baseline` — the intelligence track, when it is honest
**Addresses:** §8 of the review · **Effort:** L · **Gated, not scheduled**

Forecasting is the highest-value future track and the lowest-value current one. The
database holds 24 orders across at most 2 distinct days; a third are cancelled. This
branch does not open until its gates are met.

**Gates — all four, in order:**
1. P0-A shipped (a correct business day and hour boundary).
2. Consumption measured as *quantity*, not as a count of `order_item_ingredients`
   rows — the inventory ledger already holds the right signal and the forecast code
   does not read it.
3. Cancellations excluded from demand (P0-B does this for analytics; forecasting
   inherits it).
4. **8–12 weeks of real pilot history**, in a real shop, on the corrected boundaries.

**When the gates are met:** hand the design to the
[`forecasting-analytics-architect`](../.claude/skills/forecasting-analytics-architect/SKILL.md)
skill, which recommends a baseline before any ML. Do not design it in an audit branch
and do not design it in P1-D.

**Not in this branch, ever:** ML before a documented baseline that beats it;
weather or holiday inputs before there is a calendar of closures and a backtest
harness; any forecast surfaced to an owner without a stated method, horizon and
measured accuracy.

---

### P2-B · `feat/ui-theme-consistency` — four surfaces, one product
**Fixes:** F-15, F-31, F-20 (UI half), plus the §9 inconsistency table · **Effort:** M

**Scope:** resolve the dark-mode inheritance hazard on kitchen and owner (elements
with no explicit text colour inheriting `#ededed` onto white cards — F-15,
**NEEDS_MANUAL_BROWSER_CONFIRMATION** before acting); `lang="tr"` on all four apps;
one shared theme and one login screen instead of three; consistent focus rings;
tablet-first target sizes on the cashier bill and the kitchen timing block; remove
the `font-family: Arial` body rule that overrides the Geist fonts two apps pay to
load.

**Hand off to:** the [`ui-theme-review`](../.claude/skills/ui-theme-review/SKILL.md)
skill for the depth pass — this audit's §9 was a first-pass read only, and every
visual claim in it is unconfirmed in a browser.

**Not in this branch:** no layout redesign, no new components, no accessibility
certification, no design system beyond what the four surfaces actually need.

---

### P2-C · `chore/data-hygiene` — keep the warehouse clean before there is one
**Fixes:** F-23, F-32, F-33 · **Effort:** M

**Scope:** stop test runs leaking rows into shared catalog tables (a separate test
database is the real fix, and `PRODUCTION_READINESS.md` §14.7 already names it); a
durable way to tell demo data from real data that does not rely on hard-coded store
ids; document the clock-sourcing assumption alongside §14.12's single-node note; a
fifth reconciler-style check for catalog debris and stale-`READY` orders.

**Not in this branch:** no schema redesign, no data warehouse, no dbt work (P1-E
decides that project's fate first).

---

### P2-D · `feat/guest-and-receipt` — closing the two ends of the flow
**Fixes:** F-21, and the §11 receipt/fiscal gap · **Effort:** M

**Scope:** a live order-status view for the guest after ordering (the WebSocket and
timing data already exist), replacing a terminal confirmation built from URL
parameters; a durable, re-openable receipt for the cashier; an assessment of Turkish
receipt obligations for this class of business, written down before anything is
built against an assumption.

**Not in this branch:** no fiscal-device or POS-hardware integration, no accounting
export, no e-Arşiv/e-Fatura integration — those are post-MVP backlog items in
`PROJECT_ROADMAP.md` §3 and stay there.

---

## Sequencing

```
P0-A timezone ──┬──> P0-B revenue ──> P1-D period reports ──> P2-A forecasting (gated)
                └──────────────────────────────────────────────┘

P0-C kitchen resync ──> P1-A kitchen complete flow

P0-D real menu ──> P1-B multi-store catalog
                     ↑
P0-E onboarding ─────┘

P0-F backup/restore   (independent — start any time)
P1-C cashier rush     (independent — start any time)
P1-E retire legacy    (after P0-B decides what "revenue" means)
P2-B/C/D              (independent)
```

Three things can start immediately and in parallel: **P0-A**, **P0-C** and **P0-F**.
They share no files, no definitions and no risk with each other, and P0-A is the one
whose delay is most expensive — every day of trading recorded on a UTC day boundary
is a day of history that must be reinterpreted later.

---

## Recommended next branch

**`fix/business-timezone` (P0-A).**

It is the cheapest P0, it is a prerequisite for the two most valuable tracks (period
reporting and, eventually, forecasting), it corrects a number an owner would
otherwise act on, and its cost rises with every day of real trading that is recorded
against the wrong boundary. Nothing else in this roadmap gets more expensive by
waiting.

If the priority is instead *"put it in a shop next week"*, take **P0-C** — one day of
work, and it removes the only defect in this audit that can silently lose a
customer's order.

---

## Explicitly not on this roadmap

Per the audit skill's boundary, these are not proposed as work by this document and
become work only when asked for by name: supplier management, purchase orders,
automatic reorder, barcode, lot/expiry tracking, customer wallet, coupons and store
credit, delivery integration, bank reconciliation, accounting export, chargeback
workflow, POS hardware integration, and a mobile app. They remain where
`docs/PROJECT_ROADMAP.md` §3 puts them: an uncommitted post-MVP backlog.

Also not proposed: any redesign of the payment ledger, the inventory lifecycle, or
the shift workflow. All three are implemented, documented and reconciled, and the
audit found no defect in any of them.
