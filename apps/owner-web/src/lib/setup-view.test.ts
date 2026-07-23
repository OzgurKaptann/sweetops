/**
 * The store-setup screen's presentation logic.
 *
 * The owner-web suite is `node --test` over pure TypeScript — there is no
 * renderer — so what is tested here is the layer the screen renders FROM. That is
 * the layer worth testing anyway: every rule about what a manager is told lives in
 * these functions, and none of them lives in JSX.
 *
 * Two properties dominate:
 *
 *   * **No raw wire value reaches a row.** A `MenuRow` carries Turkish strings and
 *     no enum; a `TableRow` carries a non-secret prefix and no token.
 *   * **The empty menu is explained, not merely reported.** The whole point of
 *     this screen is that a guest's blank phone and a correctly fail-closed branch
 *     look identical, and only the shop side can tell them apart.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/setup-view.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type {
  MenuProductItem,
  SetupStatus,
  TableItem,
} from "./setup-api.ts";
import {
  EMPTY_PRODUCT_FORM,
  SETUP_COPY,
  buildProductCreateBody,
  catalogRows,
  checklistRows,
  confirmationFor,
  emptyMenuExplanation,
  formatPrice,
  menuStateFor,
  publishedRows,
  readinessSummary,
  tableRows,
  toMenuRow,
  toTableRow,
} from "./setup-view.ts";

// ── Fixtures ─────────────────────────────────────────────────────────────────

function product(over: Partial<MenuProductItem> = {}): MenuProductItem {
  return {
    product_id: 1,
    name: "Fıstıklı Waffle",
    category: "Waffle",
    base_price: "120.00",
    is_active: true,
    published: true,
    is_available: true,
    sort_order: 0,
    published_at: "2026-07-20T09:00:00Z",
    on_customer_menu: true,
    ...over,
  };
}

function status(over: Partial<SetupStatus> = {}): SetupStatus {
  return {
    store_id: 1,
    store_name: "Kadıköy",
    catalog_active_products: 6,
    tables_total: 3,
    tables_with_active_qr: 3,
    published_products: 4,
    available_products: 4,
    menu_products: 4,
    ready_for_customer_orders: true,
    checks: [
      { key: "has_table", done: true, count: 3, label: "Masa", detail: "3 masa tanımlı." },
      { key: "has_table_qr", done: true, count: 3, label: "QR", detail: "Hepsi hazır." },
      {
        key: "has_published_product",
        done: true,
        count: 4,
        label: "Menü",
        detail: "4 ürün yayında.",
      },
      { key: "menu_ready", done: true, count: 4, label: "Sipariş", detail: "Görünüyor." },
    ],
    ...over,
  };
}

function table(over: Partial<TableItem> = {}): TableItem {
  return {
    table_id: 7,
    store_id: 1,
    table_number: "3",
    display_name: "Masa 3",
    has_active_qr: true,
    token_prefix: "aB3xY9zQ",
    qr_created_at: "2026-07-20T09:00:00Z",
    qr_last_used_at: "2026-07-22T18:41:00Z",
    ...over,
  };
}

// ── Menu state ───────────────────────────────────────────────────────────────

test("a published, available, active product is on the menu", () => {
  assert.equal(menuStateFor(product()), "on_menu");
});

test("an unpublished product reads as not-on-menu, whatever its catalog flags", () => {
  // The distinction that matters after the fail-closed migration: a product
  // nobody published is not "retired", and telling a manager to reactivate it
  // would send them to the wrong control entirely.
  assert.equal(
    menuStateFor(product({ published: false, is_available: null, is_active: true })),
    "not_on_menu",
  );
  assert.equal(
    menuStateFor(product({ published: false, is_available: null, is_active: false })),
    "not_on_menu",
  );
});

test("a retired product reads as retired even while published and available", () => {
  // products.is_active is chain-wide and publication cannot override it — the
  // row is still there and the guest still cannot see it.
  assert.equal(
    menuStateFor(product({ published: true, is_available: true, is_active: false })),
    "retired",
  );
});

test("a published product switched off for the day reads as sold out", () => {
  assert.equal(
    menuStateFor(product({ is_available: false })),
    "sold_out",
  );
});

test("every menu row carries Turkish status text and no raw wire value", () => {
  const rows = [
    toMenuRow(product()),
    toMenuRow(product({ is_available: false })),
    toMenuRow(product({ is_active: false })),
    toMenuRow(product({ published: false, is_available: null, sort_order: null })),
  ];
  for (const row of rows) {
    assert.ok(row.stateLabel.length > 0);
    assert.ok(row.stateDetail.length > 0);
    // No English enum leaks into anything the screen prints.
    assert.ok(!/on_menu|sold_out|not_on_menu|retired|true|false/.test(row.stateLabel));
    assert.ok(!/on_menu|sold_out|not_on_menu|retired/.test(row.stateDetail));
  }
});

test("a nameless or price-less product still renders something safe", () => {
  const row = toMenuRow(
    product({ name: null, category: null, base_price: null, sort_order: null }),
  );
  assert.equal(row.name, "İsimsiz ürün");
  assert.equal(row.category, "—");
  assert.equal(row.price, "—");
  assert.equal(row.sortOrderLabel, "—");
});

test("published and catalog rows partition the list", () => {
  const items = [
    product({ product_id: 1, published: true }),
    product({ product_id: 2, published: false, is_available: null }),
    product({ product_id: 3, published: true, is_available: false }),
  ];
  assert.deepEqual(publishedRows(items).map((r) => r.productId), [1, 3]);
  assert.deepEqual(catalogRows(items).map((r) => r.productId), [2]);
});

test("prices are formatted as Turkish lira, never as a bare number", () => {
  const formatted = formatPrice("120.00");
  assert.ok(formatted.includes("120"));
  assert.ok(formatted.includes("₺") || formatted.includes("TRY"));
  assert.equal(formatPrice(null), "—");
  assert.equal(formatPrice("not-a-number"), "—");
});

// ── Readiness checklist ──────────────────────────────────────────────────────

test("the checklist renders one row per server check, with Turkish status words", () => {
  const summary = readinessSummary(status());
  assert.equal(summary.rows.length, 4);
  assert.deepEqual(
    summary.rows.map((r) => r.key),
    ["has_table", "has_table_qr", "has_published_product", "menu_ready"],
  );
  for (const row of summary.rows) {
    assert.ok(row.statusLabel === "Tamam" || row.statusLabel === "Eksik");
  }
  assert.equal(summary.ready, true);
  assert.equal(summary.title, SETUP_COPY.readyTitle);
  assert.equal(summary.progressLabel, "4/4 adım tamam");
});

test("a brand-new branch shows every step as missing and says it is not ready", () => {
  const summary = readinessSummary(
    status({
      tables_total: 0,
      tables_with_active_qr: 0,
      published_products: 0,
      available_products: 0,
      menu_products: 0,
      ready_for_customer_orders: false,
      checks: [
        { key: "has_table", done: false, count: 0, label: "Masa", detail: "Masa ekleyin." },
        { key: "has_table_qr", done: false, count: 0, label: "QR", detail: "QR yok." },
        {
          key: "has_published_product",
          done: false,
          count: 0,
          label: "Menü",
          detail: "Ürün ekleyin.",
        },
        { key: "menu_ready", done: false, count: 0, label: "Sipariş", detail: "Boş." },
      ],
    }),
  );
  assert.equal(summary.ready, false);
  assert.equal(summary.title, SETUP_COPY.notReadyTitle);
  assert.equal(summary.doneCount, 0);
  assert.equal(summary.progressLabel, "0/4 adım tamam");
  assert.ok(summary.rows.every((r) => r.statusLabel === "Eksik"));
});

test("the screen trusts the server's readiness verdict, not its own tally", () => {
  // Every row done, but the server says not ready. The server owns the rule about
  // which checks are load-bearing; a screen that disagreed with the guest's phone
  // would be worse than one that shows nothing.
  const summary = readinessSummary(status({ ready_for_customer_orders: false }));
  assert.equal(summary.doneCount, 4);
  assert.equal(summary.ready, false);
  assert.equal(summary.title, SETUP_COPY.notReadyTitle);
});

test("an unrecognised check is rendered, not silently dropped", () => {
  // A checklist that hides the check it does not know about hides exactly the
  // step nobody has thought about yet.
  const rows = checklistRows([
    { key: "brand_new_rule", done: false, count: 0, label: "Yeni adım", detail: "" },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].key, "brand_new_rule");
  assert.equal(rows[0].statusLabel, "Eksik");
});

test("a missing status yields an empty checklist rather than throwing", () => {
  const summary = readinessSummary(null);
  assert.deepEqual(summary.rows, []);
  assert.equal(summary.ready, false);
  assert.equal(checklistRows(undefined).length, 0);
});

// ── The empty-menu explanation ───────────────────────────────────────────────

test("nothing published: the explanation names the missing step and the catalog size", () => {
  const text = emptyMenuExplanation(
    status({ published_products: 0, available_products: 0, menu_products: 0 }),
  );
  assert.ok(text);
  assert.ok(text!.includes("6"), "should name how many catalog products exist");
  assert.ok(text!.includes("menüye ekleyin"));
});

test("published but all switched off: a different explanation, because the fix differs", () => {
  const text = emptyMenuExplanation(
    status({ published_products: 4, available_products: 0, menu_products: 0 }),
  );
  assert.ok(text);
  assert.ok(text!.includes("4"));
  assert.ok(text!.includes("kapalı") || text!.includes("pasif"));
  // And it must NOT tell the manager to publish something — they already did.
  assert.ok(!text!.includes("menüye ekleyin"));
});

test("a healthy menu gets no explanation at all", () => {
  assert.equal(emptyMenuExplanation(status()), null);
  assert.equal(emptyMenuExplanation(null), null);
});

// ── Tables ───────────────────────────────────────────────────────────────────

test("a table row shows its QR state in words and only the non-secret prefix", () => {
  const row = toTableRow(table());
  assert.equal(row.displayName, "Masa 3");
  assert.equal(row.hasQr, true);
  assert.equal(row.qrStatusLabel, "QR kodu hazır");
  assert.equal(row.tokenPrefix, "aB3xY9zQ");
  // There is no field on a row that could carry a scannable link.
  assert.ok(!("qrUrl" in row));
  assert.ok(!Object.keys(row).some((k) => /token$|rawToken|url/i.test(k)));
});

test("a table with no sticker says so and tells the manager what to do", () => {
  const row = toTableRow(
    table({ has_active_qr: false, token_prefix: null, qr_created_at: null, qr_last_used_at: null }),
  );
  assert.equal(row.hasQr, false);
  assert.equal(row.qrStatusLabel, "QR kodu yok");
  assert.ok(row.qrDetail.includes("QR kodu oluşturun"));
  assert.equal(row.tokenPrefix, "—");
  assert.equal(row.createdAt, "—");
  assert.equal(row.lastUsedAt, "—");
});

test("an empty table list renders no rows and has a copy line to show instead", () => {
  assert.deepEqual(tableRows([]), []);
  assert.deepEqual(tableRows(undefined), []);
  assert.ok(SETUP_COPY.emptyTables.includes("Masa ekleyin"));
});

// ── Product create form ──────────────────────────────────────────────────────

test("the form builds exactly the API's body and never publishes by default", () => {
  const result = buildProductCreateBody({
    ...EMPTY_PRODUCT_FORM,
    name: "  Muzlu Waffle  ",
    category: " Waffle ",
    price: "129,90",
  });
  assert.equal(result.ok, true);
  assert.deepEqual(result.body, {
    name: "Muzlu Waffle",
    category: "Waffle",
    // Turkish comma input normalised to the decimal string the API expects, and
    // never routed through a float on the way.
    base_price: "129.90",
    is_active: true,
    publish_to_current_store: false,
  });
  // No store_id anywhere: the branch comes from the session, and the backend
  // rejects a smuggled one with a 422 rather than ignoring it.
  assert.ok(!("store_id" in result.body!));
});

test("publishing on create is opt-in and travels as an explicit flag", () => {
  const result = buildProductCreateBody({
    ...EMPTY_PRODUCT_FORM,
    name: "Çilekli Waffle",
    price: "140",
    publishToCurrentStore: true,
  });
  assert.equal(result.body!.publish_to_current_store, true);
  assert.equal(result.body!.base_price, "140");
});

test("an empty name or a bad price is refused before the round-trip", () => {
  const noName = buildProductCreateBody({ ...EMPTY_PRODUCT_FORM, name: "   ", price: "10" });
  assert.equal(noName.ok, false);
  assert.equal(noName.body, null);
  assert.ok(noName.error!.includes("Ürün adı"));

  const noPrice = buildProductCreateBody({ ...EMPTY_PRODUCT_FORM, name: "X", price: "  " });
  assert.equal(noPrice.ok, false);
  assert.ok(noPrice.error!.includes("fiyat"));

  for (const bad of ["abc", "-5", "12,345", "1.2.3", "₺120"]) {
    const r = buildProductCreateBody({ ...EMPTY_PRODUCT_FORM, name: "X", price: bad });
    assert.equal(r.ok, false, `price "${bad}" should be refused`);
    assert.equal(r.body, null);
  }

  const zero = buildProductCreateBody({ ...EMPTY_PRODUCT_FORM, name: "X", price: "0" });
  assert.equal(zero.ok, false);
  assert.ok(zero.error!.includes("sıfırdan büyük"));
});

// ── Dangerous actions ────────────────────────────────────────────────────────

test("each destructive action has its own confirmation naming the consequence", () => {
  const unpublish = confirmationFor("unpublish");
  const deactivate = confirmationFor("deactivate");
  const rotate = confirmationFor("rotate_qr");

  // Three different consequences; three different sentences.
  assert.notEqual(unpublish, deactivate);
  assert.notEqual(deactivate, rotate);

  // Unpublish is branch-scoped and must say so, or a manager will fear it is a delete.
  assert.ok(unpublish.includes("şube"));
  assert.ok(unpublish.includes("silinmez"));
  // Deactivation is chain-wide and must say THAT, or a manager will use it as a
  // per-branch control.
  assert.ok(deactivate.includes("TÜM şubelerde"));
  // Rotation invalidates a printed sticker; the copy has to mention the printed code.
  assert.ok(rotate.includes("geçersiz"));
  assert.ok(rotate.includes("bastırıp") || rotate.includes("basılı"));
});

test("the one-time QR warning is stated in the shared copy", () => {
  assert.ok(SETUP_COPY.qrShownOnceWarning.includes("yalnızca"));
});

test("empty states tell the manager what to do next, not merely that a list is empty", () => {
  assert.ok(SETUP_COPY.emptyMenu.includes("ürün ekleyin"));
  assert.ok(SETUP_COPY.emptyCatalog.includes("ürün oluşturun"));
  assert.ok(SETUP_COPY.emptyTables.includes("Masa ekleyin"));
});
