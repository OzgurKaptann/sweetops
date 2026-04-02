// Types are defined locally in this file

// ── Decision Engine ──────────────────────────────────────────────────────────

export type DecisionType =
  | "stock_risk"
  | "demand_spike"
  | "slow_moving"
  | "sla_risk"
  | "revenue_anomaly";

export type DecisionSeverity = "high" | "medium" | "low";
export type DecisionStatus = "pending" | "acknowledged" | "completed" | "dismissed";
export type DecisionAction = "acknowledge" | "complete" | "dismiss";

export interface StockRiskData {
  ingredient_id: number;
  ingredient_name: string;
  unit: string;
  current_stock: number;
  reorder_level: number;
  velocity_per_hour: number;
  hours_to_stockout: number | null;
  revenue_at_risk: number;
}

export interface DemandSpikeData {
  last_1h_orders: number;
  avg_hourly_baseline: number;
  spike_ratio: number;
}

export interface SlowMovingData {
  ingredient_id: number;
  ingredient_name: string;
  current_stock: number;
  reorder_level: number;
  tied_capital: number;
  hours_since_last_use: number;
}

export interface SLARiskData {
  critical_order_ids: number[];
  warning_order_ids: number[];
  critical_count: number;
  warning_count: number;
  worst_age_minutes: number;
}

export interface RevenueAnomalyData {
  last_1h_revenue: number;
  avg_hourly_baseline: number;
  ratio: number;
  direction: "drop" | "spike";
}

export type DecisionData =
  | StockRiskData
  | DemandSpikeData
  | SlowMovingData
  | SLARiskData
  | RevenueAnomalyData;

export interface OwnerDecision {
  decision_id: string;
  type: DecisionType;
  severity: DecisionSeverity;
  decision_score: number;
  blocking_vs_non_blocking: boolean;
  title: string;
  description: string;
  impact: string;
  recommended_action: string;
  why_now: string;
  expected_impact: string;
  data: DecisionData;
  status: DecisionStatus;
  acknowledged_at: string | null;
  completed_at: string | null;
  actor_id: string | null;
  resolution_note: string | null;
  resolution_quality: ResolutionQuality | null;
  estimated_revenue_saved: number | null;
  created_at: string;
  updated_at: string;
}

export interface OwnerDecisionsResponse {
  decisions: OwnerDecision[];
  generated_at: string;
  signals_evaluated: number;
  active_count: number;
  summary: { high: number; medium: number; low: number };
}

export type ResolutionQuality = "good" | "partial" | "failed";

export interface DecisionActionRequest {
  action: DecisionAction;
  actor_id?: string;
  resolution_note?: string;
  resolution_quality?: ResolutionQuality;
  estimated_revenue_saved?: number;
}

// ── Shared types ──────────────────────────────────────────────────────────────

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

export interface DailySalesData {
  as_of: string;
  currency: string;
  points: Array<{
    sales_date: string;
    total_orders: number;
    gross_revenue: number;
    average_order_value: number;
  }>;
}

export async function fetchDailySales(): Promise<DailySalesData> {
  const res = await fetch(`${API_BASE}/owner/daily-sales`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

// ── Kitchen ───────────────────────────────────────────────────────────────────

export type SLASeverity = "ok" | "warning" | "critical";
export type OrderStatus = "NEW" | "IN_PREP" | "READY" | "DELIVERED" | "CANCELLED";

export interface KitchenOrderItem {
  id: number;
  product_id: number;
  product_name: string | null;
  quantity: number;
  ingredients: Array<{
    id: number;
    ingredient_id: number;
    ingredient_name: string | null;
    quantity: number;
  }>;
}

export interface KitchenOrder {
  id: number;
  store_id: number;
  table_id: number | null;
  status: OrderStatus;
  created_at: string;
  computed_age_minutes: number;
  priority_score: number;
  sla_severity: SLASeverity;
  should_be_started: boolean;
  urgency_reason: string;
  action_hint: string;
  items: KitchenOrderItem[];
}

export interface KitchenLoad {
  load_level: "low" | "medium" | "high";
  active_orders_count: number;
  in_prep_count: number;
  average_age_minutes: number;
  explanation: string;
}

export interface BatchingSuggestion {
  grouped_order_ids: number[];
  shared_ingredients: string[];
  estimated_time_saved: string;
}

export interface KitchenDashboardResponse {
  orders: KitchenOrder[];
  kitchen_load: KitchenLoad;
  batching_suggestions: BatchingSuggestion[];
}

export interface StatusUpdateResponse {
  order_id: number;
  new_status: string;
  updated_at: string;
}

export async function fetchKitchenOrders(
  storeId = 1,
): Promise<KitchenDashboardResponse> {
  const res = await fetch(`${API_BASE}/kitchen/orders/?store_id=${storeId}`, {
    cache: "no-store",
  });
  if (!res.ok) throw new Error("Kitchen API Error");
  return res.json();
}

export async function patchOrderStatus(
  orderId: number,
  status: OrderStatus,
  actorId?: string,
): Promise<StatusUpdateResponse> {
  const res = await fetch(`${API_BASE}/kitchen/orders/${orderId}/status`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      ...(actorId ? { "X-Actor-Id": actorId } : {}),
    },
    body: JSON.stringify({ status }),
    cache: "no-store",
  });
  if (!res.ok) throw new Error("Status update failed");
  return res.json();
}

export async function fetchDecisions(): Promise<OwnerDecisionsResponse> {
  const res = await fetch(`${API_BASE}/owner/decisions/`, { cache: 'no-store' });
  if (!res.ok) throw new Error('API Error');
  return res.json();
}

export async function patchDecision(
  decisionId: string,
  action: DecisionAction,
  actorId?: string,
  resolutionNote?: string,
  resolutionQuality?: ResolutionQuality,
  estimatedRevenueSaved?: number,
): Promise<OwnerDecision> {
  const body: DecisionActionRequest = { action };
  if (actorId) body.actor_id = actorId;
  if (resolutionNote) body.resolution_note = resolutionNote;
  if (resolutionQuality) body.resolution_quality = resolutionQuality;
  if (estimatedRevenueSaved !== undefined) body.estimated_revenue_saved = estimatedRevenueSaved;
  const res = await fetch(`${API_BASE}/owner/decisions/${decisionId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw Object.assign(new Error('Decision patch failed'), { status: res.status, detail: err });
  }
  return res.json();
}
