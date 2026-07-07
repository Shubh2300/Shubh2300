"""Environment-driven configuration for the Back Office API.

All values come from the process environment (loaded from the office
machine's ``.env`` by docker-compose). NO secrets or defaults for secrets
are hard-coded here — only names. See ``.env.example`` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core infrastructure -------------------------------------------------
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/backoffice",
        alias="DATABASE_URL",
    )
    temporal_address: str = Field(default="localhost:7233", alias="TEMPORAL_ADDRESS")
    temporal_namespace: str = Field(default="default", alias="TEMPORAL_NAMESPACE")
    temporal_task_queue: str = Field(
        default="emr-actions", alias="TEMPORAL_TASK_QUEUE"
    )

    # --- EMR bridge (runs on the HOST, not in a container) -------------------
    # The bridge holds a persistent, logged-in real browser session to the
    # EMRs, so it cannot run in the container network. From inside Docker we
    # reach it at host.docker.internal; see docker-compose.yml.
    bridge_url: str = Field(default="http://localhost:8600", alias="BRIDGE_URL")

    # --- AI parser -----------------------------------------------------------
    # Name only. If OPENAI_API_KEY is unset the /intents/parse endpoint returns
    # a structured error rather than any canned output.
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_parser_model: str = Field(
        default="gpt-4o-mini", alias="OPENAI_PARSER_MODEL"
    )

    # --- Action registry artifacts (built by the action-registry package) ----
    action_registry_path: str = Field(
        default="/app/packages/action-registry/actions.json",
        alias="ACTION_REGISTRY_PATH",
    )
    action_intent_schema_path: str = Field(
        default="/app/packages/action-registry/action_intent.schema.json",
        alias="ACTION_INTENT_SCHEMA_PATH",
    )

    # --- Audit log -----------------------------------------------------------
    # Per-install pepper for hashing patient identifiers in the audit chain.
    audit_pepper: str | None = Field(default=None, alias="AUDIT_PEPPER")

    # --- CORS ----------------------------------------------------------------
    # Comma-separated list of allowed origins for the Next.js web app.
    cors_origins: str = Field(
        default="http://localhost:3000", alias="CORS_ORIGINS"
    )

    @property
    def cors_origin_list(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
