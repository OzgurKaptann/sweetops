"use client";

import { useState, useEffect, useCallback } from "react";
import {
  fetchDecisions,
  patchDecision,
  OwnerDecision,
  OwnerDecisionsResponse,
  DecisionAction,
  DecisionType,
  ResolutionQuality,
  StockRiskData,
  DemandSpikeData,
  SLARiskData,
  RevenueAnomalyData,
  SlowMovingData,
} from "@/lib/api";

// ── Severity styling ──────────────────────────────────────────────────────────

const SEVERITY_STYLES = {
  high:   { border: "border-l-red-500",   badge: "bg-red-100 text-red-700",   label: "HIGH", dot: "bg-red-500" },
  medium: { border: "border-l-amber-400", badge: "bg-amber-100 text-amber-700", label: "MED", dot: "bg-amber-400" },
  low:    { border: "border-l-yellow-300", badge: "bg-yellow-50 text-yellow-700", label: "LOW", dot: "bg-yellow-400" },
} as const;

const TYPE_LABELS: Record<DecisionType, string> = {
  stock_risk: "Stock Risk",
  demand_spike: "Demand Spike",
  slow_moving: "Slow Moving",
  sla_risk: "SLA Risk",
  revenue_anomaly: "Revenue Anomaly",
};

const TYPE_ICONS: Record<DecisionType, string> = {
  stock_risk: "📦",
  demand_spike: "📈",
  slow_moving: "🐌",
  sla_risk: "⏱",
  revenue_anomaly: "💰",
};

// ── Resolution quality config ─────────────────────────────────────────────────

const QUALITY_CONFIG = {
  good:    { label: "Issue resolved",   bg: "bg-emerald-50", text: "text-emerald-700", icon: "✓" },
  partial: { label: "Partially resolved", bg: "bg-amber-50",   text: "text-amber-700",  icon: "~" },
  failed:  { label: "Could not resolve",  bg: "bg-red-50",     text: "text-red-600",    icon: "✕" },
} as const;

// ── Data context row ──────────────────────────────────────────────────────────

function DataContext({ decision }: { decision: OwnerDecision }) {
  const d = decision.data;
  const type = decision.type;

  if (type === "stock_risk") {
    const data = d as StockRiskData;
    return (
      <div className="flex flex-wrap gap-3 text-xs text-gray-500 mt-2">
        <span>Stock: <b className="text-gray-700">{data.current_stock.toFixed(1)} {data.unit}</b></span>
        {data.hours_to_stockout !== null && (
          <span>Stockout: <b className="text-red-600">{data.hours_to_stockout.toFixed(1)}h</b></span>
        )}
        <span>At risk: <b className="text-red-600">₺{data.revenue_at_risk.toFixed(0)}</b></span>
      </div>
    );
  }
  if (type === "demand_spike") {
    const data = d as DemandSpikeData;
    return (
      <div className="flex flex-wrap gap-3 text-xs text-gray-500 mt-2">
        <span>Last 1h: <b className="text-gray-700">{data.last_1h_orders} orders</b></span>
        <span>Baseline: <b className="text-gray-700">{data.avg_hourly_baseline.toFixed(1)}/h</b></span>
        <span>Spike: <b className="text-amber-600">{data.spike_ratio.toFixed(1)}×</b></span>
      </div>
    );
  }
  if (type === "sla_risk") {
    const data = d as SLARiskData;
    return (
      <div className="flex flex-wrap gap-3 text-xs text-gray-500 mt-2">
        <span>Critical: <b className="text-red-600">{data.critical_count}</b></span>
        <span>Warning: <b className="text-amber-600">{data.warning_count}</b></span>
        <span>Worst: <b className="text-red-600">{data.worst_age_minutes.toFixed(1)} min</b></span>
      </div>
    );
  }
  if (type === "revenue_anomaly") {
    const data = d as RevenueAnomalyData;
    const isDrop = data.direction === "drop";
    return (
      <div className="flex flex-wrap gap-3 text-xs text-gray-500 mt-2">
        <span>Last 1h: <b className="text-gray-700">₺{data.last_1h_revenue.toFixed(0)}</b></span>
        <span>Baseline: <b className="text-gray-700">₺{data.avg_hourly_baseline.toFixed(0)}/h</b></span>
        <span>Ratio: <b className={isDrop ? "text-red-600" : "text-emerald-600"}>{isDrop ? "▼" : "▲"} {(data.ratio * 100).toFixed(0)}%</b></span>
      </div>
    );
  }
  if (type === "slow_moving") {
    const data = d as SlowMovingData;
    return (
      <div className="flex flex-wrap gap-3 text-xs text-gray-500 mt-2">
        <span>Stock: <b className="text-gray-700">{data.current_stock.toFixed(1)}</b></span>
        <span>Capital tied: <b className="text-amber-600">₺{data.tied_capital.toFixed(0)}</b></span>
      </div>
    );
  }
  return null;
}

