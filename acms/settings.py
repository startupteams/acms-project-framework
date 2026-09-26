from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACMS_", env_file=".env", extra="ignore")

    admin_token: str = "change-me"
    database_url: str = "sqlite:///./acms.db"
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
