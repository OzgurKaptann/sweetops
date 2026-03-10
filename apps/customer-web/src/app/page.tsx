"use client";

import { useState, useEffect } from "react";
import { fetchMenu, createOrder } from "@/lib/api";
import { Product, Ingredient, OrderCreateRequest } from "@sweetops/types";
import { Card, Button } from "@sweetops/ui";
import { useRouter } from "next/navigation";

export default function Home() {
  const router = useRouter();
  const [products, setProducts] = useState<Product[]>([]);
  const [ingredients, setIngredients] = useState<Ingredient[]>([]);
  
  const [selectedProduct, setSelectedProduct] = useState<Product | null>(null);
  const [selectedIngredients, setSelectedIngredients] = useState<Record<number, boolean>>({});
  const [loading, setLoading] = useState(false);
  
  useEffect(() => {
    fetchMenu().then((data) => {
      setProducts(data.products);
      setIngredients(data.ingredients);
    });
  }, []);

  const toggleIngredient = (id: number) => {
    setSelectedIngredients(prev => ({ ...prev, [id]: !prev[id] }));
  };

  const handleOrder = async () => {
    if (!selectedProduct) return;
    setLoading(true);
    
    // Convert selected bool map to array of OrderItemIngredientCreate
    const ingredientList = Object.entries(selectedIngredients)
      .filter(([_, isSelected]) => isSelected)
      .map(([id]) => ({ ingredient_id: Number(id), quantity: 1 }));

    const payload: OrderCreateRequest = {
      store_id: 1, // hardcoded for MVP context
      table_id: 1,
      items: [
        {
          product_id: selectedProduct.id,
          quantity: 1,
          ingredients: ingredientList
        }
      ]
    };

    try {
      const resp = await createOrder(payload);
      router.push(`/success?order_id=${resp.order_id}&amount=${resp.total_amount}`);
    } catch (e) {
      console.error(e);
      alert("Order failed!");
    } finally {
      setLoading(false);
    }
  };

  // Calculate Subtotal dynamically
  const subtotal = selectedProduct 
    ? parseFloat(selectedProduct.base_price) + 
      Object.entries(selectedIngredients)
        .filter(([_, s]) => s)
        .reduce((sum, [id]) => {
          const ing = ingredients.find(i => i.id === Number(id));
          return sum + (ing ? parseFloat(ing.price) : 0);
        }, 0)
    : 0;

  return (
    <main className="max-w-md mx-auto min-h-screen bg-gray-50 pb-24">
      <header className="bg-white px-4 py-6 shadow-sm mb-6 rounded-b-[2rem]">
         <h1 className="text-2xl font-bold text-gray-900">Delicious Waffles</h1>
         <p className="text-gray-500 text-sm">Customize your perfect dessert</p>
      </header>

      <div className="px-4 space-y-6">
        <section>
          <h2 className="text-lg font-semibold mb-3">1. Choose your Base</h2>
          <div className="flex gap-4 overflow-x-auto pb-2 snap-x">
            {products.map(p => (
              <Card 
                key={p.id} 
                className={`snap-center min-w-[200px] cursor-pointer transition-all ${selectedProduct?.id === p.id ? 'ring-2 ring-blue-500 bg-blue-50' : ''}`}
              >
                <div onClick={() => setSelectedProduct(p)} className="p-4">
                  <h3 className="font-medium text-gray-900">{p.name}</h3>
                  <p className="text-blue-600 font-semibold mt-1">${p.base_price}</p>
                </div>
              </Card>
            ))}
          </div>
        </section>

        {selectedProduct && (
          <section>
            <h2 className="text-lg font-semibold mb-3">2. Add Ingredients</h2>
            <div className="flex flex-wrap gap-2">
              {ingredients.map(ing => {
                const isSelected = selectedIngredients[ing.id];
                return (
                  <button
                    key={ing.id}
                    onClick={() => toggleIngredient(ing.id)}
                    className={`px-3 py-1.5 rounded-full text-sm font-medium border transition-colors ${
                      isSelected 
                        ? 'bg-blue-600 text-white border-blue-600' 
                        : 'bg-white text-gray-700 border-gray-200 hover:border-blue-300'
                    }`}
                  >
                    {ing.name} <span className="opacity-75 text-xs ml-1">+${ing.price}</span>
                  </button>
                )
              })}
            </div>
          </section>
        )}
      </div>

      {selectedProduct && (
        <div className="fixed bottom-0 left-0 right-0 max-w-md mx-auto bg-white border-t p-4 shadow-[0_-10px_20px_rgba(0,0,0,0.05)]">
          <div className="flex justify-between items-center mb-3">
             <span className="text-gray-600 font-medium">Total:</span>
             <span className="text-xl font-bold">${subtotal.toFixed(2)}</span>
          </div>
          <Button 
            className="w-full py-3 h-auto text-lg rounded-xl" 
            onClick={handleOrder}
            disabled={loading}
          >
            {loading ? 'Processing...' : 'Place Order'}
          </Button>
        </div>
      )}
    </main>
  );
}
