/**
 * Pure-TypeScript unit tests for the cashier command idempotency utility.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/payment-idempotency.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  fingerprintCommand,
  generateIdempotencyKey,
  createCommandIdempotency,
  type CollectionCommandInput,
} from "./payment-idempotency.ts";

const collection: CollectionCommandInput = {
  kind: "collection",
  tableId: 5,
  orderIds: [101, 102],
  paymentMethod: "CARD",
};

// ── Fingerprint normalization ────────────────────────────────────────────────

test("order-id ordering does not change the collection fingerprint", () => {
  const a = fingerprintCommand(collection);
  const b = fingerprintCommand({ ...collection, orderIds: [102, 101] });
  assert.equal(a, b);
});

test("changing method, amount, or orders changes the fingerprint", () => {
  const base = fingerprintCommand(collection);
  assert.notEqual(base, fingerprintCommand({ ...collection, paymentMethod: "CASH" }));
  assert.notEqual(base, fingerprintCommand({ ...collection, amount: "10.00" }));
  assert.notEqual(base, fingerprintCommand({ ...collection, orderIds: [101] }));
});

test("refund fingerprint depends on allocation, amount, and reason", () => {
  const f1 = fingerprintCommand({ kind: "refund", allocationId: 1, amount: "5.00", reason: "x" });
  const f2 = fingerprintCommand({ kind: "refund", allocationId: 1, amount: "5.00", reason: "y" });
  const f3 = fingerprintCommand({ kind: "refund", allocationId: 2, amount: "5.00", reason: "x" });
  assert.notEqual(f1, f2);
  assert.notEqual(f1, f3);
});

// ── Key generation ───────────────────────────────────────────────────────────

test("generateIdempotencyKey produces unique UUID-shaped keys", () => {
  const k1 = generateIdempotencyKey();
  const k2 = generateIdempotencyKey();
  assert.notEqual(k1, k2);
  assert.match(k1, /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
});

// ── Attempt lifecycle ────────────────────────────────────────────────────────

test("double-click reuses one key and flags the second as in-flight", () => {
  const store = createCommandIdempotency();
  const fp = fingerprintCommand(collection);
  const first = store.begin(fp);
  const second = store.begin(fp);
  assert.equal(first.key, second.key);
  assert.equal(first.alreadyInFlight, false);
  assert.equal(second.alreadyInFlight, true);
});

test("network uncertainty preserves the same key for a retry", () => {
  const store = createCommandIdempotency();
  const fp = fingerprintCommand(collection);
  const first = store.begin(fp);
  store.release(); // we never learned the result
  const retry = store.begin(fp);
  assert.equal(first.key, retry.key);
  assert.equal(retry.alreadyInFlight, false); // released, so a retry is allowed
});

test("a changed command generates a new key", () => {
  const store = createCommandIdempotency();
  const k1 = store.begin(fingerprintCommand(collection)).key;
  const k2 = store.begin(
    fingerprintCommand({ ...collection, paymentMethod: "CASH" }),
  ).key;
  assert.notEqual(k1, k2);
});

test("confirmed success clears the attempt so the next command is fresh", () => {
  const store = createCommandIdempotency();
  const fp = fingerprintCommand(collection);
  const k1 = store.begin(fp).key;
  store.complete();
  assert.equal(store.peek(), null);
  const k2 = store.begin(fp).key;
  assert.notEqual(k1, k2); // same logical command, but the prior one completed
});
