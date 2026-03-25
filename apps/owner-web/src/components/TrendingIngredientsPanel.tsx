"use client";

import { useEffect, useState } from "react";
import { fetchTrendingIngredients } from "@/lib/api";

interface Trend {
  ingredient_id: number;
  ingredient_name: string;
  category: string;
  this_week: number;
  last_week: number;
  change_pct: number;
  direction: "up" | "down" | "stable";
}

interface TrendData {
  trends: Trend[];
  rising_count: number;
  falling_count: number;
  top_rising: Trend[];
  top_falling: Trend[];
}

export function TrendingIngredientsPanel() {
  const [data, setData] = useState<TrendData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchTrendingIngredients()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">📈 Trend Malzemeler</h3>
        <div className="animate-pulse space-y-3">
          {[...Array(3)].map((_, i) => <div key={i} className="h-10 bg-gray-100 rounded" />)}
        </div>
      </div>
    );
  }

  if (!data || (data.top_rising.length === 0 && data.top_falling.length === 0)) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-2">📈 Trend Malzemeler</h3>
        <p className="text-gray-400 text-sm text-center py-4">
          Trend hesaplamak için en az 2 haftalık veri gerekli.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-bold text-gray-900">📈 Trend Malzemeler</h3>
        <div className="flex gap-2 text-xs">
          {data.rising_count > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-green-100 text-green-800 font-semibold">
              {data.rising_count} yükselişte
            </span>
          )}
          {data.falling_count > 0 && (
            <span className="px-2 py-0.5 rounded-full bg-red-100 text-red-800 font-semibold">
              {data.falling_count} düşüşte
            </span>
          )}
        </div>
      </div>

      {/* Rising */}
      {data.top_rising.length > 0 && (
        <div className="mb-4">
          <h4 className="text-sm font-semibold text-green-700 mb-2">🔥 Yükselenler</h4>
          <div className="space-y-2">
            {data.top_rising.map((t) => (
              <div key={t.ingredient_id} className="flex items-center justify-between p-3 bg-green-50 rounded-lg">
                <div>
                  <span className="font-semibold text-gray-900 text-sm">{t.ingredient_name}</span>
                  <span className="text-xs text-gray-400 ml-2">{t.this_week}× bu hafta</span>
                </div>
                <span className="text-green-700 font-bold text-sm">
                  +{t.change_pct.toFixed(0)}% ↑
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Falling */}
      {data.top_falling.length > 0 && (
        <div>
          <h4 className="text-sm font-semibold text-red-700 mb-2">📉 Düşenler</h4>
          <div className="space-y-2">
            {data.top_falling.map((t) => (
              <div key={t.ingredient_id} className="flex items-center justify-between p-3 bg-red-50 rounded-lg">
                <div>
                  <span className="font-semibold text-gray-900 text-sm">{t.ingredient_name}</span>
                  <span className="text-xs text-gray-400 ml-2">{t.this_week}× bu hafta</span>
                </div>
                <span className="text-red-700 font-bold text-sm">
                  {t.change_pct.toFixed(0)}% ↓
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      <p className="text-xs text-gray-400 mt-4 text-center">
        Geçen hafta ile karşılaştırma
      </p>
    </div>
  );
}
