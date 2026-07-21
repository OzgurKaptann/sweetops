// Typed order-issue API client. Every request sends cookies; every mutation echoes
// the CSRF token and an Idempotency-Key, exactly like the payment/shift clients.
import { API_BASE, UnauthorizedError, csrfHeaders } from "./auth";
import { ApiError } from "./api";

export interface OrderIssue {
  id: number;
  store_id: number;
  order_id: number;
  order_code: string;
  issue_type: string; // never rendered raw
  status: string; // OPEN | RESOLVED | VOIDED — never rendered raw
  resolution_type: string | null;
  requested_refund_amount: string | null;
  approved_refund_amount: string | null;
  refund_id: number | null;
  reason: string;
  note: string | null;
  created_by_user_id: number;
  created_by_display: string;
  resolved_by_user_id: number | null;
  resolved_by_display: string | null;
  created_at: string;
  resolved_at: string | null;
  order_refundable_amount: string;
  idempotent_replay: boolean;
}

export type IssueType =
  | "CUSTOMER_CANCELLED"
  | "WRONG_ITEM"
  | "MISSING_ITEM"
  | "QUALITY_PROBLEM"
  | "DUPLICATE_ORDER"
  | "STAFF_ERROR"
  | "OTHER";

export type ResolutionType = "NO_REFUND" | "FULL_REFUND" | "PARTIAL_REFUND" | "CANCEL_ONLY";

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

export function fetchOrderIssues(orderId: number): Promise<{ issues: OrderIssue[] }> {
  return getJson(`/orders/${orderId}/issues`);
}

export function fetchIssues(params?: {
  status?: string;
  issue_type?: string;
  order_id?: number;
}): Promise<{ issues: OrderIssue[] }> {
  const q = new URLSearchParams();
  if (params?.status) q.set("status", params.status);
  if (params?.issue_type) q.set("issue_type", params.issue_type);
  if (params?.order_id != null) q.set("order_id", String(params.order_id));
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return getJson(`/order-issues${suffix}`);
}

export function createOrderIssue(
  orderId: number,
  body: { issue_type: IssueType; requested_refund_amount?: string | null; reason: string; note?: string | null },
  idempotencyKey: string,
): Promise<OrderIssue> {
  return postJson(`/orders/${orderId}/issues`, body, idempotencyKey);
}

export function resolveOrderIssue(
  issueId: number,
  body: { resolution_type: ResolutionType; approved_refund_amount?: string | null; reason: string; note?: string | null },
  idempotencyKey: string,
): Promise<OrderIssue> {
  return postJson(`/order-issues/${issueId}/resolve`, body, idempotencyKey);
}
