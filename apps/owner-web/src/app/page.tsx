import { KPICardGrid } from "@/components/KPICardGrid";
import { TopIngredientsPanel } from "@/components/TopIngredientsPanel";
import { HourlyDemandChart } from "@/components/HourlyDemandChart";
import { IngredientForecastPanel } from "@/components/IngredientForecastPanel";

export default function OwnerDashboard() {
  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16 items-center">
            <h1 className="text-xl font-bold text-gray-900">SweetOps <span className="text-blue-600">Analytics</span></h1>
            <div className="text-sm text-gray-500">Owner Portal • Live</div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        
        <div className="mb-6 flex items-center justify-between">
          <h2 className="text-2xl font-bold text-gray-900">Operational Overview</h2>
        </div>

        {/* Real Data KPI Cards */}
        <KPICardGrid />

        {/* Charts & Lists Grid */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
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
