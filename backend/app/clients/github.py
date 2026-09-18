import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import cycle
from threading import Lock
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from app.config import get_settings


class GitHubClientError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class GitHubNotFoundError(GitHubClientError):
    pass


class GitHubAuthenticationError(GitHubClientError):
    pass


class GitHubPermissionError(GitHubClientError):
    pass


class GitHubTransientError(GitHubClientError):
    pass


class GitHubRateLimitError(GitHubClientError):
    def __init__(self, message: str, retry_after: float = 60, secondary: bool = False):
        super().__init__(message)
        self.retry_after = max(1, retry_after)
        self.secondary = secondary


def _retry_after(response: httpx.Response) -> float:
    now = datetime.now(UTC).timestamp()
    value = response.headers.get("retry-after")
    if value:
        try:
            return max(1, float(value))
        except ValueError:
            try:
                return max(1, parsedate_to_datetime(value).timestamp() - now)
            except (ValueError, TypeError):
                pass
    if response.headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(1, float(response.headers["x-ratelimit-reset"]) - now + 1)
        except (KeyError, ValueError):
            pass
    return 60


@dataclass(frozen=True)
class GitHubRepositoryData:
    github_id: int
    full_name: str
    description: str | None
    html_url: str
    language: str | None
    topics: list[str]
    license_name: str | None
    stars_count: int
    forks_count: int
    open_issues_count: int
    is_fork: bool
    archived: bool
    disabled: bool
    pushed_at: str | None
    created_at: str | None
    owner_github_id: int | None = None
    owner_avatar_url: str | None = None
    is_private: bool = False
    visibility: str | None = "public"
    etag: str | None = None


@dataclass(frozen=True)
class GitHubReadmeData:
    repository: str
    path: str
    content: str
    html_url: str
    etag: str | None = None
    root_etag: str | None = None
    root_entries: list[dict[str, Any]] | None = None
    not_modified: bool = False
    readme_endpoint: str | None = None


@dataclass(frozen=True)
class GitHubRateLimitSnapshot:
    """The authenticated core quota observed by a worker request."""

    remaining: int | None
    reset_at: float | None


