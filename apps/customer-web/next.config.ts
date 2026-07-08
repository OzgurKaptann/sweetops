import type { NextConfig } from "next";
import { nextHeaders } from "./src/lib/security-headers";

const nextConfig: NextConfig = {
  // Defense-in-depth: emit `Referrer-Policy: no-referrer` on every route so no
  // outbound request can ever carry this app's URL (and therefore never any QR
  // token material) to a third party. The token itself is delivered in the URL
  // fragment and scrubbed on load — see src/lib/qr-session.ts.
  async headers() {
    return nextHeaders();
  },
};

export default nextConfig;
