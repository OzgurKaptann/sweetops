/**
 * Inventory command idempotency — when a key is reused, and when it must not be.
 *
 * The header being present (tested in inventory-api.test.ts) is only half of the
 * protection. The other half is the POLICY, and it cuts both ways:
 *
 *   * Reuse the key too little and a retry of an unchanged command becomes a
 *     SECOND write — the pistachio is binned twice.
 *   * Reuse it too much and an EDITED command inherits a completed key — the
 *     manager corrects 2 kg to 5 kg, the backend recognises the old key, replays
 *     the 2 kg receipt, and reports success. The correction is silently discarded.
 *
 * Both are silent. Neither raises anything. Hence these tests.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/inventory-idempotency.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createCommandIdempotency,
  fingerprintCommand,
  generateIdempotencyKey,
  type WasteCommand,
} from "./inventory-idempotency.ts";

const waste = (over: Partial<WasteCommand> = {}): WasteCommand => ({
  kind: "waste",
  ingredientId: 1,
  quantity: "2.000",
  reason: "Yanan hamur",
  ...over,
});

// ── Key generation ───────────────────────────────────────────────────────────

test("keys are unique and UUID-shaped", () => {
  const keys = new Set(Array.from({ length: 200 }, () => generateIdempotencyKey()));
  assert.equal(keys.size, 200);
  for (const key of keys) {
    assert.match(key, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
  }
});

// ── Fingerprints ─────────────────────────────────────────────────────────────

test("an unchanged command has a stable fingerprint", () => {
  assert.equal(fingerprintCommand(waste()), fingerprintCommand(waste()));
});

test("every field that changes what is persisted changes the fingerprint", () => {
  const base = fingerprintCommand(waste());
  assert.notEqual(base, fingerprintCommand(waste({ quantity: "5.000" })));
  assert.notEqual(base, fingerprintCommand(waste({ ingredientId: 2 })));
  assert.notEqual(base, fingerprintCommand(waste({ reason: "Düşen tepsi" })));
});

test("two different command kinds never collide", () => {
  const w = fingerprintCommand(waste({ quantity: "2.000" }));
  const p = fingerprintCommand({
    kind: "purchase_receipt",
    ingredientId: 1,
    quantity: "2.000",
    reason: "Yanan hamur",
  });
  // Same ingredient, same quantity, same words — but one bins stock and the other
  // buys it. Sharing a key would let a receipt replay as a write-off.
  assert.notEqual(w, p);
});

test("a transfer's destination is part of its identity", () => {
  const toBesiktas = fingerprintCommand({
    kind: "transfer",
    destinationStoreId: 2,
    ingredientId: 1,
    quantity: "10.000",
    reason: "Takviye",
  });
  const toUskudar = fingerprintCommand({
    kind: "transfer",
    destinationStoreId: 3,
    ingredientId: 1,
    quantity: "10.000",
    reason: "Takviye",
  });
  // Otherwise a manager who picked the wrong branch, noticed, and re-picked would
  // replay the shipment to the WRONG branch and be told it succeeded.
  assert.notEqual(toBesiktas, toUskudar);
});

// ── Attempt lifecycle ────────────────────────────────────────────────────────

test("retrying an UNCHANGED command reuses the same key", () => {
  const idem = createCommandIdempotency();
  const fp = fingerprintCommand(waste());

  const first = idem.begin(fp);
  idem.release(); // the request failed, or we never learned its outcome
  const retry = idem.begin(fp);

  // The backend de-duplicates on this key. If it changed, the retry would bin the
  // same 2 kg a second time.
  assert.equal(retry.key, first.key);
});

test("an EDITED command mints a new key", () => {
  const idem = createCommandIdempotency();

  const first = idem.begin(fingerprintCommand(waste({ quantity: "2.000" })));
  idem.release();
  const edited = idem.begin(fingerprintCommand(waste({ quantity: "5.000" })));

  assert.notEqual(edited.key, first.key);
});

test("a double-click is swallowed rather than sent twice", () => {
  const idem = createCommandIdempotency();
  const fp = fingerprintCommand(waste());

  const first = idem.begin(fp);
  const second = idem.begin(fp); // the first request is still in flight

  assert.equal(first.alreadyInFlight, false);
  assert.equal(second.alreadyInFlight, true, "the second click must be suppressed");
  assert.equal(second.key, first.key);
});

test("after a CONFIRMED result the key is retired, never reused", () => {
  const idem = createCommandIdempotency();
  const fp = fingerprintCommand(waste());

  const first = idem.begin(fp);
  idem.complete(); // the backend answered; the movement exists
  assert.equal(idem.peek(), null);

  // Recording the same waste again is a genuine SECOND loss — the same pistachio
  // burnt twice is two events. Inheriting the completed key would replay the first
  // and silently swallow the second.
  const again = idem.begin(fp);
  assert.notEqual(again.key, first.key);
});
