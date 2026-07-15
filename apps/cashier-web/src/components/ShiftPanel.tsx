"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "@/lib/api";
import {
  closeShift,
  fetchCurrentShift,
  openShift,
  type Shift,
} from "@/lib/shift-api";
import {
  createCommandIdempotency,
} from "@/lib/payment-idempotency";
import {
  SHIFT_COPY,
  SHIFT_LABELS,
  discrepancyClass,
  discrepancyLabel,
  fingerprintShiftCommand,
  shiftStatusLabel,
  validateCountedCash,
  validateOpeningCash,
} from "@/lib/shift-view";

const money = (v: string | null) => (v == null ? "—" : `${v} ₺`);

function openMessageFor(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "idempotency_mismatch") return SHIFT_COPY.openUncertain;
    return err.message;
  }
  return SHIFT_COPY.openUncertain;
}

function closeMessageFor(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "already_closed") return "Bu vardiya zaten kapatılmış.";
    return err.message;
  }
  // Network uncertainty on a close: never invite a blind retry.
  return SHIFT_COPY.closeUncertain;
}

/**
 * The till reconciliation panel. Sits at the top of the cashier screen so the
 * cashier can open a shift at the start of the day and close it at the end. It
 * never blocks payment collection — a missing shift is a soft warning only.
 */
