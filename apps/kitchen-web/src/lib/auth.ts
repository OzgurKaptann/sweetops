// Staff authentication client for kitchen-web.
//
// The session lives in an HttpOnly cookie the browser sends automatically with
// `credentials: "include"`. JavaScript never reads or stores the session token.
// The CSRF token lives in a readable cookie and is echoed back in X-CSRF-Token
// on every state-changing request (double-submit).

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000";

const CSRF_COOKIE = "sweetops_csrf";

// Roles allowed to use kitchen-web.
export const ALLOWED_ROLES = ["OWNER", "MANAGER", "KITCHEN"];

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

// Thrown when a protected request comes back 401 (session expired/revoked).
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

/** Returns the current staff profile, or null if not authenticated. */
export async function fetchMe(): Promise<StaffProfile | null> {
  const res = await fetch(`${API_BASE}/auth/me`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error("auth_me_failed");
  return res.json();
}

/** Attempt login. Returns the profile on success; throws Error with a Turkish
 *  message on failure (invalid credentials / lockout / network). */
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
    // Best-effort; local state is cleared regardless by the caller.
  }
}
