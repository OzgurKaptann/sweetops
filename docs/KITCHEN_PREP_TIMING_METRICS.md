# Kitchen Preparation Timing Metrics

Kitchen staff and operators can now **see and measure** how long orders wait and
how long preparation takes — without any new source of truth, and without turning
SweetOps into a forecasting system.

It answers the operational questions the kitchen actually asks mid-service:

- *Sipariş ne kadar süredir bekliyor?* (How long has this order been waiting?)
- *Hazırlık ne kadar sürdü?* (How long did preparation take?)
- *Hangi sipariş gecikiyor?* (Which order is running late?)
- *Mutfakta ortalama hazırlık süresi ne?* (What is the average prep time?)
- *Bugün kaç sipariş zamanında hazırlandı?* (How many were on time today?)

---

## 1. Source of truth

**No schema change was made.** Timing is derived entirely from records that
already exist:

| Timing point | Derived from |
| --- | --- |
| `created_at` | `orders.created_at` — equal to the first `NEW` status event written at order creation |
| `prep_started_at` | `MIN(order_status_events.created_at)` where `status_to = 'IN_PREP'` |
| `ready_at` | `MIN(order_status_events.created_at)` where `status_to = 'READY'` |
| `delivered_at` | `MIN(order_status_events.created_at)` where `status_to = 'DELIVERED'` |
| `cancelled_at` | `MIN(order_status_events.created_at)` where `status_to = 'CANCELLED'` |

`order_status_events` is the append-only transition log already written by
`order_service` (the initial `NEW` event) and `kitchen_service` (every kitchen
transition). Because it already captures every transition with a server
timestamp, **no new table, column, or backfill is required** — adding derived
timing state would duplicate a truth the event log already holds.

`MIN` ("first time it entered the state") is deliberate: the 60-second undo
window lets an order bounce `NEW → IN_PREP → NEW → IN_PREP`, and the honest
"when did the kitchen first start this?" is the earliest `IN_PREP`, not the
latest. Historical statuses are never mutated.

Implementation: [`apps/api/app/services/kitchen_timing_service.py`](../apps/api/app/services/kitchen_timing_service.py).

---

## 2. Metric definitions

All durations are integer **seconds** (or `null` when the underlying event has
not happened).

**Completed (fixed) durations**

```
queued_seconds        = prep_started_at - created_at        # once prep has started
prep_seconds          = ready_at - prep_started_at          # once READY, prep event present
time_to_ready_seconds = ready_at - created_at               # once READY (end-to-end)
```

**Active (live) durations** — measured against the current time, kept in
separate `*_active` fields so a live number is never confused with a completed
one:

```
queued_seconds_active = now - created_at         # while still NEW (waiting)
prep_seconds_active   = now - prep_started_at    # while IN_PREP (cooking)
active_seconds        = now - created_at          # any non-terminal order
```

---

## 3. Active vs. completed durations

The distinction is load-bearing:

- **Completed** durations describe something that has finished and will never
  change (a prep that took 5 minutes stays 5 minutes).
- **Active** durations grow with the clock and are only present while their
  phase is open. A `NEW` order has a live `queued_seconds_active`; an `IN_PREP`
  order has a live `prep_seconds_active`; a `READY`/terminal order has neither.

Delay classification (below) runs **only** on active durations — you can be
"currently delayed", but a finished order is simply on-time or not by its
completed numbers.

---

## 4. Delay thresholds (static, display only)

Delay is a comparison of a live duration against fixed lines. These are constants
in `kitchen_timing_service.py` — there is intentionally **no threshold settings
UI** in this branch.

| Phase | Warning | Critical |
| --- | --- | --- |
| Queue (waiting to start) | 10 min (600 s) | 15 min (900 s) |
| Prep (IN_PREP) | 12 min (720 s) | 20 min (1200 s) |

`delay_state` is `ok | warning | critical`; `delay_reason` is one of
`queue_warning`, `queue_critical`, `prep_warning`, `prep_critical` (or `null`).
Prep delay takes precedence over queue delay — an order cooking too long is the
more urgent signal than one that merely waited.