class GitHubClient:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        settings = get_settings()
        self._tokens = cycle(settings.github_tokens or [""])
        self._token_lock = Lock()
        self._etag_cache: dict[str, tuple[str, Any]] = {}
        self._last_response_etag: str | None = None
        self._request_guard: Callable[[], None] | None = None
        self._request_pacer: Callable[[], None] | None = None
        self.rate_limit_remaining: int | None = None
        self.rate_limit_reset: float | None = None
        self._client = httpx.Client(
            base_url="https://api.github.com",
            timeout=20,
            follow_redirects=True,
            transport=transport,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "RepoPulse/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def trending(self, since: str) -> list[str]:
        response = self._client.get(
            "https://github.com/trending",
            params={"since": since},
            headers={"User-Agent": "RepoPulse/0.1"},
        )
        self._raise_for_status(response)
        soup = BeautifulSoup(response.text, "html.parser")
        repositories: list[str] = []
        for article in soup.select("article.Box-row"):
            link = article.select_one("h2 a")
            if link and link.get("href"):
                repositories.append(str(link["href"]).strip().strip("/").replace(" ", ""))
        return repositories

    def search(self, query: str, per_page: int = 100, max_pages: int = 1) -> list[str]:
        repositories: list[str] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            payload = self._get_json(
                "/search/repositories",
                params={
                    "q": query,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": per_page,
                    "page": page,
                },
            )
            items = payload.get("items", [])
            for item in items:
                full_name = str(item["full_name"])
                if full_name not in seen:
                    repositories.append(full_name)
                    seen.add(full_name)
            if len(items) < per_page or len(repositories) >= int(payload.get("total_count", 0)):
                break
        return repositories

    def repository(self, full_name: str) -> GitHubRepositoryData:
        payload = self._get_json(f"/repos/{full_name}")
        if not isinstance(payload, dict) or not payload:
            raise GitHubClientError("GitHub repository metadata unavailable")
        return self._repository_data(payload, self._last_response_etag)

    def repository_conditional(
        self, full_name: str, *, etag: str | None = None
    ) -> tuple[GitHubRepositoryData | None, str | None, bool]:
        payload, observed_etag, not_modified = self._get_conditional_payload(
            f"/repos/{full_name}", etag=etag
        )
        if not_modified:
            return None, observed_etag or etag, True
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub repository metadata unavailable")
        return self._repository_data(payload, observed_etag), observed_etag, False

    @staticmethod
    def _repository_data(
        payload: dict[str, Any], etag: str | None = None
    ) -> GitHubRepositoryData:
        license_data = payload.get("license") or {}
        owner = payload.get("owner") or {}
        owner_id = owner.get("id")
        return GitHubRepositoryData(
            github_id=int(payload["id"]),
            owner_github_id=int(owner_id) if owner_id is not None else None,
            owner_avatar_url=owner.get("avatar_url"),
            is_private=bool(payload.get("private", False)),
            visibility=str(payload.get("visibility") or "public"),
            full_name=str(payload["full_name"]),
            description=payload.get("description"),
            html_url=str(payload["html_url"]),
            language=payload.get("language"),
            topics=list(payload.get("topics", [])),
            license_name=license_data.get("spdx_id"),
            stars_count=int(payload.get("stargazers_count", 0)),
            forks_count=int(payload.get("forks_count", 0)),
            open_issues_count=int(payload.get("open_issues_count", 0)),
            is_fork=bool(payload.get("fork", False)),
            archived=bool(payload.get("archived", False)),
            disabled=bool(payload.get("disabled", False)),
            pushed_at=payload.get("pushed_at"),
            created_at=payload.get("created_at"),
            etag=etag,
        )

    def readme(self, full_name: str) -> GitHubReadmeData:
        return self.readme_conditional(full_name)

    def readme_conditional(
        self,
        full_name: str,
        *,
        root_etag: str | None = None,
        root_entries: list[dict[str, Any]] | None = None,
        readme_etag: str | None = None,
        cached_path: str | None = None,
        cached_endpoint: str | None = None,
    ) -> GitHubReadmeData:
        """Read a README while allowing callers to persist both validators.

        The root listing validator and the selected file validator are kept
        separate because GitHub can rename the preferred Chinese README while
        the old file's ETag remains valid.  A 304 is surfaced to the caller so
        the database can retain its existing body without rewriting it.
        """
        entries, observed_root_etag, root_not_modified = self._readme_root_entries_conditional(
            full_name,
            etag=root_etag,
            cached_entries=root_entries,
        )
        readme_path = self._select_readme_path(entries)
        if readme_path is None and root_not_modified and cached_path and cached_endpoint != "readme":
            readme_path = cached_path

        if readme_path:
            path = quote(readme_path, safe="/")
            # Never send the old file validator to a newly selected path.
            endpoint = f"contents:{readme_path}"
            validator = readme_etag if endpoint == cached_endpoint else None
            payload, observed_etag, not_modified = self._get_conditional_payload(
                f"/repos/{full_name}/contents/{path}", etag=validator
            )
        else:
            endpoint = "readme"
            payload, observed_etag, not_modified = self._get_conditional_payload(
                f"/repos/{full_name}/readme", etag=readme_etag if endpoint == cached_endpoint else None
            )
            readme_path = cached_path or "README.md"

        if not_modified:
            return GitHubReadmeData(
                repository=full_name,
                path=readme_path,
                content="",
                html_url=f"https://github.com/{full_name}/blob/HEAD/{readme_path}",
                etag=observed_etag or readme_etag,
                root_etag=observed_root_etag or root_etag,
                root_entries=entries,
                not_modified=True,
                readme_endpoint=endpoint,
            )
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub API returned an unexpected README response")
        actual_path = str(payload.get("path") or readme_path)
        return GitHubReadmeData(
            repository=full_name,
            path=actual_path,
            content=self._decode_readme_content(payload),
            html_url=str(payload.get("html_url") or f"https://github.com/{full_name}"),
            etag=observed_etag,
            root_etag=observed_root_etag,
            root_entries=entries,
            readme_endpoint=endpoint,
        )

    def _readme_root_entries(self, full_name: str) -> list[dict[str, Any]]:
        try:
            payload = self._get_payload(f"/repos/{full_name}/contents")
        except GitHubRateLimitError:
            raise
        except GitHubClientError:
            return []
        if not isinstance(payload, list):
            return []
        return [entry for entry in payload if isinstance(entry, dict)]

    def _readme_root_entries_conditional(
        self,
        full_name: str,
        *,
        etag: str | None,
        cached_entries: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        try:
            payload, observed_etag, not_modified = self._get_conditional_payload(
                f"/repos/{full_name}/contents", etag=etag
            )
        except GitHubRateLimitError:
            raise
        except GitHubClientError:
            return cached_entries or [], etag, False
        if not_modified:
            return cached_entries or [], observed_etag or etag, True
        if not isinstance(payload, list):
            return [], observed_etag, False
        entries = [entry for entry in payload if isinstance(entry, dict)]
        return entries, observed_etag, False

    @staticmethod
    def _select_readme_path(entries: list[dict[str, Any]]) -> str | None:
        preferred_names = (
            "readme.zh-cn.md",
            "readme.zh.md",
            "readme.zhhans.md",
            "readme_cn.md",
            "readme-cn.md",
        )
        entries_by_name = {
            str(entry.get("name", "")).lower(): str(entry["name"])
            for entry in entries
            if entry.get("type") == "file" and entry.get("name")
        }
        return next(
            (entries_by_name[name] for name in preferred_names if name in entries_by_name),
            None,
        )

    @staticmethod
    def _decode_readme_content(payload: dict[str, Any]) -> str:
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise GitHubClientError("GitHub README content unavailable")
        encoding = str(payload.get("encoding", "")).lower()
        if encoding != "base64":
            return content
        try:
            return base64.b64decode("".join(content.split())).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubClientError("GitHub README content is invalid") from exc

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self._get_payload(path, params)
        if not isinstance(payload, dict):
            raise GitHubClientError("GitHub API returned an unexpected response")
        return payload

    def _get_payload(self, path: str, params: dict[str, Any] | None = None) -> Any:
        cache_key = f"{path}:{params or {}}"
        cached = self._etag_cache.get(cache_key)
        payload, etag, not_modified = self._get_conditional_payload(
            path, params=params, etag=cached[0] if cached else None
        )
        if not_modified and cached:
            return cached[1]
        if payload is None:
            raise GitHubClientError("GitHub returned 304 without a cached response")
        if not isinstance(payload, (dict, list)):
            raise GitHubClientError("GitHub API returned an unexpected response")
        if etag:
            self._etag_cache[cache_key] = (etag, payload)
        return payload

    def _get_conditional_payload(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        etag: str | None = None,
    ) -> tuple[Any | None, str | None, bool]:
        if self._request_guard is not None:
            self._request_guard()
        if self._request_pacer is not None:
            self._request_pacer()
        headers = self._auth_headers()
        if etag:
            headers["If-None-Match"] = etag
        response = self._client.get(path, params=params, headers=headers)
        self._observe_rate_limit(response)
        if response.status_code == 304:
            observed_etag = response.headers.get("etag") or etag
            self._last_response_etag = observed_etag
            return None, observed_etag, True
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise GitHubClientError("GitHub API returned an unexpected response")
        observed_etag = response.headers.get("etag")
        self._last_response_etag = observed_etag
        return payload, observed_etag, False

    def probe_rate_limit(self) -> GitHubRateLimitSnapshot:
        """Probe the authenticated core bucket before a README batch."""
        payload = self._get_json("/rate_limit")
        resources = payload.get("resources") or {}
        core = resources.get("core") or {}
        remaining = core.get("remaining")
        reset_at = core.get("reset")
        if remaining is not None:
            self.rate_limit_remaining = int(remaining)
        if reset_at is not None:
            self.rate_limit_reset = float(reset_at)
        return GitHubRateLimitSnapshot(
            remaining=int(remaining) if remaining is not None else self.rate_limit_remaining,
            reset_at=float(reset_at) if reset_at is not None else self.rate_limit_reset,
        )

    def has_quota(self, reserve: int) -> bool:
        return self.rate_limit_remaining is not None and self.rate_limit_remaining >= reserve

    def set_request_guard(self, guard: Callable[[], None] | None) -> None:
        """Install a worker supplied quota check before every HTTP request."""
        self._request_guard = guard

    def set_request_pacer(self, pacer: Callable[[], None] | None) -> None:
        """Install a worker supplied low-rate gate before every HTTP request."""
        self._request_pacer = pacer

    def _observe_rate_limit(self, response: httpx.Response) -> None:
        try:
            self.rate_limit_remaining = int(response.headers["x-ratelimit-remaining"])
            self.rate_limit_reset = float(response.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            self.rate_limit_remaining = None
            self.rate_limit_reset = None

    def _auth_headers(self) -> dict[str, str]:
        with self._token_lock:
            token = next(self._tokens)
        return {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        exhausted = response.headers.get("x-ratelimit-remaining") == "0"
        limited = response.status_code == 429 or (
            response.status_code == 403
            and (
                exhausted
                or "retry-after" in response.headers
                or "rate limit" in response.text.lower()
                or "abuse" in response.text.lower()
            )
        )
        if limited:
            raise GitHubRateLimitError(
                "GitHub API rate limit reached", _retry_after(response), secondary=not exhausted
            )
        if response.status_code >= 500:
            raise GitHubTransientError(f"GitHub request failed: {response.status_code}",
                                       response.status_code)
        errors = {401: GitHubAuthenticationError, 403: GitHubPermissionError,
                  404: GitHubNotFoundError}
        if error := errors.get(response.status_code):
            raise error(f"GitHub request failed: {response.status_code}", response.status_code)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubClientError(f"GitHub request failed: {response.status_code}",
                                    response.status_code) from exc
