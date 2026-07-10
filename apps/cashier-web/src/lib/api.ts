// Typed cashier API client. Every request sends cookies (`credentials:
// "include"`); every mutation echoes the CSRF token and an Idempotency-Key.
import { API_BASE, UnauthorizedError, csrfHeaders } from "./auth";

// ── Response types ────────────────────────────────────────────────────────────

export interface OpenTable {
  table_id: number;
  table_number: string | null;
  open_order_count: number;
  gross_amount: string;
  paid_amount: string;
  remaining_amount: string;
  oldest_order_at: string | null;
}

export interface OrderBillLine {
  order_id: number;
  order_code: string;
  created_at: string;
  preparation_status: string;
  payment_status: string;
  refund_status: string;
  order_total: string;
  paid_amount: string;
  refunded_amount: string;
  net_paid: string;
  remaining_amount: string;
  payable: boolean;
}

export interface TableBill {
  table_id: number;
  table_number: string | null;
  currency: string;
  gross_amount: string;
  paid_amount: string;
  remaining_amount: string;
  orders: OrderBillLine[];
}

export interface AllocationReceipt {
  id: number;
  order_id: number;
  order_code: string;
  amount: string;
}

export interface SettlementReceipt {
  settlement_id: number;
  table_id: number | null;
  table_number: string | null;
  payment_method: string;
  currency: string;
  gross_amount: string;
  status: string;
  cashier_display: string;
  completed_at: string;
  allocations: AllocationReceipt[];
  idempotent_replay: boolean;
}

export interface RefundReceipt {
  refund_id: number;
  settlement_id: number;
  allocation_id: number;
  order_id: number;
  order_code: string;
  amount: string;
  currency: string;
  reason: string;
  refunded_by_display: string;
  created_at: string;
  idempotent_replay: boolean;
}

export interface RecentTransaction {
  kind: string;
  settlement_id: number;
  refund_id: number | null;
  table_id: number | null;
  payment_method: string | null;
  currency: string;
  amount: string;
  actor_display: string;
  at: string;
}

export type PaymentMethod = "CASH" | "CARD" | "OTHER";

// A typed error carrying the backend's Turkish message + status.
export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
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
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw await parseError(res);
  return res.json();
}

async function postJson<T>(
  path: string,
  body: unknown,
  idempotencyKey: string,
): Promise<T> {
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

// ── Reads ─────────────────────────────────────────────────────────────────────

export function fetchOpenTables(): Promise<{ tables: OpenTable[] }> {
  return getJson("/cashier/tables/open");
}

export function fetchTableBill(tableId: number): Promise<TableBill> {
  return getJson(`/cashier/tables/${tableId}/bill`);
}

export function searchOrder(query: string): Promise<OrderBillLine & { store_id: number; table_id: number | null }> {
  return getJson(`/cashier/orders/search?q=${encodeURIComponent(query)}`);
}

export function fetchRecentTransactions(): Promise<{ transactions: RecentTransaction[] }> {
  return getJson("/cashier/recent-transactions");
}

// ── Mutations ──────────────────────────────────────────────────────────────────

export function settleTable(
  body: { table_id: number; order_ids: number[]; payment_method: PaymentMethod; note?: string | null },
  idempotencyKey: string,
): Promise<SettlementReceipt> {
  return postJson("/cashier/settlements", body, idempotencyKey);
}

export function payOrder(
  orderId: number,
  body: { payment_method: PaymentMethod; amount?: string | null; note?: string | null },
  idempotencyKey: string,
): Promise<SettlementReceipt> {
  return postJson(`/cashier/orders/${orderId}/payments`, body, idempotencyKey);
}

export function refundAllocation(
  allocationId: number,
  body: { amount: string; reason: string },
  idempotencyKey: string,
): Promise<RefundReceipt> {
  return postJson(`/cashier/allocations/${allocationId}/refunds`, body, idempotencyKey);
}
