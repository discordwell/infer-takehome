from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    playwright_headless: bool = True
    playwright_slowmo_ms: int = 0
    session_ttl_seconds: int = 1800
    mfa_timeout_seconds: int = 300
    usaa_quick_path_max_age_seconds: int = 300
    usaa_login_driver: str = "os_browser"
    usaa_os_browser_profile_dir: str = "storage/browser-profiles/usaa-os-browser"
    usaa_os_login_timeout_seconds: int = 90
    worker_base_url: str | None = None
    worker_proxy_carriers: str = "usaa"
    carrier_mock: bool = False
    mock_bad_password: bool = False
    mock_bad_mfa: bool = False
    mock_skip_mfa: bool = False
    mock_quick_path_ok: bool = True

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


settings = Settings()
