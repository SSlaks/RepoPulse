from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.avatar_cache import avatar_path, is_fresh
from app.database import async_session_factory, get_session
from app.internal.limiter import (
    LeaseDenied,
    LimitAcquireRequest,
    LimiterUnavailable,
    LimitLeaseRequest,
    LimitLeaseResponse,
    lease_from_payload,
    limit_failure_response,
    limiter,
    limiter_failure_response,
    require_internal_service,
    resolve_client_identity,
    resolve_limit_subject,
)
from app.models import Repository
from app.schemas import ChartRange, ReadmeResponse, RepositoryResponse, SnapshotSeriesResponse
from app.services.catalog import CatalogService
from app.task_queue import enqueue_avatar_refresh

router = APIRouter(tags=["repositories"])
internal_router = APIRouter(prefix="/internal/limits", tags=["internal"])


@router.get("/avatars/{owner_id}")
async def get_avatar(owner_id: int, background_tasks: BackgroundTasks) -> Response:
    path = avatar_path(owner_id)
    if path.exists():
        if not is_fresh(path):
            background_tasks.add_task(enqueue_avatar_refresh, owner_id)
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600", "ETag": str(path.stat().st_mtime_ns)},
        )
    async with async_session_factory() as session:
        known = await session.scalar(
            select(Repository.owner_avatar_url).where(Repository.owner_github_id == owner_id)
        )
    if known:
        background_tasks.add_task(enqueue_avatar_refresh, owner_id)
    return Response(status_code=404, headers={"Cache-Control": "no-store"})


@router.get("/repos/{owner}/{name}", response_model=RepositoryResponse)
async def get_repository(
    owner: str,
    name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RepositoryResponse:
    return await CatalogService(session).repository(owner, name)


@router.get("/repos/{owner}/{name}/readme", response_model=ReadmeResponse)
async def get_repository_readme(
    owner: str,
    name: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ReadmeResponse:
    return await CatalogService(session).readme(
        owner,
        name,
        client_identity=resolve_client_identity(request),
    )


@router.get("/repos/{owner}/{name}/snapshots", response_model=SnapshotSeriesResponse)
async def get_repository_snapshots(
    owner: str,
    name: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    range_name: Annotated[ChartRange, Query(alias="range")] = "90d",
) -> SnapshotSeriesResponse:
    return await CatalogService(session).snapshot_series(owner, name, range_name)


@internal_router.post("/acquire", response_model=LimitLeaseResponse)
async def acquire_limit(request: Request, payload: LimitAcquireRequest) -> LimitLeaseResponse:
    require_internal_service(request)
    subject = resolve_limit_subject(request, payload.kind)
    try:
        lease = await limiter.acquire(payload.policy, subject)
    except LeaseDenied as exc:
        raise limit_failure_response(exc) from exc
    except LimiterUnavailable as exc:
        raise limiter_failure_response() from exc
    return LimitLeaseResponse(lease_id=lease.lease_id, expires_at=lease.expires_at_ms)


@internal_router.post("/renew", response_model=LimitLeaseResponse)
async def renew_limit(request: Request, payload: LimitLeaseRequest) -> LimitLeaseResponse:
    require_internal_service(request)
    subject = resolve_limit_subject(request, payload.kind)
    lease = lease_from_payload(payload.policy, payload.lease_id, subject)
    try:
        renewed = await limiter.renew(lease)
    except LimiterUnavailable as exc:
        raise limiter_failure_response() from exc
    if not renewed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="租约已失效",
            headers={"Cache-Control": "no-store"},
        )
    return LimitLeaseResponse(lease_id=lease.lease_id, expires_at=lease.expires_at_ms)


@internal_router.post("/release", status_code=status.HTTP_204_NO_CONTENT)
async def release_limit(request: Request, payload: LimitLeaseRequest) -> Response:
    require_internal_service(request)
    subject = resolve_limit_subject(request, payload.kind)
    lease = lease_from_payload(payload.policy, payload.lease_id, subject)
    try:
        await limiter.release(lease)
    except LimiterUnavailable as exc:
        raise limiter_failure_response() from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
