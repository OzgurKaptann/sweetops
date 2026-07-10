"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useAuth } from "@/components/AuthGate";
import { canRefund } from "@/lib/auth";
import {
  ApiError,
  fetchOpenTables,
  fetchRecentTransactions,
  fetchTableBill,
  payOrder,
  refundAllocation,
  searchOrder,
  settleTable,
  type OpenTable,
  type PaymentMethod,
  type RecentTransaction,
  type SettlementReceipt,
  type TableBill,
} from "@/lib/api";
import {
  createCommandIdempotency,
  fingerprintCommand,
} from "@/lib/payment-idempotency";

const money = (v: string) => `${v} ₺`;

// Map backend error codes → the exact Turkish submission strings.
function submitMessageFor(err: unknown): string {
  if (err instanceof ApiError) {
    switch (err.code) {
      case "no_balance":
        return "Bu siparişin ödenecek bakiyesi bulunmuyor.";
      case "idempotency_mismatch":
        return "Aynı işlem anahtarı farklı bilgilerle kullanılamaz.";
      case "refund_over_balance":
        return "Bu işlem için iade edilebilir bakiye bulunmuyor.";
      case "forbidden":
        return "Bu işlem için iade yetkin yok.";
      default:
        return err.message;
    }
  }
  // Network uncertainty: we never learned the result — safe to retry same key.
  return "İşlem sonucu doğrulanamadı. Aynı işlem güvenle tekrar denenebilir.";
}

const PAYMENT_LABEL: Record<string, string> = {
  UNPAID: "Ödenmedi",
  PARTIALLY_PAID: "Kısmi ödendi",
  PAID: "Ödendi",
};

