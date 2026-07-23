# SweetOps Product Roadmap

This roadmap describes where SweetOps is today and where it is intentionally
going. It is scoped to the operational product that currently exists in the
repository.

**Positioning:** SweetOps is a store-scoped restaurant operations system for
small food businesses. It is not a forecasting/analytics demo. Demand
forecasting is intentionally deferred (see below).

---

## 1. Current MVP / Implemented

These capabilities exist in the repository today and map to backend routers,
migrations, and frontend surfaces:

- **Secure QR table context** — signed table tokens resolve to a scoped ordering
  context.
- **Customer order creation** with **idempotent** submission.
- **Kitchen order lifecycle** with real-time (WebSocket) status transitions.
- **Kitchen preparation timing metrics** — per-order queue/prep/time-to-ready
  durations and a live delay/summary view, derived from the existing order
  status-event log (no new schema). Measurement only, not forecasting.
- **Staff authentication** (cookie-based sessions) and **role-based access
  control**.
- **Payment settlement ledger**, **payment allocations**, and **payment
  refunds**.
- **Order issue and controlled refund workflow** (raise, resolve, list, with
  bounded refunds).
- **Cashier shift opening and closing** with auditable shift records.
- **Store-scoped inventory** across its full lifecycle: reservation/consumption,
  purchase receipts, waste, manual adjustments.
- **Inventory transfer workflow** between stores.
- **Physical stock count workflow** reconciling counted vs. system stock.
- **Inventory threshold alerts** for low stock.
- **Owner inventory UI** and **owner issue history**.
- **Owner operational dashboard** — one read-only, store-scoped command center
  (`GET /owner/operational-dashboard` + owner-web landing zone) aggregating
  today's orders, collected/refunded money, kitchen tempo, open issues, cashier
  shifts, and inventory alerts, plus a deterministic attention list. Aggregation
  only — it reuses each existing source of truth and adds no new money, stock,
  timing, or schema. Not forecasting, BI, or accounting.
- **Turkish user-facing copy** across customer and staff surfaces.
- **Read-only reconciliation scripts** for payments, inventory, order issues, and
  kitchen timing.
- **Deterministic demo seed data** — one command (`python scripts/seed_demo_data.py`
  / `npm run seed:demo`) populates a coherent Turkish waffle-shop demo so every
  surface is meaningful immediately: active/waiting/in-prep/ready/completed and
  cancelled orders, live kitchen-timing warnings, cash/card/partial payments,
  direct and issue-driven refunds, open/resolved issues, open and closed shifts
  (one with a discrepancy), all inventory threshold states, and stock
  receipts/waste/adjustments/transfers/counts. Idempotent, demo-scoped, and
  non-destructive: it drives the existing services so the reconcilers stay green
  and non-demo data is never touched. See [DEMO_SEED_DATA.md](DEMO_SEED_DATA.md).
- **Production-readiness hardening** — the repository is documented, verifiable,
  and safe to run, demo, and review:
  [PRODUCTION_READINESS.md](PRODUCTION_READINESS.md) (setup, environment
  checklist, migration/seed/test/reconciliation workflows, a practical security
  review, deployment and rollback checklists, and an explicit list of
  non-production limitations), [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md)
  (pre-merge tick-list plus manual browser smoke checks), and
  [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md) (day-to-day operation,
  inspection queries, dirty-database recovery, and what not to do). Audited
  `.env.example` files at the repo root and in `apps/api`, and a read-only
  `scripts/verify_release_readiness.py` that checks required docs and scripts,
  a single Alembic head, doc links, merge markers, and committed secrets.
  Documentation and verification only — no product behaviour was changed.

App surfaces: `customer-web`, `kitchen-web`, `cashier-web`, `owner-web`, and the
`apps/api` FastAPI backend.

---

## 2. Near-term MVP Completion

**This list is now empty — the MVP is complete.** Every item that finished the
MVP without expanding the product's scope has shipped and moved to section 1:

- ✅ Owner operational dashboard
- ✅ Deterministic demo and sample data
- ✅ Production-readiness hardening

The repository is at **release-candidate readiness**. It is **not yet a hosted
production deployment**: there is no CI, no monitoring or alerting, no managed
secret storage, and no backup automation. These are infrastructure concerns
rather than product scope, and the full, honest gap list is section 14 of
[PRODUCTION_READINESS.md](PRODUCTION_READINESS.md).

Forecasting is **not** in this list, and never was. It is deferred until enough
reliable operational data exists — see the closing section.

---

## 3. Post-MVP Backlog

Candidate features for after the MVP is complete. These are not committed and not
part of the current build:

- Forecasting (demand prediction)
- Supplier management
- Purchase orders
- Automatic reorder
- Scheduled alerts
- Barcode support
- Lot / expiry tracking
- Customer wallet
- Coupons / store credit
- Delivery integration
- Bank reconciliation
- Accounting export
- Chargeback workflow
- POS hardware integration
- Mobile app

---

## 4. Explicitly Out of Scope for Now

The following are deliberately excluded from the current effort:

- **Forecasting as an active feature.** A legacy `ingredient-forecast` endpoint
  and older analytics views remain in the codebase, but they are not the product
  center and are not being extended in the MVP branch plan.
- **Supplier management.**
- **Purchase orders.**
- **Automatic reorder.**
- **POS hardware integration.**
- **Mobile app.**
- **Delivery / accounting / bank integrations.**

---

## Why Forecasting Is Deferred

Forecasting depends on a meaningful volume of reliable operational data — orders,
consumption, waste, and inventory movement over time. SweetOps prioritizes
building the operational system that *produces* that data correctly and
auditably first. Until enough trustworthy operational history exists, forecasting
would be speculative rather than useful, so it is intentionally deferred to the
post-MVP backlog. The historical analytics/dbt references retained in the
repository are treated as legacy, not as the current product direction.
