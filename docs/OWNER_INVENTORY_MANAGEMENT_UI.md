# Owner Inventory Management UI

The inventory backend has been complete for three branches now — store-scoped stock,
a reservation/consumption lifecycle, manual stock commands, and an atomic
store-to-store transfer — and none of it was reachable by the person it was built
for. A manager who wanted to record a delivery had to ask someone to call the API.

This branch puts that capability on a screen: **`/inventory` — "Stok Yönetimi"** in
owner-web.

Nothing about how stock *behaves* changed. No business logic, no payment behaviour,
no schema, no migration. This is a presentation layer over endpoints that already
existed, plus one small read-only endpoint the transfer form could not work without
(§ Backend changes).

---

## 1. Scope

The manager of a branch can now:

| Capability | Backed by |
|---|---|
| See current stock by ingredient | `GET /inventory/stock` |
| Tell physical, reserved and available stock apart | same |
| See stockout risk / low-stock warning | same (`reorder_level`) |
| See recent stock movements | `GET /inventory/movements` |
| Record a purchase receipt (mal kabul) | `POST /inventory/purchase-receipts` |
| Record waste (fire) | `POST /inventory/waste` |
| Record a manual adjustment | `POST /inventory/manual-adjustments` |
| Transfer stock to another branch | `POST /inventory/transfers` |
| Choose the destination branch by name | `GET /inventory/transfer-destinations` **(new)** |

The store is **never chosen in the UI**. It comes from the session, server-side.
There is no branch picker, and there is no way to point this screen at another
branch's stock — the absence of the parameter is the security property, and the UI
does not reintroduce it.

## 2. Screens and sections

`/inventory` is one page, gated by `AuthGate` like the rest of owner-web.

```
Stok Yönetimi · Şube: Kadıköy                                  ← Panel
──────────────────────────────────────────────────────────────────────
[ Mal kabul ] [ Fire kaydı ] [ Manuel düzeltme ] [ Şube transferi ]

  ✓ Mal kabul başarıyla kaydedildi.                              ✕

  STOK DURUMU
  ┌────────────┬──────────┬──────────┬───────────────┬───────┬──────────────┐
  │ Malzeme    │ Fiziksel │ Ayrılmış │ Kullanılabilir│ Birim │ Durum        │
  ├────────────┼──────────┼──────────┼───────────────┼───────┼──────────────┤
  │ Çikolata   │       10 │        3 │             7 │ kg    │ Stok yeterli │
  │ Antep fıst.│        8 │        8 │             0 │ kg    │ Stok yetersiz│
  └────────────┴──────────┴──────────┴───────────────┴───────┴──────────────┘

  STOK HAREKETLERİ                              Hareket türü: [ Tümü ▾ ]
  Tarih · Malzeme · Hareket türü · Miktar · Fiziksel etki · Ayrılmış etki
  · Açıklama · İşlemi yapan
```

Files:

| File | Role |
|---|---|
| `apps/owner-web/src/app/inventory/page.tsx` | The screen: loads data, holds the result banner, opens the action dialog. |
| `apps/owner-web/src/components/inventory/StockOverviewTable.tsx` | Stock table. |
| `apps/owner-web/src/components/inventory/MovementHistoryTable.tsx` | Movement ledger + type filter. |
| `apps/owner-web/src/components/inventory/InventoryActionModal.tsx` | All four operation forms. |
| `apps/owner-web/src/lib/inventory-api.ts` | Typed client. Session, CSRF, `Idempotency-Key`. |
| `apps/owner-web/src/lib/inventory-idempotency.ts` | Key policy (fingerprint → key). |
| `apps/owner-web/src/lib/inventory-errors.ts` | Error code → Turkish copy. |
| `apps/owner-web/src/lib/inventory-view.ts` | Presentation: stock status, row builders, validation, banners. |

`lib/inventory-view.ts` is the choke point worth knowing about. Components never
receive a raw API row — they receive a `StockRow` / `MovementRow` whose fields are
already Turkish and already formatted. There is no `movement_type` field left on a
`MovementRow` for a table cell to print by accident.

## 3. Stock overview

Displayed per ingredient: **Malzeme · Fiziksel stok · Ayrılmış stok ·
Kullanılabilir stok · Birim · Durum**.

Status is derived from the API's own figures — the frontend never recomputes stock:

| Condition (from the API) | Label |
|---|---|
| `on_hand <= 0` | **Stokta yok** |
| `available <= 0` (but on-hand > 0) | **Stok yetersiz** |
| `available <= reorder_level` | **Düşük stok** |
| otherwise | **Stok yeterli** |

