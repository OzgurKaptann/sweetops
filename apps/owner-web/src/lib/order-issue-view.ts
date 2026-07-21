/**
 * Owner order-issue history — view logic, free of React/DOM so it is unit-testable.
 *
 * An owner/manager reads this screen to see every problematic order in their branch:
 * what went wrong, how it was resolved, and how much was refunded. The raw enums
 * (issue_type, status, resolution_type) must never reach the table — they become
 * Turkish labels here, once, where they can be tested.
 */

// ── Labels (never render the raw enum) ────────────────────────────────────────

export const ISSUE_TYPE_LABEL: Record<string, string> = {
  CUSTOMER_CANCELLED: "Müşteri iptal etti",
  WRONG_ITEM: "Yanlış ürün",
  MISSING_ITEM: "Eksik ürün",
  QUALITY_PROBLEM: "Kalite sorunu",
  DUPLICATE_ORDER: "Çift sipariş",
  STAFF_ERROR: "Personel hatası",
  OTHER: "Diğer",
};

export const ISSUE_STATUS_LABEL: Record<string, string> = {
  OPEN: "Açık",
  RESOLVED: "Çözüldü",
  VOIDED: "İptal edildi",
};

export const RESOLUTION_LABEL: Record<string, string> = {
  NO_REFUND: "İadesiz çözüldü",
  FULL_REFUND: "Tam iade",
  PARTIAL_REFUND: "Kısmi iade",
  CANCEL_ONLY: "Sadece iptal",
};

export function issueTypeLabel(v: string | null | undefined): string {
  if (!v) return "Bilinmiyor";
  return ISSUE_TYPE_LABEL[v] ?? "Bilinmiyor";
}

export function issueStatusLabel(v: string | null | undefined): string {
  if (!v) return "Bilinmiyor";
  return ISSUE_STATUS_LABEL[v] ?? "Bilinmiyor";
}

export function resolutionLabel(v: string | null | undefined): string {
  if (!v) return "—";
  return RESOLUTION_LABEL[v] ?? "—";
}

// ── Copy ──────────────────────────────────────────────────────────────────────

export const ISSUE_HISTORY_COPY = {
  heading: "Sorunlu siparişler",
  empty: "Bu şubede henüz sipariş sorunu kaydı yok.",
  loadError: "Sipariş sorunları yüklenemiyor. Lütfen daha sonra tekrar deneyin.",
  columns: {
    order: "Sipariş",
    issueType: "Sorun türü",
    status: "Durum",
    resolution: "Çözüm",
    refundAmount: "İade tutarı",
    createdBy: "Oluşturan",
    resolvedBy: "Çözen",
    date: "Tarih",
  },
} as const;

// ── Money / date formatting ───────────────────────────────────────────────────

/** A money string → "1.234,56 ₺" (tr-TR), or "—" when absent (no refund). */
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

export interface OrderIssueLike {
  id: number;
  order_id: number;
  order_code: string;
  issue_type: string;
  status: string;
  resolution_type: string | null;
  approved_refund_amount: string | null;
  created_by_display: string;
  resolved_by_display: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface OrderIssueRow {
  id: number;
  orderCode: string;
  issueTypeLabel: string;
  statusLabel: string;
  isResolved: boolean;
  resolutionLabel: string;
  refundAmount: string;
  createdBy: string;
  resolvedBy: string;
  createdAt: string;
}

/**
 * Shape one API issue into a fully display-safe row. Nothing raw survives: the type,
 * status and resolution are labels; the refund amount is formatted (or "—" when
 * there was no refund); an unresolved issue's empty resolver becomes "—".
 */
export function toOrderIssueRow(issue: OrderIssueLike): OrderIssueRow {
  const isResolved = issue.status === "RESOLVED" || issue.status === "VOIDED";
  return {
    id: issue.id,
    orderCode: issue.order_code,
    issueTypeLabel: issueTypeLabel(issue.issue_type),
    statusLabel: issueStatusLabel(issue.status),
    isResolved,
    resolutionLabel: resolutionLabel(issue.resolution_type),
    refundAmount: formatMoney(issue.approved_refund_amount),
    createdBy: issue.created_by_display || "—",
    resolvedBy: issue.resolved_by_display || "—",
    createdAt: formatDateTime(issue.created_at),
  };
}
