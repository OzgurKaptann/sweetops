"use client";

import { useState, useEffect, useCallback } from "react";
import {
  fetchDailySales,
  fetchHourlyDemand,
  fetchTopIngredients,
  DailySalesData,
  HourlyDemandData,
  TopIngredientsData,
} from "@/lib/api";
import {
  AreaChart,
  Area,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  ReferenceLine,
} from "recharts";

// ── Types ─────────────────────────────────────────────────────────────────────

type Metric = "revenue" | "orders" | "aov" | "ingredients";
type TimeRange = "today" | "7d" | "30d";

const METRICS: { key: Metric; label: string }[] = [
  { key: "revenue", label: "Revenue" },
  { key: "orders", label: "Orders" },
  { key: "aov", label: "Avg Order Value" },
  { key: "ingredients", label: "Ingredient Usage" },
];

const RANGES: { key: TimeRange; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "7d", label: "7 Days" },
  { key: "30d", label: "30 Days" },
];

// ── Helpers ──────────────────────────────────────────────────────────────────

const METRIC_CONFIG: Record<
  Metric,
  { color: string; gradient: string; unit: string; formatter: (v: number) => string }
> = {
  revenue: {
    color: "#f59e0b",
    gradient: "goldGrad",
    unit: "₺",
    formatter: (v) => `₺${v.toLocaleString("tr-TR", { maximumFractionDigits: 0 })}`,
  },
  orders: {
    color: "#3b82f6",
    gradient: "blueGrad",
    unit: "",
    formatter: (v) => String(v),
  },
  aov: {
    color: "#8b5cf6",
    gradient: "purpleGrad",
    unit: "₺",
    formatter: (v) => `₺${v.toFixed(2)}`,
  },
  ingredients: {
    color: "#10b981",
    gradient: "greenGrad",
    unit: "",
    formatter: (v) => String(v),
  },
};

function sliceDays(points: DailySalesData["points"], days: number) {
  return points.slice(-days);
}

