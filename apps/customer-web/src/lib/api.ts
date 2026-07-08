import { OrderCreateRequest, OrderCreatedResponse } from '@sweetops/types';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

// ── Enriched menu types (conversion engine output) ───────────────────────────

export type StockStatus = 'in_stock' | 'low_stock' | 'out_of_stock';

export interface OutOfStockAlternative {
  ingredient_id: number;
  ingredient_name: string;
  category: string;
  price: string;
}

export interface EnrichedIngredient {
  id: number;
  name: string;
  category: string;
  price: string;
  unit: string;
  standard_quantity: string;
  allows_portion_choice: boolean;
  stock_status: StockStatus;
  popular_badge: boolean;
  profitable_badge: boolean;
  recommended_with: number[];
  out_of_stock_alternative: OutOfStockAlternative | null;
  ranking_score: number;
}

export interface EnrichedCategory {
  name: string;
  ingredients: EnrichedIngredient[];
}

export interface Product {
  id: number;
  name: string;
  base_price: string;
}

export interface EnrichedMenuResponse {
  categories: EnrichedCategory[];
  products: Product[];
}

export interface UpsellSuggestion {
  ingredient_id: number;
  ingredient_name: string;
  category: string;
  price: string;
  reason: string;
  combo_count: number;
  stock_status: StockStatus;
}

export interface UpsellResponse {
  suggestions: UpsellSuggestion[];
  based_on_ingredient_ids: number[];
}

// ── API functions ─────────────────────────────────────────────────────────────

export async function fetchMenu(): Promise<EnrichedMenuResponse> {
  const res = await fetch(`${API_BASE}/public/menu/`, { cache: 'no-store' });
  if (!res.ok) throw new Error('Menü yüklenemedi');
  return res.json();
}

export async function fetchUpsell(ingredientIds: number[]): Promise<UpsellResponse> {
  const params = ingredientIds.map((id) => `ingredient_ids=${id}`).join('&');
  const res = await fetch(`${API_BASE}/public/menu/upsell?${params}`, { cache: 'no-store' });
  if (!res.ok) return { suggestions: [], based_on_ingredient_ids: ingredientIds };
  return res.json();
}

export async function createOrder(payload: OrderCreateRequest): Promise<OrderCreatedResponse> {
  const res = await fetch(`${API_BASE}/public/orders/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error('Sipariş oluşturulamadı');
  return res.json();
}
