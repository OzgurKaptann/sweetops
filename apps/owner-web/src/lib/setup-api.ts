/**
 * Typed store-setup / menu-provisioning client for owner-web.
 *
 * Session and CSRF behave exactly as everywhere else in this app: the session
 * lives in an HttpOnly cookie sent by `credentials: "include"` (JavaScript never
 * reads or stores it), and every state-changing request echoes the CSRF token
 * from the readable cookie in `X-CSRF-Token` (double-submit).
 *
 * Unlike ./inventory-api.ts there is **no `Idempotency-Key`**, and that is a
 * decision rather than an omission. A stock command is not repeatable — pressing
 * "Fire kaydet" twice bins the pistachio twice — so it needs a key. Publication
 * is: publishing an already-published product leaves one row and answers
 * `changed: false`. The backend therefore requires no key here, and inventing one
 * client-side would imply a de-duplication guarantee the server does not make.
 * Creation is the exception and is guarded server-side by a duplicate-name check
 * (409 `product_name_taken`), which this client surfaces rather than retries.
 *
 * The branch is never a parameter. It comes from the session server-side, so
 * there is no `store_id` to pass and nothing here that could point at another
 * shop's menu — the backend rejects a smuggled one with a 422 rather than
 * ignoring it.
 *
 * The wire contract stays English (`not_published`, `qr_token_already_active`).
 * Nothing here translates it — see ./setup-errors.ts and ./setup-view.ts for the
 * one place raw values become Turkish.
 */
// Explicit .ts extensions on the relative imports in this module and its
// siblings: Node's test runner resolves ESM specifiers literally. See
// tsconfig.json ("allowImportingTsExtensions").
import { API_BASE, UnauthorizedError, csrfHeaders } from "./auth.ts";

// ── Response types (mirroring app/schemas/store_setup.py) ────────────────────
//
// Prices are Decimal server-side and serialize as JSON strings. They stay strings
// here on purpose: parsing them into JS floats to re-add them is how a menu ends
// up quoting ₺119.99999.

export interface SetupCheck {
  /** Stable English key the UI branches on. Never rendered. */
  key: string;
  done: boolean;
  count: number;
  /** Turkish, written by the server. Rendered as-is. */
  label: string;
  detail: string;
}

export interface SetupStatus {
  store_id: number;
  store_name: string | null;
  catalog_active_products: number;
  tables_total: number;
  tables_with_active_qr: number;
  published_products: number;
  available_products: number;
  /** What a guest would actually see: published ∧ available ∧ active. */
  menu_products: number;
  ready_for_customer_orders: boolean;
  checks: SetupCheck[];
}

export interface MenuProductItem {
  product_id: number;
  name: string | null;
  category: string | null;
  base_price: string | null;
  is_active: boolean;
  published: boolean;
  /** Null when not published — there is no availability decision to report. */
  is_available: boolean | null;
  sort_order: number | null;
  published_at: string | null;
  /** Computed server-side with the customer menu's own predicate. */
  on_customer_menu: boolean;
}

export interface MenuProductListResponse {
  total: number;
  store_id: number;
  published_total: number;
  on_menu_total: number;
  items: MenuProductItem[];
}

export interface MenuPublicationReceipt {
  store_id: number;
  product_id: number;
  name: string | null;
  is_active: boolean;
  published: boolean;
  is_available: boolean | null;
  sort_order: number | null;
  on_customer_menu: boolean;
  /** False when the request asked for the state the row was already in. */
  changed: boolean;
}

export interface TableItem {
  table_id: number;
  store_id: number;
  table_number: string | null;
  display_name: string;
  has_active_qr: boolean;
  /**
   * The NON-SECRET leading fragment of the token, for matching a record to the
   * sticker physically on the table. It cannot be scanned and is not a token.
   */
  token_prefix: string | null;
  qr_created_at: string | null;
  qr_last_used_at: string | null;
}

export interface TableListResponse {
  total: number;
  store_id: number;
  with_active_qr: number;
  items: TableItem[];
}

/**
 * A freshly minted QR link — the one and only time it exists in cleartext.
 *
 * There is no endpoint that returns this for an EXISTING sticker, because the
 * raw token is stored only as a SHA-256 hash. Recovering it later is not a
 * missing feature; it is cryptographically impossible, and that is what makes a
 * database leak useless. So the UI must show `qr_url` immediately and say that it
 * will not be shown again (`notice` carries that sentence, pre-written).
 */
export interface TableQrReceipt {
  table_id: number;
  store_id: number;
  table_number: string | null;
  display_name: string;
  token_id: number;
  token_prefix: string;
  qr_url: string;
  /** True after a rotation: the sticker on that table just stopped working. */
  previous_token_revoked: boolean;
  notice: string;
}

export interface TableCreateResponse {
  table: TableItem;
  qr: TableQrReceipt | null;
}

// ── Errors ───────────────────────────────────────────────────────────────────

/**
 * A failed setup call, carrying the backend's stable `error` code and its Turkish
 * `message`. The code is what the UI branches on; the message is what a manager
 * reads. Neither a status line nor a stack trace is ever kept.
 */
