"use client";

import { useCallback, useEffect, useState } from "react";

import { canRefund, type StaffProfile } from "@/lib/auth";
import { ApiError } from "@/lib/api";
import {
  createOrderIssue,
  fetchOrderIssues,
  resolveOrderIssue,
  type IssueType,
  type OrderIssue,
  type ResolutionType,
} from "@/lib/order-issue-api";
import {
  ISSUE_COPY,
  ISSUE_LABELS,
  ISSUE_TYPE_ORDER,
  RESOLUTION_ORDER,
  fingerprintIssueCommand,
  issueStatusLabel,
  issueTypeLabel,
  resolutionActionLabel,
  resolutionLabel,
  resolutionNeedsRefundPermission,
  validatePartialRefund,
  validateRequestedRefund,
} from "@/lib/order-issue-view";
import { createCommandIdempotency, generateIdempotencyKey } from "@/lib/payment-idempotency";

/**
 * Order issue action for one order, opened from the cashier's payment panel.
 *
 * Two flows, both Turkish, both idempotent:
 *   • Sorun kaydet  — record a problem (moves no money, no stock).
 *   • Sorunu çöz    — resolve an OPEN issue (İadesiz / Sadece iptal / Tam iade /
 *                     Kısmi iade). A refund resolution needs payments:refund; the
 *                     buttons for it are hidden for a plain cashier.
 *
 * No raw enum ever reaches the DOM — everything renders through the label helpers.
 */
export function OrderIssuePanel({
  orderId,
  orderCode,
  profile,
  onClose,
}: {
  orderId: number;
  orderCode: string;
  profile: StaffProfile | null;
  onClose: () => void;
}) {
  const [issues, setIssues] = useState<OrderIssue[]>([]);
  const [refundable, setRefundable] = useState<string>("0.00");
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Create form.
  const [issueType, setIssueType] = useState<IssueType>("CUSTOMER_CANCELLED");
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");

  const mayRefund = canRefund(profile);
  const [createIdem] = useState(() => createCommandIdempotency());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchOrderIssues(orderId);
      setIssues(data.issues);
      if (data.issues.length > 0) {
        setRefundable(data.issues[0].order_refundable_amount);
      }
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : ISSUE_COPY.uncertain);
    } finally {
      setLoading(false);
    }
  }, [orderId]);

  useEffect(() => {
    load();
  }, [load]);

  const submitCreate = async () => {
    if (!reason.trim()) {
      setMsg(ISSUE_COPY.reasonRequired);
      return;
    }
    setBusy(true);
    setMsg(null);
    const fp = fingerprintIssueCommand({
      kind: "issue_create",
      orderId,
      issueType,
      reason: reason.trim(),
      note: note.trim() || null,
    });
    const { key } = createIdem.begin(fp);
    try {
      await createOrderIssue(
        orderId,
        { issue_type: issueType, reason: reason.trim(), note: note.trim() || null },
        key,
      );
      createIdem.complete();
      setReason("");
      setNote("");
      setMsg(ISSUE_COPY.createSuccess);
      await load();
    } catch (e) {
      createIdem.release();
      setMsg(e instanceof ApiError ? e.message : ISSUE_COPY.uncertain);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-white rounded-lg shadow-sm border border-slate-200 p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold text-slate-800">
          {ISSUE_LABELS.panelTitle} · <span className="font-mono text-xs">{orderCode}</span>
        </h3>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-600 text-sm">
          ✕ Kapat
        </button>
      </div>

      <p className="text-xs text-slate-500">
        {ISSUE_LABELS.remainingRefundable}: <span className="font-semibold">{refundable} ₺</span>
      </p>

      {msg && (
        <p className="text-sm px-3 py-2 rounded bg-slate-50 border border-slate-200 text-slate-700">
          {msg}
        </p>
      )}

      {/* ── Create form ─────────────────────────────────────────────── */}
      <div className="space-y-2 border-t border-slate-100 pt-3">
        <p className="text-xs font-medium text-slate-600">{ISSUE_LABELS.record}</p>
        <select
          value={issueType}
          onChange={(e) => setIssueType(e.target.value as IssueType)}
          className="w-full text-sm border border-slate-200 rounded px-2 py-1.5"
        >
          {ISSUE_TYPE_ORDER.map((t) => (
            <option key={t} value={t}>
              {issueTypeLabel(t)}
            </option>
          ))}
        </select>
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={ISSUE_LABELS.reason}
          className="w-full text-sm border border-slate-200 rounded px-2 py-1.5"
        />
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder={ISSUE_LABELS.note}
          className="w-full text-sm border border-slate-200 rounded px-2 py-1.5"
        />
        <button
          onClick={submitCreate}
          disabled={busy}
          className="text-sm px-3 py-1.5 rounded bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50"
        >
          {ISSUE_LABELS.record}
        </button>
      </div>

      {/* ── Existing issues ─────────────────────────────────────────── */}
      <div className="space-y-2 border-t border-slate-100 pt-3">
        {loading ? (
          <p className="text-xs text-slate-400">Yükleniyor…</p>
        ) : issues.length === 0 ? (
          <p className="text-xs text-slate-400">{ISSUE_COPY.noOpenIssues}</p>
        ) : (
          issues.map((issue) => (
            <IssueRow
              key={issue.id}
              issue={issue}
              mayRefund={mayRefund}
              refundable={refundable}
              onResolved={load}
              setBusy={setBusy}
              busy={busy}
              setMsg={setMsg}
            />
          ))
        )}
      </div>
    </div>
  );
}

