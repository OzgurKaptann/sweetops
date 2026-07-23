/**
 * Turkish display labels for the enum values the API sends.
 *
 * The order status on the wire stays English (NEW, IN_PREP, …) — every state
 * comparison in this app is still made against those values. This module only
 * decides how they are rendered, so a cook never reads "IN_PREP" off the board.
 */

/** Kitchen preparation state of an order. */
export const ORDER_STATUS_LABEL: Record<string, string> = {
  NEW: "Bekliyor",
  IN_PREP: "Hazırlanıyor",
  READY: "Hazır",
  DELIVERED: "Teslim edildi",
  CANCELLED: "İptal edildi",
};

/**
 * Live-connection state of the kitchen display.
 *
 * Only `live` may say "Canlı". Every other state names the actual degradation,
 * because a board that quietly claims to be live while missing tickets is worse
 * than one that admits it is unsure.
 */
export const CONNECTION_STATE_LABEL: Record<string, string> = {
  connecting: "Bağlanıyor…",
  live: "Canlı",
  reconnecting: "Yeniden bağlanılıyor…",
  polling: "Yedek mod",
  stale: "Veriler eski olabilir",
  offline: "Bağlantı yok",
};

/** One line of plain Turkish telling the cook what to do about that state. */
export const CONNECTION_STATE_NOTE: Record<string, string> = {
  connecting: "Mutfak akışına bağlanılıyor…",
  live: "Siparişler anlık olarak güncelleniyor.",
  reconnecting:
    "Canlı bağlantı koptu, yeniden kuruluyor. Liste düzenli olarak yenileniyor.",
  polling:
    "Canlı bağlantı yok. Liste her 12 saniyede bir otomatik yenileniyor.",
  stale: "Liste güncel olmayabilir. Yenile'ye basın.",
  offline:
    "Sunucuya ulaşılamıyor. Listede eksik sipariş olabilir; Yenile'ye basın.",
};

/** Unknown values render as `fallback`, never as the raw enum. */
export function labelFor(
  map: Record<string, string>,
  value: string | null | undefined,
  fallback = "Bilinmiyor",
): string {
  if (!value) return fallback;
  return map[value] ?? fallback;
}

export const orderStatusLabel = (v: string | null | undefined) =>
  labelFor(ORDER_STATUS_LABEL, v, "Bilinmiyor");

/** An unrecognised connection state degrades to the pessimistic label. */
export const connectionStateLabel = (v: string | null | undefined) =>
  labelFor(CONNECTION_STATE_LABEL, v, "Bağlantı yok");

export const connectionStateNote = (v: string | null | undefined) =>
  labelFor(
    CONNECTION_STATE_NOTE,
    v,
    "Bağlantı durumu bilinmiyor. Yenile'ye basın.",
  );

/**
 * "When did this board last actually receive data?" — the one number that tells
 * a cook whether to trust what is in front of them.
 */
export function lastSyncedLabel(
  lastSyncedAt: number | null,
  now: number,
): string {
  if (lastSyncedAt === null) return "Henüz güncellenmedi";
  const seconds = Math.max(0, Math.round((now - lastSyncedAt) / 1000));
  if (seconds < 10) return "Az önce güncellendi";
  if (seconds < 60) return `${seconds} sn önce güncellendi`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} dk önce güncellendi`;
  const hours = Math.floor(minutes / 60);
  return `${hours} sa önce güncellendi`;
}
