/**
 * Turkish copy for every way a stock operation can fail.
 *
 * The rule this module exists to enforce: a manager never reads an English error
 * code, a status number, a constraint name or a stack trace. They read a sentence
 * that says what happened and what to do next.
 *
 * The backend already sends a Turkish `message` alongside a stable English `error`
 * code, and that message is usually the best one available — it is written by the
 * service that actually refused. So the resolution order is:
 *
 *   1. A known error code → our copy, because we can be more specific about what
 *      the manager should do IN THIS SCREEN than a generic API message can.
 *   2. An unknown code with a server-supplied Turkish message → show it, but only
 *      after it passes `looksDisplaySafe` (see below).
 *   3. Anything else → one calm generic line.
 *
 * Step 2 is guarded rather than trusted. A 502 from a proxy, an unhandled
 * exception behind a misconfigured gateway, or a validation error from a layer
 * that predates the localization work can all put English or worse into `message`.
 * Displaying that verbatim is exactly the leak this module is supposed to prevent.
 */
import { InventoryApiError, InventoryNetworkUncertainError } from "./inventory-api.ts";

/** Shown when we cannot say anything more specific. Never blames the manager. */
export const INVENTORY_ERROR_UNKNOWN =
  "İşlem tamamlanamadı. Lütfen tekrar deneyin.";

/**
 * The request left the browser and no answer came back. The stock MAY have moved.
 *
 * This is deliberately not phrased as a failure. The endpoints are idempotent, so
 * a repeat with the same key is safe — but a manager who reads "başarısız" will
 * re-enter the form by hand, which mints a NEW key and genuinely doubles the
 * movement. So: check the ledger first.
 */
export const INVENTORY_ERROR_NETWORK_UNCERTAIN =
  "İşlemin sonucu doğrulanamadı. Lütfen stok hareketlerini kontrol edin; " +
  "aynı işlemi tekrar göndermeden önce sonucu doğrulayın.";

/**
 * The same uncertainty, said in the vocabulary of a physical count.
 *
 * Worth its own string rather than reusing the generic one: a manager holding a
 * count sheet is thinking about a SAYIM, not an "işlem", and the instruction that
 * actually protects the ledger — go and look at the movements before you send it
 * again — has to be the thing they read.
 */
export const STOCK_COUNT_ERROR_NETWORK_UNCERTAIN =
  "Sayım sonucunun kaydedilip kaydedilmediği doğrulanamadı. " +
  "Aynı işlemi tekrar göndermeden önce stok hareketlerini kontrol edin.";

/**
 * Backend `error` code → Turkish copy.
 *
 * Codes come from app/services/inventory_service.py and app/core/deps.py and are
 * a stable contract; the Turkish is presentation and may be reworded freely.
 */
export const INVENTORY_ERROR_MESSAGE: Record<string, string> = {
  // ── Stock configuration ────────────────────────────────────────────────────
  // No promise of a workflow that does not exist: a purchase receipt cannot create
  // the missing row either — it loads and locks an EXISTING one. Say what is true
  // and stop.
  stock_not_configured: "Bu malzeme için bu şubede stok tanımı bulunmuyor.",
  ingredient_not_found: "Böyle bir malzeme bulunamadı.",

  // ── Physical-stock guards ──────────────────────────────────────────────────
  // The distinction the manager actually needs: the shelf is not empty, but what
  // is on it is already promised to accepted orders.
  insufficient_on_hand:
    "Fiziksel stok yetersiz. Ayrılmış stok bekleyen siparişler için tutuluyor; " +
    "bu miktar düşülemez.",
  insufficient_available:
    "Kullanılabilir stok yetersiz. Ayrılmış stok bekleyen siparişler için " +
    "tutuluyor ve transfer edilemez.",

  // ── Transfer ───────────────────────────────────────────────────────────────
  same_store_transfer: "Kaynak ve hedef şube aynı olamaz.",
  destination_store_not_found: "Hedef şube bulunamadı.",
  transfer_not_found: "Bu transfer bulunamadı.",

  // ── Physical stock count ───────────────────────────────────────────────────
  // NOT phrased as "the count is wrong". The count may well be right — that is
  // precisely the problem, because it means the shop has promised stock it does not
  // physically have. So the copy sends the manager to the orders, which is the only
  // place that can actually be resolved.
  stock_count_below_reserved:
    "Sayım sonucu ayrılmış stoktan düşük olamaz. Ayrılmış stok bekleyen siparişler " +
    "için tutuluyor; önce ilgili siparişleri kontrol edin.",
  stock_count_not_found: "Bu sayım kaydı bulunamadı.",

  // ── Command validation ─────────────────────────────────────────────────────
  invalid_quantity: "Stok miktarı sıfırdan büyük olmalı.",
  reason_required: "Bu stok işlemi için neden belirtmeniz gerekiyor.",

  // ── Idempotency ────────────────────────────────────────────────────────────
  idempotency_required: "Stok işlemi başlatılamadı. Lütfen sayfayı yenileyip tekrar deneyin.",
  idempotency_mismatch:
    "Bu stok işlemi farklı bilgilerle daha önce denenmiş. " +
    "Lütfen stok hareketlerini kontrol edip yeniden başlatın.",

  // ── Session / authorization ────────────────────────────────────────────────
  forbidden: "Bu işlem için yetkiniz yok.",
  origin_rejected: "Güvenlik doğrulaması başarısız. Lütfen sayfayı yenileyip tekrar deneyin.",
  csrf_invalid: "Güvenlik doğrulaması başarısız. Lütfen sayfayı yenileyip tekrar deneyin.",
  no_store_assigned:
    "Hesabınız bir şubeye bağlı değil. Stok işlemleri için şube ataması gerekiyor.",

  // ── Transport ──────────────────────────────────────────────────────────────
  // A failed READ changed nothing, so it may be stated plainly as a failure.
  network_error: "Stok bilgileri yüklenemedi. Bağlantınızı kontrol edip tekrar deneyin.",
};

