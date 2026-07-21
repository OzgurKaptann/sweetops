"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { fetchKitchenOrders, fetchKitchenTiming, updateOrderStatus } from "@/lib/api";
import { UnauthorizedError } from "@/lib/auth";
import AuthGate, { useAuth } from "@/components/AuthGate";
import { connectionStateLabel, orderStatusLabel } from "@/lib/labels";
import {
  ActiveTimingSummary,
  OrderTiming,
  delayReasonLabel,
  delayStateLabel,
  prepPhaseNote,
  timingLines,
} from "@/lib/timing";
import { KitchenOrder } from "@sweetops/types";

// Delay badge styling keyed by the API's delay_state enum (copy is Turkish).
const DELAY_BADGE_STYLE: Record<string, string> = {
  ok: "bg-gray-100 text-gray-500",
  warning: "bg-amber-100 text-amber-800",
  critical: "bg-red-100 text-red-800",
};

// ── Kitchen tempo (timing) summary strip ────────────────────────────────────
function KitchenTempoStrip({ summary }: { summary: ActiveTimingSummary | null }) {
  if (!summary) return null;
  const stat = (value: number, label: string, alert = false) => (
    <div className="flex flex-col items-center px-3">
      <span className={`text-lg font-bold ${alert ? "text-red-600" : "text-gray-900"}`}>
        {value}
      </span>
      <span className="text-[11px] text-gray-500">{label}</span>
    </div>
  );
  return (
    <div className="mb-6 bg-white rounded-lg shadow-sm border border-gray-200 p-4">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-gray-700">Mutfak temposu</span>
        <div className="flex items-center divide-x divide-gray-100">
          {stat(summary.active_orders, "Aktif sipariş")}
          {stat(summary.waiting_orders, "Bekleyen")}
          {stat(summary.in_prep_orders, "Hazırlanıyor")}
          {stat(summary.ready_orders, "Hazır")}
          {stat(summary.delayed_orders, "Geciken", summary.delayed_orders > 0)}
        </div>
      </div>
    </div>
  );
}

