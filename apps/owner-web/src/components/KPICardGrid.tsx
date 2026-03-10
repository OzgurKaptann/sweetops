"use client";

import { useState, useEffect } from "react";
import { fetchKPIs, DashboardKPIs } from "@/lib/api";
import { Card } from "@sweetops/ui";

export function KPICardGrid() {
  const [data, setData] = useState<DashboardKPIs | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetchKPIs().then(setData).catch(() => setError(true));
  }, []);

  if (error) return <div className="p-4 text-red-500 bg-red-50 rounded-lg">Failed to load KPIs.</div>;
  if (!data) return <div className="animate-pulse flex space-x-4"><div className="h-32 bg-gray-200 rounded w-full"></div><div className="h-32 bg-gray-200 rounded w-full"></div><div className="h-32 bg-gray-200 rounded w-full"></div></div>;

  const { kpis, currency } = data;
  
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6 mb-8">
      <Card className="p-6">
        <h3 className="text-sm font-medium text-gray-500 mb-1">Gross Revenue</h3>
        <div className="text-3xl font-bold text-gray-900">
          ${kpis.gross_revenue.toFixed(2)}
        </div>
        <div className="text-xs text-gray-400 mt-2">Delivered orders only</div>
      </Card>

      <Card className="p-6">
        <h3 className="text-sm font-medium text-gray-500 mb-1">Active Orders</h3>
        <div className="text-3xl font-bold text-gray-900">
          {kpis.active_orders_count}
        </div>
        <div className="text-xs text-gray-400 mt-2">Currently in kitchen</div>
      </Card>

      <Card className="p-6">
        <h3 className="text-sm font-medium text-gray-500 mb-1">AOV</h3>
        <div className="text-3xl font-bold text-gray-900">
          ${kpis.average_order_value.toFixed(2)}
        </div>
        <div className="text-xs text-gray-400 mt-2">Average order value</div>
      </Card>

      <Card className="p-6">
        <h3 className="text-sm font-medium text-gray-500 mb-1">Total Delivered</h3>
        <div className="text-3xl font-bold text-gray-900">
          {kpis.delivered_orders_count}
        </div>
        <div className="text-xs text-gray-400 mt-2">Lifetime</div>
      </Card>
    </div>
  );
}