`Stokta yok` and `Stok yetersiz` are deliberately not merged. The first means the
shelf is empty. The second means the shelf is *not* empty — every unit on it is
already promised to an accepted order. A manager who reads "stokta yok" for the
second case goes and buys stock they already have; one who reads "stok yetersiz"
goes and looks at the order book. Any row held down by reservations also carries
*"Ayrılan stok bekleyen siparişler için tutuluyor"*, and any at-risk row carries
*"Stok tükenme riski"*.

Empty state: **"Bu şube için henüz stok tanımlanmamış."** — followed by *"Şubenizin
stok tanımları oluşturulduktan sonra malzemeler burada görünecek."*

That hint deliberately does **not** say "record a purchase receipt to create your
stock", even though the router's docstring describes a receipt as how a new branch
gets its opening stock. In the code as it stands, `record_purchase_receipt` goes
through `_load_stock_for_update`, which loads and **locks an existing** stock row and
404s `stock_not_configured` when there isn't one. So a receipt cannot create the
first row for an ingredient — seed data or a migration does — and a manager sent to
the form would be refused for the same reason. The ingredient picker in every
operation therefore offers exactly the ingredients that already have a stock row in
this branch: the set the backend will accept, and no more.

(Left as-is on purpose: making a purchase receipt able to create its own stock row is
an inventory business-logic change, which this branch is not permitted to make. It is
worth a follow-up — the docstring and the behaviour disagree.)

## 4. Movement history

The append-only ledger, newest first, filterable by movement type. Columns:
**Tarih · Malzeme · Hareket türü · Miktar · Fiziksel stok etkisi · Ayrılmış stok
etkisi · Açıklama · İşlemi yapan**.

Movement types are mapped through `lib/labels.ts` (`movementTypeLabel`), which the
Turkish localization branch established:

| Wire value | Displayed |
|---|---|
| `RESERVATION_CREATED` | Stok ayrıldı |
| `RESERVATION_RELEASED` | Ayrılan stok bırakıldı |
| `CONSUMPTION` | Tüketim |
| `WASTE` | Fire |
| `RETURNED` | İade edilen stok |
| `MANUAL_ADJUSTMENT` | Manuel düzeltme |
| `PURCHASE_RECEIPT` | Mal kabul |
| `TRANSFER_OUT` | Şubeden çıkış |
| `TRANSFER_IN` | Şubeye giriş |

An unknown type renders as **"Diğer stok hareketi"**, never as the raw value.
Movements the system booked itself (a reservation, a consumption) are attributed to
**"Sistem"** and explained by the order that caused them ("512 numaralı sipariş")
rather than an invented reason.

The one place a raw enum still exists in the UI is the *value* of a filter
`<option>` whose visible text is Turkish — the API filters by `TRANSFER_OUT`, not by
"Şubeden çıkış". The manager never sees it.

## 5. Supported operations

All four open in one dialog and all four are idempotent (§ 7).

**Mal kabul** — Malzeme, Miktar, Sebep/açıklama (optional, as the API allows).
→ *"Mal kabul başarıyla kaydedildi."*

**Fire** — Malzeme, Miktar, Fire sebebi (**mandatory** — unexplained waste is
indistinguishable from shrinkage). If the write-off would eat into reserved stock the
backend refuses (`insufficient_on_hand`) and the manager is told why, not merely that
it failed: *"Fiziksel stok yetersiz. Ayrılmış stok bekleyen siparişler için
tutuluyor; bu miktar düşülemez."*
→ *"Fire kaydı başarıyla oluşturuldu."*

**Manuel düzeltme** — Malzeme, signed Düzeltme miktarı (+ adds, − writes off),
Sebep (mandatory). Zero is refused: a correction that changes nothing is noise in the
one ledger an auditor reads. The form states what it is for and what it is not:
*"Manuel düzeltme, fiziksel sayım farkını düzeltmek içindir. Şubeler arası stok
hareketleri için manuel düzeltme yerine transfer kullanın."* — correcting two
branches with two manual adjustments loses the link between them and makes the stock
look destroyed here and bought there.
→ *"Manuel düzeltme başarıyla kaydedildi."*

**Şube transferi** — Malzeme, Hedef şube (from the new destinations endpoint),
Miktar, Sebep, Not (optional).
→ *"Transfer tamamlandı."*

## 6. Permissions

| Role | Sees `/inventory` | Can operate |
|---|---|---|
| OWNER | yes (`inventory:read`) | yes (`inventory:adjust`) |
| MANAGER | yes | yes |
| KITCHEN | — blocked from owner-web by `AuthGate` (`ALLOWED_ROLES`) | no |
| CASHIER | — blocked from owner-web; holds no inventory permission at all | no |

