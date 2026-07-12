"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import {
  ALLOWED_ROLES,
  fetchMe,
  login as apiLogin,
  logout as apiLogout,
  setUnauthorizedHandler,
  StaffProfile,
} from "@/lib/auth";

// All user-facing strings are Turkish.

type Phase = "loading" | "login" | "authed" | "forbidden";

interface AuthContextValue {
  user: StaffProfile | null;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthGate");
  return ctx;
}

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [user, setUser] = useState<StaffProfile | null>(null);
  const [expiredNotice, setExpiredNotice] = useState(false);

  const evaluate = useCallback((profile: StaffProfile | null) => {
    if (!profile) {
      setUser(null);
      setPhase("login");
      return;
    }
    if (!ALLOWED_ROLES.includes(profile.role)) {
      setUser(profile);
      setPhase("forbidden");
      return;
    }
    setUser(profile);
    setPhase("authed");
  }, []);

  const bootstrap = useCallback(async () => {
    setPhase("loading");
    try {
      evaluate(await fetchMe());
    } catch {
      setPhase("login");
    }
  }, [evaluate]);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      setExpiredNotice(true);
      setPhase("login");
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const logout = useCallback(async () => {
    await apiLogout();
    setUser(null);
    setExpiredNotice(false);
    setPhase("login");
  }, []);

  if (phase === "loading") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50 text-slate-600">
        Yükleniyor…
      </div>
    );
  }

  if (phase === "login") {
    return (
      <LoginScreen
        expiredNotice={expiredNotice}
        onSuccess={(profile) => {
          setExpiredNotice(false);
          evaluate(profile);
        }}
      />
    );
  }

  if (phase === "forbidden") {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center gap-4 bg-slate-50 text-center px-6">
        <p className="text-lg font-semibold text-slate-900">
          Bu işlem için yetkiniz yok.
        </p>
        <button
          onClick={logout}
          className="px-4 py-2 rounded bg-slate-800 text-white text-sm hover:bg-slate-900"
        >
          Çıkış Yap
        </button>
      </div>
    );
  }

  return (
    <AuthContext.Provider value={{ user, logout }}>
      <div className="fixed top-3 right-3 z-50 flex items-center gap-2">
        {user && (
          <span className="text-xs text-slate-500 bg-white/80 rounded px-2 py-1 shadow-sm hidden sm:inline">
            {user.username} · {user.role}
          </span>
        )}
        <button
          onClick={logout}
          className="px-3 py-1 rounded bg-slate-800 text-white text-xs hover:bg-slate-900 shadow-sm"
        >
          Çıkış Yap
        </button>
      </div>
      {children}
    </AuthContext.Provider>
  );
}

function LoginScreen({
  expiredNotice,
  onSuccess,
}: {
  expiredNotice: boolean;
  onSuccess: (profile: StaffProfile) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const profile = await apiLogin(username.trim(), password);
      onSuccess(profile);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Oturum açılamadı. Lütfen tekrar deneyin.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="min-h-screen flex items-center justify-center bg-slate-50 px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm bg-white rounded-lg shadow p-6 space-y-4"
      >
        <div className="text-center">
          <div className="text-3xl mb-1">🧾</div>
          <h1 className="text-xl font-bold text-slate-900">Kasa Girişi</h1>
        </div>

        {expiredNotice && (
          <p className="text-sm text-amber-700 bg-amber-50 border border-amber-200 rounded px-3 py-2">
            Oturumunuzun süresi doldu. Lütfen yeniden giriş yapın.
          </p>
        )}

        <div>
          <label htmlFor="username" className="block text-sm font-medium text-slate-700 mb-1">
            Kullanıcı adı
          </label>
          <input
            id="username"
            type="text"
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="w-full border border-slate-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            required
          />
        </div>

        <div>
          <label htmlFor="password" className="block text-sm font-medium text-slate-700 mb-1">
            Şifre
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full border border-slate-300 rounded px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            required
          />
        </div>

        {error && (
          <p className="text-sm text-red-700 bg-red-50 border border-red-200 rounded px-3 py-2">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="w-full py-2.5 rounded bg-indigo-600 hover:bg-indigo-700 text-white font-semibold text-sm disabled:opacity-60"
        >
          {submitting ? "Giriş yapılıyor…" : "Giriş Yap"}
        </button>
      </form>
    </main>
  );
}
