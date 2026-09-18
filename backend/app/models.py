from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    owner: Mapped[str] = mapped_column(String(120), index=True)
    owner_github_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    owner_avatar_url: Mapped[str | None] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(180))
    description: Mapped[str | None] = mapped_column(Text)
    html_url: Mapped[str] = mapped_column(String(500))
    language: Mapped[str | None] = mapped_column(String(80), index=True)
    topics: Mapped[list[str]] = mapped_column(JSON, default=list)
    license_name: Mapped[str | None] = mapped_column(String(120))
    stars_count: Mapped[int] = mapped_column(Integer, default=0)
    forks_count: Mapped[int] = mapped_column(Integer, default=0)
    open_issues_count: Mapped[int] = mapped_column(Integer, default=0)
    is_fork: Mapped[bool] = mapped_column(Boolean, default=False)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    availability_status: Mapped[str] = mapped_column(String(20), default="active")
    consecutive_not_found: Mapped[int] = mapped_column(Integer, default=0)
    first_not_found_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_not_found_date: Mapped[date | None] = mapped_column(Date)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    probe_backoff_step: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    github_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_tracked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    history_available_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    snapshots: Mapped[list["RepoSnapshot"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )
    ranking_items: Mapped[list["RankingItem"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )
    readme: Mapped["RepositoryReadme | None"] = relationship(
        back_populates="repository", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (Index("ix_repository_active_language", "archived", "language"),)


class RepositoryReadme(Base):
    __tablename__ = "repository_readmes"

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), unique=True, index=True
    )
    content: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str | None] = mapped_column(String(500))
    html_url: Mapped[str | None] = mapped_column(String(500))
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    visibility: Mapped[str] = mapped_column(String(20), default="unknown")
    metadata_etag: Mapped[str | None] = mapped_column(String(500))
    last_public_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    readme_etag: Mapped[str | None] = mapped_column(String(500))
    readme_endpoint: Mapped[str | None] = mapped_column(String(600))
    root_etag: Mapped[str | None] = mapped_column(String(500))
    root_entries: Mapped[list[dict] | None] = mapped_column(JSON)
    root_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    root_selected_path: Mapped[str | None] = mapped_column(String(500))

    repository: Mapped[Repository] = relationship(back_populates="readme")


class RepoSnapshot(Base):
    __tablename__ = "repo_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stars_count: Mapped[int] = mapped_column(Integer)
    forks_count: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(40), default="github_api")

    repository: Mapped[Repository] = relationship(back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint("repository_id", "snapshot_date", name="uq_repo_snapshot_date"),
    )


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    config_version: Mapped[str] = mapped_column(String(40), default="v1")
    discovered_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="running")
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RankingRun(Base):
    __tablename__ = "ranking_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    period_days: Mapped[int] = mapped_column(Integer, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    baseline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_version: Mapped[str] = mapped_column(String(40), default="v1")
    status: Mapped[str] = mapped_column(String(20), default="ready")
    collection_summary: Mapped[dict | None] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["RankingItem"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("period_days IN (1, 7, 14, 30)", name="ck_ranking_period"),
        UniqueConstraint("period_days", "as_of", "config_version", name="uq_ranking_run"),
    )


class RankingItem(Base):
    __tablename__ = "ranking_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    ranking_run_id: Mapped[int] = mapped_column(
        ForeignKey("ranking_runs.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    previous_rank: Mapped[int | None] = mapped_column(Integer)
    start_stars: Mapped[int] = mapped_column(Integer)
    end_stars: Mapped[int] = mapped_column(Integer)
    net_delta: Mapped[int] = mapped_column(Integer, index=True)
    growth_rate: Mapped[float | None] = mapped_column(Float)
    baseline_available: Mapped[bool] = mapped_column(Boolean, default=True)

    run: Mapped[RankingRun] = relationship(back_populates="items")
    repository: Mapped[Repository] = relationship(back_populates="ranking_items")

    __table_args__ = (
        UniqueConstraint("ranking_run_id", "repository_id", name="uq_ranking_repo"),
        UniqueConstraint("ranking_run_id", "rank", name="uq_ranking_position"),
    )


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_key: Mapped[str] = mapped_column(String(255), unique=True)
    task_name: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)


class SnapshotRequest(Base):
    __tablename__ = "snapshot_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_run_id: Mapped[int] = mapped_column(ForeignKey("job_runs.id", ondelete="CASCADE"))
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    availability_applied: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("job_run_id", "repository_id", name="uq_snapshot_request_repo"),
    )
