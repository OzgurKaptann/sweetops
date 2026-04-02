"use client";

import { useEffect, useState } from "react";
import { fetchCriticalAlerts } from "@/lib/api";

interface Alert {
  ingredient_id: number;
  ingredient_name: string;
  severity: string;
  message: string;
  stock_quantity: number;
  unit: string;
  days_remaining: number;
  avg_daily_revenue: number;
  estimated_lost_revenue_daily: number;
}

interface AlertsData {
  alerts: Alert[];
  total_daily_risk: number;
  total_alerts: number;
}

export function CriticalAlertsPanel() {
  const [data, setData] = useState<AlertsData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchCriticalAlerts()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">🚨 Kritik Uyarılar</h3>
        <div className="animate-pulse space-y-3">
          {[...Array(2)].map((_, i) => <div key={i} className="h-16 bg-red-50 rounded" />)}
        </div>
      </div>
    );
  }

  if (!data || data.alerts.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-2">🚨 Kritik Uyarılar</h3>
        <div className="text-center py-6">
          <div className="text-3xl mb-2">✅</div>
          <p className="text-green-600 font-semibold">Kritik uyarı yok</p>
          <p className="text-gray-400 text-sm mt-1">Tüm stoklar güvenli seviyede</p>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow p-6 border-l-4 border-red-500">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-bold text-gray-900">🚨 Kritik Uyarılar</h3>
        <div className="bg-red-100 text-red-800 px-3 py-1 rounded-full text-sm font-bold">
          Günlük risk: ₺{data.total_daily_risk.toFixed(0)}
        </div>
      </div>

      <div className="space-y-3">
        {data.alerts.map((alert) => (
          <div
            key={alert.ingredient_id}
            className={`p-4 rounded-lg ${
              alert.severity === "critical" ? "bg-red-50 border border-red-200" : "bg-amber-50 border border-amber-200"
            }`}
          >
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-sm">{alert.severity === "critical" ? "🔴" : "⚠️"}</span>
                  <span className="font-bold text-gray-900">{alert.ingredient_name}</span>
                </div>
                <p className={`text-sm mt-1 ${alert.severity === "critical" ? "text-red-600" : "text-amber-600"}`}>
                  {alert.message}
                </p>
                <p className="text-xs text-gray-500 mt-1">
                  Kalan: {alert.stock_quantity.toFixed(0)} {alert.unit}
                </p>
              </div>
              <div className="text-right">
                <div className="text-red-700 font-bold text-lg">
                  ₺{alert.estimated_lost_revenue_daily.toFixed(0)}
                </div>
                <div className="text-xs text-red-500">günlük kayıp riski</div>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-4 p-3 bg-red-50 rounded-lg text-center">
        <p className="text-sm text-red-700 font-medium">
          ⚠️ Bu stoklar tükenirse günde <span className="font-bold">₺{data.total_daily_risk.toFixed(0)}</span> gelir kaybedebilirsiniz
        </p>
      </div>
    </div>
  );
}
