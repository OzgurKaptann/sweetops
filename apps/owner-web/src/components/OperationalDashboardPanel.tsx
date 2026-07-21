"use client";

import { useEffect, useState } from "react";

import { fetchOperationalDashboard } from "@/lib/operational-dashboard-api";
import {
  DASHBOARD_COPY,
  formatBusinessDate,
  formatCount,
  formatDuration,
  formatMoney,
  severityTone,
  toAttentionRows,
  type OperationalDashboard,
} from "@/lib/operational-dashboard-view";

const C = DASHBOARD_COPY;

// ── Small building blocks ─────────────────────────────────────────────────────

function Card({
  title,
  route,
  children,
}: {
  title: string;
  route?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-white rounded-lg shadow p-5 flex flex-col">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        {route && (
          <a
            href={route}
            className="text-xs text-blue-600 font-medium hover:underline shrink-0"
          >
            {C.detailLink} →
          </a>
        )}
      </div>
      <div className="flex-1">{children}</div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between py-1">
      <span className="text-xs text-gray-500">{label}</span>
      <span className="text-sm font-semibold text-gray-900 tabular-nums">{value}</span>
    </div>
  );
}

function BigMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="mb-2">
      <div className="text-2xl font-bold text-gray-900 tabular-nums">{value}</div>
      <div className="text-xs text-gray-500">{label}</div>
    </div>
  );
}

// ── Panel ─────────────────────────────────────────────────────────────────────

export function OperationalDashboardPanel({ refreshTick }: { refreshTick?: number }) {
  const [data, setData] = useState<OperationalDashboard | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    setError(false);
    fetchOperationalDashboard()
      .then((d) => {
        if (alive) setData(d);
      })
      .catch(() => {
        if (alive) setError(true);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [refreshTick]);

  if (loading) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="bg-white rounded-lg shadow p-5">
            <div className="animate-pulse space-y-3">
              <div className="h-4 bg-gray-100 rounded w-1/2" />
              <div className="h-8 bg-gray-100 rounded w-3/4" />
              <div className="h-4 bg-gray-100 rounded w-full" />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-100 rounded-lg p-5 text-sm text-red-700">
        {C.loadError}
      </div>
    );
  }

  if (!data) {
    return (
      <div className="bg-white rounded-lg shadow p-5 text-sm text-gray-500">{C.empty}</div>
    );
  }

  const { orders, payments, kitchen, issues, shifts, inventory } = data;
  const attention = toAttentionRows(data.attention);

  return (
    <div className="space-y-4">
      {formatBusinessDate(data.business_date) && (
        <p className="text-xs text-gray-400">{formatBusinessDate(data.business_date)}</p>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {/* Günlük ciro */}
        <Card title={C.cards.payments}>
          <BigMetric label={C.labels.net} value={formatMoney(payments.net_collected_today)} />
          <Metric label={C.labels.gross} value={formatMoney(payments.gross_collected_today)} />
          <Metric label={C.labels.refunds} value={formatMoney(payments.refunds_today)} />
          <Metric
            label={C.labels.unpaid}
            value={formatCount(payments.unpaid_or_partially_paid_orders)}
          />
        </Card>

        {/* Aktif sipariş */}
        <Card title={C.cards.orders} route="/kitchen">
          <BigMetric label={C.labels.activeOrders} value={formatCount(orders.active_count)} />
          <Metric label={C.labels.waiting} value={formatCount(orders.waiting_count)} />
          <Metric label={C.labels.inPrep} value={formatCount(orders.in_prep_count)} />
          <Metric label={C.labels.ready} value={formatCount(orders.ready_count)} />
          <Metric label={C.labels.completedToday} value={formatCount(orders.completed_today)} />
          <Metric label={C.labels.cancelledToday} value={formatCount(orders.cancelled_today)} />
        </Card>

        {/* Mutfak temposu */}
        <Card title={C.cards.kitchen} route="/kitchen">
          <BigMetric label={C.labels.delayed} value={formatCount(kitchen.delayed_orders)} />
          <Metric label={C.labels.activeOrders} value={formatCount(kitchen.active_orders)} />
          <Metric
            label={C.labels.avgPrep}
            value={formatDuration(kitchen.average_prep_seconds_today)}
          />
        </Card>

        {/* Açık sorunlu sipariş */}
        <Card title={C.cards.issues} route="/order-issues">
          <BigMetric label={C.labels.openIssues} value={formatCount(issues.open_count)} />
          <Metric label={C.labels.resolvedToday} value={formatCount(issues.resolved_today)} />
          <Metric label={C.labels.issueRefund} value={formatMoney(issues.refund_amount_today)} />
        </Card>

        {/* Kasa vardiyaları */}
        <Card title={C.cards.shifts} route="/shifts">
          <BigMetric label={C.labels.openShifts} value={formatCount(shifts.open_shift_count)} />
          <Metric label={C.labels.closedToday} value={formatCount(shifts.closed_today)} />
          <Metric label={C.labels.discrepancy} value={formatMoney(shifts.total_discrepancy_today)} />
          <Metric
            label={C.labels.shiftsWithDiscrepancy}
            value={formatCount(shifts.shifts_with_discrepancy_today)}
          />
        </Card>

        {/* Kritik stok */}
        <Card title={C.cards.inventory} route="/inventory">
          <BigMetric
            label={C.labels.critical}
            value={formatCount(inventory.critical_count)}
          />
          <Metric label={C.labels.outOfStock} value={formatCount(inventory.out_of_stock_count)} />
          <Metric label={C.labels.low} value={formatCount(inventory.low_count)} />
          <Metric label={C.labels.healthy} value={formatCount(inventory.healthy_count)} />
        </Card>
      </div>

      {/* Dikkat gerektirenler */}
      <div className="bg-white rounded-lg shadow p-5">
        <h3 className="text-sm font-semibold text-gray-900 mb-3">{C.cards.attention}</h3>
        {attention.length === 0 ? (
          <p className="text-sm text-gray-500">{C.noAttention}</p>
        ) : (
          <ul className="space-y-2">
            {attention.map((a, idx) => {
              const tone = severityTone(a.severity);
              return (
                <li
                  key={`${a.title}-${idx}`}
                  className={`flex items-center justify-between gap-3 p-3 rounded-lg ${tone.bg}`}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <span className={`w-2 h-2 rounded-full shrink-0 ${tone.dot}`} />
                    <div className="min-w-0">
                      <div className={`text-sm font-semibold ${tone.text}`}>{a.title}</div>
                      <div className="text-xs text-gray-500 truncate">{a.description}</div>
                    </div>
                  </div>
                  {a.targetRoute && (
                    <a
                      href={a.targetRoute}
                      className="text-xs text-blue-600 font-medium hover:underline shrink-0"
                    >
                      {C.detailLink} →
                    </a>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
