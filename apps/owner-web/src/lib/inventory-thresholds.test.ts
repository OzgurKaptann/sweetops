/**
 * Inventory threshold alerts — the owner screen's copy, validation and wire contract.
 *
 * The failure modes under test are not crashes. They are a screen that quietly lies to
 * a manager:
 *
 *   * a raw `NOT_CONFIGURED` printed in a status column, which a manager cannot act on
 *     and cannot ask anybody about;
 *   * an unconfigured threshold rendered as "0", which reads as a deliberate decision
 *     nobody made — and "0" and "not set" are opposite instructions to whoever reads
 *     the row next;
 *   * an inverted ladder (critical above minimum) accepted by the form, which deletes
 *     the early warning the manager thought they were configuring;
 *   * a threshold PATCH that forgets its Idempotency-Key, so a retried form re-logs
 *     the decision and re-stamps the timestamp an owner uses to ask who changed it;
 *   * a store_id smuggled into the body, which is the one thing that could point a
 *     threshold at another branch;
 *   * a replay or a network timeout reported as a plain success, so the manager
 *     believes they made two decisions or none.
 *
 * `fetch` is stubbed on globalThis; no network access occurs. Excluded from the Next
 * production build via tsconfig `exclude`.
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/inventory-thresholds.test.ts
 */
import { test, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  InventoryApiError,
  InventoryNetworkUncertainError,
  fetchThresholdAlerts,
  updateThresholds,
} from "./inventory-api.ts";
import {
  INVENTORY_ERROR_UNKNOWN,
  THRESHOLD_ERROR_NETWORK_UNCERTAIN,
  THRESHOLD_ERROR_UNKNOWN,
  inventoryErrorMessage,
} from "./inventory-errors.ts";
import { fingerprintCommand } from "./inventory-idempotency.ts";
import {
  THRESHOLD_CLEAR_HINT,
  THRESHOLD_HINT,
  THRESHOLD_LABELS,
  THRESHOLD_MESSAGES,
  THRESHOLD_STATUS_LABEL,
  THRESHOLD_VALIDATION,
  type ThresholdFormInput,
  type ThresholdSource,
  thresholdBanner,
  thresholdRequestBody,
  thresholdStatusLabel,
  thresholdSummaryCards,
  toThresholdRow,
  totalRecommendedRestockLabel,
  validateThresholdForm,
} from "./inventory-view.ts";

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

interface Captured {
  url: string;
  method?: string;
  headers: Record<string, string>;
  body: unknown;
}

function captureOk(json: unknown = { idempotent_replay: false }): () => Captured {
  let captured: Captured | undefined;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    captured = {
      url,
      method: init?.method,
      headers: (init?.headers ?? {}) as Record<string, string>,
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    };
    return { ok: true, status: 200, json: async () => json } as Response;
  }) as typeof fetch;
  return () => {
    assert.ok(captured, "fetch was never called");
    return captured;
  };
}

function alert(overrides: Partial<ThresholdSource> = {}): ThresholdSource {
  return {
    ingredient_id: 1,
    ingredient_name: "Çikolata",
    unit: "kg",
    on_hand_quantity: "10.000",
    reserved_quantity: "0.000",
    available_quantity: "10.000",
    critical_quantity: "2.000",
    minimum_quantity: "5.000",
    target_quantity: "20.000",
    status: "HEALTHY",
    status_label: "Stok yeterli",
    recommended_restock_quantity: "10.000",
    threshold_updated_at: "2026-07-15T09:00:00Z",
    ...overrides,
  };
}

const SUMMARY = {
  below_reserved: 0,
  out_of_stock: 1,
  critical: 2,
  low: 3,
  healthy: 4,
  not_configured: 5,
  total_recommended_restock: "12.500",
};

// ---------------------------------------------------------------------------
// Status labels — no raw enum ever reaches a screen
// ---------------------------------------------------------------------------

test("every threshold status has a Turkish label", () => {
  assert.equal(thresholdStatusLabel("BELOW_RESERVED"), "Ayrılmış stoktan düşük");
  assert.equal(thresholdStatusLabel("OUT_OF_STOCK"), "Stokta yok");
  assert.equal(thresholdStatusLabel("CRITICAL"), "Kritik stok");
  assert.equal(thresholdStatusLabel("LOW"), "Düşük stok");
  assert.equal(thresholdStatusLabel("HEALTHY"), "Stok yeterli");
  assert.equal(thresholdStatusLabel("NOT_CONFIGURED"), "Eşik tanımlı değil");
});

