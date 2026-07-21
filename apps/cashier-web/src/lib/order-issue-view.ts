/**
 * Order issue & controlled refund — the view logic a screen needs, kept free of
 * React/DOM so it can be unit-tested as pure TypeScript.
 *
 * The API speaks English enums (CUSTOMER_CANCELLED, FULL_REFUND, OPEN); that is the
 * wire contract and is never translated in comparisons. This module is the single
 * place those become something a cashier reads, so a raw `PARTIAL_REFUND` can never
 * leak onto the screen. Everything is looked up through the helpers, which decide
 * what to show for a value this build has never seen.
 */

// ── Labels (never render the raw enum) ────────────────────────────────────────

/** Issue type. Wire values: CUSTOMER_CANCELLED | WRONG_ITEM | ... | OTHER. */
export const ISSUE_TYPE_LABEL: Record<string, string> = {
  CUSTOMER_CANCELLED: "Müşteri iptal etti",
  WRONG_ITEM: "Yanlış ürün",
  MISSING_ITEM: "Eksik ürün",
  QUALITY_PROBLEM: "Kalite sorunu",
  DUPLICATE_ORDER: "Çift sipariş",
  STAFF_ERROR: "Personel hatası",
  OTHER: "Diğer",
};

/** Issue lifecycle status. Wire values: OPEN | RESOLVED | VOIDED. */
export const ISSUE_STATUS_LABEL: Record<string, string> = {
  OPEN: "Açık",
  RESOLVED: "Çözüldü",
  VOIDED: "İptal edildi",
};

/** Resolution type. Wire values: NO_REFUND | FULL_REFUND | PARTIAL_REFUND | CANCEL_ONLY. */
export const RESOLUTION_LABEL: Record<string, string> = {
  NO_REFUND: "İadesiz çözüldü",
  FULL_REFUND: "Tam iade",
  PARTIAL_REFUND: "Kısmi iade",
  CANCEL_ONLY: "Sadece iptal",
};

/** The action buttons a cashier picks between when resolving an issue. */
export const RESOLUTION_ACTION_LABEL: Record<string, string> = {
  NO_REFUND: "İadesiz çöz",
  CANCEL_ONLY: "Sadece iptal",
  FULL_REFUND: "Tam iade",
  PARTIAL_REFUND: "Kısmi iade",
};

function look(map: Record<string, string>, value: string | null | undefined, fallback = "Bilinmiyor"): string {
  if (!value) return fallback;
  return map[value] ?? fallback;
}

export const issueTypeLabel = (v: string | null | undefined) => look(ISSUE_TYPE_LABEL, v);
export const issueStatusLabel = (v: string | null | undefined) => look(ISSUE_STATUS_LABEL, v);
export const resolutionLabel = (v: string | null | undefined) => look(RESOLUTION_LABEL, v, "—");
export const resolutionActionLabel = (v: string | null | undefined) => look(RESOLUTION_ACTION_LABEL, v);

/** The issue types offered in the create form, in operator-priority order. */
export const ISSUE_TYPE_ORDER = [
  "CUSTOMER_CANCELLED",
  "WRONG_ITEM",
  "MISSING_ITEM",
  "QUALITY_PROBLEM",
  "DUPLICATE_ORDER",
  "STAFF_ERROR",
  "OTHER",
] as const;

/** The resolutions offered when resolving, in operator-priority order. */
export const RESOLUTION_ORDER = [
  "NO_REFUND",
  "CANCEL_ONLY",
  "FULL_REFUND",
  "PARTIAL_REFUND",
] as const;

// ── Field labels & copy (Turkish) ─────────────────────────────────────────────

export const ISSUE_LABELS = {
  panelTitle: "Sipariş sorunu",
  record: "Sorun kaydet",
  resolve: "Sorunu çöz",
  issueType: "Sorun türü",
  reason: "Sebep",
  note: "Not",
  requestedRefund: "İade tutarı",
  approvedRefund: "Onaylanan iade",
  remainingRefundable: "Kalan iade edilebilir tutar",
  createdBy: "Oluşturan",
  resolvedBy: "Çözen",
  status: "Durum",
  resolution: "Çözüm",
  order: "Sipariş",
  date: "Tarih",
} as const;

