/**
 * Cashier shift — the view logic a screen needs, kept free of React/DOM so it can
 * be unit-tested as pure TypeScript.
 *
 * A shift is the daily reconciliation of the till: the cashier opens with a cash
 * figure, takes payments and refunds through the existing ledger, then closes with
 * a physical count. The one number that matters at the end is the DISCREPANCY —
 * and the raw status enum (OPEN/CLOSED) must never reach the screen.
 */

// ── Status label (never render the raw enum) ──────────────────────────────────

/** Wire values: OPEN | CLOSED. */
export const SHIFT_STATUS_LABEL: Record<string, string> = {
  OPEN: "Açık",
  CLOSED: "Kapalı",
};

export function shiftStatusLabel(value: string | null | undefined): string {
  if (!value) return "Bilinmiyor";
  return SHIFT_STATUS_LABEL[value] ?? "Bilinmiyor";
}

// ── Discrepancy classification (Denk / Eksik / Fazla) ─────────────────────────

export type DiscrepancyClass = "balanced" | "short" | "over";

export const DISCREPANCY_LABEL: Record<DiscrepancyClass, string> = {
  balanced: "Denk",
  short: "Eksik",
  over: "Fazla",
};

/**
 * Classify a signed discrepancy string (counted − expected).
 *   0  → balanced (Denk)
 *   <0 → short (Eksik) — the drawer is missing money
 *   >0 → over (Fazla) — the drawer has more than it should
 * Non-numeric input is treated as balanced rather than throwing: a missing figure
 * must not crash a receipt.
 */
export function discrepancyClass(amount: string | number | null | undefined): DiscrepancyClass {
  const n = typeof amount === "number" ? amount : Number.parseFloat(String(amount ?? ""));
  if (!Number.isFinite(n) || Math.abs(n) < 0.005) return "balanced";
  return n < 0 ? "short" : "over";
}

export function discrepancyLabel(amount: string | number | null | undefined): string {
  return DISCREPANCY_LABEL[discrepancyClass(amount)];
}

// ── Field labels (Turkish, for the close summary) ─────────────────────────────

export const SHIFT_LABELS = {
  open: "Vardiya aç",
  close: "Vardiya kapat",
  openingCash: "Açılış nakdi",
  countedCash: "Kapanış nakit sayımı",
  expectedCash: "Beklenen kasa",
  countedCashShort: "Sayılan kasa",
  discrepancy: "Eksik/Fazla",
  cashPayments: "Nakit tahsilat",
  cashRefunds: "Nakit iade",
  cardPayments: "Kart tahsilat",
  cardRefunds: "Kart iade",
  grossPayments: "Toplam tahsilat",
  totalRefunds: "Toplam iade",
  netCollected: "Net tahsilat",
  openedAt: "Açılış",
  closedAt: "Kapanış",
} as const;

// ── User-facing copy ──────────────────────────────────────────────────────────

export const SHIFT_COPY = {
  noOpenShift: "Açık vardiya bulunmuyor.",
  openSuccess: "Vardiya başarıyla açıldı.",
  closeSuccess: "Vardiya başarıyla kapatıldı.",
  alreadyOpen: "Bu kasiyer için açık vardiya zaten var.",
  openingNegative: "Açılış nakdi negatif olamaz.",
  countedNegative: "Kapanış tutarı negatif olamaz.",
  // Network uncertainty on OPEN: safe to retry the same command.
  openUncertain:
    "Vardiya açılışının tamamlanıp tamamlanmadığı doğrulanamadı. " +
    "Aynı işlemi güvenle tekrar deneyebilirsiniz.",
  // Network uncertainty on CLOSE: do NOT blind-retry — check first.
  closeUncertain:
    "Vardiya kapanışı doğrulanamadı. " +
    "Aynı işlemi tekrar göndermeden önce vardiya durumunu kontrol edin.",
  // A soft warning shown beside the payment screen when no shift is open. It never
  // blocks a payment — enforcing open-shift-before-payment is out of scope.
  paymentWithoutShift:
    "Açık vardiya yok. Tahsilat alabilirsiniz, ancak gün sonu sayımı için vardiya açmanız önerilir.",
} as const;

// ── Form validation ───────────────────────────────────────────────────────────

const _isNonNegativeMoney = (raw: string): boolean => {
  const s = raw.trim();
  if (s === "") return false;
  const n = Number(s);
  return Number.isFinite(n) && n >= 0;
};

/** Errors for the opening-cash field. Empty array = valid. */
export function validateOpeningCash(raw: string): string[] {
  const s = (raw ?? "").trim();
  if (s === "") return ["Açılış nakdi girin."];
  const n = Number(s);
  if (!Number.isFinite(n)) return ["Açılış nakdi geçerli bir tutar olmalı."];
  if (n < 0) return [SHIFT_COPY.openingNegative];
  return [];
}

/** Errors for the counted closing-cash field. Zero is valid; negative is not. */
export function validateCountedCash(raw: string): string[] {
  const s = (raw ?? "").trim();
  if (s === "") return ["Kapanış nakit sayımını girin."];
  const n = Number(s);
  if (!Number.isFinite(n)) return ["Kapanış tutarı geçerli bir tutar olmalı."];
  if (n < 0) return [SHIFT_COPY.countedNegative];
  return [];
}

export { _isNonNegativeMoney as isNonNegativeMoney };

// ── Command fingerprints (idempotency) ────────────────────────────────────────

export interface OpenShiftCommand {
  kind: "shift_open";
  openingCash: string;
  openNote?: string | null;
}

export interface CloseShiftCommand {
  kind: "shift_close";
  shiftId: number;
  countedCash: string;
  closeNote?: string | null;
}

export type ShiftCommand = OpenShiftCommand | CloseShiftCommand;

/**
 * Deterministic fingerprint of the logical command. Every field that changes what
 * the backend persists is included, so editing the counted amount mints a fresh
 * idempotency key instead of replaying the previous close.
 */
export function fingerprintShiftCommand(cmd: ShiftCommand): string {
  if (cmd.kind === "shift_open") {
    return JSON.stringify({
      kind: cmd.kind,
      openingCash: cmd.openingCash,
      openNote: cmd.openNote ?? null,
    });
  }
  return JSON.stringify({
    kind: cmd.kind,
    shiftId: cmd.shiftId,
    countedCash: cmd.countedCash,
    closeNote: cmd.closeNote ?? null,
  });
}
