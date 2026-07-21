# Owner Operational Dashboard

## 1. Purpose

SweetOps already has all the operational primitives — orders, kitchen timing,
payments, refunds, order issues, cashier shifts, inventory thresholds, stock
counts, transfers, store-scoped access. What the owner lacked was one place to
answer, at a glance, the everyday question:

> **Bugün işler nasıl gidiyor?**
> Kaç aktif sipariş var? Kasa bugün ne topladı? Ne kadar iade yapıldı? Açık
> vardiya var mı? Vardiya kapanışlarında fark var mı? Kaç sorunlu sipariş açık?
> Mutfak gecikiyor mu? Kritik stok var mı? Hangi alan bugün dikkat istiyor?

The owner operational dashboard is a single **read-only** command center that
answers those questions by **aggregating the systems that already exist**. It is
deliberately *not* forecasting, BI, accounting, supplier management, or an
analytics warehouse — see §11.

## 2. Source-of-truth tables/services

The dashboard invents no data. Every figure is re-read live from the system that
already owns it, so the dashboard can never disagree with the screen a metric
came from.

| Block | Source of truth | Reused code |
|-------|-----------------|-------------|
| Orders (live board) | `order_status_events` + `orders` | `kitchen_timing_service.get_timing_summary` |
| Kitchen tempo | derived order lifecycle timing | `kitchen_timing_service.get_timing_summary` |
| Payments (collected/refunded) | `payment_settlements` + `payment_allocations` + `payment_refunds` | same tables/definitions as `payment_analytics_service` |
| Issues | `order_issues` | status/`resolved_at`/`approved_refund_amount` columns |
| Shifts | `cashier_shifts` frozen CLOSED snapshot | `cash_discrepancy_amount`, `status`, `closed_at` |
| Inventory | `ingredient_stock` (+ `ingredients`) | `inventory_service.threshold_status` |

Implementation: [`app/services/operational_dashboard_service.py`](../apps/api/app/services/operational_dashboard_service.py),
exposed by [`app/routers/owner_dashboard.py`](../apps/api/app/routers/owner_dashboard.py) as
`GET /owner/operational-dashboard`.

## 3. Metric definitions

"Today" is the server/UTC calendar day (`func.date(col) == today`), the same day
boundary the kitchen-timing summary and owner metrics layer already use.

### orders
- `active_count` / `waiting_count` / `in_prep_count` / `ready_count` — live counts
  of NEW+IN_PREP+READY / NEW / IN_PREP / READY, from the kitchen timing summary.
- `completed_today` — orders created today in status DELIVERED.
- `cancelled_today` — orders created today in status CANCELLED.

### payments (see §4)
- `gross_collected_today` — Σ completed payment allocations whose settlement
  completed today.
- `refunds_today` — Σ refund-ledger amounts created today.
- `net_collected_today` — `gross_collected_today − refunds_today`.
- `unpaid_or_partially_paid_orders` — count of non-cancelled orders whose
  `payment_status` is UNPAID or PARTIALLY_PAID (money owed, never revenue).

### kitchen (see §5)
- `active_orders`, `delayed_orders`, `average_prep_seconds_today`,
  `average_time_to_ready_seconds_today`, `p95_prep_seconds_today` — copied
  straight from `kitchen_timing_service.get_timing_summary`. Averages/percentiles
  are `null` when no order completed prep today (never a fabricated 0).

### issues (see §6)
- `open_count` — order issues in status OPEN.
- `resolved_today` — issues RESOLVED with `resolved_at` today.
- `refund_amount_today` — Σ `approved_refund_amount` of refunding resolutions
  (FULL/PARTIAL) resolved today. By construction this equals the refund-ledger
  rows the resolution created.

### shifts (see §7)
- `open_shift_count` — cashier shifts currently OPEN.
- `closed_today` — shifts CLOSED with `closed_at` today.
- `total_discrepancy_today` — Σ frozen `cash_discrepancy_amount` of shifts closed
  today.
