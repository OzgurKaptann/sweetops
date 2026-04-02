import { KPICardGrid } from "@/components/KPICardGrid";
import { TopIngredientsPanel } from "@/components/TopIngredientsPanel";
import { HourlyDemandChart } from "@/components/HourlyDemandChart";
import { IngredientForecastPanel } from "@/components/IngredientForecastPanel";
import { StockWarningsPanel } from "@/components/StockWarningsPanel";
import { CriticalAlertsPanel } from "@/components/CriticalAlertsPanel";
import { PrepTimePanel } from "@/components/PrepTimePanel";
import { TrendingIngredientsPanel } from "@/components/TrendingIngredientsPanel";
import { PopularCombosPanel } from "@/components/PopularCombosPanel";
import { ValueSummaryPanel } from "@/components/ValueSummaryPanel";

export default function OwnerDashboard() {
  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16 items-center">
            <h1 className="text-xl font-bold text-gray-900">🧇 SweetOps <span className="text-amber-600">Panel</span></h1>
            <div className="text-sm text-gray-500">İşletme Paneli • Canlı</div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">

        {/* Value Summary — THE money shot, top of page */}
        <ValueSummaryPanel />

        <div className="mt-8 mb-6 flex items-center justify-between">
          <h2 className="text-2xl font-bold text-gray-900">İşletme Özeti</h2>
        </div>

        {/* KPI Cards */}
        <KPICardGrid />

        {/* Critical alerts — urgency */}
        <div className="mt-6">
          <CriticalAlertsPanel />
        </div>

        {/* Operational Insights Row */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
          <PrepTimePanel />
          <StockWarningsPanel />
        </div>

        {/* Intelligence Row */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
          <TrendingIngredientsPanel />
          <PopularCombosPanel />
        </div>

        {/* Charts & Lists Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mt-6">
          <div className="lg:col-span-2">
            <HourlyDemandChart />
            <div className="mt-6">
              <IngredientForecastPanel />
            </div>
          </div>
          <div>
            <TopIngredientsPanel />
          </div>
        </div>

      </div>
    </main>
  );
}
