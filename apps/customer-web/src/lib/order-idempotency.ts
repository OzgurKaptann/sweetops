/**
 * Customer-side order idempotency.
 *
 * One logical checkout attempt must create at most one order. The backend
 * already de-duplicates by the `Idempotency-Key` header (see
 * apps/api/app/services/order_service.py), but it can only do so if the
 * customer app sends *the same* key across retries of the same order and a
 * *new* key once the order payload changes.
 *
 * This module is intentionally free of React / DOM-render coupling so it can
 * be unit tested as pure TypeScript. Browser storage is accessed only through
 * the guarded provider below and always degrades to an in-memory fallback.
 */

// ── Types ────────────────────────────────────────────────────────────────────

/**
 * The logical shape of an order request that must map 1:1 to an idempotency key.
 *
 * Context is identified by the opaque `qr_token`, not by client-trusted numeric
 * store/table ids. A different QR token (a different physical table, or a
 * rotated sticker) therefore yields a different fingerprint and a fresh
 * idempotency attempt; the same token with an unchanged cart reuses the key.
 */
export interface OrderFingerprintInput {
  qr_token?: string | null;
  items: Array<{
    product_id: number;
    quantity: number;
    ingredients: Array<{ ingredient_id: number; quantity: number }>;
  }>;
}

/** Persisted record of the single in-flight checkout attempt. */
export type PendingOrderAttempt = {
  fingerprint: string;
  idempotencyKey: string;
};

/** Minimal storage contract satisfied by `sessionStorage`. */
export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

// ── Constants ────────────────────────────────────────────────────────────────

const STORAGE_KEY = "sweetops.pendingOrderAttempt";
const PROBE_KEY = "__sweetops_idem_probe__";

// ── Fingerprint ──────────────────────────────────────────────────────────────

/**
 * Produce a deterministic fingerprint of the *logical* order.
 *
 * Ordering of items and ingredients is normalized so that two payloads that
 * represent the same order — but happen to list their arrays in a different
 * order — collapse to the same fingerprint. Only fields that actually affect
 * what the backend persists are included; transient UI state is excluded.
 */
export function fingerprintOrder(input: OrderFingerprintInput): string {
  const items = [...input.items]
    .map((item) => {
      const ingredients = [...item.ingredients]
        .map((ing) => ({
          ingredient_id: ing.ingredient_id,
          quantity: ing.quantity,
        }))
        .sort(
          (a, b) =>
            a.ingredient_id - b.ingredient_id || a.quantity - b.quantity,
        );
      return {
        product_id: item.product_id,
        quantity: item.quantity,
        ingredients,
      };
    })
    .sort(
      (a, b) =>
        a.product_id - b.product_id ||
        a.quantity - b.quantity ||
        // Stable secondary representation: compare normalized ingredient lists.
        JSON.stringify(a.ingredients).localeCompare(
          JSON.stringify(b.ingredients),
        ),
    );

  return JSON.stringify({
    qr_token: input.qr_token ?? null,
    items,
  });
}

// ── Key generation ───────────────────────────────────────────────────────────

/**
 * Generate a cryptographically strong, unpredictable idempotency key.
 *
 * Prefers `crypto.randomUUID()`; falls back to a UUIDv4 built from
 * `crypto.getRandomValues`. Timestamps, counters and `Math.random` are never
 * used on their own — they are neither unique enough nor unpredictable.
 */
export function generateIdempotencyKey(): string {
  const c: Crypto | undefined =
    typeof globalThis !== "undefined"
      ? (globalThis.crypto as Crypto | undefined)
      : undefined;

  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }

  if (c && typeof c.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    c.getRandomValues(bytes);
    // RFC 4122 version 4 / variant bits.
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join(
      "",
    );
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(
      12,
      16,
    )}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  // A secure random source is mandatory: refuse to mint a weak key rather than
  // risk colliding or predictable idempotency keys.
  throw new Error("No secure random source available for idempotency key");
}

// ── Storage provider ─────────────────────────────────────────────────────────

/**
 * Resolve `sessionStorage` if — and only if — it is present and writable.
 * Returns `null` during SSR, in private-mode lockouts, or when disabled, so
 * callers transparently fall back to in-memory state.
 */
export function defaultSessionStorageProvider(): KeyValueStorage | null {
  try {
    if (typeof window === "undefined" || !window.sessionStorage) return null;
    const s = window.sessionStorage;
    // Probe: some browsers expose the object but throw on write.
    s.setItem(PROBE_KEY, "1");
    s.removeItem(PROBE_KEY);
    return s;
  } catch {
    return null;
  }
}

// ── Store ────────────────────────────────────────────────────────────────────

export interface IdempotencyStore {
  /** Current pending attempt, or `null`. */
  read(): PendingOrderAttempt | null;
  /**
   * Return the idempotency key for `fingerprint`. Reuses the existing key when
   * the fingerprint is unchanged (retries / double-clicks); mints and persists
   * a new key when the payload changed or no attempt exists.
   */
  getOrCreateKey(fingerprint: string): string;
  /** Forget the completed / abandoned attempt. */
  clear(): void;
}

/**
 * Build an idempotency store. Storage is resolved lazily on every operation so
 * a store constructed during SSR (no `window`) still uses real storage once it
 * runs in the browser. When storage is unavailable, an in-memory copy is used.
 */
export function createIdempotencyStore(
  getStorage: () => KeyValueStorage | null = defaultSessionStorageProvider,
): IdempotencyStore {
  let memory: PendingOrderAttempt | null = null;

  function storage(): KeyValueStorage | null {
    try {
      return getStorage();
    } catch {
      return null;
    }
  }

  function read(): PendingOrderAttempt | null {
    const s = storage();
    if (!s) return memory;
    try {
      const raw = s.getItem(STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as PendingOrderAttempt;
      if (
        parsed &&
        typeof parsed.fingerprint === "string" &&
        typeof parsed.idempotencyKey === "string"
      ) {
        return parsed;
      }
      return null;
    } catch {
      return memory;
    }
  }

  function write(attempt: PendingOrderAttempt): void {
    memory = attempt;
    const s = storage();
    if (!s) return;
    try {
      s.setItem(STORAGE_KEY, JSON.stringify(attempt));
    } catch {
      // Keep the in-memory copy; the attempt still survives this session.
    }
  }

  function clear(): void {
    memory = null;
    const s = storage();
    if (!s) return;
    try {
      s.removeItem(STORAGE_KEY);
    } catch {
      // In-memory copy already cleared.
    }
  }

  function getOrCreateKey(fingerprint: string): string {
    const existing = read();
    if (existing && existing.fingerprint === fingerprint) {
      return existing.idempotencyKey;
    }
    const attempt: PendingOrderAttempt = {
      fingerprint,
      idempotencyKey: generateIdempotencyKey(),
    };
    write(attempt);
    return attempt.idempotencyKey;
  }

  return { read, getOrCreateKey, clear };
}

/** Shared store for the customer app's single active checkout attempt. */
export const orderIdempotency: IdempotencyStore = createIdempotencyStore();
