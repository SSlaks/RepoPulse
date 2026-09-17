import pytest
from app.config import Settings
from pydantic import ValidationError

PRODUCTION_SETTINGS = {
    "environment": "production",
    "seed_demo_data": False,
    "database_url": "postgresql+psycopg_async://repopulse:db-secret@postgres:5432/repopulse",
    "sync_database_url": "postgresql+psycopg://repopulse:db-secret@postgres:5432/repopulse",
    "redis_url": "redis://:redis-secret@redis:6379/0",
    "frontend_origins": "https://repopulse.example.com",
    "trusted_proxy_token": "test-proxy-token-01234567890123456789",
    "internal_service_token": "test-service-token-01234567890123456789",
}


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)


def test_development_configuration_keeps_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.database_url.startswith("sqlite+")
    assert settings.redis_url == "redis://localhost:6379/0"


def test_valid_production_configuration_is_accepted() -> None:
    settings = Settings(_env_file=None, **PRODUCTION_SETTINGS)

    assert settings.environment == "production"
    assert settings.cors_origins == ["https://repopulse.example.com"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"seed_demo_data": True}, "SEED_DEMO_DATA"),
        ({"database_url": "sqlite+aiosqlite:///./repopulse.db"}, "DATABASE_URL"),
        (
            {"sync_database_url": "postgresql+psycopg://repopulse@postgres:5432/repopulse"},
            "SYNC_DATABASE_URL",
        ),
        ({"redis_url": "redis://redis:6379/0"}, "REDIS_URL"),
        ({"frontend_origins": "http://localhost:3000"}, "FRONTEND_ORIGINS"),
        ({"frontend_origins": "https://localhost"}, "localhost"),
        ({"frontend_origins": ""}, "FRONTEND_ORIGINS"),
    ],
)
def test_unsafe_production_configuration_is_rejected(
    overrides: dict[str, object], expected: str
) -> None:
    values = {**PRODUCTION_SETTINGS, **overrides}

    with pytest.raises(ValidationError, match=expected):
        Settings(_env_file=None, **values)
