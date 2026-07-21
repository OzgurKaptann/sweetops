/**
 * Owner order-issue history view logic.
 *
 * A manager reads "Kısmi iade" and "40,00 ₺"; a manager who reads "PARTIAL_REFUND"
 * or "null" reads a bug report. Every test below defends a specific way that could
 * go subtly wrong.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/order-issue-view.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ISSUE_HISTORY_COPY,
  ISSUE_STATUS_LABEL,
  ISSUE_TYPE_LABEL,
  RESOLUTION_LABEL,
  formatMoney,
  issueStatusLabel,
  issueTypeLabel,
  resolutionLabel,
  toOrderIssueRow,
} from "./order-issue-view.ts";

// ── Turkish labels (never the raw enum) ───────────────────────────────────────

test("issue types, statuses and resolutions render in Turkish", () => {
  assert.equal(issueTypeLabel("CUSTOMER_CANCELLED"), "Müşteri iptal etti");
  assert.equal(issueTypeLabel("QUALITY_PROBLEM"), "Kalite sorunu");
  assert.equal(issueStatusLabel("OPEN"), "Açık");
  assert.equal(issueStatusLabel("RESOLVED"), "Çözüldü");
  assert.equal(resolutionLabel("FULL_REFUND"), "Tam iade");
  assert.equal(resolutionLabel("PARTIAL_REFUND"), "Kısmi iade");
  assert.equal(resolutionLabel("NO_REFUND"), "İadesiz çözüldü");
  assert.equal(resolutionLabel("CANCEL_ONLY"), "Sadece iptal");
});

test("every issue type / status / resolution the API can send has a label", () => {
  for (const wire of [
    "CUSTOMER_CANCELLED", "WRONG_ITEM", "MISSING_ITEM",
    "QUALITY_PROBLEM", "DUPLICATE_ORDER", "STAFF_ERROR", "OTHER",
  ]) {
    assert.ok(ISSUE_TYPE_LABEL[wire], `no label for ${wire}`);
  }
  for (const wire of ["OPEN", "RESOLVED", "VOIDED"]) {
    assert.ok(ISSUE_STATUS_LABEL[wire], `no label for ${wire}`);
  }
  for (const wire of ["NO_REFUND", "FULL_REFUND", "PARTIAL_REFUND", "CANCEL_ONLY"]) {
    assert.ok(RESOLUTION_LABEL[wire], `no label for ${wire}`);
  }
});

test("an unknown value degrades to a safe word, never the raw enum", () => {
  assert.equal(issueTypeLabel("SOMETHING_NEW"), "Bilinmiyor");
  assert.equal(issueStatusLabel("SUSPENDED"), "Bilinmiyor");
  assert.equal(resolutionLabel("STORE_CREDIT"), "—");
  assert.equal(resolutionLabel(null), "—");
});

test("no label or copy is a raw enum or leaks an internal token", () => {
  const strings = [
    ...Object.values(ISSUE_TYPE_LABEL),
    ...Object.values(ISSUE_STATUS_LABEL),
    ...Object.values(RESOLUTION_LABEL),
    ISSUE_HISTORY_COPY.heading,
    ISSUE_HISTORY_COPY.empty,
    ...Object.values(ISSUE_HISTORY_COPY.columns),
  ];
  for (const s of strings) {
    assert.equal(/^[A-Z_]+$/.test(s), false, `${s} looks like a raw enum`);
    assert.equal(s.includes("_"), false, `${s} leaks an underscore token`);
    assert.equal(s.includes("REFUND"), false, `${s} leaks REFUND token`);
  }
});

// ── Money formatting ──────────────────────────────────────────────────────────

test("refund amounts format as Turkish lira, absent ones become an em dash", () => {
  assert.equal(formatMoney("40.00"), "40,00 ₺");
  assert.equal(formatMoney(null), "—");
  assert.equal(formatMoney(""), "—");
  assert.equal(formatMoney("abc"), "—");
});

// ── Row transform (display-safe) ──────────────────────────────────────────────

test("a resolved full-refund issue becomes a fully display-safe row", () => {
  const row = toOrderIssueRow({
    id: 3,
    order_id: 42,
    order_code: "SIP-000042",
    issue_type: "CUSTOMER_CANCELLED",
    status: "RESOLVED",
    resolution_type: "FULL_REFUND",
    approved_refund_amount: "100.00",
    created_by_display: "kasiyer1",
    resolved_by_display: "mudur1",
    created_at: "2026-07-15T10:00:00Z",
    resolved_at: "2026-07-15T10:05:00Z",
  });
  assert.equal(row.orderCode, "SIP-000042");
  assert.equal(row.issueTypeLabel, "Müşteri iptal etti");
  assert.equal(row.statusLabel, "Çözüldü");
  assert.equal(row.resolutionLabel, "Tam iade");
  assert.equal(row.refundAmount, "100,00 ₺");
  assert.equal(row.createdBy, "kasiyer1");
  assert.equal(row.resolvedBy, "mudur1");
  assert.equal(row.isResolved, true);
});

test("an open issue has an em-dash resolver, resolution, and refund amount", () => {
  const row = toOrderIssueRow({
    id: 4,
    order_id: 43,
    order_code: "SIP-000043",
    issue_type: "QUALITY_PROBLEM",
    status: "OPEN",
    resolution_type: null,
    approved_refund_amount: null,
    created_by_display: "kasiyer2",
    resolved_by_display: null,
    created_at: "2026-07-15T11:00:00Z",
    resolved_at: null,
  });
  assert.equal(row.statusLabel, "Açık");
  assert.equal(row.resolutionLabel, "—");
  assert.equal(row.refundAmount, "—");
  assert.equal(row.resolvedBy, "—");
  assert.equal(row.isResolved, false);
});

test("the empty-state copy is Turkish", () => {
  assert.equal(ISSUE_HISTORY_COPY.empty, "Bu şubede henüz sipariş sorunu kaydı yok.");
  assert.equal(ISSUE_HISTORY_COPY.heading, "Sorunlu siparişler");
});
