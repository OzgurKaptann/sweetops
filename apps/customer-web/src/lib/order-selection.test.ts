/**
 * A guest must never order something they did not choose.
 *
 * The regression these tests exist for was one line:
 *
 *     const product = menu?.products[0] ?? null;
 *
 * It made the product choice invisible and automatic — whatever the API happened
 * to return first was ordered, once, at that price. Nothing on screen said so,
 * and nothing in the payload distinguished "the guest chose this" from "the
 * array was in this order".
 *
 * So the assertions below are about ABSENCE as much as presence: given products
 * and no selection, the answer must be null, not products[0].
 *
 * Run with Node's built-in test runner:
 *   node --test src/lib/order-selection.test.ts
 */
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  INGREDIENT_REQUIRED_MESSAGE,
  MAX_QUANTITY,
  MENU_EMPTY_MESSAGE,
  MIN_QUANTITY,
  PRODUCT_REQUIRED_MESSAGE,
  blockingReason,
  buildOrderSubmission,
  canDecrease,
  canIncrease,
  canSubmit,
  clampQuantity,
  menuIsEmpty,
  selectedProduct,
  selectionTotal,
  stepQuantity,
  type CustomerSelection,
  type MenuProduct,
} from "./order-selection.ts";

const KLASIK: MenuProduct = { id: 7, name: "Klasik Waffle", base_price: "90.00" };
const CILEKLI: MenuProduct = { id: 9, name: "Çilekli Waffle", base_price: "110.00" };
const PRODUCTS = [KLASIK, CILEKLI];

/** A selection that is valid in every respect, to be broken one field at a time. */
function validSelection(over: Partial<CustomerSelection> = {}): CustomerSelection {
  return {
    qrToken: "tok_abc",
    contextReady: true,
    products: PRODUCTS,
    selectedProductId: KLASIK.id,
    quantity: 1,
    ingredientIds: [1, 2],
    ...over,
  };
}

/** Turkish copy must actually be Turkish, not merely "not English". */
const TURKISH_LETTERS = /[çğıöşüÇĞİÖŞÜ]/;

// ── No silent products[0] ────────────────────────────────────────────────────

test("no product selected yields no payload, not the first product", () => {
  const selection = validSelection({ selectedProductId: null });
  assert.equal(buildOrderSubmission(selection), null);
  assert.equal(canSubmit(selection), false);
});

test("a single-product menu is still not auto-selected", () => {
  // The tempting shortcut ("there is only one, so they must mean that one") is
  // the same class of bug: the order would contain something never tapped.
  const selection = validSelection({
    products: [KLASIK],
    selectedProductId: null,
  });
  assert.equal(buildOrderSubmission(selection), null);
});

test("the payload carries exactly the product that was chosen", () => {
  const submission = buildOrderSubmission(
    validSelection({ selectedProductId: CILEKLI.id }),
  );
  assert.ok(submission);
  assert.equal(submission.items.length, 1);
  // Not products[0] (Klasik) — the second one, because that is what was chosen.
  assert.equal(submission.items[0].product_id, CILEKLI.id);
  assert.notEqual(submission.items[0].product_id, PRODUCTS[0].id);
});

test("a selection the menu no longer contains does not submit", () => {
  // The product was withdrawn while the guest was picking toppings.
  const selection = validSelection({ selectedProductId: 4242 });
  assert.equal(selectedProduct(PRODUCTS, 4242), null);
  assert.equal(buildOrderSubmission(selection), null);
});

// ── Submit gating ────────────────────────────────────────────────────────────

test("submit is blocked until every precondition holds", () => {
  assert.equal(canSubmit(validSelection()), true);

  assert.equal(canSubmit(validSelection({ selectedProductId: null })), false);
  assert.equal(canSubmit(validSelection({ ingredientIds: [] })), false);
  assert.equal(canSubmit(validSelection({ qrToken: null })), false);
  assert.equal(canSubmit(validSelection({ contextReady: false })), false);
  assert.equal(canSubmit(validSelection({ products: [] })), false);
});

test("an out-of-range quantity refuses rather than quietly ordering fewer", () => {
  assert.equal(canSubmit(validSelection({ quantity: 0 })), false);
  assert.equal(canSubmit(validSelection({ quantity: -3 })), false);
  assert.equal(canSubmit(validSelection({ quantity: MAX_QUANTITY + 1 })), false);
  assert.equal(canSubmit(validSelection({ quantity: MAX_QUANTITY })), true);
});

