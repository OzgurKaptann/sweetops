/**
 * Turkish display labels for the enum values the API sends.
 *
 * The API speaks English enums — PAID, TRANSFER_OUT, IN_PREP — and that is the
 * wire contract: it is never translated, never renamed, and every comparison in
 * this app is still made against the English value. This module is the single
 * place where those values become something a cashier can read, and it exists so
 * that a raw `PARTIALLY_PAID` can never leak onto the screen.
 *
 * Anything added here must be looked up through the helpers below, not by
 * indexing the record directly — the helpers decide what happens when the API
 * introduces a value this build has never seen.
 */

/** Payment state of an order. Wire values: UNPAID | PARTIALLY_PAID | PAID | REFUNDED. */
export const PAYMENT_STATUS_LABEL: Record<string, string> = {
  UNPAID: "Ödenmedi",
  PARTIALLY_PAID: "Kısmi ödendi",
  PAID: "Ödendi",
  REFUNDED: "İade edildi",
};

/** Refund state of an order. Wire values: NONE | PARTIAL | FULL. */
export const REFUND_STATUS_LABEL: Record<string, string> = {
  NONE: "İade yok",
  PARTIAL: "Kısmi iade",
  FULL: "Tamamen iade edildi",
};

/** Kitchen preparation state, shown on the table bill so the cashier knows
 *  whether the food has actually gone out before taking the money. */
export const PREPARATION_STATUS_LABEL: Record<string, string> = {
  NEW: "Bekliyor",
  IN_PREP: "Hazırlanıyor",
  READY: "Hazır",
  DELIVERED: "Teslim edildi",
  CANCELLED: "İptal edildi",
};

/** How the money came in. Wire values: CASH | CARD | OTHER. */
export const PAYMENT_METHOD_LABEL: Record<string, string> = {
  CASH: "Nakit",
  CARD: "Kart",
  OTHER: "Diğer",
};

/** Direction of a row in the transaction history. */
export const TRANSACTION_KIND_LABEL: Record<string, string> = {
  SETTLEMENT: "Tahsilat",
  REFUND: "İade",
};

/**
 * Look a wire value up in one of the maps above.
 *
 * An unknown value falls back to `fallback` rather than to the raw enum: a
 * cashier who sees "Bilinmiyor" knows to ask someone, whereas one who sees
 * "PARTIALLY_REFUNDED" has been handed a bug report to read mid-shift. The
 * value is still recoverable from the network tab when it matters.
 */
export function labelFor(
  map: Record<string, string>,
  value: string | null | undefined,
  fallback = "Bilinmiyor",
): string {
  if (!value) return fallback;
  return map[value] ?? fallback;
}

export const paymentStatusLabel = (v: string | null | undefined) =>
  labelFor(PAYMENT_STATUS_LABEL, v);

export const refundStatusLabel = (v: string | null | undefined) =>
  labelFor(REFUND_STATUS_LABEL, v);

export const preparationStatusLabel = (v: string | null | undefined) =>
  labelFor(PREPARATION_STATUS_LABEL, v);

export const paymentMethodLabel = (v: string | null | undefined) =>
  labelFor(PAYMENT_METHOD_LABEL, v, "Diğer");

export const transactionKindLabel = (v: string | null | undefined) =>
  labelFor(TRANSACTION_KIND_LABEL, v, "Tahsilat");
