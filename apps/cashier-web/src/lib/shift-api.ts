// Typed cashier-shift API client. Every request sends cookies; every mutation
// echoes the CSRF token and an Idempotency-Key, exactly like the payment client.
import { API_BASE, UnauthorizedError, csrfHeaders } from "./auth";
import { ApiError } from "./api";

export interface Shift {
  id: number;
  store_id: number;
  cashier_user_id: number;
  cashier_display: string;
  status: string; // OPEN | CLOSED — never rendered raw
  opened_at: string;
  closed_at: string | null;
  opening_cash_amount: string;
  open_note: string | null;
  close_note: string | null;
  counted_closing_cash_amount: string | null;
  cash_payments_amount: string | null;
  cash_refunds_amount: string | null;
  expected_closing_cash_amount: string | null;
  cash_discrepancy_amount: string | null;
  card_payments_amount: string | null;
  card_refunds_amount: string | null;
  gross_payments_amount: string | null;
  total_refunds_amount: string | null;
  net_collected_amount: string | null;
  idempotent_replay: boolean;
}

async function parseError(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => ({}));
  const detail = body?.detail;
  if (detail && typeof detail === "object") {
    return new ApiError(res.status, detail.error || "error", detail.message || "İşlem başarısız.");
  }
  return new ApiError(res.status, "error", "İşlem başarısız.");
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { credentials: "include", cache: "no-store" });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw await parseError(res);
  return res.json();
}

async function postJson<T>(path: string, body: unknown, idempotencyKey: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
      ...csrfHeaders(),
    },
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw await parseError(res);
  return res.json();
}

export function fetchCurrentShift(): Promise<{ current_shift: Shift | null }> {
  return getJson("/cashier/shifts/current");
}

export function openShift(
  body: { opening_cash_amount: string; open_note?: string | null },
  idempotencyKey: string,
): Promise<Shift> {
  return postJson("/cashier/shifts/open", body, idempotencyKey);
}

export function closeShift(
  shiftId: number,
  body: { counted_closing_cash_amount: string; close_note?: string | null },
  idempotencyKey: string,
): Promise<Shift> {
  return postJson(`/cashier/shifts/${shiftId}/close`, body, idempotencyKey);
}
