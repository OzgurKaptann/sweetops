// Owner-web cashier-shift read client. Shift history is a store-scoped read;
// owner-web never opens or closes a shift (that is the cashier's till action).
import { API_BASE, UnauthorizedError } from "./auth";

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

export interface ShiftListResponse {
  shifts: Shift[];
}

export async function fetchShifts(params?: {
  status?: string;
  limit?: number;
}): Promise<ShiftListResponse> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await fetch(`${API_BASE}/cashier/shifts${suffix}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error("shift_history_failed");
  return res.json();
}
