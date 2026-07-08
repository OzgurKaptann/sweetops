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

import {
  createOrder,
  fetchMenu,
  resolveQrContext,
  OrderRequestError,
  QrResolveError,
} from "./api.ts";

const payload = {
  qr_token: "opaque-token-abc",
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

// ── Scenario 36 — the order request carries the QR token ─────────────────────

test("createOrder sends the qr_token in the request body (no numeric context)", async () => {
  let sentBody: string | undefined;
  globalThis.fetch = (async (_url: string, init: RequestInit) => {
    sentBody = init.body as string;
    return {
      ok: true,
      status: 200,
      json: async () => ({ order_id: 1, total_amount: "1.00" }),
    } as Response;
  }) as typeof fetch;

  await createOrder(payload, "k");
  const parsed = JSON.parse(sentBody!);
  assert.equal(parsed.qr_token, "opaque-token-abc");
  assert.equal(parsed.store_id, undefined);
  assert.equal(parsed.table_id, undefined);
});

// ── Scenario 37 / Blocker 1 — the menu request carries the QR token in the
//    BODY, never the URL ──────────────────────────────────────────────────────

test("fetchMenu posts the qr_token in the body and never in the URL", async () => {
  let seenUrl: string | undefined;
  let seenInit: RequestInit | undefined;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    seenUrl = url;
    seenInit = init;
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  }) as typeof fetch;

  await fetchMenu("opaque-token-abc");

  // Test 7 — the token appears nowhere in the request URL.
  assert.ok(!seenUrl?.includes("opaque-token-abc"));
  assert.ok(!seenUrl?.includes("qr_token"));
  assert.ok(seenUrl?.endsWith("/public/menu/resolve"));

  // Test 8 — the token is sent in the JSON body via POST.
  assert.equal(seenInit?.method, "POST");
  const parsed = JSON.parse(seenInit?.body as string);
  assert.equal(parsed.qr_token, "opaque-token-abc");
});

// ── QR resolution error classification ───────────────────────────────────────

test("resolveQrContext returns store/table context on success", async () => {
  globalThis.fetch = (async () =>
    ({
      ok: true,
      status: 200,
      json: async () => ({
        store: { id: 1, name: "SweetOps" },
        table: { id: 5, name: "Masa 5" },
        context_version: 1,
      }),
    }) as Response) as typeof fetch;

  const ctx = await resolveQrContext("tok");
  assert.equal(ctx.store.id, 1);
  assert.equal(ctx.table.name, "Masa 5");
});

test("resolveQrContext classifies a 404 as invalid", async () => {
  globalThis.fetch = (async () =>
    ({
      ok: false,
      status: 404,
      json: async () => ({ detail: "Bu QR kod geçerli değil." }),
    }) as Response) as typeof fetch;

  await assert.rejects(resolveQrContext("tok"), (err: unknown) => {
    assert.ok(err instanceof QrResolveError);
    assert.equal(err.kind, "invalid");
    assert.equal(err.canRetry, false);
    return true;
  });
});

test("resolveQrContext classifies a 409 as unavailable", async () => {
  globalThis.fetch = (async () =>
    ({ ok: false, status: 409, json: async () => ({}) }) as Response) as typeof fetch;

  await assert.rejects(resolveQrContext("tok"), (err: unknown) => {
    assert.ok(err instanceof QrResolveError);
    assert.equal(err.kind, "unavailable");
    return true;
  });
});

test("resolveQrContext classifies a rejected fetch as retryable network error", async () => {
  globalThis.fetch = (async () => {
    throw new TypeError("Failed to fetch");
  }) as typeof fetch;

  await assert.rejects(resolveQrContext("tok"), (err: unknown) => {
    assert.ok(err instanceof QrResolveError);
    assert.equal(err.kind, "network");
    assert.equal(err.canRetry, true);
    return true;
  });
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
