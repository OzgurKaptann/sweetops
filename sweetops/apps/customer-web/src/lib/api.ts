import { OrderCreateRequest, OrderCreatedResponse, MenuResponse } from '@sweetops/types';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

export async function fetchMenu(): Promise<MenuResponse> {
  const res = await fetch(`${API_BASE}/public/menu`, { cache: 'no-store' });
  if (!res.ok) throw new Error('Menü yüklenemedi');
  return res.json();
}

export async function createOrder(payload: OrderCreateRequest): Promise<OrderCreatedResponse> {
  const res = await fetch(`${API_BASE}/public/orders/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error('Sipariş oluşturulamadı');
  return res.json();
}
