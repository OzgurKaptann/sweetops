"use client";

import { useState } from "react";

import type { TableQrReceipt } from "@/lib/setup-api";
import { SETUP_COPY } from "@/lib/setup-view";

/**
 * The one and only time a QR link is readable.
 *
 * The raw token is stored as a SHA-256 hash and cannot be recovered, so this
 * dialog is the entire window in which the link exists in cleartext. Everything
 * about it is shaped by that:
 *
 *   * the warning comes first, not as fine print at the bottom;
 *   * the link is selectable text as well as a copy button, because a clipboard
 *     write can silently fail in a browser that has not granted permission;
 *   * after a ROTATION it says the old sticker is now dead, because somebody has
 *     to walk to the table with a new printout.
 */
export function QrLinkModal({
  receipt,
  onClose,
}: {
  receipt: TableQrReceipt;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState<"idle" | "ok" | "failed">("idle");

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(receipt.qr_url);
      setCopied("ok");
    } catch {
      // Never claim a copy that did not happen — the link is unrecoverable, so a
      // false "kopyalandı" is how a shop loses a sticker.
      setCopied("failed");
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 px-4">
      <div className="w-full max-w-lg bg-white rounded-xl shadow-lg p-6 space-y-4">
        <div>
          <h2 className="text-base font-semibold text-gray-900">
            {receipt.display_name} — QR bağlantısı
          </h2>
          <p className="text-sm text-amber-900 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2 mt-2 leading-relaxed">
            {/* Server-written, so no client can soften it. */}
            {receipt.notice || SETUP_COPY.qrShownOnceWarning}
          </p>
        </div>

        {receipt.previous_token_revoked && (
          <p className="text-sm text-red-800 bg-red-50 border border-red-200 rounded-lg px-3 py-2 leading-relaxed">
            Bu masanın önceki QR kodu geçersiz oldu. Masada duran basılı kodu
            okutan misafirler sipariş veremez; yeni kodu bastırıp masaya
            yerleştirin.
          </p>
        )}

        <div>
          <label
            htmlFor="qr-url"
            className="block text-xs font-medium text-gray-500 mb-1"
          >
            Misafir bağlantısı
          </label>
          <textarea
            id="qr-url"
            readOnly
            rows={3}
            value={receipt.qr_url}
            onFocus={(e) => e.currentTarget.select()}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs font-mono text-gray-900 break-all focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
          <p className="text-xs text-gray-400 mt-1">
            Kod öneki: <span className="font-mono">{receipt.token_prefix}</span> —
            bu önek gizli değildir ve masadaki kodu tanımak için kullanılır.
          </p>
        </div>

        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="text-xs">
            {copied === "ok" && (
              <span className="text-emerald-700">Bağlantı kopyalandı.</span>
            )}
            {copied === "failed" && (
              <span className="text-amber-800">
                Kopyalanamadı. Bağlantıyı seçip elle kopyalayın.
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={copy}
              className="text-sm px-3 py-2 rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 font-medium"
            >
              Bağlantıyı kopyala
            </button>
            <a
              href={receipt.qr_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-sm px-3 py-2 rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 font-medium"
            >
              Bağlantıyı aç
            </a>
            <button
              type="button"
              onClick={onClose}
              className="text-sm px-4 py-2 rounded-lg bg-gray-800 text-white hover:bg-gray-900 font-semibold"
            >
              Kaydettim, kapat
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
