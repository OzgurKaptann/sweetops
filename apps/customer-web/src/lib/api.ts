import type {
  OrderCreateRequest,
  OrderCreatedResponse,
  QrContextResponse,
} from '@sweetops/types';

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

// ── QR context resolution ─────────────────────────────────────────────────────

/**
 * Classifies why a QR resolution attempt failed so the UI can pick the right
 * Turkish state and decide whether a retry is meaningful.
 *
 * - `invalid`: server says the token is not valid (unknown / revoked / malformed).
 * - `unavailable`: token is valid but the table/store is not open to ordering.
 * - `network`: the request never got a response — a retry may succeed.
 */
export type QrResolveErrorKind = 'invalid' | 'unavailable' | 'network';

export class QrResolveError extends Error {
  readonly kind: QrResolveErrorKind;
  /** Turkish, user-facing message supplied by the server when available. */
  readonly userMessage?: string;

  constructor(kind: QrResolveErrorKind, userMessage?: string) {
    super(`qr resolve failed: ${kind}`);
    this.name = 'QrResolveError';
    this.kind = kind;
    this.userMessage = userMessage;
  }

  get canRetry(): boolean {
    return this.kind === 'network';
  }
}

/**
 * Resolve an opaque QR token to trustworthy store/table context. The customer
 * app never derives or trusts numeric store/table ids — they come only from here.
 */
export async function resolveQrContext(
  qrToken: string,
): Promise<QrContextResponse> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/public/qr-context/resolve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qr_token: qrToken }),
      cache: 'no-store',
    });
  } catch {
    throw new QrResolveError('network');
  }

  if (res.ok) return res.json();

  let detail: string | undefined;
  try {
    const body = await res.json();
    if (body && typeof body.detail === 'string') detail = body.detail;
  } catch {
    // no body / not JSON — fall back to a generic Turkish message below.
  }
  const kind: QrResolveErrorKind = res.status === 409 ? 'unavailable' : 'invalid';
  throw new QrResolveError(kind, detail);
}

// ── API functions ─────────────────────────────────────────────────────────────

/**
 * Load the menu, gated by the resolved QR token.
 *
 * The token is sent in the REQUEST BODY (POST /public/menu/resolve), never in
 * the URL — a query-string bearer token can leak through browser history,
 * proxy/CDN access logs, referrer headers and screenshots. The backend
 * re-validates the token; a missing/invalid token loads no menu.
 */
export async function fetchMenu(qrToken: string): Promise<EnrichedMenuResponse> {
  const res = await fetch(`${API_BASE}/public/menu/resolve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ qr_token: qrToken }),
    cache: 'no-store',
  });
  if (!res.ok) throw new Error('Menü yüklenemedi. Lütfen tekrar deneyin.');
  return res.json();
}

/**
 * Upsell suggestions for the table's OWN store.
 *
 * Suggestions are filtered by what is actually in stock, and stock is physical:
 * it belongs to one branch. So this posts the QR token (in the body, never the
 * URL — same rule as `fetchMenu`) and the backend resolves the store from it.
 * The ungated `GET /public/menu/upsell` has no store context and refuses once a
 * second branch is open, which would silently kill upsell in exactly the
 * multi-branch shops it matters most for.
 */
export async function fetchUpsell(
  qrToken: string,
  ingredientIds: number[],
): Promise<UpsellResponse> {
  const res = await fetch(`${API_BASE}/public/menu/upsell`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ qr_token: qrToken, ingredient_ids: ingredientIds }),
    cache: 'no-store',
  });
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
