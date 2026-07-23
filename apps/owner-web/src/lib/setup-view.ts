/**
 * Presentation layer for the store-setup screen.
 *
 * Everything a manager READS about their menu and tables is built here, and
 * nothing here talks to the network or to React. Two reasons, the same two as
 * ./inventory-view.ts:
 *
 *   1. It is the choke point that keeps raw wire values off the screen. A row is
 *      only ever rendered from a `MenuRow` / `TableRow`, and those structs hold
 *      Turkish strings — there is no `on_customer_menu` boolean left on them to
 *      leak "true" into a table cell, and no error code to leak `not_published`.
 *   2. It makes the screen's behaviour unit-testable without a DOM. The owner-web
 *      suite is `node --test` over pure TypeScript; there is no renderer.
 *
 * The backend stays the source of truth. `on_customer_menu` in particular is
 * DISPLAYED as the API computed it and is never re-derived from the three
 * component booleans — the day a fourth condition joins the customer menu's
 * predicate, a client-side AND would quietly disagree with the guest's phone.
 */
import type {
  MenuProductItem,
  SetupCheck,
  SetupStatus,
  TableItem,
} from "./setup-api.ts";

// ── Copy ─────────────────────────────────────────────────────────────────────

export const SETUP_COPY = {
  heading: "Şube kurulumu ve menü",
  subheading:
    "Şubenizin misafirlere görünen menüsünü ve masa QR kodlarını buradan yönetin.",

  readinessHeading: "Kurulum durumu",
  readyTitle: "Şubeniz sipariş almaya hazır",
  notReadyTitle: "Şubeniz henüz sipariş almaya hazır değil",
  // The sentence this whole screen exists to make unnecessary to ask support.
  readyDetail:
    "Masadaki QR kodu okutan misafir menünüzü görüyor ve sipariş verebiliyor.",
  notReadyDetail:
    "Aşağıdaki adımlar tamamlanana kadar misafirler menüyü boş görür.",

  menuHeading: "Şube menüsü",
  menuSubheading:
    "Ürünler tüm işletme için ortak tanımlanır; menüde görünmesi için şubenize ayrıca eklenmelidir.",
  catalogHeading: "Menüde olmayan ürünler",

  tablesHeading: "Masalar ve QR kodları",
  tablesSubheading:
    "Her masanın kendi QR kodu vardır. QR bağlantısı yalnızca oluşturulduğu anda gösterilir.",

  loadError:
    "Kurulum bilgileri yüklenemedi. Bağlantınızı kontrol edip tekrar deneyin.",

  // Empty states. Each says what to do next, not merely that something is empty.
  emptyMenu:
    "Şube menünüzde henüz ürün yok. Misafirler menüyü boş görüyor. " +
    "Aşağıdan ürün ekleyin veya kataloğdaki bir ürünü menüye alın.",
  emptyCatalog:
    "Henüz hiç ürün tanımlanmamış. Önce bir ürün oluşturun, sonra şube menünüze ekleyin.",
  emptyTables:
    "Şubenizde henüz masa yok. Masa ekleyin; QR kodu otomatik olarak oluşturulur.",

  // Dangerous actions — each names the consequence, in the shop's own terms.
  confirmUnpublish:
    "Bu ürün şube menüsünden kaldırılacak ve misafirler artık göremeyecek. " +
    "Ürün silinmez, diğer şubeler etkilenmez.",
  confirmDeactivate:
    "Bu ürün TÜM şubelerde pasife alınacak ve hiçbir şubenin menüsünde görünmeyecek. " +
    "Geçmiş siparişler etkilenmez.",
  confirmRotateQr:
    "Bu masanın mevcut QR kodu geçersiz olacak. Masadaki basılı kodu okutan " +
    "misafirler sipariş veremez; yeni kodu bastırıp masaya koymanız gerekir.",

  qrShownOnceWarning:
    "Bu bağlantı yalnızca şimdi gösterilir. Kapatmadan önce kaydedin veya bastırın.",
} as const;

