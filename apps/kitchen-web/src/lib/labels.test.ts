/**
 * The kitchen board must never show a cook a raw API enum.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/labels.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  CONNECTION_STATE_LABEL,
  ORDER_STATUS_LABEL,
  connectionStateLabel,
  orderStatusLabel,
} from "./labels.ts";

test("order statuses map to Turkish labels", () => {
  assert.equal(orderStatusLabel("NEW"), "Bekliyor");
  assert.equal(orderStatusLabel("IN_PREP"), "Hazırlanıyor");
  assert.equal(orderStatusLabel("READY"), "Hazır");
  assert.equal(orderStatusLabel("DELIVERED"), "Teslim edildi");
  assert.equal(orderStatusLabel("CANCELLED"), "İptal edildi");
});

test("every status in the kitchen state machine has a label", () => {
  for (const wire of ["NEW", "IN_PREP", "READY", "DELIVERED", "CANCELLED"]) {
    assert.ok(ORDER_STATUS_LABEL[wire], `no Turkish label for ${wire}`);
  }
});

test("connection states map to Turkish labels", () => {
  assert.equal(connectionStateLabel("connected"), "Canlı");
  assert.equal(connectionStateLabel("connecting"), "Bağlanıyor…");
  assert.equal(connectionStateLabel("disconnected"), "Bağlantı kesildi");
  assert.equal(connectionStateLabel("error"), "Bağlantı hatası");
  for (const wire of ["connected", "connecting", "disconnected", "error"]) {
    assert.ok(CONNECTION_STATE_LABEL[wire], `no Turkish label for ${wire}`);
  }
});

test("no label is the raw enum value", () => {
  for (const map of [ORDER_STATUS_LABEL, CONNECTION_STATE_LABEL]) {
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
  assert.equal(orderStatusLabel("ON_HOLD"), "Bilinmiyor");
  assert.equal(orderStatusLabel(null), "Bilinmiyor");
  assert.equal(connectionStateLabel("reconnecting"), "Bağlantı kesildi");
});
