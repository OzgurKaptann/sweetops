# Order Issue & Controlled Refund Workflow

## 1. Why order issues exist

SweetOps already records ordering, an append-only payment/refund ledger, an
inventory lifecycle, cashier shift reconciliation and stock operations. What it
lacked was a controlled, auditable way to handle the everyday reality of a Turkish
multi-branch waffle shop:

```
Müşteri siparişi iptal etti.
Ürün hazırlanmadı.
Ürün hazırlandı ama müşteri vazgeçti.
Sipariş yanlış hazırlandı.
Ürün eksik verildi.
Sipariş ödendi ama iade gerekiyor.
Sipariş kısmen iade edilecek.
Sipariş iptal edilecek ama stok geri dönmeli mi?
```

Without a first-class issue workflow, staff would cancel orders, refund money and
adjust stock in disconnected ways, and three things would break:

1. the payment/refund ledger becomes hard to explain,
2. inventory gets released or consumed incorrectly,
3. cashier shift closing shows totals without operational context.

**Core principle:** an order issue *coordinates* the existing systems. It never
bypasses the payment settlement ledger, the refund ledger, the inventory lifecycle,
the cashier shift snapshots, the audit log, or store-scoped authorization. Every
action is explicit, auditable, idempotent, and Turkish in the UI.

## 2. Issue lifecycle

```
                        ┌──────────────────────────────────────────┐
   POST /orders/{id}/   │                 OPEN                      │
   issues        ──────▶│  problem recorded; no money, no stock     │
                        └───────────────┬──────────────────────────┘
                                        │  POST /order-issues/{id}/resolve
                                        ▼
                        ┌──────────────────────────────────────────┐
                        │               RESOLVED                    │
                        │  exactly one resolution applied, frozen   │
                        └──────────────────────────────────────────┘
```

Creation records **only the problem**. Resolution performs the cancellation and/or
refund effects, atomically. A resolved issue is immutable (a DB trigger freezes it).
`VOIDED` is reserved in the status domain for a future explicit voiding flow; the
current workflow implements `OPEN → RESOLVED` only.

## 3. Issue types (`issue_type`)

| Wire value           | Turkish label          |
|----------------------|------------------------|
| `CUSTOMER_CANCELLED` | Müşteri iptal etti     |
| `WRONG_ITEM`         | Yanlış ürün            |
| `MISSING_ITEM`       | Eksik ürün             |
| `QUALITY_PROBLEM`    | Kalite sorunu          |
| `DUPLICATE_ORDER`    | Çift sipariş           |
| `STAFF_ERROR`        | Personel hatası        |
| `OTHER`              | Diğer                  |

The English enum is the stable wire contract; the UI never renders it raw.

## 4. Resolution types (`resolution_type`)

| Wire value       | Turkish label       | Money            | Inventory / order                                   |
|------------------|---------------------|------------------|-----------------------------------------------------|
| `NO_REFUND`      | İadesiz çözüldü     | none             | none                                                |
| `CANCEL_ONLY`    | Sadece iptal        | none             | cancel order; release outstanding reservation       |
| `FULL_REFUND`    | Tam iade            | remaining refundable | refund, then cancel + release reservation       |
| `PARTIAL_REFUND` | Kısmi iade          | approved amount  | order stays active; no stock move                   |

`CANCEL_ONLY` is refused (409) on an order that still holds collected money — the
operator must use `FULL_REFUND` so the cash is actually returned, never silently
discarded. This mirrors the existing kitchen cancel guard.

## 5. Refund rules

The **payment refund ledger stays the single source of truth** for refunded money.
An issue resolution *creates* `payment_refunds` rows through
`payment_service.create_issue_refunds`; it never restates a refunded amount.

