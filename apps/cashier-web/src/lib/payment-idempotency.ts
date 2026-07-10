/**
 * Cashier command idempotency.
 *
 * A financial command (a collection or a refund) must create at most one ledger
 * entry no matter how many times the button is pressed or how uncertain the
 * network gets. The backend de-duplicates by the `Idempotency-Key` header, but
 * only if this client sends *the same* key across retries of an unchanged
 * command and a *new* key once anything about the command changes.
 *
 * Rules implemented here:
 *   - Reuse the same key for an unchanged command (double-click, retry after a
 *     timeout where we never learned the result).
 *   - Generate a NEW key when the selected orders, method, amount, or refund
 *     details change (a different command must never inherit a completed key).
 *   - Preserve the attempt across network uncertainty; clear it only after a
 *     confirmed success.
 *   - Keys come from a cryptographically secure UUID source — never timestamps
 *     or `Math.random()` alone.
 *
 * This module is free of React/DOM coupling so it can be unit-tested as pure
 * TypeScript.
 */

// ── Command fingerprints ─────────────────────────────────────────────────────

/** A table/single-order collection command. */
export interface CollectionCommandInput {
  kind: "collection";
  storeScopedKey?: string | null; // reserved; store comes from session server-side
  tableId?: number | null;
  orderIds: number[];
  paymentMethod: string;
  /** Present only for a single-order partial payment; null/undefined for pay-all. */
  amount?: string | null;
}

/** A refund command against one allocation. */
export interface RefundCommandInput {
  kind: "refund";
  allocationId: number;
  amount: string;
  reason: string;
}

export type CommandInput = CollectionCommandInput | RefundCommandInput;

/**
 * Deterministic fingerprint of the *logical* command. Order-id ordering is
 * normalized so re-selecting the same orders in a different sequence collapses
 * to the same fingerprint; every field that changes what the backend persists
 * is included so a changed command yields a fresh fingerprint (and key).
 */
export function fingerprintCommand(input: CommandInput): string {
  if (input.kind === "refund") {
    return JSON.stringify({
      kind: "refund",
      allocationId: input.allocationId,
      amount: input.amount,
      reason: input.reason,
    });
  }
  const orderIds = [...input.orderIds].sort((a, b) => a - b);
  return JSON.stringify({
    kind: "collection",
    tableId: input.tableId ?? null,
    orderIds,
    paymentMethod: input.paymentMethod,
    amount: input.amount ?? null,
  });
}

// ── Key generation ───────────────────────────────────────────────────────────

/**
 * Generate a cryptographically strong idempotency key.
 * Prefers `crypto.randomUUID()`; falls back to UUIDv4 from
 * `crypto.getRandomValues`. Refuses to mint a weak key rather than risk
 * predictable/colliding keys.
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
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(
      16,
      20,
    )}-${hex.slice(20)}`;
  }
  throw new Error("No secure random source available for idempotency key");
}

// ── In-memory attempt store ──────────────────────────────────────────────────
//
// Deliberately in-memory only: a cashier command is an in-session action and
// the session token itself is never stored in browser storage. Keeping the
// attempt in memory avoids persisting any financial-command state to disk.

export interface PendingAttempt {
  fingerprint: string;
  idempotencyKey: string;
  inFlight: boolean;
}

export interface CommandIdempotency {
  /**
   * Begin (or resume) an attempt for `fingerprint`.
   * Returns `{ key, alreadyInFlight }`:
   *   - Same fingerprint as the current attempt → reuse its key. If it was
   *     already marked in-flight, `alreadyInFlight` is true so the caller can
   *     suppress a duplicate submit (double-click guard).
   *   - Different (or no) attempt → mint a new key and start a fresh attempt.
   */
  begin(fingerprint: string): { key: string; alreadyInFlight: boolean };
  /** Mark the current attempt as no longer in-flight (e.g. network uncertainty). */
  release(): void;
  /** Clear the attempt after a confirmed success. */
  complete(): void;
  /** Current attempt (or null). */
  peek(): PendingAttempt | null;
}

export function createCommandIdempotency(): CommandIdempotency {
  let attempt: PendingAttempt | null = null;

  return {
    begin(fingerprint: string) {
      if (attempt && attempt.fingerprint === fingerprint) {
        const alreadyInFlight = attempt.inFlight;
        attempt.inFlight = true;
        return { key: attempt.idempotencyKey, alreadyInFlight };
      }
      // A new/changed command: never reuse the previous key.
      attempt = {
        fingerprint,
        idempotencyKey: generateIdempotencyKey(),
        inFlight: true,
      };
      return { key: attempt.idempotencyKey, alreadyInFlight: false };
    },
    release() {
      if (attempt) attempt.inFlight = false;
    },
    complete() {
      attempt = null;
    },
    peek() {
      return attempt;
    },
  };
}
