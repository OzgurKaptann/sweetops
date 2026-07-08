/**
 * Pure-TypeScript unit tests for the customer order idempotency utility.
 *
 * Run with Node's built-in test runner (no extra framework):
 *   node --test src/lib/order-idempotency.test.ts
 *
 * These tests import the source with an explicit `.ts` extension because Node's
 * type-stripping loader does not add extensions. The file is excluded from the
 * Next production build via tsconfig `exclude`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  fingerprintOrder,
  generateIdempotencyKey,
  createIdempotencyStore,
  type KeyValueStorage,
  type OrderFingerprintInput,
} from "./order-idempotency.ts";

// A simple in-memory Storage double.
function fakeStorage(): KeyValueStorage {
  const map = new Map<string, string>();
  return {
    getItem: (k) => (map.has(k) ? map.get(k)! : null),
    setItem: (k, v) => {
      map.set(k, v);
    },
    removeItem: (k) => {
      map.delete(k);
    },
  };
}

// A provider bound to ONE storage instance (as the real app has one session).
function fakeStorageProvider(): () => KeyValueStorage {
  const s = fakeStorage();
  return () => s;
}

const base: OrderFingerprintInput = {
  qr_token: "tok-table-5",
  items: [
    {
      product_id: 10,
      quantity: 2,
      ingredients: [
        { ingredient_id: 3, quantity: 1 },
        { ingredient_id: 1, quantity: 2 },
      ],
    },
  ],
};

// ── Scenario 6 — deterministic normalization ─────────────────────────────────

test("equivalent payloads with different array ordering share a fingerprint", () => {
  const reordered: OrderFingerprintInput = {
    qr_token: "tok-table-5",
    items: [
      {
        product_id: 10,
        quantity: 2,
        ingredients: [
          { ingredient_id: 1, quantity: 2 },
          { ingredient_id: 3, quantity: 1 },
        ],
      },
    ],
  };
  assert.equal(fingerprintOrder(base), fingerprintOrder(reordered));
});

test("multi-item payloads are order-independent", () => {
  const a: OrderFingerprintInput = {
    qr_token: "tok-1",
    items: [
      { product_id: 2, quantity: 1, ingredients: [{ ingredient_id: 9, quantity: 1 }] },
      { product_id: 1, quantity: 1, ingredients: [{ ingredient_id: 4, quantity: 1 }] },
    ],
  };
  const b: OrderFingerprintInput = {
    qr_token: "tok-1",
    items: [
      { product_id: 1, quantity: 1, ingredients: [{ ingredient_id: 4, quantity: 1 }] },
      { product_id: 2, quantity: 1, ingredients: [{ ingredient_id: 9, quantity: 1 }] },
    ],
  };
  assert.equal(fingerprintOrder(a), fingerprintOrder(b));
});

// ── Scenario 7 — materially different payloads differ ────────────────────────

test("a different QR token, product, quantity, or ingredient changes the fingerprint", () => {
  const fp = fingerprintOrder(base);
  // Scenario 30 — a different QR context (rotated sticker / different table)
  // yields a new logical attempt.
  assert.notEqual(fp, fingerprintOrder({ ...base, qr_token: "tok-table-6" }));
  assert.notEqual(
    fp,
    fingerprintOrder({
      ...base,
      items: [{ ...base.items[0], quantity: 3 }],
    }),
  );
  assert.notEqual(
    fp,
    fingerprintOrder({
      ...base,
      items: [
        {
          ...base.items[0],
          ingredients: [
            { ingredient_id: 3, quantity: 1 },
            { ingredient_id: 1, quantity: 5 }, // ingredient quantity changed
          ],
        },
      ],
    }),
  );
  assert.notEqual(
    fp,
    fingerprintOrder({
      ...base,
      items: [
        {
          ...base.items[0],
          ingredients: [{ ingredient_id: 3, quantity: 1 }], // ingredient removed
        },
      ],
    }),
  );
});

test("qr_token null and undefined are equivalent", () => {
  const withNull = fingerprintOrder({ qr_token: null, items: [] });
  const withUndef = fingerprintOrder({ items: [] });
  assert.equal(withNull, withUndef);
});

// ── Key generation ───────────────────────────────────────────────────────────

test("generateIdempotencyKey returns unique, well-formed keys", () => {
  const a = generateIdempotencyKey();
  const b = generateIdempotencyKey();
  assert.notEqual(a, b);
  assert.match(
    a,
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
});

// ── Scenario 3 — network retry reuses the key ────────────────────────────────

test("unchanged payload reuses the same key across retries", () => {
  const store = createIdempotencyStore(fakeStorageProvider());
  const fp = fingerprintOrder(base);
  const first = store.getOrCreateKey(fp);
  const retry = store.getOrCreateKey(fp);
  assert.equal(first, retry);
});

// ── Scenario 2 — rapid double calls (same payload) share a key ───────────────

test("two rapid getOrCreateKey calls for the same payload yield one key", () => {
  const store = createIdempotencyStore(fakeStorageProvider());
  const fp = fingerprintOrder(base);
  const k1 = store.getOrCreateKey(fp);
  const k2 = store.getOrCreateKey(fp);
  assert.equal(k1, k2);
});

// ── Scenario 4 — payload change mints a new key ──────────────────────────────

test("changing the payload produces a new key", () => {
  const store = createIdempotencyStore(fakeStorageProvider());
  const k1 = store.getOrCreateKey(fingerprintOrder(base));
  const k2 = store.getOrCreateKey(
    fingerprintOrder({ ...base, items: [{ ...base.items[0], quantity: 9 }] }),
  );
  assert.notEqual(k1, k2);
});

// ── Scenario 5 — success clears the attempt; next order gets a new key ───────

test("clear() retires the attempt so the next order gets a fresh key", () => {
  const store = createIdempotencyStore(fakeStorageProvider());
  const fp = fingerprintOrder(base);
  const k1 = store.getOrCreateKey(fp);
  store.clear();
  assert.equal(store.read(), null);
  const k2 = store.getOrCreateKey(fp);
  assert.notEqual(k1, k2);
});

// ── Scenario 8 — storage unavailable → in-memory fallback ────────────────────

test("falls back to in-memory state when storage is unavailable", () => {
  const store = createIdempotencyStore(() => null);
  const fp = fingerprintOrder(base);
  const k1 = store.getOrCreateKey(fp);
  // Reused within the session even without persistent storage.
  assert.equal(store.getOrCreateKey(fp), k1);
  assert.equal(store.read()?.idempotencyKey, k1);
  store.clear();
  assert.equal(store.read(), null);
});

test("survives a storage provider that throws", () => {
  const store = createIdempotencyStore(() => {
    throw new Error("SecurityError: storage disabled");
  });
  const fp = fingerprintOrder(base);
  const k1 = store.getOrCreateKey(fp);
  assert.equal(store.getOrCreateKey(fp), k1);
});

test("persists the attempt to storage when available", () => {
  const backing = fakeStorage();
  const store = createIdempotencyStore(() => backing);
  const fp = fingerprintOrder(base);
  const key = store.getOrCreateKey(fp);
  // A fresh store instance reading the same backing storage sees the attempt —
  // this is what lets one logical attempt survive a page refresh.
  const reopened = createIdempotencyStore(() => backing);
  assert.equal(reopened.read()?.idempotencyKey, key);
  assert.equal(reopened.getOrCreateKey(fp), key);
});
