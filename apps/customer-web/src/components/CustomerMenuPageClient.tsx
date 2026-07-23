"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import {
  fetchMenu,
  fetchUpsell,
  createOrder,
  resolveQrContext,
  QrResolveError,
  EnrichedIngredient,
  EnrichedMenuResponse,
  UpsellSuggestion,
} from "@/lib/api";
import type { OrderCreateRequest, QrContextResponse } from "@sweetops/types";
import { fingerprintOrder, orderIdempotency } from "@/lib/order-idempotency";
import { orderErrorMessage, qrPhaseMessage } from "@/lib/order-messages";
import {
  MAX_QUANTITY,
  MENU_EMPTY_MESSAGE,
  blockingReason,
  buildOrderSubmission,
  canDecrease,
  canIncrease,
  menuIsEmpty,
  selectedProduct as resolveSelectedProduct,
  selectionTotal,
  stepQuantity,
  type CustomerSelection,
  type MenuProduct,
} from "@/lib/order-selection";
import { acquireQrToken, clearQrToken } from "@/lib/qr-session";

const MAX_TOPPINGS = 6;
const MAX_SAUCES = 2;

function isSauceCategory(cat: string): boolean {
  return cat === "Çikolatalar / Soslar";
}

// ── Quick Start Combos (data-driven) ─────────────────────────────────────────

interface Combo {
  label: string;
  sublabel: string;
  ids: number[];
  totalPrice: number;
}

function buildCombos(allIngredients: EnrichedIngredient[]): Combo[] {
  const inStock = allIngredients.filter((i) => i.stock_status !== "out_of_stock");

  // Most popular: top 4 by popular_badge + ranking_score
  const popular = [...inStock]
    .sort((a, b) => {
      const aScore = (a.popular_badge ? 1000 : 0) + b.ranking_score;
      const bScore = (b.popular_badge ? 1000 : 0) + a.ranking_score;
      return bScore - aScore;
    })
    .slice(0, 4);

  // Highest margin: top 4 profitable_badge items, then by price
  const margin = [...inStock]
    .sort((a, b) => {
      const aScore = (a.profitable_badge ? 100 : 0) + parseFloat(a.price);
      const bScore = (b.profitable_badge ? 100 : 0) + parseFloat(b.price);
      return bScore - aScore;
    })
    .slice(0, 4);

  // Fastest (fewest total ingredients by name length as proxy for complexity)
  // We use smallest ingredient standard_quantity as a proxy for "light"
  const light = [...inStock]
    .filter((i) => !isSauceCategory(i.category))
    .sort((a, b) => parseFloat(a.standard_quantity) - parseFloat(b.standard_quantity))
    .slice(0, 3);

  const priceOf = (ids: number[]) =>
    ids.reduce((s, id) => {
      const ing = inStock.find((i) => i.id === id);
      return s + (ing ? parseFloat(ing.price) : 0);
    }, 0);

  const combos: Combo[] = [];

  if (popular.length >= 3) {
    const ids = popular.map((i) => i.id);
    combos.push({ label: "Bugün en çok seçilen", sublabel: popular.slice(0, 3).map((i) => i.name).join(", "), ids, totalPrice: priceOf(ids) });
  }

  const marginIds = Array.from(new Set([...margin.map((i) => i.id)])).slice(0, 4);
  if (marginIds.length >= 2 && JSON.stringify(marginIds) !== JSON.stringify(popular.map((i) => i.id).slice(0, 4))) {
    combos.push({ label: "Şefin önerisi", sublabel: margin.slice(0, 3).map((i) => i.name).join(", "), ids: marginIds, totalPrice: priceOf(marginIds) });
  }

  const lightIds = light.map((i) => i.id).slice(0, 3);
  if (lightIds.length >= 2) {
    combos.push({ label: "Hafif ve hızlı", sublabel: light.slice(0, 3).map((i) => i.name).join(", "), ids: lightIds, totalPrice: priceOf(lightIds) });
  }

  return combos.slice(0, 3);
}

// ── Quick Start Combos UI ─────────────────────────────────────────────────────

