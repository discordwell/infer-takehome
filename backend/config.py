from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    playwright_headless: bool = True
    playwright_slowmo_ms: int = 0
    session_ttl_seconds: int = 1800
    carrier_mock: bool = False
    mock_bad_password: bool = False
    mock_bad_mfa: bool = False
    mock_skip_mfa: bool = False
    mock_quick_path_ok: bool = True

    # Optional credentials for smoke tests and demo pre-fill
    geico_username: str | None = None
    geico_password: str | None = None
    usaa_username: str | None = None
    usaa_password: str | None = None
    usaa_mfa_email: str | None = None
    dev_prefill_creds: bool = False


settings = Settings()
