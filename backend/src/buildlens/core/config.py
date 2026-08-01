"""Application configuration, loaded from environment variables (12-factor).

Parsed and validated once at startup; the process must not start with
invalid config. Everything else imports get_settings(), never os.environ.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="BUILDLENS_",
        extra="ignore",
    )

    database_url: str
    github_token: SecretStr | None = None
    github_api_base_url: str = "https://api.github.com"
    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore
