/**
 * Store-setup API client — what actually goes on the wire.
 *
 * The properties under test:
 *
 *   * **Every mutation carries the CSRF header and the session cookie.** These
 *     endpoints decide what a guest can order and whether a printed sticker still
 *     works; they are state changes and the backend refuses them without both.
 *   * **No request ever names a store.** The branch comes from the session, so a
 *     `store_id` in a payload is not merely useless — the backend rejects the whole
 *     request with a 422. The client must not be the thing that puts one there.
 *   * **Each control calls the endpoint it claims to.** A publish toggle wired to
 *     the unpublish route would pass every view-layer test and take a shop's menu
 *     down.
 *
 * `fetch` is stubbed on globalThis; no network access occurs. Excluded from the
 * Next production build via tsconfig `exclude`.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/setup-api.test.ts
 */
import { test, afterEach, beforeEach } from "node:test";
import assert from "node:assert/strict";

import {
  SetupApiError,
  SetupNetworkUncertainError,
  createProduct,
  createTable,
  fetchMenuProducts,
  fetchSetupStatus,
  fetchTables,
  issueTableQr,
  publishProduct,
  renameTable,
  rotateTableQr,
  setProductAvailability,
  setProductSortOrder,
  unpublishProduct,
  updateProduct,
} from "./setup-api.ts";

const originalFetch = globalThis.fetch;

interface Captured {
  url: string;
  method?: string;
  headers: Record<string, string>;
  body: unknown;
  credentials?: string;
}

let captured: Captured | undefined;

function stubOk(json: unknown = { changed: true }): void {
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    captured = {
      url,
      method: init?.method,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(init.body as string) : undefined,
      credentials: init?.credentials,
    };
    return { ok: true, status: 200, json: async () => json } as Response;
  }) as typeof fetch;
}

function stubError(status: number, error: string, message: string): void {
  globalThis.fetch = (async () =>
    ({
      ok: false,
      status,
      json: async () => ({ detail: { error, message } }),
    }) as Response) as typeof fetch;
}

function seen(): Captured {
  assert.ok(captured, "fetch was never called");
  return captured!;
}

beforeEach(() => {
  captured = undefined;
  // A readable CSRF cookie, exactly as the browser would hold it. `csrfHeaders()`
  // reads `document.cookie`, so the test provides a minimal document.
  (globalThis as { document?: { cookie: string } }).document = {
    cookie: "sweetops_csrf=csrf-token-value",
  };
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  delete (globalThis as { document?: unknown }).document;
});

// ── Reads ────────────────────────────────────────────────────────────────────

test("reads hit the right paths, send credentials, and bypass the cache", async () => {
  for (const [call, path] of [
    [fetchSetupStatus, "/owner/setup/status"],
    [fetchMenuProducts, "/owner/menu/products"],
    [fetchTables, "/owner/tables"],
  ] as const) {
    stubOk({});
    await call();
    const req = seen();
    assert.ok(req.url.endsWith(path), `${req.url} should end with ${path}`);
    assert.equal(req.credentials, "include");
    // A cached setup status would tell an owner their menu is still empty after
    // they fixed it.
    assert.ok(!req.url.includes("store_id"));
  }
});

test("no read carries a store parameter", async () => {
  stubOk({});
  await fetchMenuProducts();
  assert.ok(!seen().url.includes("store"));
});

// ── Mutations: headers ───────────────────────────────────────────────────────

test("every mutation sends the CSRF header and the session cookie", async () => {
  const mutations: Array<() => Promise<unknown>> = [
    () => createProduct({ name: "X", base_price: "10" }),
    () => updateProduct(1, { name: "Y" }),
    () => publishProduct(1),
    () => unpublishProduct(1),
    () => setProductAvailability(1, false),
    () => setProductSortOrder(1, 2),
    () => createTable({ table_number: "1" }),
    () => renameTable(1, "2"),
    () => issueTableQr(1),
    () => rotateTableQr(1),
  ];

  for (const run of mutations) {
    stubOk({});
    await run();
    const req = seen();
    assert.equal(
      req.headers["X-CSRF-Token"],
      "csrf-token-value",
      `${req.method} ${req.url} sent no CSRF token`,
    );
    assert.equal(req.credentials, "include");
    assert.ok(req.method === "POST" || req.method === "PATCH");
  }
});

test("no mutation body ever carries a store_id", async () => {
  const mutations: Array<() => Promise<unknown>> = [
    () => createProduct({ name: "X", base_price: "10", publish_to_current_store: true }),
    () => updateProduct(1, { is_active: false }),
    () => setProductAvailability(1, true),
    () => setProductSortOrder(1, 3),
    () => createTable({ table_number: "5" }),
    () => renameTable(1, "6"),
  ];

  for (const run of mutations) {
    stubOk({});
    await run();
    const body = seen().body as Record<string, unknown> | undefined;
    if (body) {
      assert.ok(!("store_id" in body), `${seen().url} smuggled a store_id`);
    }
  }
});

