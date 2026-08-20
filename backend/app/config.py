from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://infrawatch:infrawatch_dev@localhost:5432/infrawatch"
    jwt_secret: str = "change-me-in-prod"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 12
    admin_email: str = "admin@infrawatch.local"
    admin_password: str = "changeme123"
    cors_origins: str = "*"  # comma-separated list, or "*" for local dev

    # Alert email notifications — entirely optional. Leaving smtp_host empty
    # disables sending outright (see app/notifications.py), same "degrades
    # gracefully" pattern as the agent's optional websocket-client dep.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    frontend_url: str = ""  # used to build a link back into the app in alert emails

    class Config:
        env_file = ".env"


settings = Settings()
