# Repository Consolidation Report

**Branch:** `chore/repo-consolidation`
**Date:** 2026-07-08
**Scope:** Repository consolidation only. No new features, no out-of-scope business-logic fixes.

This document records the consolidation of three parallel copies of the SweetOps
application into a single canonical tree rooted at the repository root.

---

## 1. Existing application trees

Before consolidation the repository contained three complete, parallel copies of
the SweetOps monorepo:

| # | Location | Role |
|---|----------|------|
| 1 | repository root (`apps/`, `packages/`, `data/`, `docs/`, `scripts/`) | Canonical target — production-hardened version |
| 2 | `sweetops/` | Older snapshot of the root application |
| 3 | `sweetops-1/` | Newer snapshot with advanced owner/metrics/decision/conversion/kitchen functionality |

Every path that existed under `sweetops/` also existed at the root. Every path
that existed at the root also existed under `sweetops-1/` (plus 22 additional
files unique to `sweetops-1/`).

---

## 2. File counts for each tree (git-tracked, pre-consolidation)

| Tree | Tracked files |
|------|---------------|
| repository root (excluding nested copies) | 265 |
| `sweetops/` | 255 |
| `sweetops-1/` | 287 |

Path-set relationships:

* `sweetops/` ⊂ root by path — **0 files unique** to `sweetops/`. Root additionally
  contains the production-hardening files (audit log, concurrency/rollback/state-machine
  tests, `c9f1d3e8a042_production_hardening` migration, `DailySalesChart.tsx`).
* root ⊂ `sweetops-1/` by path — `sweetops-1/` adds **22 unique files** (21 application
  files + `.claude/settings.local.json`).

---

## 3. Why `sweetops/` is being removed

* `sweetops/` contains **no file path** absent from the root tree.
* For every path shared with the root, `sweetops/` is an **older** implementation:
  the root additionally contains the audit-log model/service, the production-hardening
  migration, and the concurrency / rollback / state-machine / audit / kitchen-realtime
  test suites — none of which exist in `sweetops/`.
* Therefore `sweetops/` contributes no unique functionality and is safe to delete.
  Git history is the rollback mechanism.

---

## 4. Unique functionality found in `sweetops-1/`

`sweetops-1/` is a strict superset (by path) of the root and adds these 21
application files (all migrated into the root — see §7):

**Backend — Alembic migrations**
* `apps/api/alembic/versions/d4e2f1a8b753_add_owner_decisions_table.py`
* `apps/api/alembic/versions/a1b2c3d4e5f6_add_decision_outcome_fields.py`
* `apps/api/alembic/versions/e5f3a2d9c847_add_ingredient_is_promoted.py`

**Backend — model / router / schema**
* `apps/api/app/models/owner_decision.py`
* `apps/api/app/routers/owner_metrics.py`
* `apps/api/app/schemas/metrics.py`

**Backend — services (measurement / decision / conversion / operational context)**
* `apps/api/app/services/conversion_engine.py`
* `apps/api/app/services/decision_engine.py`
* `apps/api/app/services/metric_definitions.py`
* `apps/api/app/services/metrics_service.py`
* `apps/api/app/services/operational_context_service.py`

**Backend — tests**
* `apps/api/tests/test_conversion_engine.py`
* `apps/api/tests/test_owner_decisions.py`

**Frontend — owner dashboard**
* `apps/owner-web/src/app/kitchen/page.tsx`
* `apps/owner-web/src/components/DecisionPanel.tsx`
* `apps/owner-web/src/components/FocusBanner.tsx`
* `apps/owner-web/src/components/KitchenSLAPanel.tsx`
* `apps/owner-web/src/components/MainAnalyticsChart.tsx`
* `apps/owner-web/src/components/MetricAttentionBanner.tsx`
* `apps/owner-web/src/components/MetricsPanel.tsx`
* `apps/owner-web/src/components/OperationsPanel.tsx`

The 22nd unique file, `.claude/settings.local.json`, is **not** migrated (see §9).

---

## 5. Files that differ at the same relative path (root vs `sweetops-1/`)

These 25 files existed at the same path in both trees with different
implementations and required a **semantic merge** (details in §8):

