/**
 * The physical stock count, as a manager experiences it.
 *
 * The count form is the only one in this app that asks what IS on the shelf rather
 * than what CHANGED, and nearly every test here exists because that difference is
 * easy to get subtly wrong in ways that produce a plausible number rather than a
 * crash:
 *
 *   * A count of 0 must be ACCEPTED. An empty freezer is a valid count — and the
 *     one a manager most needs to report. Reusing the "must be > 0" rule from the
 *     other forms would make "there is none left" impossible to say.
 *   * A count BELOW reserved must be blocked, with the reason. It does not mean the
 *     system was wrong; it means the shop has sold stock it does not have.
 *   * A zero-delta count is a SUCCESS, not a failure and not a replay. The shelf was
 *     checked and found correct — which is exactly what a count is for.
 *   * A network-uncertain count must NOT read as a failure. A manager who reads
 *     "başarısız" re-enters the form by hand, which mints a new idempotency key and
 *     genuinely doubles the correction.
 *   * STOCK_COUNT_ADJUSTMENT must render as "Sayım düzeltmesi" — never as the raw
 *     enum, and never as "Manuel düzeltme", which would hide shrinkage inside a
 *     label meaning "we decided to change this number".
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/inventory-stock-count.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { MOVEMENT_TYPE_LABEL, movementTypeLabel } from "./labels.ts";
import {
  InventoryApiError,
  InventoryNetworkUncertainError,
} from "./inventory-api.ts";
import {
  INVENTORY_ERROR_MESSAGE,
  INVENTORY_ERROR_NETWORK_UNCERTAIN,
  STOCK_COUNT_ERROR_NETWORK_UNCERTAIN,
  inventoryErrorMessage,
  looksDisplaySafe,
} from "./inventory-errors.ts";
import { fingerprintCommand } from "./inventory-idempotency.ts";
import {
  INVENTORY_ACTIONS,
  OPERATION_TITLE,
  STOCK_COUNT_HINT,
  STOCK_COUNT_LABELS,
  STOCK_COUNT_NO_DELTA_MESSAGE,
  STOCK_COUNT_VALIDATION,
  TRANSFER_VALIDATION,
  expectedCountDelta,
  successBanner,
  toMovementRow,
  validateStockCountForm,
} from "./inventory-view.ts";

const shelf = {
  ingredientId: 7,
  reason: "Haftalik sayim",
  onHandQuantity: "10.000",
  reservedQuantity: "2.000",
};

// ── The action, on the inventory screen ──────────────────────────────────────

test("the inventory screen offers a physical count action", () => {
  const count = INVENTORY_ACTIONS.find((a) => a.kind === "stock_count");
  assert.ok(count, "the inventory screen must offer a physical count");
  assert.equal(count.label, "Sayım gir");
});

test("the count form is titled Fiziksel sayım", () => {
  assert.equal(OPERATION_TITLE.stock_count, "Fiziksel sayım");
});

test("the count sits beside the other stock operations, not hidden away", () => {
  // A count that is harder to reach than "Manuel düzeltme" is a count that gets
  // typed in AS a manual adjustment.
  const kinds = INVENTORY_ACTIONS.map((a) => a.kind);
  assert.ok(kinds.includes("stock_count"));
  assert.ok(
    kinds.indexOf("stock_count") < kinds.indexOf("manual_adjustment"),
    "the count must be at least as prominent as the manual adjustment",
  );
});

test("no action label is a raw enum", () => {
  for (const action of INVENTORY_ACTIONS) {
    assert.equal(action.label, action.label.trim());
    assert.equal(/^[A-Z_]+$/.test(action.label), false);
    assert.ok(looksDisplaySafe(action.label), `${action.label} is not display-safe`);
  }
  for (const title of Object.values(OPERATION_TITLE)) {
    assert.ok(looksDisplaySafe(title), `${title} is not display-safe`);
  }
});

// ── The movement label ───────────────────────────────────────────────────────

test("STOCK_COUNT_ADJUSTMENT renders as Sayım düzeltmesi", () => {
  assert.equal(movementTypeLabel("STOCK_COUNT_ADJUSTMENT"), "Sayım düzeltmesi");
  assert.equal(MOVEMENT_TYPE_LABEL.STOCK_COUNT_ADJUSTMENT, "Sayım düzeltmesi");
});

test("the raw enum never reaches a movement row", () => {
  const row = toMovementRow({
    id: 1,
    ingredient_name: "Çikolata",
    movement_type: "STOCK_COUNT_ADJUSTMENT",
    quantity: "0.750",
    quantity_delta_on_hand: "-0.750",
    quantity_delta_reserved: "0",
    unit: "kg",
    reason: "Haftalik sayim",
    actor_user_id: 3,
    order_id: null,
    created_at: "2026-07-14T09:00:00Z",
  });

  assert.equal(row.typeLabel, "Sayım düzeltmesi");
  assert.equal(JSON.stringify(row).includes("STOCK_COUNT_ADJUSTMENT"), false);
  // A count is emphatically not a manual adjustment, and must not borrow its label.
  assert.notEqual(row.typeLabel, "Manuel düzeltme");
  // The on-hand effect is signed and the reserved effect is flat zero.
  assert.equal(row.onHandEffect, "−0,75");
  assert.equal(row.reservedEffect, "0");
});

test("a count's label is distinct from every other movement type", () => {
  const labels = Object.values(MOVEMENT_TYPE_LABEL);
  const unique = new Set(labels);
  assert.equal(labels.length, unique.size, "two movement types share a label");
});

// ── The expected difference ──────────────────────────────────────────────────

test("expected difference is counted minus system on-hand", () => {
  assert.equal(expectedCountDelta("9.250", "10.000"), -0.75);
  assert.equal(expectedCountDelta("11.500", "10.000"), 1.5);
  assert.equal(expectedCountDelta("10.000", "10.000"), 0);
});

test("expected difference is rounded to the stored grain, not float noise", () => {
  // 9.25 - 10 is -0.7500000000000004 in IEEE 754. That must never reach the screen.
  const delta = expectedCountDelta("9.25", "10");
  assert.equal(delta, -0.75);
  assert.equal(String(delta).includes("0000"), false);
});

test("expected difference is null when it cannot honestly be computed", () => {
  assert.equal(expectedCountDelta("", "10.000"), null);
  assert.equal(expectedCountDelta("9.250", null), null);
  assert.equal(expectedCountDelta("9.250", undefined), null);
  assert.equal(expectedCountDelta("abc", "10.000"), null);
});

// ── Form validation ──────────────────────────────────────────────────────────

test("a valid count passes", () => {
  assert.deepEqual(validateStockCountForm({ ...shelf, counted: "9.250" }), []);
});

test("counting an EMPTY shelf is allowed when nothing is reserved", () => {
  const errors = validateStockCountForm({
    ingredientId: 7,
    counted: "0",
    reason: "Dolap bos",
    onHandQuantity: "10.000",
    reservedQuantity: "0",
  });
  assert.deepEqual(errors, []);
});

test("a count below RESERVED is blocked, with the reason", () => {
  const errors = validateStockCountForm({ ...shelf, counted: "1.000" }); // reserved 2
  assert.ok(errors.includes(STOCK_COUNT_VALIDATION.belowReserved));
  assert.equal(
    STOCK_COUNT_VALIDATION.belowReserved,
    "Sayım sonucu ayrılmış stoktan düşük olamaz.",
  );
});

test("a count EQUAL to reserved is allowed — the rule is >=, not >", () => {
  assert.deepEqual(validateStockCountForm({ ...shelf, counted: "2.000" }), []);
});

test("the below-reserved block is skipped when reserved is unknown", () => {
  // The server still enforces it. Guessing here would block a legitimate count.
  const errors = validateStockCountForm({
    ingredientId: 7,
    counted: "1.000",
    reason: "sayim",
    onHandQuantity: "10.000",
    reservedQuantity: null,
  });
  assert.deepEqual(errors, []);
});

test("a negative count is refused", () => {
  const errors = validateStockCountForm({ ...shelf, counted: "-1" });
  assert.ok(errors.includes(STOCK_COUNT_VALIDATION.countedNonNegative));
});

test("an empty count is refused", () => {
  const errors = validateStockCountForm({ ...shelf, counted: "  " });
  assert.ok(errors.includes(STOCK_COUNT_VALIDATION.countedRequired));
});

test("a reason is mandatory — an unexplained correction is indistinguishable from theft", () => {
  const errors = validateStockCountForm({ ...shelf, counted: "9.250", reason: "" });
  assert.ok(errors.includes(TRANSFER_VALIDATION.reasonRequired));
});

test("an ingredient must be chosen", () => {
  const errors = validateStockCountForm({
    ...shelf,
    ingredientId: null,
    counted: "9.250",
  });
  assert.ok(errors.includes(TRANSFER_VALIDATION.ingredientRequired));
});

// ── Result copy ──────────────────────────────────────────────────────────────

test("a successful count reports Turkish success copy", () => {
  const banner = successBanner("stock_count");
  assert.equal(banner.tone, "success");
  assert.equal(banner.message, "Sayım kaydı uygulandı.");
});

test("a ZERO-DELTA count is a success with its own message, not a failure", () => {
  const banner = successBanner("stock_count", { noDelta: true });
  assert.equal(banner.tone, "info");
  assert.equal(banner.message, "Sayım kaydedildi. Stok farkı oluşmadı.");
  assert.equal(banner.message, STOCK_COUNT_NO_DELTA_MESSAGE);
  // NOT the plain success line: a manager told "uygulandı" would go hunting for a
  // stock movement that does not exist and conclude the system lost it.
  assert.notEqual(banner.message, "Sayım kaydı uygulandı.");
});

test("a REPLAY is not a second success, and outranks no-delta", () => {
  const banner = successBanner("stock_count", { replay: true, noDelta: true });
  assert.equal(banner.tone, "info");
  assert.ok(banner.message.includes("daha önce"));
  assert.ok(banner.message.includes("yeniden düzeltilmedi"));
  assert.notEqual(banner.message, STOCK_COUNT_NO_DELTA_MESSAGE);
});

// ── Failure copy ─────────────────────────────────────────────────────────────

test("a network-uncertain count says so in Turkish, and does not invite a blind retry", () => {
  const message = inventoryErrorMessage(
    new InventoryNetworkUncertainError(),
    "stock_count",
  );
  assert.equal(
    message,
    "Sayım sonucunun kaydedilip kaydedilmediği doğrulanamadı. " +
      "Aynı işlemi tekrar göndermeden önce stok hareketlerini kontrol edin.",
  );
  assert.equal(message, STOCK_COUNT_ERROR_NETWORK_UNCERTAIN);
  // Emphatically NOT phrased as a failure — that is what makes a manager re-enter
  // the form by hand, minting a new key and doubling the correction.
  assert.equal(/başarısız/i.test(message), false);
  assert.ok(message.includes("stok hareketlerini kontrol edin"));
});

test("other operations keep the generic uncertain copy", () => {
  assert.equal(
    inventoryErrorMessage(new InventoryNetworkUncertainError(), "waste"),
    INVENTORY_ERROR_NETWORK_UNCERTAIN,
  );
  assert.equal(
    inventoryErrorMessage(new InventoryNetworkUncertainError()),
    INVENTORY_ERROR_NETWORK_UNCERTAIN,
  );
});

test("count below reserved shows Turkish copy that points at the ORDERS", () => {
  const message = inventoryErrorMessage(
    new InventoryApiError(409, "stock_count_below_reserved", "ignored"),
    "stock_count",
  );
  assert.equal(message, INVENTORY_ERROR_MESSAGE.stock_count_below_reserved);
  assert.ok(message.includes("ayrılmış stoktan düşük olamaz"));
  // The actionable half: the fix is in the order book, not in the count.
  assert.ok(message.includes("siparişleri kontrol edin"));
});

test("a missing count reads as not-found, never as a permission error", () => {
  const message = inventoryErrorMessage(
    new InventoryApiError(404, "stock_count_not_found", ""),
    "stock_count",
  );
  assert.equal(message, "Bu sayım kaydı bulunamadı.");
});

test("no count error copy leaks an internal", () => {
  for (const code of ["stock_count_below_reserved", "stock_count_not_found"]) {
    const message = INVENTORY_ERROR_MESSAGE[code];
    assert.ok(looksDisplaySafe(message), `${code} is not display-safe`);
    assert.equal(message.includes("STOCK_COUNT"), false);
    assert.equal(message.includes("_"), false);
  }
});

// ── Idempotency ──────────────────────────────────────────────────────────────

test("the same count is the same command — an unchanged retry reuses its key", () => {
  const a = fingerprintCommand({
    kind: "stock_count",
    ingredientId: 7,
    countedQuantity: "9.250",
    reason: "Haftalik sayim",
  });
  const b = fingerprintCommand({
    kind: "stock_count",
    ingredientId: 7,
    countedQuantity: "9.250",
    reason: "Haftalik sayim",
    note: null,
  });
  assert.equal(a, b);
});

test("a re-count mints a NEW fingerprint — replaying the old one would report the wrong figure", () => {
  const base = {
    kind: "stock_count" as const,
    ingredientId: 7,
    countedQuantity: "9.250",
    reason: "Haftalik sayim",
  };
  const first = fingerprintCommand(base);

  // Every field that changes what the backend persists must change the fingerprint.
  assert.notEqual(first, fingerprintCommand({ ...base, countedQuantity: "8.000" }));
  assert.notEqual(first, fingerprintCommand({ ...base, ingredientId: 8 }));
  assert.notEqual(first, fingerprintCommand({ ...base, reason: "Ay sonu sayimi" }));
  assert.notEqual(first, fingerprintCommand({ ...base, note: "dolap 2" }));
});

test("a count is a different command from a manual adjustment of the same size", () => {
  const count = fingerprintCommand({
    kind: "stock_count",
    ingredientId: 7,
    countedQuantity: "9.250",
    reason: "sayim",
  });
  const adjustment = fingerprintCommand({
    kind: "manual_adjustment",
    ingredientId: 7,
    delta: "9.250",
    reason: "sayim",
  });
  assert.notEqual(count, adjustment);
});

// ── The form's own copy ──────────────────────────────────────────────────────

test("the form explains what a count does — and that reserved stock is untouched", () => {
  assert.equal(
    STOCK_COUNT_HINT,
    "Bu işlem fiziksel stok miktarını sayım sonucuna göre düzeltir. " +
      "Ayrılmış stok değişmez.",
  );
  // The second sentence is the operational one: a manager who fears that counting
  // the freezer might cancel a waiting customer's waffle will not count the freezer.
  assert.ok(STOCK_COUNT_HINT.includes("Ayrılmış stok değişmez"));
});

test("the four figures a manager reconciles against are all labelled in Turkish", () => {
  assert.equal(STOCK_COUNT_LABELS.systemOnHand, "Sistemdeki fiziksel stok");
  assert.equal(STOCK_COUNT_LABELS.reserved, "Ayrılmış stok");
  assert.equal(STOCK_COUNT_LABELS.available, "Kullanılabilir stok");
  assert.equal(STOCK_COUNT_LABELS.expectedDelta, "Beklenen fark");

  for (const label of Object.values(STOCK_COUNT_LABELS)) {
    assert.ok(looksDisplaySafe(label), `${label} is not display-safe`);
  }
});
