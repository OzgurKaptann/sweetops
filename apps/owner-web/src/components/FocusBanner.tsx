"use client";

import { OwnerDecision } from "@/lib/api";

interface Props {
  decision: OwnerDecision | null;
  onDismiss?: () => void;
}

const TYPE_VERB: Record<string, string> = {
  stock_risk: "Reorder stock",
  demand_spike: "Increase kitchen capacity",
  sla_risk: "Clear kitchen queue",
  revenue_anomaly: "Investigate revenue drop",
  slow_moving: "Reduce tied capital",
  // Metric-driven decisions
  metric_combo_health: "Boost combo visibility",
  metric_upsell_visibility: "Fix upsell placement",
  metric_owner_engagement: "Complete pending decisions",
  metric_kitchen_performance: "Investigate kitchen throughput",
};

/**
 * Full-width sticky banner that surfaces the single highest-priority decision.
 * Only shown when there is at least one active, non-dismissed decision.
 * The banner is intentionally unremovable until the decision is acted on —
 * this is the forcing function that drives the owner toward the primary action.
 */
export function FocusBanner({ decision, onDismiss }: Props) {
  if (!decision) return null;

  const verb = TYPE_VERB[decision.type] ?? "Take action";
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
          🔥 Focus now
        </span>
        <span className="text-white/80 hidden sm:block">→</span>
        <span className="text-sm font-medium truncate">
          <b>{verb}:</b> {decision.title}
        </span>
        {decision.data && "revenue_at_risk" in decision.data && (decision.data as any).revenue_at_risk > 0 && (
          <span className="shrink-0 text-xs font-semibold bg-white/20 px-2 py-0.5 rounded hidden md:block">
            ₺{(decision.data as any).revenue_at_risk.toFixed(0)} at risk
          </span>
        )}
      </div>

      <div className="flex items-center gap-3 shrink-0">
        <span className="text-xs text-white/70 hidden lg:block">
          Score: {decision.decision_score.toFixed(0)}
        </span>
        {onDismiss && (
          <button
            onClick={onDismiss}
            className="text-xs text-white/60 hover:text-white transition-colors"
            title="Snooze (scroll down to act)"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  );
}
