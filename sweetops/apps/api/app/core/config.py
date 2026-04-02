from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "SweetOps API"
    DATABASE_URL: str = "postgresql://sweetops:sweetops_password@postgres:5432/sweetops_db"
    REDIS_URL: str = "redis://redis:6379/0"

    class Config:
        env_file = ".env"

settings = Settings()
