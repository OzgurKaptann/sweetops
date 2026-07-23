/**
 * Turkish copy for every way a store-setup operation can fail.
 *
 * Same rule as ./inventory-errors.ts, and the same reason: a manager never reads
 * an English error code, a status number, a constraint name or a stack trace.
 * They read a sentence that says what happened and what to do next.
 *
 * Resolution order:
 *
 *   1. A known error code → our copy, because we can be more specific about what
 *      to do IN THIS SCREEN than a generic API message can.
 *   2. An unknown code with a server-supplied Turkish message → show it, but only
 *      after it passes `looksDisplaySafe`, which is shared with the inventory
 *      screen rather than duplicated.
 *   3. Anything else → one calm generic line.
 *
 * Step 2 is guarded rather than trusted. A 502 from a proxy or an unhandled
 * exception behind a misconfigured gateway can put English or worse into
 * `message`, and displaying that verbatim is exactly the leak this module exists
 * to prevent.
 */
import { looksDisplaySafe } from "./inventory-errors.ts";
import { SetupApiError, SetupNetworkUncertainError } from "./setup-api.ts";

/** Shown when nothing more specific can be said. Never blames the manager. */
export const SETUP_ERROR_UNKNOWN = "İşlem tamamlanamadı. Lütfen tekrar deneyin.";

/**
 * The request left the browser and no answer came back.
 *
 * Deliberately not phrased as a failure. Publishing is idempotent so a repeat is
 * harmless, but a CREATE may well have succeeded — and a manager who reads
 * "başarısız" types the product in again and ends up with two. So: check the list
 * first.
 */
export const SETUP_ERROR_NETWORK_UNCERTAIN =
  "İşlemin sonucu doğrulanamadı. Aynı işlemi tekrar göndermeden önce " +
  "ürün ve masa listesini kontrol edin.";

/**
 * Backend `error` code → Turkish copy.
 *
 * Codes come from app/services/store_setup_service.py, app/routers/owner_setup.py
 * and app/core/deps.py, and are a stable contract; the Turkish is presentation and
 * may be reworded freely.
 */
export const SETUP_ERROR_MESSAGE: Record<string, string> = {
  // ── Products ───────────────────────────────────────────────────────────────
  product_not_found: "Böyle bir ürün bulunamadı. Listeyi yenileyip tekrar deneyin.",
  // Not phrased as the manager's mistake: the usual cause is a double-submitted
  // form, and the safe next step is to look for the product that already exists.
  product_name_taken:
    "Bu isimde bir ürün zaten var. Ürün listesinden mevcut ürünü menünüze ekleyebilirsiniz.",
  product_name_required: "Ürün adı girmeniz gerekiyor.",
  invalid_price: "Ürün fiyatı sıfırdan büyük olmalı.",

  // ── Publication ────────────────────────────────────────────────────────────
  // Names the missing step rather than the failed one: nothing is wrong with the
  // product, it simply is not on this branch's menu yet.
  not_published:
    "Bu ürün şube menünüzde yayında değil. Önce menüye ekleyin, sonra durumunu değiştirin.",
  invalid_sort_order: "Menü sırası negatif olamaz.",

  // ── Tables & QR ────────────────────────────────────────────────────────────
  table_not_found: "Bu masa bulunamadı. Listeyi yenileyip tekrar deneyin.",
  table_number_required: "Masa adı veya numarası girmeniz gerekiyor.",
  table_number_taken: "Bu masa adı şubenizde zaten kullanılıyor.",
  // Points at rotation AND says what rotation costs, so nobody reaches for it as
  // a way to "see the link again".
  qr_token_already_active:
    "Bu masanın geçerli bir QR kodu zaten var. Yeni bağlantı almak için QR kodunu " +
    "yenilemeniz gerekir; yenilediğinizde masadaki basılı kod geçersiz olur.",

  // ── Session / authorization ────────────────────────────────────────────────
  forbidden: "Bu işlem için yetkiniz yok.",
  origin_rejected:
    "Güvenlik doğrulaması başarısız. Lütfen sayfayı yenileyip tekrar deneyin.",
  csrf_invalid:
    "Güvenlik doğrulaması başarısız. Lütfen sayfayı yenileyip tekrar deneyin.",
  no_store_assigned:
    "Hesabınız bir şubeye bağlı değil. Menü ve masa ayarları için şube ataması gerekiyor.",

  // ── Transport ──────────────────────────────────────────────────────────────
  // A failed READ changed nothing, so it may be stated plainly as a failure.
  network_error:
    "Kurulum bilgileri yüklenemedi. Bağlantınızı kontrol edip tekrar deneyin.",
};

/**
 * The one function a component calls. Give it whatever was thrown; get back a
 * sentence a manager can act on.
 */
export function setupErrorMessage(err: unknown): string {
  if (err instanceof SetupNetworkUncertainError) {
    return SETUP_ERROR_NETWORK_UNCERTAIN;
  }

  if (err instanceof SetupApiError) {
    const known = SETUP_ERROR_MESSAGE[err.code];
    if (known) return known;
    if (err.message && looksDisplaySafe(err.message)) return err.message;
    return SETUP_ERROR_UNKNOWN;
  }

  // A TypeError from our own code, a parse failure, anything at all. Whatever it
  // says, it was written for a developer — so it is not shown.
  return SETUP_ERROR_UNKNOWN;
}

/**
 * True when the failure left the outcome genuinely unknown, so the UI should warn
 * rather than invite a retry.
 */
export function isSetupOutcomeUncertain(err: unknown): boolean {
  return err instanceof SetupNetworkUncertainError;
}
