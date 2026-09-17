from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, model_validator
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
    trusted_proxy_token: str | None = None
    internal_service_token: str | None = None
    avatar_cache_dir: str = "/tmp/repopulse-avatars"
    avatar_cache_max_bytes: int = 524288000
    avatar_refresh_days: int = 7
    avatar_download_concurrency: int = 2
    avatar_request_timeout: float = 15
    avatar_warmup_limit: int = 100

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_production_safety(self) -> "Settings":
        if self.environment.strip().lower() != "production":
            return self

        errors: list[str] = []
        if self.seed_demo_data:
            errors.append("SEED_DEMO_DATA must be false")

        for variable, value in (
            ("DATABASE_URL", self.database_url),
            ("SYNC_DATABASE_URL", self.sync_database_url),
        ):
            scheme, separator, remainder = value.partition("://")
            parsed = urlsplit(f"postgresql://{remainder}") if separator else urlsplit("")
            if not scheme.startswith("postgresql"):
                errors.append(f"{variable} must use PostgreSQL")
            elif not parsed.hostname or not parsed.username or not parsed.password:
                errors.append(f"{variable} must include a host, username, and password")

        redis = urlsplit(self.redis_url)
        if redis.scheme not in {"redis", "rediss"} or not redis.hostname or not redis.password:
            errors.append("REDIS_URL must include a Redis host and password")

        for variable, token_value in (
            ("TRUSTED_PROXY_TOKEN", self.trusted_proxy_token),
            ("INTERNAL_SERVICE_TOKEN", self.internal_service_token),
        ):
            if not token_value or len(token_value) < 32 or token_value.startswith("CHANGE_ME"):
                errors.append(f"{variable} must be a random value of at least 32 characters")
        if self.trusted_proxy_token and self.trusted_proxy_token == self.internal_service_token:
            errors.append("TRUSTED_PROXY_TOKEN and INTERNAL_SERVICE_TOKEN must be different")

        if not self.cors_origins:
            errors.append("FRONTEND_ORIGINS must contain at least one HTTPS origin")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if parsed.scheme != "https" or not parsed.hostname:
                errors.append("FRONTEND_ORIGINS must contain only HTTPS origins")
                break
            if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
                errors.append("FRONTEND_ORIGINS must not contain localhost in production")
                break

        if errors:
            raise ValueError("Unsafe production configuration: " + "; ".join(errors))
        return self

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