A session holding `inventory:read` but not `inventory:adjust` gets the tables and no
action buttons, plus *"Stok bilgilerini görüntüleyebilirsiniz, ancak stok işlemi
yapma yetkiniz yok."* No role has that shape today, but the permission matrix allows
it (KITCHEN is one grant away), so the UI handles it rather than rendering four
buttons that would 403.

Hiding the buttons is **courtesy, not the control**. Every route re-checks the
permission server-side, and a session with no store assignment is refused outright
(`no_store_assigned`) — there is no chain-wide inventory view to fall back to.

## 7. Idempotency

Every state-changing call carries an `Idempotency-Key`. This is not ceremony: a stock
command is not naturally repeatable. Pressing "Fire kaydet" twice does not confirm
one loss — it bins the pistachio twice, and the second row is indistinguishable in
the ledger from a real second loss.

The policy (`lib/inventory-idempotency.ts`, the same shape as cashier-web's
payment-idempotency, for the same reason):

* The command is **fingerprinted** — every field that changes what the backend
  persists is in the fingerprint.
* An **unchanged** command retried after a failure **reuses its key**, so a request
  that may already have landed cannot land twice.
* An **edited** command mints a **new** key. A manager who changes 2 kg to 5 kg means
  a different event; inheriting the completed key would replay the 2 kg receipt and
  report success.
* A **double-click** is swallowed outright (`alreadyInFlight`), so the second click
  never becomes a second request.
* The key is **never rendered** — it is a replay token — and lives in memory only.
  Neither it nor the session token is put in browser storage.

**A replay is reported as a replay, not as a second success.** When the backend
returns `idempotent_replay: true` the manager reads *"Bu mal kabul daha önce
kaydedilmiş. Yeni bir kayıt oluşturulmadı."* — saying "kaydedildi" a second time
would leave them believing two receipts exist.

## 8. Session, CSRF and error handling

Session and CSRF behave exactly as elsewhere in owner-web: the session is an HttpOnly
cookie sent by `credentials: "include"` (JavaScript never reads it), and every
mutation echoes the CSRF token from the readable cookie in `X-CSRF-Token`
(double-submit, via the existing `csrfHeaders()`). A 401 raises `UnauthorizedError`,
which `AuthGate` already turns into a re-login.

Error codes are mapped to Turkish in `lib/inventory-errors.ts`:

| Code | Shown |
|---|---|
| `stock_not_configured` | Bu malzeme için bu şubede stok tanımı bulunmuyor. |
| `insufficient_on_hand` | Fiziksel stok yetersiz. Ayrılmış stok bekleyen siparişler için tutuluyor; bu miktar düşülemez. |
| `insufficient_available` | Kullanılabilir stok yetersiz. Ayrılmış stok bekleyen siparişler için tutuluyor ve transfer edilemez. |
| `same_store_transfer` | Kaynak ve hedef şube aynı olamaz. |
| `destination_store_not_found` | Hedef şube bulunamadı. |
| `invalid_quantity` / `reason_required` | Stok miktarı sıfırdan büyük olmalı. / Bu stok işlemi için neden belirtmeniz gerekiyor. |
| `idempotency_mismatch` | Bu stok işlemi farklı bilgilerle daha önce denenmiş… |
| `forbidden` / `no_store_assigned` | Bu işlem için yetkiniz yok. / Hesabınız bir şubeye bağlı değil… |
| anything unknown | **İşlem tamamlanamadı. Lütfen tekrar deneyin.** |

If the backend sends a Turkish message for a code we do not know, it is displayed —
but only after passing `looksDisplaySafe()`, a conservative shape check that rejects
constraint names, exception classes, stack fragments, URLs and ALL_CAPS identifiers.
A proxy 502 or an unhandled exception can put `duplicate key value violates unique
constraint "ix_stock_store"` into that field, and a manager handed that has been
handed an internal to interpret. Raw JSON, stack traces, SQL errors and English
identifiers never reach the screen.

### Network uncertainty

If a **mutation** gets no answer at all (offline, timeout), that is **not** reported
as a failure — the stock may well have moved:

> İşlemin sonucu doğrulanamadı. Lütfen stok hareketlerini kontrol edin; aynı işlemi
> tekrar göndermeden önce sonucu doğrulayın.

This matters precisely *because* the endpoints are idempotent. A manager who reads
"başarısız" re-keys the form by hand, which mints a **new** key and genuinely doubles
the movement. So: check the ledger first. The form is left intact and the attempt's
key is preserved, so an unchanged resubmit is de-duplicated. A failed **read**, which
changed nothing, is stated plainly as a failure.

