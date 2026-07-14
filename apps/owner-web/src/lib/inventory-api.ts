/**
 * Typed inventory API client for owner-web.
 *
 * Session and CSRF behave exactly as everywhere else in this app: the session
 * lives in an HttpOnly cookie sent by `credentials: "include"` (JavaScript never
 * reads or stores it), and every state-changing request echoes the CSRF token
 * from the readable cookie in `X-CSRF-Token` (double-submit).
 *
 * Every mutation additionally carries an `Idempotency-Key`. Stock commands are
 * not idempotent by nature — pressing "Fire kaydet" twice would bin the pistachio
 * twice — so the backend de-duplicates by that header, and this client's job is
 * to send *the same* key while retrying an unchanged command and a *fresh* key
 * once the command changes. That policy lives in ./inventory-idempotency.ts; this
 * module only guarantees the header is present. A mutation without a key is a bug
 * the backend rejects (`idempotency_required`), so `postJson` refuses to send one.
 *
 * The wire contract stays English (TRANSFER_OUT, stock_not_configured). Nothing
 * here translates it — see ./labels.ts and ./inventory-errors.ts for the one
 * place raw values become Turkish.
 */
// Explicit .ts extensions on the relative imports in this module and its
// siblings: Node's test runner resolves ESM specifiers literally. See
// tsconfig.json ("allowImportingTsExtensions").
import { API_BASE, UnauthorizedError, csrfHeaders } from "./auth.ts";

// ── Response types (mirroring app/schemas/inventory.py) ──────────────────────
//
// Quantities are Decimal server-side and are serialized as JSON strings. They
// stay strings here on purpose: parsing them into JS floats to re-add them is
// how "0.1 + 0.2" ends up on a stock report. The backend is the only thing that
// does stock arithmetic; this app formats what it is given.

export interface StockItem {
  ingredient_id: number;
  ingredient_name: string;
  category: string | null;
  unit: string;
  on_hand_quantity: string;
  reserved_quantity: string;
  available_quantity: string;
  reorder_level: string | null;
}

export interface StockListResponse {
  total: number;
  items: StockItem[];
}

export interface MovementItem {
  id: number;
  ingredient_id: number;
  ingredient_name: string | null;
  movement_type: string;
  quantity: string;
  quantity_delta_on_hand: string;
  quantity_delta_reserved: string;
  unit: string;
  order_id: number | null;
  reason: string | null;
  actor_user_id: number | null;
  created_at: string;
}

export interface MovementListResponse {
  total: number;
  items: MovementItem[];
}

export interface MovementReceipt {
  movement_id: number;
  store_id: number;
  ingredient_id: number;
  movement_type: string;
  quantity: string;
  quantity_delta_on_hand: string;
  unit: string;
  reason: string | null;
  on_hand_quantity: string;
  reserved_quantity: string;
  available_quantity: string;
  created_at: string;
  idempotent_replay: boolean;
}

export interface TransferReceipt {
  transfer_id: number;
  source_store_id: number;
  destination_store_id: number;
  ingredient_id: number;
  ingredient_name: string | null;
  quantity: string;
  unit: string;
  status: string;
  reason: string;
  note: string | null;
  initiated_by_user_id: number;
  source_movement_id: number;
  destination_movement_id: number;
  source_on_hand_quantity: string;
  source_reserved_quantity: string;
  source_available_quantity: string;
  created_at: string;
  idempotent_replay: boolean;
}

export interface TransferItem {
  transfer_id: number;
  source_store_id: number;
  destination_store_id: number;
  ingredient_id: number;
  ingredient_name: string | null;
  quantity: string;
  unit: string;
  status: string;
  reason: string;
  note: string | null;
  initiated_by_user_id: number;
  direction: string;
  created_at: string;
}

export interface TransferListResponse {
  total: number;
  items: TransferItem[];
}

export interface TransferDestination {
  store_id: number;
  name: string;
  location: string | null;
}

export interface TransferDestinationListResponse {
  total: number;
  items: TransferDestination[];
}

