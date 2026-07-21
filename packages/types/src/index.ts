export interface Product {
  id: number;
  name: string;
  category: string;
  base_price: string;
}

export interface Ingredient {
  id: number;
  name: string;
  category: string;
  price: string;
  unit?: string;
  standard_quantity?: string;
  allows_portion_choice?: boolean;
}

export interface IngredientCategory {
  name: string;
  ingredients: Ingredient[];
}

export interface MenuResponse {
  products: Product[];
  ingredients: Ingredient[];
  categories: IngredientCategory[];
}

export interface OrderItemIngredientCreate {
  ingredient_id: number;
  quantity: number;
}

export interface OrderItemCreate {
  product_id: number;
  quantity: number;
  ingredients: OrderItemIngredientCreate[];
}

export interface OrderCreateRequest {
  /**
   * Opaque QR token — the trusted source of store/table context. The backend
   * derives store_id/table_id from it server-side. The public customer client
   * must send this and must NOT send trusted numeric store/table ids.
   */
  qr_token?: string;
  /** @deprecated Legacy/transition only — never trusted in production. */
  store_id?: number;
  /** @deprecated Legacy/transition only — never trusted in production. */
  table_id?: number;
  items: OrderItemCreate[];
}

// ── Secure QR store/table context ────────────────────────────────────────────

export interface QrContextStore {
  id: number;
  name: string;
}

export interface QrContextTable {
  id: number;
  name: string;
}

export interface QrContextResponse {
  store: QrContextStore;
  table: QrContextTable;
  context_version: number;
}

export interface OrderCreatedResponse {
  order_id: number;
  status: string;
  created_at: string;
  item_count: number;
  total_amount: string;
}

export interface OrderItemIngredientResponse {
  id: number;
  ingredient_id: number;
  ingredient_name: string;
  quantity: number;
}

export interface OrderItemResponse {
  id: number;
  product_id: number;
  product_name: string;
  quantity: number;
  ingredients: OrderItemIngredientResponse[];
}

export interface KitchenOrder {
  id: number;
  store_id: number;
  table_id?: number;
  status: string;
  created_at: string;
  items: OrderItemResponse[];
}

export type OrderStatus = 'NEW' | 'IN_PREP' | 'READY' | 'DELIVERED' | 'CANCELLED';

// ── Kitchen preparation timing (derived from the order lifecycle) ─────────────

/** Static-threshold delay classification of an active order's live durations. */
export type DelayState = 'ok' | 'warning' | 'critical';

/** Per-order timing record. Durations are seconds, or null when not yet known. */
export interface OrderTiming {
  order_id: number;
  store_id: number;
  table_id?: number | null;
  status: string;
  created_at: string | null;
  prep_started_at: string | null;
  ready_at: string | null;
  delivered_at: string | null;
  cancelled_at: string | null;
  queued_seconds: number | null;
  prep_seconds: number | null;
  time_to_ready_seconds: number | null;
  queued_seconds_active: number | null;
  prep_seconds_active: number | null;
  active_seconds: number | null;
  is_delayed: boolean;
  delay_state: DelayState;
  delay_reason: string | null;
}

export interface ActiveTimingSummary {
  active_orders: number;
  waiting_orders: number;
  in_prep_orders: number;
  ready_orders: number;
  delayed_orders: number;
}

export interface ActiveTimingResponse {
  orders: OrderTiming[];
  summary: ActiveTimingSummary;
}
