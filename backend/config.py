from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    playwright_headless: bool = True
    playwright_slowmo_ms: int = 0
    session_ttl_seconds: int = 86400
    mfa_timeout_seconds: int = 300
    auth_state_max_age_seconds: int = 2592000
    usaa_quick_path_max_age_seconds: int = 1800
    persist_completed_results: bool = True
    usaa_login_driver: str = "os_browser"
    usaa_os_browser_profile_dir: str = "storage/browser-profiles/usaa-os-browser"
    usaa_os_login_timeout_seconds: int = 90
    log_level: str = "INFO"
    log_file_path: str = "storage/logs/app.log"
    log_max_bytes: int = 10485760
    log_backup_count: int = 5
    worker_base_url: str | None = None
    worker_proxy_carriers: str = "usaa"
    # NOTE: the mock-carrier toggles (CARRIER_MOCK, MOCK_BAD_PASSWORD,
    # MOCK_BAD_MFA, MOCK_SKIP_MFA, MOCK_QUICK_PATH_OK) are intentionally NOT
    # settings here. They must be read at call time so the test suite and demo
    # can flip them per process after import; see `backend/env_flags.env_truthy`
    # and `backend/carriers/{registry,mock}.py`. extra="ignore" lets them sit in
    # .env without error.

    # Optional credentials for smoke tests and demo pre-fill
    geico_username: str | None = None
    geico_password: str | None = None
    progressive_username: str | None = None
    progressive_password: str | None = None
    allstate_username: str | None = None
    allstate_password: str | None = None
    state_farm_username: str | None = None
    state_farm_password: str | None = None
    mercury_username: str | None = None
    mercury_password: str | None = None
    usaa_username: str | None = None
    usaa_password: str | None = None
    usaa_mfa_email: str | None = None
    usaa_worker_base_url: str | None = None
    dev_prefill_creds: bool = False

    # Email notification (Resend). When unset, /api/notify still accepts
    # signups but the watcher logs and skips the actual send.
    resend_api_key: str | None = None
    resend_from_email: str = "Infer <noreply@mail.discordwell.com>"
    email_max_attachment_bytes: int = 20_000_000  # 20MB
    notify_wall_seconds: int = 18000  # 5h cap


settings = Settings()
