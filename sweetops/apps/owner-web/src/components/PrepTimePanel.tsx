"use client";

import { useEffect, useState } from "react";
import { fetchPrepTime } from "@/lib/api";

interface PrepData {
  avg_prep_seconds: number | null;
  avg_prep_display: string;
  fastest_seconds: number | null;
  fastest_display: string;
  slowest_seconds: number | null;
  slowest_display: string;
  total_tracked: number;
  recent_orders: Array<{
    order_id: number;
    prep_seconds: number;
    prep_display: string;
    completed_at: string;
  }>;
}

export function PrepTimePanel() {
  const [data, setData] = useState<PrepData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchPrepTime()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">⏱️ Hazırlık Süresi</h3>
        <div className="animate-pulse h-24 bg-gray-100 rounded" />
      </div>
    );
  }

  if (!data || data.avg_prep_seconds === null) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-2">⏱️ Hazırlık Süresi</h3>
        <p className="text-gray-400 text-sm text-center py-4">
          Henüz yeterli veri yok. Siparişler hazırlandıkça süre verileri oluşacak.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-lg font-bold text-gray-900 mb-4">⏱️ Hazırlık Süresi</h3>

      {/* Big number */}
      <div className="text-center mb-4">
        <div className="text-4xl font-extrabold text-gray-900">{data.avg_prep_display}</div>
        <div className="text-sm text-gray-500 mt-1">ortalama hazırlık süresi</div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div className="bg-green-50 rounded-lg p-3 text-center">
          <div className="text-lg font-bold text-green-700">{data.fastest_display}</div>
          <div className="text-xs text-green-600">En Hızlı</div>
        </div>
        <div className="bg-blue-50 rounded-lg p-3 text-center">
          <div className="text-lg font-bold text-blue-700">{data.avg_prep_display}</div>
          <div className="text-xs text-blue-600">Ortalama</div>
        </div>
        <div className="bg-orange-50 rounded-lg p-3 text-center">
          <div className="text-lg font-bold text-orange-700">{data.slowest_display}</div>
          <div className="text-xs text-orange-600">En Yavaş</div>
        </div>
      </div>

      {/* Recent orders */}
      {data.recent_orders.length > 0 && (
        <div>
          <h4 className="text-sm font-semibold text-gray-500 mb-2">Son Siparişler</h4>
          <div className="space-y-1">
            {data.recent_orders.slice(0, 5).map((order) => {
              const avgSecs = data.avg_prep_seconds || 0;
              const isFast = order.prep_seconds <= avgSecs;
              return (
                <div key={order.order_id} className="flex items-center justify-between py-1.5 px-2 rounded hover:bg-gray-50">
                  <span className="text-sm text-gray-600">#{order.order_id}</span>
                  <span className={`text-sm font-semibold ${isFast ? "text-green-600" : "text-orange-600"}`}>
                    {order.prep_display} {isFast ? "⚡" : "🐌"}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="mt-3 text-center text-xs text-gray-400">
        Toplam {data.total_tracked} sipariş takip edildi
      </div>
    </div>
  );
}
