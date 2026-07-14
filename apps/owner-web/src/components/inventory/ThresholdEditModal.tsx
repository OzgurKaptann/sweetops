"use client";

import { useMemo, useRef, useState } from "react";

import {
  updateThresholds,
  type ThresholdAlertItem,
} from "@/lib/inventory-api";
import { inventoryErrorMessage, isOutcomeUncertain } from "@/lib/inventory-errors";
import {
  createCommandIdempotency,
  fingerprintCommand,
  type InventoryCommand,
} from "@/lib/inventory-idempotency";
import {
  INVENTORY_COPY,
  THRESHOLD_CLEAR_HINT,
  THRESHOLD_HINT,
  THRESHOLD_LABELS,
  type OperationBanner,
  type ThresholdFormInput,
  formatQuantity,
  thresholdBanner,
  thresholdRequestBody,
  thresholdStatusLabel,
  validateThresholdForm,
} from "@/lib/inventory-view";

/**
 * Eşik düzenle — set the levels at which this branch wants to be warned.
 *
 * The dialog's most important sentence is the hint at the top: this changes NO stock.
 * A manager who is not certain of that will not touch the form, and an alert system
 * nobody configures never fires. The backend guarantees it (the endpoint writes no
 * ledger movement and cannot), and the copy says it.
 *
 * Idempotency works exactly as it does for the stock commands, and for a reason that
 * is worth spelling out even though no stock moves: a retried form must not re-log the
 * decision or re-stamp `threshold_updated_at`. That timestamp is what an owner reads
 * to ask who moved a warning level and when, and it is worthless if pressing the
 * button twice moves it. So an unchanged form REUSES its key, an edited form mints a
 * new one, and a double-click is swallowed outright.
 *
 * An empty field means NOT CONFIGURED — and it is sent as null, never as "0". Zero is a
 * real threshold ("warn me only when it is actually gone"), so a manager who clears a
 * field and one who types 0 have made different decisions and the form must not merge
 * them.
 */

const EMPTY: ThresholdFormInput = {
  ingredientId: null,
  critical: "",
  minimum: "",
  target: "",
  reason: "",
};

