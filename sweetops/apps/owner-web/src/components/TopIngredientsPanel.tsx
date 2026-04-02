"use client";

import { useState, useEffect } from "react";
import { fetchTopIngredients, TopIngredientsData } from "@/lib/api";
import { Card } from "@sweetops/ui";

export function TopIngredientsPanel() {
  const [data, setData] = useState<TopIngredientsData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetchTopIngredients().then(setData).catch(() => setError(true));
  }, []);

  if (error) return <Card className="p-6 text-red-500">Failed to load ingredients.</Card>;
  if (!data) return <Card className="p-6 animate-pulse h-64 bg-gray-100"></Card>;

  if (data.items.length === 0) {
    return (
      <Card className="p-6 min-h-[300px] flex flex-col">
        <h3 className="text-lg font-semibold text-gray-900 mb-4">Top Ingredients</h3>
        <div className="flex-1 flex items-center justify-center text-gray-500">
          No ingredient data available yet.
        </div>
      </Card>
    );
  }

  return (
    <Card className="p-6 min-h-[300px]">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">Top Ingredients</h3>
      <div className="space-y-4">
        {data.items.map((item) => (
          <div key={item.rank} className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <span className="text-sm font-bold text-gray-400 w-4">{item.rank}.</span>
              <span className="font-medium text-gray-900">{item.ingredient_name}</span>
            </div>
            <div className="flex items-center gap-4">
              <span className="text-sm text-gray-500">{item.usage_count} uses</span>
              <span className="text-sm font-semibold text-blue-600 bg-blue-50 px-2 py-1 rounded">
                {(item.usage_share * 100).toFixed(1)}%
              </span>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