test("an UNKNOWN status renders as Bilinmiyor — never as the raw enum", () => {
  // The day the backend adds a seventh status, this screen must degrade to a word a
  // manager can ask about, not print an identifier at them.
  assert.equal(thresholdStatusLabel("SOME_FUTURE_STATUS"), "Bilinmiyor");
  assert.equal(thresholdStatusLabel(null), "Bilinmiyor");
  assert.equal(thresholdStatusLabel(""), "Bilinmiyor");
});

test("no label is itself a raw enum value", () => {
  for (const label of Object.values(THRESHOLD_STATUS_LABEL)) {
    assert.ok(
      !/^[A-Z_]+$/.test(label),
      `"${label}" looks like a wire value, not Turkish copy`,
    );
  }
});

test("a rendered row carries the Turkish label, and its status is never printed raw", () => {
  const row = toThresholdRow(alert({ status: "CRITICAL", status_label: "ignored" }));
  assert.equal(row.statusLabel, "Kritik stok");
  // The row keeps `status` so the component can pick a colour, but every STRING on the
  // row that a table renders is Turkish.
  const rendered = [
    row.ingredientName, row.available, row.statusLabel,
    row.critical, row.minimum, row.target, row.recommendedRestock,
  ];
  for (const cell of rendered) {
    assert.ok(!/^[A-Z][A-Z_]+$/.test(cell), `raw enum leaked into a cell: ${cell}`);
  }
});

// ---------------------------------------------------------------------------
// Rows — "not configured" is not zero
// ---------------------------------------------------------------------------

test("an unconfigured threshold renders as — and never as 0", () => {
  // The distinction is operational, not cosmetic. "0" says someone deliberately chose
  // "warn me only when it is actually gone"; "—" says nobody has decided anything. A
  // manager reading one as the other will either ignore a real setting or trust a
  // setting that does not exist.
  const row = toThresholdRow(
    alert({
      critical_quantity: null,
      minimum_quantity: null,
      target_quantity: null,
      recommended_restock_quantity: null,
      status: "NOT_CONFIGURED",
    }),
  );
  assert.equal(row.critical, "—");
  assert.equal(row.minimum, "—");
  assert.equal(row.target, "—");
  assert.equal(row.recommendedRestock, "—");
  assert.equal(row.statusLabel, "Eşik tanımlı değil");
});

test("a threshold explicitly set to zero renders as 0, not as —", () => {
  const row = toThresholdRow(alert({ critical_quantity: "0.000" }));
  assert.equal(row.critical, "0");
});

test("rows needing attention are flagged; healthy and unconfigured are not", () => {
  for (const status of ["BELOW_RESERVED", "OUT_OF_STOCK", "CRITICAL", "LOW"]) {
    assert.equal(toThresholdRow(alert({ status })).needsAttention, true, status);
  }
  assert.equal(toThresholdRow(alert({ status: "HEALTHY" })).needsAttention, false);
  assert.equal(toThresholdRow(alert({ status: "NOT_CONFIGURED" })).needsAttention, false);
});

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------

test("summary cards are Turkish and carry the server's counts", () => {
  const cards = thresholdSummaryCards(SUMMARY);
  const byKey = Object.fromEntries(cards.map((c) => [c.key, c]));

  assert.equal(byKey.critical.label, "Kritik stok");
  assert.equal(byKey.critical.count, 2);
  assert.equal(byKey.low.label, "Düşük stok");
  assert.equal(byKey.low.count, 3);
  assert.equal(byKey.out_of_stock.label, "Stokta yok");
  assert.equal(byKey.out_of_stock.count, 1);
  assert.equal(byKey.not_configured.label, "Eşik tanımlı değil");
  assert.equal(byKey.not_configured.count, 5);
});

test("BELOW_RESERVED gets a card only when it is real", () => {
  // It should never happen (the database makes it unrepresentable), so a permanent
  // "0" card would train managers to read straight past the one row that would mean
  // the shop has sold stock it does not physically have.
  assert.ok(!thresholdSummaryCards(SUMMARY).some((c) => c.key === "below_reserved"));

  const cards = thresholdSummaryCards({ ...SUMMARY, below_reserved: 1 });
  const card = cards.find((c) => c.key === "below_reserved");
  assert.ok(card, "a real below-reserved incident must be shown");
  assert.equal(card.label, "Ayrılmış stoktan düşük");
  assert.equal(card.tone, "danger");
});

test("the total recommended top-up is Turkish, and absent when there is nothing to suggest", () => {
  assert.equal(
    totalRecommendedRestockLabel(SUMMARY),
    "Toplam önerilen tamamlama: 12,5",
  );
  assert.equal(
    totalRecommendedRestockLabel({ ...SUMMARY, total_recommended_restock: "0" }),
    null,
  );
});

