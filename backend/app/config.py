from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration shared by API and worker processes."""

    app_name: str = "RepoPulse API"
    database_url: str = "sqlite+aiosqlite:///./repopulse.db"
    sync_database_url: str = "sqlite:///./repopulse.db"
    redis_url: str = "redis://localhost:6379/0"
    github_token: str | None = None
    candidate_languages: str = (
        "TypeScript,JavaScript,Python,Go,Rust,Java,C++,C#,Swift,Kotlin,PHP,Ruby,Dart,Shell"
    )
    candidate_topics: str = "ai,llm,developer-tools,database,web-framework,self-hosted"
    manual_seed_repositories: str = ""
    excluded_repositories: str = ""
    repository_auto_quarantine: bool = False
    repository_probe_enabled: bool = True
    snapshot_deadline_hours: int = Field(default=6, ge=1, le=23)
    ranking_min_completeness_percent: int = Field(default=95, ge=1, le=100)
    max_active_repositories: int = 5000
    github_search_pages: int = 2
    snapshot_concurrency: int = Field(default=4, ge=1, le=16)
    snapshot_requests_per_second: float = Field(default=4, gt=0, le=10)
    snapshot_batch_size: int = Field(default=50, ge=1, le=500)
    snapshot_flush_seconds: float = Field(default=5, gt=0, le=30)
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.05
    seed_demo_data: bool = False
    frontend_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    environment: str = "development"
    avatar_cache_dir: str = "/tmp/repopulse-avatars"
    avatar_cache_max_bytes: int = 524288000
    avatar_refresh_days: int = 7
    avatar_download_concurrency: int = 2
    avatar_request_timeout: float = 15
    avatar_warmup_limit: int = 100

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]

    @property
    def github_tokens(self) -> list[str]:
        return [token.strip() for token in (self.github_token or "").split(",") if token.strip()]

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def languages(self) -> list[str]:
        return self._csv(self.candidate_languages)

    @property
    def topics(self) -> list[str]:
        return self._csv(self.candidate_topics)

    @property
    def seed_repositories(self) -> list[str]:
        return self._csv(self.manual_seed_repositories)

    @property
    def repository_exclusions(self) -> set[str]:
        return {item.lower() for item in self._csv(self.excluded_repositories)}


@lru_cache
def get_settings() -> Settings:
    return Settings()