// ── Resolution feedback badge ─────────────────────────────────────────────────

function ResolutionBadge({ decision }: { decision: OwnerDecision }) {
  if (!decision.completed_at || !decision.resolution_quality) return null;

  const qcfg = QUALITY_CONFIG[decision.resolution_quality];
  const completedAt = new Date(decision.completed_at);
  const createdAt = new Date(decision.created_at);
  const minutesElapsed = Math.round((completedAt.getTime() - createdAt.getTime()) / 60000);

  return (
    <div className={`flex items-center gap-2 mt-2 px-3 py-1.5 rounded-lg ${qcfg.bg}`}>
      <span className={`text-xs font-bold ${qcfg.text}`}>{qcfg.icon} {qcfg.label}</span>
      {decision.estimated_revenue_saved != null && decision.estimated_revenue_saved > 0 && (
        <span className="text-xs text-emerald-600 font-semibold">
          ~₺{decision.estimated_revenue_saved.toFixed(0)} saved
        </span>
      )}
      <span className="text-xs text-gray-400 ml-auto">{minutesElapsed}m to act</span>
    </div>
  );
}

// ── Quality selector (shown when completing) ──────────────────────────────────

interface QualitySelectorProps {
  onSelect: (q: ResolutionQuality) => void;
  onCancel: () => void;
  revenueatRisk?: number;
}

function QualitySelector({ onSelect, onCancel, revenueatRisk }: QualitySelectorProps) {
  return (
    <div className="mt-2 p-3 bg-gray-50 rounded-lg border border-gray-200">
      <p className="text-xs font-semibold text-gray-600 mb-2">How was it resolved?</p>
      <div className="flex gap-2 mb-2">
        {(["good", "partial", "failed"] as ResolutionQuality[]).map((q) => {
          const cfg = QUALITY_CONFIG[q];
          return (
            <button
              key={q}
              onClick={() => onSelect(q)}
              className={`flex-1 py-1.5 text-xs font-semibold rounded-lg border transition-all
                ${cfg.bg} ${cfg.text} border-transparent hover:border-current`}
            >
              {cfg.icon} {cfg.label}
            </button>
          );
        })}
      </div>
      {revenueatRisk && revenueatRisk > 0 && (
        <p className="text-[10px] text-gray-400">
          Potential save: ₺{revenueatRisk.toFixed(0)} (used for "Issue resolved" quality)
        </p>
      )}
      <button onClick={onCancel} className="mt-1 text-xs text-gray-400 hover:text-gray-600">
        Cancel
      </button>
    </div>
  );
}

// ── Single decision card ──────────────────────────────────────────────────────

interface CardProps {
  decision: OwnerDecision;
  isPrimary: boolean;
  onAction: (id: string, action: DecisionAction, quality?: ResolutionQuality, revenueSaved?: number) => Promise<void>;
}

