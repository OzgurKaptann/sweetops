"use client";

import { useEffect, useState } from "react";
import { fetchValueSummary } from "@/lib/api";

interface ValueItem {
  icon: string;
  label: string;
  value: string;
  detail: string;
  color: string;
}

interface ValueData {
  headline: string;
  items: ValueItem[];
  weekly_revenue: number;
  weekly_risk: number;
  protected_revenue: number;
}

const COLOR_MAP: Record<string, { bg: string; border: string; text: string }> = {
  green:  { bg: "bg-emerald-50", border: "border-emerald-200", text: "text-emerald-700" },
  red:    { bg: "bg-red-50",     border: "border-red-200",     text: "text-red-700" },
  blue:   { bg: "bg-blue-50",    border: "border-blue-200",    text: "text-blue-700" },
  orange: { bg: "bg-orange-50",  border: "border-orange-200",  text: "text-orange-700" },
  purple: { bg: "bg-purple-50",  border: "border-purple-200",  text: "text-purple-700" },
};

export function ValueSummaryPanel() {
  const [data, setData] = useState<ValueData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchValueSummary()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="bg-gradient-to-br from-amber-50 to-orange-50 rounded-xl shadow-lg p-8 border border-amber-200">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-amber-100 rounded w-2/3 mx-auto" />
          <div className="grid grid-cols-2 gap-4">
            {[...Array(4)].map((_, i) => <div key={i} className="h-20 bg-amber-100 rounded-lg" />)}
          </div>
        </div>
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className="bg-gradient-to-br from-amber-50 via-orange-50 to-yellow-50 rounded-xl shadow-lg p-6 border border-amber-200">
      {/* Headline */}
      <div className="text-center mb-6">
        <div className="text-3xl mb-2">🧇</div>
        <h2 className="text-xl font-extrabold text-gray-900 leading-tight">
          {data.headline}
        </h2>
        <p className="text-sm text-gray-500 mt-1">Son 7 gün özeti</p>
      </div>

      {/* Value cards grid */}
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
        {data.items.map((item, i) => {
          const colors = COLOR_MAP[item.color] || COLOR_MAP.blue;
          return (
            <div
              key={i}
              className={`${colors.bg} border ${colors.border} rounded-xl p-4 transition-transform hover:scale-[1.02]`}
            >
              <div className="text-2xl mb-1">{item.icon}</div>
              <div className="text-xs font-medium text-gray-500 mb-1">{item.label}</div>
              <div className={`text-lg font-extrabold ${colors.text} leading-tight`}>
                {item.value}
              </div>
              <div className="text-xs text-gray-400 mt-1">{item.detail}</div>
            </div>
          );
        })}
      </div>

      {/* Bottom bar — the money shot */}
      <div className="mt-6 grid grid-cols-3 gap-3">
        <div className="bg-white rounded-lg p-3 text-center shadow-sm">
          <div className="text-xs text-gray-400">Haftalık Gelir</div>
          <div className="text-lg font-extrabold text-gray-900">₺{data.weekly_revenue.toLocaleString('tr-TR', {maximumFractionDigits: 0})}</div>
        </div>
        <div className="bg-white rounded-lg p-3 text-center shadow-sm">
          <div className="text-xs text-gray-400">Risk</div>
          <div className="text-lg font-extrabold text-red-600">₺{data.weekly_risk.toLocaleString('tr-TR', {maximumFractionDigits: 0})}</div>
        </div>
        <div className="bg-white rounded-lg p-3 text-center shadow-sm">
          <div className="text-xs text-gray-400">Korunan</div>
          <div className="text-lg font-extrabold text-emerald-600">₺{data.protected_revenue.toLocaleString('tr-TR', {maximumFractionDigits: 0})}</div>
        </div>
      </div>
    </div>
  );
}
