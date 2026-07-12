/**
 * A customer must never be shown an English technical string, a status code, or
 * a raw error name when ordering fails.
 *
 * The regression these tests guard against is subtle: it is easy to "handle" an
 * error by falling through to `err.message`, which for a fetch failure is
 * something like "order request failed: network (503)". That is a debug line, it
 * is English, and it means nothing to somebody sitting at a table with a phone.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/order-messages.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import { OrderRequestError, QrResolveError } from "./api.ts";
import {
  ORDER_REJECTED_MESSAGE,
  ORDER_UNCERTAIN_MESSAGE,
  orderErrorMessage,
  qrPhaseForError,
  qrPhaseMessage,
} from "./order-messages.ts";

/** Any Latin word that would betray an English/technical string to a customer. */
const TECHNICAL_WORDS =
  /\b(error|failed|failure|invalid|unauthorized|forbidden|not found|network|server|validation|timeout|exception|null|undefined|status|request)\b/i;

/** Turkish copy must actually be Turkish, not just "not English". */
const TURKISH_LETTERS = /[çğıöşüÇĞİÖŞÜ]/;

function assertCustomerSafe(message: string) {
  assert.ok(message.length > 0, "message is empty");
  assert.doesNotMatch(
    message,
    TECHNICAL_WORDS,
    `customer-facing copy leaks a technical word: "${message}"`,
  );
  assert.doesNotMatch(
    message,
    /\d{3}/,
    `customer-facing copy leaks what looks like a status code: "${message}"`,
  );
  assert.match(message, TURKISH_LETTERS, `copy does not look Turkish: "${message}"`);
}

// ── Order submission failures ────────────────────────────────────────────────

test("a network failure tells the customer it is safe to retry", () => {
  const msg = orderErrorMessage(new OrderRequestError("network"));
  assert.equal(msg, ORDER_UNCERTAIN_MESSAGE);
  assertCustomerSafe(msg);
  // The whole point of this branch: do not imply the order was lost.
  assert.match(msg, /iki kez oluşturulmaz/);
});

test("a 5xx is treated as uncertain, not as a rejection", () => {
  const msg = orderErrorMessage(new OrderRequestError("server", 503));
  assert.equal(msg, ORDER_UNCERTAIN_MESSAGE);
  assertCustomerSafe(msg);
});

test("a 4xx rejection asks the customer to check their selection", () => {
  const msg = orderErrorMessage(new OrderRequestError("validation", 422));
  assert.equal(msg, ORDER_REJECTED_MESSAGE);
  assertCustomerSafe(msg);
});

test("an unknown throwable still yields Turkish customer copy", () => {
  // The realistic regression: a raw Error whose message is an English debug line.
  assertCustomerSafe(orderErrorMessage(new Error("Request failed with status 500")));
  assertCustomerSafe(orderErrorMessage("boom"));
  assertCustomerSafe(orderErrorMessage(undefined));
});

test("the thrown error's own English message is never surfaced", () => {
  const err = new OrderRequestError("validation", 422);
  // `err.message` is "order request failed: validation (422)" — a debug string.
  assert.notEqual(orderErrorMessage(err), err.message);
  assert.ok(!orderErrorMessage(err).includes("422"));
});

// ── QR gate copy ─────────────────────────────────────────────────────────────

test("every QR phase has customer-safe Turkish copy", () => {
  for (const phase of ["loading", "missing", "invalid", "unavailable", "network"] as const) {
    assertCustomerSafe(qrPhaseMessage(phase));
  }
});

test("the server's Turkish message wins over the local fallback", () => {
  const fromServer = "Bu masa şu anda siparişe kapalı. Lütfen personelden yardım isteyin.";
  assert.equal(qrPhaseMessage("unavailable", fromServer), fromServer);
  // …but a missing server message still yields Turkish, never an empty string.
  assertCustomerSafe(qrPhaseMessage("unavailable", null));
  assertCustomerSafe(qrPhaseMessage("unavailable", undefined));
});

test("QR failures map to the phase the screen should show", () => {
  assert.equal(qrPhaseForError("invalid"), "invalid");
  assert.equal(qrPhaseForError("unavailable"), "unavailable");
  assert.equal(qrPhaseForError("network"), "network");
});

test("a QR error routed through orderErrorMessage stays customer-safe", () => {
  assertCustomerSafe(orderErrorMessage(new QrResolveError("invalid")));
  assertCustomerSafe(orderErrorMessage(new QrResolveError("network")));
});
