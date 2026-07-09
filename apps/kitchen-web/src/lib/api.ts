import { KitchenOrder } from '@sweetops/types';
import { API_BASE, csrfHeaders, UnauthorizedError } from './auth';

// The store is derived from the authenticated session server-side; the client
// never sends a store_id. All requests include credentials so the session
// cookie is attached; mutations attach the CSRF token.

export async function fetchKitchenOrders(): Promise<KitchenOrder[]> {
  const res = await fetch(`${API_BASE}/kitchen/orders/`, {
    credentials: 'include',
    cache: 'no-store',
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error('Failed to fetch kitchen orders');
  const data = await res.json();
  // Backend returns a dashboard object { orders, kitchen_load, batching_suggestions }.
  return Array.isArray(data) ? data : data.orders;
}

export async function updateOrderStatus(orderId: number, status: string): Promise<unknown> {
  const res = await fetch(`${API_BASE}/kitchen/orders/${orderId}/status`, {
    method: 'PATCH',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
    body: JSON.stringify({ status }),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error('Failed to update order status');
  return res.json();
}
