"use client";

import { OwnerDecision } from "@/lib/api";

interface Props {
  decision: OwnerDecision | null;
  onDismiss?: () => void;
}

// Keyed by the API's decision `type` enum — the keys stay English, the verbs
// the owner reads do not.
const TYPE_VERB: Record<string, string> = {
  stock_risk: "Stok siparişi verin",
  demand_spike: "Mutfak kapasitesini artırın",
  sla_risk: "Mutfak sırasını eritin",
  revenue_anomaly: "Ciro düşüşünü inceleyin",
  slow_moving: "Stokta bağlı sermayeyi azaltın",
  // Metric-driven decisions
  metric_combo_health: "Kombinasyon görünürlüğünü artırın",
  metric_upsell_visibility: "Öneri yerleşimini düzeltin",
  metric_owner_engagement: "Bekleyen uyarıları kapatın",
  metric_kitchen_performance: "Mutfak temposunu inceleyin",
};

/**
 * Full-width sticky banner that surfaces the single highest-priority decision.
 * Only shown when there is at least one active, non-dismissed decision.
 * The banner is intentionally unremovable until the decision is acted on —
 * this is the forcing function that drives the owner toward the primary action.
 */
export function FocusBanner({ decision, onDismiss }: Props) {
  if (!decision) return null;

  const verb = TYPE_VERB[decision.type] ?? "Harekete geçin";
  const isBlocking = decision.blocking_vs_non_blocking;

  return (
    <div
      className={`w-full border-b px-6 py-2.5 flex items-center justify-between gap-4 ${
        isBlocking
          ? "bg-red-600 border-red-700 text-white"
          : "bg-amber-500 border-amber-600 text-white"
      }`}
    >
      <div className="flex items-center gap-3 min-w-0">
        <span className="text-sm font-bold shrink-0 uppercase tracking-wide">
          🔥 Öncelik
        </span>
        <span className="text-white/80 hidden sm:block">→</span>
        <span className="text-sm font-medium truncate">
          <b>{verb}:</b> {decision.title}
        </span>
        {decision.data && "revenue_at_risk" in decision.data && (decision.data as any).revenue_at_risk > 0 && (
          <span className="shrink-0 text-xs font-semibold bg-white/20 px-2 py-0.5 rounded hidden md:block">
            ₺{(decision.data as any).revenue_at_risk.toFixed(0)} risk altında
          </span>
        )}
      </div>

      <div className="flex items-center gap-3 shrink-0">
        <span className="text-xs text-white/70 hidden lg:block">
          Öncelik puanı: {decision.decision_score.toFixed(0)}
        </span>
        {onDismiss && (
          <button
            onClick={onDismiss}
            className="text-xs text-white/60 hover:text-white transition-colors"
            title="Ertele (işlem için aşağı kaydırın)"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}