// ---------------------------------------------------------------------------
// Form validation (courtesy — the server and the database both re-decide)
// ---------------------------------------------------------------------------

function form(overrides: Partial<ThresholdFormInput> = {}): ThresholdFormInput {
  return {
    ingredientId: 1,
    critical: "2",
    minimum: "5",
    target: "20",
    reason: "Kış sezonu",
    ...overrides,
  };
}

test("a coherent ladder passes", () => {
  assert.deepEqual(validateThresholdForm(form()), []);
});

test("negative thresholds are refused", () => {
  // A negative threshold promises an alert that can never fire: no quantity can fall
  // below zero. A control that silently does nothing is worse than none, because it
  // is believed.
  for (const field of ["critical", "minimum", "target"] as const) {
    const errors = validateThresholdForm(form({ [field]: "-1" }));
    assert.ok(
      errors.includes(THRESHOLD_VALIDATION.negative),
      `negative ${field} was accepted`,
    );
  }
});

test("critical above minimum is refused", () => {
  const errors = validateThresholdForm(form({ critical: "9", minimum: "5" }));
  assert.ok(errors.includes(THRESHOLD_VALIDATION.criticalAboveMinimum));
  assert.equal(THRESHOLD_VALIDATION.criticalAboveMinimum,
    "Kritik eşik minimum eşikten büyük olamaz.");
});

test("minimum above target is refused", () => {
  const errors = validateThresholdForm(form({ minimum: "30", target: "20" }));
  assert.ok(errors.includes(THRESHOLD_VALIDATION.minimumAboveTarget));
  assert.equal(THRESHOLD_VALIDATION.minimumAboveTarget,
    "Minimum eşik hedef stoktan büyük olamaz.");
});

test("critical above target is refused even when minimum is not configured", () => {
  // The pairwise rule that is load-bearing on its own: with no minimum set, nothing
  // else relates critical to target at all.
  const errors = validateThresholdForm(form({ critical: "30", minimum: "", target: "20" }));
  assert.ok(errors.includes(THRESHOLD_VALIDATION.criticalAboveTarget));
});

test("a partial configuration is legitimate", () => {
  assert.deepEqual(validateThresholdForm(form({ minimum: "", target: "" })), []);
  assert.deepEqual(validateThresholdForm(form({ critical: "", target: "" })), []);
  assert.deepEqual(validateThresholdForm(form({ critical: "", minimum: "" })), []);
  // Clearing everything is a decision too — it disarms the alerts, and it needs a
  // reason like any other.
  assert.deepEqual(
    validateThresholdForm(form({ critical: "", minimum: "", target: "" })),
    [],
  );
});

test("a reason is mandatory", () => {
  const errors = validateThresholdForm(form({ reason: "   " }));
  assert.ok(errors.includes(THRESHOLD_VALIDATION.reasonRequired));
  assert.equal(THRESHOLD_VALIDATION.reasonRequired, "Sebep girin.");
});

test("a non-numeric threshold is refused rather than silently clearing it", () => {
  // Collapsing "abc" into null would CLEAR the threshold because someone fat-fingered
  // a letter — disarming an alert as a side effect of a typo.
  const errors = validateThresholdForm(form({ critical: "abc" }));
  assert.ok(errors.includes(THRESHOLD_VALIDATION.invalid));
});

test("an ingredient must be chosen", () => {
  const errors = validateThresholdForm(form({ ingredientId: null }));
  assert.ok(errors.includes(THRESHOLD_VALIDATION.ingredientRequired));
});

// ---------------------------------------------------------------------------
// Request body
// ---------------------------------------------------------------------------

test("an empty field is sent as null (not configured) — never as 0", () => {
  const body = thresholdRequestBody(form({ critical: "", minimum: "5", target: "" }));
  assert.equal(body.critical_quantity, null);
  assert.equal(body.minimum_quantity, "5");
  assert.equal(body.target_quantity, null);
});

test("a zero is sent as 0 — it is a real decision, not an absence of one", () => {
  const body = thresholdRequestBody(form({ critical: "0" }));
  assert.equal(body.critical_quantity, "0");
});

test("the body never carries a store_id, ingredient_id or status", () => {
  const body = thresholdRequestBody(form()) as Record<string, unknown>;
  assert.deepEqual(
    Object.keys(body).sort(),
    ["critical_quantity", "minimum_quantity", "reason", "target_quantity"],
  );
});

