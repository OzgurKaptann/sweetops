import AuthGate from "@/components/AuthGate";
import CashierPage from "@/components/CashierPage";

export default function Page() {
  return (
    <AuthGate>
      <CashierPage />
    </AuthGate>
  );
}
