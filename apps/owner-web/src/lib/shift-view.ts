/**
 * Owner shift history — view logic, free of React/DOM so it is unit-testable.
 *
 * A manager reads this screen to answer one question per cashier per day: did the
 * till close correctly? So the raw status enum (OPEN/CLOSED) and the raw signed
 * discrepancy must never reach the table — they become "Açık"/"Kapalı" and
 * "Denk"/"Eksik"/"Fazla" here, once, where they can be tested.
 */

// ── Status ────────────────────────────────────────────────────────────────────

export const SHIFT_STATUS_LABEL: Record<string, string> = {
  OPEN: "Açık",
  CLOSED: "Kapalı",
};

export function shiftStatusLabel(value: string | null | undefined): string {
  if (!value) return "Bilinmiyor";
  return SHIFT_STATUS_LABEL[value] ?? "Bilinmiyor";
}

// ── Discrepancy (Denk / Eksik / Fazla) ────────────────────────────────────────

export type DiscrepancyClass = "balanced" | "short" | "over";

export const DISCREPANCY_LABEL: Record<DiscrepancyClass, string> = {
  balanced: "Denk",
  short: "Eksik",
  over: "Fazla",
};

export function discrepancyClass(amount: string | number | null | undefined): DiscrepancyClass {
  const n = typeof amount === "number" ? amount : Number.parseFloat(String(amount ?? ""));
  if (!Number.isFinite(n) || Math.abs(n) < 0.005) return "balanced";
  return n < 0 ? "short" : "over";
}

export function discrepancyLabel(amount: string | number | null | undefined): string {
  return DISCREPANCY_LABEL[discrepancyClass(amount)];
}

// ── Copy ──────────────────────────────────────────────────────────────────────

export const SHIFT_HISTORY_COPY = {
  heading: "Vardiya geçmişi",
  empty: "Henüz kapatılmış veya açık vardiya yok.",
  loadError: "Vardiya geçmişi yüklenemiyor. Lütfen daha sonra tekrar deneyin.",
  columns: {
    cashier: "Kasiyer",
    status: "Durum",
    opened: "Açılış",
    closed: "Kapanış",
    expectedCash: "Beklenen kasa",
    countedCash: "Sayılan kasa",
    discrepancy: "Eksik/Fazla",
    netCollected: "Net tahsilat",
  },
} as const;

// ── Money / date formatting ───────────────────────────────────────────────────

/** A money string → "1.234,56 ₺" (tr-TR), or "—" when absent (an open shift). */
export function formatMoney(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n.toLocaleString("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ₺`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("tr-TR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ── Row transform (the display-safe shape a table renders) ────────────────────

export interface ShiftLike {
  id: number;
  cashier_display: string;
  status: string;
  opened_at: string;
  closed_at: string | null;
  expected_closing_cash_amount: string | null;
  counted_closing_cash_amount: string | null;
  cash_discrepancy_amount: string | null;
  net_collected_amount: string | null;
}

export interface ShiftRow {
  id: number;
  cashier: string;
  statusLabel: string;
  isClosed: boolean;
  openedAt: string;
  closedAt: string;
  expectedCash: string;
  countedCash: string;
  discrepancyLabel: string;
  discrepancyClass: DiscrepancyClass;
  discrepancyAmount: string;
  netCollected: string;
}

/**
 * Shape one API shift into a fully display-safe row. Nothing raw survives: the
 * status is a label, the discrepancy is a label + class + formatted amount, and
 * an OPEN shift's empty close columns become "—" rather than "null".
 */
export function toShiftRow(shift: ShiftLike): ShiftRow {
  const isClosed = shift.status === "CLOSED";
  return {
    id: shift.id,
    cashier: shift.cashier_display,
    statusLabel: shiftStatusLabel(shift.status),
    isClosed,
    openedAt: formatDateTime(shift.opened_at),
    closedAt: formatDateTime(shift.closed_at),
    expectedCash: formatMoney(shift.expected_closing_cash_amount),
    countedCash: formatMoney(shift.counted_closing_cash_amount),
    discrepancyLabel: isClosed ? discrepancyLabel(shift.cash_discrepancy_amount) : "—",
    discrepancyClass: discrepancyClass(shift.cash_discrepancy_amount),
    discrepancyAmount: formatMoney(shift.cash_discrepancy_amount),
    netCollected: formatMoney(shift.net_collected_amount),
  };
}
