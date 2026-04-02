"use client";

import { useState, useEffect } from "react";
import { fetchHourlyDemand, HourlyDemandData } from "@/lib/api";
import { Card } from "@sweetops/ui";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';

export function HourlyDemandChart() {
  const [data, setData] = useState<HourlyDemandData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetchHourlyDemand().then(setData).catch(() => setError(true));
  }, []);

  if (error) return <Card className="p-6 text-red-500">Failed to load chart.</Card>;
  if (!data) return <Card className="p-6 animate-pulse h-64 bg-gray-100"></Card>;

  if (data.points.length === 0) {
    return (
      <Card className="p-6 min-h-[300px] flex flex-col">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Hourly Demand</h3>
        <div className="flex-1 flex items-center justify-center text-gray-500">
          No hourly data available yet.
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-6 min-h-[300px] flex flex-col">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">Hourly Demand</h3>
      <div className="flex-1 w-full h-full min-h-[250px]">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data.points}>
            <XAxis dataKey="hour_bucket" fontSize={12} tickMargin={10} axisLine={false} tickLine={false} />
            <YAxis fontSize={12} axisLine={false} tickLine={false} />
            <Tooltip cursor={{fill: '#f3f4f6'}} />
            <Bar dataKey="order_count" fill="#3b82f6" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
