# Turkish User-Facing Localization

SweetOps is used by Turkish customers, cooks, cashiers and owners. Before this
change the product was bilingual by accident: the customer menu was Turkish, the
owner dashboard was almost entirely English, and the kitchen board printed raw
enum values (`IN_PREP`) at people mid-service. This document records the standard
that ended that, and — just as importantly — where the line is drawn.

This is a **copy standard, not an i18n framework**. There is no message catalog
keyed by locale, no language switcher, and no runtime locale negotiation. Turkish
is the product's language; the strings live where they are used. See
[Deferred](#deferred) for what that consciously postpones.

## Scope

User-facing text is Turkish across:

* the customer ordering flow (QR gate, menu, cart, submission, success),
* the kitchen board (statuses, actions, connection state, empty states),
* the cashier POS (open tables, bill, collection, refunds, transaction history),
* owner/manager inventory and analytics surfaces (KPIs, decisions, metrics, charts),
* auth and permission errors,
* inventory errors, including store-to-store transfers,
* payment, settlement and refund errors,
* QR / menu / order validation messages,
* loading, empty, success and failure states.

## The boundary

The single rule that governs everything else:

> **The API speaks English. The product speaks Turkish. The presentation layer is
> where one becomes the other.**

These stay English, permanently, and are **not** copy:

| Category | Examples |
| --- | --- |
| Enum values on the wire | `TRANSFER_OUT`, `PAID`, `IN_PREP`, `stock_risk` |
| Machine-readable error codes | `no_balance`, `idempotency_mismatch`, `forbidden` |
| API route paths | `/cashier/settlements`, `/public/orders/` |
| Database columns and SQL objects | `on_hand_quantity`, `ingredient_stocks` |
| Migration revisions | `f4b8c1d90e26_inventory_transfer_workflow` |
| Python / TypeScript identifiers | `_action_hint`, `movementTypeLabel` |
| Test function names | `test_action_hint_is_can_wait_for_fresh_order` |
| Internal comments and docstrings | throughout |

An English identifier is not a bug. An English *sentence in front of a user* is.

## Terminology standard

Pick the product-correct term and never alternate. The left column is what the
code and the database call it; the right column is the only Turkish word the
product uses for it.

| Concept | Turkish |
| --- | --- |
| Order | Sipariş |
| Table | Masa |
| Store / branch | **Şube** (never "mağaza") |
| Kitchen | Mutfak |
| Cashier | Kasa (the person: Kasiyer) |
| Owner / Manager | Yönetici |
| Staff | Personel |
| Payment (general, user-facing) | Ödeme |
| Settlement (recording money in) | **Tahsilat** |
| Refund | İade |
| Inventory | Stok |
| Ingredient | **Malzeme** (never "ürün") |
| Stock movement | Stok hareketi |
| Reservation | Ayrılan stok |
| Consumption | Tüketim |
| Waste | Fire |
| Manual adjustment | Manuel düzeltme |
| Purchase receipt | Mal kabul |
| Transfer | Transfer |
| Transfer out | Şubeden çıkış |
| Transfer in | Şubeye giriş |
| Available stock | Kullanılabilir stok |
| On-hand stock | Fiziksel stok |
| Reserved stock | Ayrılmış stok |
| Stockout risk | Stok tükenme riski |
| Permission denied | Bu işlem için yetkiniz yok. |
| CSRF / origin failure | Güvenlik doğrulaması başarısız. |
| Idempotency conflict | Bu işlem farklı bilgilerle daha önce denenmiş. |

Two distinctions carry real operational weight and are not stylistic:

* **Stokta yok** means there is none at all. **Stok yetersiz** means there is
  some, but not enough. Collapsing them hides which problem the branch has.
* **Ödeme** is the general idea of paying. **Tahsilat** is the act of taking
  money at the till. The cashier screen records *tahsilat*.

### Address

All user-facing copy uses the formal second person (**siz**): "tekrar deneyin",
not "tekrar dene". The codebase previously mixed the two inside a single screen.
Formal was chosen because it is what a Turkish business says to its customers,
and because staff and customer surfaces then read as one product.

## Enum display mapping

Raw enum values must never reach a screen. Each app owns a `src/lib/labels.ts`
that maps wire values to Turkish, and every render goes through a helper:

```ts
// apps/owner-web/src/lib/labels.ts
export const MOVEMENT_TYPE_LABEL: Record<string, string> = {
  RESERVATION_CREATED:  "Stok ayrıldı",
  RESERVATION_RELEASED: "Ayrılan stok bırakıldı",
  CONSUMPTION:          "Tüketim",
  WASTE:                "Fire",
  RETURNED:             "İade edilen stok",
  MANUAL_ADJUSTMENT:    "Manuel düzeltme",
  PURCHASE_RECEIPT:     "Mal kabul",
  TRANSFER_OUT:         "Şubeden çıkış",
  TRANSFER_IN:          "Şubeye giriş",
};
```

Rules:

1. **The map is presentation only.** The value sent by the API is unchanged, and
   every comparison in the app is still made against the English value
   (`order.status === "IN_PREP"`, never against the label).
2. **Unknown values degrade to a safe word**, never to the raw enum. If the API
   introduces `PARTIALLY_REFUNDED`, a cashier sees `Bilinmiyor` — not an
   identifier they are expected to decode mid-shift. The real value is still in
   the network tab when an engineer needs it.
3. **Movement types are pinned by test.** `WASTE` and `TRANSFER_OUT` both reduce
   a branch's stock; `PURCHASE_RECEIPT` and `TRANSFER_IN` both increase it. Label
   a transfer as *Fire* and the owner sees waste that never happened; label it as
   *Mal kabul* and they see a purchase nobody made. `labels.test.ts` asserts those
   four stay distinct.

## Backend message rule

User-facing strings served by the API are centralized in
[`apps/api/app/core/messages.py`](../apps/api/app/core/messages.py). A message in
that file must:

* be Turkish, formal, and specific about what to do next,
* **never leak internals** — no `IntegrityError`, SQLSTATE, traceback, constraint
  name, table name, token, hash or raw idempotency key. That detail goes to the
  server log.

The message and the error code are separate contracts:

```python
raise HTTPException(
    status_code=409,
    detail={
        "error": "idempotency_mismatch",          # stable — clients & tests bind to this
        "message": messages.PAY_IDEMPOTENCY_MISMATCH,  # free to reword
    },
)
```

**Error codes are stable. Copy is not.** Reword the Turkish whenever it reads
better; renaming a code is a breaking API change.

Generic messages were removed on purpose. "Kayıt bulunamadı." told a cashier
nothing; it is now "Bu sipariş bulunamadı. Sipariş numarasını kontrol edin veya
açık masalardan seçin."

### Backend-generated display prose

Some user-facing text is *composed* by the backend rather than picked from the
catalog, because it interpolates live numbers. These are Turkish too:

* `kitchen_service` — `urgency_reason`, `action_hint`, kitchen-load `explanation`
  (read by cooks on the order card).
* `decision_engine` — decision `title`, `description`, `impact`,
  `recommended_action`, `why_now`, `expected_impact` (read by the owner).
* `metrics_service` — data-quality messages.
* `operational_context_service` — the reason lines in the owner banner.
* `owner_analytics` — stock status messages (already Turkish).

The keys these functions branch on (`"critical"`, `"NEW"`, `"stock_risk"`) remain
English: they are internal identifiers, not copy.

Decision rows are persisted, so rows written before this change keep their English
text until they are regenerated. This is cosmetic and self-healing; no backfill
migration was run for it.

## Frontend app coverage

| App | What changed |
| --- | --- |
| **customer-web** | Combo labels ("Most ordered today" → "Bugün en çok seçilen"), out-of-stock chip, cart/empty state, submit button. All failure copy now resolves through `lib/order-messages.ts`, which guarantees a customer never sees a status code or an English debug string — including the uncertain-network case, which must *not* imply the order was lost. |
| **kitchen-web** | The status badge rendered `{order.status}` raw (`NEW`, `IN_PREP`); it now goes through `orderStatusLabel`. Connection states, action button, empty state and the update-failure alert are Turkish. An `error` state was tracked but never rendered — the board could go stale silently — so it now shows a Turkish banner. |
| **cashier-web** | The bill rendered `preparation_status` raw. Payment/refund/preparation status, payment method and transaction kind now all go through `lib/labels.ts`. Refund status is surfaced on the bill. Collection copy standardized on *tahsilat*. |
| **owner-web** | The largest surface: it was almost entirely English. Header, KPI cards, operations panel, decision cards (including a raw `{decision.status}` badge), metrics panel, attention banner, charts and the kitchen board are Turkish. The metrics panel also formatted money as **US dollars** (`$`, `en-US`) on a Turkish dashboard — now `₺` / `tr-TR`. |

## Tests

Focused, not exhaustive. The suites assert the *properties* that matter rather
than every string:

* `apps/owner-web/src/lib/labels.test.ts` — every movement type (transfer and
  lifecycle) has a Turkish label; transfers are never labelled as waste or as a
  purchase; no label is its own enum value; unknown values degrade safely.
* `apps/cashier-web/src/lib/labels.test.ts` — payment, refund and preparation
  statuses map to Turkish; unknown enums degrade to `Bilinmiyor`.
* `apps/kitchen-web/src/lib/labels.test.ts` — order and connection states map to
  Turkish.
* `apps/customer-web/src/lib/order-messages.test.ts` — no customer-facing failure
  string contains an English technical word or a status code; a network failure
  says it is safe to retry; the thrown error's own message is never surfaced.

Existing API tests that asserted the old English copy were updated in place. Each
kept its scenario and its claim — only the expected string was translated (for
example, `_action_hint(1, "NEW", "ok", 0.5, [])` still asserts the "can wait"
branch, now `"Bekleyebilir"`). No test's meaning was changed.

## Deferred

Explicitly **not** built here, and not implied by anything above:

* **Full i18n framework** (message catalogs, ICU/plural rules, `next-intl` or
  similar). Turkish strings live inline. If a second language is ever needed, this
  document's terminology table is the glossary to build the catalog from, and
  `labels.ts` is the natural seam to key by locale.
* **Language switcher** — no locale is negotiated, stored or toggled.
* **Multi-language support** — the product is Turkish-only.
* **Database-driven translations** — no schema change was made; no table holds copy.
* **Admin translation panel.**
* **Deep copy review with real users** — the tone here is the judgment of one pass,
  not something validated with cashiers and cooks in a live shop. Worth doing.
* **Visual UI redesign** — only strings changed; no layout, component or flow was
  touched.
