# Payment Settlement & Cashier Workflow

SweetOps production-grade payment settlement, cashier collection, and refund
system. This document is the source of truth for the financial-integrity design
introduced on `feat/payment-settlement-workflow`.

---

## 1. Original product gap

Before this branch an order carried a single `status` column drawn from the
kitchen preparation state machine (`NEW → IN_PREP → READY → DELIVERED /
CANCELLED`). There was **no representation of money**: no record of whether a
table had paid, how much, by whom, by what method, or whether anything had been
refunded. "Paid" could only have been faked by overloading a preparation status
(e.g. treating `DELIVERED` as "paid"), which is financially wrong — a waffle can
be delivered and unpaid, or paid and still in preparation. There was also no
ledger, no idempotency for money, no cashier UI, and analytics reported only
*ordered* value with no notion of *collected* cash.

## 2. Preparation / payment separation

Preparation state and payment state are **orthogonal dimensions**:

```
order.status         ∈ {NEW, IN_PREP, READY, DELIVERED, CANCELLED}   (fulfilment)
order.payment_status ∈ {UNPAID, PARTIALLY_PAID, PAID}                (money)
order.refund_status  ∈ {NONE, PARTIALLY_REFUNDED, REFUNDED}          (money)
```

Invariant: `order.status != order.payment_status`. Collecting payment never
changes preparation state; changing preparation state never marks an order paid.
The kitchen state machine is untouched except for one **guard**: an order with a
positive net paid amount cannot be cancelled (see §16).

## 3. Payment state definitions

Derived from the summary amounts (`net_paid = paid_amount − refunded_amount`):

| Condition | `payment_status` |
|---|---|
| `net_paid == 0` | `UNPAID` |
| `0 < net_paid < order_total` | `PARTIALLY_PAID` |
| `net_paid == order_total` | `PAID` |

`net_paid > order_total` is impossible — overpayment is rejected before any
write, and a DB-side guard re-checks it inside the transaction.

## 4. Refund state definitions

| Condition | `refund_status` |
|---|---|
| `refunded_amount == 0` | `NONE` |
| `0 < refunded_amount < paid_amount` | `PARTIALLY_REFUNDED` |
| `refunded_amount == paid_amount` (and `> 0`) | `REFUNDED` |

Because a refund lowers `net_paid`, a fully-refunded order returns to
`payment_status = UNPAID` while `refund_status = REFUNDED` — the two dimensions
tell the full story together.

## 5. Financial ledger architecture

An **append-only ledger** is the source of truth. Order-level summary columns
are a denormalised mirror, maintained inside the same transaction as each ledger
write for fast cashier/list queries. The ledger is never edited or deleted.

```
PaymentSettlement 1───* PaymentAllocation *───1 Order
        │                      │
        └──────* PaymentRefund ┘   (refund references settlement + allocation + order)
```

## 6. Settlement / allocation / refund model

**`payment_settlements`** — one cashier collection action:
`id, store_id, table_id, cashier_user_id, payment_method, currency,
gross_amount, status(COMPLETED|VOIDED), note, terminal_reference,
idempotency_key_hash, request_hash, created_at, completed_at`.

**`payment_allocations`** — the money applied to one order:
`id, settlement_id, order_id, amount, created_at`. A table settlement produces
one allocation per order.

**`payment_refunds`** — append-only reversal of collected money:
`id, store_id, settlement_id, allocation_id, order_id, amount, currency, reason,
refunded_by_user_id, idempotency_key_hash, request_hash, created_at`.

**Order summary mirror**: `payment_status, refund_status, paid_amount,
refunded_amount`. `paid_amount = Σ allocations`, `refunded_amount = Σ refunds`.

## 7. Money precision

All money is `NUMERIC(12,2)` in the DB and `Decimal` (quantised to 2 places,
`ROUND_HALF_UP`) in Python. **Binary floating point is never used for money.**
`orders.total_amount` remains `NUMERIC(10,2)` (its pre-existing checkout type)
and is the authoritative order value.

## 8. Order total source of truth

