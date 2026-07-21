"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthGate";
import { OrderIssueHistoryTable } from "@/components/OrderIssueHistoryTable";
import { fetchOrderIssues, type OrderIssue } from "@/lib/order-issue-api";
import { ISSUE_HISTORY_COPY } from "@/lib/order-issue-view";

/**
 * Sorunlu siparişler — the owner/manager-facing order-issue history.
 *
 * A store-scoped READ only: owner-web never records or resolves an issue (that is
 * the cashier's / manager's till action). The store comes from the session, so
 * there is no branch picker pointing at another shop. OWNER/MANAGER see every issue
 * in their own store; the server enforces that regardless of this screen.
 */
export default function OrderIssuesPage() {
  const { user } = useAuth();

  const [issues, setIssues] = useState<OrderIssue[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchOrderIssues({
        limit: 100,
        status: statusFilter || undefined,
      });
      setIssues(data.issues);
      setLoadError(null);
    } catch {
      setLoadError(ISSUE_HISTORY_COPY.loadError);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

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
              <span className="text-sm text-gray-500 font-medium">{ISSUE_HISTORY_COPY.heading}</span>
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
              <select
                value={statusFilter}
                onChange={(e) => setStatusFilter(e.target.value)}
                className="text-xs px-2 py-1.5 rounded-lg bg-gray-100 text-gray-600 font-medium"
              >
                <option value="">Tümü</option>
                <option value="OPEN">Açık</option>
                <option value="RESOLVED">Çözüldü</option>
              </select>
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
              {ISSUE_HISTORY_COPY.heading}
            </h2>
            <p className="text-xs text-gray-400 mt-0.5">
              Sipariş başına sorun türü, durum, çözüm ve iade tutarı
            </p>
          </div>
        </div>

        {loadError && (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-3">
            {loadError}
          </p>
        )}

        <OrderIssueHistoryTable issues={issues} loading={loading} />
      </main>
    </div>
  );
}