function IssueRow({
  issue,
  mayRefund,
  refundable,
  onResolved,
  setBusy,
  busy,
  setMsg,
}: {
  issue: OrderIssue;
  mayRefund: boolean;
  refundable: string;
  onResolved: () => Promise<void>;
  setBusy: (b: boolean) => void;
  busy: boolean;
  setMsg: (m: string | null) => void;
}) {
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const isOpen = issue.status === "OPEN";

  const resolve = async (resolution: ResolutionType) => {
    const why = reason.trim() || "çözüldü";
    if (resolution === "PARTIAL_REFUND") {
      const errs = validatePartialRefund(amount, refundable);
      if (errs.length) {
        setMsg(errs[0]);
        return;
      }
    }
    setBusy(true);
    setMsg(null);
    const key = generateIdempotencyKey();
    try {
      await resolveOrderIssue(
        issue.id,
        {
          resolution_type: resolution,
          approved_refund_amount: resolution === "PARTIAL_REFUND" ? amount : null,
          reason: why,
        },
        key,
      );
      setMsg(
        resolutionNeedsRefundPermission(resolution)
          ? ISSUE_COPY.refundCreated
          : ISSUE_COPY.resolveSuccess,
      );
      await onResolved();
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : ISSUE_COPY.uncertain);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="text-sm border border-slate-100 rounded p-2 space-y-1">
      <div className="flex justify-between">
        <span>{issueTypeLabel(issue.issue_type)}</span>
        <span className="text-xs text-slate-500">{issueStatusLabel(issue.status)}</span>
      </div>
      <div className="text-xs text-slate-500">{issue.reason}</div>
      {!isOpen && (
        <div className="text-xs text-emerald-700">
          {resolutionLabel(issue.resolution_type)}
          {issue.approved_refund_amount && issue.approved_refund_amount !== "0.00" && (
            <span className="ml-1">· {issue.approved_refund_amount} ₺</span>
          )}
        </div>
      )}
      {isOpen && (
        <div className="space-y-1 pt-1">
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={ISSUE_LABELS.reason}
            className="w-full text-xs border border-slate-200 rounded px-2 py-1"
          />
          <div className="flex flex-wrap gap-1">
            <button
              onClick={() => resolve("NO_REFUND")}
              disabled={busy}
              className="text-xs px-2 py-1 rounded bg-slate-100 hover:bg-slate-200 disabled:opacity-50"
            >
              {resolutionActionLabel("NO_REFUND")}
            </button>
            <button
              onClick={() => resolve("CANCEL_ONLY")}
              disabled={busy}
              className="text-xs px-2 py-1 rounded bg-slate-100 hover:bg-slate-200 disabled:opacity-50"
            >
              {resolutionActionLabel("CANCEL_ONLY")}
            </button>
            {mayRefund && (
              <>
                <button
                  onClick={() => resolve("FULL_REFUND")}
                  disabled={busy}
                  className="text-xs px-2 py-1 rounded bg-red-50 text-red-700 hover:bg-red-100 disabled:opacity-50"
                >
                  {resolutionActionLabel("FULL_REFUND")}
                </button>
                <div className="flex gap-1 items-center">
                  <input
                    value={amount}
                    onChange={(e) => setAmount(e.target.value)}
                    placeholder={ISSUE_LABELS.approvedRefund}
                    className="w-24 text-xs border border-slate-200 rounded px-2 py-1"
                  />
                  <button
                    onClick={() => resolve("PARTIAL_REFUND")}
                    disabled={busy}
                    className="text-xs px-2 py-1 rounded bg-red-50 text-red-700 hover:bg-red-100 disabled:opacity-50"
                  >
                    {resolutionActionLabel("PARTIAL_REFUND")}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
