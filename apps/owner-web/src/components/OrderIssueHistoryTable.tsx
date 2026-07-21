"use client";

import {
  ISSUE_HISTORY_COPY,
  toOrderIssueRow,
  type OrderIssueLike,
} from "@/lib/order-issue-view";

/**
 * Store-scoped order-issue history. One row per problematic order: what went wrong,
 * how it was resolved, how much was refunded, and by whom. The raw enums never reach
 * the DOM — toOrderIssueRow maps them to Turkish labels.
 */
export function OrderIssueHistoryTable({
  issues,
  loading,
}: {
  issues: OrderIssueLike[];
  loading: boolean;
}) {
  const c = ISSUE_HISTORY_COPY.columns;

  if (loading) {
    return (
      <div className="bg-white border border-gray-200 rounded-lg px-4 py-8 text-center text-sm text-gray-500">
        Yükleniyor…
      </div>
    );
  }

  if (issues.length === 0) {
    return (
      <div className="bg-white border border-gray-200 rounded-lg px-4 py-8 text-center text-sm text-gray-500">
        {ISSUE_HISTORY_COPY.empty}
      </div>
    );
  }

  const rows = issues.map(toOrderIssueRow);

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-100">
            <th className="px-4 py-3 font-medium">{c.order}</th>
            <th className="px-4 py-3 font-medium">{c.issueType}</th>
            <th className="px-4 py-3 font-medium">{c.status}</th>
            <th className="px-4 py-3 font-medium">{c.resolution}</th>
            <th className="px-4 py-3 font-medium text-right">{c.refundAmount}</th>
            <th className="px-4 py-3 font-medium">{c.createdBy}</th>
            <th className="px-4 py-3 font-medium">{c.resolvedBy}</th>
            <th className="px-4 py-3 font-medium whitespace-nowrap">{c.date}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-gray-50 last:border-0">
              <td className="px-4 py-3 font-medium text-gray-900 whitespace-nowrap">{r.orderCode}</td>
              <td className="px-4 py-3 text-gray-700">{r.issueTypeLabel}</td>
              <td className="px-4 py-3">
                <span
                  className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    r.isResolved
                      ? "bg-gray-100 text-gray-600"
                      : "bg-amber-100 text-amber-700"
                  }`}
                >
                  {r.statusLabel}
                </span>
              </td>
              <td className="px-4 py-3 text-gray-700">{r.resolutionLabel}</td>
              <td className="px-4 py-3 text-right text-gray-700">{r.refundAmount}</td>
              <td className="px-4 py-3 text-gray-600">{r.createdBy}</td>
              <td className="px-4 py-3 text-gray-600">{r.resolvedBy}</td>
              <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{r.createdAt}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
