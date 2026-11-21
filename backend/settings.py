from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal
from urllib.parse import urlsplit


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./.work/modeus.db"
    app_origin: str = "http://localhost:5173"
    cookie_secure: bool = False
    session_hours: int = Field(default=12, ge=1, le=168)
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = Field(default="", repr=False)
    llm_timeout_seconds: int = Field(default=45, ge=1, le=120)
    llm_reasoning_effort: Literal["", "none", "low", "medium", "high"] = ""
    llm_temperature: float | None = Field(default=0, ge=0, le=2)
    llm_seed: int | None = None
    llm_cache: bool = True
    hh_user_agent: str = ""
    hh_access_token: str = Field(default="", repr=False)
    hh_client_id: str = Field(default="", repr=False)
    hh_client_secret: str = Field(default="", repr=False)
    hh_areas: str = "113"
    hh_weekly_enabled: bool = True
    documentation_weekly_enabled: bool = True
    hn_collection_enabled: bool = True
    github_token: str = Field(default="", repr=False)
    openalex_api_key: str = Field(default="", repr=False)
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_api_url: str = "https://api.telegram.org"
    telegram_open_signup: bool = False

    @field_validator("llm_temperature", "llm_seed", mode="before")
    @classmethod
    def empty_means_unset(cls, value):
        # An empty value in .env leaves the provider default.
        return None if value == "" else value

    @property
    def origin_list(self):
        return tuple(value.strip().rstrip("/") for value in self.app_origin.split(",") if value.strip())

    @property
    def app_origins(self):
        return frozenset(self.origin_list)

    @property
    def secure_cookie(self):
        return self.cookie_secure or any(urlsplit(origin).scheme == "https" for origin in self.origin_list)

    @property
    def local_docs(self):
        return bool(self.origin_list) and all(
            urlsplit(origin).hostname in {"localhost", "127.0.0.1", "::1"} for origin in self.origin_list
        )
