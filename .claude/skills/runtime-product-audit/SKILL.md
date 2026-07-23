---
name: runtime-product-audit
description: Audit the SweetOps product end to end from CTO, product-manager, data-engineer, data-analyst, and UI-designer perspectives — installation readiness, runtime browser flows (customer QR, kitchen, cashier, owner), reporting and forecasting gaps, analytics and UI quality, data-quality risk, and commercial readiness. Use when asked to audit, assess, review the state of, find gaps in, or judge the readiness/maturity of SweetOps as a product. Produces audit and roadmap documents only — it never implements features.
---

# SweetOps Runtime Product Audit

Audit SweetOps as a working product, not as a codebase. The deliverable is a
written audit plus a roadmap. **Do not implement features in this skill.**

## SweetOps context

SweetOps is a store-scoped restaurant operations system for small food
businesses (Turkish waffle shop domain). Monorepo:

| Surface | Path | Dev port |
| --- | --- | --- |
| `customer-web` | `apps/customer-web` | 3001 |
| `kitchen-web` | `apps/kitchen-web` | 3002 |
| `owner-web` | `apps/owner-web` | 3003 |
| `cashier-web` | `apps/cashier-web` | 3004 |
| `apps/api` (FastAPI + PostgreSQL + Alembic) | `apps/api` | 8000 |

Implemented core: QR ordering · kitchen flow · kitchen timing · cashier
payments/refunds · cashier shifts · order issues/refunds · inventory lifecycle ·
store-scoped stock · transfers · physical counts · threshold alerts · owner
operational dashboard · demo seed · production readiness docs. All user-facing
copy is Turkish.

Read before auditing: `README.md`, `docs/PROJECT_ROADMAP.md`,
`docs/PRODUCTION_READINESS.md`, `docs/RELEASE_CHECKLIST.md`,
`docs/OPERATIONS_RUNBOOK.md`, `docs/DEMO_SEED_DATA.md`.

## Boundaries

Do **not** implement, and do not casually recommend as "quick wins":
forecasting · supplier management · purchase orders · new schema · new
dependencies · payment redesign · inventory redesign · shift redesign.

Those become work only when the user explicitly asks for that specific branch.
This skill names gaps and sequences them; it does not build them.

Also: no application-logic changes, no schema changes, no test changes, no
dependency changes. Output is documents.

## The five perspectives

Audit each area through all five lenses; label findings with the lens that
raised them.

- **CTO** — architectural risk, coupling, operational risk, what breaks at 10
  stores, what has no owner, what cannot be debugged in production.
- **Expert PM** — is the job-to-be-done actually finished for each role? What
  does a real shop still do on paper or WhatsApp? What blocks a paying pilot?
- **Senior data engineer** — is the data captured correct, complete, timestamped,
  store-scoped, and queryable? Are ledgers append-only and reconcilable? Is there
  anything a report will need later that is being thrown away today?
- **Expert data analyst** — can the owner answer their real questions? Are the
  metrics defined unambiguously? Are charts honest (baselines, denominators,
  time zones, partial days)?
- **Senior UI designer** — clarity under real conditions: a tablet in a kitchen,
  a busy cashier, a phone at a table, one-handed use, glare, gloves.

## Audit checklist

Work through every section. For each, record: what exists, what was verified
(and how), what is missing, severity, and effort.

### 1. Installation readiness
Fresh-clone rehearsal against the README verbatim: `docker-compose up -d`,
`npm install`, `cd apps/api && python -m alembic upgrade head`,
`npm run seed:demo`. Every undocumented step is a finding. Check
`.env.example` completeness and that placeholders are placeholders.

### 2. Runtime browser flows
Run the stack, seed, and drive each surface by hand (there is no browser
automation). Note real observed behaviour, never assumed behaviour.

### 3. Customer QR flow (3001)
Token resolution to the right store/table, menu correctness and Turkish copy,
order submission, idempotent re-submission, tampered-token rejection, error and
empty states, mobile ergonomics, what a guest sees after ordering.

### 4. Kitchen live updates and history (3002)
Board load, live WebSocket arrival of new orders, status transitions persisting
through refresh, timing cards (queue/prep/time-to-ready), delay flagging,
reconnect behaviour after a dropped socket, and whether the kitchen can review
what already happened (history/shift recap) or only the live moment.

### 5. Cashier real-use flow (3004)
Open tables and bills, cash/card settlement, partial payments and remaining
balance, bounded refunds, over-refund refusal message, shift open/close with
counted amount and discrepancy, immutability of a closed shift. Judge it against
a real rush: how many taps per order, what happens with a mistake, what happens
mid-shift on a page reload.

### 6. Owner reporting and dashboard gaps (3003)
The operational dashboard is read-only aggregation of *today*. Audit what the
owner still cannot answer: yesterday vs today, week and month, per-product
performance, staff/shift comparison, waste and shrinkage trend, margin. Name
each missing report as a report, with its question and its grain.

### 7. Forecasting readiness
Do **not** design forecasting here — check whether the data would support it:
history depth, order timestamps, cancellation handling, recipe/ingredient
mapping, store scoping, seasonality signal, holiday/weather absence. Conclude
with a verdict: ready / not yet ready / blocked by X. Hand the design work to
[forecasting-analytics-architect](../forecasting-analytics-architect/SKILL.md).

### 8. Analytics chart quality
For each existing chart or metric tile: is the definition stated, the time zone
explicit, the partial-day handled, the axis honest, the comparison fair, the
empty state useful? Flag any number that cannot be traced to a source of truth.

### 9. UI and theme quality
A first-pass read only — depth belongs to
[ui-theme-review](../ui-theme-review/SKILL.md). Note inconsistency across the
four surfaces, low-contrast inputs, weak focus rings, and tablet ergonomics.

### 10. Data-quality risks
Ledger integrity (payments, inventory, order issues, kitchen timing), orphan and
drift risks, time-zone and clock assumptions, soft-delete and cancellation
semantics, seed data leaking into analytics, and anything a reconciler does not
cover. Run the four read-only reconcilers and report their output:

```bash
python scripts/reconcile_kitchen_timing.py
python scripts/reconcile_payments.py
python scripts/reconcile_inventory.py
python scripts/reconcile_order_issues.py
```

### 11. Commercial readiness
What blocks charging a real shop: onboarding a new store, multi-store
administration, staff account management, backup/restore, monitoring, support
path, pricing-relevant limits, legal/receipt requirements, and the honest
non-production gap list in `docs/PRODUCTION_READINESS.md` §14.

## Deliverables

Write documents under `docs/` (or where the user asks). Default set:

1. **`docs/PRODUCT_AUDIT_<YYYY-MM-DD>.md`** — findings per area, each with:
   perspective, observation, evidence (command output, screen, file reference),
   impact, severity (blocker / major / minor), and effort (S/M/L).
2. **`docs/PRODUCT_ROADMAP_PROPOSAL_<YYYY-MM-DD>.md`** — findings sequenced into
   phases, each phase a candidate branch with an explicit scope boundary and an
   explicit "not in this branch" list.

Rules for the write-up:

- Separate **verified** from **inferred**. If the stack was not run, say so at
  the top and mark every runtime claim as unverified.
- Every finding needs evidence or an explicit "not verified".
- No fabricated metrics, no invented user research.
- Rank ruthlessly. A roadmap where everything is P1 is not a roadmap.
- State what is *good* too — an audit that only lists faults misprices the work.
- End with a one-paragraph verdict: what SweetOps is ready for today
  (demo / pilot / paid pilot / production) and the shortest path to the next
  step.
