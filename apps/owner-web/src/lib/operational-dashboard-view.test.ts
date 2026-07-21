/**
 * Owner operational dashboard, as an owner reads it.
 *
 * The owner opens this to answer "how is today going?" in one glance. So the raw
 * wire values — English enums, money-as-strings, null durations — must never reach
 * a card: every test below defends the Turkish rendering and the safe-empty state.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/operational-dashboard-view.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DASHBOARD_COPY,
  SEVERITY_LABEL,
  formatBusinessDate,
  formatCount,
  formatDuration,
  formatMoney,
  severityLabel,
  toAttentionRow,
  toAttentionRows,
  type AttentionItem,
  type OperationalDashboard,
} from "./operational-dashboard-view.ts";

const fullDashboard: OperationalDashboard = {
  business_date: "2026-07-21",
  as_of: "2026-07-21T10:00:00Z",
  store_id: 1,
  orders: {
    active_count: 4,
    waiting_count: 1,
    in_prep_count: 2,
    ready_count: 1,
    completed_today: 12,
    cancelled_today: 1,
  },
  payments: {
    currency: "TRY",
    gross_collected_today: "1250.00",
    refunds_today: "50.00",
    net_collected_today: "1200.00",
    unpaid_or_partially_paid_orders: 3,
  },
  kitchen: {
    active_orders: 4,
    delayed_orders: 2,
    average_prep_seconds_today: 365,
    average_time_to_ready_seconds_today: 500,
    p95_prep_seconds_today: 900,
  },
  issues: { open_count: 2, resolved_today: 5, refund_amount_today: "75.00" },
  shifts: {
    open_shift_count: 1,
    closed_today: 2,
    total_discrepancy_today: "-10.00",
    shifts_with_discrepancy_today: 1,
  },
  inventory: {
    out_of_stock_count: 1,
    below_reserved_count: 0,
    critical_count: 3,
    low_count: 4,
    healthy_count: 20,
    not_configured_count: 2,
  },
  attention: [
    { severity: "critical", code: "OUT_OF_STOCK", count: 1, target_route: "/inventory" },
    { severity: "warning", code: "OPEN_ISSUES", count: 2, target_route: "/order-issues" },
    { severity: "info", code: "OPEN_SHIFTS", count: 1, target_route: "/shifts" },
  ],
};

// 1 — Turkish section titles
test("section and card titles are Turkish", () => {
  assert.equal(DASHBOARD_COPY.sectionTitle, "Operasyon özeti");
  assert.equal(DASHBOARD_COPY.cards.payments, "Günlük ciro");
  assert.equal(DASHBOARD_COPY.cards.kitchen, "Mutfak temposu");
  assert.equal(DASHBOARD_COPY.cards.shifts, "Kasa vardiyaları");
  assert.equal(DASHBOARD_COPY.cards.attention, "Dikkat gerektirenler");
});

// 2 — payment card renders gross/refund/net values
test("payment values render as tr-TR money", () => {
  assert.equal(formatMoney(fullDashboard.payments.gross_collected_today), "1.250,00 ₺");
  assert.equal(formatMoney(fullDashboard.payments.refunds_today), "50,00 ₺");
  assert.equal(formatMoney(fullDashboard.payments.net_collected_today), "1.200,00 ₺");
});

// 3 — kitchen card renders delayed / average prep safely
test("kitchen delayed count and average prep render safely", () => {
  assert.equal(formatCount(fullDashboard.kitchen.delayed_orders), "2");
  assert.equal(formatDuration(fullDashboard.kitchen.average_prep_seconds_today), "6 dk 5 sn");
  // No completed data → "—", never a fabricated 0.
  assert.equal(formatDuration(null), "—");
  assert.equal(formatDuration(45), "45 sn");
});

// 4 — issue card renders open / resolved counts
test("issue counts render", () => {
  assert.equal(formatCount(fullDashboard.issues.open_count), "2");
  assert.equal(formatCount(fullDashboard.issues.resolved_today), "5");
  assert.equal(formatMoney(fullDashboard.issues.refund_amount_today), "75,00 ₺");
});

// 5 — shift card renders open / closed / discrepancy
test("shift counts and discrepancy render", () => {
  assert.equal(formatCount(fullDashboard.shifts.open_shift_count), "1");
  assert.equal(formatCount(fullDashboard.shifts.closed_today), "2");
  assert.equal(formatMoney(fullDashboard.shifts.total_discrepancy_today), "-10,00 ₺");
});

// 6 — inventory card renders alert counts
test("inventory alert counts render", () => {
  assert.equal(formatCount(fullDashboard.inventory.out_of_stock_count), "1");
  assert.equal(formatCount(fullDashboard.inventory.critical_count), "3");
  assert.equal(formatCount(fullDashboard.inventory.low_count), "4");
});

// 7 — attention list renders deterministic Turkish items
test("attention rows are Turkish and most-urgent first", () => {
  const rows = toAttentionRows(fullDashboard.attention);
  assert.equal(rows.length, 3);
  assert.equal(rows[0].title, "Tükenen stok");
  assert.equal(rows[0].severityLabel, "Acil");
  assert.equal(rows[1].title, "Açık sorunlu sipariş");
  assert.equal(rows[2].title, "Açık vardiya");
  // Deterministic non-increasing severity rank.
  const ranks = rows.map((r) => r.rank);
  assert.deepEqual(ranks, [...ranks].sort((a, b) => b - a));
});

// 8 — zero / empty state renders safely
test("empty dashboard renders safe zeros, never null or NaN", () => {
  assert.equal(formatMoney("0.00"), "0,00 ₺");
  assert.equal(formatMoney(null), "0,00 ₺");
  assert.equal(formatMoney(""), "0,00 ₺");
  assert.equal(formatCount(0), "0");
  assert.equal(formatCount(null), "—");
  assert.equal(toAttentionRows([]).length, 0);
});

// 9 — API error state copy is Turkish
test("error and empty copy are Turkish", () => {
  assert.equal(DASHBOARD_COPY.loadError, "Veriler yüklenemedi. Lütfen daha sonra tekrar deneyin.");
  assert.equal(DASHBOARD_COPY.empty, "Bugün için veri yok.");
  assert.equal(DASHBOARD_COPY.noAttention, "Şu an dikkat gerektiren bir durum yok.");
});

// 10 — no raw enum values are displayed
test("severities and unknown codes never leak raw enums", () => {
  assert.equal(severityLabel("critical"), "Acil");
  assert.equal(severityLabel("warning"), "Uyarı");
  assert.equal(severityLabel("info"), "Bilgi");
  // An unknown severity degrades to a Turkish default, not the raw string.
  assert.equal(severityLabel("SOMETHING_NEW"), "Bilgi");
  assert.equal(SEVERITY_LABEL.critical, "Acil");

  const unknown: AttentionItem = {
    severity: "warning",
    code: "SOME_FUTURE_CODE",
    count: 1,
    target_route: null,
  };
  const row = toAttentionRow(unknown);
  assert.equal(row.title, "Dikkat");
  assert.notEqual(row.title, "SOME_FUTURE_CODE");
  assert.equal(row.description, "Bu alan dikkat gerektiriyor.");
});

// 11 — detail links point to existing owner-web routes
test("attention target routes are existing owner-web pages", () => {
  const existing = new Set(["/inventory", "/kitchen", "/order-issues", "/shifts"]);
  for (const item of fullDashboard.attention) {
    const row = toAttentionRow(item);
    if (row.targetRoute !== null) {
      assert.ok(existing.has(row.targetRoute), `${row.targetRoute} is a known route`);
    }
  }
  // A code with no owning page (unpaid orders) yields no link, never a broken one.
  const unpaid: AttentionItem = {
    severity: "info",
    code: "UNPAID_ORDERS",
    count: 3,
    target_route: null,
  };
  assert.equal(toAttentionRow(unpaid).targetRoute, null);
});

test("business date formats to Turkish long date", () => {
  assert.equal(formatBusinessDate("2026-07-21"), "21 Temmuz 2026");
  assert.equal(formatBusinessDate(null), "");
  assert.equal(formatBusinessDate("garbage"), "");
});
