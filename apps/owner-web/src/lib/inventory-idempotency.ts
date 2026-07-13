/**
 * Inventory command idempotency.
 *
 * A stock command is not naturally repeatable. Pressing "Fire kaydet" twice does
 * not confirm one loss — it bins the pistachio twice, and the second write is
 * indistinguishable in the ledger from a real second loss. The backend
 * de-duplicates by the `Idempotency-Key` header, but only if this client obeys
 * two rules:
 *
 *   - Reuse the SAME key while retrying an UNCHANGED command (double-click, or a
 *     retry after a timeout where we never learned the outcome).
 *   - Mint a NEW key the moment anything about the command changes. A manager who
 *     edits 2 kg to 5 kg means a different event; inheriting the completed key
 *     would replay the 2 kg receipt and cheerfully report success.
 *
 * The same shape as cashier-web's payment-idempotency.ts, for the same reason:
 * money and stock are both ledgers that must not double-count.
 *
 * Keys are held in memory only and never rendered — an idempotency key is a
 * replay token, and neither the session token nor this key belongs in browser
 * storage or on screen.
 *
 * Free of React/DOM coupling so it can be unit-tested as pure TypeScript.
 */

// ── Command fingerprints ─────────────────────────────────────────────────────

export interface PurchaseReceiptCommand {
  kind: "purchase_receipt";
  ingredientId: number;
  quantity: string;
  reason?: string | null;
}

export interface WasteCommand {
  kind: "waste";
  ingredientId: number;
  quantity: string;
  reason: string;
}

export interface ManualAdjustmentCommand {
  kind: "manual_adjustment";
  ingredientId: number;
  /** Signed delta, as the string that will be sent. */
  delta: string;
  reason: string;
}

export interface TransferCommand {
  kind: "transfer";
  destinationStoreId: number;
  ingredientId: number;
  quantity: string;
  reason: string;
  note?: string | null;
}

export type InventoryCommand =
  | PurchaseReceiptCommand
  | WasteCommand
  | ManualAdjustmentCommand
  | TransferCommand;

/**
 * Deterministic fingerprint of the *logical* command.
 *
 * Every field that changes what the backend persists is included, so a changed
 * command yields a fresh fingerprint (and therefore a fresh key). The source
 * store is deliberately absent: it comes from the session, not from us.
 */
export function fingerprintCommand(cmd: InventoryCommand): string {
  switch (cmd.kind) {
    case "purchase_receipt":
      return JSON.stringify({
        kind: cmd.kind,
        ingredientId: cmd.ingredientId,
        quantity: cmd.quantity,
        reason: cmd.reason ?? null,
      });
    case "waste":
      return JSON.stringify({
        kind: cmd.kind,
        ingredientId: cmd.ingredientId,
        quantity: cmd.quantity,
        reason: cmd.reason,
      });
    case "manual_adjustment":
      return JSON.stringify({
        kind: cmd.kind,
        ingredientId: cmd.ingredientId,
        delta: cmd.delta,
        reason: cmd.reason,
      });
    case "transfer":
      return JSON.stringify({
        kind: cmd.kind,
        destinationStoreId: cmd.destinationStoreId,
        ingredientId: cmd.ingredientId,
        quantity: cmd.quantity,
        reason: cmd.reason,
        note: cmd.note ?? null,
      });
  }
}

// ── Key generation ───────────────────────────────────────────────────────────

/**
 * Generate a cryptographically strong idempotency key.
 *
 * Prefers `crypto.randomUUID()`, falls back to UUIDv4 from
 * `crypto.getRandomValues`, and REFUSES to mint a weak one. A predictable key is
 * worse than no key: it lets one manager's retry collide with another's command.
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

export interface PendingAttempt {
  fingerprint: string;
  idempotencyKey: string;
  inFlight: boolean;
}

export interface CommandIdempotency {
  /**
   * Begin (or resume) an attempt for `fingerprint`.
   *   - Same fingerprint as the current attempt → reuse its key. If it was already
   *     in flight, `alreadyInFlight` is true so the caller can swallow a
   *     double-click instead of firing a second request.
   *   - Different (or no) attempt → mint a new key.
   */
  begin(fingerprint: string): { key: string; alreadyInFlight: boolean };
  /** No longer in flight, but the attempt survives — e.g. network uncertainty. */
  release(): void;
  /** Clear the attempt after a CONFIRMED result. */
  complete(): void;
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
