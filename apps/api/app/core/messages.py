"""
User-facing Turkish messages.

Every string here is shown to a real person — a customer at a table, a cook, a
cashier or the owner. Rules for anything added to this file:

* Turkish, formal address ("siz"), warm but operationally precise.
* Say what happened and what to do next. "İşlem başarısız" tells nobody anything.
* Never leak internals: no exception class, SQLSTATE, constraint name, table
  name, token, hash or raw idempotency key. Technical detail belongs in the
  server log, not in a response body.
* Product vocabulary is fixed — see docs/TURKISH_USER_FACING_LOCALIZATION.md.
  Branch is "şube" (never "mağaza"), money taken in is "tahsilat", inventory
  items are "malzeme", "stokta yok" means none at all while "stok yetersiz"
  means not enough.

The machine-readable `error` code that travels next to each of these messages is
a separate, stable contract. Reword the Turkish freely; do not rename the codes.
"""

# ── Public QR / table context (customer-facing) ──────────────────────────────
# Invalid / unknown / malformed / revoked QR — one consistent response so a
# probing client cannot distinguish "never existed" from "revoked".
QR_INVALID = (
    "Bu masa bağlantısı geçersiz veya süresi dolmuş. "
    "Lütfen masadaki QR kodu tekrar okutun."
)

# Table or store is not currently open to ordering (inactive).
QR_UNAVAILABLE = (
    "Bu masa şu anda siparişe kapalı. Lütfen personelden yardım isteyin."
)

# QR token was expected but not supplied (e.g. a legacy order attempt in a
# production configuration that no longer trusts client-supplied context).
QR_REQUIRED = (
    "Sipariş verebilmek için masadaki QR kodu okutmanız gerekiyor."
)


# ── Staff authentication (owner-web / kitchen-web / cashier-web) ─────────────
# Generic invalid-credentials message. Deliberately does NOT reveal whether the
# username exists, the password was wrong, or the account is disabled.
AUTH_INVALID_CREDENTIALS = "Kullanıcı adı veya şifre hatalı."

# Account temporarily locked after too many failed attempts.
AUTH_ACCOUNT_LOCKED = (
    "Hesabınız çok fazla hatalı denemeden dolayı geçici olarak kilitlendi. "
    "Lütfen bir süre sonra tekrar deneyin."
)

# Not authenticated (missing/expired/revoked session) — 401.
AUTH_SESSION_EXPIRED = "Oturumunuzun süresi doldu. Lütfen yeniden giriş yapın."

# Authenticated but lacks permission for this area/action — 403.
AUTH_FORBIDDEN = "Bu işlem için yetkiniz yok."

# CSRF token missing or invalid — 403.
AUTH_CSRF_INVALID = (
    "Güvenlik doğrulaması başarısız. Lütfen sayfayı yenileyip tekrar deneyin."
)

# Request origin not among trusted staff origins — 403.
AUTH_ORIGIN_REJECTED = "Güvenlik doğrulaması başarısız. İstek kaynağı tanınmıyor."

# Login field validation.
AUTH_MISSING_FIELDS = "Kullanıcı adı ve şifre gerekli."


# ── Store scoping (staff-facing) ─────────────────────────────────────────────
# Fail-closed guard. Physical stock is store-scoped since the store-scoped
# inventory refactor, so this NO LONGER fires for staff inventory, owner
# analytics or the kitchen. It survives for the one genuinely storeless surface
# that is left: the UNGATED public menu reads (no QR token ⇒ no store context).
# Those cannot pick a branch's stock to report without guessing, so when more
# than one operational store exists they refuse rather than guess.
# See docs/STORE_SCOPED_INVENTORY.md § "Remaining limitation".
INVENTORY_MULTISTORE_BLOCKED = (
    "Stok bilgisi birden fazla şube için birlikte gösterilemiyor. "
    "Lütfen yöneticinizle iletişime geçin."
)

# A member of staff whose account is not attached to any store tried to reach a
# store-scoped inventory route. There is no "all stores" inventory view.
INVENTORY_NO_STORE_ASSIGNED = (
    "Hesabınız bir şubeye bağlı değil. Stok işlemleri için şube ataması gerekiyor."
)

