/**
 * Kitchen preparation timing — presentation helpers.
 *
 * The API sends timing as integer seconds (or null when a lifecycle step hasn't
 * happened) and delay state as an English enum (ok / warning / critical). This
 * module is the single place those become Turkish, cook-readable copy. A raw
 * enum or a bare number of seconds must never reach the screen.
 */
export type DelayState = "ok" | "warning" | "critical";

/** Unknown values render as `fallback`, never as the raw enum. */
function labelFor(
  map: Record<string, string>,
  value: string | null | undefined,
  fallback: string,
): string {
  if (!value) return fallback;
  return map[value] ?? fallback;
}

export interface OrderTiming {
  order_id: number;
  status: string;
  queued_seconds: number | null;
  prep_seconds: number | null;
  time_to_ready_seconds: number | null;
  queued_seconds_active: number | null;
  prep_seconds_active: number | null;
  active_seconds: number | null;
  is_delayed: boolean;
  delay_state: DelayState;
  delay_reason: string | null;
}

export interface ActiveTimingSummary {
  active_orders: number;
  waiting_orders: number;
  in_prep_orders: number;
  ready_orders: number;
  delayed_orders: number;
}

/** Delay state → Turkish badge copy. */
export const DELAY_STATE_LABEL: Record<string, string> = {
  ok: "Zamanında",
  warning: "Gecikiyor",
  critical: "Kritik gecikme",
};

/** Which phase is slow, in plain Turkish. */
export const DELAY_REASON_LABEL: Record<string, string> = {
  queue_warning: "Sırada bekliyor",
  queue_critical: "Sırada çok uzun bekliyor",
  prep_warning: "Hazırlık uzuyor",
  prep_critical: "Hazırlık çok uzun sürüyor",
};

export const delayStateLabel = (v: string | null | undefined) =>
  labelFor(DELAY_STATE_LABEL, v, "Zamanında");

export const delayReasonLabel = (v: string | null | undefined) =>
  v ? labelFor(DELAY_REASON_LABEL, v, "") : "";

/**
 * Seconds → Turkish elapsed string.
 *
 *   null/undefined       → "—"        (unknown, never fabricated as 0)
 *   < 60s                → "1 dk'dan az"
 *   whole minutes        → "12 dk"
 *   minutes + seconds    → "12 dk 30 sn"
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 0) return "—";
  if (seconds < 60) return "1 dk'dan az";
  const mins = Math.floor(seconds / 60);
  const secs = Math.round(seconds % 60);
  if (secs === 0) return `${mins} dk`;
  return `${mins} dk ${secs} sn`;
}

/** A labelled timing line for an order card, chosen by its current phase. */
export interface TimingLine {
  label: string;
  value: string;
}

/**
 * The timing lines to show on a card, derived from the order's phase.
 *
 *   NEW      → how long it has been waiting (live)
 *   IN_PREP  → how long it waited (completed) + how long it's been cooking (live)
 *   READY    → total time to ready (completed)
 *
 * Every value goes through formatDuration, so a missing timing renders "—", not
 * a fabricated number.
 */
export function timingLines(t: OrderTiming): TimingLine[] {
  if (t.status === "NEW") {
    return [{ label: "Bekleme süresi", value: formatDuration(t.queued_seconds_active) }];
  }
  if (t.status === "IN_PREP") {
    return [
      { label: "Bekleme süresi", value: formatDuration(t.queued_seconds) },
      { label: "Hazırlık süresi", value: formatDuration(t.prep_seconds_active) },
    ];
  }
  if (t.status === "READY") {
    return [{ label: "Toplam süre", value: formatDuration(t.time_to_ready_seconds) }];
  }
  return [];
}

/** Short Turkish phase note for a card (never the raw enum). */
export function prepPhaseNote(t: OrderTiming): string {
  if (t.status === "NEW") return "Henüz başlamadı";
  if (t.status === "IN_PREP") return "Hazırlık başladı";
  if (t.status === "READY") return "Hazırlandı";
  return "";
}

/** Turkish error copy for a failed timing fetch. */
export const TIMING_ERROR_MESSAGE =
  "Mutfak zamanlama verileri yüklenemedi. Bağlantı yeniden kuruluyor…";
