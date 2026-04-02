"use client";

import { useState, useEffect } from "react";
import { fetchHourlyDemand, HourlyDemandData } from "@/lib/api";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Cell,
} from "recharts";

interface Props {
  refreshTick?: number;
}

export function HourlyDemandChart({ refreshTick }: Props) {
  const [data, setData] = useState<HourlyDemandData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    setError(false);
    fetchHourlyDemand().then(setData).catch(() => setError(true));
  }, [refreshTick]);

  if (error) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 text-sm text-red-500">
        Failed to load hourly demand.
      </div>
    );
  }

  if (!data) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 animate-pulse h-64" />
    );
  }

  if (data.points.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 min-h-[280px] flex flex-col">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
          Hourly Demand
        </h3>
        <div className="flex-1 flex items-center justify-center text-gray-400 text-sm">
          No hourly data yet.
        </div>
      </div>
    );
  }

  const maxOrders = Math.max(...data.points.map((p) => p.order_count), 1);

  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5 min-h-[280px] flex flex-col">
      <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-4">
        Hourly Demand
      </h3>
      <div className="flex-1 w-full min-h-[220px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data.points} margin={{ top: 4, right: 4, bottom: 0, left: -8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" vertical={false} />
            <XAxis
              dataKey="hour_bucket"
              fontSize={10}
              tickMargin={8}
              axisLine={false}
              tickLine={false}
              interval="preserveStartEnd"
            />
            <YAxis fontSize={11} axisLine={false} tickLine={false} />
            <Tooltip
              formatter={(value: number) => [value, "Orders"]}
              contentStyle={{ borderRadius: 8, border: "1px solid #e5e7eb", fontSize: 12 }}
              cursor={{ fill: "#f3f4f6" }}
            />
            <Bar dataKey="order_count" radius={[3, 3, 0, 0]}>
              {data.points.map((p, i) => (
                <Cell
                  key={i}
                  fill={p.order_count >= maxOrders * 0.8 ? "#f59e0b" : "#3b82f6"}
                  fillOpacity={0.85}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <p className="text-[10px] text-gray-400 mt-2 text-right">
        Peak hours highlighted in amber
      </p>
    </div>
  );
}
