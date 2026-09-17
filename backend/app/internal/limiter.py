from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
from dataclasses import dataclass
from typing import Any, Literal, cast

from fastapi import HTTPException, Request, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings

logger = logging.getLogger(__name__)

PROXY_CLIENT_IP_HEADER = "X-RepoPulse-Client-IP"
PROXY_TOKEN_HEADER = "X-RepoPulse-Proxy-Token"
INTERNAL_SERVICE_TOKEN_HEADER = "X-Internal-Service-Token"

LEASE_SECONDS = 60
RENEWAL_SECONDS = 15
LimitName = Literal["readme", "ai_generate", "ai_probe"]
LimitKind = Literal["external", "internal"]


@dataclass(frozen=True)
class LimitPolicy:
    name: LimitName
    window_seconds: int
    max_requests: int
    max_concurrent: int
    max_global_concurrent: int


LIMIT_POLICIES: dict[LimitName, LimitPolicy] = {
    "readme": LimitPolicy("readme", 60, 30, 2, 8),
    "ai_generate": LimitPolicy("ai_generate", 60, 6, 1, 4),
    "ai_probe": LimitPolicy("ai_probe", 60, 20, 2, 4),
}


class LimiterUnavailable(RuntimeError):
    """Redis could not make an admission decision."""


class LeaseDenied(RuntimeError):
    def __init__(self, retry_after: int, reason: str) -> None:
        super().__init__(reason)
        self.retry_after = max(1, retry_after)
        self.reason = reason


@dataclass
class Lease:
    policy: LimitName
    subject_key: str
    owner: str
    expires_at_ms: int

    @property
    def lease_id(self) -> str:
        return self.owner


def lease_from_payload(policy: LimitName, lease_id: str, subject: str) -> Lease:
    return Lease(
        policy=policy,
        subject_key=_subject_key(subject),
        owner=lease_id,
        expires_at_ms=0,
    )


class LimitAcquireRequest(BaseModel):
    policy: LimitName
    kind: Literal["internal"]


class LimitLeaseRequest(BaseModel):
    policy: LimitName
    lease_id: str = Field(min_length=20, max_length=256)
    kind: Literal["internal"]


class LimitLeaseResponse(BaseModel):
    lease_id: str
    expires_at: int
    lease_seconds: int = LEASE_SECONDS


_RATE_SCRIPT = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local window_ms = tonumber(ARGV[1])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window_ms)
local request_count = redis.call('ZCARD', KEYS[1])
if request_count >= tonumber(ARGV[2]) then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry = window_ms
  if #first >= 2 then retry = math.max(1, tonumber(first[2]) + window_ms - now) end
  return {0, 'rate', retry}
end

redis.call('ZADD', KEYS[1], now, ARGV[3])
redis.call('PEXPIRE', KEYS[1], window_ms + 1000)
return {1}
"""


_ACQUIRE_SCRIPT = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local window_ms = tonumber(ARGV[2])
local lease_ms = tonumber(ARGV[7])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window_ms)
local request_count = redis.call('ZCARD', KEYS[1])
if request_count >= tonumber(ARGV[3]) then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry = window_ms
  if #first >= 2 then retry = math.max(1, tonumber(first[2]) + window_ms - now) end
  return {0, 'rate', retry}
end

redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
local global_count = redis.call('ZCARD', KEYS[2])
if global_count >= tonumber(ARGV[5]) then
  local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
  local retry = lease_ms
  if #first >= 2 then retry = math.max(1, tonumber(first[2]) - now) end
  return {0, 'global_concurrency', retry}
end

local subject_count = redis.call('ZCARD', KEYS[3])
if subject_count >= tonumber(ARGV[4]) then
  local first = redis.call('ZRANGE', KEYS[3], 0, 0, 'WITHSCORES')
  local retry = lease_ms
  if #first >= 2 then retry = math.max(1, tonumber(first[2]) - now) end
  return {0, 'ip_concurrency', retry}
end

local owner = ARGV[6]
local expires_at = now + lease_ms
redis.call('ZADD', KEYS[1], now, owner)
redis.call('ZADD', KEYS[2], expires_at, owner)
redis.call('ZADD', KEYS[3], expires_at, owner)
redis.call('HSET', KEYS[4], owner, ARGV[8])
redis.call('PEXPIRE', KEYS[1], window_ms + 1000)
redis.call('PEXPIRE', KEYS[2], lease_ms + 1000)
redis.call('PEXPIRE', KEYS[3], lease_ms + 1000)
redis.call('PEXPIRE', KEYS[4], lease_ms + 1000)
return {1, expires_at, request_count + 1}
"""

