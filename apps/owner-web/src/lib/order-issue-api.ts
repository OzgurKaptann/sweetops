// Owner-web order-issue read client. Issue history is a store-scoped read; owner-web
// never creates or resolves an issue (that is the cashier's / manager's till action).
import { API_BASE, UnauthorizedError } from "./auth";

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

export interface OrderIssueListResponse {
  issues: OrderIssue[];
}

export async function fetchOrderIssues(params?: {
  status?: string;
  issue_type?: string;
  limit?: number;
}): Promise<OrderIssueListResponse> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.issue_type) qs.set("issue_type", params.issue_type);
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await fetch(`${API_BASE}/order-issues${suffix}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error("order_issue_history_failed");
  return res.json();
}