- `shifts_with_discrepancy_today` — of those, how many have a non-zero
  discrepancy (`|discrepancy| ≥ 0.005`, matching owner-web's tolerance).

### inventory (see §8)
- `out_of_stock_count`, `below_reserved_count`, `critical_count`, `low_count`,
  `healthy_count`, `not_configured_count` — counts of active ingredients this
  branch stocks, classified by `inventory_service.threshold_status` (the same
  classifier the threshold-alerts screen uses).

### attention (see §9)
A deterministic priority list derived purely from the counts above.

## 4. Money rules

Money metrics use the existing ledger, never order totals:

- `gross_collected_today` = **collected** payment ledger amount (Σ completed
  allocations) for the store, scoped to settlements completed today — the same
  "collected" definition as `payment_analytics_service`, plus a date filter.
- `refunds_today` = refund-ledger amount (Σ `payment_refunds.amount`) for the
  store/date.
- `net_collected_today` = `gross − refunds`.

The dashboard **does not** use an order's `total_amount` as money collected, does
**not** count unpaid orders as revenue, and does **not** redesign the payment
ledger. `unpaid_or_partially_paid_orders` is surfaced as *money owed*, kept
strictly separate from collected cash.

## 5. Kitchen timing dependency

The kitchen block and the orders live counts both come from a single call to
`kitchen_timing_service.get_timing_summary(db, store_id)` — the same logic behind
`GET /kitchen/timing/summary`. Timing definitions are therefore identical to that
endpoint; nothing is re-derived here. The "never fabricate" rule is inherited:
averages/percentiles are `null` when there is no completed data.

## 6. Issue dependency

Open issues are `order_issues` in status OPEN. Resolved-today uses `resolved_at`.
Refund amount uses the frozen `approved_refund_amount` on refunding resolutions,
consistent with the order-issue workflow, where the refund ledger remains the
single source of truth for refunded money.

## 7. Shift dependency

Open shifts are current OPEN `cashier_shifts`. Closed-today figures read **only**
the frozen snapshot columns written at close time (`cash_discrepancy_amount`,
`closed_at`); a closed shift's discrepancy is never recomputed. This matches
`docs/CASHIER_SHIFT_CLOSING.md`.

## 8. Inventory threshold dependency

Inventory counts come from `inventory_service.threshold_status`, which classifies
each row against **available** stock (`on_hand − reserved`), never on-hand. No new
inventory status is defined; the counts mirror the `/inventory/threshold-alerts`
summary. See `docs/INVENTORY_THRESHOLD_ALERTS.md`.

## 9. Attention list

A small, deterministic priority list. Each item carries a `severity`
(`critical`/`warning`/`info`), a machine `code`, a `count`, and an optional
`target_route`. Rules (evaluated in a fixed order, then sorted by severity):

| code | severity | fires when | route |
|------|----------|-----------|-------|
| `OUT_OF_STOCK` | critical | out-of-stock or below-reserved ingredients exist | `/inventory` |
| `CRITICAL_STOCK` | warning | critical-stock ingredients exist | `/inventory` |
| `DELAYED_KITCHEN` | warning | delayed kitchen orders exist | `/kitchen` |
| `OPEN_ISSUES` | warning | open order issues exist | `/order-issues` |
| `SHIFT_DISCREPANCY` | warning | shifts closed today with a discrepancy | `/shifts` |
| `OPEN_SHIFTS` | info | open cashier shifts exist | `/shifts` |
| `UNPAID_ORDERS` | info | unpaid/partially-paid orders exist | *(no owning page)* |

The list is deterministic (fixed rules, fixed order, severity sort). There is no
scoring model, no ranking heuristic, and no LLM. `code`/`severity` are English
wire values; owner-web maps them to Turkish.

## 10. Permissions

`GET /owner/operational-dashboard` requires `owner:read`, held by **OWNER** and
**MANAGER** (never CASHIER or KITCHEN). It is:

- authenticated (401 without a valid staff session),
- store-scoped from the session — `store_id` comes from `staff.store_id`, never
  the client; there is no `store_id` parameter to tamper with,
- read-only, and served with `Cache-Control: no-store` (an operational snapshot
  must never be cached stale).

## 11. UI behavior

The owner-web landing page (`apps/owner-web/src/app/page.tsx`) gains an
**"Operasyon Özeti"** zone at the top:
[`OperationalDashboardPanel`](../apps/owner-web/src/components/OperationalDashboardPanel.tsx),
fed by [`operational-dashboard-api.ts`](../apps/owner-web/src/lib/operational-dashboard-api.ts)
with all display logic in the unit-tested
[`operational-dashboard-view.ts`](../apps/owner-web/src/lib/operational-dashboard-view.ts).

Cards: Günlük ciro, Aktif sipariş, Mutfak temposu, Açık sorunlu sipariş, Kasa
vardiyaları, Kritik stok, and a **Dikkat gerektirenler** list. Each card deep-links
to its detail page (`/inventory`, `/kitchen`, `/order-issues`, `/shifts`).

Behavior:
- **Loading** — skeleton cards.
- **Error** — Turkish message ("Veriler yüklenemedi…").
- **Empty** — safe zeros (`0,00 ₺`, `0`); durations with no data render `—`, never
  a faked 0; an empty attention list renders "Şu an dikkat gerektiren bir durum
  yok."
- **Raw enum protection** — severities and attention codes are mapped to Turkish in
  the view module; an unknown code degrades to a generic Turkish line, so no wire
  enum ever reaches the screen.

## 12. Database / migration

**No migration.** The dashboard is pure read-only aggregation over existing
tables; it stores nothing and creates no historical snapshot. There is deliberately
no new schema.

## 13. Why this is not forecasting/BI/accounting

Every figure is a **count** or a **sum** of things that have **already happened**,
plus a deterministic threshold comparison for the attention list. Nothing predicts
demand, estimates a completion time, models supply, or builds a historical
warehouse. It reuses the payment ledger, kitchen timing derivation, issue/shift
records, and inventory threshold classifier as-is — it does not restate or redesign
any of them. It is operational visibility, not analytics.
