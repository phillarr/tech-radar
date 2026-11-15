"""Loads and validates the static JSON configuration and the environment secrets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# OFFICIAL: the company, project, lab or regulator itself. MEDIA: third-party reporting.
# RESEARCH: papers and research blogs. COMMUNITY: forums, aggregators, individual blogs.
SourceType = Literal["OFFICIAL", "MEDIA", "RESEARCH", "COMMUNITY"]


class RSSSource(BaseModel):
    name: str
    url: HttpUrl
    type: SourceType
    max_items: Optional[int] = Field(default=None, gt=0)  # overrides settings.max_items_per_source


class SourcesConfig(BaseModel):
    github_repositories: List[str] = Field(default_factory=list)
    rss_feeds: List[RSSSource] = Field(default_factory=list)
    # Analysis and commentary, used only by the Weekly Radar's "Worth reading" section.
    reading_feeds: List[RSSSource] = Field(default_factory=list)

    @field_validator("github_repositories")
    @classmethod
    def _validate_repo_slugs(cls, repos: List[str]) -> List[str]:
        for repo in repos:
            parts = repo.split("/")
            if len(parts) != 2 or not all(parts):
                raise ValueError(f"Invalid GitHub repository '{repo}', expected 'owner/repo'")
        return repos


class SettingsConfig(BaseModel):
    hours_lookback: int = Field(default=24, gt=0)
    max_items_per_source: int = Field(default=5, gt=0)
    max_content_chars: int = Field(default=2000, gt=0)
    audience: str = "a full-stack Python engineer"
    model_name: str = "claude-haiku-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_events: int = Field(default=10, gt=0, le=30)
    timezone: str = "Europe/Paris"  # display timezone; storage stays UTC
    post_when_empty: bool = False
    request_timeout_seconds: float = Field(default=20, gt=0)


class AppConfig(BaseModel):
    settings: SettingsConfig
    sources: SourcesConfig


class EnvSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    anthropic_api_key: str
    slack_webhook_url: str
    github_token: Optional[str] = None
    supabase_url: Optional[str] = None  # storage is optional: unset means no history
    supabase_service_key: Optional[str] = None
    config_path: str = "config/sources.json"
    dry_run: bool = False
    scheduled: bool = False  # true for cron runs: at most one digest per day


def resolve_config_path(config_path: str) -> Path:
    path = Path(config_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_app_config(config_path: str) -> AppConfig:
    path = resolve_config_path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return AppConfig.model_validate(json.load(fh))


def load_all() -> tuple[EnvSecrets, AppConfig]:
    secrets = EnvSecrets()
    return secrets, load_app_config(secrets.config_path)