/**
 * The result of a physical count.
 *
 * `movement_id` is null when the shelf agreed with the system: nothing physical
 * happened, so no ledger row was written. The count itself still exists — proving
 * the shelf was checked is exactly what it is for. The UI must therefore treat a
 * null `movement_id` as a SUCCESS with a different message, never as a failure.
 *
 * `system_on_hand_quantity` / `system_reserved_quantity` are what the SERVER
 * believed at the instant it applied the count, read under a row lock. They may
 * legitimately differ from what this screen last displayed — an order placed thirty
 * seconds ago moves them — which is precisely why the delta is computed there and
 * not here.
 */
export interface StockCountReceipt {
  stock_count_id: number;
  store_id: number;
  ingredient_id: number;
  ingredient_name: string | null;
  counted_quantity: string;
  system_on_hand_quantity: string;
  system_reserved_quantity: string;
  delta_quantity: string;
  unit: string;
  reason: string;
  note: string | null;
  status: string;
  counted_by_user_id: number;
  movement_id: number | null;
  on_hand_quantity: string;
  reserved_quantity: string;
  available_quantity: string;
  created_at: string;
  applied_at: string;
  idempotent_replay: boolean;
}

export interface StockCountItem {
  stock_count_id: number;
  store_id: number;
  ingredient_id: number;
  ingredient_name: string | null;
  counted_quantity: string;
  system_on_hand_quantity: string;
  system_reserved_quantity: string;
  delta_quantity: string;
  unit: string;
  reason: string;
  note: string | null;
  status: string;
  counted_by_user_id: number;
  movement_id: number | null;
  created_at: string;
  applied_at: string;
}

export interface StockCountListResponse {
  total: number;
  items: StockCountItem[];
}

// ── Errors ───────────────────────────────────────────────────────────────────

/**
 * A failed API call, carrying the backend's stable `error` code and its Turkish
 * `message`. The code is what the UI branches on; the message is what a manager
 * reads. Neither a status line nor a stack trace is ever kept.
 */
export class InventoryApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "InventoryApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * A mutation whose OUTCOME IS UNKNOWN: the request left the browser and no
 * response came back (offline, timeout, tab suspended). It is emphatically not
 * "the operation failed" — the stock may well have moved. The UI must say so and
 * must not silently re-submit; see INVENTORY_ERROR_NETWORK_UNCERTAIN.
 */
export class InventoryNetworkUncertainError extends Error {
  constructor() {
    super("network_uncertain");
    this.name = "InventoryNetworkUncertainError";
  }
}

async function parseError(res: Response): Promise<InventoryApiError> {
  // The body is read defensively: a proxy 502 is HTML, not the API's JSON, and
  // its text must never reach the screen.
  const body = await res.json().catch(() => ({}));
  const detail = (body as { detail?: unknown })?.detail;
  if (detail && typeof detail === "object") {
    const d = detail as { error?: string; message?: string };
    return new InventoryApiError(res.status, d.error ?? "unknown", d.message ?? "");
  }
  return new InventoryApiError(res.status, "unknown", "");
}

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    // A failed READ is safe to retry and changed nothing, so it is a plain error.
    throw new InventoryApiError(0, "network_error", "");
  }
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw await parseError(res);
  return res.json();
}

async function postJson<T>(
  path: string,
  body: unknown,
  idempotencyKey: string,
): Promise<T> {
  if (!idempotencyKey) {
    // Refuse locally rather than send a stock command the backend will reject:
    // a missing key means the caller bypassed the idempotency policy, and the
    // command must not be retried blind.
    throw new InventoryApiError(0, "idempotency_required", "");
  }

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
        ...csrfHeaders(),
      },
      body: JSON.stringify(body),
    });
  } catch {
    // The command may or may not have been applied. Do NOT collapse this into a
    // generic failure — that is what makes a manager press the button again.
    throw new InventoryNetworkUncertainError();
  }
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw await parseError(res);
  return res.json();
}

// ── Reads ────────────────────────────────────────────────────────────────────
//
// The store is never a parameter: it comes from the session server-side. There
// is no store_id to pass and nothing here to point at another branch.

