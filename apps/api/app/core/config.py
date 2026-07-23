from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "SweetOps API"
    DATABASE_URL: str = "postgresql://sweetops:sweetops_password@localhost:5432/sweetops_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Environment ──────────────────────────────────────────────────────────
    # "development" | "production". Controls production-only invariants such as
    # forcing Secure cookies. Anything other than "production" is treated as a
    # non-production environment.
    ENVIRONMENT: str = "development"

    # Public base URL used when printing customer QR URLs from the CLI.
    CUSTOMER_WEB_BASE_URL: str = "http://localhost:3000"

    # Transition mode ONLY. When True, /public/orders/ still accepts a legacy
    # client-supplied store_id/table_id when no qr_token is provided. Defaults
    # to False so production never trusts client-supplied table context — the
    # secure QR path is the only accepted one. Non-production environments (the
    # test suite) may opt in explicitly. A qr_token, when present, always wins
    # and client-supplied ids are ignored regardless of this flag.
    ALLOW_LEGACY_ORDER_CONTEXT: bool = False

    # ── Staff authentication cookies ─────────────────────────────────────────
    # Opaque server-side session model. The session cookie is HttpOnly (never
    # readable by JavaScript); the CSRF cookie is deliberately NOT HttpOnly so
    # the SPA can echo it back in the X-CSRF-Token header (double-submit).
    SESSION_COOKIE_NAME: str = "sweetops_session"
    CSRF_COOKIE_NAME: str = "sweetops_csrf"
    SESSION_COOKIE_PATH: str = "/"
    # Empty string → host-only cookie (no Domain attribute). Never assume a
    # wildcard parent domain.
    SESSION_COOKIE_DOMAIN: str = ""
    # "lax" | "strict" | "none". Lax is the safe default for a staff SPA served
    # from a sibling origin using top-level navigation + XHR with credentials.
    SESSION_COOKIE_SAMESITE: str = "lax"
    # Secure=true is mandatory in production (enforced below). In development it
    # may be disabled explicitly so http://localhost works without HTTPS.
    SESSION_COOKIE_SECURE: bool = False

    # ── Session lifetime ─────────────────────────────────────────────────────
    SESSION_ABSOLUTE_LIFETIME_HOURS: int = 12     # hard cap regardless of activity
    SESSION_IDLE_TIMEOUT_MINUTES: int = 120       # revoke after inactivity
    SESSION_LAST_SEEN_THROTTLE_SECONDS: int = 300 # throttle last_seen writes (5 min)

    # ── Login protection (account-level brute-force) ─────────────────────────
    LOGIN_MAX_FAILED_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    # ── Password policy ──────────────────────────────────────────────────────
    PASSWORD_MIN_LENGTH: int = 10
    # Argon2 has an internal safe maximum; we do not impose a small artificial
    # cap. This guards only against absurd payloads.
    PASSWORD_MAX_LENGTH: int = 1024

    # ── Trusted staff origins ────────────────────────────────────────────────
    # Comma-separated list of exact origins allowed to send credentialed staff
    # requests (CORS) and to originate logins / state-changing mutations
    # (origin check). NEVER use "*" with credentials. Production values come
    # from the environment (STAFF_TRUSTED_ORIGINS=...).
    # Ports: kitchen-web 3002, owner-web 3003, cashier-web 3004.
    STAFF_TRUSTED_ORIGINS: str = (
        "http://localhost:3001,http://localhost:3002,"
        "http://localhost:3003,http://localhost:3004"
    )

    # Public customer origin(s) — allowed for the public QR/menu/order flow.
    # Kept separate so staff CSRF/origin rules never leak onto the public flow.
    PUBLIC_TRUSTED_ORIGINS: str = "http://localhost:3000"

    # ── WebSocket Cross-Site WebSocket Hijacking (CSWSH) defence ──────────────
    # The kitchen WebSocket authenticates via the staff session cookie. Cookie
    # auth alone does NOT stop a malicious page from opening the socket from a
    # logged-in staff browser, so the handshake Origin is validated against the
    # trusted staff origins (exact scheme/host/port — never substring).
    #
    # Browsers always send an Origin header on a WebSocket handshake; a missing
    # Origin therefore means a non-browser client (tests, CLI, server-to-server).
    # Production MUST reject missing Origin, so this defaults to False and is
    # NEVER inferred from the hostname. Enable it ONLY in an isolated
    # test/development configuration for non-browser clients.
    ALLOW_MISSING_WEBSOCKET_ORIGIN: bool = False

    # ── Business timezone ────────────────────────────────────────────────────
    # Storage stays UTC everywhere — every timestamp column is timestamptz and
    # every write uses an aware UTC datetime. This setting affects REPORTING
    # ONLY: which UTC instants make up "today", where a daily bucket starts and
    # ends, and which local hour an order falls into. An IANA zone name resolved
    # by the stdlib zoneinfo (see app/core/business_time.py) — an unknown name
    # fails loudly at import, never silently falls back to UTC.
    BUSINESS_TIMEZONE: str = "Europe/Istanbul"

    class Config:
        env_file = ".env"

    # ── Derived helpers ──────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() == "production"

    @property
    def cookie_secure(self) -> bool:
        """Secure cookies are always on in production, regardless of the raw flag."""
        return True if self.is_production else self.SESSION_COOKIE_SECURE

    @property
    def cookie_domain(self) -> str | None:
        return self.SESSION_COOKIE_DOMAIN or None

    @property
    def staff_origins(self) -> List[str]:
        return [o.strip() for o in self.STAFF_TRUSTED_ORIGINS.split(",") if o.strip()]

    @property
    def public_origins(self) -> List[str]:
        return [o.strip() for o in self.PUBLIC_TRUSTED_ORIGINS.split(",") if o.strip()]

    @property
    def all_cors_origins(self) -> List[str]:
        # De-duplicate while preserving order.
        seen: dict[str, None] = {}
        for o in self.staff_origins + self.public_origins:
            seen.setdefault(o, None)
        return list(seen.keys())


settings = Settings()
