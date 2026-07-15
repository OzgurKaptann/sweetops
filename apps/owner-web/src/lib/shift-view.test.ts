/**
 * Owner shift history, as a manager reads it.
 *
 * The manager scans this table for one thing: a cashier whose till did not close
 * "Denk". So the raw status enum and the raw signed discrepancy must never reach a
 * cell — every test below defends that, plus the money/date formatting an owner in
 * Türkiye expects.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/shift-view.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DISCREPANCY_LABEL,
  SHIFT_HISTORY_COPY,
  SHIFT_STATUS_LABEL,
  discrepancyClass,
  discrepancyLabel,
  formatMoney,
  shiftStatusLabel,
  toShiftRow,
  type ShiftLike,
} from "./shift-view.ts";

const closedShift: ShiftLike = {
  id: 1,
  cashier_display: "ayse",
  status: "CLOSED",
  opened_at: "2026-07-15T08:00:00Z",
  closed_at: "2026-07-15T17:00:00Z",
  expected_closing_cash_amount: "170.00",
  counted_closing_cash_amount: "165.00",
  cash_discrepancy_amount: "-5.00",
  net_collected_amount: "110.00",
};

const openShift: ShiftLike = {
  id: 2,
  cashier_display: "mehmet",
  status: "OPEN",
  opened_at: "2026-07-15T09:00:00Z",
  closed_at: null,
  expected_closing_cash_amount: null,
  counted_closing_cash_amount: null,
  cash_discrepancy_amount: null,
  net_collected_amount: null,
};

// ── Status + discrepancy labels ───────────────────────────────────────────────

test("status renders in Turkish, never as the raw enum", () => {
  assert.equal(shiftStatusLabel("OPEN"), "Açık");
  assert.equal(shiftStatusLabel("CLOSED"), "Kapalı");
  assert.equal(shiftStatusLabel("WEIRD"), "Bilinmiyor");
});

test("discrepancy maps to Denk / Eksik / Fazla", () => {
  assert.equal(discrepancyLabel("0.00"), "Denk");
  assert.equal(discrepancyLabel("-5.00"), "Eksik");
  assert.equal(discrepancyLabel("5.00"), "Fazla");
  assert.equal(discrepancyClass("-5.00"), "short");
  assert.equal(DISCREPANCY_LABEL.over, "Fazla");
});

// ── Money formatting ──────────────────────────────────────────────────────────

test("money is formatted tr-TR with the lira sign", () => {
  assert.equal(formatMoney("170.00"), "170,00 ₺");
  assert.equal(formatMoney("1234.5"), "1.234,50 ₺");
});

test("absent money (an open shift) renders as a dash, never null", () => {
  assert.equal(formatMoney(null), "—");
  assert.equal(formatMoney(""), "—");
  assert.equal(formatMoney("abc"), "—");
});

// ── Row transform ─────────────────────────────────────────────────────────────

test("a closed shift row exposes expected vs counted cash and the discrepancy", () => {
  const r = toShiftRow(closedShift);
  assert.equal(r.cashier, "ayse");
  assert.equal(r.statusLabel, "Kapalı");
  assert.equal(r.isClosed, true);
  assert.equal(r.expectedCash, "170,00 ₺");
  assert.equal(r.countedCash, "165,00 ₺");
  assert.equal(r.discrepancyLabel, "Eksik");
  assert.equal(r.discrepancyClass, "short");
  assert.equal(r.netCollected, "110,00 ₺");
});

test("an open shift row shows no discrepancy and dashes for close columns", () => {
  const r = toShiftRow(openShift);
  assert.equal(r.statusLabel, "Açık");
  assert.equal(r.isClosed, false);
  assert.equal(r.discrepancyLabel, "—");
  assert.equal(r.expectedCash, "—");
  assert.equal(r.countedCash, "—");
});

test("the raw status enum never survives the transform", () => {
  const serialized = JSON.stringify(toShiftRow(closedShift));
  assert.equal(serialized.includes("CLOSED"), false);
  assert.equal(serialized.includes("OPEN"), false);
});

// ── Copy ──────────────────────────────────────────────────────────────────────

test("the empty state and headings are in Turkish", () => {
  assert.equal(SHIFT_HISTORY_COPY.heading, "Vardiya geçmişi");
  assert.ok(SHIFT_HISTORY_COPY.empty.length > 0);
  assert.equal(/başarısız|error/i.test(SHIFT_HISTORY_COPY.empty), false);
  assert.equal(SHIFT_HISTORY_COPY.columns.cashier, "Kasiyer");
  assert.equal(SHIFT_HISTORY_COPY.columns.expectedCash, "Beklenen kasa");
  assert.equal(SHIFT_HISTORY_COPY.columns.countedCash, "Sayılan kasa");
  assert.equal(SHIFT_HISTORY_COPY.columns.discrepancy, "Eksik/Fazla");
});

test("no column label or status label is a raw enum token", () => {
  const strings = [
    ...Object.values(SHIFT_STATUS_LABEL),
    ...Object.values(DISCREPANCY_LABEL),
    ...Object.values(SHIFT_HISTORY_COPY.columns),
  ];
  for (const s of strings) {
    assert.equal(/^[A-Z_]+$/.test(s), false, `${s} looks like a raw enum`);
    assert.equal(s.includes("_"), false);
  }
});
