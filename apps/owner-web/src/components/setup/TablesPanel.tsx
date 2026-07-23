"use client";

import { useState } from "react";

import { SETUP_COPY, type TableRow } from "@/lib/setup-view";

/**
 * Tables and their QR stickers.
 *
 * Note what this panel CANNOT show: a scannable link for an existing table. The
 * raw token is stored only as a SHA-256 hash, so it exists in cleartext exactly
 * once — in the response that created it. That is a security property, not a
 * missing feature, and the copy says so rather than leaving a manager hunting for
 * a "show QR" button that could not exist.
 *
 * What is shown instead is what a manager can act on: whether the table can be
 * scanned at all, the non-secret prefix that identifies the printed sticker, and
 * when a guest last used it — a table nobody has scanned in weeks is usually a
 * sticker that fell off.
 */
export function TablesPanel({
  rows,
  loading,
  busyTableId,
  onCreate,
  onRename,
  onIssueQr,
  onRotateQr,
}: {
  rows: TableRow[];
  loading: boolean;
  busyTableId: number | null;
  onCreate: (tableNumber: string) => void;
  onRename: (row: TableRow, next: string) => void;
  onIssueQr: (row: TableRow) => void;
  onRotateQr: (row: TableRow) => void;
}) {
  const [newTableNumber, setNewTableNumber] = useState("");
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const submitNew = (e: React.FormEvent) => {
    e.preventDefault();
    const value = newTableNumber.trim();
    if (!value) return;
    onCreate(value);
    setNewTableNumber("");
  };

  return (
    <section className="bg-white border border-gray-200 rounded-xl overflow-hidden">
      <header className="px-5 py-4 border-b border-gray-100 flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 className="text-sm font-semibold text-gray-900">
            {SETUP_COPY.tablesHeading}
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">
            {SETUP_COPY.tablesSubheading}
          </p>
        </div>
        <form onSubmit={submitNew} className="flex items-center gap-2">
          <label htmlFor="new-table-number" className="sr-only">
            Masa adı veya numarası
          </label>
          <input
            id="new-table-number"
            type="text"
            placeholder="Masa adı / no"
            value={newTableNumber}
            onChange={(e) => setNewTableNumber(e.target.value)}
            className="w-40 border border-gray-300 rounded-lg px-3 py-1.5 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
          <button
            type="submit"
            disabled={!newTableNumber.trim()}
            className="text-xs px-3 py-2 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 font-medium whitespace-nowrap"
          >
            Masa ekle
          </button>
        </form>
      </header>

      {loading ? (
        <p className="px-5 py-6 text-sm text-gray-500">Masalar yükleniyor…</p>
      ) : rows.length === 0 ? (
        <p className="px-5 py-6 text-sm text-gray-600 leading-relaxed">
          {SETUP_COPY.emptyTables}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-gray-50 text-gray-500">
              <tr>
                <th className="text-left font-medium px-5 py-2">Masa</th>
                <th className="text-left font-medium px-3 py-2">QR durumu</th>
                <th className="text-left font-medium px-3 py-2">Kod öneki</th>
                <th className="text-left font-medium px-3 py-2">Son okutma</th>
                <th className="text-right font-medium px-5 py-2">İşlem</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {rows.map((row) => {
                const busy = busyTableId === row.tableId;
                return (
                  <tr key={row.tableId} className="hover:bg-gray-50/60">
                    <td className="px-5 py-3">
                      {renamingId === row.tableId ? (
                        <div className="flex items-center gap-2">
                          <input
                            type="text"
                            aria-label={`${row.displayName} yeni adı`}
                            value={renameValue}
                            onChange={(e) => setRenameValue(e.target.value)}
                            className="w-32 border border-gray-300 rounded px-2 py-1 text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-indigo-500"
                            autoFocus
                          />
                          <button
                            type="button"
                            disabled={busy || !renameValue.trim()}
                            onClick={() => {
                              onRename(row, renameValue.trim());
                              setRenamingId(null);
                            }}
                            className="text-xs px-2 py-1 rounded border border-gray-200 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                          >
                            Kaydet
                          </button>
                          <button
                            type="button"
                            onClick={() => setRenamingId(null)}
                            className="text-xs text-gray-400 hover:text-gray-600"
                          >
                            Vazgeç
                          </button>
                        </div>
                      ) : (
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-gray-900">
                            {row.displayName}
                          </span>
                          <button
                            type="button"
                            onClick={() => {
                              setRenamingId(row.tableId);
                              setRenameValue(row.tableNumber);
                            }}
                            className="text-xs text-gray-400 hover:text-gray-600"
                          >
                            Adı değiştir
                          </button>
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <span
                        title={row.qrDetail}
                        className={`text-[11px] font-medium px-2 py-0.5 rounded-full whitespace-nowrap ${
                          row.hasQr
                            ? "bg-emerald-100 text-emerald-800"
                            : "bg-amber-100 text-amber-900"
                        }`}
                      >
                        {row.qrStatusLabel}
                      </span>
                      <div className="text-xs text-gray-400 mt-0.5">
                        {row.qrDetail}
                      </div>
                    </td>
                    <td className="px-3 py-3 font-mono text-xs text-gray-600">
                      {row.tokenPrefix}
                    </td>
                    <td className="px-3 py-3 text-gray-600 whitespace-nowrap">
                      {row.lastUsedAt}
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-2 justify-end flex-wrap">
                        {row.hasQr ? (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => onRotateQr(row)}
                            // Destructive: it kills the sticker on the table. The
                            // handler confirms with a sentence saying exactly that.
                            className="text-xs px-2.5 py-1.5 rounded-lg border border-red-200 text-red-700 hover:bg-red-50 disabled:opacity-50 font-medium"
                          >
                            QR kodunu yenile
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => onIssueQr(row)}
                            className="text-xs px-2.5 py-1.5 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 font-medium"
                          >
                            QR kodu oluştur
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <p className="px-5 py-3 text-xs text-gray-500 bg-gray-50 border-t border-gray-100 leading-relaxed">
        QR bağlantısı güvenlik nedeniyle yalnızca oluşturulduğu anda gösterilir ve
        sonradan görüntülenemez. Bağlantıyı kaybederseniz QR kodunu yenilemeniz
        gerekir; yenilediğinizde masadaki basılı kod geçersiz olur.
      </p>
    </section>
  );
}
