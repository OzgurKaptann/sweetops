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
