from __future__ import annotations

from typing import Any

import anyio
import pytest
from app.singleflight import (
    FollowerLease,
    ProducerLease,
    SingleFlight,
    SingleFlightBusy,
    SingleFlightLoadCancelled,
    SingleFlightLoadFailed,
)


def build_flight(*, max_inflight: int = 32, max_waiters_per_key: int = 64) -> SingleFlight:
    return SingleFlight(max_inflight=max_inflight, max_waiters_per_key=max_waiters_per_key)


def as_producer(lease: ProducerLease | FollowerLease) -> ProducerLease:
    assert isinstance(lease, ProducerLease)
    return lease


def as_follower(lease: ProducerLease | FollowerLease) -> FollowerLease:
    assert isinstance(lease, FollowerLease)
    return lease


def waiter_count(flight: SingleFlight, key: str) -> int:
    return flight._flights[key].waiters


def test_new_key_is_a_producer_and_existing_key_joins() -> None:
    flight = build_flight()

    assert isinstance(flight.claim("k"), ProducerLease)
    assert isinstance(flight.claim("k"), FollowerLease)


async def test_fifty_followers_on_one_key_share_a_single_outcome() -> None:
    flight = build_flight(max_inflight=32, max_waiters_per_key=64)
    producer = as_producer(flight.claim("k"))
    followers = [as_follower(flight.claim("k")) for _ in range(50)]
    assert len(followers) == 50

    results: list[dict[str, Any]] = []

    async def consume(follower: FollowerLease) -> None:
        results.append(await flight.wait(follower, timeout=5))

    async with anyio.create_task_group() as group:
        for follower in followers:
            group.start_soon(consume, follower)
        flight.complete(producer, {"ok": True})

    assert results == [{"ok": True}] * 50


def test_existing_key_joins_before_the_new_key_cap() -> None:
    flight = build_flight(max_inflight=2, max_waiters_per_key=64)
    as_producer(flight.claim("a"))
    as_producer(flight.claim("b"))

    with pytest.raises(SingleFlightBusy):
        flight.claim("c")

    assert isinstance(flight.claim("a"), FollowerLease)


def test_thirty_third_new_key_is_busy() -> None:
    flight = build_flight(max_inflight=32, max_waiters_per_key=64)
    for index in range(32):
        as_producer(flight.claim(f"k{index}"))

    with pytest.raises(SingleFlightBusy):
        flight.claim("k32")


async def test_different_keys_are_isolated() -> None:
    flight = build_flight()
    producer_a = as_producer(flight.claim("a"))
    producer_b = as_producer(flight.claim("b"))
    follower_a = as_follower(flight.claim("a"))

    results: list[dict[str, Any]] = []

    async def consume() -> None:
        results.append(await flight.wait(follower_a, timeout=5))

    async with anyio.create_task_group() as group:
        group.start_soon(consume)
        flight.complete(producer_a, {"a": 1})

    assert results == [{"a": 1}]
    assert isinstance(flight.claim("b"), FollowerLease)
    assert producer_b.key == "b"


def test_per_key_waiter_cap_is_enforced() -> None:
    flight = build_flight(max_inflight=32, max_waiters_per_key=2)
    as_producer(flight.claim("k"))
    as_follower(flight.claim("k"))
    as_follower(flight.claim("k"))

    with pytest.raises(SingleFlightBusy):
        flight.claim("k")


async def test_follower_timeout_releases_the_waiter_slot() -> None:
    flight = build_flight(max_inflight=32, max_waiters_per_key=1)
    as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))

    with pytest.raises(TimeoutError):
        await flight.wait(follower, timeout=0.01)

    assert waiter_count(flight, "k") == 0
    assert isinstance(flight.claim("k"), FollowerLease)