Payment always settles against `orders.total_amount`, the immutable snapshot
persisted at checkout from order-item pricing. Totals are **never** recomputed
from current menu prices, and a client-supplied total is **never** trusted — the
collect endpoints do not even accept a table-wide amount; the server computes
outstanding balances from locked ledger state.

## 9. Permissions

Named permissions (centralised in `app/core/permissions.py`): `payments:read`,
`payments:collect`, `payments:refund`.

| Role | read | collect | refund | owner:* | kitchen:orders:write |
|---|---|---|---|---|---|
| OWNER | ✅ | ✅ | ✅ | ✅ | ✅ |
| MANAGER | ✅ | ✅ | ✅ | ✅ | ✅ |
| CASHIER | ✅ | ✅ | ❌ | ❌ | ❌ |
| KITCHEN | ❌ | ❌ | ❌ | ❌ | ✅ |

CASHIER can read bills and collect money but **cannot refund** and has no
owner/kitchen access.

## 10. Store isolation

Every financial write derives `store_id = current_staff.store_id` and the
actor (`cashier_user_id` / `refunded_by_user_id`) from the session. Client
`store_id` in a query string or body is never read. A cross-store table, order,
settlement, or allocation returns a non-disclosing `404`. A Store-A user can
neither read, collect, nor refund Store-B transactions.

## 11. Cash and card behaviour

Methods: `CASH`, `CARD`, `OTHER`. For this branch both cash and external
card-terminal payments are recorded **only after** the real-world collection
succeeds (there is no gateway integration and no asynchronous state). An
optional non-sensitive `terminal_reference` may be stored.

## 12. PCI-sensitive data exclusion

SweetOps **does not process or store card credentials**. No PAN, card number,
CVV, magnetic-stripe/track data, or cardholder data is accepted or persisted.
The only card-related field is an optional, non-sensitive terminal reference.

## 13. Table settlement workflow

`POST /cashier/settlements` with `{table_id, order_ids[], payment_method,
note?}`. The server: derives the store from the session; verifies the table and
every order belong to it; locks the orders `FOR UPDATE`; computes the exact
outstanding balance; creates one settlement + one allocation per order; returns
one receipt. No client amount is accepted.

## 14. Partial payment workflow

`POST /cashier/orders/{order_id}/payments` with `{payment_method, amount?}`.
Omitting `amount` collects the full outstanding balance; supplying it collects a
positive partial that must not exceed the outstanding balance. A partial marks
`PARTIALLY_PAID`; a later payment completes the remainder to `PAID`.

## 15. Refund workflow

`POST /cashier/allocations/{allocation_id}/refunds` with `{amount, reason}`
(requires `payments:refund`). Only completed collected allocations are
refundable; the amount must be positive and within the refundable balance
(`allocation.amount − Σ its refunds`); the reason is mandatory; the actor comes
from the session. Refunds are append-only and never change preparation status.

## 16. Cancellation interaction

- An **unpaid** order follows the existing cancellation workflow unchanged.
- An order with **positive net paid** amount cannot be cancelled — the
  transition fails `409` with:
  `"Ödeme alınmış sipariş doğrudan iptal edilemez. Önce tahsilatın iade
  edilmesi gerekir."`
- Refunding an order does **not** auto-cancel it; a fully-refunded order
  (`net_paid == 0`) may then follow the normal cancellation lifecycle.
- Cancelling an unpaid order creates no payment or refund record. Stock and
  preparation semantics are preserved.

## 17. Idempotency and payload-mismatch protection

Every collection and refund requires an `Idempotency-Key` header. Only
`SHA256(key)` and `SHA256(canonical request payload)` are stored — never the
raw key. Per store, `(store_id, idempotency_key_hash)` is unique.

- **Same key + same payload** → the original completed result is returned, no
  second settlement/allocation, no double increment, no duplicate financial
  audit entry (a separate `PAYMENT_IDEMPOTENT_REPLAY` marker is logged).
- **Same key + different payload** → `409` with
  `"Aynı işlem anahtarı farklı bilgilerle kullanılamaz."`
- **Different key** → a new command, subject to current outstanding/refundable
  balance.

## 18. Concurrency protection

