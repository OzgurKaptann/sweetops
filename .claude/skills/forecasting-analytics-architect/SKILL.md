---
name: forecasting-analytics-architect
description: Design the SweetOps forecasting, analytics, and reporting architecture — daily/weekly/monthly owner reports, product and hourly demand forecasts, ingredient consumption and stockout risk, prep-load forecasting, forecast confidence, accuracy backtesting, forecast-vs-actual, and analytics marts. Use when asked to design, plan, or scope reporting, analytics, metrics, demand prediction, or forecasting for SweetOps. Produces architecture and a phased v1-v4 roadmap; it recommends a baseline before any ML.
---

# SweetOps Forecasting & Analytics Architect

Design the reporting and forecasting architecture. **Output is design documents
and a phased roadmap.** Do not build models, tables, or endpoints in this skill
unless the user explicitly opens a branch for a specific phase.

## SweetOps context

Store-scoped restaurant operations system: FastAPI + PostgreSQL + Alembic,
four Next.js surfaces. Implemented core: QR ordering · kitchen flow · kitchen
timing · cashier payments/refunds · cashier shifts · order issues/refunds ·
inventory lifecycle · store-scoped stock · transfers · physical counts ·
threshold alerts · owner operational dashboard · demo seed · production
readiness docs. User-facing copy is Turkish.

Positioning to respect (`docs/PROJECT_ROADMAP.md`): SweetOps is an operations
product, **not** a forecasting demo. Forecasting is deliberately deferred until
enough reliable operational data exists. A legacy `ingredient-forecast` read
endpoint and some analytics views survive under `/owner`; treat them as legacy
surfaces to be reckoned with, not as a foundation to extend blindly.

Existing data to design against, before proposing anything new:

- orders and order items, with the order **status-event log** that already
  powers kitchen timing (`docs/KITCHEN_PREP_TIMING_METRICS.md`)
- payment settlements, allocations, refunds
  (`docs/PAYMENT_SETTLEMENT_WORKFLOW.md`)
- order issues and their bounded refunds
- store-scoped ingredient stock and its movement ledger — receipts, reservation,
  consumption, waste, adjustments, transfers, counts
  (`docs/INVENTORY_LIFECYCLE.md`, `docs/STORE_SCOPED_INVENTORY.md`)
- cashier shifts (`docs/CASHIER_SHIFT_CLOSING.md`)
- the owner operational dashboard aggregate
  (`docs/OWNER_OPERATIONAL_DASHBOARD.md`)

Start every design by mapping the requirement onto these existing sources.

## Boundaries

Do **not** casually implement: forecasting · supplier management · purchase
orders · new schema · new dependencies · payment redesign · inventory redesign ·
shift redesign. Each is its own explicitly-requested branch.

Specifically:

- **No new dependency** in a design document's v1. No pandas, no statsmodels,
  no Prophet, no scikit-learn as a starting assumption. Baseline forecasts are
  SQL and arithmetic.
- **No new schema** unless the design proves a read-only query cannot serve the
  need. If a mart is genuinely required, it is a separate migration branch with
  a single Alembic head and a working downgrade.
- Never invent a data field. If something needed is not captured today, list it
  as a **data gap** with the cheapest capture change, and design the fallback.

## Design principles

- **Baseline first.** Recommend ML only when a documented baseline has been
  measured and shown insufficient against a stated accuracy target. "Insufficient"
  means numbers from a backtest, not intuition.
- **Every metric gets a definition**: grain, filters, time zone, what counts as
  a sale (cancelled? refunded? issue-adjusted?), and the source of truth.
- **Store-scoped always.** No cross-store leakage; forecasts are per store unless
  a pooled model is explicitly justified.
- **Honest uncertainty.** A point forecast without a range invites bad
  purchasing decisions. Pair every forecast with an interval and a confidence
  label.
- **Reproducible.** A forecast produced for a date must be reconstructible later
  — that is what makes forecast-vs-actual possible at all.
- **Cold start is normal.** A shop with three weeks of data still needs the
  screen to work. Design the degraded mode explicitly.

## Scope to design

