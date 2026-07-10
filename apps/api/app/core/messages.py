"""
User-facing Turkish messages.

Every string here is shown to a customer. Keep them Turkish, calm and free of
internal/technical detail. Public responses must never reveal whether a
particular token once existed or leak diagnostic information — technical detail
belongs only in server logs.
"""

# Invalid / unknown / malformed / revoked QR — one consistent response so a
# probing client cannot distinguish "never existed" from "revoked".
QR_INVALID = (
    "Bu QR kod geçerli değil. Lütfen masadaki güncel QR kodu kullan."
)

# Table or store is not currently open to ordering (inactive).
QR_UNAVAILABLE = (
    "Bu masa şu anda siparişe açık değil. "
    "Lütfen işletme personelinden yardım iste."
)

# QR token was expected but not supplied (e.g. a legacy order attempt in a
# production configuration that no longer trusts client-supplied context).
QR_REQUIRED = (
    "Sipariş için geçerli bir QR kod gerekiyor. "
    "Lütfen masadaki QR kodu okut."
)


# ── Staff authentication (Turkish, shown in owner-web / kitchen-web) ─────────
# Generic invalid-credentials message. Deliberately does NOT reveal whether the
# username exists, the password was wrong, or the account is disabled.
AUTH_INVALID_CREDENTIALS = "Kullanıcı adı veya şifre hatalı."

# Account temporarily locked after too many failed attempts.
AUTH_ACCOUNT_LOCKED = (
    "Hesabın geçici olarak kilitlendi. Lütfen daha sonra tekrar dene."
)

# Not authenticated (missing/expired/revoked session) — 401.
AUTH_SESSION_EXPIRED = "Oturumun sona erdi. Lütfen yeniden giriş yap."

# Authenticated but lacks permission for this area/action — 403.
AUTH_FORBIDDEN = "Bu alana erişim yetkin yok."

# CSRF token missing or invalid — 403.
AUTH_CSRF_INVALID = "Güvenlik doğrulaması başarısız. Lütfen sayfayı yenileyip tekrar dene."

# Request origin not among trusted staff origins — 403.
AUTH_ORIGIN_REJECTED = "İstek kaynağı doğrulanamadı."

# Login field validation.
AUTH_MISSING_FIELDS = "Kullanıcı adı ve şifre gerekli."

# Fail-closed guard: global inventory cannot be safely shown when more than one
# operational store exists (inventory is not yet store-scoped).
INVENTORY_MULTISTORE_BLOCKED = (
    "Stok verisi şu anda birden fazla mağaza için güvenli şekilde gösterilemiyor. "
    "Lütfen işletme yöneticisiyle iletişime geç."
)


# ── Payment settlement / cashier (Turkish, shown in cashier-web) ─────────────
# Order/table/settlement not found or belongs to another store — non-disclosing.
PAY_NOT_FOUND = "Kayıt bulunamadı."

# The selected orders have no outstanding balance to collect.
PAY_NO_BALANCE = "Bu siparişin ödenecek bakiyesi bulunmuyor."

# A payment would exceed the outstanding balance.
PAY_OVERPAYMENT = "Ödeme tutarı kalan bakiyeyi aşamaz."

# Payment amount must be positive.
PAY_AMOUNT_INVALID = "Ödeme tutarı sıfırdan büyük olmalı."

# Unsupported / unknown payment method.
PAY_METHOD_INVALID = "Geçersiz ödeme yöntemi."

# A cancelled order can never be collected.
PAY_ORDER_CANCELLED = "İptal edilmiş sipariş için ödeme alınamaz."

# Idempotency key missing on a financial command.
PAY_IDEMPOTENCY_REQUIRED = "İşlem anahtarı (Idempotency-Key) gerekli."

# Same idempotency key reused with a different payload — refuse to replay.
PAY_IDEMPOTENCY_MISMATCH = "Aynı işlem anahtarı farklı bilgilerle kullanılamaz."

# The table does not belong to this store / this order is not on this table.
PAY_TABLE_MISMATCH = "Seçilen masa veya sipariş bu işletmeye ait değil."

# Currency mismatch inside one settlement.
PAY_CURRENCY_MISMATCH = "Tek bir tahsilatta farklı para birimleri kullanılamaz."

# A previously-refunded order was selected in the generic "settle whole table"
# flow. Re-collecting money on an order that was already refunded must be an
# explicit, per-order decision — never a silent side effect of one-click settle.
PAY_REFUNDED_RECOLLECT = (
    "Bu sipariş daha önce iade edildi. Yeniden tahsilat için siparişi tek tek "
    "seçerek onaylaman gerekir."
)

# ── Refunds ──────────────────────────────────────────────────────────────────
# Refund amount must be positive.
REFUND_AMOUNT_INVALID = "İade tutarı sıfırdan büyük olmalı."

# Refund would exceed the refundable balance of the collected money.
REFUND_OVER_BALANCE = "Bu işlem için iade edilebilir bakiye bulunmuyor."

# A refund reason is mandatory.
REFUND_REASON_REQUIRED = "İade nedeni gerekli."

# ── Cancellation interaction ─────────────────────────────────────────────────
# A paid (net > 0) order cannot be cancelled until the collection is refunded.
ORDER_CANCEL_BLOCKED_PAID = (
    "Ödeme alınmış sipariş doğrudan iptal edilemez. "
    "Önce tahsilatın iade edilmesi gerekir."
)
