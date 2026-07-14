"use client";

import type { MovementItem } from "@/lib/inventory-api";
import { MOVEMENT_TYPE_LABEL } from "@/lib/labels";
import { INVENTORY_COPY, toMovementRow } from "@/lib/inventory-view";

/**
 * Recent stock movements — the append-only ledger, newest first.
 *
 * The filter dropdown is the one place a raw `movement_type` still exists in this
 * component, and it exists only as the VALUE of an option whose visible text is
 * Turkish. That is the wire contract (the API filters by `TRANSFER_OUT`, not by
 * "Şubeden çıkış"); the manager never sees it.
 */

const MOVEMENT_TYPE_OPTIONS = Object.keys(MOVEMENT_TYPE_LABEL);

export function MovementHistoryTable({
  items,
  loading,
  movementType,
  onMovementTypeChange,
}: {
  items: MovementItem[];
  loading: boolean;
  movementType: string;
  onMovementTypeChange: (value: string) => void;
}) {
  const rows = items.map(toMovementRow);

  return (
    <div className="bg-white rounded-xl border border-gray-100 overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-gray-100">
        <h3 className="text-sm font-semibold text-gray-900">Stok hareketleri</h3>
        <label className="flex items-center gap-2">
          <span className="text-xs text-gray-500">Hareket türü</span>
          <select
            value={movementType}
            onChange={(e) => onMovementTypeChange(e.target.value)}
            className="text-xs border border-gray-200 rounded-lg px-2 py-1.5 text-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          >
            <option value="">Tümü</option>
            {MOVEMENT_TYPE_OPTIONS.map((type) => (
              <option key={type} value={type}>
                {MOVEMENT_TYPE_LABEL[type]}
              </option>
            ))}
          </select>
        </label>
      </div>

      {loading ? (
        <div className="p-6 animate-pulse space-y-3" aria-label={INVENTORY_COPY.loading}>
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-8 bg-gray-100 rounded" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="py-14 px-6 text-center">
          <p className="text-sm font-semibold text-gray-700">{INVENTORY_COPY.movementsEmpty}</p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">
                <th className="px-4 py-3">Tarih</th>
                <th className="px-4 py-3">Malzeme</th>
                <th className="px-4 py-3">Hareket türü</th>
                <th className="px-4 py-3 text-right">Miktar</th>
                <th className="px-4 py-3 text-right">Fiziksel stok etkisi</th>
                <th className="px-4 py-3 text-right">Ayrılmış stok etkisi</th>
                <th className="px-4 py-3">Açıklama</th>
                <th className="px-4 py-3">İşlemi yapan</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((row) => (
                <tr key={row.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3 whitespace-nowrap text-gray-500">{row.at}</td>
                  <td className="px-4 py-3 font-medium text-gray-900">{row.ingredientName}</td>
                  <td className="px-4 py-3 text-gray-700">{row.typeLabel}</td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-900">
                    {row.quantity}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-700">
                    {row.onHandEffect}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums text-gray-500">
                    {row.reservedEffect}
                  </td>
                  <td className="px-4 py-3 text-gray-500 max-w-xs truncate" title={row.reason}>
                    {row.reason}
                  </td>
                  <td className="px-4 py-3 text-gray-500 whitespace-nowrap">{row.actor}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
