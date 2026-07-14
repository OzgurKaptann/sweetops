"use client";

import { useMemo, useRef, useState } from "react";

import {
  createManualAdjustment,
  createPurchaseReceipt,
  createStockCount,
  createTransfer,
  createWaste,
  type StockItem,
  type TransferDestination,
} from "@/lib/inventory-api";
import {
  inventoryErrorMessage,
  isOutcomeUncertain,
} from "@/lib/inventory-errors";
import {
  createCommandIdempotency,
  fingerprintCommand,
  type InventoryCommand,
} from "@/lib/inventory-idempotency";
import {
  INVENTORY_COPY,
  MANUAL_ADJUSTMENT_HINT,
  OPERATION_TITLE,
  STOCK_COUNT_HINT,
  STOCK_COUNT_LABELS,
  type OperationBanner,
  type OperationKind,
  expectedCountDelta,
  formatDelta,
  formatQuantity,
  successBanner,
  validateAdjustmentForm,
  validatePurchaseReceiptForm,
  validateStockCountForm,
  validateTransferForm,
  validateWasteForm,
} from "@/lib/inventory-view";

/**
 * The five stock operations, in one dialog.
 *
 * Idempotency, in one place: a `CommandIdempotency` lives for the life of this
 * dialog (a ref, so a re-render never resets it). On submit, the form is
 * fingerprinted; an unchanged command retried after a failure REUSES its key, and
 * an edited command mints a new one. A double-click is swallowed outright
 * (`alreadyInFlight`), so the second click cannot become a second ledger row. The
 * key is never rendered — it is a replay token.
 *
 * The other rule this dialog enforces: a network-uncertain outcome is NOT reported
 * as a failure, and the form is left intact rather than cleared, so the manager can
 * check the ledger and — if the operation never landed — resubmit the SAME command
 * under the SAME key.
 *
 * The physical count is the odd one out, and deliberately so. Every other form asks
 * for a CHANGE (add 5 kg, bin 2 kg, ship 1 kg); the count asks what is ACTUALLY
 * THERE and shows the system's figures beside it so the manager can see the
 * difference before they commit to it. The difference itself is only ever a preview
 * — the server recomputes it from the row it locks.
 */

// Titles live with the rest of this screen's Turkish copy, in lib/inventory-view.ts.
const TITLE = OPERATION_TITLE;

const SUBMIT_LABEL: Record<OperationKind, string> = {
  purchase_receipt: "Mal kabul kaydet",
  waste: "Fire kaydet",
  manual_adjustment: "Manuel düzeltme kaydet",
  transfer: "Şube transferi oluştur",
  stock_count: "Sayımı uygula",
};

const REASON_LABEL: Record<OperationKind, string> = {
  purchase_receipt: "Sebep / açıklama",
  waste: "Fire sebebi",
  manual_adjustment: "Sebep",
  transfer: "Sebep",
  stock_count: "Sebep",
};

const REASON_PLACEHOLDER: Record<OperationKind, string> = {
  purchase_receipt: "Örn. Tedarikçi teslimatı",
  waste: "Örn. Yanan hamur",
  manual_adjustment: "Örn. Sayım farkı",
  transfer: "Örn. Beşiktaş şubesine takviye",
  stock_count: "Örn. Haftalık dolap sayımı",
};

