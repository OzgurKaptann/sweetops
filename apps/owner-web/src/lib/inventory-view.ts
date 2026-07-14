/**
 * Presentation layer for the inventory screen.
 *
 * Everything a manager READS about stock is built here, and nothing here talks to
 * the network or to React. Two reasons:
 *
 *   1. It is the choke point that keeps raw wire values off the screen. A row is
 *      only ever rendered from a `StockRow` / `MovementRow`, and those structs
 *      hold Turkish strings — there is no `movement_type` field left on them to
 *      leak `TRANSFER_OUT` into a table cell by accident.
 *   2. It makes the screen's behaviour unit-testable without a DOM.
 *
 * The backend remains the source of truth for stock. Nothing here recomputes it:
 * `available` is DISPLAYED as the API's `available_quantity` (a generated column,
 * on_hand − reserved), never derived locally and never used to authorize an
 * operation. The client-side transfer checks below are courtesy validation — they
 * spare the manager a round-trip, and the server re-decides regardless.
 */
import { movementTypeLabel } from "./labels.ts";

// ── Quantity formatting ──────────────────────────────────────────────────────
//
// Quantities arrive as Decimal strings. They are formatted for reading, never
// re-added: JS floats are not a currency-grade or stock-grade number type.

/** Turkish number formatting (1234.5 → "1.234,5"), trailing zeros trimmed. */
export function formatQuantity(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  const trimmed = Math.round(n * 1000) / 1000;
  return trimmed.toLocaleString("tr-TR", { maximumFractionDigits: 3 });
}

/** "12,5 kg" — quantity plus its unit, for a table cell. */
export function formatQuantityWithUnit(
  value: string | number | null | undefined,
  unit: string | null | undefined,
): string {
  const q = formatQuantity(value);
  if (q === "—") return q;
  return unit ? `${q} ${unit}` : q;
}

/**
 * A signed stock effect: "+12,5" / "−3" / "0".
 *
 * The minus sign is U+2212, not a hyphen, so a negative movement is unmistakable
 * at a glance in a column of numbers.
 */
export function formatDelta(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "0";
  const magnitude = formatQuantity(Math.abs(n));
  return n > 0 ? `+${magnitude}` : `−${magnitude}`;
}

/** "13.07.2026 14:32" — an operations log is read by local wall-clock time. */
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

// ── Stock status ─────────────────────────────────────────────────────────────

/**
 * The five states a manager needs to tell apart, in the vocabulary fixed by
 * docs/TURKISH_USER_FACING_LOCALIZATION.md.
 *
 * `out` and `insufficient` are NOT the same thing and must not be merged:
 *   out          — nothing on the shelf at all.
 *   insufficient — there IS stock, but every unit of it is already promised to an
 *                  accepted order. Available is zero while on-hand is not. A
 *                  manager who reads "stokta yok" here will go and buy stock they
 *                  already have; one who reads "stok yetersiz" goes and looks at
 *                  the order book.
 */
export type StockStatus = "out" | "insufficient" | "low" | "ok";

export const STOCK_STATUS_LABEL: Record<StockStatus, string> = {
  out: "Stokta yok",
  insufficient: "Stok yetersiz",
  low: "Düşük stok",
  ok: "Stok yeterli",
};

/** Shown next to any row whose available stock is held down by reservations. */
export const RESERVED_STOCK_NOTE =
  "Ayrılan stok bekleyen siparişler için tutuluyor";

/** Shown as a column-level risk flag when a branch is at or below reorder level. */
export const STOCKOUT_RISK_LABEL = "Stok tükenme riski";

export interface StockLike {
  on_hand_quantity: string;
  reserved_quantity: string;
  available_quantity: string;
  reorder_level: string | null;
}

/**
 * Classify one ingredient's stock in one branch.
 *
 * Reads the API's figures; does not recompute them. The ordering of the branches
 * is the whole point: an empty shelf is reported before a reserved-out shelf, and
 * both before a merely low one.
 */
