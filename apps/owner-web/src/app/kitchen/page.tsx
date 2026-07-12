"use client";

import { useState, useEffect, useRef, useCallback } from "react";
import {
  fetchKitchenOrders,
  patchOrderStatus,
  KitchenDashboardResponse,
  KitchenOrder,
  KitchenLoad,
  BatchingSuggestion,
  OrderStatus,
} from "@/lib/api";
import { loadLevelLabel, orderStatusLabel } from "@/lib/labels";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/ws/kitchen";
const POLL_MS = 15_000;

// ── Flow status ───────────────────────────────────────────────────────────────

type FlowStatus = "smooth" | "stressed" | "critical";

function computeFlowStatus(data: KitchenDashboardResponse | null): FlowStatus {
  if (!data) return "smooth";
  const { kitchen_load, orders } = data;
  const hasCritical = orders.some((o) => o.sla_severity === "critical");
  const hasWarning  = orders.some((o) => o.sla_severity === "warning");
  if (hasCritical || kitchen_load.load_level === "high") return "critical";
  if (hasWarning  || kitchen_load.load_level === "medium") return "stressed";
  return "smooth";
}

const FLOW_STYLE: Record<FlowStatus, { bg: string; text: string; message: string }> = {
  smooth:   { bg: "bg-emerald-600", text: "text-white", message: "Mutfak akışı düzgün" },
  stressed: { bg: "bg-amber-500",   text: "text-white", message: "Mutfak yoğun — hazırlık sürelerine dikkat" },
  critical: { bg: "bg-red-600",     text: "text-white", message: "Mutfak kritik — önce hızı önceliklendirin" },
};

function FlowStatusBanner({ status }: { status: FlowStatus }) {
  const cfg = FLOW_STYLE[status];
  if (status === "smooth") return null; // smooth = no banner needed
  return (
    <div className={`w-full px-6 py-2 ${cfg.bg} ${cfg.text}`}>
      <p className="text-xs font-bold text-center tracking-wide uppercase">
        {cfg.message}
      </p>
    </div>
  );
}

// ── Types ─────────────────────────────────────────────────────────────────────

// Next logical status for each current status
const NEXT_STATUS: Partial<Record<OrderStatus, OrderStatus>> = {
  NEW: "IN_PREP",
  IN_PREP: "READY",
  READY: "DELIVERED",
};

// Keyed by the API's order-status enum; the button text is presentation only.
const ACTION_LABEL: Partial<Record<OrderStatus, string>> = {
  NEW: "Hazırlamaya başla",
  IN_PREP: "Hazır olarak işaretle",
  READY: "Teslim edildi olarak işaretle",
};

// ── Helpers ───────────────────────────────────────────────────────────────────

// `sla_severity` is an API enum (ok/warning/critical); only the badge is copy.
const SLA_STYLE = {
  critical: {
    border: "border-l-red-500",
    badge: "bg-red-100 text-red-700",
    label: "SÜRE AŞILDI",
    ageBg: "bg-red-50 text-red-700",
  },
  warning: {
    border: "border-l-amber-400",
    badge: "bg-amber-100 text-amber-700",
    label: "SÜRE DOLUYOR",
    ageBg: "bg-amber-50 text-amber-700",
  },
  ok: {
    border: "border-l-gray-200",
    badge: "bg-gray-100 text-gray-500",
    label: "ZAMANINDA",
    ageBg: "bg-gray-50 text-gray-500",
  },
};

const LOAD_STYLE = {
  low: "bg-emerald-50 text-emerald-700",
  medium: "bg-amber-50 text-amber-700",
  high: "bg-red-50 text-red-700",
};

function formatAge(minutes: number): string {
  if (minutes < 1) return "1 dk'dan az";
  return `${minutes.toFixed(1)} dk`;
}

// ── Kitchen Load Bar ──────────────────────────────────────────────────────────

