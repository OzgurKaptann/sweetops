"use client";

import { useState, useEffect, useRef, useCallback } from "react";

// KPI & performance
import { KPICardGrid } from "@/components/KPICardGrid";
import { OperationsPanel } from "@/components/OperationsPanel";
import { MainAnalyticsChart } from "@/components/MainAnalyticsChart";

// Decisions + focus
import { FocusBanner } from "@/components/FocusBanner";
import { DecisionPanel } from "@/components/DecisionPanel";
import { StockWarningsPanel } from "@/components/StockWarningsPanel";
import { OwnerDecision } from "@/lib/api";

// Measurement + attention
import { MetricsPanel } from "@/components/MetricsPanel";
import { MetricAttentionBanner } from "@/components/MetricAttentionBanner";

// Analytics
import { HourlyDemandChart } from "@/components/HourlyDemandChart";
import { IngredientForecastPanel } from "@/components/IngredientForecastPanel";
import { TopIngredientsPanel } from "@/components/TopIngredientsPanel";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws/kitchen";
const POLL_INTERVAL_MS = 30_000;

// ── Section header ────────────────────────────────────────────────────────────

function SectionHeader({
  title,
  subtitle,
  accent,
}: {
  title: string;
  subtitle?: string;
  accent?: "neutral" | "alert" | "analytics";
}) {
  const bar =
    accent === "alert" ? "bg-red-500" : accent === "analytics" ? "bg-blue-500" : "bg-amber-500";
  return (
    <div className="flex items-baseline gap-3 mb-4">
      <div className={`w-1 h-5 rounded-full ${bar} shrink-0`} />
      <div>
        <h2 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">{title}</h2>
        {subtitle && <p className="text-xs text-gray-400 mt-0.5">{subtitle}</p>}
      </div>
    </div>
  );
}

function LiveDot({ connected }: { connected: boolean }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={`w-2 h-2 rounded-full ${connected ? "bg-emerald-500 animate-pulse" : "bg-gray-300"}`} />
      <span className="text-xs text-gray-500">{connected ? "Live" : "Offline"}</span>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function OwnerDashboard() {
  const [refreshTick, setRefreshTick] = useState(0);
  const [wsConnected, setWsConnected] = useState(false);
  const [primaryDecision, setPrimaryDecision] = useState<OwnerDecision | null>(null);
  const [bannerDismissed, setBannerDismissed] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const refresh = () => setRefreshTick((t) => t + 1);

  // Reset banner dismiss when the primary decision changes
  const handlePrimaryDecision = useCallback((d: OwnerDecision | null) => {
    setPrimaryDecision((prev) => {
      if (prev?.decision_id !== d?.decision_id) setBannerDismissed(false);
      return d;
    });
  }, []);

  useEffect(() => {
    const timer = setInterval(refresh, POLL_INTERVAL_MS);
    const connect = () => {
      const ws = new WebSocket(WS_URL);
      ws.onopen = () => setWsConnected(true);
      ws.onmessage = (e) => {
        try {
          const p = JSON.parse(e.data);
          if (p.event === "order_created" || p.event === "order_status_updated") refresh();
        } catch {}
      };
      ws.onclose = () => { setWsConnected(false); setTimeout(connect, 5000); };
      wsRef.current = ws;
    };
    connect();
    return () => { clearInterval(timer); wsRef.current?.close(); };
  }, []);

  const now = new Date().toLocaleDateString("tr-TR", { day: "numeric", month: "long", year: "numeric" });

  return (
    <div className="min-h-screen bg-[#f8f9fa]">

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-20">
        <div className="max-w-screen-xl mx-auto px-6">
          <div className="flex items-center justify-between h-14">
            <div className="flex items-center gap-3">
              <span className="text-base font-bold text-gray-900 tracking-tight">SweetOps</span>
              <span className="text-gray-300 text-sm">|</span>
              <span className="text-sm text-gray-500 font-medium">Owner Dashboard</span>
            </div>
            <div className="flex items-center gap-4">
              <span className="text-xs text-gray-400 hidden sm:block">{now}</span>
              <a href="/kitchen" className="text-xs px-3 py-1.5 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 transition-colors font-medium">
                Kitchen →
              </a>
              <LiveDot connected={wsConnected} />
              <button onClick={refresh} className="text-xs px-3 py-1.5 rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors font-medium">
                ↻ Refresh
              </button>
            </div>
          </div>
        </div>

        {/* Focus banner — highest-priority realtime decision */}
        {!bannerDismissed && (
          <FocusBanner
            decision={primaryDecision}
            onDismiss={() => setBannerDismissed(true)}
          />
        )}
        {/* Metric attention banner — metric-driven mode (below focus banner) */}
        <MetricAttentionBanner refreshTick={refreshTick} />
      </header>

      {/* ── Main content ───────────────────────────────────────────────────── */}
      <main className="max-w-screen-xl mx-auto px-6 py-6 space-y-10">

        {/* ━━━ ZONE 1 · TODAY'S PERFORMANCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */}
        <section>
          <SectionHeader title="Today's Performance" subtitle="Key metrics vs previous period" accent="neutral" />
          <KPICardGrid key={`kpi-${refreshTick}`} refreshTick={refreshTick} />
          <div className="mt-4">
            <OperationsPanel key={`ops-${refreshTick}`} refreshTick={refreshTick} />
          </div>
          <div className="mt-4">
            <MainAnalyticsChart key={`chart-${refreshTick}`} refreshTick={refreshTick} />
          </div>
        </section>

        {/* ━━━ ZONE 2 · ALERTS & DECISIONS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */}
        <section>
          <SectionHeader title="Alerts & Decisions" subtitle="Sorted by urgency · act on the primary focus first" accent="alert" />
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="lg:col-span-2">
              <DecisionPanel
                key={`decisions-${refreshTick}`}
                refreshTick={refreshTick}
                onPrimaryDecision={handlePrimaryDecision}
              />
            </div>
            {/* Stock sidebar — shown only when there are non-ok items */}
            <div>
              <StockWarningsPanel key={`stock-${refreshTick}`} />
            </div>
          </div>
        </section>

        {/* ━━━ ZONE 3 · MEASUREMENT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */}
        <section id="metrics-section">
          <SectionHeader title="Measurement" subtitle="Is the system actually working? · day-over-day trends" accent="neutral" />
          <MetricsPanel key={`metrics-${refreshTick}`} refreshTick={refreshTick} />
        </section>

        {/* ━━━ ZONE 4 · ANALYTICS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ */}
        <section>
          <SectionHeader title="Analytics" subtitle="Demand patterns · forecast · ingredient breakdown" accent="analytics" />
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <HourlyDemandChart key={`hourly-${refreshTick}`} refreshTick={refreshTick} />
            <IngredientForecastPanel key={`forecast-${refreshTick}`} refreshTick={refreshTick} />
          </div>
          <div className="mt-4">
            <TopIngredientsPanel key={`top-${refreshTick}`} refreshTick={refreshTick} />
          </div>
        </section>

      </main>
    </div>
  );
}
