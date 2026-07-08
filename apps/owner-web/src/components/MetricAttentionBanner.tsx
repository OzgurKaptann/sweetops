"use client";

/**
 * MetricAttentionBanner — Part 3: UI Enforcement
 *
 * Shows ONE metric that needs attention, with:
 *   - the specific metric value vs threshold
 *   - a plain-language reason
 *   - one concrete action the owner can take NOW
 *   - the downstream adaptation already applied (e.g. combo boost active)
 *
 * Priority order mirrors the operational context mode hierarchy:
 *   sla_critical > high_kitchen_load > boost_combos
 *   normal → banner is hidden entirely
 *
 * The banner is informational, not blocking — unlike the FocusBanner
 * which always requires action. This banner disappears on normal mode.
 */

import { useEffect, useState } from "react";
import { fetchOperationalContext, OperationalContextData } from "@/lib/api";

// ── Mode config ───────────────────────────────────────────────────────────────

interface ModeConfig {
  label: string;
  bg: string;
  border: string;
  icon: string;
  metricLabel: string;
  actionLabel: string;
  actionHref?: string;
  adaptationNote: string;
}

const MODE_CONFIG: Record<string, ModeConfig> = {
  sla_critical: {
    label: "Kitchen Critical",
    bg: "bg-red-600",
    border: "border-red-700",
    icon: "🚨",
    metricLabel: "SLA breach rate",
    actionLabel: "Open Kitchen Display →",
    actionHref: "/kitchen",
    adaptationNote: "Upsell suggestions reduced to 1 to protect kitchen capacity.",
  },
  high_kitchen_load: {
    label: "High Kitchen Load",
    bg: "bg-orange-500",
    border: "border-orange-600",
    icon: "⏱",
    metricLabel: "SLA breach / avg prep",
    actionLabel: "Open Kitchen Display →",
    actionHref: "/kitchen",
    adaptationNote: "Upsell suggestions reduced to 1 to decrease order complexity.",
  },
  boost_combos: {
    label: "Combo Visibility Low",
    bg: "bg-amber-500",
    border: "border-amber-600",
    icon: "🔄",
    metricLabel: "Combo usage rate",
    actionLabel: "Review Popular Combos ↓",
    adaptationNote: "Menu ranking is automatically boosting combo ingredients (1.6× weight).",
  },
};

// ── Value formatter ───────────────────────────────────────────────────────────

function formatMetricValue(mode: string, mv: OperationalContextData["metric_values"]): string {
  if (mode === "sla_critical" || mode === "high_kitchen_load") {
    const parts: string[] = [];
    if (mv.sla_breach_rate !== null)
      parts.push(`${(mv.sla_breach_rate * 100).toFixed(0)}% breach rate`);
    if (mv.avg_prep_time_minutes !== null)
      parts.push(`${mv.avg_prep_time_minutes.toFixed(1)} min avg prep`);
    return parts.join(" · ") || "—";
  }
  if (mode === "boost_combos") {
    const parts: string[] = [];
    if (mv.combo_usage_rate !== null)
      parts.push(`${(mv.combo_usage_rate * 100).toFixed(0)}% combos`);
    if (mv.upsell_acceptance_rate !== null)
      parts.push(`${(mv.upsell_acceptance_rate * 100).toFixed(0)}% upsell`);
    return parts.join(" · ") || "—";
  }
  return "—";
}

function formatThreshold(mode: string, thresholds: OperationalContextData["thresholds"]): string {
  if (mode === "sla_critical")
    return `>${(thresholds.sla_breach_critical * 100).toFixed(0)}% breach`;
  if (mode === "high_kitchen_load")
    return `>${(thresholds.sla_breach_high_load * 100).toFixed(0)}% breach or >${thresholds.avg_prep_high_load_min}min prep`;
  if (mode === "boost_combos")
    return `<${(thresholds.combo_rate_boost * 100).toFixed(0)}% combo rate`;
  return "";
}

// ── Main banner ───────────────────────────────────────────────────────────────

export function MetricAttentionBanner({ refreshTick }: { refreshTick: number }) {
  const [data, setData] = useState<OperationalContextData | null>(null);

  useEffect(() => {
    fetchOperationalContext()
      .then(setData)
      .catch(() => {}); // silent — this is supplementary
  }, [refreshTick]);

  if (!data || data.mode === "normal") return null;

  const cfg = MODE_CONFIG[data.mode];
  if (!cfg) return null;

  const metricValue = formatMetricValue(data.mode, data.metric_values);
  const threshold   = formatThreshold(data.mode, data.thresholds);
  const reason      = data.reasons[0] ?? "";

  return (
    <div className={`w-full border-b ${cfg.bg} ${cfg.border} px-6 py-2 text-white`}>
      <div className="max-w-screen-xl mx-auto flex items-center justify-between gap-4 flex-wrap">

        {/* Left: label + metric value */}
        <div className="flex items-center gap-3 min-w-0">
          <span className="text-sm shrink-0">{cfg.icon}</span>
          <div className="flex items-baseline gap-2 flex-wrap min-w-0">
            <span className="text-xs font-bold uppercase tracking-wide text-white/90 shrink-0">
              {cfg.label}
            </span>
            <span className="text-xs text-white font-semibold shrink-0">
              {cfg.metricLabel}: <span className="font-bold">{metricValue}</span>
            </span>
            <span className="text-[10px] text-white/70 hidden sm:block">
              (threshold: {threshold})
            </span>
          </div>
        </div>

        {/* Center: adaptation note */}
        <span className="text-[11px] text-white/80 hidden md:block italic shrink-0">
          {cfg.adaptationNote}
        </span>

        {/* Right: action */}
        {cfg.actionHref ? (
          <a
            href={cfg.actionHref}
            className="shrink-0 text-xs font-semibold bg-white/20 hover:bg-white/30 px-3 py-1 rounded-lg transition-colors"
          >
            {cfg.actionLabel}
          </a>
        ) : (
          <button
            onClick={() => {
              // Scroll to measurement section
              document.getElementById("metrics-section")?.scrollIntoView({ behavior: "smooth" });
            }}
            className="shrink-0 text-xs font-semibold bg-white/20 hover:bg-white/30 px-3 py-1 rounded-lg transition-colors"
          >
            {cfg.actionLabel}
          </button>
        )}

      </div>

      {/* Reason row (full width, collapsed on small screens) */}
      {reason && (
        <div className="max-w-screen-xl mx-auto mt-1 hidden lg:block">
          <p className="text-[10px] text-white/70 truncate">{reason}</p>
        </div>
      )}
    </div>
  );
}
