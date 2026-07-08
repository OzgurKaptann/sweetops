"use client";

import { useState, useEffect } from "react";
import { fetchKPIs, fetchDailySales, DashboardKPIs, DailySalesData } from "@/lib/api";

interface KPICardProps {
  label: string;
  value: string;
  subtext: string;
  delta: number | null; // percentage delta vs previous period
  loading?: boolean;
}

function DeltaBadge({ delta }: { delta: number | null }) {
  if (delta === null) return <span className="text-xs text-gray-400">—</span>;
  const positive = delta >= 0;
  return (
    <span
      className={`inline-flex items-center gap-0.5 text-xs font-semibold px-1.5 py-0.5 rounded ${
        positive
          ? "bg-emerald-50 text-emerald-700"
          : "bg-red-50 text-red-600"
      }`}
    >
      {positive ? "▲" : "▼"} {Math.abs(delta).toFixed(1)}%
    </span>
  );
}

function KPICard({ label, value, subtext, delta, loading }: KPICardProps) {
  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 animate-pulse">
        <div className="h-3 bg-gray-200 rounded w-24 mb-3" />
        <div className="h-8 bg-gray-200 rounded w-32 mb-2" />
        <div className="h-3 bg-gray-100 rounded w-20" />
      </div>
    );
  }
  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5 hover:shadow-sm transition-shadow">
      <div className="flex items-start justify-between mb-1">
        <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">{label}</span>
        <DeltaBadge delta={delta} />
      </div>
      <div className="text-2xl font-bold text-gray-900 mt-1">{value}</div>
      <div className="text-xs text-gray-400 mt-1">{subtext}</div>
    </div>
  );
}

interface Props {
  refreshTick?: number;
}

export function KPICardGrid({ refreshTick }: Props) {
  const [kpis, setKpis] = useState<DashboardKPIs | null>(null);
  const [sales, setSales] = useState<DailySalesData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    setError(false);
    Promise.all([fetchKPIs(), fetchDailySales()])
      .then(([k, s]) => { setKpis(k); setSales(s); })
      .catch(() => setError(true));
  }, [refreshTick]);

  if (error) {
    return (
      <div className="p-4 text-sm text-red-600 bg-red-50 rounded-lg border border-red-100">
        KPI verileri yüklenemedi.
      </div>
    );
  }

  const loading = !kpis;

  // Compute deltas from daily-sales: today vs yesterday
  let revenueDelta: number | null = null;
  let ordersDelta: number | null = null;
  let aovDelta: number | null = null;

  if (sales && sales.points.length >= 2) {
    const pts = sales.points;
    const today = pts[pts.length - 1];
    const yesterday = pts[pts.length - 2];

    if (yesterday.gross_revenue > 0) {
      revenueDelta = ((today.gross_revenue - yesterday.gross_revenue) / yesterday.gross_revenue) * 100;
    }
    if (yesterday.total_orders > 0) {
      ordersDelta = ((today.total_orders - yesterday.total_orders) / yesterday.total_orders) * 100;
    }
    if (yesterday.average_order_value > 0) {
      aovDelta = ((today.average_order_value - yesterday.average_order_value) / yesterday.average_order_value) * 100;
    }
  }

  const peakHour = kpis?.kpis.peak_hour ?? null;
  const currency = kpis?.currency ?? "TRY";
  const symbol = currency === "USD" ? "$" : "₺";

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
      <KPICard
        loading={loading}
        label="Gross Revenue"
        value={kpis ? `${symbol}${kpis.kpis.gross_revenue.toLocaleString("tr-TR", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}` : "—"}
        subtext="Teslim edilen siparişler"
        delta={revenueDelta}
      />
      <KPICard
        loading={loading}
        label="Total Orders"
        value={kpis ? String(kpis.kpis.total_orders) : "—"}
        subtext={peakHour ? `Peak: ${peakHour}` : "Bugünkü tüm siparişler"}
        delta={ordersDelta}
      />
      <KPICard
        loading={loading}
        label="Active Orders"
        value={kpis ? String(kpis.kpis.active_orders_count) : "—"}
        subtext="Currently in kitchen"
        delta={null}
      />
      <KPICard
        loading={loading}
        label="Avg Order Value"
        value={kpis ? `${symbol}${kpis.kpis.average_order_value.toFixed(2)}` : "—"}
        subtext="Sipariş başı ortalama"
        delta={aovDelta}
      />
    </div>
  );
}