export function fetchStock(): Promise<StockListResponse> {
  return getJson("/inventory/stock");
}

export function fetchMovements(params?: {
  ingredientId?: number;
  movementType?: string;
  limit?: number;
}): Promise<MovementListResponse> {
  const q = new URLSearchParams();
  if (params?.ingredientId !== undefined) q.set("ingredient_id", String(params.ingredientId));
  if (params?.movementType) q.set("movement_type", params.movementType);
  if (params?.limit !== undefined) q.set("limit", String(params.limit));
  const qs = q.toString();
  return getJson(`/inventory/movements${qs ? `?${qs}` : ""}`);
}

export function fetchTransfers(params?: {
  direction?: "OUTBOUND" | "INBOUND";
  limit?: number;
}): Promise<TransferListResponse> {
  const q = new URLSearchParams();
  if (params?.direction) q.set("direction", params.direction);
  if (params?.limit !== undefined) q.set("limit", String(params.limit));
  const qs = q.toString();
  return getJson(`/inventory/transfers${qs ? `?${qs}` : ""}`);
}

export function fetchTransferDestinations(): Promise<TransferDestinationListResponse> {
  return getJson("/inventory/transfer-destinations");
}

// ── Mutations (every one carries an Idempotency-Key) ─────────────────────────

export interface PurchaseReceiptBody {
  ingredient_id: number;
  /** Decimal as a string — never a float. */
  quantity: string;
  reason?: string | null;
}

export interface WasteBody {
  ingredient_id: number;
  quantity: string;
  reason: string;
}

export interface ManualAdjustmentBody {
  ingredient_id: number;
  /** SIGNED: negative writes stock off, positive adds it. */
  delta: string;
  reason: string;
}

export interface TransferBody {
  destination_store_id: number;
  ingredient_id: number;
  quantity: string;
  reason: string;
  note?: string | null;
}

export function createPurchaseReceipt(
  body: PurchaseReceiptBody,
  idempotencyKey: string,
): Promise<MovementReceipt> {
  return postJson("/inventory/purchase-receipts", body, idempotencyKey);
}

export function createWaste(
  body: WasteBody,
  idempotencyKey: string,
): Promise<MovementReceipt> {
  return postJson("/inventory/waste", body, idempotencyKey);
}

export function createManualAdjustment(
  body: ManualAdjustmentBody,
  idempotencyKey: string,
): Promise<MovementReceipt> {
  return postJson("/inventory/manual-adjustments", body, idempotencyKey);
}

export function createTransfer(
  body: TransferBody,
  idempotencyKey: string,
): Promise<TransferReceipt> {
  return postJson("/inventory/transfers", body, idempotencyKey);
}

/**
 * A physical count.
 *
 * Note what this body does NOT carry: no delta, no system quantities, no store.
 * The client states only what it COUNTED — the server reads what it believed from
 * the locked stock row and works out the difference itself. A client-computed delta
 * would be measured against whatever this screen last rendered, which an order
 * placed in the meantime has already made stale. The backend rejects unknown fields
 * outright, so sending them is not merely useless but a 422.
 */
export interface StockCountBody {
  ingredient_id: number;
  /** Decimal as a string. May be "0" — an empty shelf is a valid count. */
  counted_quantity: string;
  reason: string;
  note?: string | null;
}

export function createStockCount(
  body: StockCountBody,
  idempotencyKey: string,
): Promise<StockCountReceipt> {
  return postJson("/inventory/stock-counts", body, idempotencyKey);
}

export function fetchStockCounts(params?: {
  ingredientId?: number;
  limit?: number;
}): Promise<StockCountListResponse> {
  const q = new URLSearchParams();
  if (params?.ingredientId !== undefined) q.set("ingredient_id", String(params.ingredientId));
  if (params?.limit !== undefined) q.set("limit", String(params.limit));
  const qs = q.toString();
  return getJson(`/inventory/stock-counts${qs ? `?${qs}` : ""}`);
}
