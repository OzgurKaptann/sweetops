"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import Link from "next/link";

function SuccessContent() {
  const searchParams = useSearchParams();
  const orderId = searchParams?.get("order_id") || "-";
  const amount = searchParams?.get("amount") || "0";

  return (
    <main className="success-page">
      <div className="success-icon">✓</div>
      <h1>Siparişiniz Alındı!</h1>
      <p className="sub">Waffle'ınız hazırlanıyor 🧇</p>

      <div className="success-card">
        <div className="success-row">
          <span className="label">Sipariş No</span>
          <span className="value">#{orderId}</span>
        </div>
        <div className="success-row">
          <span className="label">Toplam</span>
          <span className="value">₺{parseFloat(amount).toFixed(2)}</span>
        </div>
      </div>

      <Link href="/" className="new-order-link">
        Yeni Sipariş Ver
      </Link>
    </main>
  );
}

export default function SuccessPage() {
  return (
    <Suspense
      fallback={
        <div className="loading-screen">
          <div className="loading-spinner" />
        </div>
      }
    >
      <SuccessContent />
    </Suspense>
  );
}