Collection and refund run in one transaction: selected orders are loaded and
locked `SELECT ... FOR UPDATE` in ascending id order (deterministic → no
deadlock), with `populate_existing=True` so the ORM reflects the freshly-locked
row rather than a stale identity-map copy. Outstanding/refundable balances are
computed from locked state, so two cashiers can never both collect the same
balance and concurrent refunds can never exceed the refundable amount. Proven by
real-PostgreSQL threaded tests.

## 19. Audit behaviour

Append-only audit events: `PAYMENT_COLLECTED`, `PAYMENT_REFUNDED`,
`PAYMENT_IDEMPOTENT_REPLAY`. The actor is always the authenticated staff member.
Payloads carry settlement id, order ids, method, amount, currency — never
session tokens, CSRF tokens, raw idempotency keys, passwords, or card data. An
idempotent replay never emits a second financial-mutation audit entry.

## 20. Cashier frontend

`apps/cashier-web` (Next.js) — a dedicated cashier app, preferred over granting
CASHIER access to owner-web. Access is allowed for OWNER/MANAGER/CASHIER and
denied for KITCHEN. Cookie-based auth reuses the secure staff session; the
session token is HttpOnly and **never** placed in browser storage. Mutations
send `credentials: include`, the CSRF token, and an `Idempotency-Key`. A focused
client utility (`src/lib/payment-idempotency.ts`) mints a cryptographically
secure UUID per command, reuses it across double-clicks and network uncertainty,
generates a new key when the command changes, and clears the attempt only on
confirmed success.

## 21. Turkish UX

All user-facing text is Turkish. Key labels: `Kasa, Açık Masalar, Sipariş Ara,
Masa, Sipariş No, Hazırlık Durumu, Ödeme Durumu, Sipariş Tutarı, Ödenen, Kalan,
Ödeme Al, Tüm Hesabı Kapat, Nakit, Kart, Tahsilat Başarılı, İşlem Geçmişi,
İade Et, Çıkış Yap`. Submission/refund states include `Ödeme kaydediliyor…`,
`İşlem sonucu doğrulanamadı. Aynı işlem güvenle tekrar denenebilir.`,
`Bu siparişin ödenecek bakiyesi bulunmuyor.`, `Bu işlem daha önce tamamlandı.`,
`Aynı işlem anahtarı farklı bilgilerle kullanılamaz.`, `İade nedeni`,
`İade tutarı`, `İade kaydediliyor…`, `İade işlemi tamamlandı.`,
`Bu işlem için iade edilebilir bakiye bulunmuyor.`,
`Bu işlem için iade yetkin yok.`.

## 22. Financial metric definitions

`GET /owner/payment-summary` (OWNER/MANAGER), store-scoped. This is **additive**
— it does not replace the existing `/owner/kpis` gross-revenue metric.

```
gross_order_value    = Σ orders.total_amount WHERE status <> 'CANCELLED'
collected_amount     = Σ payment_allocations.amount  (COMPLETED settlements)
refunded_amount      = Σ payment_refunds.amount
net_collected_amount = collected_amount − refunded_amount
outstanding_amount   = gross_order_value − net_collected_amount
```

Ordered value and collected money are different quantities: order totals are the
source of truth for gross order value; the ledger is the source of truth for
cash. Cancelled orders are excluded from `gross_order_value` (and cannot hold
collected money, since a paid order cannot be cancelled).

## 23. Reconciliation

`scripts/reconcile_payments.py` is **read-only**. It compares each order's
stored `paid_amount`/`refunded_amount` summary against the ledger
(`Σ completed allocations` / `Σ refunds`), supports `--store`, prints mismatches
(no credentials/card data), and exits non-zero when any mismatch exists. It
never rewrites financial history.

## 24. Migration and rollback

Single Alembic migration `b8c4d1e6f207` (revises `a7d3f9b21c05`):

- Adds order columns `payment_status, refund_status, paid_amount,
  refunded_amount` with safe server defaults; existing orders backfill to
  `UNPAID / NONE / 0 / 0` — **no prior payment is fabricated**.
- Creates `payment_settlements`, `payment_allocations`, `payment_refunds` with
  check constraints, FKs, and indexes (incl. per-store idempotency uniqueness).
