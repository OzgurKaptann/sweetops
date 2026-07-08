"use client";

import { useState, useEffect } from "react";
import { fetchPrepTime, fetchDecisions, SLARiskData } from "@/lib/api";

interface PrepData {
  avg_prep_seconds: number | null;
  avg_prep_display: string;
  fastest_seconds: number | null;
  fastest_display: string;
  slowest_seconds: number | null;
  slowest_display: string;
  total_tracked: number;
}

function LoadBar({ value, max, color }: { value: number; max: number; color: string }) {
  const pct = max > 0 ? Math.min((value / max) * 100, 100) : 0;
  return (
    <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden">
      <div
        className={`h-full rounded-full transition-all duration-500 ${color}`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

interface Props {
  refreshTick?: number;
}

export function KitchenSLAPanel({ refreshTick }: Props) {
  const [prep, setPrep] = useState<PrepData | null>(null);
  const [slaData, setSlaData] = useState<SLARiskData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetchPrepTime().catch(() => null),
      fetchDecisions().catch(() => null),
    ])
      .then(([p, d]) => {
        setPrep(p as PrepData | null);
        // Extract SLA risk data from decisions if present
        if (d) {
          const slaDecision = d.decisions.find((dec) => dec.type === "sla_risk");
          if (slaDecision) {
            setSlaData(slaDecision.data as SLARiskData);
          } else {
            setSlaData(null);
          }
        }
      })
      .finally(() => setLoading(false));
  }, [refreshTick]);

  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-gray-100 p-5 animate-pulse">
        <div className="h-3 w-32 bg-gray-200 rounded mb-4" />
        <div className="space-y-3">
          <div className="h-12 bg-gray-100 rounded" />
          <div className="h-12 bg-gray-100 rounded" />
        </div>
      </div>
    );
  }

  const hasSlaRisk = slaData && (slaData.critical_count > 0 || slaData.warning_count > 0);

  return (
    <div className="bg-white rounded-xl border border-gray-100 p-5">
      <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-4">
        Kitchen / SLA
      </h3>

      {/* SLA Status */}
      {hasSlaRisk ? (
        <div
          className={`rounded-lg px-4 py-3 mb-4 ${
            slaData!.critical_count > 0
              ? "bg-red-50 border border-red-100"
              : "bg-amber-50 border border-amber-100"
          }`}
        >
          <div className="flex items-center justify-between mb-1">
            <span
              className={`text-xs font-semibold uppercase tracking-wide ${
                slaData!.critical_count > 0 ? "text-red-600" : "text-amber-600"
              }`}
            >
              {slaData!.critical_count > 0 ? "SLA Breached" : "SLA Warning"}
            </span>
            <span
              className={`text-sm font-bold ${
                slaData!.critical_count > 0 ? "text-red-700" : "text-amber-700"
              }`}
            >
              {slaData!.worst_age_minutes.toFixed(1)} min
            </span>
          </div>
          <div className="flex gap-4 text-xs">
            {slaData!.critical_count > 0 && (
              <span className="text-red-600">
                <b>{slaData!.critical_count}</b> critical (&gt;10 min)
              </span>
            )}
            {slaData!.warning_count > 0 && (
              <span className="text-amber-600">
                <b>{slaData!.warning_count}</b> warning (&gt;7 min)
              </span>
            )}
          </div>
        </div>
      ) : (
        <div className="rounded-lg px-4 py-3 mb-4 bg-emerald-50 border border-emerald-100">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-emerald-700 uppercase tracking-wide">SLA OK</span>
            <span className="text-xs text-emerald-600">All orders on time</span>
          </div>
        </div>
      )}

      {/* Prep time stats */}
      {prep && prep.avg_prep_seconds !== null ? (
        <div className="space-y-3">
          <div>
            <div className="flex justify-between items-baseline mb-1">
              <span className="text-xs text-gray-500">Avg prep time</span>
              <span className="text-sm font-bold text-gray-900">{prep.avg_prep_display}</span>
            </div>
            <LoadBar
              value={prep.avg_prep_seconds}
              max={prep.slowest_seconds ?? prep.avg_prep_seconds}
              color="bg-blue-400"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="bg-gray-50 rounded-lg p-3">
              <div className="text-xs text-gray-400 mb-0.5">Fastest</div>
              <div className="text-sm font-semibold text-emerald-700">
                {prep.fastest_display}
              </div>
            </div>
            <div className="bg-gray-50 rounded-lg p-3">
              <div className="text-xs text-gray-400 mb-0.5">Slowest</div>
              <div className="text-sm font-semibold text-orange-600">
                {prep.slowest_display}
              </div>
            </div>
          </div>

          <p className="text-xs text-gray-400 text-center">{prep.total_tracked} orders tracked</p>
        </div>
      ) : (
        <p className="text-xs text-gray-400 text-center py-2">
          No prep time data yet.
        </p>
      )}
    </div>
  );
}