export const ISSUE_COPY = {
  createSuccess: "Sorun kaydedildi.",
  resolveSuccess: "Sorun çözüldü.",
  refundCreated: "İade başarıyla oluşturuldu.",
  refundOverRemaining: "İade tutarı kalan iade edilebilir tutarı aşamaz.",
  stockNotRestored: "Hazırlanmış siparişin stoğu otomatik geri alınmaz.",
  cancelBlockedPaid: "Tahsilatı yapılmış sipariş sadece iptal edilemez. Tam iade ile çözerek tahsilatı iade edin.",
  refundForbidden: "İade işlemi için yetkiniz yok. Bu çözümü yönetici veya işletme sahibi onaylamalı.",
  reasonRequired: "Sebep girmeniz gerekiyor.",
  amountRequired: "Kısmi iade için onaylanan tutarı girmeniz gerekiyor.",
  nothingRefundable: "Bu siparişin iade edilebilir bakiyesi yok. İadesiz çözün veya sadece iptal edin.",
  noOpenIssues: "Açık sipariş sorunu yok.",
  emptyHistory: "Bu şubede henüz sipariş sorunu kaydı yok.",
  // Network uncertainty: do NOT blind-retry — check order status first.
  uncertain:
    "Bu işlem doğrulanamadı. " +
    "Aynı işlemi tekrar göndermeden önce sipariş durumunu kontrol edin.",
} as const;

// ── Validation ────────────────────────────────────────────────────────────────

const _num = (raw: string | number | null | undefined): number =>
  typeof raw === "number" ? raw : Number.parseFloat(String(raw ?? ""));

/**
 * Errors for the requested-refund field on the CREATE form. Empty and unset are
 * allowed (an issue may be raised with no refund request). A supplied value must be
 * a non-negative number that does not exceed the remaining refundable amount.
 */
export function validateRequestedRefund(
  raw: string,
  remainingRefundable: string | number,
): string[] {
  const s = (raw ?? "").trim();
  if (s === "") return [];
  const n = Number(s);
  if (!Number.isFinite(n) || n < 0) return ["İade tutarı geçerli bir tutar olmalı."];
  if (n > _num(remainingRefundable) + 1e-9) return [ISSUE_COPY.refundOverRemaining];
  return [];
}

/**
 * Errors for the approved-refund field when resolving with PARTIAL_REFUND. A
 * positive amount not exceeding the remaining refundable amount is required.
 * (FULL_REFUND takes the whole remaining amount and needs no field; NO_REFUND /
 * CANCEL_ONLY take no amount at all.)
 */
export function validatePartialRefund(
  raw: string,
  remainingRefundable: string | number,
): string[] {
  const s = (raw ?? "").trim();
  if (s === "") return [ISSUE_COPY.amountRequired];
  const n = Number(s);
  if (!Number.isFinite(n) || n <= 0) return [ISSUE_COPY.amountRequired];
  if (n > _num(remainingRefundable) + 1e-9) return [ISSUE_COPY.refundOverRemaining];
  return [];
}

/** True when this resolution moves money and therefore needs payments:refund. */
export function resolutionNeedsRefundPermission(resolution: string): boolean {
  return resolution === "FULL_REFUND" || resolution === "PARTIAL_REFUND";
}

// ── Command fingerprints (idempotency) ────────────────────────────────────────

export interface CreateIssueCommand {
  kind: "issue_create";
  orderId: number;
  issueType: string;
  requestedRefund?: string | null;
  reason: string;
  note?: string | null;
}

export interface ResolveIssueCommand {
  kind: "issue_resolve";
  issueId: number;
  resolutionType: string;
  approvedRefund?: string | null;
  reason: string;
  note?: string | null;
}

export type IssueCommand = CreateIssueCommand | ResolveIssueCommand;

/**
 * Deterministic fingerprint of the logical command. Every field that changes what
 * the backend persists is included, so editing the resolution or the amount mints a
 * fresh idempotency key rather than replaying the previous decision.
 */
export function fingerprintIssueCommand(cmd: IssueCommand): string {
  if (cmd.kind === "issue_create") {
    return JSON.stringify({
      kind: cmd.kind,
      orderId: cmd.orderId,
      issueType: cmd.issueType,
      requestedRefund: cmd.requestedRefund ?? null,
      reason: cmd.reason,
      note: cmd.note ?? null,
    });
  }
  return JSON.stringify({
    kind: cmd.kind,
    issueId: cmd.issueId,
    resolutionType: cmd.resolutionType,
    approvedRefund: cmd.approvedRefund ?? null,
    reason: cmd.reason,
    note: cmd.note ?? null,
  });
}