- Installs the PL/pgSQL **integrity, immutability and total-check trigger
  layer** described in §§27–31 (cross-entity consistency, append-only
  enforcement, deferred settlement-total validation).
- `downgrade()` drops only payment schema; dropping the three ledger tables also
  drops their triggers, and the five trigger **functions** are then dropped
  explicitly. Order rows and their pre-existing columns are untouched. Verified
  upgrade → downgrade → re-upgrade leaves a single Alembic head and preserves
  existing orders.
- No cashier users or credentials are created. Alembic remains single-head.

## 25. Deployment workflow

1. Back up PostgreSQL.
2. `alembic upgrade head`.
3. Verify payment tables + constraints exist (`\d payment_settlements`, etc.).
4. Verify canonical roles/permissions (`permissions_for_role`).
5. Create/enable a CASHIER account via the existing
   `scripts/manage_staff_users.py` CLI (no default credentials).
6. Configure `STAFF_TRUSTED_ORIGINS` to include the cashier-web origin.
7. Verify HTTPS and Secure cookies in production.
8. Test cash collection.
9. Test card-terminal recording.
10. Test duplicate submit (same key → same receipt).
11. Test table-wide settlement.
12. Test partial payment.
13. Test manager refund.
14. Test cross-store denial (404).
15. Run `scripts/reconcile_payments.py`.
16. Confirm customer QR ordering still works.
17. Confirm kitchen workflow is unchanged.
18. Confirm preparation status and payment status remain independent.

Physical rollout is **not** complete until real cashier, terminal, and receipt
testing has been performed on-site.

## 26. Tests and verification

Backend (real PostgreSQL): money/model integrity & constraints, migration
backfill, permission matrix, store isolation, single/partial/full-table
settlement, receipts, idempotency (replay + payload-mismatch), concurrency
(threaded: double-collect, over-partial, full-table, over-refund), refunds,
cancellation interaction, search/bill, analytics, reconciliation. Frontend:
`node --test` unit tests for the cashier idempotency utility; production builds
of all four Next.js apps.

Database-boundary tests added in this hardening pass (all bypass the service and
drive raw SQL against real PostgreSQL):

- `test_payment_db_integrity.py` — the eight cross-entity rejections of §§28–30.
- `test_payment_immutability.py` — UPDATE/DELETE of settlements, allocations and
  refunds are refused; the guarded bypass is proven default-closed (§31).
- `test_payment_total_integrity.py` — deferred settlement-total trigger: matching
  total commits; lower / higher / missing / mismatched multi-order totals fail
  at COMMIT (§32).
- `test_payment_currency.py` — a client cannot set or override currency; receipt
  and analytics report the server-derived TRY (§33).
- `test_payment_recollection.py` — fully-refunded vs never-paid distinction and
  the one-click recollection guard (§34).

No invariant below is described as "guaranteed" unless a test in this suite
exercises it.

## 27. Database-enforced financial integrity (overview)

Application-layer validation in `payment_service` is the first line of defence,
but it is **not** the last. The database enforces the following invariants
independently, so a bug, a script, or a direct `psql` session cannot persist an
internally inconsistent financial record. All are implemented as PL/pgSQL
triggers installed by migration `b8c4d1e6f207` and every violation raises
SQLSTATE `23000` (surfaced to the driver as `IntegrityError`):

| Guarantee | Mechanism | Trigger |
|---|---|---|
| settlement ↔ table/cashier store | `BEFORE INSERT` refs check | `trg_settlement_refs` |
| allocation ↔ order store & table | `BEFORE INSERT` refs check | `trg_allocation_refs` |
| refund ↔ allocation/settlement/order/store/currency | `BEFORE INSERT` refs check | `trg_refund_refs` |
| gross_amount == Σ allocations | `DEFERRABLE INITIALLY DEFERRED` constraint trigger | `trg_settlement_total_on_settlement` / `_on_allocation` |
| append-only (no UPDATE/DELETE) | `BEFORE UPDATE OR DELETE` block | `trg_*_immutable` (×3) |

