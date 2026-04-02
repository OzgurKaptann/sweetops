from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    PROJECT_NAME: str = "SweetOps API"
    DATABASE_URL: str = "postgresql://sweetops:sweetops_password@localhost:5432/sweetops_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    class Config:
        env_file = ".env"

settings = Settings()