```
apps/api/app/core/config.py
apps/api/app/main.py
apps/api/app/models/__init__.py
apps/api/app/models/ingredient.py
apps/api/app/routers/kitchen_orders.py
apps/api/app/routers/owner_analytics.py
apps/api/app/routers/public_menu.py
apps/api/app/schemas/order.py
apps/api/app/schemas/owner_analytics.py
apps/api/app/services/kitchen_service.py
apps/api/app/services/menu_service.py
apps/api/app/services/owner_analytics_service.py
apps/api/tests/conftest.py
apps/api/tests/test_kitchen_rt.py
apps/api/tests/test_main.py
apps/customer-web/src/app/page.tsx
apps/customer-web/src/lib/api.ts
apps/kitchen-web/package.json
apps/owner-web/src/app/page.tsx
apps/owner-web/src/components/HourlyDemandChart.tsx
apps/owner-web/src/components/IngredientForecastPanel.tsx
apps/owner-web/src/components/KPICardGrid.tsx
apps/owner-web/src/components/StockWarningsPanel.tsx
apps/owner-web/src/components/TopIngredientsPanel.tsx
apps/owner-web/src/lib/api.ts
```

(`package-lock.json` also differed but is governed by the single-root-lockfile strategy — §9.)

---

## 6. Production behavior preserved from the root version

The following root behaviors were verified to be preserved and were **not**
regressed by adopting `sweetops-1/` code:

* **Audit log integration** — `models/audit_log.py`, `services/audit_service.py`,
  `tests/test_audit.py` retained unchanged; `AuditLog` still exported from `models/__init__.py`.
* **Order state machine** — `tests/test_state_machine.py` retained; `kitchen_service`
  status-transition logic is byte-identical between root and `sweetops-1` (only additive
  decision-intelligence code was layered on top).
* **Concurrency protection** — `tests/test_concurrency.py` retained.
* **Rollback / undo window** — `tests/test_rollback.py` retained.
* **Production-hardening migration** — `c9f1d3e8a042_production_hardening` retained; the
  new `sweetops-1` migrations chain **onto** it (`d4e2f1a8b753.down_revision = c9f1d3e8a042`).
* **Kitchen real-time behavior** — `tests/test_kitchen_rt.py` WebSocket/broadcast tests retained
  (and extended).
* **Owner analytics contract** — the root, self-contained analytics service
  (direct table queries, `points` / `sales_date` / `currency` fields) was kept because it is
  test-consistent and has no dbt-mart dependency (see §8).

---

## 7. Files migrated (copied from `sweetops-1/` and integrated)

All 21 unique application files listed in §4 were migrated. Integration performed:

* **Model export** — `OwnerDecision` added to `models/__init__.py` (`import` + `__all__`)
  so Alembic autogenerate/discovery and ORM lookups resolve it.
* **Router registration** — `owner_metrics.router` imported and `include_router`-ed in `main.py`.
* **Migration discovery** — the 3 new migration files are discovered by Alembic (verified via
  `alembic heads`, §10).
* **Service dependencies** — `decision_engine`, `conversion_engine`, `metrics_service`,
  `metric_definitions`, `operational_context_service` import cleanly; all use raw
  transactional tables (no `analytics.*` dbt dependency), so they run against the test DB.
* **Schema wiring** — `schemas/metrics.py` consumed by `owner_metrics` router; decision schemas
  added to `schemas/owner_analytics.py` and consumed by the `owner_analytics` router.
* **Frontend types / API client** — owner-web `lib/api.ts` gained decision / metrics /
  operational-context / kitchen-dashboard types and fetchers; components wired into
  `owner-web` `page.tsx` and `kitchen/page.tsx`.

Verification: `python -c "import app.main"` succeeds and exposes the new routes
`/owner/decisions/`, `/owner/decisions/{decision_id}`, `/owner/operational-context`,
`/owner/metrics/`, `/owner/metrics/dictionary`.

---

## 8. Files that required semantic merging (decision per file)

