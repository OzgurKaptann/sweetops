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
- **Turkish user-facing copy** across customer and staff surfaces.
- **Read-only reconciliation scripts** for payments, inventory, order issues, and
  kitchen timing.

App surfaces: `customer-web`, `kitchen-web`, `cashier-web`, `owner-web`, and the
`apps/api` FastAPI backend.

---

## 2. Near-term MVP Completion

Work that finishes the MVP without expanding the product's scope:

- **Owner operational dashboard** — consolidate the owner surfaces (inventory,
  kitchen, order issues, shifts) into a single operational view.
- **Seed demo and sample data** — reliable, reproducible sample data for
  portfolio/review/demo usage.
- **Production readiness hardening** — configuration, error handling, and
  operational robustness for a demonstrable deployment.

Forecasting is **not** in this list. It is deferred until enough reliable
operational data exists.

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
