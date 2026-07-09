// Staff authentication client for owner-web.
//
// The session lives in an HttpOnly cookie sent automatically with
// `credentials: "include"`; JavaScript never reads or stores the session token.
// The CSRF token lives in a readable cookie and is echoed in X-CSRF-Token on
// state-changing requests (double-submit).

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

const CSRF_COOKIE = "sweetops_csrf";

// Roles allowed to use owner-web.
export const ALLOWED_ROLES = ["OWNER", "MANAGER"];

export interface StaffProfile {
  id: number;
  username: string;
  role: string;
  store: { id: number; name: string } | null;
  permissions: string[];
}

// Global 401 handler: AuthGate registers a callback so any protected request
// that returns 401 (expired/revoked session) sends the app back to login.
let unauthorizedHandler: (() => void) | null = null;
export function setUnauthorizedHandler(cb: (() => void) | null): void {
  unauthorizedHandler = cb;
}

export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
    this.name = "UnauthorizedError";
    if (unauthorizedHandler) unauthorizedHandler();
  }
}

export function readCsrfToken(): string {
  if (typeof document === "undefined") return "";
  const match = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${CSRF_COOKIE}=`));
  return match ? decodeURIComponent(match.split("=")[1]) : "";
}

export function csrfHeaders(): Record<string, string> {
  const token = readCsrfToken();
  return token ? { "X-CSRF-Token": token } : {};
}

export async function fetchMe(): Promise<StaffProfile | null> {
  const res = await fetch(`${API_BASE}/auth/me`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error("auth_me_failed");
  return res.json();
}

export async function login(
  username: string,
  password: string,
): Promise<StaffProfile> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
  } catch {
    throw new Error("Bağlantı hatası. Lütfen tekrar dene.");
  }

  if (res.ok) return res.json();

  const body = await res.json().catch(() => ({}));
  const detail = body?.detail;
  const message =
    (detail && typeof detail === "object" && detail.message) ||
    "Oturum açılamadı. Lütfen tekrar dene.";
  throw new Error(message);
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: { ...csrfHeaders() },
    });
  } catch {
    // best-effort
  }
}
