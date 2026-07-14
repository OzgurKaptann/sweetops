/**
 * Inventory error copy — what a manager reads when a stock operation is refused.
 *
 * Two properties are pinned here, and they pull in opposite directions:
 *
 *   1. A KNOWN refusal is explained in Turkish, precisely enough to act on. "Stok
 *      yetersiz" and "stok tanımlı değil" send a manager to two different places;
 *      collapsing them into one message wastes a trip to the storeroom.
 *   2. An UNKNOWN failure is never explained by echoing whatever the server said.
 *      A proxy 502, an unhandled exception or a constraint violation can put
 *      English — or a table name — into `message`, and a manager reading
 *      `duplicate key value violates unique constraint` has been handed an
 *      internal to interpret.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/inventory-errors.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { InventoryApiError, InventoryNetworkUncertainError } from "./inventory-api.ts";
import {
  INVENTORY_ERROR_MESSAGE,
  INVENTORY_ERROR_NETWORK_UNCERTAIN,
  INVENTORY_ERROR_UNKNOWN,
  inventoryErrorMessage,
  isOutcomeUncertain,
  looksDisplaySafe,
} from "./inventory-errors.ts";

const TURKISH = /[çğıöşüÇĞİÖŞÜ]/;

function apiError(code: string, message = ""): InventoryApiError {
  return new InventoryApiError(409, code, message);
}

// ── Known refusals ───────────────────────────────────────────────────────────

test("insufficient available stock is explained in Turkish, with the reason", () => {
  const message = inventoryErrorMessage(apiError("insufficient_available"));

  assert.match(message, TURKISH);
  // The manager must learn WHY the shelf is not usable, not merely that it isn't:
  // the stock is there, it is promised to accepted orders.
  assert.match(message, /Ayrılmış stok/);
  assert.match(message, /transfer edilemez/);
});

test("insufficient on-hand stock is explained in Turkish", () => {
  const message = inventoryErrorMessage(apiError("insufficient_on_hand"));

  assert.match(message, TURKISH);
  assert.match(message, /Fiziksel stok yetersiz/);
  assert.match(message, /Ayrılmış stok/);
});

test("stock_not_configured says the branch has no stock definition — and no more", () => {
  const message = inventoryErrorMessage(apiError("stock_not_configured"));

  assert.match(message, TURKISH);
  assert.equal(message, "Bu malzeme için bu şubede stok tanımı bulunmuyor.");

  // It must NOT tell the manager to fix it with a purchase receipt. A receipt loads
  // and locks an EXISTING stock row and 404s the same way when it is missing, so
  // that advice would send them into a form that refuses them for the same reason.
  assert.doesNotMatch(message, /mal kabul/i);
});

test("a same-store transfer is refused in the manager's own words", () => {
  assert.equal(
    inventoryErrorMessage(apiError("same_store_transfer")),
    "Kaynak ve hedef şube aynı olamaz.",
  );
});

test("no error message leaks a raw code, an enum or an identifier", () => {
  for (const [code, message] of Object.entries(INVENTORY_ERROR_MESSAGE)) {
    assert.ok(!message.includes(code), `${code}: message repeats the raw code`);
    assert.doesNotMatch(
      message,
      /[A-Z][A-Z0-9]*_[A-Z0-9_]+/,
      `${code}: message contains a raw enum`,
    );
    assert.match(message, TURKISH, `${code}: message is not Turkish`);
  }
});

// ── Unknown failures ─────────────────────────────────────────────────────────

test("an unknown error code degrades to one calm Turkish line", () => {
  assert.equal(
    inventoryErrorMessage(apiError("some_unmapped_future_code")),
    INVENTORY_ERROR_UNKNOWN,
  );
});

test("anything that is not an API error at all degrades to the same line", () => {
  assert.equal(inventoryErrorMessage(new TypeError("x.map is not a function")), INVENTORY_ERROR_UNKNOWN);
  assert.equal(inventoryErrorMessage("boom"), INVENTORY_ERROR_UNKNOWN);
  assert.equal(inventoryErrorMessage(undefined), INVENTORY_ERROR_UNKNOWN);
});

test("an unknown code WITH a safe Turkish server message shows that message", () => {
  // The service that refused knows more than we do. If what it said is safe to
  // show, show it — a specific true sentence beats a generic one.
  const message = inventoryErrorMessage(
    apiError("future_code", "Bu şube için stok işlemi şu anda yapılamıyor."),
  );
  assert.equal(message, "Bu şube için stok işlemi şu anda yapılamıyor.");
});

test("an unknown code with a TECHNICAL server message is suppressed", () => {
  // The exact leak this layer exists to stop.
  const leaks = [
    'IntegrityError: duplicate key value violates unique constraint "ix_stock_store"',
    "psycopg2.errors.CheckViolation: ck_movement_delta_sign",
    "Traceback (most recent call last): File app/services/inventory_service.py",
    "movement_type TRANSFER_OUT is not permitted here",
    '{"detail": "insufficient_available"}',
    "502 Bad Gateway from http://api:8000/inventory/waste",
  ];

  for (const leak of leaks) {
    assert.equal(
      inventoryErrorMessage(apiError("future_code", leak)),
      INVENTORY_ERROR_UNKNOWN,
      `leaked: ${leak}`,
    );
  }
});

test("looksDisplaySafe accepts the backend's real Turkish copy", () => {
  // Lifted verbatim from app/core/messages.py — these MUST pass, or a correctly
  // localized message would be thrown away in favour of a generic one.
  const real = [
    "Bu malzeme şubenizin stoğunda tanımlı değil. Önce mal kabul veya sayım girişi yapın.",
    "Hedef şube, gönderen şubeden farklı olmalı.",
    "Stok miktarı sıfırdan büyük olmalı.",
    "Bu işlem için yetkiniz yok.",
  ];
  for (const message of real) {
    assert.ok(looksDisplaySafe(message), `rejected real copy: ${message}`);
  }
});

// ── Network uncertainty ──────────────────────────────────────────────────────

test("an unconfirmed outcome tells the manager to CHECK, not to retry blindly", () => {
  const err = new InventoryNetworkUncertainError();

  assert.equal(inventoryErrorMessage(err), INVENTORY_ERROR_NETWORK_UNCERTAIN);
  assert.ok(isOutcomeUncertain(err));

  // The whole point: it must not read as a failure, and it must send the manager
  // to the ledger before they touch the form again.
  assert.match(INVENTORY_ERROR_NETWORK_UNCERTAIN, /doğrulanamadı/);
  assert.match(INVENTORY_ERROR_NETWORK_UNCERTAIN, /stok hareketlerini kontrol edin/);
  assert.doesNotMatch(INVENTORY_ERROR_NETWORK_UNCERTAIN, /başarısız/);
});

test("an ordinary refusal is NOT flagged as uncertain", () => {
  // A 409 is a definite answer: nothing moved. Warning the manager to go and check
  // the ledger after every validation error would train them to ignore the warning.
  assert.equal(isOutcomeUncertain(apiError("insufficient_available")), false);
});
