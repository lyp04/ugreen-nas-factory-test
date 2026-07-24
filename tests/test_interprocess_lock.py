from __future__ import annotations

import multiprocessing
import os
import threading
import time
import uuid
from dataclasses import replace

import pytest

from src.utils import interprocess_lock
from src.utils.interprocess_lock import InterProcessLock


def _hold_lock(key: str, ready, release_now, result_queue) -> None:
    lock = InterProcessLock(key)
    token = lock.try_acquire()
    result_queue.put(token is not None)
    ready.set()
    if token is None:
        return
    release_now.wait(15)
    result_queue.put(lock.release(token))


def _acquire_then_crash(key: str, ready) -> None:
    lock = InterProcessLock(key)
    token = lock.try_acquire()
    if token is None:
        os._exit(2)
    ready.set()
    os._exit(0)


def _spawn_context():
    # Spawn is the only start method on production Windows. Using it everywhere
    # also proves that coordination comes from the OS primitive, not inherited
    # Python globals.
    return multiprocessing.get_context("spawn")


def _unique_key(label: str) -> str:
    return f"pytest-{label}-{uuid.uuid4().hex}"


def test_try_acquire_is_non_reentrant_and_token_release_is_fail_safe() -> None:
    lock = InterProcessLock(_unique_key("token"))
    token = lock.try_acquire()

    assert token is not None
    assert lock.acquired is True
    assert lock.try_acquire() is None

    wrong_token = replace(token, nonce=uuid.uuid4().hex)
    assert lock.release(wrong_token) is False
    assert lock.release(object()) is False  # type: ignore[arg-type]
    assert lock.acquired is True
    assert lock.release(token) is True
    assert lock.acquired is False
    assert lock.release(token) is False


def test_separate_instances_in_one_process_still_contend() -> None:
    key = _unique_key("same-process")
    owner = InterProcessLock(key)
    contender = InterProcessLock(key)
    token = owner.try_acquire()

    assert token is not None
    assert contender.try_acquire() is None
    assert owner.release(token) is True

    contender_token = contender.try_acquire()
    assert contender_token is not None
    assert contender.release(contender_token) is True


def test_backend_permission_failure_fails_closed_without_leaking_local_lock(monkeypatch) -> None:
    key = _unique_key("backend-failure")
    broken = InterProcessLock(key)
    monkeypatch.setattr(
        broken,
        "_new_backend",
        lambda: (_ for _ in ()).throw(
            interprocess_lock.InterProcessLockError("Global CreateMutex access denied")
        ),
    )

    with pytest.raises(
        interprocess_lock.InterProcessLockError,
        match="Global CreateMutex access denied",
    ):
        broken.try_acquire()

    healthy = InterProcessLock(key)
    token = healthy.try_acquire()
    assert token is not None
    assert healthy.release(token) is True


def test_wrong_thread_cannot_release_owner_token() -> None:
    lock = InterProcessLock(_unique_key("thread-owner"))
    token = lock.try_acquire()
    assert token is not None

    results: list[bool] = []
    thread = threading.Thread(target=lambda: results.append(lock.release(token)))
    thread.start()
    thread.join(timeout=5)

    assert results == [False]
    assert lock.acquired is True
    assert lock.release(token) is True


def test_same_key_contends_across_processes() -> None:
    ctx = _spawn_context()
    key = _unique_key("contention")
    ready = ctx.Event()
    release_now = ctx.Event()
    result_queue = ctx.Queue()
    process = ctx.Process(target=_hold_lock, args=(key, ready, release_now, result_queue))
    process.start()

    try:
        assert ready.wait(10)
        assert result_queue.get(timeout=5) is True

        contender = InterProcessLock(key)
        assert contender.try_acquire() is None

        release_now.set()
        assert result_queue.get(timeout=5) is True
        process.join(timeout=10)
        assert process.exitcode == 0

        token = contender.try_acquire()
        assert token is not None
        assert contender.release(token) is True
    finally:
        release_now.set()
        process.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_lock_recovers_after_owner_process_os_exit() -> None:
    ctx = _spawn_context()
    key = _unique_key("crash")
    ready = ctx.Event()
    process = ctx.Process(target=_acquire_then_crash, args=(key, ready))
    process.start()

    assert ready.wait(10)
    process.join(timeout=10)
    assert process.exitcode == 0

    lock = InterProcessLock(key)
    deadline = time.monotonic() + 5
    token = None
    while token is None and time.monotonic() < deadline:
        token = lock.try_acquire()
        if token is None:
            time.sleep(0.05)

    assert token is not None
    assert lock.release(token) is True


def test_different_keys_can_be_held_by_different_processes() -> None:
    ctx = _spawn_context()
    held_key = _unique_key("held")
    independent_key = _unique_key("independent")
    ready = ctx.Event()
    release_now = ctx.Event()
    result_queue = ctx.Queue()
    process = ctx.Process(target=_hold_lock, args=(held_key, ready, release_now, result_queue))
    process.start()

    try:
        assert ready.wait(10)
        assert result_queue.get(timeout=5) is True

        independent = InterProcessLock(independent_key)
        token = independent.try_acquire()
        assert token is not None
        assert independent.release(token) is True
    finally:
        release_now.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


@pytest.mark.parametrize(
    "result",
    [
        interprocess_lock._WINDOWS_WAIT_OBJECT_0,
        interprocess_lock._WINDOWS_WAIT_ABANDONED,
    ],
)
def test_windows_normal_and_abandoned_wait_results_both_grant_ownership(result: int) -> None:
    assert interprocess_lock._windows_wait_result_is_acquired(result) is True


def test_windows_timeout_does_not_grant_ownership() -> None:
    assert (
        interprocess_lock._windows_wait_result_is_acquired(
            interprocess_lock._WINDOWS_WAIT_TIMEOUT
        )
        is False
    )


def test_windows_mutex_uses_machine_wide_namespace() -> None:
    name = interprocess_lock._windows_mutex_name("abc123")

    assert name == r"Global\UGREEN_NAS_FACTORY_LOCK_V1_abc123"
    assert not name.startswith("Local\\")