export function InventoryActionModal({
  kind,
  stock,
  destinations,
  sourceStoreId,
  initialIngredientId,
  onClose,
  onSuccess,
}: {
  kind: OperationKind;
  stock: StockItem[];
  destinations: TransferDestination[];
  sourceStoreId: number | null;
  initialIngredientId: number | null;
  onClose: () => void;
  /** Reports the result banner AND asks the page to reload stock + movements. */
  onSuccess: (banner: OperationBanner) => void;
}) {
  const [ingredientId, setIngredientId] = useState<number | null>(initialIngredientId);
  const [quantity, setQuantity] = useState("");
  const [delta, setDelta] = useState("");
  /** What was physically found on the shelf. NOT a delta — see STOCK_COUNT_HINT. */
  const [counted, setCounted] = useState("");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [destinationStoreId, setDestinationStoreId] = useState<number | null>(null);

  const [errors, setErrors] = useState<string[]>([]);
  const [failure, setFailure] = useState<OperationBanner | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Survives re-renders; one attempt-tracker per dialog.
  const idempotency = useRef(createCommandIdempotency());

  const selected = useMemo(
    () => stock.find((s) => s.ingredient_id === ingredientId) ?? null,
    [stock, ingredientId],
  );

  // Every operation — purchase receipt included — acts on a stock row that already
  // exists in this branch: the service loads (and locks) that row and 404s
  // `stock_not_configured` when it is missing. So the picker offers exactly the
  // ingredients the backend will accept, and no more. Creating the first stock row
  // for an ingredient is not a thing this screen can do.
  const ingredientOptions = stock;

  const buildCommand = (): InventoryCommand | null => {
    if (ingredientId === null) return null;
    switch (kind) {
      case "purchase_receipt":
        return {
          kind: "purchase_receipt",
          ingredientId,
          quantity: quantity.trim(),
          reason: reason.trim() || null,
        };
      case "waste":
        return {
          kind: "waste",
          ingredientId,
          quantity: quantity.trim(),
          reason: reason.trim(),
        };
      case "manual_adjustment":
        return {
          kind: "manual_adjustment",
          ingredientId,
          delta: delta.trim(),
          reason: reason.trim(),
        };
      case "transfer":
        if (destinationStoreId === null) return null;
        return {
          kind: "transfer",
          destinationStoreId,
          ingredientId,
          quantity: quantity.trim(),
          reason: reason.trim(),
          note: note.trim() || null,
        };
      case "stock_count":
        return {
          kind: "stock_count",
          ingredientId,
          countedQuantity: counted.trim(),
          reason: reason.trim(),
          note: note.trim() || null,
        };
    }
  };

  const validate = (): string[] => {
    switch (kind) {
      case "purchase_receipt":
        return validatePurchaseReceiptForm({ ingredientId, quantity, reason });
      case "waste":
        return validateWasteForm({ ingredientId, quantity, reason });
      case "manual_adjustment":
        return validateAdjustmentForm({ ingredientId, delta, reason });
      case "transfer":
        return validateTransferForm({
          sourceStoreId,
          destinationStoreId,
          ingredientId,
          quantity,
          reason,
          availableQuantity: selected?.available_quantity ?? null,
        });
      case "stock_count":
        return validateStockCountForm({
          ingredientId,
          counted,
          reason,
          onHandQuantity: selected?.on_hand_quantity ?? null,
          reservedQuantity: selected?.reserved_quantity ?? null,
        });
    }
  };

  // The difference the count is EXPECTED to apply. Preview only: the server
  // recomputes it from the row it locks, and between this render and the request
  // landing an order may have moved the shelf.
  const previewDelta =
    kind === "stock_count"
      ? expectedCountDelta(counted, selected?.on_hand_quantity ?? null)
      : null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setFailure(null);

    const found = validate();
    setErrors(found);
    if (found.length > 0) return;

    const command = buildCommand();
    if (!command) return;

    const { key, alreadyInFlight } = idempotency.current.begin(fingerprintCommand(command));
    if (alreadyInFlight) return; // double-click: the first request is still running.

    setSubmitting(true);
    try {
      let replay = false;
      // A count that found the shelf CORRECT writes no ledger row. That is a
      // success with its own message, not a failure and not a replay.
      let noDelta = false;

      if (command.kind === "purchase_receipt") {
        const receipt = await createPurchaseReceipt(
          {
            ingredient_id: command.ingredientId,
            quantity: command.quantity,
            reason: command.reason,
          },
          key,
        );
        replay = receipt.idempotent_replay;
      } else if (command.kind === "waste") {
        const receipt = await createWaste(
          {
            ingredient_id: command.ingredientId,
            quantity: command.quantity,
            reason: command.reason,
          },
          key,
        );
        replay = receipt.idempotent_replay;
      } else if (command.kind === "manual_adjustment") {
        const receipt = await createManualAdjustment(
          {
            ingredient_id: command.ingredientId,
            delta: command.delta,
            reason: command.reason,
          },
          key,
        );
        replay = receipt.idempotent_replay;
      } else if (command.kind === "transfer") {
        const receipt = await createTransfer(
          {
            destination_store_id: command.destinationStoreId,
            ingredient_id: command.ingredientId,
            quantity: command.quantity,
            reason: command.reason,
            note: command.note,
          },
          key,
        );
        replay = receipt.idempotent_replay;
      } else {
        const receipt = await createStockCount(
          {
            ingredient_id: command.ingredientId,
            counted_quantity: command.countedQuantity,
            reason: command.reason,
            note: command.note,
          },
          key,
        );
        replay = receipt.idempotent_replay;
        // `movement_id: null` means the shelf agreed with the system. The count WAS
        // applied — it is just that nothing physical had to move.
        noDelta = receipt.movement_id === null;
      }

      // A confirmed outcome — the key has done its job and must not be reused.
      idempotency.current.complete();
      onSuccess(successBanner(kind, { replay, noDelta }));
      onClose();
    } catch (err) {
      // The attempt survives: an unchanged retry reuses this key, so a request that
      // may already have landed cannot land twice.
      idempotency.current.release();
      setFailure({
        tone: isOutcomeUncertain(err) ? "warning" : "error",
        // `kind` only changes the network-uncertain copy, which has to speak the
        // manager's vocabulary: they are holding a count sheet, not an "işlem".
        message: inventoryErrorMessage(err, kind),
      });
    } finally {
      setSubmitting(false);
    }
  };

  const showQuantity = kind !== "manual_adjustment" && kind !== "stock_count";
  const reasonRequired = kind !== "purchase_receipt";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      role="dialog"
      aria-modal="true"
      aria-label={TITLE[kind]}
    >
      <form
        onSubmit={submit}
        className="w-full max-w-md bg-white rounded-xl shadow-lg p-6 space-y-4 max-h-[90vh] overflow-y-auto"
      >
        <div className="flex items-start justify-between">
          <h2 className="text-base font-bold text-gray-900">{TITLE[kind]}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 text-sm"
            aria-label="Kapat"
          >
            ✕
          </button>
        </div>

        {kind === "manual_adjustment" && (
          <p className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            {MANUAL_ADJUSTMENT_HINT}
          </p>
        )}

        {kind === "stock_count" && (
          <p className="text-xs text-blue-800 bg-blue-50 border border-blue-200 rounded-lg px-3 py-2">
            {STOCK_COUNT_HINT}
          </p>
        )}

        {/* Malzeme */}
        <div>
          <label htmlFor="ingredient" className="block text-sm font-medium text-gray-700 mb-1">
            Malzeme
          </label>
          <select
            id="ingredient"
            value={ingredientId ?? ""}
            onChange={(e) => setIngredientId(e.target.value ? Number(e.target.value) : null)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          >
            <option value="">Malzeme seçin</option>
            {ingredientOptions.map((s) => (
              <option key={s.ingredient_id} value={s.ingredient_id}>
                {s.ingredient_name}
              </option>
            ))}
          </select>
          {selected && (
            <p className="text-xs text-gray-500 mt-1">
              Kullanılabilir stok: {formatQuantity(selected.available_quantity)} {selected.unit}
              {" · "}
              Fiziksel stok: {formatQuantity(selected.on_hand_quantity)} {selected.unit}
            </p>
          )}
        </div>

        {/* Hedef şube (transfer only) */}
        {kind === "transfer" && (
          <div>
            <label htmlFor="destination" className="block text-sm font-medium text-gray-700 mb-1">
              Hedef şube
            </label>
            {destinations.length === 0 ? (
              <p className="text-xs text-gray-500 bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                {INVENTORY_COPY.destinationsEmpty}
              </p>
            ) : (
              <select
                id="destination"
                value={destinationStoreId ?? ""}
                onChange={(e) =>
                  setDestinationStoreId(e.target.value ? Number(e.target.value) : null)
                }
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              >
                <option value="">Hedef şube seçin</option>
                {destinations.map((d) => (
                  <option key={d.store_id} value={d.store_id}>
                    {d.name}
                  </option>
                ))}
              </select>
            )}
          </div>
        )}

        {/* Miktar */}
        {showQuantity && (
          <div>
            <label htmlFor="quantity" className="block text-sm font-medium text-gray-700 mb-1">
              Miktar {selected ? `(${selected.unit})` : ""}
            </label>
            <input
              id="quantity"
              type="number"
              inputMode="decimal"
              step="0.001"
              min="0"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
        )}

        {/* Sayım sonucu (stock count only — an ABSOLUTE quantity, not a delta) */}
        {kind === "stock_count" && (
          <div>
            <label htmlFor="counted" className="block text-sm font-medium text-gray-700 mb-1">
              Sayım sonucu {selected ? `(${selected.unit})` : ""}
            </label>
            <input
              id="counted"
              type="number"
              inputMode="decimal"
              step="0.001"
              // min=0, not min>0: an empty shelf is a valid count, and the one a
              // manager most needs to be able to report.
              min="0"
              value={counted}
              onChange={(e) => setCounted(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
            <p className="text-xs text-gray-500 mt-1">
              Rafta fiziksel olarak saydığınız miktarı girin.
            </p>

            {/* The three figures the manager is reconciling against, and the
                difference their count would apply. Read from the API; the expected
                difference is a preview — the server recomputes it under a lock. */}
            {selected && (
              <dl className="mt-3 space-y-1.5 text-xs bg-gray-50 border border-gray-200 rounded-lg px-3 py-2">
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-gray-500">{STOCK_COUNT_LABELS.systemOnHand}</dt>
                  <dd className="tabular-nums text-gray-900 font-medium">
                    {formatQuantity(selected.on_hand_quantity)} {selected.unit}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-gray-500">{STOCK_COUNT_LABELS.reserved}</dt>
                  <dd className="tabular-nums text-gray-700">
                    {formatQuantity(selected.reserved_quantity)} {selected.unit}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-gray-500">{STOCK_COUNT_LABELS.available}</dt>
                  <dd className="tabular-nums text-gray-700">
                    {formatQuantity(selected.available_quantity)} {selected.unit}
                  </dd>
                </div>
                <div className="flex items-center justify-between gap-3 pt-1.5 border-t border-gray-200">
                  <dt className="text-gray-600 font-medium">
                    {STOCK_COUNT_LABELS.expectedDelta}
                  </dt>
                  <dd
                    className={`tabular-nums font-semibold ${
                      previewDelta === null || previewDelta === 0
                        ? "text-gray-500"
                        : previewDelta > 0
                          ? "text-emerald-700"
                          : "text-red-700"
                    }`}
                  >
                    {previewDelta === null
                      ? "—"
                      : `${formatDelta(previewDelta)} ${selected.unit}`}
                  </dd>
                </div>
              </dl>
            )}
          </div>
        )}

        {/* Düzeltme miktarı (manual adjustment only — signed) */}
        {kind === "manual_adjustment" && (
          <div>
            <label htmlFor="delta" className="block text-sm font-medium text-gray-700 mb-1">
              Düzeltme miktarı {selected ? `(${selected.unit})` : ""}
            </label>
            <input
              id="delta"
              type="number"
              inputMode="decimal"
              step="0.001"
              value={delta}
              onChange={(e) => setDelta(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
            <p className="text-xs text-gray-500 mt-1">
              Sayımda fazla çıktıysa artı (örn. 2), eksik çıktıysa eksi (örn. −2) girin.
            </p>
          </div>
        )}

        {/* Sebep */}
        <div>
          <label htmlFor="reason" className="block text-sm font-medium text-gray-700 mb-1">
            {REASON_LABEL[kind]}
            {!reasonRequired && <span className="text-gray-400 font-normal"> (isteğe bağlı)</span>}
          </label>
          <input
            id="reason"
            type="text"
            maxLength={500}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={REASON_PLACEHOLDER[kind]}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
        </div>

        {/* Not (transfer and stock count) */}
        {(kind === "transfer" || kind === "stock_count") && (
          <div>
            <label htmlFor="note" className="block text-sm font-medium text-gray-700 mb-1">
              Not <span className="text-gray-400 font-normal">(isteğe bağlı)</span>
            </label>
            <input
              id="note"
              type="text"
              maxLength={500}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
        )}

        {/* Client-side validation */}
        {errors.length > 0 && (
          <ul className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2 space-y-1">
            {errors.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        )}

        {/* Server refusal, or an outcome we could not confirm */}
        {failure && (
          <p
            className={`text-sm rounded-lg px-3 py-2 border ${
              failure.tone === "warning"
                ? "text-amber-800 bg-amber-50 border-amber-200"
                : "text-red-700 bg-red-50 border-red-200"
            }`}
          >
            {failure.message}
          </p>
        )}

        <div className="flex gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 py-2.5 rounded-lg border border-gray-200 text-gray-600 text-sm font-medium hover:bg-gray-50"
          >
            Vazgeç
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="flex-1 py-2.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white text-sm font-semibold disabled:opacity-60"
          >
            {submitting ? "Kaydediliyor…" : SUBMIT_LABEL[kind]}
          </button>
        </div>
      </form>
    </div>
  );
}
