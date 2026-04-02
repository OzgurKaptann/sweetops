"use client";

import { useEffect, useState } from "react";
import { fetchPopularCombos } from "@/lib/api";

interface Pair {
  ingredient_1: string;
  ingredient_2: string;
  count: number;
  display: string;
}

interface Triple {
  ingredients: string[];
  count: number;
  display: string;
}

interface ComboData {
  top_pairs: Pair[];
  top_triples: Triple[];
}

const MEDALS = ["🥇", "🥈", "🥉", "4.", "5."];

export function PopularCombosPanel() {
  const [data, setData] = useState<ComboData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchPopularCombos()
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-4">🤝 Popüler Kombinasyonlar</h3>
        <div className="animate-pulse space-y-3">
          {[...Array(3)].map((_, i) => <div key={i} className="h-10 bg-gray-100 rounded" />)}
        </div>
      </div>
    );
  }

  if (!data || data.top_pairs.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-bold text-gray-900 mb-2">🤝 Popüler Kombinasyonlar</h3>
        <p className="text-gray-400 text-sm text-center py-4">
          Henüz yeterli sipariş verisi yok.
        </p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-lg font-bold text-gray-900 mb-4">🤝 Popüler Kombinasyonlar</h3>

      {/* Top pairs */}
      <h4 className="text-sm font-semibold text-gray-500 mb-2">En Çok Birlikte Seçilenler</h4>
      <div className="space-y-2 mb-4">
        {data.top_pairs.map((pair, i) => (
          <div key={pair.display} className="flex items-center justify-between p-3 bg-purple-50 rounded-lg">
            <div className="flex items-center gap-2">
              <span className="text-sm">{MEDALS[i] || `${i + 1}.`}</span>
              <span className="font-semibold text-gray-900 text-sm">{pair.display}</span>
            </div>
            <span className="text-purple-700 font-bold text-sm">{pair.count}×</span>
          </div>
        ))}
      </div>

      {/* Top triple */}
      {data.top_triples.length > 0 && (
        <>
          <h4 className="text-sm font-semibold text-gray-500 mb-2">En Popüler Üçlü</h4>
          <div className="space-y-2">
            {data.top_triples.map((triple) => (
              <div key={triple.display} className="flex items-center justify-between p-3 bg-indigo-50 rounded-lg">
                <div className="flex items-center gap-2">
                  <span className="text-sm">⭐</span>
                  <span className="font-semibold text-gray-900 text-sm">{triple.display}</span>
                </div>
                <span className="text-indigo-700 font-bold text-sm">{triple.count}×</span>
              </div>
            ))}
          </div>
        </>
      )}

      <p className="text-xs text-gray-400 mt-4 text-center">
        Müşteriler bunları birlikte tercih ediyor
      </p>
    </div>
  );
}