export function stockStatus(stock: StockLike): StockStatus {
  const onHand = Number(stock.on_hand_quantity);
  const available = Number(stock.available_quantity);
  const reorder = stock.reorder_level === null ? null : Number(stock.reorder_level);

  if (!Number.isFinite(onHand) || onHand <= 0) return "out";
  if (!Number.isFinite(available) || available <= 0) return "insufficient";
  if (reorder !== null && Number.isFinite(reorder) && available <= reorder) return "low";
  return "ok";
}

/** True when this row should carry a stockout-risk flag. */
export function hasStockoutRisk(stock: StockLike): boolean {
  const status = stockStatus(stock);
  return status === "out" || status === "insufficient" || status === "low";
}

/** True when reservations are what is holding available stock down. */
export function hasReservedHold(stock: StockLike): boolean {
  const reserved = Number(stock.reserved_quantity);
  return Number.isFinite(reserved) && reserved > 0;
}

// ── Stock rows ───────────────────────────────────────────────────────────────

export interface StockSource extends StockLike {
  ingredient_id: number;
  ingredient_name: string;
  category: string | null;
  unit: string;
}

/**
 * One row of the stock overview, fully rendered.
 *
 * Note what is NOT on this type: no status enum, no raw quantities. A component
 * holding a `StockRow` has nothing untranslated left to print.
 */
export interface StockRow {
  ingredientId: number;
  ingredientName: string;
  onHand: string;
  reserved: string;
  available: string;
  unit: string;
  status: StockStatus;
  statusLabel: string;
  atRisk: boolean;
  riskLabel: string | null;
  reservedNote: string | null;
}

export function toStockRow(item: StockSource): StockRow {
  const status = stockStatus(item);
  const atRisk = hasStockoutRisk(item);
  return {
    ingredientId: item.ingredient_id,
    ingredientName: item.ingredient_name,
    onHand: formatQuantity(item.on_hand_quantity),
    reserved: formatQuantity(item.reserved_quantity),
    available: formatQuantity(item.available_quantity),
    unit: item.unit,
    status,
    statusLabel: STOCK_STATUS_LABEL[status],
    atRisk,
    riskLabel: atRisk ? STOCKOUT_RISK_LABEL : null,
    reservedNote: hasReservedHold(item) ? RESERVED_STOCK_NOTE : null,
  };
}

// ── Movement rows ────────────────────────────────────────────────────────────

export interface MovementSource {
  id: number;
  ingredient_name: string | null;
  movement_type: string;
  quantity: string;
  quantity_delta_on_hand: string;
  quantity_delta_reserved: string;
  unit: string;
  reason: string | null;
  actor_user_id: number | null;
  order_id: number | null;
  created_at: string;
}

export interface MovementRow {
  id: number;
  at: string;
  ingredientName: string;
  /** Already Turkish. `movementTypeLabel` never returns the raw enum. */
  typeLabel: string;
  quantity: string;
  onHandEffect: string;
  reservedEffect: string;
  reason: string;
  actor: string;
}

/** An ingredient the catalog lost (deleted, renamed) still has ledger rows. */
const UNKNOWN_INGREDIENT = "Bilinmeyen malzeme";
/** Movements booked by the system itself — a reservation, a consumption. */
const SYSTEM_ACTOR = "Sistem";

export function toMovementRow(m: MovementSource): MovementRow {
  return {
    id: m.id,
    at: formatDateTime(m.created_at),
    ingredientName: m.ingredient_name ?? UNKNOWN_INGREDIENT,
    typeLabel: movementTypeLabel(m.movement_type),
    quantity: formatQuantityWithUnit(m.quantity, m.unit),
    onHandEffect: formatDelta(m.quantity_delta_on_hand),
    reservedEffect: formatDelta(m.quantity_delta_reserved),
    // An order-driven movement has no typed reason; naming the order IS the reason.
    reason: m.reason ?? (m.order_id !== null ? `${m.order_id} numaralı sipariş` : "—"),
    // The API gives a user id, not a name. An id is an internal identifier, so it
    // is rendered as a person-shaped label rather than printed raw.
    actor: m.actor_user_id !== null ? `Personel #${m.actor_user_id}` : SYSTEM_ACTOR,
  };
}

// ── Empty / permission / loading copy ────────────────────────────────────────

