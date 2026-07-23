/**
 * What the guest has actually chosen, and whether it may be submitted.
 *
 * The bug this module exists to make impossible:
 *
 *     const product = menu?.products[0] ?? null;   // ← the whole product choice
 *     items: [{ product_id: product.id, quantity: 1, … }]
 *
 * The screen rendered a list of toppings, the guest never saw a product, and
 * whatever happened to sit first in the array was ordered on their behalf — one
 * of them, always. With a catalog of fourteen that made thirteen unreachable and
 * quietly billed the first (RUNTIME_PRODUCT_GAP_REVIEW F-01).
 *
 * So the rule here is: an order is built from an EXPLICIT selection or it is not
 * built at all. `buildOrderSubmission` returns null rather than guessing —
 * there is deliberately no fallback, no default-to-first, and no "if there is
 * only one product it must be that one". A single-product shop costs the guest
 * one tap; the alternative is a screen that can order something nobody chose.
 *
 * This module imports nothing at runtime (same rule as ./order-messages), which
 * keeps it runnable under `node --test` while the component that consumes it
 * stays a React component.
 */

// ── Quantity bounds ──────────────────────────────────────────────────────────
// The customer app offers a deliberately narrower range than the API accepts
// (MAX_ITEM_QUANTITY = 20 in app/schemas/order.py). A table ordering more than
// ten of one item is a conversation with a member of staff, not a stepper. The
// server is what enforces safety; this is what a thumb can reach.
export const MIN_QUANTITY = 1;
export const MAX_QUANTITY = 10;

// ── Turkish copy for the states this module can produce ──────────────────────
export const MENU_EMPTY_MESSAGE =
  "Bu masada şu anda sipariş verilebilecek ürün bulunmuyor. Lütfen personelden yardım isteyin.";

export const PRODUCT_REQUIRED_MESSAGE = "Lütfen bir ürün seçin.";

export const INGREDIENT_REQUIRED_MESSAGE = "En az bir malzeme seçmelisiniz.";

export interface MenuProduct {
  id: number;
  name: string;
  category?: string;
  /** Decimal-as-string, exactly as the API sends it. */
  base_price: string;
}

export interface OrderIngredientLine {
  ingredient_id: number;
  quantity: number;
}

export interface OrderLine {
  product_id: number;
  quantity: number;
  ingredients: OrderIngredientLine[];
}

export interface OrderSubmission {
  qr_token: string;
  items: OrderLine[];
}

/** Everything the submit decision depends on, in one place. */
export interface CustomerSelection {
  /** Null until the QR token has been resolved — ordering is impossible before. */
  qrToken: string | null;
  /** True only in the `ready` phase of the QR gate. */
  contextReady: boolean;
  products: MenuProduct[];
  selectedProductId: number | null;
  quantity: number;
  ingredientIds: number[];
}

/**
 * Is there anything to sell at this table?
 *
 * An empty list is a legitimate answer from a correctly scoped menu — a branch
 * that has published nothing — and it must render as a calm Turkish sentence,
 * never as a spinner that never stops or an error the guest cannot act on.
 */
export function menuIsEmpty(products: readonly MenuProduct[] | null | undefined): boolean {
  return !products || products.length === 0;
}

/**
 * The chosen product, or null.
 *
 * Looked up by id in the CURRENT list every time, so a selection that the menu
 * no longer contains (it was withdrawn, the menu reloaded) resolves to null and
 * disables submit — instead of being carried forward into an order the server
 * would reject anyway.
 */
export function selectedProduct(
  products: readonly MenuProduct[] | null | undefined,
  selectedProductId: number | null,
): MenuProduct | null {
  if (!products || selectedProductId === null) return null;
  return products.find((p) => p.id === selectedProductId) ?? null;
}

/**
 * Force any incoming value into the allowed range.
 *
 * Takes `unknown` on purpose: the value can come from an `<input type=number>`,
 * which yields "" while being typed, "abc" on some keyboards, and floats. None
 * of those may become part of a payload.
 */
export function clampQuantity(value: unknown): number {
  const n =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number.parseInt(value, 10)
        : Number.NaN;
  if (!Number.isFinite(n)) return MIN_QUANTITY;
  const whole = Math.trunc(n);
  if (whole < MIN_QUANTITY) return MIN_QUANTITY;
  if (whole > MAX_QUANTITY) return MAX_QUANTITY;
  return whole;
}

/** Step the quantity by ±1 (or any delta), staying inside the bounds. */
export function stepQuantity(current: unknown, delta: number): number {
  return clampQuantity(clampQuantity(current) + delta);
}

export function canDecrease(quantity: number): boolean {
  return clampQuantity(quantity) > MIN_QUANTITY;
}

export function canIncrease(quantity: number): boolean {
  return clampQuantity(quantity) < MAX_QUANTITY;
}

/**
 * May this selection be submitted?
 *
 * Every clause is a thing the server independently refuses. The button is
 * disabled so the guest is not sent to a rejection they cannot read, NOT to
 * spare the server the check.
 */
export function canSubmit(selection: CustomerSelection): boolean {
  return buildOrderSubmission(selection) !== null;
}

/**
 * The payload, or null when the selection is not complete and current.
 *
 * Null is the honest answer and the only alternative to inventing a choice.
 * Callers must treat it as "do not submit" — never as "submit something else".
 */
export function buildOrderSubmission(
  selection: CustomerSelection,
): OrderSubmission | null {
  const { qrToken, contextReady, products, quantity, ingredientIds } = selection;

  if (!qrToken || !contextReady) return null;

  const product = selectedProduct(products, selection.selectedProductId);
  if (!product) return null;

  if (ingredientIds.length === 0) return null;

  // A quantity outside the bounds is refused rather than clamped here: silently
  // ordering 10 when the state somehow says 400 is the same class of mistake as
  // silently ordering products[0].
  if (quantity !== clampQuantity(quantity)) return null;

  return {
    qr_token: qrToken,
    items: [
      {
        product_id: product.id,
        quantity,
        ingredients: ingredientIds.map((id) => ({
          ingredient_id: id,
          quantity: 1,
        })),
      },
    ],
  };
}

/**
 * What the guest will pay: (product + its toppings) × how many.
 *
 * Kept here, next to the payload builder, so the number on the button and the
 * number the server computes come from the same understanding of the order.
 */
export function selectionTotal(
  product: MenuProduct | null,
  ingredientTotal: number,
  quantity: number,
): number {
  if (!product) return 0;
  const base = Number.parseFloat(product.base_price);
  const unit = (Number.isFinite(base) ? base : 0) + ingredientTotal;
  return unit * clampQuantity(quantity);
}

/** Why submit is disabled, in Turkish — or null when it is enabled. */
export function blockingReason(selection: CustomerSelection): string | null {
  if (menuIsEmpty(selection.products)) return MENU_EMPTY_MESSAGE;
  if (!selectedProduct(selection.products, selection.selectedProductId)) {
    return PRODUCT_REQUIRED_MESSAGE;
  }
  if (selection.ingredientIds.length === 0) return INGREDIENT_REQUIRED_MESSAGE;
  return null;
}
