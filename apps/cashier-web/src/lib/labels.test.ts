/**
 * The cashier screen must never show a customer-facing operator a raw API enum.
 *
 * These tests pin two things: that every wire value the API can send has a
 * Turkish label, and that an unrecognised value degrades to "Bilinmiyor" rather
 * than falling through to the enum itself.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/labels.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  PAYMENT_METHOD_LABEL,
  PAYMENT_STATUS_LABEL,
  PREPARATION_STATUS_LABEL,
  REFUND_STATUS_LABEL,
  TRANSACTION_KIND_LABEL,
  paymentMethodLabel,
  paymentStatusLabel,
  preparationStatusLabel,
  refundStatusLabel,
  transactionKindLabel,
} from "./labels.ts";

// ── Payment status ───────────────────────────────────────────────────────────

test("payment statuses map to Turkish labels", () => {
  assert.equal(paymentStatusLabel("UNPAID"), "Ödenmedi");
  assert.equal(paymentStatusLabel("PARTIALLY_PAID"), "Kısmi ödendi");
  assert.equal(paymentStatusLabel("PAID"), "Ödendi");
  assert.equal(paymentStatusLabel("REFUNDED"), "İade edildi");
});

test("every payment_status the API can send has a label", () => {
  // Mirrors the backend payment_status enum.
  for (const wire of ["UNPAID", "PARTIALLY_PAID", "PAID", "REFUNDED"]) {
    assert.ok(PAYMENT_STATUS_LABEL[wire], `no label for ${wire}`);
  }
});

// ── Refund status ────────────────────────────────────────────────────────────

test("refund statuses map to Turkish labels", () => {
  assert.equal(refundStatusLabel("NONE"), "İade yok");
  assert.equal(refundStatusLabel("PARTIAL"), "Kısmi iade");
  assert.equal(refundStatusLabel("FULL"), "Tamamen iade edildi");
});

test("every refund_status the API can send has a label", () => {
  for (const wire of ["NONE", "PARTIAL", "FULL"]) {
    assert.ok(REFUND_STATUS_LABEL[wire], `no label for ${wire}`);
  }
});

// ── Preparation status ───────────────────────────────────────────────────────

test("preparation statuses map to Turkish labels", () => {
  assert.equal(preparationStatusLabel("NEW"), "Bekliyor");
  assert.equal(preparationStatusLabel("IN_PREP"), "Hazırlanıyor");
  assert.equal(preparationStatusLabel("READY"), "Hazır");
  assert.equal(preparationStatusLabel("DELIVERED"), "Teslim edildi");
  assert.equal(preparationStatusLabel("CANCELLED"), "İptal edildi");
});

test("every preparation status in the kitchen state machine has a label", () => {
  for (const wire of ["NEW", "IN_PREP", "READY", "DELIVERED", "CANCELLED"]) {
    assert.ok(PREPARATION_STATUS_LABEL[wire], `no label for ${wire}`);
  }
});

// ── Payment method + transaction kind ────────────────────────────────────────

test("payment methods map to Turkish labels", () => {
  assert.equal(paymentMethodLabel("CASH"), "Nakit");
  assert.equal(paymentMethodLabel("CARD"), "Kart");
  assert.equal(paymentMethodLabel("OTHER"), "Diğer");
  assert.ok(PAYMENT_METHOD_LABEL["CASH"]);
});

test("transaction kinds map to Turkish labels", () => {
  assert.equal(transactionKindLabel("SETTLEMENT"), "Tahsilat");
  assert.equal(transactionKindLabel("REFUND"), "İade");
  assert.ok(TRANSACTION_KIND_LABEL["REFUND"]);
});

// ── The property that actually matters ───────────────────────────────────────

test("no label is the raw enum value", () => {
  const maps = [
    PAYMENT_STATUS_LABEL,
    REFUND_STATUS_LABEL,
    PREPARATION_STATUS_LABEL,
    PAYMENT_METHOD_LABEL,
    TRANSACTION_KIND_LABEL,
  ];
  for (const map of maps) {
    for (const [wire, label] of Object.entries(map)) {
      assert.notEqual(label, wire, `${wire} is rendered as its own enum value`);
      assert.doesNotMatch(
        label,
        /^[A-Z_]+$/,
        `${wire} maps to "${label}", which still looks like an enum`,
      );
    }
  }
});

test("an unknown enum degrades to a safe word, never to the raw value", () => {
  // The API introducing PARTIALLY_REFUNDED must not put that on a cashier's screen.
  assert.equal(paymentStatusLabel("PARTIALLY_REFUNDED"), "Bilinmiyor");
  assert.equal(preparationStatusLabel("ON_HOLD"), "Bilinmiyor");
  assert.equal(refundStatusLabel("SOMETHING_NEW"), "Bilinmiyor");
  assert.equal(paymentMethodLabel("VOUCHER"), "Diğer");
});

test("null and undefined are handled without leaking the word null", () => {
  assert.equal(paymentStatusLabel(null), "Bilinmiyor");
  assert.equal(paymentStatusLabel(undefined), "Bilinmiyor");
  assert.equal(preparationStatusLabel(""), "Bilinmiyor");
});
