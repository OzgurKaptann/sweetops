// Owner-web operational-dashboard read client. The dashboard is a store-scoped,
// read-only aggregate; owner-web only ever GETs it (never mutates through it).
import { API_BASE, UnauthorizedError } from "./auth";
import type { OperationalDashboard } from "./operational-dashboard-view";

export async function fetchOperationalDashboard(): Promise<OperationalDashboard> {
  const res = await fetch(`${API_BASE}/owner/operational-dashboard`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error("operational_dashboard_failed");
  return res.json();
}