| File | Decision |
|------|----------|
| `app/main.py` | **Merge** — root + register `owner_metrics` router. |
| `app/core/config.py` | **Keep root** — root defaults to `localhost` for `DATABASE_URL`/`REDIS_URL`; docker-compose already overrides to `postgres`/`redis` via env. `sweetops-1`'s `postgres`/`redis` defaults would break local pytest. |
| `app/models/__init__.py` | **Merge** — root + `OwnerDecision` import/export. |
| `app/models/ingredient.py` | **Merge** — root + `is_promoted` column (required by `e5f3a2d9c847`). |
| `app/routers/kitchen_orders.py` | **Take sweetops-1** — response model becomes `KitchenDashboardResponse`. |
| `app/services/kitchen_service.py` | **Take sweetops-1** — adds decision signals / action hints / batching / kitchen-load; **state-machine, concurrency, audit, rollback regions are byte-identical to root** (additive only). `get_kitchen_orders` now returns a dashboard dict. |
| `app/schemas/order.py` | **Take sweetops-1** — superset; adds `should_be_started` / `urgency_reason` / `action_hint`, `KitchenLoadResponse`, `BatchingSuggestion`, `KitchenDashboardResponse`. No root field removed. |
| `app/routers/public_menu.py` | **Take sweetops-1** — adds `/upsell` and `/validate`; menu enriched with conversion signals. |
| `app/services/menu_service.py` | **Take sweetops-1** — enriched + operational-context-aware menu ranking. |
| `app/routers/owner_analytics.py` | **Merge** — keep root analytics endpoints; **add** `GET /owner/decisions/`, `PATCH /owner/decisions/{id}`, `GET /owner/operational-context`. |
| `app/schemas/owner_analytics.py` | **Merge** — keep root analytics schemas (`points`, `sales_date`, `currency`); **add** `DecisionSummary`, `OwnerDecision`, `OwnerDecisionsResponse`, `DecisionActionRequest`. |
| `app/services/owner_analytics_service.py` | **Keep root** — root is self-contained (direct table queries, no `analytics.*` dbt-mart dependency) and is consistent with the tests (`test_main` asserts `points`) and with the identical frontend `DailySalesData` type. `sweetops-1`'s mart-based rewrite (renamed `sales`/`USD`, depends on `analytics.agg_*`/`fact_orders`) would break tests and contradicts its own frontend, and dbt-mart refactoring is out of scope. |
| `tests/conftest.py` | **Take sweetops-1** — additive optional `name=` param on `make_ingredient` (backward compatible). |
| `tests/test_kitchen_rt.py` | **Take sweetops-1** — superset: updates list→dashboard access (`.json()["orders"]`) and adds ~30 unit tests. |
| `tests/test_main.py` | **Take sweetops-1** — kitchen dashboard assertions; owner-analytics assertions remain on `points` (matches kept root backend). |
| `customer-web/src/lib/api.ts` | **Take sweetops-1** — enriched-menu + upsell/validate types (consumers of the adopted `menu_service`/`public_menu`). |
| `customer-web/src/app/page.tsx` | **Take sweetops-1** — conversion-engine menu UI. |
| `owner-web/src/lib/api.ts` | **Take sweetops-1** (superset) + **fix** `OwnerDecision.decision_id` → `id` to match the backend contract (see §11 "silent contract" repair). |
| `owner-web/src/app/page.tsx` | **Take sweetops-1** — advanced dashboard wiring (also fixed `decision_id`→`id`). |
| `owner-web/src/components/{HourlyDemandChart,IngredientForecastPanel,KPICardGrid,StockWarningsPanel,TopIngredientsPanel}.tsx` | **Take sweetops-1** — advanced panels; consume the same root-compatible analytics types. Minor type-compat repairs applied so they compile against the pinned `recharts` (Tooltip formatter param typing) and preserve the `label`-augmented daily point (generic `sliceDays`). |
| `kitchen-web/package.json` | **Keep root** — root keeps the `@sweetops/types` dependency that `kitchen-web` still imports; `sweetops-1` had removed it inconsistently. |

### Contract-cascade repairs (no silent breakage)

The kitchen contract changed shape (`list` → dashboard `dict`). All consumers were updated
in the same change:

* `kitchen_orders` router response model → `KitchenDashboardResponse`.
* `tests/test_kitchen_rt.py`, `tests/test_main.py` → read `.json()["orders"]`.
* `kitchen-web/src/lib/api.ts` `fetchKitchenOrders` → returns `data.orders`
  (this simple screen renders only the order list; the advanced dashboard lives in owner-web).
* owner-web `lib/api.ts` already models the full `KitchenDashboardResponse`.

The `OwnerDecision` identifier mismatch (frontend `decision_id` vs backend `id`) was
repaired on the frontend to match the backend + tests (`id`).

---

## 9. Directories, files, and generated artifacts removed

* **`sweetops/`** — 255 tracked files (older duplicate application tree).
* **`sweetops-1/`** — 287 tracked files (duplicate tree; its 21 unique files were migrated first).
* **`sweetops-1/.claude/settings.local.json`** — **not migrated.** It contained only
  machine-local Claude permission entries (localhost `curl` / `docker` allowlists) with no
  reusable, secret-free documentation value. Local Claude/Fable permission settings must not be
  committed to the production repository, so it was removed with its tree (nothing extracted).
* **Nested per-app lockfiles** — `apps/customer-web/package-lock.json`,
  `apps/kitchen-web/package-lock.json`, `apps/owner-web/package-lock.json` removed to enforce a
  single root lockfile in the npm-workspaces monorepo.
