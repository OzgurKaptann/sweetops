"use client";

import { SHIFT_HISTORY_COPY, toShiftRow, type ShiftLike } from "@/lib/shift-view";

const DISCREPANCY_STYLE: Record<string, string> = {
  balanced: "text-emerald-700",
  short: "text-red-600",
  over: "text-amber-600",
};

/**
 * Store-scoped shift history. One row per shift, one question answered per row:
 * did this cashier's till close correctly? The raw status enum and the raw
 * discrepancy sign never reach the DOM — toShiftRow maps them to Turkish labels.
 */
export function ShiftHistoryTable({
  shifts,
  loading,
}: {
  shifts: ShiftLike[];
  loading: boolean;
}) {
  const c = SHIFT_HISTORY_COPY.columns;

  if (loading) {
    return (
      <div className="bg-white border border-gray-200 rounded-lg px-4 py-8 text-center text-sm text-gray-500">
        Yükleniyor…
      </div>
    );
  }

  if (shifts.length === 0) {
    return (
      <div className="bg-white border border-gray-200 rounded-lg px-4 py-8 text-center text-sm text-gray-500">
        {SHIFT_HISTORY_COPY.empty}
      </div>
    );
  }

  const rows = shifts.map(toShiftRow);

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-gray-500 border-b border-gray-100">
            <th className="px-4 py-3 font-medium">{c.cashier}</th>
            <th className="px-4 py-3 font-medium">{c.status}</th>
            <th className="px-4 py-3 font-medium">{c.opened}</th>
            <th className="px-4 py-3 font-medium">{c.closed}</th>
            <th className="px-4 py-3 font-medium text-right">{c.expectedCash}</th>
            <th className="px-4 py-3 font-medium text-right">{c.countedCash}</th>
            <th className="px-4 py-3 font-medium text-right">{c.discrepancy}</th>
            <th className="px-4 py-3 font-medium text-right">{c.netCollected}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-gray-50 last:border-0">
              <td className="px-4 py-3 font-medium text-gray-900">{r.cashier}</td>
              <td className="px-4 py-3">
                <span
                  className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    r.isClosed
                      ? "bg-gray-100 text-gray-600"
                      : "bg-emerald-100 text-emerald-700"
                  }`}
                >
                  {r.statusLabel}
                </span>
              </td>
              <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{r.openedAt}</td>
              <td className="px-4 py-3 text-gray-600 whitespace-nowrap">{r.closedAt}</td>
              <td className="px-4 py-3 text-right text-gray-700">{r.expectedCash}</td>
              <td className="px-4 py-3 text-right text-gray-700">{r.countedCash}</td>
              <td className="px-4 py-3 text-right">
                {r.isClosed ? (
                  <span className={`font-semibold ${DISCREPANCY_STYLE[r.discrepancyClass]}`}>
                    {r.discrepancyLabel}
                    <span className="ml-1 text-xs font-normal text-gray-500">
                      {r.discrepancyAmount}
                    </span>
                  </span>
                ) : (
                  <span className="text-gray-400">—</span>
                )}
              </td>
              <td className="px-4 py-3 text-right text-gray-700">{r.netCollected}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
