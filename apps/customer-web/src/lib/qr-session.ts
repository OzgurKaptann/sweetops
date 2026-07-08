/**
 * Client-side QR token session.
 *
 * SECURITY MODEL — why the token lives in the URL fragment, not a query string:
 *
 * The QR token is a long-lived bearer token printed on a physical sticker. A
 * query-string token (`?qr=…`) is transmitted to the web server on the very
 * first request and therefore leaks into places application code cannot redact:
 * browser history, reverse-proxy / CDN / hosting-platform access logs,
 * observability pipelines, `Referer` headers, copied URLs and screenshots.
 *
 * The URL *fragment* (`#qr=…`) is NOT sent to the server on the initial page
 * request. So the physical QR encodes `https://host/#qr=<token>`, the client
 * reads it here, immediately persists it to `sessionStorage`, and scrubs it
 * from the address bar with `history.replaceState`. From then on the session
 * token drives the app — no token ever appears in a request URL.
 *
 * This module is deliberately free of React/DOM-render coupling and takes its
 * environment (hash, storage, scrub) by injection so it can be unit-tested as
 * pure TypeScript. It NEVER logs the token (no console / analytics / errors).
 */

// Reuse the storage contract shape from the idempotency module's design: a
// minimal, guarded key/value store that always degrades safely.
export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** sessionStorage key holding the active session's QR token. */
export const QR_TOKEN_STORAGE_KEY = "sweetops.qrToken";

const PROBE_KEY = "__sweetops_qr_probe__";

/**
 * Parse a `#qr=<token>` fragment. Accepts the raw `window.location.hash`
 * (leading `#` optional). Returns the token or null if absent/empty/malformed.
 * Only the exact `qr` key is honored — any other fragment shape yields null.
 */
export function parseQrTokenFromHash(
  hash: string | null | undefined,
): string | null {
  if (!hash) return null;
  const body = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!body) return null;
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(body);
  } catch {
    return null;
  }
  const token = params.get("qr");
  if (token == null) return null;
  const trimmed = token.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * Resolve `sessionStorage` if — and only if — it is present and writable.
 * Returns null during SSR, private-mode lockouts, or when disabled, so callers
 * transparently fall back to fragment-only (single-render) behaviour.
 */
export function defaultQrSessionStorage(): KeyValueStorage | null {
  try {
    if (typeof window === "undefined" || !window.sessionStorage) return null;
    const s = window.sessionStorage;
    s.setItem(PROBE_KEY, "1");
    s.removeItem(PROBE_KEY);
    return s;
  } catch {
    return null;
  }
}

/** Remove the fragment from the visible address bar without a navigation. */
function defaultScrubHash(): void {
  if (
    typeof window === "undefined" ||
    typeof window.history?.replaceState !== "function"
  ) {
    return;
  }
  const { pathname, search } = window.location;
  // Replace the entry so no fragment (and therefore no token) remains visible
  // in the address bar or in this history entry.
  window.history.replaceState(null, "", `${pathname}${search}`);
}

export interface AcquireQrTokenOptions {
  /** Raw hash string. Defaults to `window.location.hash`. */
  hash?: string;
  /** Storage. Pass `null` to disable persistence. Defaults to sessionStorage. */
  storage?: KeyValueStorage | null;
  /** Address-bar scrub. Defaults to `history.replaceState`. */
  scrub?: () => void;
}

/**
 * Acquire the QR token for this browser session.
 *
 *   1. If the URL fragment carries `#qr=<token>`, capture it, persist it to
 *      session storage, scrub it from the address bar, and return it.
 *   2. Otherwise fall back to a token captured earlier this session (so a
 *      same-tab refresh keeps working without the token in the URL).
 *   3. If neither exists (e.g. a new tab opened without scanning), return null.
 *
 * The returned token is never logged. Callers must not place it in a URL.
 */
export function acquireQrToken(opts: AcquireQrTokenOptions = {}): string | null {
  const hash =
    opts.hash ?? (typeof window !== "undefined" ? window.location.hash : "");
  const storage =
    opts.storage !== undefined ? opts.storage : defaultQrSessionStorage();

  const fromHash = parseQrTokenFromHash(hash);
  if (fromHash) {
    if (storage) {
      try {
        storage.setItem(QR_TOKEN_STORAGE_KEY, fromHash);
      } catch {
        // Non-fatal: the token still drives this render from memory.
      }
    }
    try {
      (opts.scrub ?? defaultScrubHash)();
    } catch {
      // Scrubbing is best-effort; the app must still work if it fails.
    }
    return fromHash;
  }

  if (storage) {
    try {
      const stored = storage.getItem(QR_TOKEN_STORAGE_KEY);
      if (stored && stored.length > 0) return stored;
    } catch {
      // Fall through to null.
    }
  }
  return null;
}

/**
 * Forget the session token. Called after a *definitive* invalid/revoked
 * resolution so a same-tab refresh does not keep retrying a dead token. NOT
 * called on network failures (the token may still be valid — see the client).
 */
export function clearQrToken(
  storage: KeyValueStorage | null = defaultQrSessionStorage(),
): void {
  if (!storage) return;
  try {
    storage.removeItem(QR_TOKEN_STORAGE_KEY);
  } catch {
    // Nothing else to do.
  }
}
