"use client";

import type { ThresholdAlertItem, ThresholdAlertSummary } from "@/lib/inventory-api";
import {
  INVENTORY_COPY,
  RECOMMENDED_RESTOCK_HINT,
  THRESHOLD_COPY,
  THRESHOLD_LABELS,
  type ThresholdRow,
  type ThresholdSummaryCard,
  thresholdSummaryCards,
  toThresholdRow,
  totalRecommendedRestockLabel,
} from "@/lib/inventory-view";

/**
 * Stok uyarıları — which ingredients this branch needs to do something about, before
 * the shelf is empty and a customer is standing at the counter.
 *
 * Every cell comes from a `ThresholdRow`, which is already Turkish and already
 * formatted (see lib/inventory-view.ts). This component chooses colours and nothing
 * else: it renders `row.statusLabel`, never `row.status`, so a raw `NOT_CONFIGURED`
 * has no path to the screen.
 *
 * The columns show AVAILABLE stock, not on-hand, because that is what the status was
 * computed against — showing a full-looking on-hand figure beside a "Düşük stok" badge
 * would look like a bug and get the alert ignored. What makes the two differ is
 * reserved stock, and the row that is low BECAUSE of reservations is exactly the row a
 * manager most needs to understand.
 *
 * Nothing here is a purchase order. "Önerilen tamamlama" is a number a manager reads
 * and then decides about; it orders nothing and names no supplier.
 */

const CARD_STYLE: Record<ThresholdSummaryCard["tone"], string> = {
  danger: "bg-red-50 border-red-200 text-red-800",
  warning: "bg-amber-50 border-amber-200 text-amber-800",
  neutral: "bg-gray-50 border-gray-200 text-gray-700",
};

const STATUS_STYLE: Record<string, string> = {
  BELOW_RESERVED: "bg-red-200 text-red-900",
  OUT_OF_STOCK: "bg-red-100 text-red-800",
  CRITICAL: "bg-orange-100 text-orange-800",
  LOW: "bg-yellow-100 text-yellow-800",
  HEALTHY: "bg-emerald-100 text-emerald-800",
  NOT_CONFIGURED: "bg-gray-100 text-gray-600",
};

/** An unknown status still gets a neutral badge — and its label, never its value. */
const statusStyle = (status: string) => STATUS_STYLE[status] ?? STATUS_STYLE.NOT_CONFIGURED;

export function ThresholdAlertsPanel({
  items,
  summary,
  loading,
  onEditThresholds,
}: {
  items: ThresholdAlertItem[];
  summary: ThresholdAlertSummary | null;
  loading: boolean;
  onEditThresholds?: (ingredientId: number) => void;
}) {
  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-6">
        <div className="animate-pulse space-y-3" aria-label={INVENTORY_COPY.loading}>
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-9 bg-gray-100 rounded" />
          ))}
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 py-16 px-6 text-center">
        <div className="text-3xl mb-3">🔔</div>
        <p className="text-sm font-semibold text-gray-700">{THRESHOLD_COPY.empty}</p>
        <p className="text-xs text-gray-400 mt-1">{THRESHOLD_COPY.emptyHint}</p>
      </div>
    );
  }

  const rows: ThresholdRow[] = items.map(toThresholdRow);
  const cards = summary ? thresholdSummaryCards(summary) : [];
  const restockTotal = summary ? totalRecommendedRestockLabel(summary) : null;

  return (
    <div className="space-y-3">
      {/* Summary cards — what needs a decision, at a glance. */}
      {cards.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {cards.map((card) => (
            <div
              key={card.key}
              className={`border rounded-lg px-3 py-2 ${CARD_STYLE[card.tone]}`}
            >
              <p className="text-xs font-medium opacity-80">{card.label}</p>
              <p className="text-xl font-bold tabular-nums mt-0.5">{card.count}</p>
            </div>
          ))}
        </div>
      )}

      {restockTotal && (
        <p className="text-xs text-gray-500" title={RECOMMENDED_RESTOCK_HINT}>
          {restockTotal}
        </p>
      )}

      <div className="bg-white rounded-xl border border-gray-100 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                <th className="px-4 py-3">{THRESHOLD_LABELS.ingredient}</th>
                <th className="px-4 py-3 text-right">{THRESHOLD_LABELS.available}</th>
                <th className="px-4 py-3">{THRESHOLD_LABELS.status}</th>
                <th className="px-4 py-3 text-right">{THRESHOLD_LABELS.critical}</th>
                <th className="px-4 py-3 text-right">{THRESHOLD_LABELS.minimum}</th>
                <th className="px-4 py-3 text-right">{THRESHOLD_LABELS.target}</th>
                <th className="px-4 py-3 text-right" title={RECOMMENDED_RESTOCK_HINT}>
                  {THRESHOLD_LABELS.recommendedRestock}
                </th>
                {onEditThresholds && <th className="px-4 py-3" />}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((row) => (
                <tr
                  key={row.ingredientId}
                  className={row.needsAttention ? "bg-amber-50/40" : undefined}
                >
                  <td className="px-4 py-3 font-medium text-gray-900">
                    {row.ingredientName}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums font-semibold text-gray-900">
                    {row.available} {row.unit}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={`inline-block px-2 py-0.5 rounded-full text-xs font-semibold ${statusStyle(row.status)}`}
                    >
                      {/* The LABEL. The raw status never reaches the DOM. */}
                      {row.statusLabel}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-500">
                    {row.critical}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-500">
                    {row.minimum}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-500">
                    {row.target}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-900">
                    {row.recommendedRestock}
                  </td>
                  {onEditThresholds && (
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => onEditThresholds(row.ingredientId)}
                        className="text-xs px-3 py-1.5 rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 transition-colors whitespace-nowrap"
                      >
                        Eşik düzenle
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
