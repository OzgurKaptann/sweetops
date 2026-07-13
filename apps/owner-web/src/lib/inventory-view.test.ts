/**
 * The inventory screen's presentation layer.
 *
 * Every cell the inventory tables render is built by the functions under test
 * here, so these are the tests that decide what a manager actually sees. Three
 * things are worth stating plainly, because each one is a real operational error
 * waiting to happen:
 *
 *   * "Stokta yok" and "Stok yetersiz" are DIFFERENT. The first means the shelf is
 *     empty. The second means the shelf is not empty, but every unit on it is
 *     already promised to an accepted order. A manager who reads "stokta yok" for
 *     the second case orders stock they already have.
 *   * A RAW ENUM must never reach a cell. `TRANSFER_OUT` in a movement table is an
 *     internal identifier handed to someone who has to interpret it under pressure.
 *   * A REPLAY is not a second success. Saying "kaydedildi" twice makes a manager
 *     believe two receipts exist.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/inventory-view.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { MOVEMENT_TYPE_LABEL } from "./labels.ts";
import {
  ADJUSTMENT_VALIDATION,
  INVENTORY_COPY,
  MANUAL_ADJUSTMENT_HINT,
  STOCK_STATUS_LABEL,
  TRANSFER_VALIDATION,
  formatDelta,
  formatQuantity,
  hasStockoutRisk,
  stockStatus,
  successBanner,
  toMovementRow,
  toStockRow,
  toTransferRow,
  transferDirectionLabel,
  validateAdjustmentForm,
  validateTransferForm,
  validateWasteForm,
} from "./inventory-view.ts";

// Every raw wire value that must never appear on screen.
const RAW_ENUMS = [
  "RESERVATION_CREATED",
  "RESERVATION_RELEASED",
  "CONSUMPTION",
  "WASTE",
  "RETURNED",
  "MANUAL_ADJUSTMENT",
  "PURCHASE_RECEIPT",
  "TRANSFER_OUT",
  "TRANSFER_IN",
  "OUTBOUND",
  "INBOUND",
];

function stock(over: Partial<Parameters<typeof toStockRow>[0]> = {}) {
  return {
    ingredient_id: 1,
    ingredient_name: "Çikolata",
    category: "Sos",
    unit: "kg",
    on_hand_quantity: "10.000",
    reserved_quantity: "0.000",
    available_quantity: "10.000",
    reorder_level: "5.000",
    ...over,
  };
}

function movement(over: Partial<Parameters<typeof toMovementRow>[0]> = {}) {
  return {
    id: 1,
    ingredient_name: "Çikolata",
    movement_type: "TRANSFER_OUT",
    quantity: "2.000",
    quantity_delta_on_hand: "-2.000",
    quantity_delta_reserved: "0.000",
    unit: "kg",
    reason: "Beşiktaş şubesine takviye",
    actor_user_id: 7,
    order_id: null,
    created_at: "2026-07-13T14:32:00Z",
    ...over,
  };
}

// ═══════════════════════════════════════════════════════════════════════════
// Stock status — the four states, and the one that is easy to get wrong
// ═══════════════════════════════════════════════════════════════════════════

test("an empty shelf is 'Stokta yok'", () => {
  const row = toStockRow(
    stock({ on_hand_quantity: "0.000", reserved_quantity: "0.000", available_quantity: "0.000" }),
  );
  assert.equal(row.status, "out");
  assert.equal(row.statusLabel, "Stokta yok");
});

test("stock that exists but is fully reserved is 'Stok yetersiz', NOT 'Stokta yok'", () => {
  // 8 kg on the shelf; all 8 promised to accepted orders. The branch HAS chocolate.
  // It just cannot spend it — and telling the manager the shelf is empty would send
  // them to a supplier they do not need.
  const row = toStockRow(
    stock({ on_hand_quantity: "8.000", reserved_quantity: "8.000", available_quantity: "0.000" }),
  );
  assert.equal(row.status, "insufficient");
  assert.equal(row.statusLabel, "Stok yetersiz");
  assert.notEqual(row.statusLabel, "Stokta yok");
});

test("available stock at or below the reorder level is 'Düşük stok'", () => {
  const row = toStockRow(
    stock({ on_hand_quantity: "5.000", available_quantity: "5.000", reorder_level: "5.000" }),
  );
  assert.equal(row.status, "low");
  assert.equal(row.statusLabel, "Düşük stok");
});

test("healthy stock is 'Stok yeterli' and carries no risk flag", () => {
  const row = toStockRow(stock());
  assert.equal(row.statusLabel, "Stok yeterli");
  assert.equal(row.atRisk, false);
  assert.equal(row.riskLabel, null);
});

test("out, insufficient and low all raise the stockout-risk flag", () => {
  assert.ok(hasStockoutRisk(stock({ on_hand_quantity: "0", available_quantity: "0" })));
  assert.ok(
    hasStockoutRisk(stock({ on_hand_quantity: "8", reserved_quantity: "8", available_quantity: "0" })),
  );
  assert.ok(hasStockoutRisk(stock({ available_quantity: "4", reorder_level: "5" })));
  assert.equal(hasStockoutRisk(stock()), false);

  const row = toStockRow(stock({ available_quantity: "4", reorder_level: "5" }));
  assert.equal(row.riskLabel, "Stok tükenme riski");
});

test("a row with reservations explains WHY its available stock is held down", () => {
  const row = toStockRow(stock({ reserved_quantity: "3.000", available_quantity: "7.000" }));
  assert.equal(row.reservedNote, "Ayrılan stok bekleyen siparişler için tutuluyor");

  // No reservations, no note — an explanation nobody needs is just noise.
  assert.equal(toStockRow(stock()).reservedNote, null);
});

test("a stock row shows physical, reserved and available separately", () => {
  const row = toStockRow(
    stock({ on_hand_quantity: "10.000", reserved_quantity: "3.000", available_quantity: "7.000" }),
  );
  assert.equal(row.onHand, "10");
  assert.equal(row.reserved, "3");
  assert.equal(row.available, "7");
  assert.equal(row.unit, "kg");
});

test("no stock status label is a raw enum", () => {
  for (const label of Object.values(STOCK_STATUS_LABEL)) {
    assert.doesNotMatch(label, /[A-Z][A-Z0-9]*_[A-Z0-9_]+/);
    assert.match(label, /[çğıöşüÇĞİÖŞÜ]|Stok/);
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// Movement rows — no raw enum reaches a cell
// ═══════════════════════════════════════════════════════════════════════════

test("a movement row renders the Turkish label, never the movement type", () => {
  const row = toMovementRow(movement({ movement_type: "TRANSFER_OUT" }));
  assert.equal(row.typeLabel, "Şubeden çıkış");
});

test("no cell of any movement row contains a raw enum value", () => {
  // The table renders exactly these fields. Sweep every movement type through the
  // row builder and assert that no cell — not the type, not the reason, not the
  // actor — carries a wire value.
  for (const type of Object.keys(MOVEMENT_TYPE_LABEL)) {
    const row = toMovementRow(movement({ movement_type: type }));
    const cells = [
      row.at,
      row.ingredientName,
      row.typeLabel,
      row.quantity,
      row.onHandEffect,
      row.reservedEffect,
      row.reason,
      row.actor,
    ];
    for (const cell of cells) {
      for (const raw of RAW_ENUMS) {
        assert.equal(cell.includes(raw), false, `"${raw}" leaked into a cell for ${type}`);
      }
    }
  }
});

test("an unrecognised movement type degrades to a safe phrase, never the raw value", () => {
  const row = toMovementRow(movement({ movement_type: "TRANSFER_OUT_PENDING" }));
  assert.equal(row.typeLabel, "Diğer stok hareketi");
  assert.equal(row.typeLabel.includes("TRANSFER"), false);
});

test("a movement's stock effects are signed and readable", () => {
  const out = toMovementRow(
    movement({ quantity_delta_on_hand: "-2.000", quantity_delta_reserved: "0.000" }),
  );
  assert.equal(out.onHandEffect, "−2"); // U+2212, unmistakable in a number column
  assert.equal(out.reservedEffect, "0");

  const inbound = toMovementRow(movement({ quantity_delta_on_hand: "2.500" }));
  assert.equal(inbound.onHandEffect, "+2,5");
});

test("a system-booked movement names the order instead of inventing a person", () => {
  // A reservation is booked by the system when an order is accepted; there is no
  // member of staff to attribute it to, and "Personel #null" would be a lie.
  const row = toMovementRow(
    movement({
      movement_type: "RESERVATION_CREATED",
      actor_user_id: null,
      reason: null,
      order_id: 512,
    }),
  );
  assert.equal(row.actor, "Sistem");
  assert.equal(row.reason, "512 numaralı sipariş");
  assert.equal(row.typeLabel, "Stok ayrıldı");
});

test("a movement for a deleted ingredient is still readable", () => {
  const row = toMovementRow(movement({ ingredient_name: null }));
  assert.equal(row.ingredientName, "Bilinmeyen malzeme");
});

// ═══════════════════════════════════════════════════════════════════════════
// Transfer rows
// ═══════════════════════════════════════════════════════════════════════════

test("transfer direction is Turkish, never OUTBOUND/INBOUND", () => {
  assert.equal(transferDirectionLabel("OUTBOUND"), "Şubeden çıkış");
  assert.equal(transferDirectionLabel("INBOUND"), "Şubeye giriş");
  assert.equal(transferDirectionLabel("SIDEWAYS"), "Bilinmiyor");
  assert.equal(transferDirectionLabel(null), "Bilinmiyor");
});

test("a transfer row carries no raw direction value", () => {
  const row = toTransferRow({
    transfer_id: 3,
    ingredient_name: "Çikolata",
    quantity: "2.000",
    unit: "kg",
    direction: "OUTBOUND",
    reason: "Beşiktaş şubesine takviye",
    note: null,
    created_at: "2026-07-13T14:32:00Z",
  });
  assert.equal(row.directionLabel, "Şubeden çıkış");
  assert.equal(row.outbound, true);
  assert.equal(row.quantity, "2 kg");
  for (const raw of RAW_ENUMS) {
    assert.equal(row.directionLabel.includes(raw), false);
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// Empty states
// ═══════════════════════════════════════════════════════════════════════════

test("an empty stock list is explained in Turkish, and promises nothing false", () => {
  assert.equal(INVENTORY_COPY.stockEmpty, "Bu şube için henüz stok tanımlanmamış.");

  // The hint must NOT say "record a purchase receipt to create your stock". Every
  // stock command acts on a row that already exists (the service 404s
  // `stock_not_configured` otherwise), so a branch with no rows cannot bootstrap
  // itself from this screen — and a manager told to try would be refused.
  assert.doesNotMatch(INVENTORY_COPY.stockEmptyHint, /Mal kabul kaydederek/i);
  assert.match(INVENTORY_COPY.stockEmptyHint, /stok tanımları oluşturulduktan sonra/i);
});

test("every empty/loading/permission string is Turkish and free of raw values", () => {
  for (const [key, copy] of Object.entries(INVENTORY_COPY)) {
    assert.doesNotMatch(copy, /[A-Z][A-Z0-9]*_[A-Z0-9_]+/, `${key} contains a raw enum`);
    assert.ok(copy.length > 0, `${key} is empty`);
  }
  assert.match(INVENTORY_COPY.movementsEmpty, /stok hareketi bulunmuyor/);
  assert.match(INVENTORY_COPY.forbidden, /yetkiniz yok/);
});

// ═══════════════════════════════════════════════════════════════════════════
// Operation result banners
// ═══════════════════════════════════════════════════════════════════════════

test("a completed transfer is reported in Turkish", () => {
  const banner = successBanner("transfer");
  assert.equal(banner.tone, "success");
  assert.equal(banner.message, "Transfer tamamlandı.");
});

test("each operation has its own success line", () => {
  assert.equal(successBanner("purchase_receipt").message, "Mal kabul başarıyla kaydedildi.");
  assert.equal(successBanner("waste").message, "Fire kaydı başarıyla oluşturuldu.");
  assert.equal(
    successBanner("manual_adjustment").message,
    "Manuel düzeltme başarıyla kaydedildi.",
  );
});

test("a replay is reported as a replay, never as a second success", () => {
  // The backend recognised the key and moved no further stock. Saying "kaydedildi"
  // again would leave the manager believing two receipts exist.
  const banner = successBanner("purchase_receipt", { replay: true });
  assert.equal(banner.tone, "info");
  assert.match(banner.message, /daha önce kaydedilmiş/);
  assert.match(banner.message, /Yeni bir kayıt oluşturulmadı/);
  assert.notEqual(banner.message, "Mal kabul başarıyla kaydedildi.");

  const transfer = successBanner("transfer", { replay: true });
  assert.match(transfer.message, /Stok yeniden gönderilmedi/);
});

// ═══════════════════════════════════════════════════════════════════════════
// Form validation (courtesy — the server re-decides every one of these)
// ═══════════════════════════════════════════════════════════════════════════

const transferInput = {
  sourceStoreId: 1,
  destinationStoreId: 2,
  ingredientId: 5,
  quantity: "3",
  reason: "Beşiktaş şubesine takviye",
  availableQuantity: "10.000",
};

test("a valid transfer passes", () => {
  assert.deepEqual(validateTransferForm(transferInput), []);
});

test("a transfer to the manager's OWN store is rejected when the source is known", () => {
  const errors = validateTransferForm({ ...transferInput, destinationStoreId: 1 });
  assert.ok(errors.includes(TRANSFER_VALIDATION.sameStore));
  assert.equal(TRANSFER_VALIDATION.sameStore, "Kaynak ve hedef şube aynı olamaz.");
});

test("when the source store is NOT known, the same-store check stands down", () => {
  // The session has not resolved yet. We cannot know it is the same store, so we
  // do not invent a refusal — the server still rejects it (`same_store_transfer`).
  const errors = validateTransferForm({
    ...transferInput,
    sourceStoreId: null,
    destinationStoreId: 1,
  });
  assert.equal(errors.includes(TRANSFER_VALIDATION.sameStore), false);
});

test("a transfer with no destination asks for one", () => {
  const errors = validateTransferForm({ ...transferInput, destinationStoreId: null });
  assert.ok(errors.includes("Hedef şube seçin."));
});

test("a transfer larger than available stock is refused, with the reason", () => {
  const errors = validateTransferForm({ ...transferInput, quantity: "11" });
  assert.ok(errors.includes(TRANSFER_VALIDATION.overAvailable));
  assert.match(TRANSFER_VALIDATION.overAvailable, /Ayrılmış stok/);
});

test("a transfer is not blocked locally when available stock is unknown", () => {
  // Display data may be stale or absent. The client must not invent a refusal the
  // server would not make; the server owns this decision.
  const errors = validateTransferForm({
    ...transferInput,
    quantity: "9999",
    availableQuantity: null,
  });
  assert.equal(errors.includes(TRANSFER_VALIDATION.overAvailable), false);
});

test("quantity must be positive and reason mandatory for waste", () => {
  const errors = validateWasteForm({ ingredientId: 1, quantity: "0", reason: "" });
  assert.ok(errors.includes("Stok miktarı sıfırdan büyük olmalı."));
  assert.ok(errors.includes("Bu stok işlemi için neden belirtmeniz gerekiyor."));

  assert.deepEqual(
    validateWasteForm({ ingredientId: 1, quantity: "2", reason: "Yanan hamur" }),
    [],
  );
});

test("a manual adjustment accepts a signed delta and refuses zero", () => {
  assert.deepEqual(
    validateAdjustmentForm({ ingredientId: 1, delta: "-3", reason: "Sayım farkı" }),
    [],
  );
  assert.deepEqual(
    validateAdjustmentForm({ ingredientId: 1, delta: "3", reason: "Sayım farkı" }),
    [],
  );

  const zero = validateAdjustmentForm({ ingredientId: 1, delta: "0", reason: "Sayım farkı" });
  assert.ok(zero.includes(ADJUSTMENT_VALIDATION.deltaNonZero));
});

test("the manual adjustment form says it is for counts, not for transfers", () => {
  // The confusion this hint exists to prevent: correcting two branches with two
  // manual adjustments instead of one transfer, which loses the link between them
  // and makes the stock look like it was destroyed here and bought there.
  assert.match(MANUAL_ADJUSTMENT_HINT, /transfer kullanın/);
  assert.match(MANUAL_ADJUSTMENT_HINT, /sayım/i);
});

// ── Formatting ───────────────────────────────────────────────────────────────

test("quantities are formatted for a Turkish reader and never NaN", () => {
  assert.equal(formatQuantity("1234.5"), "1.234,5");
  assert.equal(formatQuantity("10.000"), "10");
  assert.equal(formatQuantity(null), "—");
  assert.equal(formatQuantity("not-a-number"), "—");
  assert.equal(formatDelta(null), "—");
});