// ── Per-card timing block ───────────────────────────────────────────────────
function OrderTimingBlock({ timing }: { timing: OrderTiming | undefined }) {
  if (!timing) {
    return (
      <div className="mt-2 text-xs text-gray-400">Zamanlama bilgisi yok</div>
    );
  }
  const lines = timingLines(timing);
  return (
    <div className="mt-3 space-y-1">
      <div className="flex items-center gap-2">
        <span className="text-[11px] text-gray-500">{prepPhaseNote(timing)}</span>
        {timing.is_delayed ? (
          <span
            className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
              DELAY_BADGE_STYLE[timing.delay_state] ?? DELAY_BADGE_STYLE.ok
            }`}
          >
            {delayStateLabel(timing.delay_state)}
          </span>
        ) : (
          <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-gray-100 text-gray-500">
            {delayStateLabel(timing.delay_state)}
          </span>
        )}
      </div>
      {lines.map((l) => (
        <div key={l.label} className="flex justify-between text-xs">
          <span className="text-gray-500">{l.label}</span>
          <span className="font-medium text-gray-800 font-mono">{l.value}</span>
        </div>
      ))}
      {timing.is_delayed && timing.delay_reason && (
        <div className="text-[11px] text-amber-700">{delayReasonLabel(timing.delay_reason)}</div>
      )}
    </div>
  );
}

// Connection States
type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error';

export default function KitchenPage() {
  return (
    <AuthGate>
      <KitchenDashboard />
    </AuthGate>
  );
}

function KitchenDashboard() {
  const { user, logout, reportUnauthorized } = useAuth();
  const [orders, setOrders] = useState<KitchenOrder[]>([]);
  const [timingById, setTimingById] = useState<Record<number, OrderTiming>>({});
  const [tempo, setTempo] = useState<ActiveTimingSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected');
  const wsRef = useRef<WebSocket | null>(null);

  // Initial HTTP Fetch — orders + derived preparation timing, in parallel.
  const loadOrders = useCallback(async () => {
    try {
      const [data, timing] = await Promise.all([
        fetchKitchenOrders(),
        fetchKitchenTiming(),
      ]);
      setOrders(data);
      const byId: Record<number, OrderTiming> = {};
      for (const t of timing.orders) byId[t.order_id] = t;
      setTimingById(byId);
      setTempo(timing.summary);
      setError(false);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        reportUnauthorized();
        return;
      }
      console.error("Failed to fetch kitchen orders:", err);
      setError(true);
    } finally {
      setLoading(false);
    }
  }, [reportUnauthorized]);

  // Connect WebSocket
  useEffect(() => {
    // Ilk olarak HTTP ile yukle
    loadOrders();

    const connectWS = () => {
      setConnectionState('connecting');
      const ws = new WebSocket(process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000/ws/kitchen');
      
      ws.onopen = () => {
        setConnectionState('connected');
        console.log("WS Connected to Kitchen");
      };

      ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          
          if (payload.event === 'order_created') {
            console.log("Live Event: New Order", payload.data);
            // Strateji: Sessizce taze listeyi GET ile yeniden çek (Refetch)
            loadOrders();
          } 
          else if (payload.event === 'order_status_updated') {
            console.log("Live Event: Status Update", payload.data);
            const  { order_id, status } = payload.data;
            
            // Strateji: Local Patch yap
            setOrders(prevOrders => {
                // Eğer yeni konum READY, DELIVERED veya CANCELLED ise ekrandan düşür
                if (['READY', 'DELIVERED', 'CANCELLED'].includes(status)) {
                    return prevOrders.filter(o => o.id !== order_id);
                }
                
                // Yoksa mutate ederek ilerle (Örn: IN_PREP vs)
                return prevOrders.map(o => {
                    if (o.id === order_id) {
                        return { ...o, status: status };
                    }
                    return o;
                });
            });
          }
        } catch (err) {
            console.error("WS Parse Error:", err);
            // Hatali formattaki event ignore edilir, cokus engellenir.
        }
      };

      ws.onclose = () => {
        setConnectionState('disconnected');
        // İsteğe bağlı 5 saniyede bir basit reconnect eklenebilir
        setTimeout(connectWS, 5000); 
      };

      ws.onerror = () => {
        setConnectionState('error');
      };

      wsRef.current = ws;
    };

    connectWS();

    return () => {
      if (wsRef.current) {
         wsRef.current.close();
      }
    };
  }, [loadOrders]);


  const handleStatusChange = async (orderId: number, currentStatus: string) => {
    const nextStatus = currentStatus === "NEW" ? "IN_PREP" : "READY";
    
    // Note: WS should echo back and handle the update, but we can do an optimistic 
    // update here if we want instant feedback before the WS message arrives. 
    // To prove WS is working, we rely entirely on the WS broadcast for this phase, OR we just let the API call finish and wait for WS.
    try {
      await updateOrderStatus(orderId, nextStatus);
    } catch (err) {
        if (err instanceof UnauthorizedError) {
          reportUnauthorized();
          return;
        }
        alert("Sipariş durumu güncellenemedi. Lütfen tekrar deneyin.");
    }
  };

  const CONNECTION_BADGE_STYLE: Record<ConnectionState, string> = {
    connected:    "bg-green-100 text-green-800",
    connecting:   "bg-yellow-100 text-yellow-800 animate-pulse",
    error:        "bg-red-100 text-red-800",
    disconnected: "bg-gray-100 text-gray-800",
  };

  const getConnectionBadge = () => (
    <span
      className={`inline-flex items-center px-2 py-1 rounded text-xs font-medium ${CONNECTION_BADGE_STYLE[connectionState]}`}
    >
      ● {connectionStateLabel(connectionState)}
    </span>
  );

  if (loading) {
    return (
      <div className="p-8">
        <p className="text-sm text-gray-500 mb-4">Siparişler yükleniyor…</p>
        <div className="animate-pulse flex space-x-4">
          <div className="h-32 bg-gray-200 rounded w-full"></div>
        </div>
      </div>
    );
  }

  return (
    <main className="min-h-screen bg-gray-100 p-8">
      <header className="mb-8 flex justify-between items-center bg-white p-4 rounded-lg shadow-sm">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">🧇 Mutfak Ekranı</h1>
          <p className="text-gray-500 mt-1">Canlı sipariş takibi</p>
        </div>
        <div className="flex items-center gap-4">
            {getConnectionBadge()}
            {connectionState !== 'connected' && (
                <button
                  onClick={loadOrders}
                  className="px-3 py-1 bg-blue-50 text-blue-600 rounded text-sm hover:bg-blue-100 transition-colors"
                >
                  Yenile

                </button>
            )}
            {user && (
              <span className="text-sm text-gray-500 hidden sm:inline">
                {user.username}
              </span>
            )}
            <button
              onClick={logout}
              className="px-3 py-1 bg-gray-100 text-gray-700 rounded text-sm hover:bg-gray-200 transition-colors"
            >
              Çıkış Yap
            </button>
        </div>
      </header>

      {error && (
        <div className="mb-6 px-4 py-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700">
          Siparişler yüklenemedi. Bağlantı yeniden kuruluyor…
        </div>
      )}

      <KitchenTempoStrip summary={tempo} />

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
        {orders.map((order) => (
          <div key={order.id} className="bg-white rounded-lg shadow border border-gray-200 overflow-hidden flex flex-col">
            <div className={`p-4 border-b ${order.status === 'NEW' ? 'bg-amber-50 border-amber-200' : 'bg-blue-50 border-blue-200'}`}>
              <div className="flex justify-between items-start mb-2">
                <div>
                  <span className="text-xl font-bold font-mono">#{order.id}</span>
                  <div className="text-sm font-medium text-gray-500">Masa {order.table_id}</div>
                </div>
                <span className={`px-3 py-1 rounded-full text-xs font-bold ${
                  order.status === 'NEW' ? 'bg-amber-100 text-amber-800' : 'bg-blue-100 text-blue-800'
                }`}>
                  {orderStatusLabel(order.status)}
                </span>
              </div>
              <div className="text-xs text-gray-400 mt-2">
                Sipariş saati: {new Date(order.created_at).toLocaleTimeString('tr-TR')}
              </div>
              <OrderTimingBlock timing={timingById[order.id]} />
            </div>

            <div className="p-4 flex-grow">
              <ul className="space-y-4">
                {order.items.map((item) => (
                  <li key={item.id} className="text-sm">
                    <div className="font-medium text-gray-900 flex justify-between">
                      <span>{item.quantity}x {item.product_name}</span>
                    </div>
                    {item.ingredients.length > 0 && (
                      <ul className="mt-1 ml-4 text-xs text-gray-500 list-disc list-inside">
                        {item.ingredients.map(ing => (
                          <li key={ing.id}>{ing.quantity}x {ing.ingredient_name}</li>
                        ))}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            </div>

            <div className="p-4 bg-gray-50 border-t border-gray-200">
              <button
                onClick={() => handleStatusChange(order.id, order.status)}
                className={`w-full py-3 rounded-lg font-bold text-sm shadow-sm transition-all focus:outline-none focus:ring-2 focus:ring-offset-2 ${
                  order.status === 'NEW' 
                    ? 'bg-amber-500 hover:bg-amber-600 text-white focus:ring-amber-500' 
                    : 'bg-green-500 hover:bg-green-600 text-white focus:ring-green-500'
                }`}
              >
                {order.status === 'NEW' ? 'Hazırlamaya başla' : 'Hazır ✓'}
              </button>
            </div>
          </div>
        ))}

        {orders.length === 0 && !loading && (
          <div className="col-span-full py-16 text-center border-2 border-dashed border-gray-300 rounded-lg bg-white">
            <div className="text-4xl mb-4">🧇</div>
            <h3 className="text-lg font-medium text-gray-900 mb-1">Yeni sipariş yok</h3>
            <p className="text-gray-500">Şu anda hazırlanacak sipariş bulunmuyor.</p>
          </div>
        )}
      </div>
    </main>
  );
}
