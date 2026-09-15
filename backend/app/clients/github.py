import base64
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


@dataclass(frozen=True)
class GitHubReadmeData:
    repository: str
    path: str
    content: str
    html_url: str


class GitHubClient:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        settings = get_settings()
        self._tokens = cycle(settings.github_tokens or [""])
        self._token_lock = Lock()
        self._etag_cache: dict[str, tuple[str, Any]] = {}
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
        license_data = payload.get("license") or {}
        owner = payload.get("owner") or {}
        owner_id = owner.get("id")
        return GitHubRepositoryData(
            github_id=int(payload["id"]),
            owner_github_id=int(owner_id) if owner_id is not None else None,
            owner_avatar_url=owner.get("avatar_url"),
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
        )

    def readme(self, full_name: str) -> GitHubReadmeData:
        root_entries = self._readme_root_entries(full_name)
        readme_path = self._select_readme_path(root_entries)
        if readme_path:
            path = quote(readme_path, safe="/")
            payload = self._get_json(f"/repos/{full_name}/contents/{path}")
        else:
            payload = self._get_json(f"/repos/{full_name}/readme")

        return GitHubReadmeData(
            repository=full_name,
            path=str(payload.get("path") or "README.md"),
            content=self._decode_readme_content(payload),
            html_url=str(payload.get("html_url") or f"https://github.com/{full_name}"),
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
        headers = self._auth_headers()
        cache_key = f"{path}:{params or {}}"
        cached = self._etag_cache.get(cache_key)
        if cached:
            headers["If-None-Match"] = cached[0]
        response = self._client.get(path, params=params, headers=headers)
        try:
            self.rate_limit_remaining = int(response.headers["x-ratelimit-remaining"])
            self.rate_limit_reset = float(response.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            self.rate_limit_remaining = None
            self.rate_limit_reset = None
        if response.status_code == 304 and cached:
            return cached[1]
        self._raise_for_status(response)
        payload = response.json()
        if not isinstance(payload, (dict, list)):
            raise GitHubClientError("GitHub API returned an unexpected response")
        if etag := response.headers.get("etag"):
            self._etag_cache[cache_key] = (etag, payload)
        return payload

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
