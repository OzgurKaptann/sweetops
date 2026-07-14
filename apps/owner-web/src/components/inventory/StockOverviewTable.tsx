"use client";

import type { StockItem } from "@/lib/inventory-api";
import {
  INVENTORY_COPY,
  RESERVED_STOCK_NOTE,
  type StockRow,
  type StockStatus,
  toStockRow,
} from "@/lib/inventory-view";

/**
 * Stock overview — physical, reserved and available stock per ingredient, for the
 * manager's own branch.
 *
 * Every cell comes from a `StockRow`, which is already Turkish and already
 * formatted (see lib/inventory-view.ts). This component chooses colours and
 * nothing else; it has no access to a raw quantity or a status enum to print.
 */

const STATUS_STYLE: Record<StockStatus, string> = {
  out: "bg-red-100 text-red-800",
  insufficient: "bg-amber-100 text-amber-800",
  low: "bg-yellow-100 text-yellow-800",
  ok: "bg-emerald-100 text-emerald-800",
};

export function StockOverviewTable({
  items,
  loading,
  onSelectIngredient,
}: {
  items: StockItem[];
  loading: boolean;
  onSelectIngredient?: (ingredientId: number) => void;
}) {
  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-6">
        <div className="animate-pulse space-y-3" aria-label={INVENTORY_COPY.loading}>
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-9 bg-gray-100 rounded" />
          ))}
        </div>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 py-16 px-6 text-center">
        <div className="text-3xl mb-3">📦</div>
        <p className="text-sm font-semibold text-gray-700">{INVENTORY_COPY.stockEmpty}</p>
        <p className="text-xs text-gray-400 mt-1">{INVENTORY_COPY.stockEmptyHint}</p>
      </div>
    );
  }

  const rows: StockRow[] = items.map(toStockRow);
  const atRiskCount = rows.filter((r) => r.atRisk).length;

  return (
    <div className="bg-white rounded-xl border border-gray-100 overflow-hidden">
      {atRiskCount > 0 && (
        <div className="px-4 py-2 bg-amber-50 border-b border-amber-100">
          <p className="text-xs font-semibold text-amber-800">
            {atRiskCount} malzemede stok tükenme riski var.
          </p>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
              <th className="px-4 py-3">Malzeme</th>
              <th className="px-4 py-3 text-right">Fiziksel stok</th>
              <th className="px-4 py-3 text-right">Ayrılmış stok</th>
              <th className="px-4 py-3 text-right">Kullanılabilir stok</th>
              <th className="px-4 py-3">Birim</th>
              <th className="px-4 py-3">Durum</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((row) => (
              <tr
                key={row.ingredientId}
                onClick={() => onSelectIngredient?.(row.ingredientId)}
                className={`${onSelectIngredient ? "cursor-pointer hover:bg-gray-50" : ""} transition-colors`}
              >
                <td className="px-4 py-3">
                  <span className="font-medium text-gray-900">{row.ingredientName}</span>
                  {row.reservedNote && (
                    <span className="block text-xs text-gray-400 mt-0.5">
                      {RESERVED_STOCK_NOTE}
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-gray-900">
                  {row.onHand}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-gray-500">
                  {row.reserved}
                </td>
                <td className="px-4 py-3 text-right tabular-nums font-semibold text-gray-900">
                  {row.available}
                </td>
                <td className="px-4 py-3 text-gray-500">{row.unit}</td>
                <td className="px-4 py-3">
                  <span
                    className={`inline-block px-2 py-0.5 rounded-full text-xs font-semibold ${STATUS_STYLE[row.status]}`}
                  >
                    {row.statusLabel}
                  </span>
                  {row.riskLabel && (
                    <span className="block text-xs text-gray-400 mt-0.5">{row.riskLabel}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