function DecisionCard({ decision, isPrimary, onAction }: CardProps) {
  const [acting, setActing] = useState<DecisionAction | null>(null);
  const [showQuality, setShowQuality] = useState(false);
  const sev = SEVERITY_STYLES[decision.severity];

  const isPending = decision.status === "pending";
  const isAcknowledged = decision.status === "acknowledged";
  const isResolved = decision.status === "completed" || decision.status === "dismissed";

  const handleAction = async (action: DecisionAction) => {
    if (action === "complete") {
      setShowQuality(true);
      return;
    }
    setActing(action);
    try { await onAction(decision.decision_id, action); }
    finally { setActing(null); }
  };

  const handleQuality = async (quality: ResolutionQuality) => {
    setShowQuality(false);
    setActing("complete");
    const revAtRisk = (decision.data as any)?.revenue_at_risk;
    const saved = quality === "good" && revAtRisk ? revAtRisk : undefined;
    try { await onAction(decision.decision_id, "complete", quality, saved); }
    finally { setActing(null); }
  };

  return (
    <div
      className={`bg-white rounded-xl border border-gray-100 border-l-4 ${sev.border} p-4 transition-all ${
        isResolved ? "opacity-50" : ""
      } ${isPrimary ? "ring-2 ring-amber-400 ring-offset-1" : ""}`}
    >
      {/* Primary focus badge */}
      {isPrimary && !isResolved && (
        <div className="flex items-center gap-1.5 mb-2 -mt-1">
          <span className="text-[10px] font-bold bg-amber-400 text-white px-2 py-0.5 rounded uppercase tracking-wide">
            🔥 Primary Focus
          </span>
        </div>
      )}

      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-base leading-none shrink-0">{TYPE_ICONS[decision.type]}</span>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap mb-0.5">
              <span className={`text-[10px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wide ${sev.badge}`}>
                {sev.label}
              </span>
              <span className="text-[10px] text-gray-400 font-medium uppercase tracking-wide">
                {TYPE_LABELS[decision.type]}
              </span>
              {decision.blocking_vs_non_blocking && (
                <span className="text-[10px] bg-red-50 text-red-600 px-1.5 py-0.5 rounded font-semibold uppercase">
                  Blocking
                </span>
              )}
            </div>
            <p className="text-sm font-semibold text-gray-900 leading-snug">{decision.title}</p>
          </div>
        </div>
        {!isPending && (
          <span className={`shrink-0 text-[10px] font-semibold px-2 py-0.5 rounded-full uppercase ${
            decision.status === "acknowledged" ? "bg-blue-50 text-blue-600" :
            decision.status === "completed" ? "bg-emerald-50 text-emerald-700" :
            "bg-gray-100 text-gray-500"
          }`}>
            {decision.status}
          </span>
        )}
      </div>

      {/* Data context */}
      <DataContext decision={decision} />

      {/* Why now + expected impact */}
      <div className="mt-3 space-y-1">
        <div className="flex gap-1.5">
          <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide shrink-0 mt-0.5 w-16">Why now</span>
          <span className="text-xs text-gray-600">{decision.why_now}</span>
        </div>
        <div className="flex gap-1.5">
          <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide shrink-0 mt-0.5 w-16">Impact</span>
          <span className="text-xs text-gray-600">{decision.expected_impact}</span>
        </div>
      </div>

      {/* Recommended action */}
      <div className="mt-3 px-3 py-2 bg-gray-50 rounded-lg">
        <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wide">Action</span>
        <p className="text-xs font-medium text-gray-800 mt-0.5">{decision.recommended_action}</p>
      </div>

      {/* Resolution feedback (completed decisions) */}
      <ResolutionBadge decision={decision} />

      {/* Quality selector (shown before completing) */}
      {showQuality && (
        <QualitySelector
          onSelect={handleQuality}
          onCancel={() => setShowQuality(false)}
          revenueatRisk={(decision.data as any)?.revenue_at_risk}
        />
      )}

      {/* Action buttons */}
      {!isResolved && !showQuality && (
        <div className="flex items-center gap-2 mt-3">
          {isPending && (
            <button
              onClick={() => handleAction("acknowledge")}
              disabled={acting !== null}
              className="px-3 py-1.5 text-xs font-medium rounded-lg bg-blue-50 text-blue-700 hover:bg-blue-100 disabled:opacity-50 transition-colors"
            >
              {acting === "acknowledge" ? "…" : "Acknowledge"}
            </button>
          )}
          <button
            onClick={() => handleAction("complete")}
            disabled={acting !== null}
            className="px-3 py-1.5 text-xs font-medium rounded-lg bg-emerald-50 text-emerald-700 hover:bg-emerald-100 disabled:opacity-50 transition-colors"
          >
            {acting === "complete" ? "…" : "Mark Done"}
          </button>
          <button
            onClick={() => handleAction("dismiss")}
            disabled={acting !== null}
            className="px-3 py-1.5 text-xs font-medium rounded-lg text-gray-400 hover:bg-gray-100 disabled:opacity-50 transition-colors ml-auto"
          >
            {acting === "dismiss" ? "…" : "Dismiss"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── Summary pills ─────────────────────────────────────────────────────────────

function SummaryPill({ count, severity }: { count: number; severity: "high" | "medium" | "low" }) {
  if (count === 0) return null;
  const s = SEVERITY_STYLES[severity];
  return (
    <span className={`inline-flex items-center gap-1 text-xs font-semibold px-2 py-0.5 rounded-full ${s.badge}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s.dot}`} />
      {count} {severity}
    </span>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

interface Props {
  refreshTick?: number;
  /** Called with the top decision so page can render FocusBanner */
  onPrimaryDecision?: (d: OwnerDecision | null) => void;
}

export function DecisionPanel({ refreshTick, onPrimaryDecision }: Props) {
  const [data, setData] = useState<OwnerDecisionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(false);
    fetchDecisions()
      .then((d) => {
        setData(d);
        // Surface primary decision to parent
        const active = d.decisions.filter(
          (dec) => dec.status !== "completed" && dec.status !== "dismissed",
        );
        const top = active.sort((a, b) => {
          if (a.blocking_vs_non_blocking !== b.blocking_vs_non_blocking)
            return a.blocking_vs_non_blocking ? -1 : 1;
          return b.decision_score - a.decision_score;
        })[0] ?? null;
        onPrimaryDecision?.(top);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, [onPrimaryDecision]);

  useEffect(() => { load(); }, [load, refreshTick]);

  const handleAction = useCallback(
    async (id: string, action: DecisionAction, quality?: ResolutionQuality, revenueSaved?: number) => {
      await patchDecision(id, action, undefined, undefined, quality, revenueSaved);
      load();
    },
    [load],
  );

  if (loading) {
    return (
      <div className="space-y-3">
        {[...Array(2)].map((_, i) => (
          <div key={i} className="bg-white rounded-xl border border-gray-100 border-l-4 border-l-gray-200 p-4 animate-pulse">
            <div className="flex gap-2 mb-3"><div className="h-4 w-12 bg-gray-200 rounded" /><div className="h-4 w-20 bg-gray-100 rounded" /></div>
            <div className="h-3 bg-gray-200 rounded w-3/4 mb-2" />
            <div className="h-3 bg-gray-100 rounded w-full mb-2" />
            <div className="h-8 bg-gray-200 rounded-lg" />
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-4 text-sm text-red-600 bg-red-50 rounded-xl border border-red-100">
        Decision system unavailable.
      </div>
    );
  }

  const activeDecisions = data?.decisions.filter(
    (d) => d.status !== "completed" && d.status !== "dismissed",
  ) ?? [];

  // Include recently-completed for feedback (last 3 hours)
  const recentCompleted = data?.decisions.filter((d) => {
    if (d.status !== "completed" || !d.completed_at) return false;
    const age = Date.now() - new Date(d.completed_at).getTime();
    return age < 3 * 60 * 60 * 1000;
  }) ?? [];

  if (activeDecisions.length === 0 && recentCompleted.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-6 text-center">
        <div className="text-3xl mb-2">✓</div>
        <p className="text-sm font-semibold text-emerald-600">No active alerts</p>
        <p className="text-xs text-gray-400 mt-1">All signals clear.</p>
      </div>
    );
  }

  const sorted = [...activeDecisions].sort((a, b) => {
    if (a.blocking_vs_non_blocking !== b.blocking_vs_non_blocking)
      return a.blocking_vs_non_blocking ? -1 : 1;
    return b.decision_score - a.decision_score;
  });

  const primaryId = sorted[0]?.decision_id;
  const summary = data?.summary ?? { high: 0, medium: 0, low: 0 };

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <SummaryPill count={summary.high} severity="high" />
        <SummaryPill count={summary.medium} severity="medium" />
        <SummaryPill count={summary.low} severity="low" />
        <span className="ml-auto text-xs text-gray-400">{data?.active_count ?? 0} active</span>
      </div>

      <div className="space-y-3">
        {sorted.map((d) => (
          <DecisionCard
            key={d.decision_id}
            decision={d}
            isPrimary={d.decision_id === primaryId}
            onAction={handleAction}
          />
        ))}

        {/* Recently resolved — feedback only, no actions */}
        {recentCompleted.length > 0 && (
          <div className="mt-2">
            <p className="text-[10px] text-gray-400 uppercase tracking-wide mb-2">Recently resolved</p>
            {recentCompleted.map((d) => (
              <DecisionCard
                key={d.decision_id}
                decision={d}
                isPrimary={false}
                onAction={handleAction}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
