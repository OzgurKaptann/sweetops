/**
 * Customer-web security response headers.
 *
 * `Referrer-Policy: no-referrer` is defense-in-depth for the QR bearer token.
 * The token is delivered in the URL *fragment* (`#qr=…`) and scrubbed from the
 * address bar on load, so it should never reach a `Referer` header — but a
 * strict no-referrer policy guarantees that even a stray outbound request (an
 * image, a link, an analytics beacon) can never carry any part of this app's
 * URL to a third party. It does NOT replace fragment delivery; it backs it up.
 */
export const REFERRER_POLICY = "no-referrer";

/** Header list applied to every route. */
export function securityHeaders(): Array<{ key: string; value: string }> {
  return [{ key: "Referrer-Policy", value: REFERRER_POLICY }];
}

/** Shape consumed by Next's `headers()` config hook. */
export async function nextHeaders() {
  return [
    {
      source: "/:path*",
      headers: securityHeaders(),
    },
  ];
}