export default function CashierPage() {
  const { user } = useAuth();
  const allowRefund = canRefund(user);

  const [tables, setTables] = useState<OpenTable[]>([]);
  const [bill, setBill] = useState<TableBill | null>(null);
  const [recent, setRecent] = useState<RecentTransaction[]>([]);
  const [query, setQuery] = useState("");
  const [method, setMethod] = useState<PaymentMethod>("CASH");
  const [status, setStatus] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<SettlementReceipt | null>(null);
  const [busy, setBusy] = useState(false);

  // One idempotency store per mounted cashier screen (in-memory only).
  const idem = useRef(createCommandIdempotency());

  const loadTables = useCallback(async () => {
    try {
      const res = await fetchOpenTables();
      setTables(res.tables);
    } catch {
      /* handled globally by 401 or ignored */
    }
  }, []);

  const loadRecent = useCallback(async () => {
    try {
      const res = await fetchRecentTransactions();
      setRecent(res.transactions);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    loadTables();
    loadRecent();
  }, [loadTables, loadRecent]);

  const openTable = useCallback(async (tableId: number) => {
    setStatus(null);
    setReceipt(null);
    try {
      setBill(await fetchTableBill(tableId));
    } catch (err) {
      setStatus(submitMessageFor(err));
    }
  }, []);

  const refreshBill = useCallback(async () => {
    if (bill) setBill(await fetchTableBill(bill.table_id));
    await loadTables();
    await loadRecent();
  }, [bill, loadTables, loadRecent]);

  const onSearch = useCallback(async () => {
    if (!query.trim()) return;
    setStatus(null);
    setReceipt(null);
    try {
      const order = await searchOrder(query.trim());
      if (order.table_id != null) {
        await openTable(order.table_id);
      }
    } catch {
      setStatus("Kayıt bulunamadı.");
    }
  }, [query, openTable]);

  // ── Collection ─────────────────────────────────────────────────────────────

  const payableOrderIds = useMemo(
    () => (bill ? bill.orders.filter((o) => o.payable).map((o) => o.order_id) : []),
    [bill],
  );

  const settleAll = useCallback(async () => {
    if (!bill || payableOrderIds.length === 0) return;
    const fp = fingerprintCommand({
      kind: "collection",
      tableId: bill.table_id,
      orderIds: payableOrderIds,
      paymentMethod: method,
    });
    const { key, alreadyInFlight } = idem.current.begin(fp);
    if (alreadyInFlight) return; // double-click guard

    setBusy(true);
    setStatus("Ödeme kaydediliyor…");
    try {
      const r = await settleTable(
        { table_id: bill.table_id, order_ids: payableOrderIds, payment_method: method },
        key,
      );
      idem.current.complete();
      setReceipt(r);
      setStatus(r.idempotent_replay ? "Bu işlem daha önce tamamlandı." : "Tahsilat Başarılı");
      await refreshBill();
    } catch (err) {
      // Preserve the attempt for a safe retry unless the payload was rejected.
      idem.current.release();
      setStatus(submitMessageFor(err));
    } finally {
      setBusy(false);
    }
  }, [bill, payableOrderIds, method, refreshBill]);

  const payOne = useCallback(
    async (orderId: number) => {
      const fp = fingerprintCommand({
        kind: "collection",
        tableId: bill?.table_id ?? null,
        orderIds: [orderId],
        paymentMethod: method,
      });
      const { key, alreadyInFlight } = idem.current.begin(fp);
      if (alreadyInFlight) return;

      setBusy(true);
      setStatus("Ödeme kaydediliyor…");
      try {
        const r = await payOrder(orderId, { payment_method: method }, key);
        idem.current.complete();
        setReceipt(r);
        setStatus(r.idempotent_replay ? "Bu işlem daha önce tamamlandı." : "Tahsilat Başarılı");
        await refreshBill();
      } catch (err) {
        idem.current.release();
        setStatus(submitMessageFor(err));
      } finally {
        setBusy(false);
      }
    },
    [bill, method, refreshBill],
  );

  return (
    <main className="min-h-screen bg-slate-50 text-slate-900 px-4 py-6 max-w-5xl mx-auto">
      <header className="mb-6">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <span>🧾</span> Kasa
        </h1>
        {user?.store && (
          <p className="text-sm text-slate-500">{user.store.name}</p>
        )}
      </header>

      {/* Search */}
      <section className="mb-6">
        <label className="block text-sm font-medium mb-1" htmlFor="q">Sipariş Ara</label>
        <div className="flex gap-2">
          <input
            id="q"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onSearch()}
            placeholder="Sipariş No (örn. SIP-000123)"
            className="flex-1 border border-slate-300 rounded px-3 py-2 text-sm"
          />
          <button
            onClick={onSearch}
            className="px-4 py-2 rounded bg-slate-800 text-white text-sm hover:bg-slate-900"
          >
            Ara
          </button>
        </div>
      </section>

      <div className="grid md:grid-cols-2 gap-6">
        {/* Open tables */}
        <section>
          <h2 className="text-lg font-semibold mb-2">Açık Masalar</h2>
          <div className="space-y-2">
            {tables.length === 0 && (
              <p className="text-sm text-slate-500">Açık masa yok.</p>
            )}
            {tables.map((t) => (
              <button
                key={t.table_id}
                onClick={() => openTable(t.table_id)}
                className="w-full text-left bg-white rounded-lg shadow-sm border border-slate-200 px-4 py-3 hover:border-indigo-400"
              >
                <div className="flex justify-between">
                  <span className="font-semibold">Masa {t.table_number ?? t.table_id}</span>
                  <span className="text-sm text-slate-500">{t.open_order_count} sipariş</span>
                </div>
                <div className="mt-1 text-sm flex justify-between">
                  <span>Kalan</span>
                  <span className="font-semibold text-indigo-700">{money(t.remaining_amount)}</span>
                </div>
              </button>
            ))}
          </div>
        </section>

        {/* Table bill */}
        <section>
          <h2 className="text-lg font-semibold mb-2">Masa Hesabı</h2>
          {!bill && <p className="text-sm text-slate-500">Bir masa seç.</p>}
          {bill && (
            <div className="bg-white rounded-lg shadow-sm border border-slate-200 p-4 space-y-3">
              <div className="flex justify-between font-semibold">
                <span>Masa {bill.table_number ?? bill.table_id}</span>
                <span className="text-indigo-700">Kalan {money(bill.remaining_amount)}</span>
              </div>

              <table className="w-full text-sm">
                <thead className="text-slate-500 text-left">
                  <tr>
                    <th className="py-1">Sipariş No</th>
                    <th>Hazırlık Durumu</th>
                    <th>Ödeme Durumu</th>
                    <th className="text-right">Sipariş Tutarı</th>
                    <th className="text-right">Ödenen</th>
                    <th className="text-right">Kalan</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {bill.orders.map((o) => (
                    <tr key={o.order_id} className="border-t border-slate-100">
                      <td className="py-1 font-mono text-xs">{o.order_code}</td>
                      <td>{o.preparation_status}</td>
                      <td>{PAYMENT_LABEL[o.payment_status] ?? o.payment_status}</td>
                      <td className="text-right">{o.order_total}</td>
                      <td className="text-right">{o.net_paid}</td>
                      <td className="text-right">{o.remaining_amount}</td>
                      <td className="text-right">
                        {o.payable && (
                          <button
                            onClick={() => payOne(o.order_id)}
                            disabled={busy}
                            className="text-indigo-600 hover:underline text-xs disabled:opacity-50"
                          >
                            Ödeme Al
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {/* Method + pay-all */}
              <div className="flex items-center gap-3 pt-2 border-t border-slate-100">
                <MethodToggle method={method} setMethod={setMethod} />
                <button
                  onClick={settleAll}
                  disabled={busy || payableOrderIds.length === 0}
                  className="ml-auto px-4 py-2 rounded bg-indigo-600 text-white text-sm font-semibold hover:bg-indigo-700 disabled:opacity-50"
                >
                  Tüm Hesabı Kapat
                </button>
              </div>
            </div>
          )}
        </section>
      </div>

      {status && (
        <p className="mt-4 text-sm rounded px-3 py-2 bg-white border border-slate-200 text-slate-700">
          {status}
        </p>
      )}

      {receipt && (
        <Receipt receipt={receipt} allowRefund={allowRefund} onRefunded={refreshBill} />
      )}

      {/* Recent transactions */}
      <section className="mt-8">
        <h2 className="text-lg font-semibold mb-2">İşlem Geçmişi</h2>
        <div className="bg-white rounded-lg shadow-sm border border-slate-200 divide-y divide-slate-100">
          {recent.length === 0 && (
            <p className="text-sm text-slate-500 px-4 py-3">Kayıt yok.</p>
          )}
          {recent.map((t, i) => (
            <div key={i} className="px-4 py-2 text-sm flex justify-between">
              <span>
                {t.kind === "REFUND" ? "İade" : "Tahsilat"}
                {t.payment_method ? ` · ${t.payment_method === "CASH" ? "Nakit" : t.payment_method === "CARD" ? "Kart" : t.payment_method}` : ""}
                {" · "}
                {t.actor_display}
              </span>
              <span className={t.kind === "REFUND" ? "text-red-600" : "text-emerald-700"}>
                {t.kind === "REFUND" ? "-" : ""}{money(t.amount)}
              </span>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}

function MethodToggle({
  method,
  setMethod,
}: {
  method: PaymentMethod;
  setMethod: (m: PaymentMethod) => void;
}) {
  return (
    <div className="inline-flex rounded border border-slate-300 overflow-hidden text-sm">
      <button
        onClick={() => setMethod("CASH")}
        className={`px-3 py-1.5 ${method === "CASH" ? "bg-slate-800 text-white" : "bg-white"}`}
      >
        Nakit
      </button>
      <button
        onClick={() => setMethod("CARD")}
        className={`px-3 py-1.5 ${method === "CARD" ? "bg-slate-800 text-white" : "bg-white"}`}
      >
        Kart
      </button>
    </div>
  );
}

function Receipt({
  receipt,
  allowRefund,
  onRefunded,
}: {
  receipt: SettlementReceipt;
  allowRefund: boolean;
  onRefunded: () => void | Promise<void>;
}) {
  const [openRefund, setOpenRefund] = useState<number | null>(null);
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const idem = useRef(createCommandIdempotency());

  const submitRefund = async (allocationId: number) => {
    const fp = fingerprintCommand({ kind: "refund", allocationId, amount, reason });
    const { key, alreadyInFlight } = idem.current.begin(fp);
    if (alreadyInFlight) return;
    setBusy(true);
    setMsg("İade kaydediliyor…");
    try {
      await refundAllocation(allocationId, { amount, reason }, key);
      idem.current.complete();
      setMsg("İade işlemi tamamlandı.");
      setOpenRefund(null);
      setAmount("");
      setReason("");
      await onRefunded();
    } catch (err) {
      idem.current.release();
      if (err instanceof ApiError && err.code === "refund_over_balance") {
        setMsg("Bu işlem için iade edilebilir bakiye bulunmuyor.");
      } else if (err instanceof ApiError && err.code === "forbidden") {
        setMsg("Bu işlem için iade yetkin yok.");
      } else if (err instanceof ApiError) {
        setMsg(err.message);
      } else {
        setMsg("İşlem sonucu doğrulanamadı. Aynı işlem güvenle tekrar denenebilir.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="mt-4 bg-emerald-50 border border-emerald-200 rounded-lg p-4">
      <h3 className="font-semibold text-emerald-800">
        Tahsilat Başarılı · {receipt.gross_amount} {receipt.currency}
      </h3>
      <p className="text-sm text-emerald-700">
        {receipt.payment_method === "CASH" ? "Nakit" : receipt.payment_method === "CARD" ? "Kart" : receipt.payment_method}
        {" · "}Kasiyer: {receipt.cashier_display}
      </p>
      <ul className="mt-2 space-y-1 text-sm">
        {receipt.allocations.map((a) => (
          <li key={a.id} className="flex items-center justify-between">
            <span className="font-mono text-xs">{a.order_code}</span>
            <span>{money(a.amount)}</span>
            {allowRefund && (
              <button
                onClick={() => setOpenRefund(openRefund === a.id ? null : a.id)}
                className="text-red-600 text-xs hover:underline"
              >
                İade Et
              </button>
            )}
            {openRefund === a.id && (
              <div className="w-full mt-2 flex flex-col gap-2">
                <input
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder="İade tutarı"
                  className="border border-slate-300 rounded px-2 py-1 text-sm"
                />
                <input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder="İade nedeni"
                  className="border border-slate-300 rounded px-2 py-1 text-sm"
                />
                <button
                  onClick={() => submitRefund(a.id)}
                  disabled={busy || !amount || !reason}
                  className="px-3 py-1.5 rounded bg-red-600 text-white text-sm disabled:opacity-50"
                >
                  İade Et
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {msg && <p className="mt-2 text-sm text-slate-700">{msg}</p>}
    </section>
  );
}
