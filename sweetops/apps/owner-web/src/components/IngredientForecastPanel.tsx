"use client";

import { useState, useEffect } from "react";
import { fetchIngredientForecast, IngredientForecastData } from "@/lib/api";
import { Card } from "@sweetops/ui";

export function IngredientForecastPanel() {
  const [data, setData] = useState<IngredientForecastData | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetchIngredientForecast()
        .then(res => {
            console.log("Forecast Data:", res);
            setData(res);
        })
        .catch(err => {
            console.error("Forecast Error:", err);
            setError(true);
        });
  }, []);

  if (error) return <Card className="p-6 text-red-500">Failed to load forecast data.</Card>;
  if (!data) return <Card className="p-6 animate-pulse h-64 bg-gray-100"></Card>;

  if (data.items.length === 0) {
    return (
      <Card className="p-6 min-h-[300px] flex flex-col">
        <h3 className="text-lg font-semibold text-gray-900 mb-2">Demand Forecast</h3>
        <p className="text-xs text-gray-500 mb-4">7-Day Horizon Baseline</p>
        <div className="flex-1 flex items-center justify-center text-gray-500 text-sm bg-gray-50 rounded border border-dashed border-gray-200">
          Not enough historical data to generate forecast yet.
        </div>
      </Card>
    );
  }

  const getTrendIcon = (direction: string) => {
    if (direction === 'up') return <span className="text-green-500 font-bold">↑</span>;
    if (direction === 'down') return <span className="text-red-500 font-bold">↓</span>;
    return <span className="text-gray-400 font-bold">-</span>;
  };

  const getConfidenceBadge = (level: string) => {
    if (level === 'high') return <span className="w-2 h-2 rounded-full bg-green-500 inline-block mr-1"></span>;
    if (level === 'medium') return <span className="w-2 h-2 rounded-full bg-yellow-400 inline-block mr-1"></span>;
    return <span className="w-2 h-2 rounded-full bg-gray-400 inline-block mr-1"></span>; // low
  };

  return (
    <Card className="p-6 min-h-[300px]">
      <div className="flex justify-between items-start mb-4">
        <div>
            <h3 className="text-lg font-semibold text-gray-900">Demand Forecast</h3>
            <p className="text-xs text-gray-500 mt-1">Based on {data.items[0]?.baseline_method.replace(/_/g, ' ')}</p>
        </div>
        <div className="text-xs text-gray-400 text-right">
            Horizon: {data.forecast_horizon_days} Days <br/>
            <span className="text-[10px]">Data points required: 7-14</span>
        </div>
      </div>
      
      <div className="space-y-4">
        {data.items.map((item, idx) => (
          <div key={idx} className="flex flex-col sm:flex-row sm:items-center justify-between p-3 border border-gray-100 rounded-lg hover:bg-gray-50 transition-colors">
            <div className="flex items-center gap-3 w-full sm:w-1/3 mb-2 sm:mb-0">
              {getConfidenceBadge(item.confidence_level)}
              <span className="font-medium text-gray-900">{item.ingredient_name}</span>
            </div>
            
            <div className="flex items-center justify-between w-full sm:w-2/3">
                <div className="text-sm text-gray-500 w-24 text-center">
                    {item.forecast_date}
                </div>
                <div className="flex items-center gap-2 w-20 justify-end">
                    <span className="text-sm text-gray-400" title="Recent Avg">({item.recent_avg_usage})</span>
                    <span className="font-semibold text-gray-900 text-base">{item.predicted_usage}</span>
                </div>
                <div className="w-16 text-right">
                   {getTrendIcon(item.trend_direction)} <span className="text-xs text-gray-500 ml-1">{item.trend_delta > 0 ? '+' : ''}{item.trend_delta || ''}</span>
                </div>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