# This store has never stocked this ingredient. Deliberately distinct from
# "ingredient not found": the ingredient exists in the shared catalog, but this
# branch has no physical stock row for it. Another store's stock is NOT used as
# a fallback — the branch must receive or count stock in explicitly.
INVENTORY_STOCK_NOT_CONFIGURED = (
    "Bu malzeme şubenizin stoğunda tanımlı değil. "
    "Önce mal kabul veya sayım girişi yapın."
)


# ── Inventory lifecycle (staff-facing, owner-web) ────────────────────────────
# A manual stock command needs an idempotency key.
INVENTORY_IDEMPOTENCY_REQUIRED = "Stok işlemi için işlem anahtarı gerekli."

# Same key replayed with a different payload.
INVENTORY_IDEMPOTENCY_MISMATCH = (
    "Bu stok işlemi farklı bilgilerle daha önce denenmiş. "
    "Lütfen kontrol edip yeniden başlatın."
)

# Quantity must be positive.
INVENTORY_QUANTITY_INVALID = "Stok miktarı sıfırdan büyük olmalı."

# Waste / manual adjustment without a reason.
INVENTORY_REASON_REQUIRED = "Bu stok işlemi için neden belirtmeniz gerekiyor."

# Unknown / inactive ingredient, or no stock row for it.
INVENTORY_INGREDIENT_NOT_FOUND = "Böyle bir malzeme bulunamadı."

# A negative adjustment / waste would push physical stock below what is already
# promised to accepted orders (or below zero).
INVENTORY_INSUFFICIENT_ON_HAND = (
    "Fiziksel stok yetersiz. Bu işlem, bekleyen siparişler için ayrılmış stoğun "
    "altına iniyor."
)


# ── Store-to-store inventory transfer ────────────────────────────────────────
# The named destination store does not exist.
INVENTORY_TRANSFER_DESTINATION_NOT_FOUND = "Hedef şube bulunamadı."

# Source and destination are the same branch. Shipping stock to yourself is not a
# transfer; it is a no-op that would leave a cancelling pair of movements behind.
INVENTORY_TRANSFER_SAME_STORE = "Hedef şube, gönderen şubeden farklı olmalı."

# The source branch does not physically have enough UNRESERVED stock. Stock that
# accepted orders are already counting on cannot be put on a van to another
# branch — the waiting customer's waffle is a promise this shop has already made.
INVENTORY_TRANSFER_INSUFFICIENT_AVAILABLE = (
    "Gönderen şubede yeterli kullanılabilir stok yok. "
    "Bekleyen siparişler için ayrılmış stok transfer edilemez."
)

# No such transfer, or it is a transfer between two OTHER branches. Deliberately
# a 404 and not a 403: a 403 would confirm the transfer exists.
INVENTORY_TRANSFER_NOT_FOUND = "Bu transfer bulunamadı."


# ── Physical stock count ─────────────────────────────────────────────────────
# The counted quantity is below what accepted orders are already promised. This is
# NOT a stock correction — it means the shop has sold stock it does not physically
# have, and quietly writing on-hand down would break a promise made to a customer
# who is sitting at a table waiting. So the message names the cause (ayrılmış stok)
# and points at the only honest fix: deal with the orders first, then count.
INVENTORY_STOCK_COUNT_BELOW_RESERVED = (
    "Sayım sonucu, bekleyen siparişler için ayrılmış stoktan düşük olamaz. "
    "Önce ilgili siparişleri kontrol edin."
)

# A counted quantity may be ZERO — an empty shelf is a valid count, and the one a
# manager most needs to be able to report. It may never be negative.
INVENTORY_COUNT_QUANTITY_INVALID = "Sayım sonucu negatif olamaz."

# No such count, or it belongs to another branch. A 404 and not a 403: a 403 would
# confirm the count exists.
INVENTORY_STOCK_COUNT_NOT_FOUND = "Bu sayım kaydı bulunamadı."


# ── Inventory threshold alerts ───────────────────────────────────────────────
# A threshold is a SETTING, not stock. None of these messages should ever suggest
# that stock moved, that something was ordered, or that a supplier was involved —
# see docs/INVENTORY_THRESHOLD_ALERTS.md.

# A threshold below zero would promise an alert that can never fire: no quantity can
# fall below zero. A control that silently does nothing is worse than no control,
# because the manager believes they are covered by it.
INVENTORY_THRESHOLD_NEGATIVE = "Eşik değerleri negatif olamaz."

