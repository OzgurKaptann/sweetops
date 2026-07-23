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
  CONNECTION_STATE_NOTE,
  ORDER_STATUS_LABEL,
  connectionStateLabel,
  connectionStateNote,
  lastSyncedLabel,
  orderStatusLabel,
} from "./labels.ts";

/** Every link state the kitchen board can be in. Mirrors lib/liveSync.ts. */
const LINK_STATES = [
  "connecting",
  "live",
  "reconnecting",
  "polling",
  "stale",
  "offline",
];

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
  assert.equal(connectionStateLabel("live"), "Canlı");
  assert.equal(connectionStateLabel("connecting"), "Bağlanıyor…");
  assert.equal(connectionStateLabel("reconnecting"), "Yeniden bağlanılıyor…");
  assert.equal(connectionStateLabel("polling"), "Yedek mod");
  assert.equal(connectionStateLabel("stale"), "Veriler eski olabilir");
  assert.equal(connectionStateLabel("offline"), "Bağlantı yok");
  for (const wire of LINK_STATES) {
    assert.ok(CONNECTION_STATE_LABEL[wire], `no Turkish label for ${wire}`);
    assert.ok(CONNECTION_STATE_NOTE[wire], `no Turkish note for ${wire}`);
  }
});

test("only the live state is allowed to say 'Canlı'", () => {
  // A board that claims to be live while the socket is down or the data is old
  // is the exact failure this branch exists to remove.
  for (const wire of LINK_STATES) {
    if (wire === "live") continue;
    assert.notEqual(
      connectionStateLabel(wire),
      "Canlı",
      `${wire} is presented as a live board`,
    );
  }
  // An unrecognised state degrades to the pessimistic label, never to "Canlı".
  assert.equal(connectionStateLabel("connected"), "Bağlantı yok");
  assert.equal(connectionStateLabel(null), "Bağlantı yok");
  assert.equal(connectionStateLabel(undefined), "Bağlantı yok");
});

test("every degraded state tells the cook what to do", () => {
  for (const wire of ["reconnecting", "polling", "stale", "offline"]) {
    assert.match(connectionStateNote(wire), /\S/);
  }
  assert.match(connectionStateNote("unknown-state"), /Yenile/);
});

test("the last-synced line is honest about never having synced", () => {
  assert.equal(lastSyncedLabel(null, 1_000), "Henüz güncellenmedi");
  assert.equal(lastSyncedLabel(1_000, 1_000), "Az önce güncellendi");
  assert.equal(lastSyncedLabel(0, 25_000), "25 sn önce güncellendi");
  assert.equal(lastSyncedLabel(0, 180_000), "3 dk önce güncellendi");
  assert.equal(lastSyncedLabel(0, 7_200_000), "2 sa önce güncellendi");
  // A clock that drifts backwards must not render a negative age.
  assert.equal(lastSyncedLabel(5_000, 1_000), "Az önce güncellendi");
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
  assert.equal(connectionStateLabel("half-open"), "Bağlantı yok");
});
