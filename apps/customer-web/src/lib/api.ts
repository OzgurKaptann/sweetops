import type { OrderCreateRequest, OrderCreatedResponse } from '@sweetops/types';

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

/**
 * Classifies why an order request failed so the caller can decide whether the
 * idempotency attempt must be preserved.
 *
 * - `network`: the request never got a response (offline, timeout, aborted).
 *   The order MAY already exist on the server — the same key must be retried.
 * - `server`: the server responded 5xx. Outcome is uncertain — preserve the key.
 * - `validation`: the server responded 4xx. A deterministic rejection — the
 *   payload must change before retrying, which yields a fresh key.
 */
export type OrderRequestErrorKind = 'network' | 'server' | 'validation';

export class OrderRequestError extends Error {
  readonly kind: OrderRequestErrorKind;
  readonly status?: number;

  constructor(kind: OrderRequestErrorKind, status?: number) {
    super(`order request failed: ${kind}${status ? ` (${status})` : ''}`);
    this.name = 'OrderRequestError';
    this.kind = kind;
    this.status = status;
  }

  /** Uncertain outcomes — the pending idempotency attempt must be kept. */
  get isUncertain(): boolean {
    return this.kind === 'network' || this.kind === 'server';
  }
}

/**
 * Create an order.
 *
 * The idempotency key is passed explicitly by the call site so it always maps
 * to a single logical checkout attempt — it is never generated inside a
 * generic fetch helper. The same key is sent verbatim on retries of an
 * unchanged payload; the backend returns the already-created order (HTTP 200)
 * instead of creating a duplicate.
 */
export async function createOrder(
  payload: OrderCreateRequest,
  idempotencyKey: string,
): Promise<OrderCreatedResponse> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (idempotencyKey) {
    headers['Idempotency-Key'] = idempotencyKey;
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE}/public/orders/`, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
  } catch {
    // Request never completed — outcome is unknown, key must be retried.
    throw new OrderRequestError('network');
  }

  if (!res.ok) {
    const kind: OrderRequestErrorKind = res.status >= 500 ? 'server' : 'validation';
    throw new OrderRequestError(kind, res.status);
  }

  return res.json();
}
