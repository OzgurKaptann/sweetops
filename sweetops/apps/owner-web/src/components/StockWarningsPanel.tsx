"use client";

import { useEffect, useState } from "react";
import { fetchStockStatus, StockStatusData, StockItem } from "@/lib/api";

const SEVERITY_CONFIG = {
  critical: { bg: "bg-red-50", text: "text-red-700", badge: "bg-red-100 text-red-800", icon: "🔴" },
  warning:  { bg: "bg-amber-50", text: "text-amber-700", badge: "bg-amber-100 text-amber-800", icon: "⚠️" },
  low:      { bg: "bg-yellow-50", text: "text-yellow-700", badge: "bg-yellow-100 text-yellow-800", icon: "🟡" },
  ok:       { bg: "bg-green-50", text: "text-green-700", badge: "bg-green-100 text-green-800", icon: "✅" },
};

export function StockWarningsPanel() {
  const [data, setData] = useState<StockStatusData | null>(null);
  const [loading, setLoading] = useState(true);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    fetchStockStatus()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">📦 Stok Durumu</h3>
        <div className="animate-pulse space-y-3">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="h-10 bg-gray-100 rounded" />
          ))}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const alertItems = data.items.filter(i => i.severity !== "ok");
  const visibleItems = showAll ? data.items : (alertItems.length > 0 ? alertItems : data.items.slice(0, 5));

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-bold text-gray-900">📦 Stok Durumu</h3>
        {(data.critical_count > 0 || data.warning_count > 0) && (
          <div className="flex gap-2">
            {data.critical_count > 0 && (
              <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-red-100 text-red-800">
                {data.critical_count} kritik
              </span>
            )}
            {data.warning_count > 0 && (
              <span className="px-2 py-0.5 rounded-full text-xs font-bold bg-amber-100 text-amber-800">
                {data.warning_count} uyarı
              </span>
            )}
          </div>
        )}
      </div>

      <div className="space-y-2">
        {visibleItems.map((item) => {
          const cfg = SEVERITY_CONFIG[item.severity];
          return (
            <div key={item.ingredient_id} className={`flex items-center justify-between p-3 rounded-lg ${cfg.bg}`}>
              <div className="flex items-center gap-2">
                <span className="text-sm">{cfg.icon}</span>
                <div>
                  <span className={`text-sm font-semibold ${cfg.text}`}>{item.ingredient_name}</span>
                  <span className="text-xs text-gray-500 ml-2">{item.message}</span>
                </div>
              </div>
              <div className="text-right">
                <span className={`text-sm font-bold ${cfg.text}`}>
                  {item.stock_quantity.toFixed(0)} {item.unit}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {data.items.length > visibleItems.length && (
        <button
          onClick={() => setShowAll(!showAll)}
          className="mt-3 text-sm text-blue-600 font-medium hover:underline w-full text-center"
        >
          {showAll ? "Sadece uyarıları göster" : `Tümünü göster (${data.total})`}
        </button>
      )}

      {alertItems.length === 0 && (
        <p className="mt-3 text-sm text-green-600 text-center font-medium">
          ✅ Tüm malzemelerin stoğu yeterli
        </p>
      )}
    </div>
  );
}
