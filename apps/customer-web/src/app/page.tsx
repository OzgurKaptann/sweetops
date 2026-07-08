"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import {
  fetchMenu,
  fetchUpsell,
  createOrder,
  EnrichedIngredient,
  EnrichedMenuResponse,
  UpsellSuggestion,
} from "@/lib/api";

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
    combos.push({ label: "Most ordered today", sublabel: popular.slice(0, 3).map((i) => i.name).join(", "), ids, totalPrice: priceOf(ids) });
  }

  const marginIds = Array.from(new Set([...margin.map((i) => i.id)])).slice(0, 4);
  if (marginIds.length >= 2 && JSON.stringify(marginIds) !== JSON.stringify(popular.map((i) => i.id).slice(0, 4))) {
    combos.push({ label: "Chef's pick", sublabel: margin.slice(0, 3).map((i) => i.name).join(", "), ids: marginIds, totalPrice: priceOf(marginIds) });
  }

  const lightIds = light.map((i) => i.id).slice(0, 3);
  if (lightIds.length >= 2) {
    combos.push({ label: "Quick & light", sublabel: light.slice(0, 3).map((i) => i.name).join(", "), ids: lightIds, totalPrice: priceOf(lightIds) });
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
        Quick start — one tap
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
              Tükendi
            </span>
          </div>
          {ingredient.out_of_stock_alternative && (
            <p className="text-[10px] text-gray-400 mt-0.5">
              → {ingredient.out_of_stock_alternative.ingredient_name} mevcut
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
        Seçtiklerinle harika gider:
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
          En Popüler
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

// ── Page ──────────────────────────────────────────────────────────────────────

export default function Home() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const storeId = Number(searchParams?.get("store") || 1);
  const tableId = searchParams?.get("table")
    ? Number(searchParams.get("table"))
    : undefined;

  const [menu, setMenu] = useState<EnrichedMenuResponse | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [upsell, setUpsell] = useState<UpsellSuggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const upsellTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load menu
  useEffect(() => {
    fetchMenu()
      .then(setMenu)
      .catch(() => setError("Menü yüklenemedi. Lütfen tekrar deneyin."))
      .finally(() => setLoading(false));
  }, []);

  // Debounced upsell fetch when selection changes
  useEffect(() => {
    if (selected.size < 2) {
      setUpsell([]);
      return;
    }
    if (upsellTimer.current) clearTimeout(upsellTimer.current);
    upsellTimer.current = setTimeout(() => {
      fetchUpsell(Array.from(selected))
        .then((r) => setUpsell(r.suggestions))
        .catch(() => {});
    }, 400);
  }, [selected]);

  const applyCombo = useCallback(
    (ids: number[], label: string) => {
      setSelected(new Set(ids));
      showToast(`${label} seçildi ✓`);
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
          showToast(`En fazla ${limit} ${isSauce ? "sos" : "malzeme"} seçebilirsiniz`);
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

  // Price calculation
  const product = menu?.products[0] ?? null;
  const basePrice = product ? parseFloat(product.base_price) : 0;
  const ingredientTotal = Array.from(selected).reduce((sum, id) => {
    const ing = ingredientMap.get(id);
    return sum + (ing ? parseFloat(ing.price) : 0);
  }, 0);
  const totalPrice = basePrice + ingredientTotal;

  // Submit
  const handleSubmit = async () => {
    if (selected.size === 0) {
      showToast("En az 1 malzeme seçmelisiniz");
      return;
    }
    if (!product) return;

    setSubmitting(true);
    try {
      const res = await createOrder({
        store_id: storeId,
        table_id: tableId,
        items: [
          {
            product_id: product.id,
            quantity: 1,
            ingredients: Array.from(selected).map((id) => ({
              ingredient_id: id,
              quantity: 1,
            })),
          },
        ],
      });
      router.push(`/success?order_id=${res.order_id}&amount=${res.total_amount}`);
    } catch {
      showToast("Sipariş gönderilemedi. Tekrar deneyin.");
      setSubmitting(false);
    }
  };

  // ── Loading / error ────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3">
        <div className="w-8 h-8 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
        <p className="text-sm text-gray-400">Menü yükleniyor…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3 px-6 text-center">
        <p className="text-gray-500 text-sm">{error}</p>
        <button
          onClick={() => window.location.reload()}
          className="text-sm font-semibold text-amber-600 hover:underline"
        >
          Tekrar Dene
        </button>
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
        <h1 className="text-xl font-bold text-gray-900">Waffle'ını Oluştur</h1>
        {tableId && (
          <p className="text-xs text-gray-400 mt-0.5">Masa {tableId}</p>
        )}
      </header>

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
                  <span className="text-[10px] text-gray-400">Max {MAX_SAUCES}</span>
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
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs text-gray-500">
            {selected.size > 0
              ? `${selected.size} malzeme seçildi`
              : "Malzeme seçin"}
          </span>
          <span className="text-lg font-bold text-gray-900">
            ₺{totalPrice.toFixed(0)}
          </span>
        </div>
        <button
          onClick={handleSubmit}
          disabled={submitting || selected.size === 0}
          className={`w-full py-3.5 rounded-xl text-sm font-bold transition-all ${
            selected.size === 0
              ? "bg-gray-100 text-gray-400 cursor-not-allowed"
              : "bg-amber-400 text-white hover:bg-amber-500 active:scale-[0.98]"
          } disabled:opacity-70`}
        >
          {submitting
            ? "Gönderiliyor…"
            : selected.size === 0
            ? "Malzeme Seçin"
            : `Sipariş Ver — ₺${totalPrice.toFixed(0)}`}
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
