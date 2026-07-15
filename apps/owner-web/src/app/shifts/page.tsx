"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthGate";
import { ShiftHistoryTable } from "@/components/ShiftHistoryTable";
import { fetchShifts, type Shift } from "@/lib/shift-api";
import { SHIFT_HISTORY_COPY } from "@/lib/shift-view";

/**
 * Vardiya geçmişi — the manager-facing shift history.
 *
 * A store-scoped READ only: owner-web never opens or closes a shift (that is the
 * cashier's till action). The store comes from the session, so there is no branch
 * picker pointing at another shop's tills. OWNER/MANAGER see every shift in their
 * own store; the server enforces that regardless of this screen.
 */
export default function ShiftsPage() {
  const { user } = useAuth();

  const [shifts, setShifts] = useState<Shift[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchShifts({ limit: 100 });
      setShifts(data.shifts);
      setLoadError(null);
    } catch {
      setLoadError(SHIFT_HISTORY_COPY.loadError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="min-h-screen bg-[#f8f9fa]">
      <header className="bg-white border-b border-gray-200 sticky top-0 z-20">
        <div className="max-w-screen-xl mx-auto px-6">
          <div className="flex items-center justify-between h-14 gap-4">
            <div className="flex items-center gap-3 min-w-0">
              <span className="text-base font-bold text-gray-900 tracking-tight">SweetOps</span>
              <span className="text-gray-300 text-sm">|</span>
              <span className="text-sm text-gray-500 font-medium">{SHIFT_HISTORY_COPY.heading}</span>
              {user?.store && (
                <>
                  <span className="text-gray-300 text-sm hidden sm:inline">·</span>
                  <span className="text-xs text-gray-500 hidden sm:inline truncate">
                    Şube: {user.store.name}
                  </span>
                </>
              )}
            </div>
            <div className="flex items-center gap-3 shrink-0">
              <button
                onClick={load}
                className="text-xs px-3 py-1.5 rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors font-medium"
              >
                ↻ Yenile
              </button>
              <a href="/" className="text-xs text-gray-400 hover:text-gray-600 transition-colors">
                ← Panel
              </a>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-screen-xl mx-auto px-6 py-6 space-y-4">
        <div className="flex items-baseline gap-3">
          <div className="w-1 h-5 rounded-full bg-indigo-500 shrink-0" />
          <div>
            <h2 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">
              {SHIFT_HISTORY_COPY.heading}
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Kasiyer başına açılış, kapanış, beklenen ve sayılan kasa ile eksik/fazla
            </p>
          </div>
        </div>

        {loadError && (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-3">
            {loadError}
          </p>
        )}

        <ShiftHistoryTable shifts={shifts} loading={loading} />
      </main>
    </div>
  );
}
