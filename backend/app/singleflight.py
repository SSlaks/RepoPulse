from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, assert_never

import anyio


class SingleFlightError(RuntimeError):
    """Base class for bounded single-flight coordination errors."""


class SingleFlightBusy(SingleFlightError):
    """Raised when a claim exceeds the in-flight or per-key waiter cap."""


class SingleFlightLoadFailed(SingleFlightError):
    """Raised to a follower when the shared producer failed."""

    def __init__(self, error_type: str | None = None) -> None:
        super().__init__(f"shared load failed: {error_type or 'unknown'}")
        self.error_type = error_type


class SingleFlightLoadCancelled(SingleFlightError):
    """Raised to a follower when the shared producer was cancelled (retryable)."""


class FlightState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class _Outcome:
    state: FlightState
    payload: bytes | None = None
    error_type: str | None = None


@dataclass(slots=True)
class _Flight:
    key: str
    event: anyio.Event
    waiters: int = 0
    outcome: _Outcome | None = None


@dataclass(frozen=True, slots=True)
class ProducerLease:
    """Opaque producer token completed or failed by its owning task."""

    key: str
    _flight: _Flight


@dataclass(slots=True)
class FollowerLease:
    """Opaque follower token that waits once for the producer outcome."""

    key: str
    _flight: _Flight
    _released: bool = False


class SingleFlight:
    """Bounded coordination merging concurrent loads for the same key.

    Every transition except ``wait`` is synchronous, so claim and terminal state
    cannot interleave with an ``await``. Producers own their task and session;
    this core never spawns tasks and never retains ORM state, sessions, or
    tracebacks. Call ``complete``/``fail`` only after the producer finished its
    own cleanup, so followers resume once the value and resources are final.

    Contract:

    - ``claim(key)`` is synchronous and returns a ``ProducerLease`` for a new
      key or a ``FollowerLease`` when the key already has an active producer.
      Existing keys join before the new-key cap is checked. Raises
      ``SingleFlightBusy`` when the in-flight or per-key waiter cap is exceeded,
      or after ``close``.
    - ``wait(lease, timeout=...)`` returns a freshly decoded ``dict`` payload,
      or raises ``SingleFlightLoadFailed`` (producer failed),
      ``SingleFlightLoadCancelled`` (producer cancelled, retryable once), or
      ``TimeoutError``. It always decrements the waiter count in ``finally``,
      and cancelling the follower never cancels the producer.
    - ``complete(lease, value)`` serializes once, stores immutable bytes,
      removes the exact flight, and wakes every waiter.
    - ``fail(lease, error)`` records failure or cancellation using only the
      exception type name, then removes and wakes.
    - ``release_follower(lease)`` is idempotent; ``wait`` calls it so callers
      that claimed a follower but will not wait can release it directly.
    """

    def __init__(self, *, max_inflight: int, max_waiters_per_key: int) -> None:
        self._max_inflight = max_inflight
        self._max_waiters_per_key = max_waiters_per_key
        self._flights: dict[str, _Flight] = {}
        self._closed = False

    def claim(self, key: str) -> ProducerLease | FollowerLease:
        if self._closed:
            raise SingleFlightBusy("single-flight is closed")
        flight = self._flights.get(key)
        if flight is not None:
            if flight.waiters >= self._max_waiters_per_key:
                raise SingleFlightBusy(f"waiter cap reached for {key!r}")
            flight.waiters += 1
            return FollowerLease(key=key, _flight=flight)
        if len(self._flights) >= self._max_inflight:
            raise SingleFlightBusy("in-flight load cap reached")
        new_flight = _Flight(key=key, event=anyio.Event())
        self._flights[key] = new_flight
        return ProducerLease(key=key, _flight=new_flight)

    async def wait(self, lease: FollowerLease, *, timeout: float) -> dict[str, Any]:
        try:
            with anyio.fail_after(timeout):
                await lease._flight.event.wait()
        finally:
            self.release_follower(lease)
        outcome = lease._flight.outcome
        if outcome is None:
            raise SingleFlightLoadFailed("load finished without an outcome")
        match outcome.state:
            case FlightState.SUCCEEDED:
                if outcome.payload is None:
                    raise SingleFlightLoadFailed("load succeeded without a payload")
                return json.loads(outcome.payload)
            case FlightState.CANCELLED:
                raise SingleFlightLoadCancelled()
            case FlightState.FAILED:
                raise SingleFlightLoadFailed(outcome.error_type)
            case unexpected:
                assert_never(unexpected)

    def complete(self, lease: ProducerLease, value: dict[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        self._finish(lease, _Outcome(state=FlightState.SUCCEEDED, payload=payload))

    def fail(self, lease: ProducerLease, error: BaseException) -> None:
        if isinstance(error, anyio.get_cancelled_exc_class()):
            outcome = _Outcome(state=FlightState.CANCELLED)
        else:
            outcome = _Outcome(state=FlightState.FAILED, error_type=type(error).__name__)
        self._finish(lease, outcome)

    def release_follower(self, lease: FollowerLease) -> None:
        if lease._released:
            return
        lease._released = True
        if lease._flight.waiters > 0:
            lease._flight.waiters -= 1

    def close(self) -> None:
        """Cancel every outstanding flight so waiters stop instead of hanging."""
        self._closed = True
        for flight in self._flights.values():
            flight.outcome = _Outcome(state=FlightState.CANCELLED)
            flight.event.set()
        self._flights.clear()

    def _finish(self, lease: ProducerLease, outcome: _Outcome) -> None:
        flight = lease._flight
        if flight.outcome is not None:
            return
        flight.outcome = outcome
        if self._flights.get(flight.key) is flight:
            del self._flights[flight.key]
        flight.event.set()
