// Types are defined locally in this file

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

export interface StockItem {
  ingredient_id: number;
  ingredient_name: string;
  category: string;
  unit: string;
  stock_quantity: number;
  reorder_level: number;
  severity: 'critical' | 'warning' | 'low' | 'ok';
  message: string;
}

export interface StockStatusData {
  total: number;
  critical_count: number;
  warning_count: number;
  items: StockItem[];
}

export async function fetchStockStatus(): Promise<StockStatusData> {
  const res = await fetch(`${API_BASE}/owner/stock-status`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

// --- Owner Insights ---

export async function fetchCriticalAlerts(): Promise<any> {
  const res = await fetch(`${API_BASE}/owner/insights/critical-alerts`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export async function fetchPrepTime(): Promise<any> {
  const res = await fetch(`${API_BASE}/owner/insights/prep-time`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export async function fetchTrendingIngredients(): Promise<any> {
  const res = await fetch(`${API_BASE}/owner/insights/trending-ingredients`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export async function fetchPopularCombos(): Promise<any> {
  const res = await fetch(`${API_BASE}/owner/insights/popular-combos`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export async function fetchValueSummary(): Promise<any> {
  const res = await fetch(`${API_BASE}/owner/insights/value-summary`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}



