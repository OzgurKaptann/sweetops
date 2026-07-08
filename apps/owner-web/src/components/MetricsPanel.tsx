"use client";

import { useEffect, useState } from "react";
import {
  fetchMetrics,
  DailyMetricsData,
  TrendValue,
  DataQuality,
  MetricsObservability,
} from "@/lib/api";

// ── Constants ─────────────────────────────────────────────────────────────────

const RETRY_DELAY_MS = 15_000;   // retry after API failure


// ── Data quality badge ────────────────────────────────────────────────────────

function QualityBadge({ quality }: { quality: DataQuality }) {
  if (quality.status === "valid") return null;

  const styles: Record<string, string> = {
    low_sample: "bg-amber-50 text-amber-700 border-amber-200",
    no_data:    "bg-gray-100 text-gray-500 border-gray-200",
    unreliable: "bg-red-50 text-red-700 border-red-200",
  };
  const labels: Record<string, string> = {
    low_sample: "low sample",
    no_data:    "no data",
    unreliable: "unreliable",
  };

  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 rounded border text-[10px] font-medium ${styles[quality.status] ?? styles["no_data"]}`}
      title={quality.message ?? undefined}
    >
      {labels[quality.status] ?? quality.status}
    </span>
  );
}


// ── Trend arrow ───────────────────────────────────────────────────────────────

function TrendArrow({
  trend,
  pct_change,
  lowerIsBetter = false,
  quality,
}: {
  trend: string;
  pct_change: number | null;
  lowerIsBetter?: boolean;
  quality: DataQuality;
}) {
  // Don't show trend on unreliable or no_data metrics
  if (quality.status === "unreliable" || quality.status === "no_data") return null;
  if (trend === "flat" || pct_change === null) {
    return <span className="text-gray-400 text-xs">—</span>;
  }

  // For "lower is better" metrics (prep time, SLA breach): down = good
  const isGood = lowerIsBetter ? trend === "down" : trend === "up";
  const colour  = isGood ? "text-emerald-600" : "text-red-500";
  const arrow   = trend === "up" ? "↑" : "↓";
  const sign    = pct_change > 0 ? "+" : "";

  return (
    <span className={`text-xs font-medium ${colour}`}>
      {arrow} {sign}{pct_change}%
    </span>
  );
}


// ── Single metric row ─────────────────────────────────────────────────────────

function MetricRow({
  label,
  tv,
  format,
  lowerIsBetter,
}: {
  label: string;
  tv: TrendValue;
  format: (v: number) => string;
  lowerIsBetter?: boolean;
}) {
  const isNoData = tv.quality.status === "no_data";

  return (
    <div className="flex items-center justify-between py-1.5 border-b border-gray-100 last:border-0">
      <span className="text-xs text-gray-500 truncate pr-2">{label}</span>
      <div className="flex items-center gap-1.5 shrink-0">
        {isNoData ? (
          <span className="text-xs text-gray-400 italic">—</span>
        ) : (
          <span className="text-xs font-semibold text-gray-900">{format(tv.value)}</span>
        )}
        <QualityBadge quality={tv.quality} />
        <TrendArrow
          trend={tv.trend}
          pct_change={tv.pct_change}
          lowerIsBetter={lowerIsBetter}
          quality={tv.quality}
        />
      </div>
    </div>
  );
}


// ── Metric group card ─────────────────────────────────────────────────────────

function MetricGroup({
  title,
  icon,
  children,
}: {
  title: string;
  icon: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <div className="flex items-center gap-2 mb-3">
        <span className="text-sm">{icon}</span>
        <span className="text-xs font-semibold text-gray-700 uppercase tracking-wide">{title}</span>
      </div>
      <div>{children}</div>
    </div>
  );
}


// ── Raw count row (no trend, no quality) ──────────────────────────────────────

function CountRow({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-gray-100 last:border-0">
      <span className="text-xs text-gray-500">{label}</span>
      <span className="text-xs font-semibold text-gray-900">{value}</span>
    </div>
  );
}


// ── Formatters ────────────────────────────────────────────────────────────────

const pct   = (v: number) => `${(v * 100).toFixed(1)}%`;
const mins  = (v: number) => `${v.toFixed(1)} min`;
const money = (v: number) =>
  v === 0
    ? "—"
    : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;


// ── Freshness footer ──────────────────────────────────────────────────────────

function FreshnessFooter({ meta }: { meta: MetricsObservability }) {
  const computedAt = new Date(meta.computed_at);
  const timeStr = computedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const hasErrors = meta.errors.length > 0;

  return (
    <div className="flex items-center justify-between mt-3 px-0.5">
      <span className="text-[10px] text-gray-400">
        vs {meta.comparison_date} · computed {timeStr} · {meta.computation_ms}ms
      </span>
      {hasErrors && (
        <span
          className="text-[10px] text-amber-600 font-medium cursor-help"
          title={`Validation issues:\n${meta.errors.join("\n")}`}
        >
          ⚠ {meta.errors.length} data issue{meta.errors.length > 1 ? "s" : ""}
        </span>
      )}
    </div>
  );
}


// ── Skeleton loader ───────────────────────────────────────────────────────────

function MetricsSkeleton() {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 animate-pulse">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="bg-white rounded-xl border border-gray-200 h-44" />
        ))}
      </div>
    </div>
  );
}


// ── Degraded state (API unavailable) ─────────────────────────────────────────

function MetricsDegraded({
  error,
  onRetry,
}: {
  error: { status?: number; detail?: { message?: string } } | Error;
  onRetry: () => void;
}) {
  const isInfra = "status" in error && error.status === 503;
  const isFuture = "status" in error && error.status === 422;

  let title = "Metrics unavailable";
  let body  = "Could not load measurement data. Other dashboard panels are unaffected.";

  if (isInfra) {
    title = "Metrics service unavailable";
    body  = "The metrics database is temporarily unavailable. Data will appear automatically when it recovers.";
  } else if (isFuture) {
    title = "Future date selected";
    body  = "Metrics can only be computed for today or past dates.";
  }

  return (
    <div className="bg-white rounded-xl border border-amber-200 p-4">
      <div className="flex items-start gap-3">
        <span className="text-amber-500 text-base mt-0.5">⚠</span>
        <div className="flex-1">
          <p className="text-xs font-semibold text-gray-800">{title}</p>
          <p className="text-xs text-gray-500 mt-0.5">{body}</p>
        </div>
        {!isFuture && (
          <button
            onClick={onRetry}
            className="shrink-0 text-xs px-2 py-1 rounded border border-gray-200 text-gray-600 hover:bg-gray-50 transition-colors"
          >
            Retry
          </button>
        )}
      </div>
    </div>
  );
}


// ── Main component ────────────────────────────────────────────────────────────

export function MetricsPanel({ refreshTick }: { refreshTick: number }) {
  const [data, setData]       = useState<DailyMetricsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<unknown>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    fetchMetrics()
      .then((d) => { setData(d); setLoading(false); })
      .catch((e) => { setError(e); setLoading(false); });
  };

  useEffect(() => { load(); }, [refreshTick]);

  if (loading && !data) return <MetricsSkeleton />;

  if (error && !data) {
    return (
      <MetricsDegraded
        error={error as { status?: number; detail?: { message?: string } } | Error}
        onRetry={load}
      />
    );
  }

  if (!data) return null;

  const { conversion, decisions, kitchen, revenue_protection, meta } = data;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">

        {/* Conversion */}
        <MetricGroup title="Conversion" icon="🔄">
          <MetricRow
            label="Combo usage rate"
            tv={conversion.combo_usage_rate}
            format={pct}
          />
          <MetricRow
            label="AOV with combo"
            tv={conversion.avg_order_value_with_combo}
            format={money}
          />
          <MetricRow
            label="AOV without combo"
            tv={conversion.avg_order_value_without_combo}
            format={money}
          />
          <MetricRow
            label="Upsell acceptance"
            tv={conversion.upsell_acceptance_rate}
            format={pct}
          />
        </MetricGroup>

        {/* Decision Quality */}
        <MetricGroup title="Decision Quality" icon="✅">
          <CountRow label="Seen today"    value={decisions.decisions_seen} />
          <CountRow label="Acknowledged"  value={decisions.decisions_acknowledged} />
          <CountRow label="Completed"     value={decisions.decisions_completed} />
          <MetricRow
            label="Completion rate"
            tv={decisions.completion_rate}
            format={pct}
          />
        </MetricGroup>

        {/* Kitchen — lower is better */}
        <MetricGroup title="Kitchen" icon="⏱">
          <MetricRow
            label="Avg prep time"
            tv={kitchen.avg_prep_time_minutes}
            format={mins}
            lowerIsBetter
          />
          <MetricRow
            label="P90 prep time"
            tv={kitchen.p90_prep_time_minutes}
            format={mins}
            lowerIsBetter
          />
          <MetricRow
            label="SLA breach rate"
            tv={kitchen.sla_breach_rate}
            format={pct}
            lowerIsBetter
          />
        </MetricGroup>

        {/* Revenue Protection */}
        <MetricGroup title="Revenue Protection" icon="🛡">
          <CountRow label="Risks triggered" value={revenue_protection.stock_risk_triggered} />
          <CountRow label="Resolved"        value={revenue_protection.stock_risk_resolved} />
          <div className="flex items-center justify-between py-1.5 border-b border-gray-100">
            <span className="text-xs text-gray-500">Est. saved</span>
            <span className={`text-xs font-semibold ${
              revenue_protection.estimated_revenue_saved > 0
                ? "text-emerald-700"
                : "text-gray-400"
            }`}>
              {revenue_protection.estimated_revenue_saved > 0
                ? money(revenue_protection.estimated_revenue_saved)
                : "—"}
            </span>
          </div>
          <div className="flex items-center justify-between py-1.5">
            <span className="text-xs text-gray-500">Outcome</span>
            <span className="text-xs text-gray-700 tabular-nums">
              <span className="text-emerald-700">{revenue_protection.actual_outcome.good}G</span>
              {" · "}
              <span className="text-amber-600">{revenue_protection.actual_outcome.partial}P</span>
              {" · "}
              <span className="text-red-500">{revenue_protection.actual_outcome.failed}F</span>
            </span>
          </div>
        </MetricGroup>

      </div>

      {/* Freshness + validation warnings */}
      <FreshnessFooter meta={meta} />
    </div>
  );
}
