"use client";

import { useState, useEffect } from "react";
import { fetchIngredientForecast, IngredientForecastData } from "@/lib/api";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
  Cell,
} from "recharts";

interface Props {
  refreshTick?: number;
}

const TREND_COLOR = {
  up: "text-emerald-600",
  down: "text-red-500",
  stable: "text-gray-400",
};

const TREND_ARROW = { up: "▲", down: "▼", stable: "—" };

const CONFIDENCE_DOT = {
  high: "bg-emerald-500",
  medium: "bg-yellow-400",
  low: "bg-gray-400",
};

// Group forecast items by ingredient, take the nearest forecast_date for each
function groupByIngredient(items: IngredientForecastData["items"]) {
  const map = new Map<string, IngredientForecastData["items"][0]>();
  for (const item of items) {
    if (!map.has(item.ingredient_name)) {
      map.set(item.ingredient_name, item);
    } else {
      // keep the closest date
      const existing = map.get(item.ingredient_name)!;
      if (item.forecast_date < existing.forecast_date) {
        map.set(item.ingredient_name, item);
      }
    }
  }
  return Array.from(map.values());
}

export function IngredientForecastPanel({ refreshTick }: Props) {
  const [data, setData] = useState<IngredientForecastData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    setError(false);
    fetchIngredientForecast().then(setData).catch(() => setError(true));
  }, [refreshTick]);

  if (error) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 text-sm text-red-500">
        Forecast data unavailable.
      </div>
    );
  }

  if (!data) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 animate-pulse h-[280px]">
        <div className="h-3 w-32 bg-gray-200 rounded mb-4" />
        <div className="h-48 bg-gray-100 rounded" />
      </div>
    );
  }

  if (data.items.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 min-h-[280px] flex flex-col">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
          Forecast vs Baseline
        </h3>
        <div className="flex-1 flex items-center justify-center text-gray-400 text-sm text-center">
          Not enough history to forecast yet.
          <br />
          <span className="text-xs mt-1 block">Needs 7–14 days of data.</span>
        </div>
      </div>
    );
  }

  const nearest = groupByIngredient(data.items);

  // Build chart data: predicted vs recent avg
  const chartData = nearest.map((item) => ({
    name: item.ingredient_name,
    predicted: item.predicted_usage,
    baseline: item.recent_avg_usage,
    trend: item.trend_direction,
    confidence: item.confidence_level,
    delta: item.trend_delta,
  }));

  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5 min-h-[280px] flex flex-col">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          Forecast vs Baseline
        </h3>
        <span className="text-[10px] text-gray-400">
          {data.forecast_horizon_days}-day horizon
        </span>
      </div>

      {/* Chart */}
      <div className="flex-1 min-h-[200px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            margin={{ top: 4, right: 4, bottom: 0, left: -8 }}
            barSize={10}
            barCategoryGap="30%"
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" vertical={false} />
            <XAxis
              dataKey="name"
              fontSize={10}
              axisLine={false}
              tickLine={false}
              tickMargin={6}
            />
            <YAxis fontSize={10} axisLine={false} tickLine={false} />
            <Tooltip
              contentStyle={{ borderRadius: 8, border: "1px solid #e5e7eb", fontSize: 11 }}
              formatter={(value: number, name: string) => [
                value.toFixed(1),
                name === "predicted" ? "Forecast" : "Baseline",
              ]}
            />
            <Legend
              iconType="square"
              iconSize={8}
              wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
              formatter={(v) => (v === "predicted" ? "Forecast" : "Baseline")}
            />
            <Bar dataKey="baseline" fill="#e5e7eb" radius={[2, 2, 0, 0]} />
            <Bar dataKey="predicted" radius={[2, 2, 0, 0]}>
              {chartData.map((entry, i) => (
                <Cell
                  key={i}
                  fill={
                    entry.trend === "up"
                      ? "#f59e0b"
                      : entry.trend === "down"
                      ? "#f87171"
                      : "#3b82f6"
                  }
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Confidence legend */}
      <div className="flex items-center gap-3 mt-3 flex-wrap">
        {nearest.map((item) => (
          <div key={item.ingredient_name} className="flex items-center gap-1">
            <span
              className={`w-1.5 h-1.5 rounded-full shrink-0 ${CONFIDENCE_DOT[item.confidence_level]}`}
            />
            <span className="text-[10px] text-gray-400">{item.ingredient_name}</span>
            <span
              className={`text-[10px] font-semibold ${TREND_COLOR[item.trend_direction]}`}
            >
              {TREND_ARROW[item.trend_direction]}
            </span>
          </div>
        ))}
        <span className="text-[10px] text-gray-300 ml-auto">
          ● high&nbsp; ● med&nbsp; ● low confidence
        </span>
      </div>
    </div>
  );
}