Design note — declarative vs. trigger. The nullable `table_id` on both
settlements and orders means a composite foreign key would fall back to `MATCH
SIMPLE` (unenforced when any column is NULL), which would silently weaken the
table-consistency guarantee for a table-less single-order settlement. The
trigger checks use null-safe `IS [NOT] DISTINCT FROM` comparisons, which are
strictly stronger, so triggers are preferred here. The guarantees hold in both
directions and are covered by direct-SQL tests.

## 28. Settlement ↔ table / store / cashier consistency

`trg_settlement_refs` runs before every settlement insert and enforces:

```
settlement.table_id IS NULL OR table.store_id  == settlement.store_id
                                cashier.store_id == settlement.store_id
```

A settlement can therefore never reference a table belonging to another store,
nor be attributed to a cashier who does not belong to the settling store — even
if the service layer were bypassed. `store_id` and `cashier_user_id` in the
service always come from the authenticated session, never the request body.

## 29. Allocation ↔ order / settlement consistency

`trg_allocation_refs` runs before every allocation insert and enforces, against
the parent settlement:

```
order.store_id == settlement.store_id
order.table_id IS NOT DISTINCT FROM settlement.table_id   (null-safe)
```

This makes both of the following impossible at the database level:

```
Store A settlement → Store B order      (rejected: store mismatch)
Table 1 settlement → Table 2 order      (rejected: table mismatch)
```

No redundant `store_id`/`table_id` columns are added to allocations; the trigger
reads the authoritative values from the locked `orders` and `payment_settlements`
rows, so nothing here is client-controlled.

## 30. Refund relationship consistency

`trg_refund_refs` runs before every refund insert and enforces that the refund's
five references all describe **one** original financial allocation:

```
allocation.settlement_id == refund.settlement_id     (allocation belongs to settlement)
allocation.order_id      == refund.order_id           (order matches allocation)
settlement.store_id      == refund.store_id           (store consistency)
settlement.currency      == refund.currency           (currency consistency, §33)
```

A refund can never stitch together an allocation from settlement A, a different
settlement B, and an unrelated order C, nor be booked against the wrong store or
currency.

## 31. Ledger immutability (append-only)

`payment_settlements`, `payment_allocations` and `payment_refunds` are
**append-only**. `trg_*_immutable` fire `BEFORE UPDATE OR DELETE` on each table
and refuse the operation. Consequences:

- a completed settlement can never be edited or deleted;
- an allocation can never be edited or deleted;
- a refund can never be edited or deleted;
- **corrections are new rows** — a reversal is a new `payment_refund`; there is
  no mutable state and no `VOIDED` transition (see §35).

No application-accessible bypass. The immutability guard is **unconditional**:
`sweetops_block_ledger_mutation()` honours no GUC, session variable, or any other
value the application role can set with `SET`, `SET LOCAL`, or `set_config`. An
earlier design honoured a custom GUC (`sweetops.ledger_admin`); that was
**removed** because a dotted custom GUC is settable by *any* role — including an
SQL-injection path inside an ordinary query — so it was never a real privilege
boundary. There is now no runtime switch that the application role, a compromised
service, an injection, or an accidental maintenance command can flip.

Rollback is not DELETE. A transaction **rollback** discards uncommitted work
without ever issuing an `UPDATE`/`DELETE`, so it never touches these triggers and
remains fully supported (`test_payment_immutability.py` proves an appended refund
that is rolled back simply disappears).

Exceptional corrections / maintenance. The only ways to alter existing ledger
history are (a) an **append-only reversal** (a new `payment_refund`, the normal
path) or (b) a **controlled database migration** / **ownership-gated trigger
administration** (`ALTER TABLE … DISABLE TRIGGER`) performed by a privileged role
outside the application runtime. `ALTER TABLE` requires table ownership, which
the ordinary application role does **not** hold in a correctly provisioned
deployment and which no `SET`/`set_config` can substitute for. The test suite
uses exactly this ownership-gated DDL for teardown cleanup, not any runtime GUC.
`test_payment_immutability.py` proves the refusal both with the trigger's normal
firing and with `SET LOCAL sweetops.ledger_admin='on'` / `set_config(...)` in the
same transaction — neither permits any UPDATE or DELETE.

