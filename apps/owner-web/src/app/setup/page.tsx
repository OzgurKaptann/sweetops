"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/AuthGate";
import { MenuProductsPanel } from "@/components/setup/MenuProductsPanel";
import { ProductFormModal } from "@/components/setup/ProductFormModal";
import { QrLinkModal } from "@/components/setup/QrLinkModal";
import { ReadinessChecklist } from "@/components/setup/ReadinessChecklist";
import { TablesPanel } from "@/components/setup/TablesPanel";
import {
  createProduct,
  createTable,
  fetchMenuProducts,
  fetchSetupStatus,
  fetchTables,
  issueTableQr,
  publishProduct,
  renameTable,
  rotateTableQr,
  setProductAvailability,
  setProductSortOrder,
  unpublishProduct,
  updateProduct,
  type MenuProductItem,
  type ProductCreateBody,
  type SetupStatus,
  type TableItem,
  type TableQrReceipt,
} from "@/lib/setup-api";
import { setupErrorMessage } from "@/lib/setup-errors";
import {
  SETUP_COPY,
  catalogRows,
  confirmationFor,
  emptyMenuExplanation,
  publishedRows,
  readinessSummary,
  tableRows,
  type MenuRow,
  type TableRow,
} from "@/lib/setup-view";

/**
 * Şube kurulumu ve menü — the screen a shop is actually opened from.
 *
 * This is the owner-facing half of `docs/CUSTOMER_MENU_SCOPING.md`. Migration
 * `a9e4c7b25d13` made the customer menu fail closed and left no supported way to
 * open it; RUNTIME_PRODUCT_GAP_REVIEW F-13 called that "there is no way to onboard
 * a shop". Everything here is store-scoped from the SESSION — there is no branch
 * picker, and the API rejects a smuggled `store_id` rather than ignoring it.
 *
 * Two behaviours are deliberate and worth naming:
 *
 *   * **Every mutation is followed by a reload from the server.** Optimistic local
 *     state would let this screen disagree with the guest's phone, which is the
 *     one thing it exists to prevent. A publish that half-succeeded must show as
 *     it actually is, not as the click hoped.
 *   * **Only the destructive directions confirm.** Putting an item on the menu, or
 *     bringing it back after a sold-out day, needs no ceremony. Taking it away
 *     from guests, retiring it chain-wide, and killing a printed QR sticker each
 *     get a sentence naming what is lost.
 */