/**
 * Is a server-supplied string safe to put in front of a manager?
 *
 * A conservative shape check, not a translation check. It rejects the things that
 * betray an internal: SQLSTATE-ish tokens, table/constraint names, exception class
 * names, JSON/stack fragments, URLs, and ALL_CAPS_ENUM identifiers. Turkish
 * user-facing copy from app/core/messages.py passes; `IntegrityError: duplicate
 * key value violates unique constraint "ix_stock_store_ingredient"` does not.
 */
export function looksDisplaySafe(message: string): boolean {
  const text = message.trim();
  if (!text || text.length > 300) return false;

  // Technical shrapnel: braces/brackets, SQL quoting, paths, code-ish separators.
  if (/[{}[\]<>\\|`]/.test(text)) return false;
  if (/https?:\/\//i.test(text)) return false;
  if (/\b\w+\.(py|ts|tsx|sql)\b/i.test(text)) return false;
  // `Error:` / `Exception:` / `Traceback` and friends.
  if (/\b(error|exception|traceback|sqlstate|constraint|psycopg|sqlalchemy)\b/i.test(text)) {
    return false;
  }
  // A raw enum / identifier: TRANSFER_OUT, stock_not_configured, ix_stock_store.
  if (/\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/.test(text)) return false;
  if (/\b[a-z][a-z0-9]*_[a-z0-9_]+\b/.test(text)) return false;

  return true;
}

/**
 * The one function a component calls. Give it whatever was thrown; get back a
 * sentence a manager can act on.
 *
 * `kind` is optional and only changes the NETWORK-UNCERTAIN copy, where the
 * operation's own vocabulary matters: a manager holding a count sheet needs to read
 * about a sayım. Everything else resolves identically for every operation, because
 * the backend's error codes already are the specific thing.
 */
export function inventoryErrorMessage(err: unknown, kind?: string): string {
  if (err instanceof InventoryNetworkUncertainError) {
    return kind === "stock_count"
      ? STOCK_COUNT_ERROR_NETWORK_UNCERTAIN
      : INVENTORY_ERROR_NETWORK_UNCERTAIN;
  }

  if (err instanceof InventoryApiError) {
    const known = INVENTORY_ERROR_MESSAGE[err.code];
    if (known) return known;
    if (err.message && looksDisplaySafe(err.message)) return err.message;
    return INVENTORY_ERROR_UNKNOWN;
  }

  // A TypeError from our own code, a parse failure, anything at all. Whatever it
  // says, it was written for a developer — so it is not shown.
  return INVENTORY_ERROR_UNKNOWN;
}

/**
 * True when the failure left the outcome genuinely unknown, so the UI should warn
 * rather than invite a retry.
 */
export function isOutcomeUncertain(err: unknown): boolean {
  return err instanceof InventoryNetworkUncertainError;
}
