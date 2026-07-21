/**
 * Owner operational dashboard — view logic, free of React/DOM so it is unit-testable.
 *
 * The API speaks English enums/codes and money-as-strings. This module is the ONE
 * place those become the Turkish an owner reads: severities, attention codes, money
 * and durations. Nothing raw (an enum, a null, a signed number) is allowed to reach
 * a card — every mapping below defends that, and the tests defend the mappings.
 */

// ── Wire types (mirror app/schemas/owner_dashboard.py) ────────────────────────

export interface DashboardOrders {
  active_count: number;
  waiting_count: number;
  in_prep_count: number;
  ready_count: number;
  completed_today: number;
  cancelled_today: number;
}

export interface DashboardPayments {
  currency: string;
  gross_collected_today: string;
  refunds_today: string;
  net_collected_today: string;
  unpaid_or_partially_paid_orders: number;
}

export interface DashboardKitchen {
  active_orders: number;
  delayed_orders: number;
  average_prep_seconds_today: number | null;
  average_time_to_ready_seconds_today: number | null;
  p95_prep_seconds_today: number | null;
}

export interface DashboardIssues {
  open_count: number;
  resolved_today: number;
  refund_amount_today: string;
}

export interface DashboardShifts {
  open_shift_count: number;
  closed_today: number;
  total_discrepancy_today: string;
  shifts_with_discrepancy_today: number;
}

export interface DashboardInventory {
  out_of_stock_count: number;
  below_reserved_count: number;
  critical_count: number;
  low_count: number;
  healthy_count: number;
  not_configured_count: number;
}

export type AttentionSeverity = "critical" | "warning" | "info";

export interface AttentionItem {
  severity: string; // critical | warning | info — never rendered raw
  code: string;     // OUT_OF_STOCK | ... — never rendered raw
  count: number;
  target_route: string | null;
}

export interface OperationalDashboard {
  business_date: string;
  as_of: string;
  store_id: number;
  orders: DashboardOrders;
  payments: DashboardPayments;
  kitchen: DashboardKitchen;
  issues: DashboardIssues;
  shifts: DashboardShifts;
  inventory: DashboardInventory;
  attention: AttentionItem[];
}

// ── Section / card copy (Turkish) ─────────────────────────────────────────────

export const DASHBOARD_COPY = {
  sectionTitle: "Operasyon özeti",
  sectionSubtitle: "Bugünün durumu · mevcut sistemlerden canlı okunur",
  loadError: "Veriler yüklenemedi. Lütfen daha sonra tekrar deneyin.",
  empty: "Bugün için veri yok.",
  detailLink: "Detaya git",
  cards: {
    payments: "Günlük ciro",
    orders: "Aktif sipariş",
    kitchen: "Mutfak temposu",
    issues: "Açık sorunlu sipariş",
    shifts: "Kasa vardiyaları",
    inventory: "Kritik stok",
    attention: "Dikkat gerektirenler",
  },
  labels: {
    gross: "Tahsilat",
    refunds: "İade",
    net: "Net tahsilat",
    unpaid: "Ödenmemiş sipariş",
    waiting: "Bekleyen",
    inPrep: "Hazırlanan",
    ready: "Hazır",
    completedToday: "Bugün teslim",
    cancelledToday: "Bugün iptal",
    delayed: "Geciken sipariş",
    avgPrep: "Ortalama hazırlık",
    activeOrders: "Aktif sipariş",
    openIssues: "Açık sorun",
    resolvedToday: "Bugün çözülen",
    issueRefund: "Bugünkü iade tutarı",
    openShifts: "Açık vardiya",
    closedToday: "Bugün kapanan",
    discrepancy: "Kasa farkı",
    shiftsWithDiscrepancy: "Farklı kapanış",
    outOfStock: "Tükenen",
    critical: "Kritik",
    low: "Düşük",
    healthy: "Sağlıklı",
  },
  noAttention: "Şu an dikkat gerektiren bir durum yok.",
} as const;

// ── Severity ──────────────────────────────────────────────────────────────────

export const SEVERITY_LABEL: Record<AttentionSeverity, string> = {
  critical: "Acil",
  warning: "Uyarı",
  info: "Bilgi",
};

