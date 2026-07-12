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
