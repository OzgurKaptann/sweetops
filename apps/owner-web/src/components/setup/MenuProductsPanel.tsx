"use client";

import { useState } from "react";

import { SETUP_COPY, type MenuRow } from "@/lib/setup-view";

/**
 * The branch's menu, and the rest of the catalog underneath it.
 *
 * Both halves are on one screen because both are needed to answer a manager's
 * actual question. "What do guests see?" needs the publication rows; "what can I
 * add?" needs the catalog — and a manager who can only see what they have already
 * published cannot publish anything else, which is exactly the position every
 * branch is in the moment the fail-closed menu ships.
 *
 * Every status is a Turkish sentence from ./setup-view.ts. No enum, no boolean and
 * no wire value is rendered anywhere in this file.
 */

const STATE_STYLE: Record<string, string> = {
  on_menu: "bg-emerald-100 text-emerald-800",
  sold_out: "bg-amber-100 text-amber-900",
  retired: "bg-gray-200 text-gray-700",
  not_on_menu: "bg-gray-100 text-gray-500",
};

export interface MenuActions {
  onPublish: (row: MenuRow) => void;
  onUnpublish: (row: MenuRow) => void;
  onToggleAvailability: (row: MenuRow, next: boolean) => void;
  onSortOrder: (row: MenuRow, next: number) => void;
  onToggleActive: (row: MenuRow, next: boolean) => void;
}

function StateBadge({ row }: { row: MenuRow }) {
  return (
    <span
      title={row.stateDetail}
      className={`text-[11px] font-medium px-2 py-0.5 rounded-full whitespace-nowrap ${
        STATE_STYLE[row.state] ?? "bg-gray-100 text-gray-600"
      }`}
    >
      {row.stateLabel}
    </span>
  );
}

/**
 * The menu-order input.
 *
 * Committed on blur or Enter rather than on every keystroke: typing "12" through
 * an on-change handler would fire a request for position 1 first, and the menu
 * would visibly jump before settling.
 */
function SortOrderInput({
  row,
  onCommit,
  disabled,
}: {
  row: MenuRow;
  onCommit: (next: number) => void;
  disabled: boolean;
}) {
  const [value, setValue] = useState(String(row.sortOrder ?? 0));

  const commit = () => {
    const parsed = Number(value);
    if (!Number.isInteger(parsed) || parsed < 0) {
      setValue(String(row.sortOrder ?? 0));
      return;
    }
    if (parsed === row.sortOrder) return;
    onCommit(parsed);
  };

  return (
    <input
      type="number"
      min={0}
      inputMode="numeric"
      aria-label={`${row.name} menü sırası`}
      value={value}
      disabled={disabled}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      className="w-16 border border-gray-300 rounded px-2 py-1 text-sm text-gray-900 disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-indigo-500"
    />
  );
}