export function severityLabel(value: string | null | undefined): string {
  if (value === "critical" || value === "warning" || value === "info") {
    return SEVERITY_LABEL[value];
  }
  return "Bilgi";
}

const SEVERITY_RANK: Record<string, number> = { critical: 3, warning: 2, info: 1 };

/** Severity → Tailwind tone tokens, so a card colours consistently by urgency. */
export function severityTone(value: string | null | undefined): {
  bg: string;
  text: string;
  dot: string;
} {
  switch (value) {
    case "critical":
      return { bg: "bg-red-50", text: "text-red-700", dot: "bg-red-500" };
    case "warning":
      return { bg: "bg-amber-50", text: "text-amber-700", dot: "bg-amber-500" };
    default:
      return { bg: "bg-blue-50", text: "text-blue-700", dot: "bg-blue-500" };
  }
}

// ── Attention codes → Turkish sentences ───────────────────────────────────────

const ATTENTION_TEXT: Record<
  string,
  { title: string; describe: (n: number) => string }
> = {
  OUT_OF_STOCK: {
    title: "Tükenen stok",
    describe: (n) => `${n} malzeme tükendi veya söz verilenin altında.`,
  },
  CRITICAL_STOCK: {
    title: "Kritik stok",
    describe: (n) => `${n} malzeme kritik seviyede.`,
  },
  DELAYED_KITCHEN: {
    title: "Mutfak gecikiyor",
    describe: (n) => `${n} sipariş beklenenden uzun sürüyor.`,
  },
  OPEN_ISSUES: {
    title: "Açık sorunlu sipariş",
    describe: (n) => `${n} sorunlu sipariş çözüm bekliyor.`,
  },
  SHIFT_DISCREPANCY: {
    title: "Kasa farkı",
    describe: (n) => `${n} vardiya farkla kapandı.`,
  },
  OPEN_SHIFTS: {
    title: "Açık vardiya",
    describe: (n) => `${n} kasa vardiyası hâlâ açık.`,
  },
  UNPAID_ORDERS: {
    title: "Ödenmemiş sipariş",
    describe: (n) => `${n} sipariş henüz tam ödenmedi.`,
  },
};

export interface AttentionRow {
  severity: AttentionSeverity;
  severityLabel: string;
  title: string;
  description: string;
  targetRoute: string | null;
  rank: number;
}

/**
 * Shape one API attention item into a display-safe row. An unknown code degrades
 * to a generic Turkish line rather than leaking the raw enum onto the screen.
 */
export function toAttentionRow(item: AttentionItem): AttentionRow {
  const severity: AttentionSeverity =
    item.severity === "critical" || item.severity === "warning" ? item.severity : "info";
  const text = ATTENTION_TEXT[item.code];
  return {
    severity,
    severityLabel: severityLabel(item.severity),
    title: text ? text.title : "Dikkat",
    description: text ? text.describe(item.count) : "Bu alan dikkat gerektiriyor.",
    targetRoute: item.target_route ?? null,
    rank: SEVERITY_RANK[item.severity] ?? 0,
  };
}

/** All attention items as rows, most-urgent first (stable, deterministic). */
export function toAttentionRows(items: AttentionItem[]): AttentionRow[] {
  return items.map(toAttentionRow);
}

// ── Money / duration formatting ───────────────────────────────────────────────

/** A money string → "1.234,56 ₺" (tr-TR); "0,00 ₺" for a clean zero, never blank. */
export function formatMoney(value: string | number | null | undefined): string {
  if (value == null || value === "") return "0,00 ₺";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "0,00 ₺";
  return `${n.toLocaleString("tr-TR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ₺`;
}

/**
 * Whole seconds → a short Turkish duration ("6 dk 5 sn", "45 sn"), or "—" when the
 * timing layer reported null (no completed data — never faked as 0).
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} sn`;
  const mins = Math.floor(s / 60);
  const rem = s % 60;
  return rem === 0 ? `${mins} dk` : `${mins} dk ${rem} sn`;
}

/** An integer count, or "—" when the value is missing. Never renders "null". */
export function formatCount(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return String(Math.round(value));
}

/** A business date "2026-07-21" → "21 Temmuz 2026", or "" when unparseable. */
export function formatBusinessDate(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(`${value}T00:00:00`);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("tr-TR", { day: "numeric", month: "long", year: "numeric" });
}