* Remaining refundable of an order = `net_paid = paid − refunded` (equal to the sum
  of every allocation's refundable balance).
* `FULL_REFUND` refunds the whole remaining refundable amount.
* `PARTIAL_REFUND` refunds the supplied `approved_refund_amount` (must be `> 0` and
  `≤ remaining refundable`).
* A refund that would exceed the remaining refundable amount is refused (409).
* An order paid across several settlements is refunded by distributing the amount
  across its allocations (ascending id); every resulting refund row carries the
  issue's id in `payment_refunds.order_issue_id`, and `order_issues.refund_id` links
  to the first (primary) one.
* Refund creation is idempotent *through* the issue resolution: replaying the
  resolve command returns the same result and creates no second refund; the same key
  with a different resolution payload → 409.

## 6. Inventory rules

Issue resolution reuses the existing inventory lifecycle primitive
(`inventory_service.release_order_reservation`) and invents no new movement type.

1. Reserved-but-not-consumed inventory: a `CANCEL_ONLY`/`FULL_REFUND` resolution
   **releases** the outstanding reservation (reserved falls; on-hand untouched).
2. Already-consumed inventory: cancellation **does not restore** stock — the batter
   the kitchen really poured stays spent. (`Hazırlanmış siparişin stoğu otomatik geri
   alınmaz.`)
3. Returning usable stock to the shelf remains a **separate, explicit, actor-
   attributed** movement and is **deferred** (see §15). Issue resolution never does it
   implicitly.
4. A refund never restores stock (money and stock are separate authorities).
5. Issue resolution never writes a manual-adjustment or stock-count movement.

## 7. Cashier shift interaction

A refund created by resolving an issue is an ordinary `payment_refunds` row, so it
flows through the **existing** shift attribution rule with no new shift logic:

* A refund inside an OPEN shift window is picked up by the close snapshot (refunds of
  the cashier's own collected money in `opened_at ≤ t < closed_at`).
* A CLOSED shift's snapshot is frozen by its trigger, so a refund taken after the
  close can never retroactively change what the shift reported.

See `docs/CASHIER_SHIFT_CLOSING.md`.

## 8. Idempotency

Both commands require an `Idempotency-Key`. Only SHA-256 hashes of the key and the
canonical payload are stored — never the raw key.

* **Creation** uniqueness is store-scoped (`uq_order_issue_store_create_idem`). Same
  key + same payload replays the original issue; same key + different payload → 409.
* **Resolution** writes onto the issue's own row, so its uniqueness is inherently
  issue-scoped. A replay of the resolve returns the same result (no second refund); a
  different payload under the same key → 409; a different key against an already-
  resolved issue → 409 `already_resolved`.

## 9. Audit

Two audit actions are recorded (append-only, in the same transaction as the change):

* `ORDER_ISSUE_CREATED` — `issue_id, order_id, store_id, issue_type,
  requested_refund_amount, created_by_user_id, reason`.
* `ORDER_ISSUE_RESOLVED` — `issue_id, order_id, store_id, resolution_type,
  approved_refund_amount, refund_id, resolved_by_user_id, reason`.

Never logged: the raw idempotency key, the request hash, the CSRF token, the session
token. (The money-movement `PAYMENT_REFUNDED` audit is written per refund row, as for
an ordinary till refund.)

## 10. Permissions

No new permission was introduced; the existing payment permissions are reused, which
preserves the established refund-authority boundary (a cashier never refunds).

| Action                                    | Permission required                    |
|-------------------------------------------|----------------------------------------|
| Read issues / issue history               | `payments:read`                        |
| Create an issue                           | `payments:collect`                     |
| Resolve `NO_REFUND` / `CANCEL_ONLY`       | `payments:collect`                     |
| Resolve `FULL_REFUND` / `PARTIAL_REFUND`  | `payments:collect` **and** `payments:refund` |

So a **CASHIER** (has `payments:read` + `payments:collect`) can record issues and
close them without money; a **MANAGER/OWNER** (also has `payments:refund`) approves the
actual refund. The refund-permission check is enforced inside the service, so the
endpoint accepts cashiers but refuses a refunding resolution from one (403). **KITCHEN**
has neither payment permission and cannot reach the issue endpoints at all; it sees a
cancelled order through the existing order status (see §13). No cross-store access:
the store comes only from the session and every read/write is store-scoped, with DB
composite foreign keys as the backstop.

## 11. Cashier-web behaviour

`OrderIssuePanel` opens from the payment panel of an order (the **Sorun** button on
each bill row):

* **Sorun kaydet** — a create form: issue type (Turkish dropdown), reason, note.
* Existing issues are listed with their Turkish status; an OPEN one shows resolve
  actions: **İadesiz çöz**, **Sadece iptal**, and — only when the operator holds
  `payments:refund` — **Tam iade** and **Kısmi iade** (with an amount field validated
  against the remaining refundable amount).
* The remaining refundable amount is shown. Success, error and *uncertain* states are
  Turkish; the uncertain copy asks the operator to check the order status rather than
  blind-retrying. Every enum is rendered through a label helper — no raw enum reaches
  the screen. Both commands send an `Idempotency-Key`.

The existing payment flow is untouched.

## 12. Owner-web behaviour

`Sorunlu siparişler` (`/order-issues`) is a store-scoped, read-only history table:
Sipariş, Sorun türü, Durum, Çözüm, İade tutarı, Oluşturan, Çözen, Tarih. A status
filter (Tümü / Açık / Çözüldü) is provided. Empty state and all labels are Turkish;
no raw enum is displayed. Owner-web never creates or resolves an issue.

## 13. Kitchen visibility

Kitchen-web was **not** modified. The existing order lifecycle already covers what
kitchen needs: a resolution that cancels an order sets its status to `CANCELLED`,
which the kitchen dashboard already renders as `İptal edildi`, and `CANCELLED` is a
terminal state the kitchen state machine already refuses to transition out of — so the
kitchen cannot continue a cancelled order. Kitchen has no payment permission and
therefore cannot refund. Adding a bespoke issue panel to kitchen-web would be
redundant with this existing behaviour, so it was deliberately omitted.

## 14. Reconciliation

`scripts/reconcile_order_issues.py` is a **read-only** check that every resolved issue
tells the same story as the refund ledger:

1. a FULL/PARTIAL refund resolution with a positive approved amount has a valid
   `refund_id` that links back to the issue,
2. the sum of `payment_refunds` linked to the issue equals `approved_refund_amount`,
3. every linked refund is the same store **and** the same order as the issue,
4. the issue's store equals its order's store,
5. a NO_REFUND / CANCEL_ONLY (or OPEN) issue has no linked refund,
6. the linked refunds never sum to *more* than the approved amount (no duplicate
   refund for one issue),
7. no order's total refunds exceed what was paid on it.

Exit code 0 when everything matches, 1 otherwise. It never mutates data and never
prints a credential.

## 15. Deferred (explicitly out of scope)

* returned-stock workflow (putting usable stock back on the shelf),
* customer wallet / store credit,
* coupons / loyalty points,
* chargeback workflow,
* bank reconciliation,
* accounting export,
* automatic compensation,
* forecasting, supplier management, purchase orders,
* delivery integration, POS hardware integration, kitchen quality scoring.

## 16. Database summary

`order_issues` (new): store/order/type/status/resolution, requested & approved refund
amounts, `refund_id`, reason, note, creator/resolver, timestamps, and created/resolved
idempotency hashes. CHECK constraints enforce the type/status/resolution domains,
non-negative amounts, the status⟺resolution-snapshot consistency rule (a half-resolved
row is unrepresentable), and the refund-link rules. Composite foreign keys tie the
issue to its order's store, the creator/resolver to the store, and a linked refund to
the same store **and** order. A `BEFORE UPDATE OR DELETE` trigger makes a resolved
issue immutable and refuses every delete, with no application-reachable bypass.

`payment_refunds` (altered): a nullable `order_issue_id` link column and a redundant
`uq_refund_store_order_id` unique constraint (so the issue's composite FK can prove
same-store, same-order). The refund ledger's immutability is unchanged.

`downgrade()` removes only this branch's schema and **refuses** while any issue exists
(dropping the table would destroy the record of why refunds were issued while the
refunds themselves remain). Alembic stays single-head; upgrade/downgrade/re-upgrade
are verified.
