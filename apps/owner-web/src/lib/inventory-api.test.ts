/**
 * Inventory API client — the headers that make a stock command safe to retry.
 *
 * The property under test: EVERY state-changing inventory call carries an
 * `Idempotency-Key`. Without it the backend refuses (`idempotency_required`), and
 * worse, a client that could send one without it would have no defence against the
 * one failure mode that matters here — a manager pressing "Fire kaydet" twice, or a
 * retry after a timeout, binning the same 2 kg of pistachio twice. The ledger would
 * record two losses, and nothing downstream could tell them apart from two real ones.
 *
 * `fetch` is stubbed on globalThis; no network access occurs. Excluded from the
 * Next production build via tsconfig `exclude`.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/inventory-api.test.ts
 */
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";

import {
  InventoryApiError,
  InventoryNetworkUncertainError,
  createManualAdjustment,
  createPurchaseReceipt,
  createTransfer,
  createWaste,
  fetchStock,
} from "./inventory-api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

interface Captured {
  url: string;
  method?: string;
  headers: Record<string, string>;
  body: unknown;
}

/** Stub `fetch` with a 200 and capture what the client sent. */
function captureOk(json: unknown = { idempotent_replay: false }): () => Captured {
  let captured: Captured | undefined;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    captured = {
      url,
      method: init?.method,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    };
    return { ok: true, status: 200, json: async () => json } as Response;
  }) as typeof fetch;
  return () => {
    assert.ok(captured, "fetch was never called");
    return captured;
  };
}

/** Stub `fetch` with an API error carrying the backend's {error, message} detail. */
function captureError(status: number, error: string, message: string): void {
  globalThis.fetch = (async () =>
    ({
      ok: false,
      status,
      json: async () => ({ detail: { error, message } }),
    }) as Response) as typeof fetch;
}

// ═══════════════════════════════════════════════════════════════════════════
// Idempotency-Key — one per state-changing endpoint
// ═══════════════════════════════════════════════════════════════════════════

test("purchase receipt sends the Idempotency-Key header", async () => {
  const seen = captureOk();
  await createPurchaseReceipt(
    { ingredient_id: 1, quantity: "5.000", reason: "Tedarikçi teslimatı" },
    "key-purchase-1",
  );

  const req = seen();
  assert.equal(req.headers["Idempotency-Key"], "key-purchase-1");
  assert.equal(req.headers["Content-Type"], "application/json");
  assert.equal(req.method, "POST");
  assert.match(req.url, /\/inventory\/purchase-receipts$/);
});

test("waste sends the Idempotency-Key header", async () => {
  const seen = captureOk();
  await createWaste(
    { ingredient_id: 1, quantity: "2.000", reason: "Yanan hamur" },
    "key-waste-1",
  );

  const req = seen();
  assert.equal(req.headers["Idempotency-Key"], "key-waste-1");
  assert.match(req.url, /\/inventory\/waste$/);
});

test("manual adjustment sends the Idempotency-Key header", async () => {
  const seen = captureOk();
  await createManualAdjustment(
    { ingredient_id: 1, delta: "-3.000", reason: "Sayım farkı" },
    "key-adjust-1",
  );

  const req = seen();
  assert.equal(req.headers["Idempotency-Key"], "key-adjust-1");
  assert.match(req.url, /\/inventory\/manual-adjustments$/);
});

test("transfer sends the Idempotency-Key header", async () => {
  const seen = captureOk();
  await createTransfer(
    {
      destination_store_id: 2,
      ingredient_id: 1,
      quantity: "10.000",
      reason: "Beşiktaş şubesine takviye",
      note: null,
    },
    "key-transfer-1",
  );

  const req = seen();
  assert.equal(req.headers["Idempotency-Key"], "key-transfer-1");
  assert.match(req.url, /\/inventory\/transfers$/);
});

test("a mutation with no key is refused locally, before the request is sent", async () => {
  // A stock command that reaches the network without a key cannot be safely
  // retried, and the caller has skipped the idempotency policy. Refuse it here
  // rather than send it and hope.
  let called = false;
  globalThis.fetch = (async () => {
    called = true;
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  }) as typeof fetch;

  await assert.rejects(
    () => createWaste({ ingredient_id: 1, quantity: "1.000", reason: "x" }, ""),
    (err: unknown) =>
      err instanceof InventoryApiError && err.code === "idempotency_required",
  );
  assert.equal(called, false, "no request should have left the client");
});

// ═══════════════════════════════════════════════════════════════════════════
// Session / CSRF
// ═══════════════════════════════════════════════════════════════════════════

test("every call sends cookies and is never cached", async () => {
  // The session is an HttpOnly cookie; it only travels if credentials are included.
  // `no-store` keeps a stale stock figure from being served out of the HTTP cache.
  let init: RequestInit | undefined;
  globalThis.fetch = (async (_url: string, i: RequestInit) => {
    init = i;
    return { ok: true, status: 200, json: async () => ({ total: 0, items: [] }) } as Response;
  }) as typeof fetch;

  await fetchStock();
  assert.equal(init?.credentials, "include");
  assert.equal(init?.cache, "no-store");
});

test("the source store is never sent — it comes from the session", async () => {
  // A body that could name a store would be a body that could ship another
  // branch's stock. There is no such field, and this pins that.
  const seen = captureOk();
  await createTransfer(
    {
      destination_store_id: 2,
      ingredient_id: 1,
      quantity: "10.000",
      reason: "Takviye",
      note: null,
    },
    "key-1",
  );

  const body = seen().body as Record<string, unknown>;
  assert.equal("source_store_id" in body, false);
  assert.equal("store_id" in body, false);
  assert.equal("actor_user_id" in body, false);
});

// ═══════════════════════════════════════════════════════════════════════════
// Failure shapes
// ═══════════════════════════════════════════════════════════════════════════

test("an API refusal surfaces the backend's stable error code", async () => {
  captureError(409, "insufficient_available", "Gönderen şubede yeterli kullanılabilir stok yok.");

  await assert.rejects(
    () =>
      createTransfer(
        { destination_store_id: 2, ingredient_id: 1, quantity: "999", reason: "x" },
        "key-1",
      ),
    (err: unknown) =>
      err instanceof InventoryApiError &&
      err.code === "insufficient_available" &&
      err.status === 409,
  );
});

test("a mutation that never got an answer is uncertain, not failed", async () => {
  // The request left the browser and nothing came back. The stock may well have
  // moved. Reporting this as a failure is what makes a manager re-key the form by
  // hand — which mints a NEW key and genuinely doubles the movement.
  globalThis.fetch = (async () => {
    throw new TypeError("network error");
  }) as typeof fetch;

  await assert.rejects(
    () => createWaste({ ingredient_id: 1, quantity: "2.000", reason: "Yanan hamur" }, "key-1"),
    (err: unknown) => err instanceof InventoryNetworkUncertainError,
  );
});

test("a failed READ is a plain error — it changed nothing", async () => {
  globalThis.fetch = (async () => {
    throw new TypeError("network error");
  }) as typeof fetch;

  await assert.rejects(
    () => fetchStock(),
    (err: unknown) => err instanceof InventoryApiError && err.code === "network_error",
  );
});
