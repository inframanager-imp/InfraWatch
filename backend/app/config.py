from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://infrawatch:infrawatch_dev@localhost:5432/infrawatch"
    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12
    admin_email: str = "admin@infrawatch.local"
    admin_password: str = "changeme123"
    cors_origins: str = "*"  # comma-separated list, or "*" for local dev

    class Config:
        env_file = ".env"


settings = Settings()
