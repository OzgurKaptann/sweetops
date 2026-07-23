/**
 * Setup errors become Turkish sentences a manager can act on — never wire values.
 *
 * The property under test is the one that keeps an internal off a shop's screen:
 * a known backend code resolves to our copy, an unknown code resolves to the
 * server's Turkish message only if it looks display-safe, and everything else
 * resolves to one calm generic line. No branch of this function can put an
 * exception class, a constraint name, an enum or a URL in front of a manager.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/setup-errors.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { SetupApiError, SetupNetworkUncertainError } from "./setup-api.ts";
import {
  SETUP_ERROR_MESSAGE,
  SETUP_ERROR_NETWORK_UNCERTAIN,
  SETUP_ERROR_UNKNOWN,
  isSetupOutcomeUncertain,
  setupErrorMessage,
} from "./setup-errors.ts";

test("every known backend code resolves to Turkish copy", () => {
  for (const code of Object.keys(SETUP_ERROR_MESSAGE)) {
    const message = setupErrorMessage(new SetupApiError(400, code, ""));
    assert.equal(message, SETUP_ERROR_MESSAGE[code]);
    assert.ok(message.length > 0, `${code} has empty copy`);
    // The code itself never appears in what the manager reads.
    assert.ok(!message.includes(code), `${code} leaked into its own message`);
  }
});

test("the codes this screen can actually produce are all covered", () => {
  // Every `error` value raised by app/services/store_setup_service.py and
  // app/routers/owner_setup.py, plus the shared auth ones. A gap here is a
  // manager reading a generic line where a specific one existed.
  for (const code of [
    "product_not_found",
    "product_name_taken",
    "product_name_required",
    "invalid_price",
    "not_published",
    "invalid_sort_order",
    "table_not_found",
    "table_number_required",
    "table_number_taken",
    "qr_token_already_active",
    "no_store_assigned",
    "forbidden",
    "csrf_invalid",
    "origin_rejected",
  ]) {
    assert.ok(SETUP_ERROR_MESSAGE[code], `no copy for ${code}`);
  }
});

test("the not-published message names the fix rather than the failure", () => {
  // Nothing is wrong with the product; it simply is not on this branch's menu.
  const message = setupErrorMessage(new SetupApiError(409, "not_published", ""));
  assert.ok(message.includes("menüye ekleyin"));
});

test("the QR-already-active message says what rotation costs", () => {
  // Otherwise a manager reaches for rotation as a way to "see the link again"
  // and silently kills the sticker on the table.
  const message = setupErrorMessage(
    new SetupApiError(409, "qr_token_already_active", ""),
  );
  assert.ok(message.includes("yenile"));
  assert.ok(message.includes("geçersiz"));
});

test("an unknown code falls back to a safe server message", () => {
  const message = setupErrorMessage(
    new SetupApiError(400, "brand_new_code", "Bu işlem şu anda yapılamıyor."),
  );
  assert.equal(message, "Bu işlem şu anda yapılamıyor.");
});

test("an unsafe server message is replaced, never displayed", () => {
  for (const unsafe of [
    'IntegrityError: duplicate key value violates unique constraint "uq_store_products_store_product"',
    "Traceback (most recent call last): app/services/store_setup_service.py",
    "See https://internal.example/runbook",
    "STORE_PRODUCT_CONFLICT",
  ]) {
    const message = setupErrorMessage(new SetupApiError(500, "weird_code", unsafe));
    assert.equal(message, SETUP_ERROR_UNKNOWN, `leaked: ${unsafe}`);
  }
});

test("a thrown TypeError from our own code never reaches the screen", () => {
  assert.equal(
    setupErrorMessage(new TypeError("Cannot read properties of undefined")),
    SETUP_ERROR_UNKNOWN,
  );
  assert.equal(setupErrorMessage("something"), SETUP_ERROR_UNKNOWN);
  assert.equal(setupErrorMessage(null), SETUP_ERROR_UNKNOWN);
});

test("network uncertainty is its own message and is flagged as uncertain", () => {
  const err = new SetupNetworkUncertainError();
  assert.equal(setupErrorMessage(err), SETUP_ERROR_NETWORK_UNCERTAIN);
  assert.equal(isSetupOutcomeUncertain(err), true);
  // …and it does not tell the manager the operation failed, because it may not have.
  assert.ok(!SETUP_ERROR_NETWORK_UNCERTAIN.includes("başarısız"));
  assert.ok(SETUP_ERROR_NETWORK_UNCERTAIN.includes("kontrol edin"));

  assert.equal(isSetupOutcomeUncertain(new SetupApiError(409, "x", "")), false);
  assert.equal(isSetupOutcomeUncertain(new Error("boom")), false);
});
