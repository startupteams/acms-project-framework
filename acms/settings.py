from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACMS_", env_file=".env", extra="ignore")

    admin_token: str = "change-me"
    database_url: str = "sqlite:///./acms.db"
    environment: str = "development"

    # Web UI human authentication (plan §6; ADR-0008).
    # Machine/API access stays on the bearer token and is never shared with the browser.
    # Empty/placeholder values fail closed: UI login is disabled rather than insecure.
    session_secret: str = ""
    session_lifetime_minutes: int = 480
    session_cookie_secure: bool = True
    ldap_url: str = ""
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_user_base: str = ""
    ldap_user_filter: str = "(uid={username})"
    ldap_group_admin: str = ""
    ldap_group_worker: str = ""
    ldap_group_observer: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
