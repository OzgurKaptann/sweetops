# SweetOps User Flows

## Customer (Ordering Flow)
1. **QR Scan**: Customer scans a table QR code and enters the `Customer Ordering App`. Information about Store ID and Table Number is stored in context.
2. **Menu Browse**: Customer selects "Custom Waffle".
3. **Customize**: Customer adds "Nutella" (base), "Strawberries", and "Banana". Rule engine validates no conflicting ingredients.
4. **Checkout & Realtime Tracking**: Order is confirmed. Customer is taken to a tracking view connected via WebSockets to see realtime status changes (`NEW`, `IN_PREP`, `READY`).

## Kitchen Staff / Waffle Master (Fulfillment Flow)
1. **Queue Management**: KDS screen displays a chronological queue of colored cards. New orders appear instantly without reloading.
2. **Acknowledge**: Waffle Master taps Table 5's new order. Status changes to `IN_PREP` and changes color to yellow.
3. **Assembly**: Waffle Master follows the large, readable ingredient breakdown on the card.
4. **Completion**: Taps "READY". Order flashes green and waitstaff collects it. Status sent to Customer.
5. **Clear**: Waitstaff or Master marks it as `DELIVERED`, clearing it from the active queue.

## Owner / Manager (Management Flow)
1. **Live View**: Store Owner opens Dashboard. Sees active orders and today's total revenue compared to yesterday.
2. **Analytics Breakdown**: Views the "Combinations" chart to discover "Nutella + Banana" is 18% of orders today.
3. **Forecasting Widget**: Checks tomorrow's forecast to see Strawberry demand is modeled to increase by 22% during afternoon operating hours.
4. **Adjustments**: Preemptively flags an ingredient as out of stock or adjusts pricing through the Admin API limits.
