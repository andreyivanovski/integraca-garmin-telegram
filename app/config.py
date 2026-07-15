from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_dir: Path = Path("data")

    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""

    # ID do seu relógio — só no .env local (não versionado)
    default_device_id: int | None = None

    garmin_email: str = ""
    garmin_password: str = ""
    garmin_locale: str = "pt-PT"
    garmin_service: str = "https://connect.garmin.com/app"
    garmin_sso: str = "https://sso.garmin.com"
    garmin_connectapi: str = "https://connectapi.garmin.com"
    garmin_diauth: str = "https://diauth.garmin.com"

    @field_validator("default_device_id", mode="before")
    @classmethod
    def _empty_device_id(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return value

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "garmin_tokens.json"

    @property
    def allowed_chat_ids(self) -> set[int]:
        if not self.telegram_allowed_chat_ids.strip():
            return set()
        return {int(x.strip()) for x in self.telegram_allowed_chat_ids.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