export class SetupApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "SetupApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * A mutation whose OUTCOME IS UNKNOWN: the request left the browser and no
 * response came back.
 *
 * Less dangerous here than in the stock ledger — publishing twice is a no-op —
 * but a CREATE may well have succeeded, and telling a manager it failed makes
 * them type the product in again. So it stays a distinct error and the copy says
 * "check the list before retrying".
 */
export class SetupNetworkUncertainError extends Error {
  constructor() {
    super("network_uncertain");
    this.name = "SetupNetworkUncertainError";
  }
}

async function parseError(res: Response): Promise<SetupApiError> {
  // Read defensively: a proxy 502 is HTML, not the API's JSON, and its text must
  // never reach the screen.
  const body = await res.json().catch(() => ({}));
  const detail = (body as { detail?: unknown })?.detail;
  if (detail && typeof detail === "object") {
    const d = detail as { error?: string; message?: string };
    return new SetupApiError(res.status, d.error ?? "unknown", d.message ?? "");
  }
  return new SetupApiError(res.status, "unknown", "");
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
    throw new SetupApiError(0, "network_error", "");
  }
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw await parseError(res);
  return res.json();
}

/**
 * Every state-changing setup call, whatever its verb.
 *
 * `body` is omitted entirely for the verb-only routes (publish, unpublish,
 * rotate-qr): they carry no payload, and sending `{}` would invite somebody to
 * later put a `store_id` in it.
 */
async function sendJson<T>(
  method: "POST" | "PATCH",
  path: string,
  body?: unknown,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method,
      credentials: "include",
      cache: "no-store",
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...csrfHeaders(),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  } catch {
    throw new SetupNetworkUncertainError();
  }
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw await parseError(res);
  return res.json();
}

// ── Reads ────────────────────────────────────────────────────────────────────

export function fetchSetupStatus(): Promise<SetupStatus> {
  return getJson("/owner/setup/status");
}

export function fetchMenuProducts(): Promise<MenuProductListResponse> {
  return getJson("/owner/menu/products");
}

export function fetchTables(): Promise<TableListResponse> {
  return getJson("/owner/tables");
}

// ── Menu mutations ───────────────────────────────────────────────────────────

export interface ProductCreateBody {
  name: string;
  category?: string | null;
  /** Decimal as a string — never a float. */
  base_price: string;
  is_active?: boolean;
  /**
   * Publishes to the CALLER'S branch only, and only when the form asked. Defaults
   * to false server-side; sent explicitly so the request says what the checkbox
   * said.
   */
  publish_to_current_store?: boolean;
}

export function createProduct(
  body: ProductCreateBody,
): Promise<MenuPublicationReceipt> {
  return sendJson("POST", "/owner/menu/products", body);
}

/** A genuine patch: omitted fields are left alone server-side. */
export interface ProductUpdateBody {
  name?: string;
  category?: string | null;
  base_price?: string;
  /** Retires the product CHAIN-WIDE. Every branch's menu loses it at once. */
  is_active?: boolean;
}

export function updateProduct(
  productId: number,
  body: ProductUpdateBody,
): Promise<MenuPublicationReceipt> {
  return sendJson("PATCH", `/owner/menu/products/${productId}`, body);
}

export function publishProduct(
  productId: number,
): Promise<MenuPublicationReceipt> {
  return sendJson("POST", `/owner/menu/products/${productId}/publish`);
}

export function unpublishProduct(
  productId: number,
): Promise<MenuPublicationReceipt> {
  return sendJson("POST", `/owner/menu/products/${productId}/unpublish`);
}

export function setProductAvailability(
  productId: number,
  isAvailable: boolean,
): Promise<MenuPublicationReceipt> {
  return sendJson("PATCH", `/owner/menu/products/${productId}/availability`, {
    is_available: isAvailable,
  });
}

export function setProductSortOrder(
  productId: number,
  sortOrder: number,
): Promise<MenuPublicationReceipt> {
  return sendJson("PATCH", `/owner/menu/products/${productId}/sort-order`, {
    sort_order: sortOrder,
  });
}

// ── Table mutations ──────────────────────────────────────────────────────────

export interface TableCreateBody {
  table_number: string;
  /** A table with no sticker cannot be scanned, so this defaults to true. */
  issue_qr?: boolean;
}

export function createTable(body: TableCreateBody): Promise<TableCreateResponse> {
  return sendJson("POST", "/owner/tables", body);
}

export function renameTable(
  tableId: number,
  tableNumber: string,
): Promise<TableListResponse> {
  return sendJson("PATCH", `/owner/tables/${tableId}`, {
    table_number: tableNumber,
  });
}

/** Mints the FIRST sticker for a table that has none. 409 if one already exists. */
export function issueTableQr(tableId: number): Promise<TableQrReceipt> {
  return sendJson("POST", `/owner/tables/${tableId}/qr-token`);
}

/**
 * Replaces a table's sticker. **The printed code on that table stops working.**
 * The caller must confirm with the manager before invoking this.
 */
export function rotateTableQr(tableId: number): Promise<TableQrReceipt> {
  return sendJson("POST", `/owner/tables/${tableId}/rotate-qr`);
}
