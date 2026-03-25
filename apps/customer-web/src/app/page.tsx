"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams, useRouter } from "next/navigation";
import { fetchMenu, createOrder } from "@/lib/api";
import type { IngredientCategory, Ingredient, Product } from "@sweetops/types";

const MAX_TOPPINGS = 6;
const MAX_SAUCES = 2;

const CATEGORY_META: Record<string, { icon: string; cssClass: string }> = {
  "Meyveler":               { icon: "🍓", cssClass: "fruits" },
  "Kuruyemiş / Süslemeler": { icon: "🥜", cssClass: "toppings" },
  "Çikolatalar / Soslar":   { icon: "🍫", cssClass: "sauces" },
};

function isSauceCategory(cat: string): boolean {
  return cat === "Çikolatalar / Soslar";
}

export default function Home() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const storeId = Number(searchParams?.get("store") || 1);
  const tableId = searchParams?.get("table") ? Number(searchParams.get("table")) : undefined;

  const [categories, setCategories] = useState<IngredientCategory[]>([]);
  const [product, setProduct] = useState<Product | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load menu
  useEffect(() => {
    fetchMenu()
      .then((menu) => {
        setCategories(menu.categories);
        if (menu.products.length > 0) {
          setProduct(menu.products[0]);
        }
        setLoading(false);
      })
      .catch(() => {
        setError("Menü yüklenemedi. Lütfen tekrar deneyin.");
        setLoading(false);
      });
  }, []);

  // Show toast helper
  const showToast = useCallback((msg: string) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg);
    toastTimer.current = setTimeout(() => setToast(null), 2500);
  }, []);

  // Build an ingredient map for price lookups
  const ingredientMap = new Map<number, Ingredient>();
  categories.forEach((cat) =>
    cat.ingredients.forEach((ing) => ingredientMap.set(ing.id, ing))
  );

  // Count selected per category type
  const selectedToppings = Array.from(selected).filter((id) => {
    const ing = ingredientMap.get(id);
    return ing && !isSauceCategory(ing.category);
  }).length;

  const selectedSauces = Array.from(selected).filter((id) => {
    const ing = ingredientMap.get(id);
    return ing && isSauceCategory(ing.category);
  }).length;

  // Toggle ingredient selection
  const toggleIngredient = useCallback(
    (id: number) => {
      const ing = ingredientMap.get(id);
      if (!ing) return;

      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(id)) {
          next.delete(id);
          return next;
        }

        // Check limits
        if (isSauceCategory(ing.category)) {
          const currentSauces = Array.from(next).filter((sid) => {
            const s = ingredientMap.get(sid);
            return s && isSauceCategory(s.category);
          }).length;
          if (currentSauces >= MAX_SAUCES) {
            showToast(`En fazla ${MAX_SAUCES} sos seçebilirsiniz`);
            return prev;
          }
        } else {
          const currentToppings = Array.from(next).filter((sid) => {
            const s = ingredientMap.get(sid);
            return s && !isSauceCategory(s.category);
          }).length;
          if (currentToppings >= MAX_TOPPINGS) {
            showToast(`En fazla ${MAX_TOPPINGS} malzeme seçebilirsiniz`);
            return prev;
          }
        }

        next.add(id);
        return next;
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [categories, showToast]
  );

  // Calculate total price
  const basePrice = product ? parseFloat(product.base_price) : 0;
  const ingredientTotal = Array.from(selected).reduce((sum, id) => {
    const ing = ingredientMap.get(id);
    return sum + (ing ? parseFloat(ing.price) : 0);
  }, 0);
  const totalPrice = basePrice + ingredientTotal;

  // Submit order
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
      router.push(
        `/success?order_id=${res.order_id}&amount=${res.total_amount}`
      );
    } catch {
      showToast("Sipariş gönderilemedi. Tekrar deneyin.");
      setSubmitting(false);
    }
  };

  // --- Render states ---
  if (loading) {
    return (
      <div className="loading-screen">
        <div className="loading-spinner" />
        <p className="loading-text">Menü yükleniyor...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="error-screen">
        <h2>😕</h2>
        <p>{error}</p>
        <button className="retry-btn" onClick={() => window.location.reload()}>
          Tekrar Dene
        </button>
      </div>
    );
  }
  const isDemo = searchParams?.get("demo") === "true";

  // Demo presets — one-tap waffle combos
  const DEMO_PRESETS = [
    { label: "🍫 Klasik", names: ["Nutella", "Muz", "Fındık"] },
    { label: "🎉 Çikolata Şölen", names: ["Kinder Bueno", "Oreo", "Çikolata Topları", "Nutella"] },
    { label: "🍓 Meyveli Taze", names: ["Çilek", "Muz", "Karamel", "Sprinkle"] },
  ];

  const applyPreset = (names: string[]) => {
    const ids = new Set<number>();
    ingredientMap.forEach((ing, id) => {
      if (names.includes(ing.name)) ids.add(id);
    });
    setSelected(ids);
    showToast(`${ids.size} malzeme seçildi ✓`);
  };

  return (
    <div className="page-container">
      {/* Header */}
      <header className="header">
        <div className="header-emoji">🧇</div>
        <h1>Waffle&apos;ını Oluştur</h1>
        <p>Malzemelerini seç, biz hazırlayalım!</p>
      </header>

      {/* Demo Quick Order Presets */}
      {isDemo && categories.length > 0 && (
        <div style={{
          padding: '12px 16px',
          background: 'linear-gradient(135deg, #FEF3C7 0%, #FDE68A 100%)',
          borderBottom: '1px solid #F59E0B33',
        }}>
          <div style={{ fontSize: '12px', fontWeight: 600, color: '#92400E', marginBottom: '8px', textAlign: 'center' }}>
            ⚡ Hızlı Sipariş — Tek Dokunuşla
          </div>
          <div style={{ display: 'flex', gap: '8px', overflowX: 'auto' }}>
            {DEMO_PRESETS.map((preset) => (
              <button
                key={preset.label}
                onClick={() => applyPreset(preset.names)}
                style={{
                  flex: '1 0 auto',
                  padding: '10px 16px',
                  background: 'white',
                  border: '2px solid #D4940A',
                  borderRadius: '12px',
                  fontSize: '13px',
                  fontWeight: 700,
                  color: '#2C1810',
                  cursor: 'pointer',
                  whiteSpace: 'nowrap',
                  boxShadow: '0 2px 8px rgba(212, 148, 10, 0.15)',
                }}
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Category sections */}
      {categories.map((cat) => {
        const meta = CATEGORY_META[cat.name] || {
          icon: "📦",
          cssClass: "toppings",
        };
        return (
          <section key={cat.name} className="category-section">
            <div className="category-header">
              <div className={`category-icon ${meta.cssClass}`}>
                {meta.icon}
              </div>
              <span className="category-name">{cat.name}</span>
            </div>
            <div className="chips-grid">
              {cat.ingredients.map((ing) => (
                <button
                  key={ing.id}
                  className={`chip ${selected.has(ing.id) ? "selected" : ""}`}
                  onClick={() => toggleIngredient(ing.id)}
                  id={`ingredient-${ing.id}`}
                >
                  <div className="chip-info">
                    <span className="chip-name">{ing.name}</span>
                    <span className="chip-price">+₺{parseFloat(ing.price).toFixed(0)}</span>
                  </div>
                  <div className="chip-check">✓</div>
                </button>
              ))}
            </div>
          </section>
        );
      })}

      <div className="bottom-spacer" />

      {/* Sticky bottom bar */}
      <div className="bottom-bar">
        <div className="bottom-bar-inner">
          <div className="price-row">
            <span className="price-label">
              Waffle + {selected.size} malzeme
            </span>
            <span className="price-value">
              ₺{totalPrice.toFixed(0)}
              <span className="currency">,00</span>
            </span>
          </div>
          <button
            className={`order-btn ${submitting ? "loading" : ""}`}
            onClick={handleSubmit}
            disabled={submitting || selected.size === 0}
            id="submit-order-btn"
          >
            {submitting ? (
              "Gönderiliyor..."
            ) : (
              <>
                Siparişi Gönder
                <span className="ing-count">{selected.size}</span>
              </>
            )}
          </button>
        </div>
      </div>

      {/* Toast */}
      <div className={`toast ${toast ? "visible" : ""}`}>{toast}</div>
    </div>
  );
}
