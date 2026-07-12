/**
 * Owner-facing enum → Turkish label mapping.
 *
 * The sharp edge here is inventory movement types. WASTE and TRANSFER_OUT both
 * decrease a branch's stock, and PURCHASE_RECEIPT and TRANSFER_IN both increase
 * it — but they mean completely different things to the person reading the
 * report. Mislabel a transfer as "Fire" and the owner sees waste that never
 * happened; mislabel it as "Mal kabul" and they see a purchase nobody made.
 * These tests pin those four apart.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/labels.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DECISION_STATUS_LABEL,
  DECISION_TYPE_LABEL,
  MOVEMENT_TYPE_LABEL,
  ORDER_STATUS_LABEL,
  RESOLUTION_QUALITY_LABEL,
  dataQualityLabel,
  decisionStatusLabel,
  decisionTypeLabel,
  loadLevelLabel,
  movementTypeLabel,
  orderStatusLabel,
  resolutionQualityLabel,
  slaSeverityLabel,
} from "./labels.ts";

// ── Inventory movement types ─────────────────────────────────────────────────

test("transfer movement types map to branch-transfer labels", () => {
  assert.equal(movementTypeLabel("TRANSFER_OUT"), "Şubeden çıkış");
  assert.equal(movementTypeLabel("TRANSFER_IN"), "Şubeye giriş");
});

test("lifecycle movement types map to Turkish labels", () => {
  assert.equal(movementTypeLabel("RESERVATION_CREATED"), "Stok ayrıldı");
  assert.equal(movementTypeLabel("RESERVATION_RELEASED"), "Ayrılan stok bırakıldı");
  assert.equal(movementTypeLabel("CONSUMPTION"), "Tüketim");
  assert.equal(movementTypeLabel("WASTE"), "Fire");
  assert.equal(movementTypeLabel("RETURNED"), "İade edilen stok");
  assert.equal(movementTypeLabel("MANUAL_ADJUSTMENT"), "Manuel düzeltme");
  assert.equal(movementTypeLabel("PURCHASE_RECEIPT"), "Mal kabul");
});

test("a transfer is never labelled as waste or as a purchase", () => {
  // The whole point of the separate labels: a van leaving for another branch is
  // not stock in the bin, and stock arriving from one is not stock we bought.
  assert.notEqual(movementTypeLabel("TRANSFER_OUT"), movementTypeLabel("WASTE"));
  assert.notEqual(movementTypeLabel("TRANSFER_IN"), movementTypeLabel("PURCHASE_RECEIPT"));
  assert.ok(!movementTypeLabel("TRANSFER_OUT").includes("Fire"));
  assert.ok(!movementTypeLabel("TRANSFER_IN").includes("Mal kabul"));
});

test("every movement_type the API can send has a label", () => {
  // Mirrors the backend movement_type enum, transfer types included.
  const wireValues = [
    "RESERVATION_CREATED",
    "RESERVATION_RELEASED",
    "CONSUMPTION",
    "WASTE",
    "RETURNED",
    "MANUAL_ADJUSTMENT",
    "PURCHASE_RECEIPT",
    "TRANSFER_OUT",
    "TRANSFER_IN",
  ];
  for (const wire of wireValues) {
    assert.ok(MOVEMENT_TYPE_LABEL[wire], `no Turkish label for ${wire}`);
  }
});

// ── Order status ─────────────────────────────────────────────────────────────

test("order statuses map to Turkish labels", () => {
  assert.equal(orderStatusLabel("NEW"), "Bekliyor");
  assert.equal(orderStatusLabel("IN_PREP"), "Hazırlanıyor");
  assert.equal(orderStatusLabel("READY"), "Hazır");
  assert.equal(orderStatusLabel("DELIVERED"), "Teslim edildi");
  assert.equal(orderStatusLabel("CANCELLED"), "İptal edildi");
});

// ── Decisions ────────────────────────────────────────────────────────────────

test("decision types and statuses map to Turkish labels", () => {
  assert.equal(decisionTypeLabel("stock_risk"), "Stok riski");
  assert.equal(decisionTypeLabel("sla_risk"), "Hazırlık süresi riski");
  assert.equal(decisionStatusLabel("pending"), "Bekliyor");
  assert.equal(decisionStatusLabel("acknowledged"), "Görüldü");
  assert.equal(decisionStatusLabel("completed"), "Tamamlandı");
  assert.equal(decisionStatusLabel("dismissed"), "Kapatıldı");
});

test("every decision type the engine emits has a label", () => {
  const wireValues = [
    "stock_risk",
    "demand_spike",
    "slow_moving",
    "sla_risk",
    "revenue_anomaly",
    "metric_combo_health",
    "metric_upsell_visibility",
    "metric_owner_engagement",
    "metric_kitchen_performance",
  ];
  for (const wire of wireValues) {
    assert.ok(DECISION_TYPE_LABEL[wire], `no Turkish label for ${wire}`);
  }
});

test("resolution qualities map to Turkish labels", () => {
  assert.equal(resolutionQualityLabel("good"), "Sorun çözüldü");
  assert.equal(resolutionQualityLabel("partial"), "Kısmen çözüldü");
  assert.equal(resolutionQualityLabel("failed"), "Çözülemedi");
});

// ── Operational bands ────────────────────────────────────────────────────────

test("load levels and SLA severities map to Turkish labels", () => {
  assert.equal(loadLevelLabel("low"), "Sakin");
  assert.equal(loadLevelLabel("medium"), "Normal");
  assert.equal(loadLevelLabel("high"), "Yoğun");
  assert.equal(slaSeverityLabel("ok"), "Zamanında");
  assert.equal(slaSeverityLabel("warning"), "Süre doluyor");
  assert.equal(slaSeverityLabel("critical"), "Süre aşıldı");
});

test("data quality states map to Turkish labels", () => {
  assert.equal(dataQualityLabel("no_data"), "veri yok");
  assert.equal(dataQualityLabel("low_sample"), "az veri");
  assert.equal(dataQualityLabel("unreliable"), "güvenilmez");
});

// ── The property that actually matters ───────────────────────────────────────

test("no label is the raw enum value", () => {
  const maps = [
    MOVEMENT_TYPE_LABEL,
    ORDER_STATUS_LABEL,
    DECISION_TYPE_LABEL,
    DECISION_STATUS_LABEL,
    RESOLUTION_QUALITY_LABEL,
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
  assert.equal(movementTypeLabel("SHRINKAGE"), "Diğer stok hareketi");
  assert.equal(orderStatusLabel("ON_HOLD"), "Bilinmiyor");
  assert.equal(decisionStatusLabel("snoozed"), "Bilinmiyor");
  assert.equal(movementTypeLabel(null), "Diğer stok hareketi");
  assert.equal(orderStatusLabel(undefined), "Bilinmiyor");
});