export const INVENTORY_COPY = {
  stockEmpty: "Bu şube için henüz stok tanımlanmamış.",
  // Deliberately NOT "record a purchase receipt to create it". A stock command
  // acts on a stock row that already exists — the service 404s
  // `stock_not_configured` when it is missing — so a branch with no rows at all
  // cannot be bootstrapped from this screen, and telling a manager otherwise sends
  // them into a form that will refuse them.
  stockEmptyHint:
    "Şubenizin stok tanımları oluşturulduktan sonra malzemeler burada görünecek.",
  movementsEmpty: "Bu şube için henüz stok hareketi bulunmuyor.",
  transfersEmpty: "Bu şube için henüz şube transferi bulunmuyor.",
  destinationsEmpty: "Transfer yapılabilecek başka bir şube bulunmuyor.",
  loading: "Yükleniyor…",
  forbidden: "Bu işlem için yetkiniz yok.",
  readOnly: "Stok bilgilerini görüntüleyebilirsiniz, ancak stok işlemi yapma yetkiniz yok.",
} as const;

// ── Operation result banners ─────────────────────────────────────────────────

export type OperationKind =
  | "purchase_receipt"
  | "waste"
  | "manual_adjustment"
  | "transfer"
  | "stock_count";

export type BannerTone = "success" | "info" | "error" | "warning";

export interface OperationBanner {
  tone: BannerTone;
  message: string;
}

/**
 * The stock operations offered on the inventory screen, in the order they appear.
 *
 * It lives here rather than in the page because these are Turkish COPY, and this
 * module is the one place copy is written and tested. "Sayım gir" sits directly
 * beside the other operations on purpose: a manager who has just counted the
 * freezer is standing at the same screen they use for a purchase receipt, and
 * hiding the count behind a separate page is exactly how it ends up being typed in
 * as a manual adjustment instead.
 */
export interface InventoryAction {
  kind: OperationKind;
  label: string;
  primary?: boolean;
}

export const INVENTORY_ACTIONS: readonly InventoryAction[] = [
  { kind: "purchase_receipt", label: "Mal kabul", primary: true },
  { kind: "stock_count", label: "Sayım gir" },
  { kind: "waste", label: "Fire kaydı" },
  { kind: "manual_adjustment", label: "Manuel düzeltme" },
  { kind: "transfer", label: "Şube transferi" },
] as const;

/** The dialog title for each operation. */
export const OPERATION_TITLE: Record<OperationKind, string> = {
  purchase_receipt: "Mal kabul",
  waste: "Fire kaydı",
  manual_adjustment: "Manuel düzeltme",
  transfer: "Şube transferi",
  stock_count: "Fiziksel sayım",
};

const SUCCESS_MESSAGE: Record<OperationKind, string> = {
  purchase_receipt: "Mal kabul başarıyla kaydedildi.",
  waste: "Fire kaydı başarıyla oluşturuldu.",
  manual_adjustment: "Manuel düzeltme başarıyla kaydedildi.",
  transfer: "Transfer tamamlandı.",
  stock_count: "Sayım kaydı uygulandı.",
};

/**
 * A count that found the shelf CORRECT is a success, not a failure and not a no-op.
 *
 * The backend returns `movement_id: null` — nothing physical happened, so nothing
 * went in the ledger. Reporting that as "Sayım kaydı uygulandı" would leave the
 * manager hunting a stock movement that does not exist and concluding the system
 * lost it. So it gets its own sentence, which says the true and useful thing: the
 * count was recorded, and there was no difference.
 */
export const STOCK_COUNT_NO_DELTA_MESSAGE =
  "Sayım kaydedildi. Stok farkı oluşmadı.";

/**
 * A REPLAY is not a second success and must not be reported as one.
 *
 * The backend recognised the idempotency key and returned the original result
 * without moving any more stock. Telling the manager "kaydedildi" a second time
 * would leave them believing two receipts exist. Telling them the truth — this was
 * already recorded, nothing was added — is the entire point of idempotency being
 * visible.
 */
