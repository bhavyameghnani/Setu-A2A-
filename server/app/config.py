from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-insecure-secret-change-me-0123456789"
# Fernet keys are 32 url-safe-base64 bytes. This one is public: dev only.
# Ed25519 seed (32 bytes, base64url). Public, dev only: anyone can forge signatures with it.
_DEV_SIGNING_KEY = "c2V0dS1kZXYtb25seS1zaWduaW5nLWtleS0zMmJ5dGU="
_DEV_ENCRYPTION_KEY = "ZGV2LW9ubHktaW5zZWN1cmUta2V5LTMyYnl0ZXMhISE="


class Settings(BaseSettings):
    # Reads real env vars first, then .env (repo root or server/).
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_env: str = "dev"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://localhost:5432/setu"
    jwt_secret: str | None = None
    jwt_ttl_minutes: int = 60
    encryption_key: str | None = None
    signing_key: str | None = None  # Ed25519 seed for signed artifacts (see core/signing.py)
    public_url: str | None = None  # base URL agents use to reach Setu; default: the request's
    a2a_allow_private_hosts: bool | None = None  # default: allowed only when APP_ENV=dev
    agent_card_refresh_seconds: int = 3600  # 0 disables the scheduled card refresh
    a2a_retry_backoff_seconds: float = 0.5
    a2a_max_concurrent_tasks: int = 20
    snapshot_interval_seconds: int = 300  # 0 disables the background snapshot job
    snapshot_min_messages: int = 50

    def resolved_jwt_secret(self) -> str:
        """Secrets come from env. Only `dev` may fall back to a well-known placeholder."""
        if self.jwt_secret:
            return self.jwt_secret
        if self.app_env == "dev":
            return _DEV_SECRET
        raise RuntimeError("JWT_SECRET must be set when APP_ENV is not 'dev'")

    def resolved_encryption_key(self) -> str:
        if self.encryption_key:
            return self.encryption_key
        if self.app_env == "dev":
            return _DEV_ENCRYPTION_KEY
        raise RuntimeError("ENCRYPTION_KEY must be set when APP_ENV is not 'dev'")

    def resolved_signing_key(self) -> str:
        if self.signing_key:
            return self.signing_key
        if self.app_env == "dev":
            return _DEV_SIGNING_KEY
        raise RuntimeError("SIGNING_KEY must be set when APP_ENV is not 'dev'")

    def allow_private_agent_hosts(self) -> bool:
        if self.a2a_allow_private_hosts is not None:
            return self.a2a_allow_private_hosts
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
