"use client";

import { useSearchParams } from 'next/navigation';
import { Card } from '@sweetops/ui';
import Link from 'next/link';

export default function SuccessPage() {
  const searchParams = useSearchParams();
  const orderId = searchParams?.get('order_id') || 'Unknown';
  const amount = searchParams?.get('amount') || '0.00';

  return (
    <main className="max-w-md mx-auto min-h-screen bg-green-50 flex items-center justify-center p-4">
      <Card className="w-full p-8 text-center bg-white shadow-xl border-green-100">
        <div className="w-16 h-16 bg-green-100 text-green-600 rounded-full flex items-center justify-center mx-auto mb-4 text-3xl">
          ✓
        </div>
        <h1 className="text-2xl font-bold text-gray-900 mb-2">Order Confirmed!</h1>
        <p className="text-gray-600 mb-6">Your delicious waffle is being prepared.</p>
        
        <div className="bg-gray-50 rounded-lg p-4 mb-6">
          <div className="flex justify-between mb-2">
            <span className="text-gray-500">Order #</span>
            <span className="font-bold">{orderId}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-500">Amount Paid</span>
            <span className="font-bold">${amount}</span>
          </div>
        </div>

        <Link href="/" className="text-blue-600 font-medium hover:underline">
          Start a new order
        </Link>
      </Card>
    </main>
  );
}
