import { OrderCreateRequest, OrderCreatedResponse, Product, Ingredient } from '@sweetops/types';

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8000';

export async function fetchMenu(): Promise<{ products: Product[]; ingredients: Ingredient[] }> {
  const res = await fetch(`${API_BASE}/public/menu`, { cache: 'no-store' });
  if (!res.ok) throw new Error('Failed to fetch menu');
  return res.json();
}

export async function createOrder(payload: OrderCreateRequest): Promise<OrderCreatedResponse> {
  const res = await fetch(`${API_BASE}/public/orders/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error('Failed to create order');
  return res.json();
}
