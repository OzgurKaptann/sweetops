import { KPIsResponse, TopIngredientsResponse, HourlyDemandResponse, DailySalesResponse } from '@sweetops/types';

// Extend our shared types to match the new endpoints safely for MVP
export interface DashboardKPIs {
  as_of: string;
  currency: string;
  kpis: {
    total_orders: number;
    gross_revenue: number;
    average_order_value: number;
    active_orders_count: number;
    delivered_orders_count: number;
    peak_hour: string | null;
  }
}

export interface TopIngredientsData {
  as_of: string;
  items: Array<{
    rank: number;
    ingredient_name: string;
    usage_count: number;
    usage_share: number;
  }>;
}

export interface HourlyDemandData {
  as_of: string;
  points: Array<{
    hour_bucket: string;
    order_count: number;
  }>;
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

export async function fetchKPIs(): Promise<DashboardKPIs> {
  const res = await fetch(`${API_BASE}/owner/kpis`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export async function fetchTopIngredients(): Promise<TopIngredientsData> {
  const res = await fetch(`${API_BASE}/owner/top-ingredients`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export async function fetchHourlyDemand(): Promise<HourlyDemandData> {
  const res = await fetch(`${API_BASE}/owner/hourly-demand`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export interface IngredientForecastData {
  as_of: string;
  forecast_horizon_days: number;
  items: Array<{
    ingredient_name: string;
    forecast_date: string;
    predicted_usage: number;
    recent_avg_usage: number;
    trend_direction: 'up' | 'down' | 'stable';
    trend_delta: number;
    baseline_method: string;
    confidence_level: 'high' | 'medium' | 'low';
    data_points_used: number;
  }>;
}

export async function fetchIngredientForecast(): Promise<IngredientForecastData> {
  const res = await fetch(`${API_BASE}/owner/ingredient-forecast`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}
