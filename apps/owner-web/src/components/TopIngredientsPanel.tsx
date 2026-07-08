"use client";

import { useState, useEffect } from "react";
import { fetchTopIngredients, TopIngredientsData } from "@/lib/api";

interface Props {
  refreshTick?: number;
}

export function TopIngredientsPanel({ refreshTick }: Props) {
  const [data, setData] = useState<TopIngredientsData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    setError(false);
    fetchTopIngredients().then(setData).catch(() => setError(true));
  }, [refreshTick]);

  if (error) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 text-sm text-red-500">
        Ingredient data unavailable.
      </div>
    );
  }

  if (!data) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 animate-pulse">
        <div className="h-3 w-32 bg-gray-200 rounded mb-4" />
        {[...Array(5)].map((_, i) => (
          <div key={i} className="flex items-center gap-3 py-2.5">
            <div className="h-3 w-4 bg-gray-200 rounded" />
            <div className="h-3 flex-1 bg-gray-100 rounded" />
            <div className="h-3 w-12 bg-gray-200 rounded" />
          </div>
        ))}
      </div>
    );
  }

  if (data.items.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 text-sm text-gray-400 text-center py-8">
        No ingredient usage data yet.
      </div>
    );
  }

  const maxUsage = Math.max(...data.items.map((i) => i.usage_count), 1);

  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5">
      <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-4">
        Top Ingredients
      </h3>

      <div className="space-y-1">
        {data.items.map((item) => {
          const barPct = (item.usage_count / maxUsage) * 100;
          const isTop = item.rank === 1;
          return (
            <div
              key={item.rank}
              className="flex items-center gap-3 py-2 px-2 rounded-lg hover:bg-gray-50 transition-colors"
            >
              {/* Rank */}
              <span
                className={`text-xs font-bold w-5 text-right shrink-0 ${
                  isTop ? "text-amber-500" : "text-gray-300"
                }`}
              >
                {item.rank}
              </span>

              {/* Name + bar */}
              <div className="flex-1 min-w-0">
                <div className="flex items-baseline justify-between mb-1">
                  <span className="text-sm font-medium text-gray-900 truncate">
                    {item.ingredient_name}
                  </span>
                  <span className="text-xs text-gray-400 ml-2 shrink-0">
                    {item.usage_count}×
                  </span>
                </div>
                <div className="h-1 bg-gray-100 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full ${isTop ? "bg-amber-400" : "bg-blue-300"}`}
                    style={{ width: `${barPct}%` }}
                  />
                </div>
              </div>

              {/* Share */}
              <span className="text-xs font-semibold text-gray-500 w-10 text-right shrink-0">
                {(item.usage_share * 100).toFixed(0)}%
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
