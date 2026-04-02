import { KitchenOrder } from '@sweetops/types';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

export async function fetchKitchenOrders(storeId: number = 1): Promise<KitchenOrder[]> {
  const res = await fetch(`${API_BASE}/kitchen/orders/?store_id=${storeId}`, { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch kitchen orders');
  return res.json();
}

export async function updateOrderStatus(orderId: number, status: string): Promise<any> {
  const res = await fetch(`${API_BASE}/kitchen/orders/${orderId}/status`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error('Failed to update order status');
  return res.json();
}
