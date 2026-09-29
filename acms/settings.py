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

    # Session-rotation advisory thresholds (REV2 plan §7): CONFIGURABLE and
    # EXPERIMENTAL — starting points for empirical cost-vs-output tuning, not
    # final policy. Rotation is advisory-only; auto-rotation is feature-flagged
    # OFF (session_rotation_auto_enabled=False) and must never fire on context
    # threshold alone.
    session_advisory_monitor_percent: int = 50
    session_advisory_checkpoint_percent: int = 70
    session_advisory_rotate_percent: int = 85
    session_rotation_auto_enabled: bool = False

    # Agent Bridge targets (plan §15): JSON list of {agent_id, base_url,
    # api_key}. Keys live in .env/config, never committed (plan §18).
    bridge_targets_json: str = ""

    # Server Manager integration (JINT-001, REV4 §12/§13): base URL of the
    # machine API (e.g. http://10.0.20.108:8300) + the scoped svc-acms token
    # (§11: outside Git, rotatable).
    server_manager_base_url: str = ""
    server_manager_token: str = ""

    # Work Item creation authority (ADR-0011 Accepted): comma-separated agent
    # IDs holding Executive delegation. Workers may NOT create Work Items; they
    # record proposed_next_work in handoffs for Executive review instead.
    executive_agent_ids: str = ""

    # Jira v1 integration (2026-09-29 plan Phase D): Jira = human planning
    # layer, ACMS = AI execution beneath it. Credentials env-only, never Git.
    # Empty base_url/email/token => client runs in MOCK mode (no fabricated
    # credentials). Status mutation is flagged OFF by default (plan D6).
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_projects: str = ""
    jira_status_mutation_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
