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

    # Live fleet telemetry thresholds (combined slice 3+4; ADR-0010 Accepted defaults).
    heartbeat_interval_seconds: int = 60
    heartbeat_stale_seconds: int = 300
    stale_reconcile_seconds: int = 3600
    fleet_reconcile_seconds: int = 86400
    telemetry_sample_seconds: int = 300
    session_alignment_grace_seconds: int = 120
    context_elevated_percent: int = 70
    context_high_percent: int = 85
    context_critical_percent: int = 95

    # Agent Bridge targets (plan §15): JSON list of {agent_id, base_url,
    # api_key}. Keys live in .env/config, never committed (plan §18).
    bridge_targets_json: str = ""

    # Server Manager integration (JINT-001, REV4 §12/§13): base URL of the
    # machine API (e.g. http://10.0.20.108:8300) + the scoped svc-acms token
    # (§11: outside Git, rotatable).
    server_manager_base_url: str = ""
    server_manager_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