> These thresholds are distinct from `kitchen_service`'s SLA bands (7/10 min),
> which score **total age from creation** to drive the live priority queue. Prep
> timing measures the **queue and prep phases separately**, which is a different
> question, so it uses its own lines.

---

## 5. Permissions & store scoping

- Both endpoints require an authenticated staff session and the `kitchen:read`
  permission — so **KITCHEN, MANAGER, and OWNER** may read; **CASHIER is
  rejected** (403), exactly as for the kitchen dashboard.
- The **store is derived from the session**; a client-supplied `store_id` is
  never read. Every query is filtered to `staff.store_id`, so cross-store reads
  are structurally impossible.
- Unauthenticated requests get **401**.
- Both responses set **`Cache-Control: no-store`** — operational data is
  per-second-fresh and identity-scoped, matching the kitchen/cashier surfaces.

### Endpoints

```
GET /kitchen/timing/orders    → active board (NEW/IN_PREP/READY), most delayed first, + live summary
GET /kitchen/timing/summary   → live counts + today's completed prep averages
```

Both are read-only. Nothing here touches payment, refund, or inventory state.

### Summary fields

```
active_orders, waiting_orders, in_prep_orders, ready_orders, delayed_orders   # live counts
completed_orders_today                                                        # count with completed prep today
average_prep_seconds_today
average_time_to_ready_seconds_today
p95_prep_seconds_today
```

"Today" is keyed on `orders.created_at::date = today (UTC)`, matching the day
boundary the owner-metrics layer already uses. **Averages are computed only from
real completed prep timing.** With no completed orders today, every completed
figure is `null` (never `0`-as-if-measured), and `completed_orders_today` is `0`.
`CANCELLED`-before-`READY` orders have no `READY` event, so they never inflate
the averages.

---

## 6. UI behaviour (kitchen-web)

[`apps/kitchen-web/src/app/page.tsx`](../apps/kitchen-web/src/app/page.tsx) with
helpers in [`src/lib/timing.ts`](../apps/kitchen-web/src/lib/timing.ts):

- A **"Mutfak temposu"** summary strip shows live active / waiting / hazırlanıyor
  / hazır / geciken counts (the delayed count turns red when > 0).
- Each order card shows the relevant elapsed time(s):
  - `NEW` → **Bekleme süresi** (live)
  - `IN_PREP` → **Bekleme süresi** (completed) + **Hazırlık süresi** (live)
  - `READY` → **Toplam süre**
- Delayed orders carry a Turkish badge: **Gecikiyor** (warning) / **Kritik
  gecikme** (critical), plus a reason line (e.g. *Hazırlık çok uzun sürüyor*).
- **Raw enum values are never displayed.** Statuses and delay states are mapped
  to Turkish; an unknown value degrades to a safe word, never to the raw enum. A
  missing/unknown duration renders as `—`, never a fabricated `0`.
- The timing fetch failing shows a Turkish error and retries.

All copy is Turkish. The English enums remain the wire contract.

---

## 7. Why this is not forecasting

Everything here is **arithmetic on timestamps that already happened**, plus a
comparison against static thresholds for display. Nothing predicts the future,
estimates a completion time, or models demand. `is_delayed` means *"this order
has already been waiting/cooking longer than the configured line"*, never *"this
order will be late"*. Forecasting remains intentionally deferred (see the
roadmap).

---

## 8. Diagnostics

[`scripts/reconcile_kitchen_timing.py`](../scripts/reconcile_kitchen_timing.py)
is a **read-only** check that re-reads the event log and flags any order whose
lifecycle events are internally impossible: `READY` at/before `IN_PREP`,
negative prep, a ready/terminal order with no prep event, contradictory terminal
history (both `READY`/`DELIVERED` and `CANCELLED`), or an event predating the
order's creation. It never mutates data. Exit `0` = clean, `1` = at least one
inconsistency.

---

## 9. Post-MVP deferred items

Intentionally **not** in this branch:

- Forecasting / demand prediction / estimated completion times.
- A full owner operational dashboard (owner-web is untouched here; it can consume
  these endpoints later).
- Configurable thresholds / a threshold settings UI.
- Per-item or per-station timing (this branch is per-order).
- Historical timing charts and trend lines beyond today's summary.
- Alerting / scheduled notifications on delays.