## 32. Settlement total vs. allocation total

Every completed settlement must satisfy:

```
payment_settlement.gross_amount == SUM(payment_allocations.amount)
```

`payment_service` constructs the settlement and its allocations in one
transaction with equal totals, but reconciliation (§23) is a backstop, not the
first detector of a malformed settlement. A `DEFERRABLE INITIALLY DEFERRED`
constraint trigger (`sweetops_settlement_total_check`, attached to both
`payment_settlements` and `payment_allocations`) validates the sum **at COMMIT**.
This allows the natural write order — insert settlement → insert allocations →
commit — while rejecting, at commit time, any settlement whose parts do not add
up. Tested: matching total commits; lower, higher, missing, and mismatched
multi-order totals are all rejected.

## 33. Server-controlled currency (TRY)

SweetOps is **single-currency (TRY)** and the currency is **server-controlled**:

- request schemas (`SettlementCreateRequest`, `OrderPaymentRequest`,
  `RefundCreateRequest`) carry **no `currency` field** and are **strict**
  (`extra="forbid"`): a `currency` — or any other unknown field — injected into a
  financial-mutation payload is **rejected with a 422 validation error**, not
  silently dropped. There is therefore no accepted channel to supply a currency,
  and a client can never be misled into thinking an ignored override was honoured
  (see §36);
- the settlement currency is fixed by server configuration
  (`payment_service.DEFAULT_CURRENCY = "TRY"`), never by the client;
- a refund's currency is **derived** from its original settlement, and
  `trg_refund_refs` (§30) rejects any refund whose currency differs from the
  settlement;
- the cashier frontend never chooses currency; responses may *display* it;
- there is **no exchange conversion** anywhere.

Because `orders` carry no currency snapshot, "reject a full-table settlement with
mixed order currencies" is vacuously satisfied — there is only ever one currency
to settle in. This is the deliberate, documented single-currency invariant, not
a pretence of multi-currency support. Tested: a client cannot create a USD
settlement for a TRY order; a client cannot override refund currency; a
multi-order settlement is always TRY; receipt and analytics report the persisted
server currency.

## 36. Strict financial-mutation schemas

The three financial write schemas — `SettlementCreateRequest`,
`OrderPaymentRequest`, `RefundCreateRequest` — set Pydantic
`model_config = ConfigDict(extra="forbid")`. Any field not declared on the model
is **rejected with 422**, rather than silently discarded. This matters for money:
silently dropping an injected `currency`, `gross_amount`, or `amount` override
would let a client believe a financial instruction was accepted when it was
ignored. Forbidding extras makes the contract explicit and keeps the server the
sole authority over currency (TRY) and computed balances.

Backward compatibility: valid requests are unchanged — every field the documented
contract already accepts (`payment_method`, `order_ids`, `table_id`, `note`,
`terminal_reference`, refund `amount`/`reason`, optional partial-payment
`amount`) still works. Only *unexpected* fields are now refused. Coverage:
`test_payment_schema_strictness.py` proves settlement/refund `currency` and an
arbitrary unknown field are rejected while valid settlement, order-payment, and
refund requests still succeed.

## 37. Trigger-function security posture

Every payment trigger function installed by `b8c4d1e6f207` is hardened:

- **`SECURITY INVOKER`** (the default) — the triggers need no elevated privilege,
  so none is taken; there is no `SECURITY DEFINER` function and thus no owner-
  privilege surface to abuse.
- **Fixed `search_path = pg_catalog`** on each function plus **schema-qualified**
  references to every table (`public.tables`, `public.users`, `public.orders`,
  `public.payment_settlements`, `public.payment_allocations`), so object
  resolution can never be diverted by an attacker-controlled `search_path`.
- **No dynamic SQL** and **no client value interpolated** into executable SQL;
  every check is a static statement over `NEW`/`OLD`.
- **`EXECUTE` revoked from `PUBLIC`** on all five functions — only the trigger
  machinery invokes them.
- **Stable SQLSTATE** `integrity_constraint_violation` (23000) on every raise,
  with no secret data in the message text.
