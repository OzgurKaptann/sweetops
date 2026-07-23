"use client";

import { useState } from "react";

import {
  EMPTY_PRODUCT_FORM,
  buildProductCreateBody,
  type ProductFormValues,
} from "@/lib/setup-view";
import type { ProductCreateBody } from "@/lib/setup-api";

/**
 * Add a product to the catalog, and — only if the manager ticks the box — to this
 * branch's menu.
 *
 * The checkbox is the whole reason the API has a `publish_to_current_store` flag
 * at all. "Add my menu" is the honest first-run flow, but a create that published
 * silently would be the same shape that put test debris one render away from a
 * customer's phone. So the publication is still a decision somebody took, it is
 * scoped to THIS branch by the session, and the label says which branch.
 *
 * Validation is courtesy only: `buildProductCreateBody` spares a round-trip, and
 * the server re-checks every rule and answers with its own Turkish sentence.
 */
export function ProductFormModal({
  storeName,
  submitting,
  serverError,
  onSubmit,
  onClose,
}: {
  storeName: string | null;
  submitting: boolean;
  /** Turkish, already resolved through setup-errors.ts. */
  serverError: string | null;
  onSubmit: (body: ProductCreateBody) => void;
  onClose: () => void;
}) {
  const [values, setValues] = useState<ProductFormValues>(EMPTY_PRODUCT_FORM);
  const [localError, setLocalError] = useState<string | null>(null);

  const set = <K extends keyof ProductFormValues>(
    key: K,
    value: ProductFormValues[K],
  ) => setValues((v) => ({ ...v, [key]: value }));

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const result = buildProductCreateBody(values);
    if (!result.ok || !result.body) {
      setLocalError(result.error);
      return;
    }
    setLocalError(null);
    onSubmit(result.body);
  };

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-md bg-white rounded-xl shadow-lg p-6 space-y-4"
      >
        <div>
          <h2 className="text-base font-semibold text-gray-900">Yeni ürün</h2>
          <p className="text-xs text-gray-500 mt-1 leading-relaxed">
            Ürün işletme kataloğuna eklenir. Misafirlerin görmesi için şube
            menüsüne ayrıca eklenmesi gerekir.
          </p>
        </div>

        <div>
          <label
            htmlFor="product-name"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Ürün adı
          </label>
          <input
            id="product-name"
            type="text"
            value={values.name}
            onChange={(e) => set("name", e.target.value)}
            className="w-full border border-gray-300 rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            autoFocus
          />
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label
              htmlFor="product-category"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Kategori <span className="text-gray-400 font-normal">(isteğe bağlı)</span>
            </label>
            <input
              id="product-category"
              type="text"
              value={values.category}
              onChange={(e) => set("category", e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label
              htmlFor="product-price"
              className="block text-sm font-medium text-gray-700 mb-1"
            >
              Fiyat (₺)
            </label>
            <input
              id="product-price"
              type="text"
              inputMode="decimal"
              placeholder="129,90"
              value={values.price}
              onChange={(e) => set("price", e.target.value)}
              className="w-full border border-gray-300 rounded px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
        </div>

        <label className="flex items-start gap-2.5 text-sm text-gray-700 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2.5 cursor-pointer">
          <input
            type="checkbox"
            checked={values.publishToCurrentStore}
            onChange={(e) => set("publishToCurrentStore", e.target.checked)}
            className="mt-0.5"
          />
          <span>
            Bu ürünü hemen{" "}
            <strong>{storeName ? `${storeName} şubesinin` : "bu şubenin"}</strong>{" "}
            menüsüne ekle
            <span className="block text-xs text-gray-500 mt-0.5">
              İşaretlemezseniz ürün oluşturulur ama hiçbir şubenin menüsünde
              görünmez.
            </span>
          </span>
        </label>

        {(localError || serverError) && (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
            {localError ?? serverError}
          </p>
        )}

        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="text-sm px-3 py-2 rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50 font-medium"
          >
            Vazgeç
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="text-sm px-4 py-2 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-60 font-semibold"
          >
            {submitting ? "Kaydediliyor…" : "Ürünü kaydet"}
          </button>
        </div>
      </form>
    </div>
  );
}