const REPLAY_MESSAGE: Record<OperationKind, string> = {
  purchase_receipt: "Bu mal kabul daha önce kaydedilmiş. Yeni bir kayıt oluşturulmadı.",
  waste: "Bu fire kaydı daha önce oluşturulmuş. Yeni bir kayıt oluşturulmadı.",
  manual_adjustment: "Bu manuel düzeltme daha önce kaydedilmiş. Yeni bir kayıt oluşturulmadı.",
  transfer: "Bu transfer daha önce tamamlanmış. Stok yeniden gönderilmedi.",
  stock_count: "Bu sayım daha önce kaydedilmiş. Stok yeniden düzeltilmedi.",
};

/**
 * `noDelta` reports a count that found the shelf correct. It is deliberately NOT a
 * replay: a replay means "you already sent this", while no-delta means "this was
 * applied, and the shelf was right". Conflating them would tell a manager who
 * counted a correct shelf that they had counted it twice.
 */
export function successBanner(
  kind: OperationKind,
  opts: { replay?: boolean; noDelta?: boolean } = {},
): OperationBanner {
  if (opts.replay) return { tone: "info", message: REPLAY_MESSAGE[kind] };
  if (kind === "stock_count" && opts.noDelta) {
    return { tone: "info", message: STOCK_COUNT_NO_DELTA_MESSAGE };
  }
  return { tone: "success", message: SUCCESS_MESSAGE[kind] };
}

// ── Client-side form validation (courtesy only — the server re-decides) ──────

export const TRANSFER_VALIDATION = {
  destinationRequired: "Hedef şube seçin.",
  sameStore: "Kaynak ve hedef şube aynı olamaz.",
  ingredientRequired: "Malzeme seçin.",
  quantityRequired: "Miktar girin.",
  quantityPositive: "Stok miktarı sıfırdan büyük olmalı.",
  reasonRequired: "Bu stok işlemi için neden belirtmeniz gerekiyor.",
  overAvailable:
    "Kullanılabilir stoktan fazla transfer edilemez. " +
    "Ayrılmış stok bekleyen siparişler için tutuluyor ve transfer edilemez.",
} as const;

export interface TransferFormInput {
  /** The caller's own store, from the session profile. Null when not yet known. */
  sourceStoreId: number | null;
  destinationStoreId: number | null;
  ingredientId: number | null;
  quantity: string;
  reason: string;
  /** Displayed available stock for the chosen ingredient, when known. */
  availableQuantity?: string | null;
}

/**
 * Validate the transfer form. Returns [] when it may be submitted.
 *
 * Two checks are worth naming. Same-store: if the source store is known, a
 * transfer to itself is refused HERE so the manager is not sent to the server to
 * be told something the UI already knew. And over-available: a transfer that would
 * dip into reserved stock is stopped with the reason, not just a refusal.
 *
 * Neither check is a security control. The service enforces both
 * (`same_store_transfer`, `insufficient_available`) and its answer is the one that
 * counts — a client that skipped this function entirely still could not ship
 * reserved stock.
 */
export function validateTransferForm(input: TransferFormInput): string[] {
  const errors: string[] = [];

  if (input.ingredientId === null) errors.push(TRANSFER_VALIDATION.ingredientRequired);

  if (input.destinationStoreId === null) {
    errors.push(TRANSFER_VALIDATION.destinationRequired);
  } else if (
    input.sourceStoreId !== null &&
    input.destinationStoreId === input.sourceStoreId
  ) {
    errors.push(TRANSFER_VALIDATION.sameStore);
  }

  errors.push(...validateQuantity(input.quantity));

  if (!input.reason.trim()) errors.push(TRANSFER_VALIDATION.reasonRequired);

  const qty = Number(input.quantity);
  const available =
    input.availableQuantity === null || input.availableQuantity === undefined
      ? null
      : Number(input.availableQuantity);
  if (
    available !== null &&
    Number.isFinite(available) &&
    Number.isFinite(qty) &&
    qty > available
  ) {
    errors.push(TRANSFER_VALIDATION.overAvailable);
  }

  return errors;
}