- **Clean downgrade** — `downgrade()` drops every payment trigger and trigger
  function explicitly *before* dropping the payment tables
  (`test_payment_migration.py` proves downgrade and re-upgrade reinstall the
  immutability triggers).

## 34. Fully-refunded vs. never-paid orders

A fully-refunded order has `net_paid == 0`, exactly like a never-paid order, but
it is **not** operationally the same and must never be mistaken for one:

| | `payment_status` | `refund_status` | `net_paid` |
|---|---|---|---|
| Never paid | `UNPAID` | `NONE` | 0 |
| Fully refunded | `UNPAID` | `REFUNDED` | 0 |

Behaviour in this branch:

- `refund_status` is exposed on the order detail and every table-bill line, so
  the cashier UI and API can visibly distinguish the two states even though both
  read net-zero;
- a fully-refunded order still carries an outstanding balance, so the **generic
  one-click "settle whole table" flow refuses to recollect it** — it returns a
  specific `409 refunded_recollect`. Recollection must be an **explicit
  per-order action** through `POST /cashier/orders/{id}/payments`, which the
  operator selects deliberately (the confirmation path);
- preparation status is unchanged by any of this;
- existing cancellation rules (§16) still apply — a fully-refunded order returns
  to `net_paid == 0` and so becomes cancellable again.

No discounts or comping are introduced. Regression coverage lives in
`test_payment_recollection.py`.

## 35. Settlement status — COMPLETED only

The settlement `status` domain is **`COMPLETED` only**; the previously-present
`VOIDED` value has been removed (DB check constraint `ck_settlement_status_domain`
now reads `status IN ('COMPLETED')`). Cash and card entries are recorded *after*
the real-world collection has already succeeded, so there is no pending/void
lifecycle to model, and — given ledger immutability (§31) — a completed row could
never be mutated into a void anyway. Any reversal is a separate, immutable
`payment_refund` record. An unused mutable state is not retained for hypothetical
future use.

## 36. Lockfile and clean-install verification

The branch shows a large `package-lock.json` rewrite
(`+5084 / −11749` lines). It was audited rather than trusted:

- **Cause.** Both `main` and this branch use `lockfileVersion 3`. `main`'s
  lockfile had **1108** package entries with per-workspace **nested**
  `node_modules/*` duplicates (an older npm that hoisted less) and only
  Windows-platform binaries; this branch's lockfile has **510** entries because
  a newer npm (**npm 11.8.0**, Node **v24.13.1**) **hoisted** shared transitive
  dependencies to the root and normalised the platform-binary set. The deletion
  count is large purely because ~980 nested duplicate entries collapsed into
  ~408 hoisted root entries — a representation change, not a dependency change.
- **No dependencies removed.** A key-set diff shows **0** root-level packages
  removed and no workspace `package.json` altered except the additive new
  `apps/cashier-web/package.json` and the one added `dev:cashier` script; every
  pre-existing direct dependency remains.
- **All six workspaces present** in the lockfile: `customer-web`, `kitchen-web`,
  `owner-web`, `cashier-web`, `packages/types`, `packages/ui`.
- **`packageManager` pin added** — `"packageManager": "npm@11.8.0"` in the root
  `package.json` records the intended npm version so future installs do not
  reintroduce version-driven churn.
- **Clean install proven.** In a disposable `git worktree` at the current commit
  (not the developer's `node_modules`), `npm ci` installed 423 packages cleanly
  from the lockfile; `build:types`, `build:ui`, the `customer-web` (32) and
  `cashier-web` (8) `node --test` suites, and production `next build` of all four
  web apps all succeeded. `npm audit fix --force` was **not** run.

## 37. Deferred (out of scope for this branch)

- **External payment gateway** — no online card processing / gateway integration.
- **Cash-drawer shift management** and **end-of-day cash reconciliation**.
- **Receipt-printer integration**.
- **Online customer payment** — customer QR ordering remains payment-independent.
- **Line-item split billing**, **store-scoped inventory / inventory lifecycle
  redesign**, and **Turkish analytics-wide localization** are likewise deferred.