export default function ShiftPanel({
  onShiftChange,
}: {
  onShiftChange?: (hasOpenShift: boolean) => void;
}) {
  const [shift, setShift] = useState<Shift | null>(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [openingCash, setOpeningCash] = useState("");
  const [openNote, setOpenNote] = useState("");
  const [countedCash, setCountedCash] = useState("");
  const [closeNote, setCloseNote] = useState("");
  const [showClose, setShowClose] = useState(false);
  const [closedSummary, setClosedSummary] = useState<Shift | null>(null);

  const idem = useRef(createCommandIdempotency());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetchCurrentShift();
      setShift(res.current_shift);
      onShiftChange?.(res.current_shift != null);
    } catch {
      /* handled globally by 401 or ignored */
    } finally {
      setLoading(false);
    }
  }, [onShiftChange]);

  useEffect(() => {
    load();
  }, [load]);

  const openingErrors = useMemo(
    () => (openingCash.trim() === "" ? [] : validateOpeningCash(openingCash)),
    [openingCash],
  );
  const countedErrors = useMemo(
    () => (countedCash.trim() === "" ? [] : validateCountedCash(countedCash)),
    [countedCash],
  );

  const submitOpen = useCallback(async () => {
    const errs = validateOpeningCash(openingCash);
    if (errs.length > 0) {
      setStatus(errs[0]);
      return;
    }
    const fp = fingerprintShiftCommand({
      kind: "shift_open",
      openingCash: openingCash.trim(),
      openNote: openNote.trim() || null,
    });
    const { key, alreadyInFlight } = idem.current.begin(fp);
    if (alreadyInFlight) return;

    setBusy(true);
    setStatus("Vardiya açılıyor…");
    try {
      const s = await openShift(
        { opening_cash_amount: openingCash.trim(), open_note: openNote.trim() || null },
        key,
      );
      idem.current.complete();
      setShift(s);
      onShiftChange?.(true);
      setOpeningCash("");
      setOpenNote("");
      setStatus(SHIFT_COPY.openSuccess);
    } catch (err) {
      idem.current.release();
      setStatus(openMessageFor(err));
    } finally {
      setBusy(false);
    }
  }, [openingCash, openNote, onShiftChange]);

  const submitClose = useCallback(async () => {
    if (!shift) return;
    const errs = validateCountedCash(countedCash);
    if (errs.length > 0) {
      setStatus(errs[0]);
      return;
    }
    const fp = fingerprintShiftCommand({
      kind: "shift_close",
      shiftId: shift.id,
      countedCash: countedCash.trim(),
      closeNote: closeNote.trim() || null,
    });
    const { key, alreadyInFlight } = idem.current.begin(fp);
    if (alreadyInFlight) return;

    setBusy(true);
    setStatus("Vardiya kapatılıyor…");
    try {
      const s = await closeShift(
        shift.id,
        { counted_closing_cash_amount: countedCash.trim(), close_note: closeNote.trim() || null },
        key,
      );
      idem.current.complete();
      setClosedSummary(s);
      setShift(null);
      onShiftChange?.(false);
      setShowClose(false);
      setCountedCash("");
      setCloseNote("");
      setStatus(SHIFT_COPY.closeSuccess);
    } catch (err) {
      idem.current.release();
      setStatus(closeMessageFor(err));
    } finally {
      setBusy(false);
    }
  }, [shift, countedCash, closeNote, onShiftChange]);

  if (loading) {
    return (
      <section className="bg-white rounded-lg shadow-sm border border-slate-200 p-4 mb-6">
        <p className="text-sm text-slate-500">Vardiya durumu yükleniyor…</p>
      </section>
    );
  }

  return (
    <section className="bg-white rounded-lg shadow-sm border border-slate-200 p-4 mb-6">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold flex items-center gap-2">
          <span>🗄️</span> Vardiya
        </h2>
        {shift && (
          <span className="text-xs px-2 py-1 rounded-full bg-emerald-100 text-emerald-800 font-medium">
            {shiftStatusLabel(shift.status)}
          </span>
        )}
      </div>

      {/* No open shift → open form */}
      {!shift && (
        <div>
          <p className="text-sm text-slate-500 mb-3">{SHIFT_COPY.noOpenShift}</p>
          <div className="flex flex-col gap-2 max-w-sm">
            <label className="text-sm font-medium" htmlFor="opening-cash">
              {SHIFT_LABELS.openingCash}
            </label>
            <input
              id="opening-cash"
              inputMode="decimal"
              value={openingCash}
              onChange={(e) => setOpeningCash(e.target.value)}
              placeholder="örn. 200.00"
              className="border border-slate-300 rounded px-3 py-2 text-sm"
            />
            {openingErrors.length > 0 && (
              <p className="text-xs text-red-600">{openingErrors[0]}</p>
            )}
            <input
              value={openNote}
              onChange={(e) => setOpenNote(e.target.value)}
              placeholder="Not (isteğe bağlı)"
              className="border border-slate-300 rounded px-3 py-2 text-sm"
            />
            <button
              onClick={submitOpen}
              disabled={busy || openingCash.trim() === "" || openingErrors.length > 0}
              className="mt-1 px-4 py-2 rounded bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50"
            >
              {SHIFT_LABELS.open}
            </button>
          </div>
        </div>
      )}

      {/* Open shift → details + close */}
      {shift && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm max-w-md">
            <span className="text-slate-500">{SHIFT_LABELS.openedAt}</span>
            <span className="text-right font-mono text-xs">
              {new Date(shift.opened_at).toLocaleString("tr-TR")}
            </span>
            <span className="text-slate-500">{SHIFT_LABELS.openingCash}</span>
            <span className="text-right font-semibold">{money(shift.opening_cash_amount)}</span>
            <span className="text-slate-500">Kasiyer</span>
            <span className="text-right">{shift.cashier_display}</span>
          </div>

          {!showClose && (
            <button
              onClick={() => {
                setStatus(null);
                setShowClose(true);
              }}
              className="px-4 py-2 rounded bg-slate-800 text-white text-sm font-semibold hover:bg-slate-900"
            >
              {SHIFT_LABELS.close}
            </button>
          )}

          {showClose && (
            <div className="flex flex-col gap-2 max-w-sm border-t border-slate-100 pt-3">
              <label className="text-sm font-medium" htmlFor="counted-cash">
                {SHIFT_LABELS.countedCash}
              </label>
              <input
                id="counted-cash"
                inputMode="decimal"
                value={countedCash}
                onChange={(e) => setCountedCash(e.target.value)}
                placeholder="Kasadaki sayılan nakit"
                className="border border-slate-300 rounded px-3 py-2 text-sm"
              />
              {countedErrors.length > 0 && (
                <p className="text-xs text-red-600">{countedErrors[0]}</p>
              )}
              <input
                value={closeNote}
                onChange={(e) => setCloseNote(e.target.value)}
                placeholder="Not (isteğe bağlı)"
                className="border border-slate-300 rounded px-3 py-2 text-sm"
              />
              <div className="flex gap-2 mt-1">
                <button
                  onClick={submitClose}
                  disabled={busy || countedCash.trim() === "" || countedErrors.length > 0}
                  className="px-4 py-2 rounded bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50"
                >
                  {SHIFT_LABELS.close}
                </button>
                <button
                  onClick={() => setShowClose(false)}
                  className="px-4 py-2 rounded border border-slate-300 text-sm hover:bg-slate-50"
                >
                  Vazgeç
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Closed summary */}
      {closedSummary && <ClosedSummary shift={closedSummary} />}

      {status && (
        <p className="mt-3 text-sm rounded px-3 py-2 bg-slate-50 border border-slate-200 text-slate-700">
          {status}
        </p>
      )}
    </section>
  );
}

function ClosedSummary({ shift }: { shift: Shift }) {
  const klass = discrepancyClass(shift.cash_discrepancy_amount);
  const tone =
    klass === "balanced"
      ? "bg-emerald-50 border-emerald-200 text-emerald-800"
      : klass === "short"
        ? "bg-red-50 border-red-200 text-red-700"
        : "bg-amber-50 border-amber-200 text-amber-800";

  const row = (label: string, value: string | null) => (
    <>
      <span className="text-slate-500">{label}</span>
      <span className="text-right font-semibold">{value == null ? "—" : `${value} ₺`}</span>
    </>
  );

  return (
    <div className={`mt-3 rounded-lg border p-4 ${tone}`}>
      <h3 className="font-semibold mb-2">Vardiya kapandı</h3>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm max-w-md">
        {row(SHIFT_LABELS.openingCash, shift.opening_cash_amount)}
        {row(SHIFT_LABELS.cashPayments, shift.cash_payments_amount)}
        {row(SHIFT_LABELS.cashRefunds, shift.cash_refunds_amount)}
        {row(SHIFT_LABELS.cardPayments, shift.card_payments_amount)}
        {row(SHIFT_LABELS.cardRefunds, shift.card_refunds_amount)}
        {row(SHIFT_LABELS.grossPayments, shift.gross_payments_amount)}
        {row(SHIFT_LABELS.totalRefunds, shift.total_refunds_amount)}
        {row(SHIFT_LABELS.netCollected, shift.net_collected_amount)}
        {row(SHIFT_LABELS.expectedCash, shift.expected_closing_cash_amount)}
        {row(SHIFT_LABELS.countedCashShort, shift.counted_closing_cash_amount)}
        <span className="text-slate-500">{SHIFT_LABELS.discrepancy}</span>
        <span className="text-right font-bold">
          {discrepancyLabel(shift.cash_discrepancy_amount)}
          {" · "}
          {shift.cash_discrepancy_amount} ₺
        </span>
      </div>
    </div>
  );
}