function validateQuantity(raw: string): string[] {
  const text = raw.trim();
  if (!text) return [TRANSFER_VALIDATION.quantityRequired];
  const n = Number(text);
  if (!Number.isFinite(n)) return [TRANSFER_VALIDATION.quantityPositive];
  if (n <= 0) return [TRANSFER_VALIDATION.quantityPositive];
  return [];
}

export interface MovementFormInput {
  ingredientId: number | null;
  quantity: string;
  reason: string;
}

/** Purchase receipt: quantity > 0; reason optional (the API allows it to be null). */
export function validatePurchaseReceiptForm(input: MovementFormInput): string[] {
  const errors: string[] = [];
  if (input.ingredientId === null) errors.push(TRANSFER_VALIDATION.ingredientRequired);
  errors.push(...validateQuantity(input.quantity));
  return errors;
}

/** Waste: quantity > 0 and a MANDATORY reason — unexplained waste is shrinkage. */
export function validateWasteForm(input: MovementFormInput): string[] {
  const errors: string[] = [];
  if (input.ingredientId === null) errors.push(TRANSFER_VALIDATION.ingredientRequired);
  errors.push(...validateQuantity(input.quantity));
  if (!input.reason.trim()) errors.push(TRANSFER_VALIDATION.reasonRequired);
  return errors;
}

export const ADJUSTMENT_VALIDATION = {
  deltaRequired: "Düzeltme miktarı girin.",
  deltaNonZero: "Düzeltme miktarı sıfır olamaz.",
} as const;

/** What a manual adjustment is FOR — and, just as importantly, what it is not. */
export const MANUAL_ADJUSTMENT_HINT =
  "Manuel düzeltme, fiziksel sayım farkını düzeltmek içindir. " +
  "Şubeler arası stok hareketleri için manuel düzeltme yerine transfer kullanın.";

export interface AdjustmentFormInput {
  ingredientId: number | null;
  /** Signed: negative writes stock off, positive adds it. */
  delta: string;
  reason: string;
}

/**
 * Manual adjustment: a SIGNED, non-zero delta and a mandatory reason.
 *
 * Zero is rejected rather than accepted as a no-op: a zero correction records an
 * event that changed nothing, which is noise in the one ledger an auditor reads.
 */
export function validateAdjustmentForm(input: AdjustmentFormInput): string[] {
  const errors: string[] = [];
  if (input.ingredientId === null) errors.push(TRANSFER_VALIDATION.ingredientRequired);

  const text = input.delta.trim();
  if (!text) {
    errors.push(ADJUSTMENT_VALIDATION.deltaRequired);
  } else {
    const n = Number(text);
    if (!Number.isFinite(n)) errors.push(ADJUSTMENT_VALIDATION.deltaRequired);
    else if (n === 0) errors.push(ADJUSTMENT_VALIDATION.deltaNonZero);
  }

  if (!input.reason.trim()) errors.push(TRANSFER_VALIDATION.reasonRequired);
  return errors;
}

// ── Physical stock count ─────────────────────────────────────────────────────

/**
 * What a physical count IS — and what it does not touch.
 *
 * The second sentence is the one that matters operationally. A manager who thinks
 * counting the freezer might cancel a waiting customer's waffle will not count the
 * freezer.
 */
export const STOCK_COUNT_HINT =
  "Bu işlem fiziksel stok miktarını sayım sonucuna göre düzeltir. " +
  "Ayrılmış stok değişmez.";

export const STOCK_COUNT_VALIDATION = {
  countedRequired: "Sayım sonucunu girin.",
  countedNonNegative: "Sayım sonucu negatif olamaz.",
  belowReserved: "Sayım sonucu ayrılmış stoktan düşük olamaz.",
} as const;

/** Field labels for the count form — the four figures a manager reconciles against. */
export const STOCK_COUNT_LABELS = {
  systemOnHand: "Sistemdeki fiziksel stok",
  reserved: "Ayrılmış stok",
  available: "Kullanılabilir stok",
  expectedDelta: "Beklenen fark",
} as const;

export interface StockCountFormInput {
  ingredientId: number | null;
  /** What was physically found on the shelf. NOT a delta. */
  counted: string;
  reason: string;
  /** The system's current figures for the chosen ingredient, when known. */
  onHandQuantity?: string | null;
  reservedQuantity?: string | null;
}

