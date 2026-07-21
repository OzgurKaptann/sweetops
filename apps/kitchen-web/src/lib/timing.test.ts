/**
 * Kitchen preparation-timing presentation helpers.
 *
 * The board must show cooks Turkish elapsed times and delay labels — never a raw
 * enum, never a bare seconds count, and never a fabricated number when timing is
 * unknown.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/timing.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import type { OrderTiming } from "./timing.ts";
import {
  DELAY_REASON_LABEL,
  DELAY_STATE_LABEL,
  TIMING_ERROR_MESSAGE,
  delayReasonLabel,
  delayStateLabel,
  formatDuration,
  prepPhaseNote,
  timingLines,
} from "./timing.ts";

function makeTiming(overrides: Partial<OrderTiming> = {}): OrderTiming {
  return {
    order_id: 1,
    status: "NEW",
    queued_seconds: null,
    prep_seconds: null,
    time_to_ready_seconds: null,
    queued_seconds_active: null,
    prep_seconds_active: null,
    active_seconds: null,
    is_delayed: false,
    delay_state: "ok",
    delay_reason: null,
    ...overrides,
  };
}

// 1. Waiting (queue) elapsed time renders in Turkish.
test("a waiting order shows its Turkish waiting elapsed time", () => {
  const t = makeTiming({ status: "NEW", queued_seconds_active: 330 });
  const lines = timingLines(t);
  assert.equal(lines[0].label, "Bekleme süresi");
  assert.equal(lines[0].value, "5 dk 30 sn");
});

// 2. Prep elapsed time renders in Turkish.
test("an in-prep order shows its Turkish prep elapsed time", () => {
  const t = makeTiming({
    status: "IN_PREP",
    queued_seconds: 120,
    prep_seconds_active: 480,
  });
  const lines = timingLines(t);
  assert.deepEqual(
    lines.map((l) => l.label),
    ["Bekleme süresi", "Hazırlık süresi"],
  );
  assert.equal(lines[1].value, "8 dk");
});

test("a ready order shows total time to ready", () => {
  const t = makeTiming({ status: "READY", time_to_ready_seconds: 600 });
  const lines = timingLines(t);
  assert.equal(lines[0].label, "Toplam süre");
  assert.equal(lines[0].value, "10 dk");
});

// 3–4. Delay labels render in Turkish (warning and critical).
test("delayed states map to Turkish labels", () => {
  assert.equal(delayStateLabel("ok"), "Zamanında");
  assert.equal(delayStateLabel("warning"), "Gecikiyor");
  assert.equal(delayStateLabel("critical"), "Kritik gecikme");
});

test("delay reasons map to Turkish phrases", () => {
  assert.equal(delayReasonLabel("queue_critical"), "Sırada çok uzun bekliyor");
  assert.equal(delayReasonLabel("prep_warning"), "Hazırlık uzuyor");
  assert.equal(delayReasonLabel(null), "");
});

// 5. Phase note is Turkish, never the raw enum.
test("prep phase note is Turkish for each active status", () => {
  assert.equal(prepPhaseNote(makeTiming({ status: "NEW" })), "Henüz başlamadı");
  assert.equal(prepPhaseNote(makeTiming({ status: "IN_PREP" })), "Hazırlık başladı");
  assert.equal(prepPhaseNote(makeTiming({ status: "READY" })), "Hazırlandı");
});

// 6. No label is the raw enum value.
test("no delay label is the raw enum value", () => {
  for (const map of [DELAY_STATE_LABEL, DELAY_REASON_LABEL]) {
    for (const [wire, label] of Object.entries(map)) {
      assert.notEqual(label, wire, `${wire} is rendered as its own enum value`);
      assert.doesNotMatch(label, /^[a-z_]+$/, `${wire} maps to "${label}", still enum-like`);
    }
  }
});

// 7. Null / missing timing renders safely, never a fabricated 0.
test("missing timing renders as an em dash, never a fabricated number", () => {
  assert.equal(formatDuration(null), "—");
  assert.equal(formatDuration(undefined), "—");
  assert.equal(formatDuration(-5), "—");
  const t = makeTiming({ status: "NEW", queued_seconds_active: null });
  assert.equal(timingLines(t)[0].value, "—");
});

test("sub-minute durations round to a friendly Turkish phrase", () => {
  assert.equal(formatDuration(0), "1 dk'dan az");
  assert.equal(formatDuration(45), "1 dk'dan az");
  assert.equal(formatDuration(60), "1 dk");
});

test("an unknown delay enum degrades to a safe Turkish word", () => {
  assert.equal(delayStateLabel("meltdown"), "Zamanında");
  assert.equal(delayStateLabel(undefined), "Zamanında");
});

// 8. API error state copy is Turkish.
test("the timing error message is Turkish", () => {
  assert.match(TIMING_ERROR_MESSAGE, /yüklenemedi/);
  assert.doesNotMatch(TIMING_ERROR_MESSAGE, /[A-Z_]{3,}/);
});
