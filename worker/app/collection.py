"""Bounded network workers; ORM objects never cross the thread boundary."""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, Lock, local
from time import monotonic, time

import httpx
from app.clients.github import (
    GitHubAuthenticationError,
    GitHubClient,
    GitHubRateLimitError,
    GitHubRepositoryData,
    GitHubTransientError,
)


class RepositoryRequests:
    def __init__(
        self, concurrency: int, requests_per_second: float,
        client_factory: Callable[[], GitHubClient] = GitHubClient,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=concurrency)
        self._factory = client_factory
        self._local = local()
        self._clients: list[GitHubClient] = []
        self._lock = Lock()
        self._interval = 1 / requests_per_second
        self._next_start = 0.0
        self.stopped = Event()
        self.rate_limit: GitHubRateLimitError | None = None
        self.authentication_error: GitHubAuthenticationError | None = None

    def submit(self, name: str) -> Future[GitHubRepositoryData | None]:
        return self._executor.submit(self._fetch, name)

    def stop(self) -> None:
        self.stopped.set()

    def close(self) -> None:
        self.stop()
        self._executor.shutdown(wait=True, cancel_futures=True)
        for client in self._clients:
            client.close()

    def _client(self) -> GitHubClient:
        if not hasattr(self._local, "client"):
            self._local.client = self._factory()
            with self._lock:
                self._clients.append(self._local.client)
        return self._local.client

    def _admit(self) -> bool:
        # Admit at actual start time, rather than reserving slots which retries can overtake.
        while not self.stopped.is_set():
            with self._lock:
                delay = self._next_start - monotonic()
                if delay <= 0:
                    self._next_start = monotonic() + self._interval
                    return True
            self.stopped.wait(delay)
        return False

    def _set_rate_limit(self, error: GitHubRateLimitError) -> None:
        with self._lock:
            previous = self.rate_limit
            delay = max(error.retry_after, previous.retry_after if previous else 0)
            secondary = error.secondary or bool(previous and previous.secondary)
            self.rate_limit = GitHubRateLimitError(str(error), delay, secondary)
            self.stop()

    def _observe_quota(self, client: GitHubClient) -> None:
        if client.rate_limit_remaining == 0 and client.rate_limit_reset is not None:
            self._set_rate_limit(GitHubRateLimitError(
                "GitHub API quota exhausted", client.rate_limit_reset - time() + 1,
            ))

    def _fetch(self, name: str) -> GitHubRepositoryData | None:
        client = self._client()
        for attempt in range(4):
            if not self._admit():
                return None
            try:
                data = client.repository(name)
                self._observe_quota(client)
                return data
            except GitHubRateLimitError as exc:
                self._set_rate_limit(exc)
                raise
            except GitHubAuthenticationError as exc:
                self.authentication_error = exc
                self.stop()
                raise
            except (httpx.RequestError, GitHubTransientError):
                if attempt == 3:
                    raise
                if self.stopped.wait(2 ** attempt):
                    return None
        return None
