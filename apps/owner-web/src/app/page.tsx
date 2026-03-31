"use client";

import { useState, useEffect, useRef } from "react";
import { KPICardGrid } from "@/components/KPICardGrid";
import { TopIngredientsPanel } from "@/components/TopIngredientsPanel";
import { HourlyDemandChart } from "@/components/HourlyDemandChart";
import { IngredientForecastPanel } from "@/components/IngredientForecastPanel";
import { StockWarningsPanel } from "@/components/StockWarningsPanel";
import { CriticalAlertsPanel } from "@/components/CriticalAlertsPanel";
import { PrepTimePanel } from "@/components/PrepTimePanel";
import { TrendingIngredientsPanel } from "@/components/TrendingIngredientsPanel";
import { PopularCombosPanel } from "@/components/PopularCombosPanel";
import { ValueSummaryPanel } from "@/components/ValueSummaryPanel";
import { DailySalesChart } from "@/components/DailySalesChart";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws/kitchen";
const POLL_INTERVAL_MS = 30_000;

export default function OwnerDashboard() {
  const [refreshTick, setRefreshTick] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);

  const refresh = () => setRefreshTick((t) => t + 1);

  useEffect(() => {
    // 30-second auto-poll
    const timer = setInterval(refresh, POLL_INTERVAL_MS);

    // WebSocket — refresh immediately on new orders
    const connect = () => {
      const ws = new WebSocket(WS_URL);
      ws.onmessage = (e) => {
        try {
          const payload = JSON.parse(e.data);
          if (payload.event === "order_created" || payload.event === "order_status_updated") {
            refresh();
          }
        } catch {}
      };
      ws.onclose = () => setTimeout(connect, 5000);
      wsRef.current = ws;
    };
    connect();

    return () => {
      clearInterval(timer);
      wsRef.current?.close();
    };
  }, []);

  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16 items-center">
            <h1 className="text-xl font-bold text-gray-900">🧇 SweetOps <span className="text-amber-600">Panel</span></h1>
            <div className="flex items-center gap-3">
              <button
                onClick={refresh}
                className="text-xs px-3 py-1.5 rounded-lg bg-amber-50 text-amber-700 hover:bg-amber-100 transition-colors font-medium"
              >
                ↻ Yenile
              </button>
              <div className="text-sm text-gray-500">İşletme Paneli • Canlı</div>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

        <ValueSummaryPanel key={`value-${refreshTick}`} />

        <div className="mt-8 mb-6 flex items-center justify-between">
          <h2 className="text-2xl font-bold text-gray-900">İşletme Özeti</h2>
        </div>

        <KPICardGrid key={`kpi-${refreshTick}`} />

        <div className="mt-6">
          <CriticalAlertsPanel key={`alerts-${refreshTick}`} />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
          <PrepTimePanel key={`prep-${refreshTick}`} />
          <StockWarningsPanel key={`stock-${refreshTick}`} />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
          <TrendingIngredientsPanel key={`trending-${refreshTick}`} />
          <PopularCombosPanel key={`combos-${refreshTick}`} />
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mt-6">
          <div className="lg:col-span-2">
            <DailySalesChart key={`daily-${refreshTick}`} refreshTick={refreshTick} />
            <div className="mt-6">
              <HourlyDemandChart key={`hourly-${refreshTick}`} />
            </div>
            <div className="mt-6">
              <IngredientForecastPanel key={`forecast-${refreshTick}`} />
            </div>
          </div>
          <div>
            <TopIngredientsPanel key={`top-${refreshTick}`} />
          </div>
        </div>

      </div>
    </main>
  );
}