export default function SetupPage() {
  const { user } = useAuth();

  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [products, setProducts] = useState<MenuProductItem[]>([]);
  const [tables, setTables] = useState<TableItem[]>([]);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [busyProductId, setBusyProductId] = useState<number | null>(null);
  const [busyTableId, setBusyTableId] = useState<number | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [formSubmitting, setFormSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Held in state only until the manager dismisses it. The link is unrecoverable,
  // so it is never persisted, never logged and never put in the URL.
  const [qrReceipt, setQrReceipt] = useState<TableQrReceipt | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [s, m, t] = await Promise.all([
        fetchSetupStatus(),
        fetchMenuProducts(),
        fetchTables(),
      ]);
      setStatus(s);
      setProducts(m.items);
      setTables(t.items);
      setLoadError(null);
    } catch (err) {
      // A read changed nothing, so it may be stated plainly as a failure — but
      // still through the resolver, so no wire value reaches the screen.
      setLoadError(setupErrorMessage(err) || SETUP_COPY.loadError);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  /**
   * Run one mutation, then re-read everything.
   *
   * The reload is not a convenience. A publication changes the readiness
   * checklist, the menu list and (via availability) what a guest can order, and
   * three panels reconstructed from one optimistic guess is how a screen starts
   * lying about a shop's menu.
   */
  const run = useCallback(
    async (
      fn: () => Promise<unknown>,
      opts?: { productId?: number; tableId?: number; success?: string },
    ) => {
      setActionError(null);
      setNotice(null);
      if (opts?.productId !== undefined) setBusyProductId(opts.productId);
      if (opts?.tableId !== undefined) setBusyTableId(opts.tableId);
      try {
        await fn();
        if (opts?.success) setNotice(opts.success);
        await load();
      } catch (err) {
        setActionError(setupErrorMessage(err));
      } finally {
        setBusyProductId(null);
        setBusyTableId(null);
      }
    },
    [load],
  );

  // ── Derived view models ───────────────────────────────────────────────────

  const summary = useMemo(() => readinessSummary(status), [status]);
  const explanation = useMemo(() => emptyMenuExplanation(status), [status]);
  const published = useMemo(() => publishedRows(products), [products]);
  const catalog = useMemo(() => catalogRows(products), [products]);
  const tableViewRows = useMemo(() => tableRows(tables), [tables]);

  // ── Menu actions ──────────────────────────────────────────────────────────

  const onPublish = (row: MenuRow) =>
    run(() => publishProduct(row.productId), {
      productId: row.productId,
      success: `${row.name} şube menüsüne eklendi.`,
    });

  const onUnpublish = (row: MenuRow) => {
    if (!window.confirm(confirmationFor("unpublish"))) return;
    return run(() => unpublishProduct(row.productId), {
      productId: row.productId,
      success: `${row.name} şube menüsünden kaldırıldı.`,
    });
  };

  const onToggleAvailability = (row: MenuRow, next: boolean) =>
    run(() => setProductAvailability(row.productId, next), {
      productId: row.productId,
      success: next
        ? `${row.name} tekrar menüde görünüyor.`
        : `${row.name} bugün için kapatıldı.`,
    });

  const onSortOrder = (row: MenuRow, next: number) =>
    run(() => setProductSortOrder(row.productId, next), {
      productId: row.productId,
    });

  const onToggleActive = (row: MenuRow, next: boolean) => {
    // Only DEACTIVATION is confirmed: it is chain-wide and removes the item from
    // every branch's menu at once. Reactivating restores something.
    if (!next && !window.confirm(confirmationFor("deactivate"))) return;
    return run(() => updateProduct(row.productId, { is_active: next }), {
      productId: row.productId,
      success: next
        ? `${row.name} tekrar aktif.`
        : `${row.name} tüm şubelerde pasife alındı.`,
    });
  };

  const onCreateProduct = async (body: ProductCreateBody) => {
    setFormSubmitting(true);
    setFormError(null);
    try {
      const receipt = await createProduct(body);
      setFormOpen(false);
      setNotice(
        receipt.published
          ? `${receipt.name} oluşturuldu ve şube menüsüne eklendi.`
          : `${receipt.name} oluşturuldu. Misafirlerin görmesi için menüye ekleyin.`,
      );
      await load();
    } catch (err) {
      // Stays in the dialog: the manager's typing is still on screen and a
      // duplicate-name refusal is fixed by editing the field, not by starting over.
      setFormError(setupErrorMessage(err));
    } finally {
      setFormSubmitting(false);
    }
  };

  // ── Table actions ─────────────────────────────────────────────────────────

  const onCreateTable = (tableNumber: string) =>
    run(
      async () => {
        const result = await createTable({
          table_number: tableNumber,
          issue_qr: true,
        });
        // Shown immediately — this is the only moment the link exists.
        if (result.qr) setQrReceipt(result.qr);
      },
      { success: `${tableNumber} eklendi.` },
    );

  const onRenameTable = (row: TableRow, next: string) =>
    run(() => renameTable(row.tableId, next), {
      tableId: row.tableId,
      success: "Masa adı güncellendi. QR kodu değişmedi.",
    });

  const onIssueQr = (row: TableRow) =>
    run(
      async () => {
        setQrReceipt(await issueTableQr(row.tableId));
      },
      { tableId: row.tableId },
    );

  const onRotateQr = (row: TableRow) => {
    if (!window.confirm(confirmationFor("rotate_qr"))) return;
    return run(
      async () => {
        setQrReceipt(await rotateTableQr(row.tableId));
      },
      { tableId: row.tableId },
    );
  };

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="min-h-screen bg-[#f8f9fa]">
      <header className="bg-white border-b border-gray-200 sticky top-0 z-20">
        <div className="max-w-screen-xl mx-auto px-6">
          <div className="flex items-center justify-between h-14 gap-4">
            <div className="flex items-center gap-3 min-w-0">
              <span className="text-base font-bold text-gray-900 tracking-tight">
                SweetOps
              </span>
              <span className="text-gray-300 text-sm">|</span>
              <span className="text-sm text-gray-500 font-medium">
                {SETUP_COPY.heading}
              </span>
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
                onClick={() => setFormOpen(true)}
                className="text-xs px-3 py-1.5 rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 transition-colors font-medium"
              >
                + Yeni ürün
              </button>
              <button
                onClick={load}
                className="text-xs px-3 py-1.5 rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors font-medium"
              >
                ↻ Yenile
              </button>
              <a
                href="/"
                className="text-xs text-gray-400 hover:text-gray-600 transition-colors"
              >
                ← Panel
              </a>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-screen-xl mx-auto px-6 py-6 space-y-5">
        <div className="flex items-baseline gap-3">
          <div className="w-1 h-5 rounded-full bg-indigo-500 shrink-0" />
          <div>
            <h1 className="text-sm font-semibold text-gray-900 uppercase tracking-wide">
              {SETUP_COPY.heading}
            </h1>
            <p className="text-xs text-gray-400 mt-0.5">
              {SETUP_COPY.subheading}
            </p>
          </div>
        </div>

        {loadError && (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-3">
            {loadError}
          </p>
        )}
        {actionError && (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg px-4 py-3">
            {actionError}
          </p>
        )}
        {notice && (
          <p className="text-sm text-emerald-800 bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3">
            {notice}
          </p>
        )}

        <ReadinessChecklist
          summary={summary}
          explanation={explanation}
          loading={loading && !status}
        />

        <MenuProductsPanel
          published={published}
          catalog={catalog}
          loading={loading && products.length === 0}
          busyProductId={busyProductId}
          actions={{
            onPublish,
            onUnpublish,
            onToggleAvailability,
            onSortOrder,
            onToggleActive,
          }}
        />

        <TablesPanel
          rows={tableViewRows}
          loading={loading && tables.length === 0}
          busyTableId={busyTableId}
          onCreate={onCreateTable}
          onRename={onRenameTable}
          onIssueQr={onIssueQr}
          onRotateQr={onRotateQr}
        />
      </main>

      {formOpen && (
        <ProductFormModal
          storeName={user?.store?.name ?? null}
          submitting={formSubmitting}
          serverError={formError}
          onSubmit={onCreateProduct}
          onClose={() => {
            setFormOpen(false);
            setFormError(null);
          }}
        />
      )}

      {qrReceipt && (
        <QrLinkModal receipt={qrReceipt} onClose={() => setQrReceipt(null)} />
      )}
    </div>
  );
}
