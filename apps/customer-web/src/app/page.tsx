import { Suspense } from "react";
import CustomerMenuPageClient from "@/components/CustomerMenuPageClient";

// Shared loading UI. Mirrors the client component's own "menu loading" state so
// there is no layout shift or flash when Suspense resolves and hydration begins.
// It carries no store/table-specific or product data, so it is safe to prerender.
function CustomerMenuLoadingFallback() {
  return (
    <div className="min-h-screen bg-white flex flex-col items-center justify-center gap-3">
      <div className="w-8 h-8 border-2 border-amber-400 border-t-transparent rounded-full animate-spin" />
      <p className="text-sm text-gray-400">Menü yükleniyor…</p>
    </div>
  );
}

// Server Component entry point. `CustomerMenuPageClient` reads the `store` and
// `table` query params via `useSearchParams()`, which requires a Suspense
// boundary so the shell can be statically prerendered while the client-only
// param reading happens on the client.
export default function Page() {
  return (
    <Suspense fallback={<CustomerMenuLoadingFallback />}>
      <CustomerMenuPageClient />
    </Suspense>
  );
}