function QuickStartSection({
  allIngredients,
  basePrice,
  onApply,
}: {
  allIngredients: EnrichedIngredient[];
  basePrice: number;
  onApply: (ids: number[], label: string) => void;
}) {
  const combos = buildCombos(allIngredients);
  if (combos.length === 0) return null;

  return (
    <section className="px-4 py-3 border-b border-gray-100 bg-amber-50">
      <p className="text-xs font-semibold text-amber-800 mb-2 uppercase tracking-wide">
        Hazır seçimler — tek dokunuş
      </p>
      <div className="space-y-2">
        {combos.map((combo) => (
          <button
            key={combo.label}
            onClick={() => onApply(combo.ids, combo.label)}
            className="w-full flex items-center justify-between px-3 py-2.5 bg-white rounded-xl border border-amber-200 hover:border-amber-400 transition-colors text-left active:scale-[0.99]"
          >
            <div className="min-w-0">
              <p className="text-sm font-semibold text-gray-900">{combo.label}</p>
              <p className="text-xs text-gray-400 truncate mt-0.5">{combo.sublabel}</p>
            </div>
            <span className="shrink-0 text-sm font-bold text-amber-700 ml-3">
              ₺{(basePrice + combo.totalPrice).toFixed(0)}
            </span>
          </button>
        ))}
      </div>
    </section>
  );
}

// ── Ingredient chip ───────────────────────────────────────────────────────────

interface ChipProps {
  ingredient: EnrichedIngredient;
  selected: boolean;
  onToggle: (id: number) => void;
  isMostOrdered?: boolean;
}