# An inverted ladder. If critical sits above minimum, the ingredient reaches "kritik"
# before it ever reaches "düşük", and the early warning the manager set up to give
# themselves time never appears at all.
INVENTORY_THRESHOLD_CRITICAL_ABOVE_MINIMUM = (
    "Kritik eşik minimum eşikten büyük olamaz."
)

# Restocking to target would land the branch straight back into "düşük stok": a
# replenishment that is a warning the moment it arrives.
INVENTORY_THRESHOLD_MINIMUM_ABOVE_TARGET = (
    "Minimum eşik hedef stoktan büyük olamaz."
)

# The same ordering rule, for the case where minimum is not configured at all and
# nothing else is holding critical and target together.
INVENTORY_THRESHOLD_CRITICAL_ABOVE_TARGET = (
    "Kritik eşik hedef stoktan büyük olamaz."
)

# The six alert statuses, in Turkish. The wire value (CRITICAL, NOT_CONFIGURED …)
# stays the stable English contract; THIS is what a manager reads, and a raw status
# enum must never reach a screen.
#
# Two pairs here are easy to conflate and expensive to get wrong:
#
#   "Stokta yok"             there is nothing available to promise anybody.
#   "Ayrılmış stoktan düşük" the branch has promised MORE than it physically holds.
#                            Not a stock level — an incident. A manager who reads
#                            "stokta yok" goes and orders more; one who reads this
#                            goes and looks at the orders that cannot be fulfilled.
#
#   "Stok yeterli"           above every configured warning level. A statement of fact.
#   "Eşik tanımlı değil"     nobody has said what "low" means here. NOT an all-clear —
#                            it is the absence of one, and saying "yeterli" instead
#                            would be the system inventing reassurance it has no basis
#                            for.
INVENTORY_THRESHOLD_STATUS_LABEL = {
    "BELOW_RESERVED": "Ayrılmış stoktan düşük",
    "OUT_OF_STOCK": "Stokta yok",
    "CRITICAL": "Kritik stok",
    "LOW": "Düşük stok",
    "HEALTHY": "Stok yeterli",
    "NOT_CONFIGURED": "Eşik tanımlı değil",
}

# The recommended top-up quantity, explained. Pointedly NOT called a purchase order,
# a reorder, or anything a supplier would recognise: nothing is ordered, nothing is
# reserved, and no system acts on this number. The manager reads it and decides.
INVENTORY_THRESHOLD_RESTOCK_HINT = (
    "Hedef stok seviyesine ulaşmak için önerilen tamamlama miktarı"
)


# ── Payment settlement / cashier (cashier-web) ───────────────────────────────
# Order/table/settlement not found or belongs to another store — non-disclosing,
# but still tells the cashier what to do next instead of "kayıt bulunamadı".
PAY_NOT_FOUND = (
    "Bu sipariş bulunamadı. Sipariş numarasını kontrol edin veya açık masalardan seçin."
)

# The selected orders have no outstanding balance to collect.
PAY_NO_BALANCE = "Bu siparişin ödenecek bakiyesi kalmadı."

# A payment would exceed the outstanding balance.
PAY_OVERPAYMENT = "Tahsilat tutarı, kalan bakiyeden fazla olamaz."

# Payment amount must be positive.
PAY_AMOUNT_INVALID = "Tahsilat tutarı sıfırdan büyük olmalı."

# Unsupported / unknown payment method.
PAY_METHOD_INVALID = "Geçersiz ödeme yöntemi."

# A cancelled order can never be collected.
PAY_ORDER_CANCELLED = "İptal edilmiş sipariş için tahsilat yapılamaz."

# Idempotency key missing on a financial command.
PAY_IDEMPOTENCY_REQUIRED = "Tahsilat işlemi için işlem anahtarı gerekli."

# Same idempotency key reused with a different payload — refuse to replay.
PAY_IDEMPOTENCY_MISMATCH = (
    "Bu ödeme farklı bilgilerle daha önce denenmiş. "
    "Lütfen tutarı ve yöntemi kontrol edip yeniden başlatın."
)

# The table does not belong to this store / this order is not on this table.
PAY_TABLE_MISMATCH = "Seçilen masa veya sipariş bu şubeye ait değil."

# Currency mismatch inside one settlement.
PAY_CURRENCY_MISMATCH = "Tek bir tahsilatta farklı para birimleri kullanılamaz."

