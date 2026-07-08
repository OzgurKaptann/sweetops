from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "SweetOps API"
    DATABASE_URL: str = "postgresql://sweetops:sweetops_password@localhost:5432/sweetops_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Public base URL used when printing customer QR URLs from the CLI.
    CUSTOMER_WEB_BASE_URL: str = "http://localhost:3000"

    # Transition mode ONLY. When True, /public/orders/ still accepts a legacy
    # client-supplied store_id/table_id when no qr_token is provided. Defaults
    # to False so production never trusts client-supplied table context — the
    # secure QR path is the only accepted one. Non-production environments (the
    # test suite) may opt in explicitly. A qr_token, when present, always wins
    # and client-supplied ids are ignored regardless of this flag.
    ALLOW_LEGACY_ORDER_CONTEXT: bool = False

    class Config:
        env_file = ".env"

settings = Settings()