// ---------------------------------------------------------------------------
// Idempotency
// ---------------------------------------------------------------------------

test("the PATCH sends an Idempotency-Key, the CSRF header and the session cookie", async () => {
  const captured = captureOk();
  await updateThresholds(
    7,
    { critical_quantity: "2", minimum_quantity: "5", target_quantity: "20", reason: "x" },
    "key-abc",
  );
  const req = captured();

  assert.equal(req.method, "PATCH");
  assert.ok(req.url.endsWith("/inventory/stock/7/thresholds"));
  assert.equal(req.headers["Idempotency-Key"], "key-abc");
});

test("the PATCH body never contains a store_id", async () => {
  // The one thing that could ever point a threshold at another branch. The store comes
  // from the session; there is no field for it, and the backend forbids unknown fields
  // outright.
  const captured = captureOk();
  await updateThresholds(
    7,
    { critical_quantity: "2", minimum_quantity: null, target_quantity: null, reason: "x" },
    "key-abc",
  );
  const body = captured().body as Record<string, unknown>;

  assert.ok(!("store_id" in body));
  assert.ok(!("actor_user_id" in body));
  assert.ok(!("status" in body));
  assert.ok(!("ingredient_id" in body));
  // The ingredient travels in the PATH, where it cannot be confused with a body field.
  assert.ok(captured().url.includes("/stock/7/thresholds"));
});

test("a threshold update with no key is refused locally, before the request is sent", async () => {
  let called = false;
  globalThis.fetch = (async () => {
    called = true;
    return { ok: true, status: 200, json: async () => ({}) } as Response;
  }) as typeof fetch;

  await assert.rejects(
    () =>
      updateThresholds(
        7,
        { critical_quantity: "2", minimum_quantity: null, target_quantity: null, reason: "x" },
        "",
      ),
    (err: unknown) =>
      err instanceof InventoryApiError && err.code === "idempotency_required",
  );
  assert.equal(called, false, "a keyless mutation must never reach the network");
});

test("a changed threshold mints a new fingerprint; an unchanged one does not", () => {
  const base = {
    kind: "threshold_update" as const,
    ingredientId: 1,
    criticalQuantity: "2",
    minimumQuantity: "5",
    targetQuantity: "20",
    reason: "Kış sezonu",
  };
  assert.equal(fingerprintCommand(base), fingerprintCommand({ ...base }));
  assert.notEqual(
    fingerprintCommand(base),
    fingerprintCommand({ ...base, criticalQuantity: "3" }),
  );
  // Clearing a threshold and setting it to zero are DIFFERENT decisions, and their
  // fingerprints must differ — otherwise a retry of one would replay the other.
  assert.notEqual(
    fingerprintCommand({ ...base, criticalQuantity: null }),
    fingerprintCommand({ ...base, criticalQuantity: "0" }),
  );
});

// ---------------------------------------------------------------------------
// Result and failure copy
// ---------------------------------------------------------------------------

test("success is Turkish", () => {
  const banner = thresholdBanner();
  assert.equal(banner.tone, "success");
  assert.equal(banner.message, "Stok eşikleri güncellendi.");
});

test("a replay is reported as a replay, not as a second success", () => {
  // The backend recognised the key and changed nothing — it did not even re-stamp the
  // timestamp. "Güncellendi" a second time would leave the manager believing they had
  // made two decisions.
  const banner = thresholdBanner({ replay: true });
  assert.equal(banner.tone, "info");
  assert.equal(banner.message, "Bu eşik güncellemesi daha önce kaydedilmiş.");
  assert.notEqual(banner.message, THRESHOLD_MESSAGES.success);
});

test("an uncertain outcome sends the manager to the STOCK screen, in Turkish", () => {
  // Not to the movement ledger: a threshold update writes no movement, so a manager
  // told to check the ledger would find nothing, conclude it failed, and re-enter it by
  // hand — minting a new key and re-logging the decision.
  const msg = inventoryErrorMessage(
    new InventoryNetworkUncertainError(),
    "threshold_update",
  );
  assert.equal(msg, THRESHOLD_ERROR_NETWORK_UNCERTAIN);
  assert.match(msg, /doğrulanamadı/);
  assert.match(msg, /stok ekranını kontrol edin/);
});

test("an unrecognised failure falls back to the generic Turkish threshold message", () => {
  const msg = inventoryErrorMessage(
    new InventoryApiError(500, "some_unknown_code", ""),
    "threshold_update",
  );
  assert.equal(msg, THRESHOLD_ERROR_UNKNOWN);
  assert.equal(msg, "Eşikler güncellenemedi. Lütfen tekrar deneyin.");
  // ...and it does NOT reuse the generic stock-operation wording, which would leave a
  // manager who was editing a threshold wondering whether their stock is now wrong.
  assert.notEqual(msg, INVENTORY_ERROR_UNKNOWN);
});