/**
 * The difference the count is EXPECTED to apply: counted − system on-hand.
 *
 * Display only. The server recomputes it from the stock row it locks, and its
 * answer is the one that counts — between this render and the request landing, an
 * order may have been placed. Returns null when either figure is unusable, so the
 * UI shows "—" rather than a confidently wrong number.
 */
export function expectedCountDelta(
  counted: string,
  onHandQuantity: string | null | undefined,
): number | null {
  if (onHandQuantity === null || onHandQuantity === undefined) return null;
  const text = counted.trim();
  if (!text) return null;
  const c = Number(text);
  const onHand = Number(onHandQuantity);
  if (!Number.isFinite(c) || !Number.isFinite(onHand)) return null;
  // Quantities are stored to 3 places; round the subtraction to the same grain so
  // float noise (9.25 - 10 = -0.7500000000000004) never reaches the screen.
  return Math.round((c - onHand) * 1000) / 1000;
}

/**
 * Validate the count form. Returns [] when it may be submitted.
 *
 * Two rules are worth naming. Zero IS allowed — an empty shelf is a valid count and
 * the one a manager most needs to be able to report, so this deliberately does NOT
 * reuse the "must be greater than zero" quantity check that the other forms use.
 *
 * And a count below RESERVED is blocked here, with the reason, rather than sending
 * the manager to the server to be told something the UI already knew. That is a
 * courtesy, not a security control: the service enforces it
 * (`stock_count_below_reserved`) and its answer is the one that counts — a client
 * that skipped this function entirely still could not count below reserved.
 */
export function validateStockCountForm(input: StockCountFormInput): string[] {
  const errors: string[] = [];
  if (input.ingredientId === null) errors.push(TRANSFER_VALIDATION.ingredientRequired);

  const text = input.counted.trim();
  const counted = Number(text);
  if (!text) {
    errors.push(STOCK_COUNT_VALIDATION.countedRequired);
  } else if (!Number.isFinite(counted)) {
    errors.push(STOCK_COUNT_VALIDATION.countedRequired);
  } else if (counted < 0) {
    errors.push(STOCK_COUNT_VALIDATION.countedNonNegative);
  } else {
    const reserved =
      input.reservedQuantity === null || input.reservedQuantity === undefined
        ? null
        : Number(input.reservedQuantity);
    if (reserved !== null && Number.isFinite(reserved) && counted < reserved) {
      errors.push(STOCK_COUNT_VALIDATION.belowReserved);
    }
  }

  if (!input.reason.trim()) errors.push(TRANSFER_VALIDATION.reasonRequired);
  return errors;
}

// ── Transfer list rows ───────────────────────────────────────────────────────

export interface TransferSource {
  transfer_id: number;
  ingredient_name: string | null;
  quantity: string;
  unit: string;
  direction: string;
  reason: string;
  note: string | null;
  created_at: string;
}

export interface TransferRow {
  id: number;
  at: string;
  ingredientName: string;
  quantity: string;
  /** "Şubeden çıkış" / "Şubeye giriş" — never OUTBOUND/INBOUND. */
  directionLabel: string;
  outbound: boolean;
  reason: string;
  note: string | null;
}

const DIRECTION_LABEL: Record<string, string> = {
  OUTBOUND: "Şubeden çıkış",
  INBOUND: "Şubeye giriş",
};

export function transferDirectionLabel(direction: string | null | undefined): string {
  if (!direction) return "Bilinmiyor";
  return DIRECTION_LABEL[direction] ?? "Bilinmiyor";
}

export function toTransferRow(t: TransferSource): TransferRow {
  return {
    id: t.transfer_id,
    at: formatDateTime(t.created_at),
    ingredientName: t.ingredient_name ?? UNKNOWN_INGREDIENT,
    quantity: formatQuantityWithUnit(t.quantity, t.unit),
    directionLabel: transferDirectionLabel(t.direction),
    outbound: t.direction === "OUTBOUND",
    reason: t.reason,
    note: t.note,
  };
}