export function MenuProductsPanel({
  published,
  catalog,
  actions,
  busyProductId,
  loading,
}: {
  published: MenuRow[];
  catalog: MenuRow[];
  actions: MenuActions;
  /** The row with a request in flight — its controls are disabled, not hidden. */
  busyProductId: number | null;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-5 text-sm text-gray-500">
        Menü yükleniyor…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* ── This branch's menu ────────────────────────────────────────────── */}
      <section className="bg-white border border-gray-200 rounded-xl overflow-hidden">
        <header className="px-5 py-4 border-b border-gray-100">
          <h2 className="text-sm font-semibold text-gray-900">
            {SETUP_COPY.menuHeading}
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">
            {SETUP_COPY.menuSubheading}
          </p>
        </header>

        {published.length === 0 ? (
          <p className="px-5 py-6 text-sm text-gray-600 leading-relaxed">
            {SETUP_COPY.emptyMenu}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  <th className="text-left font-medium px-5 py-2">Ürün</th>
                  <th className="text-left font-medium px-3 py-2">Kategori</th>
                  <th className="text-right font-medium px-3 py-2">Fiyat</th>
                  <th className="text-left font-medium px-3 py-2">Durum</th>
                  <th className="text-left font-medium px-3 py-2">Sıra</th>
                  <th className="text-right font-medium px-5 py-2">İşlem</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {published.map((row) => {
                  const busy = busyProductId === row.productId;
                  return (
                    <tr key={row.productId} className="hover:bg-gray-50/60">
                      <td className="px-5 py-3">
                        <div className="font-medium text-gray-900">{row.name}</div>
                        <div className="text-xs text-gray-400 mt-0.5">
                          {row.stateDetail}
                        </div>
                      </td>
                      <td className="px-3 py-3 text-gray-600">{row.category}</td>
                      <td className="px-3 py-3 text-right text-gray-900 tabular-nums">
                        {row.price}
                      </td>
                      <td className="px-3 py-3">
                        <StateBadge row={row} />
                      </td>
                      <td className="px-3 py-3">
                        <SortOrderInput
                          row={row}
                          disabled={busy}
                          onCommit={(next) => actions.onSortOrder(row, next)}
                        />
                      </td>
                      <td className="px-5 py-3">
                        <div className="flex items-center gap-2 justify-end flex-wrap">
                          {row.isActive && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                actions.onToggleAvailability(
                                  row,
                                  row.isAvailable === false,
                                )
                              }
                              className="text-xs px-2.5 py-1.5 rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 disabled:opacity-50 font-medium"
                            >
                              {row.isAvailable === false
                                ? "Bugün aç"
                                : "Bugün kapat"}
                            </button>
                          )}
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => actions.onUnpublish(row)}
                            // Destructive for guests: styled as such, and the
                            // handler asks for confirmation naming what is lost.
                            className="text-xs px-2.5 py-1.5 rounded-lg border border-red-200 text-red-700 hover:bg-red-50 disabled:opacity-50 font-medium"
                          >
                            Menüden kaldır
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── The rest of the catalog ───────────────────────────────────────── */}
      <section className="bg-white border border-gray-200 rounded-xl overflow-hidden">
        <header className="px-5 py-4 border-b border-gray-100">
          <h2 className="text-sm font-semibold text-gray-900">
            {SETUP_COPY.catalogHeading}
          </h2>
          <p className="text-xs text-gray-500 mt-0.5">
            İşletme kataloğunda olan ama şubenizde satılmayan ürünler.
          </p>
        </header>

        {catalog.length === 0 ? (
          <p className="px-5 py-6 text-sm text-gray-600 leading-relaxed">
            {SETUP_COPY.emptyCatalog}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 text-gray-500">
                <tr>
                  <th className="text-left font-medium px-5 py-2">Ürün</th>
                  <th className="text-left font-medium px-3 py-2">Kategori</th>
                  <th className="text-right font-medium px-3 py-2">Fiyat</th>
                  <th className="text-left font-medium px-3 py-2">Durum</th>
                  <th className="text-right font-medium px-5 py-2">İşlem</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {catalog.map((row) => {
                  const busy = busyProductId === row.productId;
                  return (
                    <tr key={row.productId} className="hover:bg-gray-50/60">
                      <td className="px-5 py-3 font-medium text-gray-900">
                        {row.name}
                      </td>
                      <td className="px-3 py-3 text-gray-600">{row.category}</td>
                      <td className="px-3 py-3 text-right text-gray-900 tabular-nums">
                        {row.price}
                      </td>
                      <td className="px-3 py-3">
                        {row.isActive ? (
                          <StateBadge row={row} />
                        ) : (
                          <span className="text-[11px] font-medium px-2 py-0.5 rounded-full bg-gray-200 text-gray-700">
                            Pasif ürün
                          </span>
                        )}
                      </td>
                      <td className="px-5 py-3 text-right">
                        <div className="flex items-center gap-2 justify-end flex-wrap">
                          {!row.isActive && (
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => actions.onToggleActive(row, true)}
                              className="text-xs px-2.5 py-1.5 rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 disabled:opacity-50 font-medium"
                            >
                              Ürünü aktif et
                            </button>
                          )}
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => actions.onPublish(row)}
                            className="text-xs px-2.5 py-1.5 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 font-medium"
                          >
                            Menüye ekle
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