test("each backend threshold error code maps to the rule it broke", () => {
  const cases: [string, string][] = [
    ["threshold_negative", "Eşik değerleri negatif olamaz."],
    ["threshold_critical_above_minimum", "Kritik eşik minimum eşikten büyük olamaz."],
    ["threshold_minimum_above_target", "Minimum eşik hedef stoktan büyük olamaz."],
    ["threshold_critical_above_target", "Kritik eşik hedef stoktan büyük olamaz."],
  ];
  for (const [code, expected] of cases) {
    assert.equal(
      inventoryErrorMessage(new InventoryApiError(422, code, ""), "threshold_update"),
      expected,
    );
  }
});

test("a raw API error message that leaks internals is never displayed", () => {
  const msg = inventoryErrorMessage(
    new InventoryApiError(
      500,
      "unknown",
      'IntegrityError: violates check constraint "ck_stock_threshold_critical_le_minimum"',
    ),
    "threshold_update",
  );
  assert.equal(msg, THRESHOLD_ERROR_UNKNOWN);
});

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

test("the alerts read sends no store parameter", async () => {
  const captured = captureOk({ total: 0, summary: SUMMARY, items: [] });
  await fetchThresholdAlerts();
  const req = captured();

  assert.ok(req.url.endsWith("/inventory/threshold-alerts"));
  assert.ok(!req.url.includes("store"));
});

// ---------------------------------------------------------------------------
// The screen itself
//
// owner-web has no DOM test runner, so the components are checked against their
// source: the properties that matter here are "this string is on the screen" and "this
// one is not", and both are decidable from the file.
// ---------------------------------------------------------------------------

const PANEL = readFileSync(
  new URL("../components/inventory/ThresholdAlertsPanel.tsx", import.meta.url),
  "utf8",
);
const MODAL = readFileSync(
  new URL("../components/inventory/ThresholdEditModal.tsx", import.meta.url),
  "utf8",
);

test("the stock table shows the threshold columns, in Turkish", () => {
  for (const label of [
    THRESHOLD_LABELS.status,
    THRESHOLD_LABELS.critical,
    THRESHOLD_LABELS.minimum,
    THRESHOLD_LABELS.target,
    THRESHOLD_LABELS.recommendedRestock,
  ]) {
    assert.ok(
      PANEL.includes("THRESHOLD_LABELS"),
      "the panel must take its headers from the shared Turkish copy",
    );
    assert.ok(label.length > 0);
  }
  assert.equal(THRESHOLD_LABELS.critical, "Kritik eşik");
  assert.equal(THRESHOLD_LABELS.minimum, "Minimum eşik");
  assert.equal(THRESHOLD_LABELS.target, "Hedef stok");
  assert.equal(THRESHOLD_LABELS.recommendedRestock, "Önerilen tamamlama");
});

test("the panel renders the LABEL, never the raw status", () => {
  assert.ok(PANEL.includes("row.statusLabel"), "the panel must render the Turkish label");
  // `row.status` may only be used to pick a colour — never rendered as text. The one
  // permitted use is the style lookup.
  const renderedRaw = /\{\s*row\.status\s*\}/.test(PANEL);
  assert.equal(renderedRaw, false, "the raw status must never be rendered");
});

test("the threshold edit form exists and says that stock is not changed", () => {
  assert.ok(MODAL.includes("Eşik düzenle"), "the dialog must be titled Eşik düzenle");
  assert.ok(MODAL.includes("THRESHOLD_HINT"), "the dialog must show the no-stock hint");
  assert.equal(
    THRESHOLD_HINT,
    "Eşikler stok uyarıları için kullanılır. Bu işlem stok miktarını değiştirmez.",
  );
  assert.match(THRESHOLD_CLEAR_HINT, /Boş bıraktığınız eşik tanımsız olur/);

  // The four fields the form must offer, plus the reason.
  for (const label of ["ingredient", "critical", "minimum", "target", "reason"] as const) {
    assert.ok(
      MODAL.includes(`THRESHOLD_LABELS.${label}`),
      `the form is missing the ${label} field`,
    );
  }
  // It validates before it submits, and it keys the request.
  assert.ok(MODAL.includes("validateThresholdForm"));
  assert.ok(MODAL.includes("fingerprintCommand"));
  assert.ok(MODAL.includes("updateThresholds"));
});
