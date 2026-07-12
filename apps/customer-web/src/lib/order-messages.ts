/**
 * Turkish copy for the things that can go wrong while ordering.
 *
 * A customer at a table is not an operator: they cannot act on "validation
 * failed", they have no idea what a 409 is, and an English string in the middle
 * of a Turkish menu reads as a broken app. So every failure this screen can show
 * resolves through here, and what comes out says (a) what happened, and (b) what
 * they can do about it.
 *
 * The distinction that matters most is uncertainty. If the request never got a
 * response the order may well exist, and telling the customer it failed would
 * invite a duplicate. That case gets its own message.
 *
 * This module deliberately imports nothing. It is pure copy plus a classifier,
 * which keeps it runnable under `node --test` (Node's TS resolver wants explicit
 * `.ts` specifiers, which the Next build in turn rejects). The errors are matched
 * on the shape they actually carry — `name` plus the field the decision hinges
 * on — rather than with `instanceof`, so the two toolchains agree.
 */

/** Phases of the QR gate, mirrored from the menu screen. */
export type QrPhase =
  | "loading"
  | "missing"
  | "invalid"
  | "unavailable"
  | "network"
  | "ready";

/** Why a QR token failed to resolve. Mirrors `QrResolveErrorKind` in ./api. */
export type QrErrorKind = "invalid" | "unavailable" | "network";

const QR_PHASE_MESSAGE: Record<Exclude<QrPhase, "ready">, string> = {
  loading: "Menü yükleniyor…",
  missing: "Masa bilgisi bulunamadı. Lütfen masadaki QR kodu okutun.",
  invalid:
    "Bu masa bağlantısı geçersiz veya süresi dolmuş. Lütfen masadaki QR kodu tekrar okutun.",
  unavailable: "Bu masa şu anda siparişe kapalı. Lütfen personelden yardım isteyin.",
  network: "Menü yüklenemedi. Lütfen tekrar deneyin.",
};

/**
 * Copy for a QR gate phase.
 *
 * `serverMessage` wins when present: the API already speaks Turkish and knows
 * more than we do about why this particular token was refused. The local string
 * is the fallback for when the request never reached it.
 */
export function qrPhaseMessage(
  phase: Exclude<QrPhase, "ready">,
  serverMessage?: string | null,
): string {
  return serverMessage ?? QR_PHASE_MESSAGE[phase];
}

/** Map a QR failure to the phase the screen should show. */
export function qrPhaseForError(
  kind: QrErrorKind,
): Exclude<QrPhase, "ready" | "loading" | "missing"> {
  if (kind === "invalid") return "invalid";
  if (kind === "unavailable") return "unavailable";
  return "network";
}

/** Order submission failed but the outcome is unknown — the order may exist. */
export const ORDER_UNCERTAIN_MESSAGE =
  "Bağlantı kesildi. Tekrar deneyebilirsiniz; siparişiniz iki kez oluşturulmaz.";

/** Order submission was deterministically rejected — the cart needs changing. */
export const ORDER_REJECTED_MESSAGE =
  "Sipariş gönderilemedi. Lütfen seçimlerinizi kontrol edip tekrar deneyin.";

interface UncertainOrderErrorShape {
  name: "OrderRequestError";
  isUncertain: boolean;
}

interface QrErrorShape {
  name: "QrResolveError";
  kind: QrErrorKind;
  userMessage?: string;
}

function isOrderRequestError(err: unknown): err is UncertainOrderErrorShape {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { name?: unknown }).name === "OrderRequestError"
  );
}

function isQrResolveError(err: unknown): err is QrErrorShape {
  return (
    typeof err === "object" &&
    err !== null &&
    (err as { name?: unknown }).name === "QrResolveError"
  );
}

/**
 * The Turkish line to show when creating an order fails.
 *
 * Never surfaces the status code, the error kind, or the thrown Error's own
 * message — those belong in the console, not in front of someone holding a menu.
 * Anything unrecognised falls back to the rejection copy rather than to
 * `err.message`, because an unknown throwable is exactly the case where that
 * message is an English debug string.
 */
export function orderErrorMessage(err: unknown): string {
  if (isOrderRequestError(err) && err.isUncertain) {
    return ORDER_UNCERTAIN_MESSAGE;
  }
  if (isQrResolveError(err)) {
    return qrPhaseMessage(qrPhaseForError(err.kind), err.userMessage);
  }
  return ORDER_REJECTED_MESSAGE;
}
