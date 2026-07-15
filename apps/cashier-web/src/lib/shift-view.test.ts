/**
 * Cashier shift, as the cashier experiences it.
 *
 * The one thing that must never happen here is a raw status enum (OPEN/CLOSED) or
 * a raw discrepancy sign reaching the screen. A cashier reads "Eksik" and counts
 * the drawer again; a cashier who reads "-10.00 CLOSED" reads a bug report. Every
 * test below defends a specific way that could go subtly wrong.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/shift-view.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DISCREPANCY_LABEL,
  SHIFT_COPY,
  SHIFT_LABELS,
  SHIFT_STATUS_LABEL,
  discrepancyClass,
  discrepancyLabel,
  fingerprintShiftCommand,
  shiftStatusLabel,
  validateCountedCash,
  validateOpeningCash,
} from "./shift-view.ts";

// ── Status label (never the raw enum) ─────────────────────────────────────────

test("OPEN and CLOSED render in Turkish, never as the raw enum", () => {
  assert.equal(shiftStatusLabel("OPEN"), "Açık");
  assert.equal(shiftStatusLabel("CLOSED"), "Kapalı");
  assert.equal(SHIFT_STATUS_LABEL.OPEN, "Açık");
  assert.equal(SHIFT_STATUS_LABEL.CLOSED, "Kapalı");
});

test("an unknown status falls back to Bilinmiyor, not the raw value", () => {
  assert.equal(shiftStatusLabel("SUSPENDED"), "Bilinmiyor");
  assert.equal(shiftStatusLabel(null), "Bilinmiyor");
  assert.equal(shiftStatusLabel(undefined), "Bilinmiyor");
});

// ── Discrepancy (Denk / Eksik / Fazla) ────────────────────────────────────────

test("discrepancy maps zero to Denk, negative to Eksik, positive to Fazla", () => {
  assert.equal(discrepancyClass("0.00"), "balanced");
  assert.equal(discrepancyClass("-10.00"), "short");
  assert.equal(discrepancyClass("5.00"), "over");
  assert.equal(discrepancyLabel("0.00"), "Denk");
  assert.equal(discrepancyLabel("-10.00"), "Eksik");
  assert.equal(discrepancyLabel("5.00"), "Fazla");
});

test("the three discrepancy labels are exactly Denk / Eksik / Fazla", () => {
  assert.equal(DISCREPANCY_LABEL.balanced, "Denk");
  assert.equal(DISCREPANCY_LABEL.short, "Eksik");
  assert.equal(DISCREPANCY_LABEL.over, "Fazla");
});

test("a sub-cent difference is treated as balanced, not a spurious Eksik/Fazla", () => {
  assert.equal(discrepancyClass("0.004"), "balanced");
  assert.equal(discrepancyClass("-0.004"), "balanced");
});

test("a missing discrepancy does not crash a receipt — it reads as Denk", () => {
  assert.equal(discrepancyClass(null), "balanced");
  assert.equal(discrepancyClass(undefined), "balanced");
  assert.equal(discrepancyClass("abc"), "balanced");
});

// ── Opening-cash validation ───────────────────────────────────────────────────

test("a valid opening cash passes", () => {
  assert.deepEqual(validateOpeningCash("200.00"), []);
});

test("opening cash of zero is allowed — a drawer can start empty", () => {
  assert.deepEqual(validateOpeningCash("0"), []);
});

test("a negative opening cash is refused with Turkish copy", () => {
  const errs = validateOpeningCash("-1");
  assert.ok(errs.includes(SHIFT_COPY.openingNegative));
  assert.equal(SHIFT_COPY.openingNegative, "Açılış nakdi negatif olamaz.");
});

test("an empty or non-numeric opening cash is refused", () => {
  assert.equal(validateOpeningCash("").length, 1);
  assert.equal(validateOpeningCash("abc").length, 1);
});

// ── Counted-cash validation ───────────────────────────────────────────────────

test("counting an empty drawer (0) is a valid close", () => {
  assert.deepEqual(validateCountedCash("0"), []);
});

test("a negative counted cash is refused with Turkish copy", () => {
  const errs = validateCountedCash("-5");
  assert.ok(errs.includes(SHIFT_COPY.countedNegative));
  assert.equal(SHIFT_COPY.countedNegative, "Kapanış tutarı negatif olamaz.");
});

// ── Turkish copy is present and display-safe ──────────────────────────────────

test("the key operational lines are in Turkish", () => {
  assert.equal(SHIFT_COPY.noOpenShift, "Açık vardiya bulunmuyor.");
  assert.equal(SHIFT_COPY.openSuccess, "Vardiya başarıyla açıldı.");
  assert.equal(SHIFT_COPY.closeSuccess, "Vardiya başarıyla kapatıldı.");
  assert.equal(SHIFT_COPY.alreadyOpen, "Bu kasiyer için açık vardiya zaten var.");
});

test("the close-uncertain copy does NOT read as a failure and points at checking first", () => {
  // A cashier told 'başarısız' re-enters by hand, minting a new key and risking a
  // double close. The copy must invite a status check, not a blind retry.
  assert.equal(/başarısız/i.test(SHIFT_COPY.closeUncertain), false);
  assert.ok(SHIFT_COPY.closeUncertain.includes("vardiya durumunu kontrol edin"));
});

test("no label or copy is a raw enum or leaks an internal token", () => {
  const strings = [
    ...Object.values(SHIFT_LABELS),
    ...Object.values(SHIFT_STATUS_LABEL),
    ...Object.values(DISCREPANCY_LABEL),
    ...Object.values(SHIFT_COPY),
  ];
  for (const s of strings) {
    assert.equal(/^[A-Z_]+$/.test(s), false, `${s} looks like a raw enum`);
    assert.equal(s.includes("OPEN"), false);
    assert.equal(s.includes("CLOSED"), false);
    assert.equal(s.includes("_"), false, `${s} leaks an underscore token`);
  }
});

// ── Idempotency fingerprints ──────────────────────────────────────────────────

test("an unchanged open command reuses its fingerprint (note null vs absent match)", () => {
  const a = fingerprintShiftCommand({ kind: "shift_open", openingCash: "200.00" });
  const b = fingerprintShiftCommand({ kind: "shift_open", openingCash: "200.00", openNote: null });
  assert.equal(a, b);
});

test("editing the opening cash mints a fresh fingerprint", () => {
  const a = fingerprintShiftCommand({ kind: "shift_open", openingCash: "200.00" });
  const b = fingerprintShiftCommand({ kind: "shift_open", openingCash: "250.00" });
  assert.notEqual(a, b);
});

test("editing the counted cash mints a fresh close fingerprint", () => {
  const base = { kind: "shift_close" as const, shiftId: 7, countedCash: "160.00" };
  const first = fingerprintShiftCommand(base);
  assert.notEqual(first, fingerprintShiftCommand({ ...base, countedCash: "150.00" }));
  assert.notEqual(first, fingerprintShiftCommand({ ...base, shiftId: 8 }));
  assert.notEqual(first, fingerprintShiftCommand({ ...base, closeNote: "kasa devri" }));
});

test("an open and a close command never share a fingerprint", () => {
  const open = fingerprintShiftCommand({ kind: "shift_open", openingCash: "160.00" });
  const close = fingerprintShiftCommand({ kind: "shift_close", shiftId: 1, countedCash: "160.00" });
  assert.notEqual(open, close);
});
