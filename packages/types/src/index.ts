export interface Product {
  id: number;
  name: string;
  category: string;
  base_price: string;
}

export interface Ingredient {
  id: number;
  name: string;
  category: string;
  price: string;
}

export interface OrderItemIngredientCreate {
  ingredient_id: number;
  quantity: number;
}

export interface OrderItemCreate {
  product_id: number;
  quantity: number;
  ingredients: OrderItemIngredientCreate[];
}

export interface OrderCreateRequest {
  store_id: number;
  table_id?: number;
  items: OrderItemCreate[];
}

export interface OrderCreatedResponse {
  order_id: number;
  status: string;
  created_at: string;
  item_count: number;
  total_amount: string;
}

export interface OrderItemIngredientResponse {
  id: number;
  ingredient_id: number;
  ingredient_name: string;
  quantity: number;
}

export interface OrderItemResponse {
  id: number;
  product_id: number;
  product_name: string;
  quantity: number;
  ingredients: OrderItemIngredientResponse[];
}

export interface KitchenOrder {
  id: number;
  store_id: number;
  table_id?: number;
  status: string;
  created_at: string;
  items: OrderItemResponse[];
}

export type OrderStatus = 'NEW' | 'IN_PREP' | 'READY' | 'DELIVERED' | 'CANCELLED';
