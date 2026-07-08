/**
 * Tests for the customer API client's order creation.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/api.test.ts
 *
 * `fetch` is stubbed on globalThis; no network access occurs. Excluded from the
 * Next production build via tsconfig `exclude`.
 */
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";

import { createOrder, OrderRequestError } from "./api.ts";

const payload = {
  store_id: 1,
  table_id: 2,
  items: [
    {
      product_id: 1,
      quantity: 1,
      ingredients: [{ ingredient_id: 3, quantity: 1 }],
    },
  ],
};

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

// ── Scenario 1 — the Idempotency-Key header is sent ──────────────────────────

test("createOrder sends a non-empty Idempotency-Key header", async () => {
  let seen: Record<string, string> | undefined;
  globalThis.fetch = (async (_url: string, init: RequestInit) => {
    seen = init.headers as Record<string, string>;
    return {
      ok: true,
      status: 200,
      json: async () => ({ order_id: 42, total_amount: "10.00" }),
    } as Response;
  }) as typeof fetch;

  const res = await createOrder(payload, "key-abc-123");
  assert.equal(seen?.["Idempotency-Key"], "key-abc-123");
  assert.equal(seen?.["Content-Type"], "application/json");
  assert.equal(res.order_id, 42);
});

// ── Error classification drives retry safety ─────────────────────────────────

test("a rejected fetch (network) is classified as uncertain", async () => {
  globalThis.fetch = (async () => {
    throw new TypeError("Failed to fetch");
  }) as typeof fetch;

  await assert.rejects(createOrder(payload, "k"), (err: unknown) => {
    assert.ok(err instanceof OrderRequestError);
    assert.equal(err.kind, "network");
    assert.equal(err.isUncertain, true);
    return true;
  });
});

test("a 5xx response is classified as uncertain (server)", async () => {
  globalThis.fetch = (async () =>
    ({ ok: false, status: 503, json: async () => ({}) }) as Response) as typeof fetch;

  await assert.rejects(createOrder(payload, "k"), (err: unknown) => {
    assert.ok(err instanceof OrderRequestError);
    assert.equal(err.kind, "server");
    assert.equal(err.isUncertain, true);
    return true;
  });
});

test("a 4xx response is a deterministic validation failure", async () => {
  globalThis.fetch = (async () =>
    ({ ok: false, status: 422, json: async () => ({}) }) as Response) as typeof fetch;

  await assert.rejects(createOrder(payload, "k"), (err: unknown) => {
    assert.ok(err instanceof OrderRequestError);
    assert.equal(err.kind, "validation");
    assert.equal(err.isUncertain, false);
    return true;
  });
});