* **`apps/owner-web/src/components/DailySalesChart.tsx`** — removed as orphaned/superseded dead
  code. After adopting `sweetops-1`'s `page.tsx`, nothing imports it; its function is superseded by
  `MainAnalyticsChart.tsx`. It was also the only remaining broken `@sweetops/ui` `Card` consumer.

No `backup/`, `legacy/`, `archive/`, `old/`, or `old-version/` directory was created. Git
history is the rollback mechanism. No generated artifact (`node_modules/`, `.next/`,
`__pycache__/`, `.pytest_cache/`, dbt `target/`) was newly committed.

---

## 10. Current Alembic migration graph

```
2478943c11df  (initial_models,          down: None)
        │
b7e5f2a9c341  (add_waffle_mvp,          down: 2478943c11df)
        │
c9f1d3e8a042  (production_hardening,    down: b7e5f2a9c341)      ← root hardening
        │
d4e2f1a8b753  (add_owner_decisions,     down: c9f1d3e8a042)      ← sweetops-1
        │
        ├── a1b2c3d4e5f6 (add_decision_outcome_fields,  down: d4e2f1a8b753)   ← HEAD
        └── e5f3a2d9c847 (add_ingredient_is_promoted,   down: d4e2f1a8b753)   ← HEAD
```

Real output of `alembic heads` (run from `apps/api/`):

```
a1b2c3d4e5f6 (head)
e5f3a2d9c847 (head)
```

There are **two heads** because `d4e2f1a8b753` has two independent children. This is the
expected multi-head state noted in the task. Per scope, migrations were **not** rewritten
in this branch (no deletions, no revision-id changes, no squash, no merge migration, no
`down_revision` edits). The graph will be linearized in the follow-up branch
`fix/alembic-single-head`.

> **Update (follow-up branch `fix/alembic-single-head`):** the two heads above were
> subsequently joined by a no-op Alembic merge revision (`4299b615f7aa`), leaving a single
> head. That work does not modify any of the migrations described in this report. See
> [`ALEMBIC_SINGLE_HEAD_RESOLUTION.md`](ALEMBIC_SINGLE_HEAD_RESOLUTION.md) for the full
> resolution, graph, and verification.

---

## 11. Problems intentionally excluded from this branch

Discovered but **not fixed** here (out of scope — repository consolidation only):

1. **Multiple Alembic heads** (`a1b2c3d4e5f6`, `e5f3a2d9c847`) — to be fixed in
   `fix/alembic-single-head`.
2. **customer-web production build** fails at static prerender:
   `useSearchParams() should be wrapped in a suspense boundary`. This is **pre-existing** —
   the root `HEAD` version of `customer-web/src/app/page.tsx` uses `useSearchParams()` at the
   top level with **zero** Suspense wrappers, and no Next.js version was changed by this branch.
   TypeScript type-checking of the merged page **passes**; only the Next.js 16 static-generation
   rule fails. Root cause: Next.js requires `useSearchParams()` to sit inside a `<Suspense>`
   boundary (or the route to opt out of static prerendering). Fixing it is a UI/framework change
   unrelated to consolidation.
3. **`owner_analytics_service` mart divergence** — `sweetops-1` had rewritten it to depend on dbt
   `analytics.*` marts and renamed the daily-sales contract (`sales`/`USD`), which is internally
   inconsistent with its own frontend and tests. Kept the root self-contained version; dbt-mart
   refactoring is out of scope.
4. **Pre-existing tracked dbt artifacts** under `data/dbt/target/compiled/**` remain tracked.
   Un-tracking them is repo hygiene unrelated to consolidation and would pollute this diff.

Other explicitly out-of-scope areas (order-quantity × consumption, modifier pricing, QR/token
security, auth, RBAC, store scoping, reservation-vs-consumption, idempotency redesign,
forecasting model, dbt mart refactor, UI redesign, dependency upgrades) were not touched.

---

## 12. Final repository structure

```
sweetops/ (repository root)
├── apps/
│   ├── api/
│   ├── customer-web/
│   ├── kitchen-web/
│   └── owner-web/
├── data/
│   └── dbt/
├── docs/
│   └── REPO_CONSOLIDATION_REPORT.md
├── packages/
│   ├── types/
│   └── ui/
├── scripts/
├── docker-compose.yml
├── package.json          (single root workspaces manifest: apps/*, packages/*)
├── package-lock.json     (single root lockfile)
├── README.md
└── .gitignore
```

The nested `sweetops/` and `sweetops-1/` application trees no longer exist.
