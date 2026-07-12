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

/** Live-connection state of the kitchen display. */
export const CONNECTION_STATE_LABEL: Record<string, string> = {
  connected: "Canlı",
  connecting: "Bağlanıyor…",
  disconnected: "Bağlantı kesildi",
  error: "Bağlantı hatası",
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
  labelFor(ORDER_STATUS_LABEL, v);

export const connectionStateLabel = (v: string | null | undefined) =>
  labelFor(CONNECTION_STATE_LABEL, v, "Bağlantı kesildi");