function KitchenLoadHeader({
  load,
  onRefresh,
}: {
  load: KitchenLoad | null;
  onRefresh: () => void;
}) {
  return (
    <header className="bg-white border-b border-gray-200 sticky top-0 z-20">
      <div className="max-w-screen-xl mx-auto px-6">
        <div className="flex items-center justify-between h-14">
          <div className="flex items-center gap-3">
            <span className="text-sm font-bold text-gray-900">Mutfak</span>
            {load && (
              <>
                <span className="text-gray-200">|</span>
                <span
                  className={`text-xs font-semibold px-2.5 py-1 rounded-full ${
                    LOAD_STYLE[load.load_level]
                  }`}
                >
                  {loadLevelLabel(load.load_level)}
                </span>
              </>
            )}
          </div>

          <div className="flex items-center gap-4">
            {load && (
              <div className="hidden sm:flex items-center gap-5 text-xs text-gray-500">
                <span>
                  <b className="text-gray-900">{load.active_orders_count}</b> açık sipariş
                </span>
                <span>
                  <b className="text-gray-900">{load.in_prep_count}</b> hazırlanıyor
                </span>
                <span>
                  Ort. bekleme:{" "}
                  <b className={load.average_age_minutes > 8 ? "text-red-600" : "text-gray-900"}>
                    {load.average_age_minutes.toFixed(1)} dk
                  </b>
                </span>
              </div>
            )}
            <a
              href="/owner-web"
              className="text-xs text-gray-400 hover:text-gray-600 transition-colors"
            >
              ← Panel
            </a>
            <button
              onClick={onRefresh}
              className="text-xs px-3 py-1.5 rounded-lg bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors font-medium"
            >
              ↻
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}

// ── Batching Banner ───────────────────────────────────────────────────────────

function BatchingBanner({ suggestions }: { suggestions: BatchingSuggestion[] }) {
  if (suggestions.length === 0) return null;

  return (
    <div className="bg-blue-50 border-b border-blue-100 px-6 py-2">
      <div className="max-w-screen-xl mx-auto flex flex-wrap items-center gap-3">
        <span className="text-xs font-semibold text-blue-700">Birlikte hazırlama fırsatı:</span>
        {suggestions.map((s, i) => (
          <span key={i} className="text-xs text-blue-600 bg-blue-100 px-2 py-0.5 rounded">
            #{s.grouped_order_ids.join(" + #")} siparişlerinde ortak:{" "}
            <b>{s.shared_ingredients.join(", ")}</b> — {s.estimated_time_saved} kazanç
          </span>
        ))}
      </div>
    </div>
  );
}

// ── Single order card ─────────────────────────────────────────────────────────

interface CardProps {
  order: KitchenOrder;
  onAction: (orderId: number, status: OrderStatus) => Promise<void>;
}

function OrderCard({ order, onAction }: CardProps) {
  const [acting, setActing] = useState(false);
  const sla = SLA_STYLE[order.sla_severity];
  const nextStatus = NEXT_STATUS[order.status as OrderStatus];
  const actionLabel = ACTION_LABEL[order.status as OrderStatus];

  const handleAction = async () => {
    if (!nextStatus) return;
    setActing(true);
    try {
      await onAction(order.id, nextStatus);
    } finally {
      setActing(false);
    }
  };

  // Ingredient names (deduplicated across items)
  const ingredientNames = Array.from(
    new Set(
      order.items.flatMap((item) =>
        item.ingredients.map((i) => i.ingredient_name ?? `#${i.ingredient_id}`),
      ),
    ),
  );

  return (
    <div
      className={`bg-white rounded-xl border border-gray-100 border-l-4 ${sla.border} p-4 flex flex-col gap-3`}
    >
      {/* Top row */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm font-bold text-gray-900">#{order.id}</span>
          {order.table_id && (
            <span className="text-xs text-gray-400">T{order.table_id}</span>
          )}
          <span
            className={`text-[10px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wide ${sla.badge}`}
          >
            {sla.label}
          </span>
          {order.should_be_started && (
            <span className="text-[10px] font-bold px-1.5 py-0.5 rounded uppercase tracking-wide bg-red-600 text-white animate-pulse">
              HEMEN BAŞLA
            </span>
          )}
        </div>

        <div
          className={`shrink-0 text-xs font-bold px-2 py-1 rounded-lg ${sla.ageBg}`}
        >
          {formatAge(order.computed_age_minutes)}
        </div>
      </div>

      {/* Status */}
      <div className="flex items-center gap-2">
        <span
          className={`text-xs font-semibold px-2 py-0.5 rounded ${
            order.status === "IN_PREP"
              ? "bg-blue-50 text-blue-700"
              : order.status === "READY"
              ? "bg-emerald-50 text-emerald-700"
              : "bg-gray-100 text-gray-600"
          }`}
        >
          {orderStatusLabel(order.status)}
        </span>
        {order.urgency_reason && (
          <span className="text-xs text-gray-400 truncate">{order.urgency_reason}</span>
        )}
      </div>

      {/* Ingredients */}
      <div className="flex flex-wrap gap-1">
        {ingredientNames.map((name) => (
          <span
            key={name}
            className="text-xs bg-gray-50 text-gray-700 px-2 py-0.5 rounded"
          >
            {name}
          </span>
        ))}
      </div>

      {/* Action hint */}
      {order.action_hint && (
        <p className="text-xs text-gray-500 italic">{order.action_hint}</p>
      )}

      {/* Primary action */}
      {nextStatus && actionLabel && (
        <button
          onClick={handleAction}
          disabled={acting}
          className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors disabled:opacity-50 ${
            order.sla_severity === "critical"
              ? "bg-red-600 text-white hover:bg-red-700"
              : order.sla_severity === "warning"
              ? "bg-amber-500 text-white hover:bg-amber-600"
              : "bg-gray-900 text-white hover:bg-gray-800"
          }`}
        >
          {acting ? "…" : `→ ${actionLabel}`}
        </button>
      )}
    </div>
  );
}

// ── Section ───────────────────────────────────────────────────────────────────

function OrderSection({
  title,
  orders,
  accent,
  onAction,
}: {
  title: string;
  orders: KitchenOrder[];
  accent?: "alert" | "neutral";
  onAction: (id: number, status: OrderStatus) => Promise<void>;
}) {
  if (orders.length === 0) return null;

  const barColor = accent === "alert" ? "bg-red-500" : "bg-gray-300";

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <div className={`w-1 h-4 rounded-full ${barColor}`} />
        <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
          {title}
        </h2>
        <span className="text-xs text-gray-400">({orders.length})</span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
        {orders.map((o) => (
          <OrderCard key={o.id} order={o} onAction={onAction} />
        ))}
      </div>
    </div>
  );
}

// ── Empty state ───────────────────────────────────────────────────────────────

function EmptyKitchen() {
  return (
    <div className="flex flex-col items-center justify-center py-24 text-center">
      <div className="text-4xl mb-3">✓</div>
      <p className="text-sm font-semibold text-gray-700">Yeni sipariş yok</p>
      <p className="text-xs text-gray-400 mt-1">Şu anda hazırlanacak sipariş bulunmuyor.</p>
    </div>
  );
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function KitchenSkeleton() {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
      {[...Array(4)].map((_, i) => (
        <div
          key={i}
          className="bg-white rounded-xl border border-gray-100 border-l-4 border-l-gray-200 p-4 animate-pulse"
        >
          <div className="h-4 bg-gray-200 rounded w-1/2 mb-3" />
          <div className="h-3 bg-gray-100 rounded w-3/4 mb-2" />
          <div className="flex gap-1 mb-3">
            {[...Array(3)].map((_, j) => (
              <div key={j} className="h-5 w-14 bg-gray-100 rounded" />
            ))}
          </div>
          <div className="h-9 bg-gray-200 rounded-lg" />
        </div>
      ))}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function KitchenPage() {
  const [data, setData] = useState<KitchenDashboardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const load = useCallback(() => {
    fetchKitchenOrders()
      .then((d) => {
        setData(d);
        setError(false);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);

    const connect = () => {
      const ws = new WebSocket(WS_URL);
      ws.onmessage = (e) => {
        try {
          const payload = JSON.parse(e.data);
          if (
            payload.event === "order_created" ||
            payload.event === "order_status_updated"
          ) {
            load();
          }
        } catch {}
      };
      ws.onclose = () => setTimeout(connect, 5000);
      wsRef.current = ws;
    };
    connect();

    return () => {
      clearInterval(timer);
      wsRef.current?.close();
    };
  }, [load]);

  const handleAction = useCallback(
    async (orderId: number, status: OrderStatus) => {
      await patchOrderStatus(orderId, status);
      load();
    },
    [load],
  );

  const kitchenLoad = data?.kitchen_load ?? null;
  const batching = data?.batching_suggestions ?? [];
  const orders = data?.orders ?? [];

  // Split into sections
  const urgentOrders = orders.filter(
    (o) =>
      (o.sla_severity === "critical" || o.sla_severity === "warning") &&
      o.status !== "DELIVERED" &&
      o.status !== "CANCELLED",
  );
  const newOrders = orders.filter(
    (o) =>
      o.status === "NEW" &&
      o.sla_severity === "ok",
  );
  const inPrepOrders = orders.filter((o) => o.status === "IN_PREP" && o.sla_severity === "ok");
  const readyOrders = orders.filter((o) => o.status === "READY");

  return (
    <div className="min-h-screen bg-[#f8f9fa]">
      <KitchenLoadHeader load={kitchenLoad} onRefresh={load} />
      <FlowStatusBanner status={computeFlowStatus(data)} />
      <BatchingBanner suggestions={batching} />

      <main className="max-w-screen-xl mx-auto px-6 py-6 space-y-8">
        {loading && <KitchenSkeleton />}

        {error && !loading && (
          <div className="p-4 text-sm text-red-600 bg-red-50 rounded-xl border border-red-100">
            Mutfak verileri yüklenemedi. Bağlantı yeniden kuruluyor…
          </div>
        )}

        {!loading && !error && orders.length === 0 && <EmptyKitchen />}

        {!loading && !error && orders.length > 0 && (
          <>
            <OrderSection
              title="Acil — süre riski var"
              orders={urgentOrders}
              accent="alert"
              onAction={handleAction}
            />
            <OrderSection
              title="Teslime hazır"
              orders={readyOrders}
              accent="neutral"
              onAction={handleAction}
            />
            <OrderSection
              title="Hazırlanıyor"
              orders={inPrepOrders}
              accent="neutral"
              onAction={handleAction}
            />
            <OrderSection
              title="Başlamayı bekliyor"
              orders={newOrders}
              accent="neutral"
              onAction={handleAction}
            />
          </>
        )}
      </main>
    </div>
  );
}