function formatDate(dateStr: string) {
  return new Date(dateStr).toLocaleDateString("tr-TR", { day: "numeric", month: "short" });
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function ChartSkeleton() {
  return (
    <div className="animate-pulse">
      <div className="flex gap-3 mb-5">
        {[...Array(4)].map((_, i) => <div key={i} className="h-7 w-24 bg-gray-200 rounded-lg" />)}
        <div className="ml-auto flex gap-2">
          {[...Array(3)].map((_, i) => <div key={i} className="h-7 w-16 bg-gray-100 rounded-lg" />)}
        </div>
      </div>
      <div className="h-64 bg-gray-100 rounded-lg" />
    </div>
  );
}

// ── Ingredient bar chart (snapshot) ──────────────────────────────────────────

function IngredientUsageChart({ data }: { data: TopIngredientsData }) {
  const cfg = METRIC_CONFIG.ingredients;
  const formatted = data.items.map((i) => ({
    name: i.ingredient_name,
    uses: i.usage_count,
    share: (i.usage_share * 100).toFixed(1),
  }));

  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={formatted} layout="vertical" margin={{ left: 8, right: 24, top: 4, bottom: 4 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" horizontal={false} />
          <XAxis type="number" fontSize={11} axisLine={false} tickLine={false} tickFormatter={(v) => `${v}`} />
          <YAxis
            type="category"
            dataKey="name"
            fontSize={12}
            axisLine={false}
            tickLine={false}
            width={100}
          />
          <Tooltip
            formatter={(value: number, _: string, entry: any) => [
              `${value} uses (${entry.payload.share}%)`,
              "Kullanım",
            ]}
            contentStyle={{ borderRadius: 8, border: "1px solid #e5e7eb", fontSize: 12 }}
          />
          <Bar dataKey="uses" fill={cfg.color} radius={[0, 4, 4, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Main Chart ────────────────────────────────────────────────────────────────

interface Props {
  refreshTick?: number;
}

export function MainAnalyticsChart({ refreshTick }: Props) {
  const [metric, setMetric] = useState<Metric>("revenue");
  const [range, setRange] = useState<TimeRange>("7d");
  const [salesData, setSalesData] = useState<DailySalesData | null>(null);
  const [hourlyData, setHourlyData] = useState<HourlyDemandData | null>(null);
  const [ingredientData, setIngredientData] = useState<TopIngredientsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(false);
    Promise.all([fetchDailySales(), fetchHourlyDemand(), fetchTopIngredients()])
      .then(([s, h, i]) => { setSalesData(s); setHourlyData(h); setIngredientData(i); })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load, refreshTick]);

  // Switch to today when metric doesn't apply to multi-day
  const cfg = METRIC_CONFIG[metric];

  if (loading) return (
    <div className="bg-white rounded-xl border border-gray-100 p-5">
      <ChartSkeleton />
    </div>
  );

  if (error) return (
    <div className="bg-white rounded-xl border border-gray-100 p-5 text-sm text-red-500">
      Grafik verileri yüklenemedi.
    </div>
  );

  // ── Build chart data ────────────────────────────────────────────────────────

  const isToday = range === "today";
  const isIngredients = metric === "ingredients";

  // Ingredient view is always a snapshot — ignore time range
  const showIngredientSnapshot = isIngredients;

  // Hourly data for "today" view (orders only — only field available hourly)
  const hourlyPoints = hourlyData?.points ?? [];

  // Daily data sliced by range
  const dailyPoints = salesData
    ? sliceDays(
        salesData.points.map((p) => ({
          ...p,
          label: formatDate(p.sales_date),
        })),
        range === "7d" ? 7 : 30,
      )
    : [];

  const dataKey: Record<Exclude<Metric, "ingredients">, string> = {
    revenue: "gross_revenue",
    orders: "total_orders",
    aov: "average_order_value",
  };

  // Chart source: today uses hourly order count; daily uses sales
  const chartData = isToday
    ? hourlyPoints.map((p) => ({ label: p.hour_bucket, value: p.order_count }))
    : dailyPoints.map((p) => ({
        label: p.label,
        value: p[dataKey[metric as Exclude<Metric, "ingredients">] as keyof typeof p] as number,
      }));

  const effectiveMetricLabel = isToday ? "Orders (hourly)" : METRICS.find((m) => m.key === metric)!.label;

  // Average reference line
  const avg = chartData.length > 0
    ? chartData.reduce((s, d) => s + d.value, 0) / chartData.length
    : null;

  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5">
      {/* Controls */}
      <div className="flex flex-wrap items-center gap-3 mb-5">
        {/* Metric tabs */}
        <div className="flex gap-1 bg-gray-100 rounded-lg p-0.5">
          {METRICS.map((m) => (
            <button
              key={m.key}
              onClick={() => setMetric(m.key)}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition-all ${
                metric === m.key
                  ? "bg-white text-gray-900 shadow-sm"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              {m.label}
            </button>
          ))}
        </div>

        {/* Time range (hidden for ingredient snapshot) */}
        {!showIngredientSnapshot && (
          <div className="flex gap-1 ml-auto">
            {RANGES.map((r) => (
              <button
                key={r.key}
                onClick={() => setRange(r.key)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-all ${
                  range === r.key
                    ? "bg-gray-900 text-white"
                    : "text-gray-500 hover:bg-gray-100"
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Chart label */}
      <div className="flex items-baseline gap-2 mb-3">
        <span className="text-sm font-semibold text-gray-700">{effectiveMetricLabel}</span>
        {isToday && metric !== "orders" && (
          <span className="text-xs text-gray-400 italic">— today view shows order count (hourly)</span>
        )}
        {showIngredientSnapshot && (
          <span className="text-xs text-gray-400 italic">— point-in-time snapshot</span>
        )}
      </div>

      {/* Ingredient snapshot */}
      {showIngredientSnapshot && ingredientData && (
        <IngredientUsageChart data={ingredientData} />
      )}

      {/* Time-series chart */}
      {!showIngredientSnapshot && (
        <>
          {chartData.length === 0 ? (
            <div className="h-64 flex items-center justify-center text-gray-400 text-sm">
              Yeterli veri yok.
            </div>
          ) : (
            <div className="h-64">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                  <defs>
                    <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor={cfg.color} stopOpacity={0.2} />
                      <stop offset="95%" stopColor={cfg.color} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" vertical={false} />
                  <XAxis
                    dataKey="label"
                    fontSize={11}
                    axisLine={false}
                    tickLine={false}
                    tickMargin={8}
                  />
                  <YAxis
                    fontSize={11}
                    axisLine={false}
                    tickLine={false}
                    tickFormatter={(v) => (cfg.unit ? `${cfg.unit}${v}` : String(v))}
                    width={52}
                  />
                  <Tooltip
                    formatter={(value: number) => [cfg.formatter(value), effectiveMetricLabel]}
                    labelStyle={{ fontWeight: 600, fontSize: 12 }}
                    contentStyle={{ borderRadius: 8, border: "1px solid #e5e7eb", fontSize: 12 }}
                  />
                  {avg !== null && (
                    <ReferenceLine
                      y={avg}
                      stroke={cfg.color}
                      strokeDasharray="4 4"
                      strokeOpacity={0.5}
                      label={{
                        value: `Avg: ${cfg.formatter(avg)}`,
                        position: "insideTopRight",
                        fontSize: 10,
                        fill: cfg.color,
                        opacity: 0.7,
                      }}
                    />
                  )}
                  <Area
                    type="monotone"
                    dataKey="value"
                    stroke={cfg.color}
                    strokeWidth={2}
                    fill="url(#chartGrad)"
                    dot={false}
                    activeDot={{ r: 4, strokeWidth: 0 }}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}
        </>
      )}
    </div>
  );
}
