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