function IngredientChip({ ingredient, selected, onToggle, isMostOrdered }: ChipProps) {
  const outOfStock = ingredient.stock_status === "out_of_stock";
  const lowStock = ingredient.stock_status === "low_stock";

  if (outOfStock) {
    // Show with alternative hint — do NOT hide entirely
    return (
      <div className="relative">
        <button
          disabled
          className="w-full text-left px-3 py-2.5 rounded-xl border border-gray-100 bg-gray-50 opacity-60 cursor-not-allowed"
        >
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-400 line-through">{ingredient.name}</span>
            <span className="text-[10px] bg-gray-200 text-gray-500 px-1.5 py-0.5 rounded font-medium">
              Stokta yok
            </span>
          </div>
          {ingredient.out_of_stock_alternative && (
            <p className="text-[10px] text-gray-400 mt-0.5">
              Bunun yerine {ingredient.out_of_stock_alternative.ingredient_name} var
            </p>
          )}
        </button>
      </div>
    );
  }

  return (
    <button
      onClick={() => onToggle(ingredient.id)}
      className={`w-full text-left px-3 py-2.5 rounded-xl border transition-all ${
        selected
          ? "border-amber-400 bg-amber-50 ring-1 ring-amber-300"
          : "border-gray-100 bg-white hover:border-gray-200 hover:bg-gray-50"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          {/* Badges */}
          {isMostOrdered && (
            <span className="text-[10px] shrink-0 font-bold text-white bg-amber-500 px-1.5 py-0.5 rounded uppercase tracking-wide">
              #1
            </span>
          )}
          {!isMostOrdered && ingredient.popular_badge && (
            <span className="text-[10px] shrink-0 font-bold text-amber-600 bg-amber-50 px-1 py-0.5 rounded uppercase tracking-wide">
              🔥
            </span>
          )}
          {ingredient.profitable_badge && !ingredient.popular_badge && (
            <span className="text-[10px] shrink-0 font-bold text-emerald-600 bg-emerald-50 px-1 py-0.5 rounded uppercase tracking-wide">
              ✦
            </span>
          )}
          <span
            className={`text-sm font-medium truncate ${
              selected ? "text-amber-900" : "text-gray-800"
            }`}
          >
            {ingredient.name}
          </span>
          {lowStock && (
            <span className="text-[10px] shrink-0 text-amber-600 font-medium">Az kaldı</span>
          )}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span
            className={`text-xs font-semibold ${
              selected ? "text-amber-700" : "text-gray-500"
            }`}
          >
            +₺{parseFloat(ingredient.price).toFixed(0)}
          </span>
          {selected && (
            <span className="w-4 h-4 rounded-full bg-amber-400 text-white text-[10px] flex items-center justify-center font-bold">
              ✓
            </span>
          )}
        </div>
      </div>
    </button>
  );
}

// ── Upsell panel ──────────────────────────────────────────────────────────────

function UpsellPanel({
  suggestions,
  onAdd,
  selectedIds,
}: {
  suggestions: UpsellSuggestion[];
  onAdd: (id: number) => void;
  selectedIds: Set<number>;
}) {
  const available = suggestions.filter(
    (s) => s.stock_status !== "out_of_stock" && !selectedIds.has(s.ingredient_id),
  );
  if (available.length === 0) return null;

  return (
    <div className="mx-4 mb-4 p-3 bg-blue-50 rounded-xl border border-blue-100">
      <p className="text-xs font-semibold text-blue-700 mb-2">
        Seçtiklerinizle iyi gider:
      </p>
      <div className="flex flex-wrap gap-2">
        {available.map((s) => (
          <button
            key={s.ingredient_id}
            onClick={() => onAdd(s.ingredient_id)}
            className="text-xs font-medium px-3 py-1.5 bg-white rounded-lg border border-blue-200 text-blue-800 hover:bg-blue-100 transition-colors"
          >
            + {s.ingredient_name}{" "}
            <span className="text-blue-500">₺{parseFloat(s.price).toFixed(0)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Popular section ───────────────────────────────────────────────────────────

function PopularSection({
  ingredients,
  selected,
  onToggle,
  mostOrderedId,
}: {
  ingredients: EnrichedIngredient[];
  selected: Set<number>;
  onToggle: (id: number) => void;
  mostOrderedId: number;
}) {
  const popular = ingredients
    .filter((i) => i.popular_badge && i.stock_status !== "out_of_stock")
    .sort((a, b) => b.ranking_score - a.ranking_score)
    .slice(0, 4);

  if (popular.length === 0) return null;

  return (
    <section className="px-4 py-3 border-b border-gray-100">
      <div className="flex items-center gap-1.5 mb-2">
        <span className="text-sm">🔥</span>
        <span className="text-xs font-semibold text-gray-700 uppercase tracking-wide">
          En popüler
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2">
        {popular.map((ing) => (
          <IngredientChip
            key={ing.id}
            ingredient={ing}
            selected={selected.has(ing.id)}
            onToggle={onToggle}
            isMostOrdered={ing.id === mostOrderedId}
          />
        ))}
      </div>
    </section>
  );
}

// ── Product picker ────────────────────────────────────────────────────────────

/**
 * The product choice, made explicitly.
 *
 * This section is the fix for "the guest never chose a product". Nothing is
 * pre-selected — not even when the branch sells exactly one thing — so the
 * order can never contain something nobody tapped.
 */
function ProductSection({
  products,
  selectedProductId,
  onSelect,
}: {
  products: MenuProduct[];
  selectedProductId: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <section className="px-4 py-3 border-b border-gray-100">
      <div className="flex items-center gap-1.5 mb-2">
        <span className="text-xs font-semibold text-gray-700 uppercase tracking-wide">
          Ürün seçin
        </span>
      </div>
      <div className="grid grid-cols-1 gap-2">
        {products.map((product) => {
          const selected = product.id === selectedProductId;
          return (
            <button
              key={product.id}
              onClick={() => onSelect(product.id)}
              aria-pressed={selected}
              className={`w-full text-left px-3 py-2.5 rounded-xl border transition-all ${
                selected
                  ? "border-amber-400 bg-amber-50 ring-1 ring-amber-300"
                  : "border-gray-100 bg-white hover:border-gray-200 hover:bg-gray-50"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span
                  className={`text-sm font-medium truncate ${
                    selected ? "text-amber-900" : "text-gray-800"
                  }`}
                >
                  {product.name}
                </span>
                <div className="flex items-center gap-1.5 shrink-0">
                  <span
                    className={`text-xs font-semibold ${
                      selected ? "text-amber-700" : "text-gray-500"
                    }`}
                  >
                    ₺{parseFloat(product.base_price).toFixed(0)}
                  </span>
                  {selected && (
                    <span className="w-4 h-4 rounded-full bg-amber-400 text-white text-[10px] flex items-center justify-center font-bold">
                      ✓
                    </span>
                  )}
                </div>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}

// ── Quantity stepper ──────────────────────────────────────────────────────────

/**
 * How many. Visible, bounded, and never assumed: the previous screen hard-coded
 * `quantity: 1` into the payload with nothing on screen to say so.
 */
function QuantityStepper({
  quantity,
  onChange,
}: {
  quantity: number;
  onChange: (next: number) => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-gray-500">
        Adet{!canIncrease(quantity) && ` (en fazla ${MAX_QUANTITY})`}
      </span>
      <div className="flex items-center gap-3">
        <button
          type="button"
          aria-label="Adedi azalt"
          disabled={!canDecrease(quantity)}
          onClick={() => onChange(stepQuantity(quantity, -1))}
          className="w-8 h-8 rounded-full border border-gray-200 text-gray-700 text-lg leading-none disabled:opacity-40 disabled:cursor-not-allowed"
        >
          −
        </button>
        <span
          aria-live="polite"
          className="min-w-[1.5rem] text-center text-sm font-bold text-gray-900"
        >
          {quantity}
        </span>
        <button
          type="button"
          aria-label="Adedi artır"
          disabled={!canIncrease(quantity)}
          onClick={() => onChange(stepQuantity(quantity, 1))}
          className="w-8 h-8 rounded-full border border-gray-200 text-gray-700 text-lg leading-none disabled:opacity-40 disabled:cursor-not-allowed"
        >
          +
        </button>
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

// Phases of the QR-gated menu screen. Plain query params (?store, ?table) are
// intentionally never read — only the opaque ?qr token is trusted.
type QrPhase =
  | "loading" // resolving the token / loading the menu
  | "missing" // no ?qr in the URL
  | "invalid" // token unknown / revoked / malformed
  | "unavailable" // valid token but table/store not open to ordering
  | "network" // transient failure — retry is meaningful
  | "ready"; // context resolved and menu loaded

export default function CustomerMenuPageClient() {
  const router = useRouter();

  // The ONLY trusted context source is an opaque QR token delivered in the URL
  // *fragment* (`#qr=<token>`), captured client-side, then scrubbed from the
  // address bar. Legacy `?qr=` / `?store=` / `?table=` query params are never
  // read. `qrToken` starts null and is populated once, on mount, below.
  const [qrToken, setQrToken] = useState<string | null>(null);
  const [tokenAcquired, setTokenAcquired] = useState(false);

  const [phase, setPhase] = useState<QrPhase>("loading");
  const [qrErrorMessage, setQrErrorMessage] = useState<string | null>(null);
  const [context, setContext] = useState<QrContextResponse | null>(null);
  const [menu, setMenu] = useState<EnrichedMenuResponse | null>(null);
  // Null until the guest taps a product. Never seeded from the menu — see
  // lib/order-selection.ts for why there is no "if there is only one" shortcut.
  const [selectedProductId, setSelectedProductId] = useState<number | null>(null);
  const [quantity, setQuantity] = useState<number>(1);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [upsell, setUpsell] = useState<UpsellSuggestion[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // Synchronous double-click guard: React state updates are async, so a second
  // click can fire before `submitting` re-renders. A ref blocks it immediately.
  const submittingRef = useRef(false);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const upsellTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Capture the QR token from the URL fragment (or this session's storage)
  // exactly once, client-side. `acquireQrToken` also scrubs the token out of
  // the visible address bar. A same-tab refresh re-reads it from sessionStorage
  // (the fragment is gone by then); a new tab opened without scanning finds no
  // token and lands on the "missing" state.
  useEffect(() => {
    setQrToken(acquireQrToken());
    setTokenAcquired(true);
  }, []);

  // Resolve the QR token → context → menu. No default store is ever assumed;
  // without a valid token the menu is never loaded and ordering is impossible.
  useEffect(() => {
    // Wait until the fragment/session read has happened, else a transient
    // "missing" would flash before the token is acquired.
    if (!tokenAcquired) return;

    let cancelled = false;

    if (!qrToken) {
      setPhase("missing");
      return;
    }

    setPhase("loading");
    setQrErrorMessage(null);

    (async () => {
      try {
        const ctx = await resolveQrContext(qrToken);
        if (cancelled) return;
        setContext(ctx);
        const loadedMenu = await fetchMenu(qrToken);
        if (cancelled) return;
        setMenu(loadedMenu);
        setPhase("ready");
      } catch (err) {
        if (cancelled) return;
        if (err instanceof QrResolveError) {
          setQrErrorMessage(err.userMessage ?? null);
          if (err.kind === "invalid") {
            // Definitive: the stored token is dead (unknown / revoked /
            // rotated). Forget it so a same-tab refresh does not keep retrying
            // a token that can never resolve.
            clearQrToken();
            setPhase("invalid");
          } else if (err.kind === "unavailable") {
            // Valid token, table/store temporarily closed — keep the token.
            setPhase("unavailable");
          } else {
            // Network: outcome unknown. Keep the token so a retry can succeed.
            setPhase("network");
          }
        } else {
          // Menu load or unexpected failure after a valid token — transient.
          // The token stays; a retry may succeed.
          setPhase("network");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [qrToken, tokenAcquired]);

  // Debounced upsell fetch when selection changes. The token goes with it:
  // suggestions are filtered by in-stock, and stock belongs to this table's
  // store, so there is nothing to suggest until we know which branch we are in.
  useEffect(() => {
    if (selected.size < 2 || !qrToken) {
      setUpsell([]);
      return;
    }
    if (upsellTimer.current) clearTimeout(upsellTimer.current);
    upsellTimer.current = setTimeout(() => {
      fetchUpsell(qrToken, Array.from(selected))
        .then((r) => setUpsell(r.suggestions))
        .catch(() => {});
    }, 400);
  }, [selected, qrToken]);

  const applyCombo = useCallback(
    (ids: number[], label: string) => {
      setSelected(new Set(ids));
      showToast(`${label} sepetinize eklendi ✓`);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const showToast = useCallback((msg: string) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = setTimeout(() => setToast(null), 2500);
  }, []);

  // Build ingredient map
  const ingredientMap = new Map<number, EnrichedIngredient>();
  (menu?.categories ?? []).forEach((cat) =>
    cat.ingredients.forEach((ing) => ingredientMap.set(ing.id, ing)),
  );

  const countByType = (ids: number[], sauces: boolean) =>
    Array.from(ids).filter((id) => {
      const ing = ingredientMap.get(id);
      return ing ? isSauceCategory(ing.category) === sauces : false;
    }).length;

  const toggleIngredient = useCallback(
    (id: number) => {
      const ing = ingredientMap.get(id);
      if (!ing) return;
      if (ing.stock_status === "out_of_stock") return;

      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(id)) {
          next.delete(id);
          return next;
        }

        // Limit check
        const isSauce = isSauceCategory(ing.category);
        const currentCount = countByType(Array.from(next), isSauce);
        const limit = isSauce ? MAX_SAUCES : MAX_TOPPINGS;
        if (currentCount >= limit) {
          showToast(`En fazla ${limit} ${isSauce ? "sos" : "malzeme"} seçebilirsiniz.`);
          return prev;
        }

        next.add(id);
        return next;
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [menu, showToast],
  );

  const addIngredient = useCallback(
    (id: number) => {
      const ing = ingredientMap.get(id);
      if (!ing) return;
      toggleIngredient(id);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [toggleIngredient, menu],
  );

  // The whole submit decision in one value. Everything below — the price, the
  // button label, whether the button is live — is derived from it, so the screen
  // and the payload can never disagree about what was chosen.
  const products: MenuProduct[] = menu?.products ?? [];
  const selection: CustomerSelection = {
    qrToken,
    contextReady: phase === "ready",
    products,
    selectedProductId,
    quantity,
    ingredientIds: Array.from(selected),
  };

  // Resolved against the CURRENT menu: a product that was withdrawn while the
  // guest was choosing toppings resolves to null and disables submit.
  const product = resolveSelectedProduct(products, selectedProductId);
  // Only a chosen product contributes a base price — the combo prices below
  // therefore read as "toppings only" until the guest has picked one, which is
  // the truth rather than the first product's price standing in for it.
  const basePrice = product ? parseFloat(product.base_price) : 0;
  const ingredientTotal = Array.from(selected).reduce((sum, id) => {
    const ing = ingredientMap.get(id);
    return sum + (ing ? parseFloat(ing.price) : 0);
  }, 0);
  const totalPrice = selectionTotal(product, ingredientTotal, quantity);
  const submission = buildOrderSubmission(selection);
  const blocked = blockingReason(selection);

  // Submit
  const handleSubmit = async () => {
    // Hard double-click / re-entrancy guard (synchronous, not state-based).
    if (submittingRef.current) return;
    // One gate for every precondition: a resolved QR context, an explicitly
    // chosen product that is still on the menu, at least one ingredient, and a
    // quantity inside the bounds. If any of them fails, `submission` is null and
    // nothing is sent — there is no fallback payload to send instead.
    if (submission === null) {
      if (blocked) showToast(blocked);
      return;
    }

    // Derive one idempotency key for this logical order. A retry of the same
    // selection reuses the key; any change (including the quantity) mints a new
    // one. The QR token is the trusted context — no numeric store/table is sent.
    const payload: OrderCreateRequest = submission;
    const idempotencyKey = orderIdempotency.getOrCreateKey(
      fingerprintOrder(payload),
    );

    submittingRef.current = true;
    setSubmitting(true);
    try {
      const res = await createOrder(payload, idempotencyKey);
      // Confirmed success (new order OR the same order returned for this key):
      // retire the attempt and reset the cart so it can never be resubmitted.
      orderIdempotency.clear();
      setSelected(new Set());
      setSelectedProductId(null);
      setQuantity(1);
      // Keep the button disabled through navigation — do not reset the guard.
      router.push(`/success?order_id=${res.order_id}&amount=${res.total_amount}`);
    } catch (err) {
      // Uncertain outcomes keep the key and cart so a retry is safe and never
      // duplicates; a deterministic rejection keeps the cart so the customer can
      // adjust it, which mints a new key by itself.
      showToast(orderErrorMessage(err));
      submittingRef.current = false;
      setSubmitting(false);
    }
  };

  // ── QR gate: loading / missing / invalid / unavailable / network ────────────

  if (phase === "loading") {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3">
        <div className="w-8 h-8 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
        <p className="text-sm text-gray-400">{qrPhaseMessage("loading")}</p>
      </div>
    );
  }

  if (phase === "missing") {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3 px-6 text-center">
        <span className="text-3xl">📷</span>
        <p className="text-gray-700 text-sm font-medium">
          {qrPhaseMessage("missing")}
        </p>
      </div>
    );
  }

  if (phase === "invalid") {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3 px-6 text-center">
        <span className="text-3xl">⚠️</span>
        <p className="text-gray-700 text-sm font-medium">
          {qrPhaseMessage("invalid", qrErrorMessage)}
        </p>
      </div>
    );
  }

  if (phase === "unavailable") {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3 px-6 text-center">
        <span className="text-3xl">🔒</span>
        <p className="text-gray-700 text-sm font-medium">
          {qrPhaseMessage("unavailable", qrErrorMessage)}
        </p>
      </div>
    );
  }

  if (phase === "network") {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3 px-6 text-center">
        <p className="text-gray-500 text-sm">
          {qrPhaseMessage("network")}
        </p>
        <button
          onClick={() => window.location.reload()}
          className="text-sm font-semibold text-amber-600 hover:underline"
        >
          Tekrar dene
        </button>
      </div>
    );
  }

  // The branch resolved, but it has published nothing to sell. A correctly
  // scoped menu can legitimately be empty (see menu_service.list_menu_products);
  // it must read as a calm Turkish sentence, not as a broken screen — and there
  // must be no way to submit from it.
  if (menuIsEmpty(products)) {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3 px-6 text-center">
        <span className="text-3xl">🧇</span>
        <p className="text-gray-700 text-sm font-medium">{MENU_EMPTY_MESSAGE}</p>
        {context && (
          <p className="text-xs text-gray-400">
            {context.store.name} · {context.table.name}
          </p>
        )}
      </div>
    );
  }

  const allIngredients = (menu?.categories ?? []).flatMap((c) => c.ingredients);

  // Single ingredient with highest ranking_score across all categories (in-stock only)
  const mostOrderedId = allIngredients
    .filter((i) => i.stock_status !== "out_of_stock")
    .sort((a, b) => b.ranking_score - a.ranking_score)[0]?.id ?? -1;

  return (
    <div className="min-h-screen bg-white flex flex-col max-w-md mx-auto">
      {/* Header */}
      <header className="px-4 pt-6 pb-4 border-b border-gray-100">
        <h1 className="text-xl font-bold text-gray-900">Waffle'ınızı oluşturun</h1>
        {context && (
          <p className="text-xs text-gray-400 mt-0.5">
            {context.store.name} · {context.table.name}
          </p>
        )}
      </header>

      {/* Product choice — explicit, nothing pre-selected */}
      <ProductSection
        products={products}
        selectedProductId={selectedProductId}
        onSelect={setSelectedProductId}
      />

      {/* Quick Start Combos — above everything else */}
      {selected.size === 0 && (
        <QuickStartSection
          allIngredients={allIngredients}
          basePrice={basePrice}
          onApply={applyCombo}
        />
      )}

      {/* Popular section */}
      <PopularSection
        ingredients={allIngredients}
        selected={selected}
        onToggle={toggleIngredient}
        mostOrderedId={mostOrderedId}
      />

      {/* Upsell */}
      {upsell.length > 0 && (
        <UpsellPanel suggestions={upsell} onAdd={addIngredient} selectedIds={selected} />
      )}

      {/* Category sections */}
      <div className="flex-1 overflow-y-auto">
        {(menu?.categories ?? []).map((cat) => {
          // Sort: in-stock popular first, then in-stock, then low-stock, then out-of-stock
          const sorted = [...cat.ingredients].sort((a, b) => {
            if (a.stock_status === "out_of_stock" && b.stock_status !== "out_of_stock") return 1;
            if (b.stock_status === "out_of_stock" && a.stock_status !== "out_of_stock") return -1;
            return b.ranking_score - a.ranking_score;
          });

          return (
            <section key={cat.name} className="px-4 py-3 border-b border-gray-50">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-xs font-semibold text-gray-600 uppercase tracking-wide">
                  {cat.name}
                </span>
                {isSauceCategory(cat.name) && (
                  <span className="text-[10px] text-gray-400">En fazla {MAX_SAUCES}</span>
                )}
              </div>
              <div className="grid grid-cols-1 gap-2">
                {sorted.map((ing) => (
                  <IngredientChip
                    key={ing.id}
                    ingredient={ing}
                    selected={selected.has(ing.id)}
                    onToggle={toggleIngredient}
                    isMostOrdered={ing.id === mostOrderedId}
                  />
                ))}
              </div>
            </section>
          );
        })}

        {/* Bottom spacer for sticky bar */}
        <div className="h-28" />
      </div>

      {/* Sticky bottom bar */}
      <div className="fixed bottom-0 left-1/2 -translate-x-1/2 w-full max-w-md bg-white border-t border-gray-100 px-4 py-3 shadow-lg">
        {/* What is being ordered, spelled out before submit — the product name
            and the count, never implied. */}
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-gray-500 truncate mr-2">
            {product
              ? `${product.name} · ${selected.size} malzeme`
              : "Ürün seçilmedi"}
          </span>
          <span className="text-lg font-bold text-gray-900">
            ₺{totalPrice.toFixed(0)}
          </span>
        </div>

        <div className="mb-3">
          <QuantityStepper quantity={quantity} onChange={setQuantity} />
        </div>

        <button
          onClick={handleSubmit}
          disabled={submitting || submission === null}
          className={`w-full py-3.5 rounded-xl text-sm font-bold transition-all ${
            submission === null
              ? "bg-gray-100 text-gray-400 cursor-not-allowed"
              : "bg-amber-400 text-white hover:bg-amber-500 active:scale-[0.98]"
          } disabled:opacity-70`}
        >
          {submitting
            ? "Siparişiniz gönderiliyor…"
            : !product
            ? "Ürün seçin"
            : selected.size === 0
            ? "Malzeme seçin"
            : `Sipariş ver — ₺${totalPrice.toFixed(0)}`}
        </button>
      </div>

      {/* Toast */}
      {toast && (
        <div className="fixed top-4 left-1/2 -translate-x-1/2 bg-gray-900 text-white text-xs font-medium px-4 py-2 rounded-full shadow-lg z-50 animate-fade-in">
          {toast}
        </div>
      )}
    </div>
  );
}