# A previously-refunded order was selected in the generic "settle whole table"
# flow. Re-collecting money on an order that was already refunded must be an
# explicit, per-order decision — never a silent side effect of one-click settle.
PAY_REFUNDED_RECOLLECT = (
    "Bu sipariş daha önce iade edilmiş. Yeniden tahsilat için siparişi tek tek "
    "seçip onaylamanız gerekiyor."
)


# ── Refunds ──────────────────────────────────────────────────────────────────
# Refund amount must be positive.
REFUND_AMOUNT_INVALID = "İade tutarı sıfırdan büyük olmalı."

# Refund would exceed the refundable balance of the collected money.
REFUND_OVER_BALANCE = "Bu tahsilatın iade edilebilir bakiyesi kalmadı."

# A refund reason is mandatory.
REFUND_REASON_REQUIRED = "İade nedeni girmeniz gerekiyor."


# ── Cashier shift closing (cashier-web / owner-web) ──────────────────────────
# A shift command needs an idempotency key.
SHIFT_IDEMPOTENCY_REQUIRED = "Vardiya işlemi için işlem anahtarı gerekli."

# Same key replayed with a different payload — refuse to replay.
SHIFT_IDEMPOTENCY_MISMATCH = (
    "Bu vardiya işlemi farklı bilgilerle daha önce denenmiş. "
    "Lütfen bilgileri kontrol edip yeniden başlatın."
)

# Opening / closing cash may be zero but never negative.
SHIFT_OPENING_CASH_INVALID = "Açılış nakdi negatif olamaz."
SHIFT_COUNTED_CASH_INVALID = "Kapanış tutarı negatif olamaz."

# No open shift for this cashier/store.
SHIFT_NONE_OPEN = "Açık vardiya bulunmuyor."

# The cashier already has an open shift at this store. Deliberately not an error
# the caller has to recover from: the open shift is returned so they can just
# continue or close it.
SHIFT_ALREADY_OPEN = "Bu kasiyer için açık vardiya zaten var."

# Shift not found, or it belongs to another store/cashier. Non-disclosing on
# purpose: a 403 would confirm the shift exists somewhere else.
SHIFT_NOT_FOUND = "Bu vardiya bulunamadı."

# A closed shift cannot be closed again (except an exact idempotent replay).
SHIFT_ALREADY_CLOSED = "Bu vardiya zaten kapatılmış."

# The close could not be confirmed (e.g. a mismatched replay against a shift whose
# state has moved on). Points the cashier at the safe next step rather than a blind
# retry that could double-submit.
SHIFT_CLOSE_UNVERIFIED = (
    "Vardiya kapanışı doğrulanamadı. "
    "Aynı işlemi tekrar göndermeden önce vardiya durumunu kontrol edin."
)


# ── Order lifecycle (kitchen / cashier) ──────────────────────────────────────
# No such order, or it belongs to another store. Non-disclosing on purpose: a
# 403 here would confirm the order exists in some other branch.
ORDER_NOT_FOUND = "Bu sipariş bulunamadı."

# The customer submitted a waffle with no ingredients on it.
ORDER_NO_INGREDIENTS = "Siparişinizde en az bir malzeme olmalı."

# The order already reached a terminal state — nothing left to change.
ORDER_ALREADY_CLOSED = "Bu sipariş tamamlanmış veya iptal edilmiş."

# Undo window for a kitchen status change has passed.
ORDER_UNDO_EXPIRED = "Geri alma süresi doldu."

# A paid (net > 0) order cannot be cancelled until the collection is refunded.
ORDER_CANCEL_BLOCKED_PAID = (
    "Tahsilatı yapılmış sipariş doğrudan iptal edilemez. "
    "Önce tahsilatı iade etmeniz gerekiyor."
)


# ── Metrics / analytics (owner-web) ──────────────────────────────────────────
# The analytics store is down. The rest of the dashboard keeps working, so say
# so — an owner who thinks the whole system is broken will stop trusting all of it.
METRICS_UNAVAILABLE = (
    "Ölçüm verileri şu anda yüklenemiyor. Panelin geri kalanı çalışmaya devam ediyor; "
    "veriler bağlantı düzelince otomatik olarak görünecek."
)

# Metric computation blew up for a reason we did not anticipate.
METRICS_FAILED = (
    "Ölçüm verileri hesaplanamadı. Panelin geri kalanı çalışmaya devam ediyor."
)