test("verb-only routes send no body at all", async () => {
  // Publish, unpublish and rotate carry no payload. Sending `{}` would invite
  // somebody to later put a store_id in it.
  for (const run of [
    () => publishProduct(9),
    () => unpublishProduct(9),
    () => rotateTableQr(9),
    () => issueTableQr(9),
  ]) {
    stubOk({});
    await run();
    assert.equal(seen().body, undefined);
  }
});

// ── Mutations: the right endpoint for the right control ──────────────────────

test("publish and unpublish hit their own distinct endpoints", async () => {
  stubOk({});
  await publishProduct(42);
  assert.ok(seen().url.endsWith("/owner/menu/products/42/publish"));
  assert.equal(seen().method, "POST");

  stubOk({});
  await unpublishProduct(42);
  assert.ok(seen().url.endsWith("/owner/menu/products/42/unpublish"));
});

test("the availability toggle sends the state it is switching TO", async () => {
  stubOk({});
  await setProductAvailability(7, false);
  assert.ok(seen().url.endsWith("/owner/menu/products/7/availability"));
  assert.equal(seen().method, "PATCH");
  assert.deepEqual(seen().body, { is_available: false });

  stubOk({});
  await setProductAvailability(7, true);
  assert.deepEqual(seen().body, { is_available: true });
});

test("sort order is sent as a number on its own endpoint", async () => {
  stubOk({});
  await setProductSortOrder(7, 4);
  assert.ok(seen().url.endsWith("/owner/menu/products/7/sort-order"));
  assert.deepEqual(seen().body, { sort_order: 4 });
});

test("create sends exactly the product fields, with the publish flag as given", async () => {
  stubOk({});
  await createProduct({
    name: "Muzlu Waffle",
    category: "Waffle",
    base_price: "129.90",
    is_active: true,
    publish_to_current_store: true,
  });
  const req = seen();
  assert.ok(req.url.endsWith("/owner/menu/products"));
  assert.equal(req.method, "POST");
  assert.deepEqual(req.body, {
    name: "Muzlu Waffle",
    category: "Waffle",
    base_price: "129.90",
    is_active: true,
    publish_to_current_store: true,
  });
});

test("a partial product edit sends only the fields that changed", async () => {
  // A genuine patch: the server leaves omitted fields alone, so a manager
  // renaming an item has not thereby made a decision about its price.
  stubOk({});
  await updateProduct(3, { name: "Yeni ad" });
  assert.deepEqual(seen().body, { name: "Yeni ad" });

  stubOk({});
  await updateProduct(3, { is_active: false });
  assert.deepEqual(seen().body, { is_active: false });
});

test("table create and rotate hit the table routes", async () => {
  stubOk({});
  await createTable({ table_number: "12", issue_qr: true });
  assert.ok(seen().url.endsWith("/owner/tables"));
  assert.deepEqual(seen().body, { table_number: "12", issue_qr: true });

  stubOk({});
  await rotateTableQr(12);
  assert.ok(seen().url.endsWith("/owner/tables/12/rotate-qr"));

  stubOk({});
  await issueTableQr(12);
  assert.ok(seen().url.endsWith("/owner/tables/12/qr-token"));

  stubOk({});
  await renameTable(12, "Teras 1");
  assert.ok(seen().url.endsWith("/owner/tables/12"));
  assert.deepEqual(seen().body, { table_number: "Teras 1" });
});

// ── Errors ───────────────────────────────────────────────────────────────────

test("an API failure surfaces the backend code and Turkish message", async () => {
  stubError(409, "not_published", "Bu ürün şubenizin menüsünde yayında değil.");
  await assert.rejects(
    () => setProductAvailability(1, true),
    (err: unknown) => {
      assert.ok(err instanceof SetupApiError);
      assert.equal((err as SetupApiError).code, "not_published");
      assert.equal((err as SetupApiError).status, 409);
      return true;
    },
  );
});

test("a dropped connection on a mutation is UNCERTAIN, never a plain failure", async () => {
  // The create may well have succeeded. Reporting "failed" is what makes a
  // manager type the product in again and end up with two.
  globalThis.fetch = (async () => {
    throw new TypeError("network down");
  }) as typeof fetch;
  await assert.rejects(
    () => createProduct({ name: "X", base_price: "10" }),
    (err: unknown) => err instanceof SetupNetworkUncertainError,
  );
});

test("a dropped connection on a READ is a plain error — it changed nothing", async () => {
  globalThis.fetch = (async () => {
    throw new TypeError("network down");
  }) as typeof fetch;
  await assert.rejects(
    () => fetchSetupStatus(),
    (err: unknown) =>
      err instanceof SetupApiError && (err as SetupApiError).code === "network_error",
  );
});

test("a non-JSON error body never leaks into the error message", async () => {
  // A proxy 502 is HTML. Its text must not reach a manager's screen.
  globalThis.fetch = (async () =>
    ({
      ok: false,
      status: 502,
      json: async () => {
        throw new SyntaxError("Unexpected token <");
      },
    }) as unknown as Response) as typeof fetch;

  await assert.rejects(
    () => publishProduct(1),
    (err: unknown) => {
      assert.ok(err instanceof SetupApiError);
      assert.equal((err as SetupApiError).code, "unknown");
      assert.equal((err as SetupApiError).message, "");
      return true;
    },
  );
});