test("every blocked state explains itself in Turkish", () => {
  assert.equal(blockingReason(validSelection()), null);

  for (const [selection, expected] of [
    [validSelection({ products: [] }), MENU_EMPTY_MESSAGE],
    [validSelection({ selectedProductId: null }), PRODUCT_REQUIRED_MESSAGE],
    [validSelection({ ingredientIds: [] }), INGREDIENT_REQUIRED_MESSAGE],
  ] as const) {
    const reason = blockingReason(selection);
    assert.equal(reason, expected);
    assert.ok(reason && reason.length > 0);
    assert.match(reason, TURKISH_LETTERS);
  }
});

// ── Quantity control ─────────────────────────────────────────────────────────

test("quantity is clamped into the allowed range", () => {
  assert.equal(clampQuantity(3), 3);
  assert.equal(clampQuantity(0), MIN_QUANTITY);
  assert.equal(clampQuantity(-9), MIN_QUANTITY);
  assert.equal(clampQuantity(MAX_QUANTITY + 50), MAX_QUANTITY);
});

test("junk from a number input never becomes a quantity", () => {
  // "" while typing, "abc" from some keyboards, floats from a scroll wheel.
  assert.equal(clampQuantity(""), MIN_QUANTITY);
  assert.equal(clampQuantity("abc"), MIN_QUANTITY);
  assert.equal(clampQuantity(undefined), MIN_QUANTITY);
  assert.equal(clampQuantity(null), MIN_QUANTITY);
  assert.equal(clampQuantity(Number.NaN), MIN_QUANTITY);
  assert.equal(clampQuantity(Infinity), MIN_QUANTITY);
  assert.equal(clampQuantity(2.7), 2);
  assert.equal(clampQuantity("4"), 4);
});

test("the stepper walks the range and stops at both ends", () => {
  assert.equal(stepQuantity(1, 1), 2);
  assert.equal(stepQuantity(2, -1), 1);
  assert.equal(stepQuantity(MIN_QUANTITY, -1), MIN_QUANTITY);
  assert.equal(stepQuantity(MAX_QUANTITY, 1), MAX_QUANTITY);

  assert.equal(canDecrease(MIN_QUANTITY), false);
  assert.equal(canDecrease(2), true);
  assert.equal(canIncrease(MAX_QUANTITY), false);
  assert.equal(canIncrease(MAX_QUANTITY - 1), true);
});

test("the chosen quantity is what is submitted", () => {
  const submission = buildOrderSubmission(validSelection({ quantity: 3 }));
  assert.ok(submission);
  assert.equal(submission.items[0].quantity, 3);
  // …and the default is a real, visible 1 rather than an implied one.
  assert.equal(buildOrderSubmission(validSelection())!.items[0].quantity, 1);
});

// ── Payload shape ────────────────────────────────────────────────────────────

test("the payload carries the QR token and no numeric store or table", () => {
  const submission = buildOrderSubmission(validSelection())!;
  assert.equal(submission.qr_token, "tok_abc");
  assert.ok(!("store_id" in submission));
  assert.ok(!("table_id" in submission));
});

test("chosen ingredients travel with the line", () => {
  const submission = buildOrderSubmission(
    validSelection({ ingredientIds: [5, 8, 13] }),
  )!;
  assert.deepEqual(submission.items[0].ingredients, [
    { ingredient_id: 5, quantity: 1 },
    { ingredient_id: 8, quantity: 1 },
    { ingredient_id: 13, quantity: 1 },
  ]);
});

// ── Menu states ──────────────────────────────────────────────────────────────

test("an empty menu is a state, not a crash", () => {
  assert.equal(menuIsEmpty([]), true);
  assert.equal(menuIsEmpty(null), true);
  assert.equal(menuIsEmpty(undefined), true);
  assert.equal(menuIsEmpty(PRODUCTS), false);

  // Nothing can be submitted from it, and it says so in Turkish.
  assert.equal(canSubmit(validSelection({ products: [], selectedProductId: null })), false);
  assert.match(MENU_EMPTY_MESSAGE, TURKISH_LETTERS);
});

test("lookups on an absent menu return null instead of throwing", () => {
  assert.equal(selectedProduct(null, 7), null);
  assert.equal(selectedProduct(undefined, 7), null);
  assert.equal(selectedProduct(PRODUCTS, null), null);
});

// ── Price ────────────────────────────────────────────────────────────────────

test("the total multiplies the whole line, not just the base price", () => {
  // (90 base + 25 toppings) × 2
  assert.equal(selectionTotal(KLASIK, 25, 2), 230);
  assert.equal(selectionTotal(KLASIK, 0, 1), 90);
});

test("no chosen product means no price to show", () => {
  assert.equal(selectionTotal(null, 25, 2), 0);
});

test("an unparseable price does not become NaN on the button", () => {
  const broken: MenuProduct = { id: 1, name: "?", base_price: "" };
  assert.equal(selectionTotal(broken, 10, 2), 20);
});