## 9. Data integrity

The backend remains the source of truth for stock.

* `available` is **displayed** as the API's `available_quantity` (a generated column,
  `on_hand − reserved`). It is never derived in the browser and never used to
  authorize anything.
* Quantities stay **strings** end to end. They are formatted for reading, never
  re-added — JS floats are not a stock-grade number type.
* The transfer form refuses a same-store destination and a quantity above displayed
  available stock. Both are **courtesy validation**: they spare a round-trip, and the
  server re-decides regardless (`same_store_transfer`, `insufficient_available`). A
  client that skipped them entirely still could not ship reserved stock.
* After any successful operation the page **re-reads** stock and movements rather
  than patching local state from the receipt.

## 10. Backend changes

One, and only one: **`GET /inventory/transfer-destinations`**.

A manager filling in "Şube transferi" has to name the branch the van is going to, and
no endpoint in the API would tell them a sibling branch exists — the transfer form
could not be built without it. It returns `store_id`, `name`, `location` for every
store except the caller's own, behind `inventory:read` and a store-assigned session.

It is deliberately **not** a store-management API: read-only, no create/update, and
it carries no stock, staff, takings or table data about the other branch. Filtering
out the caller's own store is a usability courtesy — `transfer_stock` still rejects a
same-store transfer server-side, and `test_inventory_transfer_destinations.py`
asserts that the service still refuses even if a client ignores the list.

No inventory business logic, no payment behaviour, no schema and no migration were
touched.

## 11. Turkish terminology

Fixed by `docs/TURKISH_USER_FACING_LOCALIZATION.md` and used verbatim here:

Stok · Şube · Malzeme · Fiziksel stok · Ayrılmış stok · Kullanılabilir stok · Stok
hareketi · Mal kabul · Fire · Manuel düzeltme · Transfer · Şubeden çıkış · Şubeye
giriş · Stok tükenme riski · Stokta yok · Stok yetersiz · Düşük stok · Stok yeterli

## 12. What remains backend-only

Reachable through the API but not surfaced on this screen:

* **Transfer history** (`GET /inventory/transfers`, `GET /inventory/transfers/{id}`).
  The client functions and the `TransferRow` view model exist and are tested; the
  table is not rendered on the page. The transfer's two legs already appear in the
  movement ledger as *Şubeden çıkış* / *Şubeye giriş*, so nothing is invisible — a
  dedicated inbound/outbound view is a second cut.
* **Store-scoped reconciliation** — an operator/CI concern, not a manager's screen.
* **The reservation/consumption lifecycle** — driven by orders, visible here only as
  movements. Nothing in the UI reserves or consumes stock.

## 13. Deferred

Explicitly out of scope for this branch, and not implemented:

* full inventory UI redesign
* transfer approval workflow
* in-transit transfer state
* transfer cancellation
* supplier management
* purchase-order management
* lot / expiry tracking
* barcode scanning
* physical count (sayım) workflow

## 14. Tests

Backend — `apps/api/tests/test_inventory_transfer_destinations.py`: the list's
contents, the exclusion of the caller's own store, the absence of any other
operational field, the permission matrix (owner/manager yes, cashier no, anonymous
no, storeless no), and a re-assertion that the *service* still refuses a same-store
transfer.

Frontend — `npm run test --workspace=owner-web` (Node's built-in runner, no DOM):

* `src/lib/inventory-api.test.ts` — an `Idempotency-Key` on purchase receipt, waste,
  manual adjustment and transfer; cookies + `no-store` on every call; no
  `source_store_id` in any body; a keyless mutation refused before it reaches the
  network; an API refusal surfacing its stable code; a mutation with no answer
  raising *uncertain*, not *failed*.
* `src/lib/inventory-errors.test.ts` — Turkish for insufficient stock and
  `stock_not_configured`; no message leaking a code or enum; unknown codes degrading
  to one calm line; technical server messages suppressed; real `messages.py` copy
  passing `looksDisplaySafe`.
* `src/lib/inventory-view.test.ts` — the four stock statuses (including "full
  reserved is *Stok yetersiz*, not *Stokta yok*"); no raw enum in any movement cell,
  swept across every movement type; unknown types degrading safely; the empty-stock
  state; the transfer success banner; replay reported as replay; transfer form
  validation including the same-store rejection.
* `src/lib/labels.test.ts` (pre-existing) already pins every movement type to Turkish
  and unknown values to `Bilinmiyor`.
