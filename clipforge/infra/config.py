"""12-factor settings. Every value comes from env (or .env); nothing secret has a real default."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
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
    api_token: SecretStr = SecretStr("")  # required outside tests; see validator

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
    # Provider: anthropic | openrouter | groq | openai_compat (any OpenAI-compatible endpoint)
    llm_provider: Literal["anthropic", "openrouter", "groq", "openai_compat"] = "anthropic"
    anthropic_api_key: SecretStr | None = None
    llm_api_key: SecretStr | None = None  # key for openrouter / groq / openai_compat
    llm_base_url: str | None = None  # required for openai_compat; optional override otherwise
    llm_model: str = "claude-haiku-5-5"  # must be set explicitly for non-anthropic providers
    llm_timeout_s: float = 90.0
    llm_max_retries: int = 3
    judge_batch_size: int = Field(default=8, ge=1, le=25)
    judge_max_windows_per_video: int = Field(default=150, ge=1)

    # USD per 1M tokens, for the cost estimate in usage_events (Haiku 5.5, <=100K prompts)
    llm_price_in_per_mtok: float = 0.10
    llm_price_out_per_mtok: float = 0.50
    llm_price_cache_read_per_mtok: float = 0.01
    llm_price_cache_write_per_mtok: float = 0.125

    # Render (M4)
    preview_top_n: int = Field(default=5, ge=0)  # auto-previews after analysis; 0 = off
    render_timeout_s: float = 1800

    # Publishing (M6). The key encrypts OAuth tokens at rest (ADR-0039); generate one with
    # `clipforge secret-key`. Instagram/TikTok are stubs until their app reviews are done.
    secret_key: SecretStr | None = None
    enable_instagram_publish: bool = False
    enable_tiktok_publish: bool = False
    scheduler_tick_s: float = Field(default=30, ge=5)
    stats_refresh_s: float = Field(default=6 * 3600, ge=300)
    stats_window_days: int = Field(default=30, ge=1)

    # SaaS stubs
    per_workspace_max_concurrent_jobs: int = Field(default=4, ge=1)

    @model_validator(mode="after")
    def _refuse_weak_token(self) -> Settings:
        """Fail closed: never start with an empty/placeholder/short API token outside tests."""
        token = self.api_token.get_secret_value()
        if self.env != "test" and (len(token) < 24 or token.startswith("change-me")):
            raise ValueError(
                "CLIPFORGE_API_TOKEN must be set to a random string of >= 24 chars "
                '(e.g. python -c "import secrets; print(secrets.token_urlsafe(32))")'
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
