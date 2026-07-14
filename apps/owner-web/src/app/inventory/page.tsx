"use client";

import { useCallback, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthGate";
import { InventoryActionModal } from "@/components/inventory/InventoryActionModal";
import { MovementHistoryTable } from "@/components/inventory/MovementHistoryTable";
import { StockOverviewTable } from "@/components/inventory/StockOverviewTable";
import {
  fetchMovements,
  fetchStock,
  fetchTransferDestinations,
  type MovementItem,
  type StockItem,
  type TransferDestination,
} from "@/lib/inventory-api";
import { inventoryErrorMessage } from "@/lib/inventory-errors";
import {
  INVENTORY_ACTIONS,
  INVENTORY_COPY,
  type OperationBanner,
  type OperationKind,
} from "@/lib/inventory-view";

/**
 * Stok Yönetimi — the owner/manager inventory screen.
 *
 * Reads need `inventory:read`; every stock OPERATION needs `inventory:adjust`. A
 * role that holds the former and not the latter (none today, but the permission
 * matrix allows it and KITCHEN is one grant away) gets the tables and no buttons —
 * the UI hides what it cannot do rather than offering a button that 403s. The
 * server enforces this regardless; hiding it is courtesy, not the control.
 *
 * The store is never chosen here. It comes from the session, server-side, and
 * there is no branch picker to point at somebody else's stock.
 */

const MOVEMENT_LIMIT = 100;

// The action list (and its Turkish copy) lives in lib/inventory-view.ts, where the
// rest of this screen's copy is written and unit-tested.
const BANNER_STYLE: Record<OperationBanner["tone"], string> = {
  success: "bg-emerald-50 border-emerald-200 text-emerald-800",
  info: "bg-blue-50 border-blue-200 text-blue-800",
  warning: "bg-amber-50 border-amber-200 text-amber-800",
  error: "bg-red-50 border-red-200 text-red-700",
};

export default function InventoryPage() {
  const { user } = useAuth();
  const canAdjust = user?.permissions.includes("inventory:adjust") ?? false;
  const sourceStoreId = user?.store?.id ?? null;

  const [stock, setStock] = useState<StockItem[]>([]);
  const [movements, setMovements] = useState<MovementItem[]>([]);
  const [destinations, setDestinations] = useState<TransferDestination[]>([]);

  const [stockLoading, setStockLoading] = useState(true);
  const [movementsLoading, setMovementsLoading] = useState(true);
  const [movementType, setMovementType] = useState("");

  const [banner, setBanner] = useState<OperationBanner | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [action, setAction] = useState<OperationKind | null>(null);
  const [actionIngredientId, setActionIngredientId] = useState<number | null>(null);

  const loadStock = useCallback(async () => {
    setStockLoading(true);
    try {
      const data = await fetchStock();
      setStock(data.items);
      setLoadError(null);
    } catch (err) {
      setLoadError(inventoryErrorMessage(err));
    } finally {
      setStockLoading(false);
    }
  }, []);

  const loadMovements = useCallback(async () => {
    setMovementsLoading(true);
    try {
      const data = await fetchMovements({
        movementType: movementType || undefined,
        limit: MOVEMENT_LIMIT,
      });
      setMovements(data.items);
    } catch (err) {
      setLoadError(inventoryErrorMessage(err));
    } finally {
      setMovementsLoading(false);
    }
  }, [movementType]);

  useEffect(() => {
    loadStock();
  }, [loadStock]);

  useEffect(() => {
    loadMovements();
  }, [loadMovements]);

  // Only needed by the transfer form, and only by a manager who may transfer.
  useEffect(() => {
    if (!canAdjust) return;
    fetchTransferDestinations()
      .then((data) => setDestinations(data.items))
      .catch(() => setDestinations([]));
  }, [canAdjust]);

  const openAction = (kind: OperationKind) => {
    setBanner(null);
    setAction(kind);
  };

  const handleSuccess = useCallback(
    (result: OperationBanner) => {
      setBanner(result);
      // The receipt already carries the new stock figures, but the ledger and the
      // other ingredients may have moved too. Re-read rather than patch locally:
      // the backend is the source of truth for stock, not this component.
      loadStock();
      loadMovements();
    },
    [loadStock, loadMovements],
  );

  return (
    <div className="min-h-screen bg-[#f8f9fa]">
      <header className="bg-white border-b border-gray-200 sticky top-0 z-20">
        <div className="max-w-screen-xl mx-auto px-6">
          <div className="flex items-center justify-between h-14 gap-4">
            <div className="flex items-center gap-3 min-w-0">
              <span className="text-base font-bold text-gray-900 tracking-tight">SweetOps</span>
              <span className="text-gray-300 text-sm">|</span>
              <span className="text-sm text-gray-500 font-medium">Stok Yönetimi</span>
              {user?.store && (
                <>
                  <span className="text-gray-300 text-sm hidden sm:inline">·</span>
                  <span className="text-xs text-gray-500 hidden sm:inline truncate">
                    Şube: {user.store.name}
                  </span>
                </>
              )}
            </div>
            <a
              href="/"
              className="text-xs text-gray-400 hover:text-gray-600 transition-colors shrink-0"
            >
              ← Panel
            </a>
          </div>
        </div>
      </header>

      <main className="max-w-screen-xl mx-auto px-6 py-6 space-y-6">
        {/* Actions */}
        {canAdjust ? (
          <div className="flex flex-wrap gap-2">
            {INVENTORY_ACTIONS.map(({ kind, label, primary }) => (
              <button
                key={kind}
                onClick={() => {
                  setActionIngredientId(null);
                  openAction(kind);
                }}
                className={`text-sm px-4 py-2 rounded-lg font-medium transition-colors ${
                  primary
                    ? "bg-indigo-600 text-white hover:bg-indigo-700"
                    : "bg-white border border-gray-200 text-gray-700 hover:bg-gray-50"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        ) : (
          <p className="text-sm text-gray-600 bg-white border border-gray-200 rounded-lg px-4 py-3">
            {INVENTORY_COPY.readOnly}
          </p>
        )}

        {/* Operation result */}
        {banner && (
          <div
            role="status"
            className={`flex items-start justify-between gap-3 text-sm border rounded-lg px-4 py-3 ${BANNER_STYLE[banner.tone]}`}
          >
            <span>{banner.message}</span>
            <button
              onClick={() => setBanner(null)}
              className="shrink-0 opacity-60 hover:opacity-100"
              aria-label="Kapat"
            >
              ✕
            </button>
          </div>
        )}

        {/* Read failure (a read changed nothing, so it is safe to state plainly) */}
        {loadError && (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-3">
            {loadError}
          </p>
        )}

        <section className="space-y-3">
          <div className="flex items-baseline gap-3">
            <div className="w-1 h-5 rounded-full bg-amber-500 shrink-0" />
            <h2 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">
              Stok durumu
            </h2>
          </div>
          <StockOverviewTable
            items={stock}
            loading={stockLoading}
            onSelectIngredient={
              canAdjust
                ? (ingredientId) => {
                    setActionIngredientId(ingredientId);
                    openAction("purchase_receipt");
                  }
                : undefined
            }
          />
        </section>

        <section className="space-y-3">
          <div className="flex items-baseline gap-3">
            <div className="w-1 h-5 rounded-full bg-blue-500 shrink-0" />
            <h2 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">
              Stok hareketleri
            </h2>
          </div>
          <MovementHistoryTable
            items={movements}
            loading={movementsLoading}
            movementType={movementType}
            onMovementTypeChange={setMovementType}
          />
        </section>
      </main>

      {action && (
        <InventoryActionModal
          kind={action}
          stock={stock}
          destinations={destinations}
          sourceStoreId={sourceStoreId}
          initialIngredientId={actionIngredientId}
          onClose={() => setAction(null)}
          onSuccess={handleSuccess}
        />
      )}
    </div>
  );
}