_RENEW_SCRIPT = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local owner = ARGV[1]
local expected_subject = ARGV[2]
local stored_subject = redis.call('HGET', KEYS[3], owner)
if not stored_subject or stored_subject ~= expected_subject then
  return {0}
end
local current = redis.call('ZSCORE', KEYS[1], owner)
if not current or tonumber(current) <= now then
  redis.call('ZREM', KEYS[1], owner)
  redis.call('ZREM', KEYS[2], owner)
  redis.call('HDEL', KEYS[3], owner)
  return {0}
end
local expires_at = now + tonumber(ARGV[3])
redis.call('ZADD', KEYS[1], expires_at, owner)
redis.call('ZADD', KEYS[2], expires_at, owner)
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]) + 1000)
redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[3]) + 1000)
redis.call('PEXPIRE', KEYS[3], tonumber(ARGV[3]) + 1000)
return {1, expires_at}
"""

_RELEASE_SCRIPT = """
local owner = ARGV[1]
local expected_subject = ARGV[2]
local stored_subject = redis.call('HGET', KEYS[3], owner)
if not stored_subject or stored_subject ~= expected_subject then
  return 0
end
local removed = redis.call('ZREM', KEYS[1], owner)
redis.call('ZREM', KEYS[2], owner)
redis.call('HDEL', KEYS[3], owner)
return removed
"""


def policy_for(name: str) -> LimitPolicy:
    try:
        return LIMIT_POLICIES[cast(LimitName, name)]
    except KeyError:
        raise ValueError(f"Unknown limiter policy: {name}")


def _subject_key(subject: str) -> str:
    value = subject.strip()
    if not value or len(value) > 256:
        raise ValueError("Invalid limiter subject")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _keys(policy: LimitName, subject_key: str) -> tuple[str, str, str, str]:
    tag = f"{{{policy}}}"
    prefix = f"repopulse:limiter:v1:{tag}"
    return (
        f"{prefix}:requests:{subject_key}",
        f"{prefix}:leases",
        f"{prefix}:leases:{subject_key}",
        f"{prefix}:owners",
    )


class RedisLimiter:
    def __init__(self) -> None:
        settings = get_settings()
        self._client: Redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    async def acquire(self, policy_name: LimitName, subject: str) -> Lease:
        policy = policy_for(policy_name)
        subject_key = _subject_key(subject)
        owner = secrets.token_urlsafe(32)
        try:
            result = await cast(Any, self._client).eval(
                _ACQUIRE_SCRIPT,
                4,
                *_keys(policy.name, subject_key),
                str(policy.window_seconds * 1000),
                str(policy.window_seconds * 1000),
                str(policy.max_requests),
                str(policy.max_concurrent),
                str(policy.max_global_concurrent),
                owner,
                str(LEASE_SECONDS * 1000),
                subject_key,
            )
        except RedisError as exc:
            raise LimiterUnavailable("Redis limiter unavailable") from exc
        if not result:
            raise LimiterUnavailable("Redis limiter returned an empty decision")
        if int(result[0]) != 1:
            retry_ms = int(float(result[2])) if len(result) > 2 else LEASE_SECONDS * 1000
            reason = str(result[1]) if len(result) > 1 else "rate"
            raise LeaseDenied(math.ceil(retry_ms / 1000), reason)
        return Lease(
            policy=policy.name,
            subject_key=subject_key,
            owner=owner,
            expires_at_ms=int(result[1]),
        )

    async def record(self, policy_name: LimitName, subject: str) -> None:
        policy = policy_for(policy_name)
        subject_key = _subject_key(subject)
        owner = secrets.token_urlsafe(16)
        request_key, _, _, _ = _keys(policy.name, subject_key)
        try:
            result = await cast(Any, self._client).eval(
                _RATE_SCRIPT,
                1,
                request_key,
                str(policy.window_seconds * 1000),
                str(policy.max_requests),
                owner,
            )
        except RedisError as exc:
            raise LimiterUnavailable("Redis limiter unavailable") from exc
        if not result:
            raise LimiterUnavailable("Redis limiter returned an empty decision")
        if int(result[0]) != 1:
            retry_ms = int(float(result[2])) if len(result) > 2 else policy.window_seconds * 1000
            raise LeaseDenied(math.ceil(retry_ms / 1000), "rate")

    async def renew(self, lease: Lease) -> bool:
        policy_for(lease.policy)
        try:
            result = await cast(Any, self._client).eval(
                _RENEW_SCRIPT,
                3,
                *_keys(lease.policy, lease.subject_key)[1:],
                lease.owner,
                lease.subject_key,
                str(LEASE_SECONDS * 1000),
            )
        except RedisError as exc:
            raise LimiterUnavailable("Redis limiter unavailable") from exc
        if not result or int(result[0]) != 1:
            return False
        lease.expires_at_ms = int(result[1])
        return True

    async def release(self, lease: Lease) -> bool:
        policy_for(lease.policy)
        _, global_key, subject_key, owners_key = _keys(lease.policy, lease.subject_key)
        try:
            result = await cast(Any, self._client).eval(
                _RELEASE_SCRIPT,
                3,
                global_key,
                subject_key,
                owners_key,
                lease.owner,
                lease.subject_key,
            )
        except RedisError as exc:
            raise LimiterUnavailable("Redis limiter unavailable") from exc
        return bool(result and int(result) == 1)

    async def close(self) -> None:
        await self._client.aclose()


limiter = RedisLimiter()


def _proxy_failure(message: str = "可信代理身份不可用") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=message,
        headers={"Cache-Control": "no-store", "Retry-After": "5"},
    )


def resolve_client_identity(request: Request) -> str:
    settings = get_settings()
    received_token = request.headers.get(PROXY_TOKEN_HEADER, "")
    received_ip = request.headers.get(PROXY_CLIENT_IP_HEADER, "")
    is_production = settings.environment.strip().lower() == "production"

    if received_token or received_ip:
        configured_token = settings.trusted_proxy_token or ""
        if not configured_token or not hmac.compare_digest(received_token, configured_token):
            if is_production:
                raise _proxy_failure()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="可信代理身份无效")
        try:
            return str(ipaddress.ip_address(received_ip))
        except ValueError as exc:
            if is_production:
                raise _proxy_failure() from exc
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="客户端地址无效") from exc

    if is_production:
        raise _proxy_failure()
    return (request.client.host if request.client and request.client.host else "local").strip()


def resolve_limit_subject(request: Request, kind: LimitKind) -> str:
    if kind == "internal":
        has_proxy_context = bool(
            request.headers.get(PROXY_CLIENT_IP_HEADER)
            or request.headers.get(PROXY_TOKEN_HEADER)
        )
        if not has_proxy_context:
            return "internal"
    identity = resolve_client_identity(request)
    return identity


def require_internal_service(request: Request) -> None:
    configured_token = get_settings().internal_service_token or ""
    received_token = request.headers.get(INTERNAL_SERVICE_TOKEN_HEADER, "")
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="内部服务未配置",
            headers={"Cache-Control": "no-store", "Retry-After": "5"},
        )
    if not received_token or not hmac.compare_digest(received_token, configured_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="禁止访问内部接口")


def limit_failure_response(exc: LeaseDenied) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="请求过于频繁，请稍后重试",
        headers={"Cache-Control": "no-store", "Retry-After": str(exc.retry_after)},
    )


def limiter_failure_response() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="限流服务暂时不可用，请稍后重试",
        headers={"Cache-Control": "no-store", "Retry-After": "5"},
    )
