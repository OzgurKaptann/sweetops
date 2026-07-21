/**
 * Order issue view logic, as the cashier experiences it.
 *
 * The one thing that must never happen is a raw enum (CUSTOMER_CANCELLED,
 * PARTIAL_REFUND, OPEN) or a raw refund amount reaching the screen. A cashier reads
 * "Kısmi iade"; a cashier who reads "PARTIAL_REFUND" reads a bug report.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/order-issue-view.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  ISSUE_COPY,
  ISSUE_LABELS,
  ISSUE_STATUS_LABEL,
  ISSUE_TYPE_LABEL,
  RESOLUTION_ACTION_LABEL,
  RESOLUTION_LABEL,
  fingerprintIssueCommand,
  issueStatusLabel,
  issueTypeLabel,
  resolutionActionLabel,
  resolutionLabel,
  resolutionNeedsRefundPermission,
  validatePartialRefund,
  validateRequestedRefund,
} from "./order-issue-view.ts";

// ── Turkish labels (never the raw enum) ───────────────────────────────────────

test("issue types render in Turkish, never as the raw enum", () => {
  assert.equal(issueTypeLabel("CUSTOMER_CANCELLED"), "Müşteri iptal etti");
  assert.equal(issueTypeLabel("WRONG_ITEM"), "Yanlış ürün");
  assert.equal(issueTypeLabel("MISSING_ITEM"), "Eksik ürün");
  assert.equal(issueTypeLabel("QUALITY_PROBLEM"), "Kalite sorunu");
  assert.equal(issueTypeLabel("DUPLICATE_ORDER"), "Çift sipariş");
  assert.equal(issueTypeLabel("STAFF_ERROR"), "Personel hatası");
  assert.equal(issueTypeLabel("OTHER"), "Diğer");
});

test("every issue type the API can send has a label", () => {
  for (const wire of [
    "CUSTOMER_CANCELLED", "WRONG_ITEM", "MISSING_ITEM",
    "QUALITY_PROBLEM", "DUPLICATE_ORDER", "STAFF_ERROR", "OTHER",
  ]) {
    assert.ok(ISSUE_TYPE_LABEL[wire], `no label for ${wire}`);
  }
});

test("statuses and resolutions render in Turkish", () => {
  assert.equal(issueStatusLabel("OPEN"), "Açık");
  assert.equal(issueStatusLabel("RESOLVED"), "Çözüldü");
  assert.equal(resolutionLabel("NO_REFUND"), "İadesiz çözüldü");
  assert.equal(resolutionLabel("FULL_REFUND"), "Tam iade");
  assert.equal(resolutionLabel("PARTIAL_REFUND"), "Kısmi iade");
  assert.equal(resolutionLabel("CANCEL_ONLY"), "Sadece iptal");
});

test("the resolution action buttons read in Turkish", () => {
  assert.equal(resolutionActionLabel("NO_REFUND"), "İadesiz çöz");
  assert.equal(resolutionActionLabel("CANCEL_ONLY"), "Sadece iptal");
  assert.equal(resolutionActionLabel("FULL_REFUND"), "Tam iade");
  assert.equal(resolutionActionLabel("PARTIAL_REFUND"), "Kısmi iade");
});

test("an unknown value degrades to a safe word, never the raw enum", () => {
  assert.equal(issueTypeLabel("SOMETHING_NEW"), "Bilinmiyor");
  assert.equal(issueStatusLabel("SUSPENDED"), "Bilinmiyor");
  assert.equal(resolutionLabel("STORE_CREDIT"), "—");
  assert.equal(issueTypeLabel(null), "Bilinmiyor");
  assert.equal(issueTypeLabel(undefined), "Bilinmiyor");
});

test("no label or copy is a raw enum or leaks an internal token", () => {
  const strings = [
    ...Object.values(ISSUE_TYPE_LABEL),
    ...Object.values(ISSUE_STATUS_LABEL),
    ...Object.values(RESOLUTION_LABEL),
    ...Object.values(RESOLUTION_ACTION_LABEL),
    ...Object.values(ISSUE_LABELS),
    ...Object.values(ISSUE_COPY),
  ];
  for (const s of strings) {
    assert.equal(/^[A-Z_]+$/.test(s), false, `${s} looks like a raw enum`);
    assert.equal(s.includes("REFUND"), false, `${s} leaks REFUND token`);
    assert.equal(s.includes("_"), false, `${s} leaks an underscore token`);
  }
});

// ── Validation ────────────────────────────────────────────────────────────────

test("the remaining refundable amount caps a requested refund", () => {
  assert.deepEqual(validateRequestedRefund("", "100.00"), []); // no request is fine
  assert.deepEqual(validateRequestedRefund("50.00", "100.00"), []);
  assert.deepEqual(validateRequestedRefund("100.00", "100.00"), []);
  assert.deepEqual(validateRequestedRefund("150.00", "100.00"), [ISSUE_COPY.refundOverRemaining]);
});

test("a partial refund must be positive and within the remaining refundable amount", () => {
  assert.deepEqual(validatePartialRefund("40.00", "100.00"), []);
  assert.deepEqual(validatePartialRefund("", "100.00"), [ISSUE_COPY.amountRequired]);
  assert.deepEqual(validatePartialRefund("0", "100.00"), [ISSUE_COPY.amountRequired]);
  assert.deepEqual(validatePartialRefund("120.00", "100.00"), [ISSUE_COPY.refundOverRemaining]);
});

test("the over-remaining message is exactly the agreed Turkish copy", () => {
  assert.equal(ISSUE_COPY.refundOverRemaining, "İade tutarı kalan iade edilebilir tutarı aşamaz.");
  assert.equal(ISSUE_COPY.stockNotRestored, "Hazırlanmış siparişin stoğu otomatik geri alınmaz.");
});

test("only full/partial refund resolutions need the refund permission", () => {
  assert.equal(resolutionNeedsRefundPermission("FULL_REFUND"), true);
  assert.equal(resolutionNeedsRefundPermission("PARTIAL_REFUND"), true);
  assert.equal(resolutionNeedsRefundPermission("NO_REFUND"), false);
  assert.equal(resolutionNeedsRefundPermission("CANCEL_ONLY"), false);
});

// ── The uncertain message never reads as a plain failure ──────────────────────

test("the uncertain copy invites a status check, not a blind retry", () => {
  assert.equal(/başarısız/i.test(ISSUE_COPY.uncertain), false);
  assert.ok(ISSUE_COPY.uncertain.includes("sipariş durumunu kontrol edin"));
});

// ── Idempotency fingerprints ──────────────────────────────────────────────────

test("an unchanged create command reuses its fingerprint (note null vs absent match)", () => {
  const a = fingerprintIssueCommand({ kind: "issue_create", orderId: 5, issueType: "OTHER", reason: "x" });
  const b = fingerprintIssueCommand({ kind: "issue_create", orderId: 5, issueType: "OTHER", reason: "x", note: null });
  assert.equal(a, b);
});

test("editing the resolution or amount mints a fresh fingerprint", () => {
  const base = { kind: "issue_resolve" as const, issueId: 7, resolutionType: "PARTIAL_REFUND", reason: "r" };
  const first = fingerprintIssueCommand({ ...base, approvedRefund: "40.00" });
  assert.notEqual(first, fingerprintIssueCommand({ ...base, approvedRefund: "50.00" }));
  assert.notEqual(first, fingerprintIssueCommand({ ...base, resolutionType: "FULL_REFUND", approvedRefund: "40.00" }));
  assert.notEqual(first, fingerprintIssueCommand({ ...base, issueId: 8, approvedRefund: "40.00" }));
});

test("a create and a resolve command never share a fingerprint", () => {
  const create = fingerprintIssueCommand({ kind: "issue_create", orderId: 1, issueType: "OTHER", reason: "r" });
  const resolve = fingerprintIssueCommand({ kind: "issue_resolve", issueId: 1, resolutionType: "NO_REFUND", reason: "r" });
  assert.notEqual(create, resolve);
});