export function ThresholdEditModal({
  items,
  initialIngredientId,
  onClose,
  onSuccess,
}: {
  items: ThresholdAlertItem[];
  initialIngredientId: number | null;
  onClose: () => void;
  onSuccess: (banner: OperationBanner) => void;
}) {
  const [form, setForm] = useState<ThresholdFormInput>(() => {
    const item = items.find((i) => i.ingredient_id === initialIngredientId);
    if (!item) return { ...EMPTY, ingredientId: initialIngredientId };
    // Pre-fill with the thresholds ALREADY in force. The body states the complete
    // configuration, so a manager who came here to change only the critical level must
    // not silently clear the other two by leaving them blank.
    return {
      ingredientId: item.ingredient_id,
      critical: item.critical_quantity ?? "",
      minimum: item.minimum_quantity ?? "",
      target: item.target_quantity ?? "",
      reason: "",
    };
  });

  const [errors, setErrors] = useState<string[]>([]);
  const [failure, setFailure] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // A ref, so a re-render never resets the attempt and loses its key.
  const idempotency = useRef(createCommandIdempotency());

  const selected = useMemo(
    () => items.find((i) => i.ingredient_id === form.ingredientId) ?? null,
    [items, form.ingredientId],
  );

  const set = (patch: Partial<ThresholdFormInput>) =>
    setForm((prev) => ({ ...prev, ...patch }));

  /** Switching ingredient reloads that ingredient's CURRENT thresholds into the form. */
  const selectIngredient = (raw: string) => {
    const id = raw === "" ? null : Number(raw);
    const item = items.find((i) => i.ingredient_id === id);
    setForm((prev) => ({
      ...prev,
      ingredientId: id,
      critical: item?.critical_quantity ?? "",
      minimum: item?.minimum_quantity ?? "",
      target: item?.target_quantity ?? "",
    }));
  };

  const submit = async () => {
    const found = validateThresholdForm(form);
    setErrors(found);
    setFailure(null);
    setUncertain(false);
    if (found.length > 0 || form.ingredientId === null) return;

    const body = thresholdRequestBody(form);
    const command: InventoryCommand = {
      kind: "threshold_update",
      ingredientId: form.ingredientId,
      criticalQuantity: body.critical_quantity,
      minimumQuantity: body.minimum_quantity,
      targetQuantity: body.target_quantity,
      reason: body.reason,
    };

    const { key, alreadyInFlight } = idempotency.current.begin(
      fingerprintCommand(command),
    );
    // A double-click is not a second decision. Swallow it rather than fire twice.
    if (alreadyInFlight) return;

    setSubmitting(true);
    try {
      const receipt = await updateThresholds(form.ingredientId, body, key);
      idempotency.current.complete();
      onSuccess(thresholdBanner({ replay: receipt.idempotent_replay }));
      onClose();
    } catch (err) {
      // The outcome is UNKNOWN, not failed. Keep the attempt (and its key) alive and
      // leave the form intact, so a resubmit is the SAME command under the SAME key
      // rather than a new decision.
      idempotency.current.release();
      setFailure(inventoryErrorMessage(err, "threshold_update"));
      setUncertain(isOutcomeUncertain(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-30 bg-black/40 flex items-center justify-center p-4">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-md max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100">
          <h3 className="text-sm font-semibold text-gray-900">Eşik düzenle</h3>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600"
            aria-label="Kapat"
          >
            ✕
          </button>
        </div>

        <div className="px-5 py-4 space-y-4">
          {/* The sentence that makes this form safe to use. */}
          <p className="text-xs text-gray-600 bg-blue-50 border border-blue-100 rounded-lg px-3 py-2">
            {THRESHOLD_HINT}
          </p>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              {THRESHOLD_LABELS.ingredient}
            </label>
            <select
              value={form.ingredientId ?? ""}
              onChange={(e) => selectIngredient(e.target.value)}
              className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2"
            >
              <option value="">Seçin…</option>
              {items.map((item) => (
                <option key={item.ingredient_id} value={item.ingredient_id}>
                  {item.ingredient_name}
                </option>
              ))}
            </select>
          </div>

          {/* The stock these thresholds will be judged against — and its current
              verdict. Shown because a manager setting "warn me at 3 kg" needs to know
              they have 2.5 kg available right now. Read-only: this dialog cannot
              change any of it. */}
          {selected && (
            <div className="text-xs text-gray-500 bg-gray-50 border border-gray-100 rounded-lg px-3 py-2 space-y-1">
              <div className="flex justify-between">
                <span>{THRESHOLD_LABELS.available}</span>
                <span className="tabular-nums font-medium text-gray-700">
                  {formatQuantity(selected.available_quantity)} {selected.unit}
                </span>
              </div>
              <div className="flex justify-between">
                <span>{THRESHOLD_LABELS.status}</span>
                <span className="font-medium text-gray-700">
                  {thresholdStatusLabel(selected.status)}
                </span>
              </div>
            </div>
          )}

          <div className="grid grid-cols-3 gap-2">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                {THRESHOLD_LABELS.critical}
              </label>
              <input
                type="number"
                inputMode="decimal"
                step="0.001"
                value={form.critical}
                onChange={(e) => set({ critical: e.target.value })}
                className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 tabular-nums"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                {THRESHOLD_LABELS.minimum}
              </label>
              <input
                type="number"
                inputMode="decimal"
                step="0.001"
                value={form.minimum}
                onChange={(e) => set({ minimum: e.target.value })}
                className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 tabular-nums"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                {THRESHOLD_LABELS.target}
              </label>
              <input
                type="number"
                inputMode="decimal"
                step="0.001"
                value={form.target}
                onChange={(e) => set({ target: e.target.value })}
                className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2 tabular-nums"
              />
            </div>
          </div>
          <p className="text-xs text-gray-400">{THRESHOLD_CLEAR_HINT}</p>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">
              {THRESHOLD_LABELS.reason}
            </label>
            <input
              type="text"
              value={form.reason}
              onChange={(e) => set({ reason: e.target.value })}
              maxLength={500}
              className="w-full text-sm border border-gray-200 rounded-lg px-3 py-2"
            />
          </div>

          {errors.length > 0 && (
            <ul className="text-xs text-red-700 bg-red-50 border border-red-200 rounded-lg px-3 py-2 space-y-1">
              {errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          )}

          {/* An uncertain outcome is NOT a failure, and is not coloured like one:
              amber, with an instruction to go and look — never an invitation to press
              the button again. */}
          {failure && (
            <p
              role="alert"
              className={`text-xs border rounded-lg px-3 py-2 ${
                uncertain
                  ? "text-amber-800 bg-amber-50 border-amber-200"
                  : "text-red-700 bg-red-50 border-red-200"
              }`}
            >
              {failure}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 py-4 border-t border-gray-100">
          <button
            onClick={onClose}
            className="text-sm px-4 py-2 rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50"
          >
            Vazgeç
          </button>
          <button
            onClick={submit}
            disabled={submitting}
            className="text-sm px-4 py-2 rounded-lg bg-indigo-600 text-white font-medium hover:bg-indigo-700 disabled:opacity-50"
          >
            {submitting ? INVENTORY_COPY.loading : "Eşikleri kaydet"}
          </button>
        </div>
      </div>
    </div>
  );
}
