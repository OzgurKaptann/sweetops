"use client";

import type { ReadinessSummary } from "@/lib/setup-view";

/**
 * "Is my shop ready to take an order, and if not, what is missing?"
 *
 * The one screen that answers the question a fail-closed customer menu cannot:
 * a guest's blank phone looks identical whether nothing has been published or the
 * server is down. Every row shows the WORD "Tamam" / "Eksik" as well as a colour —
 * a tick with no text is unreadable to anyone who cannot distinguish the colours,
 * and this is the screen somebody uses once, under pressure, on their first day.
 */
export function ReadinessChecklist({
  summary,
  explanation,
  loading,
}: {
  summary: ReadinessSummary;
  /** Why the customer menu is currently empty, when it is. */
  explanation: string | null;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-5 text-sm text-gray-500">
        Kurulum durumu yükleniyor…
      </div>
    );
  }

  if (summary.rows.length === 0) {
    return (
      <div className="bg-white border border-gray-200 rounded-xl p-5 text-sm text-gray-500">
        Kurulum durumu şu anda görüntülenemiyor.
      </div>
    );
  }

  return (
    <section
      className={`rounded-xl border p-5 space-y-4 ${
        summary.ready
          ? "bg-emerald-50 border-emerald-200"
          : "bg-amber-50 border-amber-200"
      }`}
      aria-labelledby="setup-readiness-heading"
    >
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2
            id="setup-readiness-heading"
            className={`text-sm font-semibold ${
              summary.ready ? "text-emerald-900" : "text-amber-900"
            }`}
          >
            {summary.title}
          </h2>
          <p
            className={`text-xs mt-1 ${
              summary.ready ? "text-emerald-800" : "text-amber-800"
            }`}
          >
            {summary.detail}
          </p>
        </div>
        <span
          className={`text-xs font-medium px-2.5 py-1 rounded-full shrink-0 ${
            summary.ready
              ? "bg-emerald-100 text-emerald-800"
              : "bg-amber-100 text-amber-900"
          }`}
        >
          {summary.progressLabel}
        </span>
      </div>

      {explanation && (
        <p className="text-xs text-amber-900 bg-white/70 border border-amber-200 rounded-lg px-3 py-2 leading-relaxed">
          {explanation}
        </p>
      )}

      <ul className="space-y-2">
        {summary.rows.map((row) => (
          <li
            key={row.key}
            className="flex items-start gap-3 bg-white rounded-lg border border-gray-200 px-3 py-2.5"
          >
            <span
              aria-hidden="true"
              className={`mt-0.5 w-5 h-5 shrink-0 rounded-full text-[11px] font-bold flex items-center justify-center ${
                row.done
                  ? "bg-emerald-100 text-emerald-700"
                  : "bg-amber-100 text-amber-800"
              }`}
            >
              {row.done ? "✓" : "!"}
            </span>
            <div className="min-w-0">
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="text-sm font-medium text-gray-900">
                  {row.label}
                </span>
                {/* The status in WORDS, never colour alone. */}
                <span
                  className={`text-[11px] font-semibold ${
                    row.done ? "text-emerald-700" : "text-amber-800"
                  }`}
                >
                  {row.statusLabel}
                </span>
              </div>
              <p className="text-xs text-gray-500 mt-0.5 leading-relaxed">
                {row.detail}
              </p>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
