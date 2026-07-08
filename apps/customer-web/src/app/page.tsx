import { Suspense } from "react";
import CustomerMenuPageClient from "@/components/CustomerMenuPageClient";

// Shared loading UI. Mirrors the client component's own "menu loading" state so
// there is no layout shift or flash when Suspense resolves and hydration begins.
// It carries no store/table-specific or product data, so it is safe to prerender.
function CustomerMenuLoadingFallback() {
  return (
    <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3">
      <div className="w-8 h-8 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
      <p className="text-sm text-gray-400">QR kod doğrulanıyor…</p>
    </div>
  );
}

// Server Component entry point. `CustomerMenuPageClient` reads the opaque token
// from the URL *fragment* (`#qr=<token>`) on the client (never a query param),
// scrubs it from the address bar, and persists it to sessionStorage. The
// Suspense boundary lets this static shell prerender while the client-only
// token capture and QR resolution happen after hydration.
export default function Page() {
  return (
    <Suspense fallback={<CustomerMenuLoadingFallback />}>
      <CustomerMenuPageClient />
    </Suspense>
  );
}