### Owner reports — daily / weekly / monthly
Grain, comparison period, and the owner's actual question for each. Revenue net
of refunds, order and cover counts, average ticket, product mix, hour-of-day
profile, waste, stock consumption, shift performance, issue rate. Define
day-boundary and time-zone handling and how a partial current day is displayed.

### Product demand forecast
Per product per day, per store. Baseline: seasonal-naive on day-of-week with a
trailing-window average, holiday/closure exclusion, and a floor for new or rare
products. State the horizon (start with 1–7 days) and the refresh cadence.

### Hourly demand forecast
Day total distributed by a day-of-week hourly profile from the order timestamps.
Drives prep and staffing. Note the noise floor: hourly counts at a single small
store are low-count data — say so and set expectations for the interval width.

### Ingredient consumption forecast
Product forecast × recipe mapping → ingredient quantities. First confirm the
product↔ingredient mapping actually exists and is complete; if it does not,
that is the first blocking data gap. Account for waste rate and yield.

### Stockout risk forecast
Current store-scoped stock, forecast consumption, and lead time → days-of-cover
and a risk level per ingredient. This is the natural extension of the existing
threshold alerts — extend that concept rather than inventing a parallel one.

### Prep load forecast
Forecast orders × measured per-product prep durations (from the kitchen timing
metrics) → expected kitchen minutes per hour, versus capacity. Surfaces "this
Saturday 19:00 is over capacity" before it happens.

### Forecast confidence
Interval method (empirical quantiles of historical residuals is enough for a
baseline), plus a plain-Turkish confidence label the owner can act on. Define
when confidence is too low to show a number at all.

### Forecast accuracy and backtesting
Rolling-origin backtest over held-out history. Metrics: MAE, MAPE (with its
zero-denominator caveat), WAPE, and bias. Always report the naive baseline
alongside — a model that does not beat seasonal-naive is not shippable.

### Forecast vs actual
Persist each forecast with its as-of timestamp so actuals can be joined later.
Owner-facing view showing yesterday's forecast against what happened, plus a
running accuracy trend. This is what earns trust in the numbers.

### Analytics marts, facts, dimensions
Only if operational queries genuinely cannot serve the reports. If needed:
`fct_order_item`, `fct_payment`, `fct_stock_movement`, `dim_product`,
`dim_ingredient`, `dim_store`, `dim_date`; define grain, refresh strategy,
late-arriving corrections (refunds and issues change history), and idempotent
rebuild. Prefer views, then materialized views, then physical tables — in that
order, and justify each step up.

### Baseline before ML
Document the baseline family (naive, seasonal-naive, moving average,
day-of-week × trailing mean), its cost, and its measured error. ML is a v4
proposal that must cite baseline backtest numbers as its justification.

## Phased roadmap

Structure every deliverable into these phases; each is a separate branch with
its own scope and its own explicit exclusions:

```text
v1 baseline reporting and forecast
v2 forecast accuracy/backtesting
v3 inventory recommendation
v4 advanced ML
```

- **v1** — daily/weekly/monthly owner reports plus baseline product and hourly
  forecasts, read-only, no new schema if avoidable, no new dependency.
- **v2** — persisted forecasts, rolling-origin backtesting, accuracy metrics
  against the naive baseline, and the forecast-vs-actual view.
- **v3** — ingredient consumption, stockout risk, days-of-cover, and suggested
  order quantities. Recommendation only; purchase orders and supplier
  management remain out of scope.
- **v4** — ML only if v2's numbers prove the baseline insufficient, with the
  dependency cost, retraining story, and failure mode written down.

## Deliverables

1. **`docs/ANALYTICS_ARCHITECTURE.md`** — data sources mapped to existing
   tables, metric definitions with grain and time zone, forecast method specs,
   data gaps with their cheapest fix, degraded/cold-start behaviour, and the
   read-path design (endpoints and owner-web surfaces) at spec level.
2. **`docs/FORECASTING_ROADMAP.md`** — the v1–v4 phases above, each with scope,
   exclusions, prerequisites, acceptance criteria (including the accuracy target
   a phase must hit), files that would be touched, and verification commands.

Rules: no fabricated accuracy numbers; state assumptions inline; flag every
place where a design decision depends on data that has not been inspected yet;
and end with the single next branch you recommend opening.
