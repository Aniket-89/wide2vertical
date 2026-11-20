"""12-factor settings. Every value comes from env (or .env); nothing secret has a real default."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="CLIPFORGE_", extra="ignore", case_sensitive=False
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+psycopg://clipforge:clipforge@127.0.0.1:5432/clipforge"
    redis_url: str = "redis://127.0.0.1:6380/0"

    # Auth seam: one static token in the MVP.
    api_token: SecretStr = SecretStr("change-me")

    # Single-tenant MVP: the seeded workspace/user. Ids are fixed so seed is idempotent.
    default_workspace_id: str = "01936f00-0000-7000-8000-000000000001"
    default_user_id: str = "01936f00-0000-7000-8000-000000000002"
    default_user_email: str = "owner@clipforge.local"

    # Storage
    blob_root: Path = Path("./data/blobs")
    raw_video_retention_days: int = 7

    # Feature flags
    enable_live: bool = False
    enable_autopublish: bool = False
    enable_llm_judge: bool = False
    enable_kick_chat: bool = False
    enable_twitch_chat_gql: bool = False
    enable_youtube_chat_replay: bool = False

    # Transcription (CPU-first; see DECISIONS ADR-0005)
    whisper_model: str = "small"
    whisper_device: Literal["auto", "cpu", "cuda"] = "cpu"
    whisper_compute_type: str = "int8"

    # LLM judge
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-haiku-5-5"

    # SaaS stubs
    per_workspace_max_concurrent_jobs: int = Field(default=4, ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