async def test_follower_cancellation_spares_the_producer_and_releases_the_slot() -> None:
    flight = build_flight(max_inflight=32, max_waiters_per_key=4)
    producer = as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))
    entered = anyio.Event()
    scopes: list[anyio.CancelScope] = []

    async def wait_follower() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            entered.set()
            await flight.wait(follower, timeout=30)

    async def cancel_follower() -> None:
        await entered.wait()
        scopes[0].cancel()

    async with anyio.create_task_group() as group:
        group.start_soon(wait_follower)
        group.start_soon(cancel_follower)

    assert waiter_count(flight, "k") == 0
    flight.complete(producer, {"v": 1})


async def test_failure_retains_only_the_exception_type_and_allows_reacquire() -> None:
    flight = build_flight()
    producer = as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))
    errors: list[str | None] = []

    async def wait_follower() -> None:
        try:
            await flight.wait(follower, timeout=5)
        except SingleFlightLoadFailed as exc:
            errors.append(exc.error_type)

    async with anyio.create_task_group() as group:
        group.start_soon(wait_follower)
        flight.fail(producer, ValueError("secret detail"))

    assert errors == ["ValueError"]
    assert isinstance(flight.claim("k"), ProducerLease)


async def test_cancelled_producer_is_retryable_for_followers() -> None:
    flight = build_flight()
    producer = as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))
    outcomes: list[str] = []

    async def wait_follower() -> None:
        try:
            await flight.wait(follower, timeout=5)
        except SingleFlightLoadCancelled:
            outcomes.append("cancelled")

    async with anyio.create_task_group() as group:
        group.start_soon(wait_follower)
        flight.fail(producer, anyio.get_cancelled_exc_class()())

    assert outcomes == ["cancelled"]
    assert isinstance(flight.claim("k"), ProducerLease)


async def test_failure_outcome_exposes_no_exception_object_or_message() -> None:
    flight = build_flight()
    producer = as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))

    flight.fail(producer, ValueError("secret detail"))

    with pytest.raises(SingleFlightLoadFailed) as caught:
        await flight.wait(follower, timeout=5)
    assert caught.value.error_type == "ValueError"
    assert caught.value.__cause__ is None
    assert "secret detail" not in str(caught.value)


async def test_stale_producer_cannot_remove_a_reacquired_flight() -> None:
    flight = build_flight()
    first = as_producer(flight.claim("k"))
    flight.fail(first, anyio.get_cancelled_exc_class()())
    second = as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))

    flight.complete(first, {"stale": True})

    results: list[dict[str, Any]] = []

    async def consume() -> None:
        results.append(await flight.wait(follower, timeout=5))

    async with anyio.create_task_group() as group:
        group.start_soon(consume)
        flight.complete(second, {"fresh": True})

    assert results == [{"fresh": True}]


async def test_each_waiter_receives_a_fresh_decoded_copy() -> None:
    flight = build_flight()
    producer = as_producer(flight.claim("k"))
    first = as_follower(flight.claim("k"))
    second = as_follower(flight.claim("k"))

    flight.complete(producer, {"items": [1]})

    decoded_first = await flight.wait(first, timeout=5)
    decoded_first["items"].append(2)
    decoded_second = await flight.wait(second, timeout=5)

    assert decoded_second == {"items": [1]}


def test_release_follower_is_idempotent() -> None:
    flight = build_flight(max_inflight=32, max_waiters_per_key=2)
    as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))

    flight.release_follower(follower)
    flight.release_follower(follower)

    assert waiter_count(flight, "k") == 0
    assert isinstance(flight.claim("k"), FollowerLease)


async def test_close_wakes_waiters_and_refuses_new_claims() -> None:
    flight = build_flight()
    as_producer(flight.claim("k"))
    follower = as_follower(flight.claim("k"))
    outcomes: list[str] = []

    async def consume() -> None:
        try:
            await flight.wait(follower, timeout=5)
        except SingleFlightLoadCancelled:
            outcomes.append("cancelled")

    async with anyio.create_task_group() as group:
        group.start_soon(consume)
        flight.close()

    assert outcomes == ["cancelled"]
    with pytest.raises(SingleFlightBusy):
        flight.claim("k")
