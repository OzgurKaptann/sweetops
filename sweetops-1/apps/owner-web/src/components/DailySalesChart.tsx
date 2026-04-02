"use client";

import { useState, useEffect } from "react";
import { fetchDailySales, DailySalesData } from "@/lib/api";
import { Card } from "@sweetops/ui";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

interface Props {
  refreshTick?: number;
}

export function DailySalesChart({ refreshTick }: Props) {
  const [data, setData] = useState<DailySalesData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    setError(false);
    fetchDailySales().then(setData).catch(() => setError(true));
  }, [refreshTick]);

  if (error) return <Card className="p-6 text-red-500">Günlük satış verisi yüklenemedi.</Card>;
  if (!data) return <Card className="p-6 animate-pulse h-64 bg-gray-100" />;

  if (data.points.length === 0) {
    return (
      <Card className="p-6 min-h-[300px] flex flex-col">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Günlük Satışlar</h3>
        <div className="flex-1 flex items-center justify-center text-gray-500">
          Henüz günlük veri yok.
        </div>
      </Card>
    );
  }

  const formatted = data.points.map((p) => ({
    ...p,
    label: new Date(p.sales_date).toLocaleDateString("tr-TR", { day: "numeric", month: "short" }),
  }));

  return (
    <Card className="p-6 min-h-[300px] flex flex-col">
      <div className="flex justify-between items-center mb-4">
        <h3 className="text-lg font-semibold text-gray-900">Günlük Satışlar</h3>
        <span className="text-xs text-gray-400">{data.currency}</span>
      </div>
      <div className="flex-1 w-full min-h-[250px]">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={formatted}>
            <defs>
              <linearGradient id="revenueGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#f59e0b" stopOpacity={0.3} />
                <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" />
            <XAxis dataKey="label" fontSize={12} axisLine={false} tickLine={false} tickMargin={8} />
            <YAxis fontSize={12} axisLine={false} tickLine={false} tickFormatter={(v) => `₺${v}`} />
            <Tooltip
              formatter={(value: number) => [`₺${value.toFixed(0)}`, "Gelir"]}
              labelStyle={{ fontWeight: 600 }}
              contentStyle={{ borderRadius: 8, border: "1px solid #e5e7eb" }}
            />
            <Area
              type="monotone"
              dataKey="gross_revenue"
              stroke="#f59e0b"
              strokeWidth={2}
              fill="url(#revenueGrad)"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
