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
