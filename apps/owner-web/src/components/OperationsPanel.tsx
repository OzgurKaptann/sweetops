"use client";

import { useState, useEffect } from "react";
import { fetchPrepTime, fetchKitchenOrders, KitchenDashboardResponse } from "@/lib/api";

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

function StatCell({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: "ok" | "warn" | "crit" | "neutral";
}) {
  const valueColor =
    accent === "crit"
      ? "text-red-600"
      : accent === "warn"
      ? "text-amber-600"
      : accent === "ok"
      ? "text-emerald-700"
      : "text-gray-900";

  return (
    <div className="flex flex-col">
      <span className="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">{label}</span>
      <span className={`text-base font-bold leading-tight ${valueColor}`}>{value}</span>
    </div>
  );
}

function computeP90(
  recent: PrepData["recent_orders"],
): number | null {
  if (recent.length < 3) return null;
  const sorted = [...recent].sort((a, b) => a.prep_seconds - b.prep_seconds);
  const idx = Math.floor(sorted.length * 0.9);
  return sorted[Math.min(idx, sorted.length - 1)].prep_seconds;
}

function formatSeconds(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem > 0 ? `${m}m ${rem}s` : `${m}m`;
}

const LOAD_STYLE = {
  low: { bg: "bg-emerald-50", text: "text-emerald-700", label: "Low Load" },
  medium: { bg: "bg-amber-50", text: "text-amber-700", label: "Medium Load" },
  high: { bg: "bg-red-50", text: "text-red-700", label: "High Load" },
};

interface Props {
  refreshTick?: number;
}

export function OperationsPanel({ refreshTick }: Props) {
  const [prep, setPrep] = useState<PrepData | null>(null);
  const [kitchen, setKitchen] = useState<KitchenDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetchPrepTime().catch(() => null),
      fetchKitchenOrders().catch(() => null),
    ])
      .then(([p, k]) => {
        setPrep(p as PrepData | null);
        setKitchen(k);
      })
      .finally(() => setLoading(false));
  }, [refreshTick]);

  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 animate-pulse">
        <div className="h-3 w-24 bg-gray-200 rounded mb-4" />
        <div className="grid grid-cols-4 gap-4">
          {[...Array(4)].map((_, i) => (
            <div key={i} className="h-12 bg-gray-100 rounded" />
          ))}
        </div>
      </div>
    );
  }

  const load = kitchen?.kitchen_load;
  const loadStyle = LOAD_STYLE[load?.load_level ?? "low"];

  // SLA breach count: count orders with sla_severity = "critical" or "warning"
  const slaBreachCount =
    kitchen?.orders.filter((o) => o.sla_severity === "critical").length ?? 0;
  const slaWarnCount =
    kitchen?.orders.filter((o) => o.sla_severity === "warning").length ?? 0;

  const activeOrders = load?.active_orders_count ?? 0;
  const inPrepCount = load?.in_prep_count ?? 0;

  // P90 from recent_orders
  const p90Seconds =
    prep?.recent_orders && prep.recent_orders.length > 0
      ? computeP90(prep.recent_orders)
      : null;

  const avgDisplay = prep?.avg_prep_display ?? "—";
  const p90Display = p90Seconds !== null ? formatSeconds(p90Seconds) : "—";

  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5">
      {/* Header row */}
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Operations
        </h3>
        {load && (
          <span
            className={`text-xs font-semibold px-2.5 py-1 rounded-full ${loadStyle.bg} ${loadStyle.text}`}
          >
            {loadStyle.label}
          </span>
        )}
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-4">
        <StatCell
          label="Active Orders"
          value={String(activeOrders)}
          accent={activeOrders > 6 ? "warn" : "neutral"}
        />
        <StatCell
          label="In Prep"
          value={String(inPrepCount)}
          accent="neutral"
        />
        <StatCell
          label="Avg Prep Time"
          value={avgDisplay}
          accent="neutral"
        />
        <StatCell
          label="P90 Prep Time"
          value={p90Display}
          accent={
            p90Seconds !== null && p90Seconds > 600
              ? "crit"
              : p90Seconds !== null && p90Seconds > 420
              ? "warn"
              : "neutral"
          }
        />
        <StatCell
          label="SLA Breached"
          value={String(slaBreachCount)}
          accent={slaBreachCount > 0 ? "crit" : "ok"}
        />
        <StatCell
          label="SLA Warning"
          value={String(slaWarnCount)}
          accent={slaWarnCount > 0 ? "warn" : "ok"}
        />
        <StatCell
          label="Avg Queue Age"
          value={load ? `${load.average_age_minutes.toFixed(1)} min` : "—"}
          accent={
            load && load.average_age_minutes > 8 ? "crit" :
            load && load.average_age_minutes > 5 ? "warn" : "ok"
          }
        />
      </div>

      {/* Load explanation */}
      {load?.explanation && (
        <p className="text-xs text-gray-400 mt-3 pt-3 border-t border-gray-50">
          {load.explanation}
        </p>
      )}
    </div>
  );
}