// ── Formatting ───────────────────────────────────────────────────────────────

/** "₺120,00" — Turkish money formatting from the API's Decimal string. */
export function formatPrice(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("tr-TR", {
    style: "currency",
    currency: "TRY",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** "13.07.2026 14:32" — an operations screen is read by local wall-clock time. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("tr-TR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ── Menu rows ────────────────────────────────────────────────────────────────

/**
 * The four states a product can be in, from this branch's point of view.
 *
 * They are NOT a rewording of one boolean. Each has a different fix, and merging
 * any two of them would send a manager to the wrong control:
 *
 *   on_menu      guests can see and order it right now.
 *   sold_out     published here, switched off for today. One toggle away.
 *   retired      pasif chain-wide. Publication cannot override that; the fix is
 *                on the product, not on this branch's menu.
 *   not_on_menu  this branch never published it. It is not hidden — a guest has
 *                no path to it at all.
 */
export type MenuState = "on_menu" | "sold_out" | "retired" | "not_on_menu";

export interface MenuRow {
  productId: number;
  name: string;
  category: string;
  price: string;
  state: MenuState;
  /** Turkish sentence for `state`. The only thing rendered as status text. */
  stateLabel: string;
  /** What this state means for a guest, in one line. */
  stateDetail: string;
  published: boolean;
  isActive: boolean;
  isAvailable: boolean | null;
  sortOrder: number | null;
  sortOrderLabel: string;
}

const STATE_LABEL: Record<MenuState, string> = {
  on_menu: "Menüde",
  sold_out: "Bugün kapalı",
  retired: "Pasif ürün",
  not_on_menu: "Menüde değil",
};

const STATE_DETAIL: Record<MenuState, string> = {
  on_menu: "Misafirler bu ürünü görüyor ve sipariş verebiliyor.",
  sold_out: "Şube menüsünde yayında ama bugün için kapalı; misafirler göremiyor.",
  retired:
    "Ürün tüm işletmede pasif. Şube menüsünde yayında olsa bile misafirler göremiyor.",
  not_on_menu: "Bu ürün şube menünüzde yayında değil; misafirler göremiyor.",
};

/**
 * Which state a product is in for this branch.
 *
 * Order matters. "Not published here" is checked first because it is the most
 * actionable and the most common right after the fail-closed menu shipped — an
 * item nobody published is not "retired", whatever its catalog flag says, and
 * telling a manager to reactivate a product they never put on their menu would
 * send them to the wrong screen entirely.
 */
export function menuStateFor(item: MenuProductItem): MenuState {
  if (!item.published) return "not_on_menu";
  if (!item.is_active) return "retired";
  if (item.is_available === false) return "sold_out";
  return "on_menu";
}

export function toMenuRow(item: MenuProductItem): MenuRow {
  const state = menuStateFor(item);
  return {
    productId: item.product_id,
    name: item.name?.trim() || "İsimsiz ürün",
    category: item.category?.trim() || "—",
    price: formatPrice(item.base_price),
    state,
    stateLabel: STATE_LABEL[state],
    stateDetail: STATE_DETAIL[state],
    published: item.published,
    isActive: item.is_active,
    isAvailable: item.is_available,
    sortOrder: item.sort_order,
    sortOrderLabel: item.sort_order === null ? "—" : String(item.sort_order),
  };
}

/** The rows this branch has published — its actual menu, in menu order. */
export function publishedRows(items: MenuProductItem[]): MenuRow[] {
  return items.filter((i) => i.published).map(toMenuRow);
}

/** The rest of the catalog: what this branch COULD add. */
export function catalogRows(items: MenuProductItem[]): MenuRow[] {
  return items.filter((i) => !i.published).map(toMenuRow);
}

// ── Readiness checklist ──────────────────────────────────────────────────────

export interface ChecklistRow {
  key: string;
  done: boolean;
  count: number;
  label: string;
  detail: string;
  /** "Tamam" / "Eksik" — never a raw boolean, never a tick with no words. */
  statusLabel: string;
}

/**
 * The checklist, as rows a screen can render directly.
 *
 * The `label` and `detail` come from the SERVER, already in Turkish. This client
 * deliberately does not hold a second copy of those sentences: the backend knows
 * whether two of three tables have a QR code, and a client-side template would
 * have to re-derive that from counts and would drift the first time the rule
 * changes.
 *
 * An unknown `key` is not dropped. A checklist that silently omits the check it
 * does not recognise is a checklist that hides the one step nobody has thought
 * about — the same reason the threshold screen lists NOT_CONFIGURED ingredients.
 */
export function checklistRows(checks: SetupCheck[] | undefined): ChecklistRow[] {
  if (!checks || checks.length === 0) return [];
  return checks.map((c) => ({
    key: c.key,
    done: !!c.done,
    count: Number.isFinite(c.count) ? c.count : 0,
    label: c.label || "Kurulum adımı",
    detail: c.detail || "",
    statusLabel: c.done ? "Tamam" : "Eksik",
  }));
}

export interface ReadinessSummary {
  ready: boolean;
  title: string;
  detail: string;
  doneCount: number;
  totalCount: number;
  /** "2/4 adım tamam" */
  progressLabel: string;
  rows: ChecklistRow[];
}

export function readinessSummary(
  status: SetupStatus | null,
): ReadinessSummary {
  const rows = checklistRows(status?.checks);
  const doneCount = rows.filter((r) => r.done).length;
  // Trust the SERVER's verdict rather than recomputing it from the rows: it owns
  // the rule about which checks are load-bearing, and a screen that disagreed
  // with the guest's phone would be worse than one that shows nothing.
  const ready = !!status?.ready_for_customer_orders;
  return {
    ready,
    title: ready ? SETUP_COPY.readyTitle : SETUP_COPY.notReadyTitle,
    detail: ready ? SETUP_COPY.readyDetail : SETUP_COPY.notReadyDetail,
    doneCount,
    totalCount: rows.length,
    progressLabel: `${doneCount}/${rows.length} adım tamam`,
    rows,
  };
}

/**
 * One line explaining WHY the customer menu is empty, or null when it is not.
 *
 * This is the specific confusion the branch exists to end: a guest's phone shows
 * the same calm empty state whether nothing was published or the server is down.
 * The distinction between "nothing published" and "everything switched off" is
 * kept because the fixes are nothing alike.
 */
export function emptyMenuExplanation(status: SetupStatus | null): string | null {
  if (!status) return null;
  if (status.menu_products > 0) return null;
  if (status.published_products === 0) {
    return (
      `Şube menünüzde yayında ürün yok, bu yüzden misafirler menüyü boş görüyor. ` +
      `İşletmede ${status.catalog_active_products} aktif ürün tanımlı; ` +
      `bunlardan şubenizde satmak istediklerinizi menüye ekleyin.`
    );
  }
  return (
    `Şube menünüzde ${status.published_products} ürün yayında ancak hiçbiri şu anda ` +
    `misafire görünmüyor: ürünler ya bugün için kapalı ya da işletme genelinde pasif.`
  );
}

// ── Table rows ───────────────────────────────────────────────────────────────

export interface TableRow {
  tableId: number;
  displayName: string;
  tableNumber: string;
  hasQr: boolean;
  qrStatusLabel: string;
  qrDetail: string;
  /**
   * The non-secret prefix, shown so a manager can match this record to the
   * sticker on the table. Never a token and never scannable.
   */
  tokenPrefix: string;
  createdAt: string;
  lastUsedAt: string;
}

export function toTableRow(item: TableItem): TableRow {
  const hasQr = !!item.has_active_qr;
  return {
    tableId: item.table_id,
    displayName: item.display_name || `Masa #${item.table_id}`,
    tableNumber: item.table_number?.trim() || "",
    hasQr,
    qrStatusLabel: hasQr ? "QR kodu hazır" : "QR kodu yok",
    qrDetail: hasQr
      ? "Bu masadaki basılı kod çalışıyor."
      : "Bu masa okutulamıyor. QR kodu oluşturun.",
    // Prefix is display-only; it is padded with an em dash rather than an empty
    // cell so a manager can tell "no sticker" from "we forgot to render it".
    tokenPrefix: item.token_prefix || "—",
    createdAt: formatDateTime(item.qr_created_at),
    lastUsedAt: formatDateTime(item.qr_last_used_at),
  };
}

export function tableRows(items: TableItem[] | undefined): TableRow[] {
  if (!items) return [];
  return items.map(toTableRow);
}

// ── Product form ─────────────────────────────────────────────────────────────

export interface ProductFormValues {
  name: string;
  category: string;
  price: string;
  isActive: boolean;
  publishToCurrentStore: boolean;
}

export const EMPTY_PRODUCT_FORM: ProductFormValues = {
  name: "",
  category: "",
  price: "",
  isActive: true,
  // Defaults to false, matching the API. Putting an item in front of guests is a
  // decision the manager takes by ticking the box, never one the form takes for
  // them.
  publishToCurrentStore: false,
};

export interface ProductFormResult {
  ok: boolean;
  /** Turkish, ready to render under the offending field. */
  error: string | null;
  body: {
    name: string;
    category: string | null;
    base_price: string;
    is_active: boolean;
    publish_to_current_store: boolean;
  } | null;
}

/**
 * Validate and normalise the create-product form into the exact request body.
 *
 * Courtesy validation: the server re-checks every rule and answers with its own
 * Turkish sentence. This exists so a manager is not made to wait for a round-trip
 * to be told the price field is empty.
 *
 * The price is normalised from Turkish input ("119,90") to the decimal string the
 * API expects ("119.90") and is never converted to a float on the way — a menu
 * price that arrives as 119.89999 is a price a shop will be asked about.
 */
export function buildProductCreateBody(
  values: ProductFormValues,
): ProductFormResult {
  const name = values.name.trim();
  if (!name) {
    return { ok: false, error: "Ürün adı girmeniz gerekiyor.", body: null };
  }
  if (name.length > 200) {
    return { ok: false, error: "Ürün adı çok uzun.", body: null };
  }

  const normalised = values.price.trim().replace(/\s/g, "").replace(",", ".");
  if (!normalised) {
    return { ok: false, error: "Ürün fiyatı girmeniz gerekiyor.", body: null };
  }
  if (!/^\d+(\.\d{1,2})?$/.test(normalised)) {
    return {
      ok: false,
      error: "Fiyatı 120 veya 119,90 biçiminde girin.",
      body: null,
    };
  }
  if (Number(normalised) <= 0) {
    return { ok: false, error: "Ürün fiyatı sıfırdan büyük olmalı.", body: null };
  }

  return {
    ok: true,
    error: null,
    body: {
      name,
      category: values.category.trim() || null,
      base_price: normalised,
      is_active: values.isActive,
      publish_to_current_store: values.publishToCurrentStore,
    },
  };
}

/**
 * Turkish confirmation for the state change a toggle is ABOUT to make.
 *
 * Only the destructive direction gets a confirmation. Putting something on the
 * menu, or bringing it back after a sold-out day, needs no ceremony; taking it
 * away from guests does.
 */
export function confirmationFor(
  action: "unpublish" | "deactivate" | "rotate_qr",
): string {
  switch (action) {
    case "unpublish":
      return SETUP_COPY.confirmUnpublish;
    case "deactivate":
      return SETUP_COPY.confirmDeactivate;
    case "rotate_qr":
      return SETUP_COPY.confirmRotateQr;
  }
}
