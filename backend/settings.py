from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal


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
