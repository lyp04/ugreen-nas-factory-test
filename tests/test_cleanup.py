from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src import cli
from src.flows import cleanup


class _Page:
    def __init__(self) -> None:
        self.waits: list[int] = []

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.waits.append(timeout_ms)


def test_cleanup_defers_cancellation_until_deleted_pool_is_checkpointed(monkeypatch) -> None:
    class Cancelled(RuntimeError):
        pass

    cancelled = False
    events: list[str] = []

    monkeypatch.setattr(cleanup, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cleanup, "_open_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cleanup, "_navigate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cleanup, "_list_pool_ids", lambda *_args, **_kwargs: ["pool1"])

    def delete_pool(*_args, **_kwargs) -> None:
        nonlocal cancelled
        events.append("delete-confirmed")
        cancelled = True

    def check_cancel() -> None:
        if cancelled:
            events.append("cancel-observed")
            raise Cancelled("operator cancelled")

    monkeypatch.setattr(cleanup, "_delete_pool", delete_pool)

    with pytest.raises(Cancelled, match="operator cancelled"):
        cleanup.run(
            object(),
            {},
            {},
            cancel_check_cb=check_cancel,
            on_pool_deleted=lambda pool_id: events.append(f"checkpoint:{pool_id}"),
        )

    assert events == ["delete-confirmed", "checkpoint:pool1", "cancel-observed"]


def test_list_pool_ids_fails_closed_when_empty_list_cannot_be_verified() -> None:
    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def evaluate_all(self, _script: str) -> list[str]:
            assert self.selector == ".pool"
            return []

        def inner_text(self, timeout: int) -> str:
            assert self.selector == "body"
            assert timeout == 2_000
            raise RuntimeError("frame detached")

    class BrokenFrame:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    with pytest.raises(cleanup.CleanupError, match="page text could not be read"):
        cleanup._list_pool_ids(BrokenFrame(), {"pool_container": ".pool"})


def test_wait_for_pool_removed_does_not_treat_browser_errors_as_success(monkeypatch) -> None:
    class BrokenFrame:
        def locator(self, _selector: str):
            raise RuntimeError("frame detached")

    monkeypatch.setattr(cleanup.time, "monotonic", lambda: 0.0)
    page = _Page()

    with pytest.raises(cleanup.CleanupError, match="Could not verify.*pool1"):
        cleanup._wait_for_pool_removed(page, BrokenFrame(), "pool1")

    assert page.waits == [2_000, 2_000]


def test_wait_for_pool_removed_counts_locator_read_errors_consecutively(monkeypatch) -> None:
    class BrokenLocator:
        first = None

        def __init__(self) -> None:
            self.first = self

        def count(self) -> int:
            raise RuntimeError("locator read failed")

    class BrokenFrame:
        def locator(self, _selector: str):
            return BrokenLocator()

    monkeypatch.setattr(cleanup.time, "monotonic", lambda: 0.0)
    page = _Page()

    with pytest.raises(cleanup.CleanupError, match="Could not verify.*pool1"):
        cleanup._wait_for_pool_removed(page, BrokenFrame(), "pool1")

    assert page.waits == [2_000, 2_000]


def test_wait_for_pool_removed_tolerates_one_transient_error(monkeypatch) -> None:
    class MissingLocator:
        first = None

        def __init__(self) -> None:
            self.first = self

        def count(self) -> int:
            return 0

    class RecoveringFrame:
        calls = 0

        def locator(self, _selector: str):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary reload")
            return MissingLocator()

    monkeypatch.setattr(cleanup.time, "monotonic", lambda: 0.0)
    page = _Page()

    cleanup._wait_for_pool_removed(page, RecoveringFrame(), "pool2")

    assert page.waits == [2_000, 2_000, 2_000]


def test_wait_for_pool_removed_requires_consecutive_absence(monkeypatch) -> None:
    class Locator:
        first = None

        def __init__(self, visible: bool) -> None:
            self.first = self
            self.visible = visible

        def count(self) -> int:
            return int(self.visible)

        def is_visible(self, timeout: int) -> bool:
            assert timeout == 500
            return self.visible

        def inner_text(self, timeout: int) -> str:
            assert timeout == 1_000
            return "存储池1"

    class RerenderingFrame:
        def __init__(self) -> None:
            self.states = iter([False, True, False, False, False])

        def locator(self, _selector: str):
            return Locator(next(self.states))

    monkeypatch.setattr(cleanup.time, "monotonic", lambda: 0.0)
    page = _Page()

    cleanup._wait_for_pool_removed(page, RerenderingFrame(), "pool3")

    assert page.waits == [2_000, 2_000, 2_000, 2_000]


def test_wait_for_pool_removed_does_not_treat_hidden_dom_node_as_absent(monkeypatch) -> None:
    class Locator:
        first = None

        def __init__(self, count: int, visible: bool) -> None:
            self.first = self
            self._count = count
            self._visible = visible

        def count(self) -> int:
            return self._count

        def is_visible(self, timeout: int) -> bool:
            assert timeout == 500
            return self._visible

    class HidingThenRemovedFrame:
        def __init__(self) -> None:
            self.states = iter(
                [
                    (1, False),
                    (1, False),
                    (1, False),
                    (0, False),
                    (0, False),
                    (0, False),
                ]
            )

        def locator(self, _selector: str) -> Locator:
            return Locator(*next(self.states))

    monkeypatch.setattr(cleanup.time, "monotonic", lambda: 0.0)
    page = _Page()

    cleanup._wait_for_pool_removed(page, HidingThenRemovedFrame(), "pool4")

    # Three hidden-but-present observations must not satisfy the three-sample
    # absence requirement; success only follows three count()==0 observations.
    assert page.waits == [2_000, 2_000, 2_000, 2_000, 2_000]


FULL_SN = "EC752VV42251611A"
WRONG_SN = "EC752VV42251699Z"
TARGET_IP = "192.0.2.10"


class _CleanupLock:
    def __init__(self) -> None:
        self.released = False

    def acquire(self, blocking: bool) -> bool:
        assert blocking is False
        return True

    def release(self) -> None:
        self.released = True


class _CleanupPage:
    def __init__(self, url: str) -> None:
        self.url = url

    def set_default_timeout(self, _timeout: int) -> None:
        pass


@contextmanager
def _playwright():
    yield object()


def _configure_run_cleanup(
    monkeypatch,
    tmp_path,
    *,
    identity_results: list[object],
    page_url: str = f"http://{TARGET_IP}:9999/desktop",
    report_write_error: Exception | None = None,
) -> SimpleNamespace:
    lock = _CleanupLock()
    identity_calls: list[tuple[str, str]] = []
    identity_result_iter = iter(identity_results)
    cleanup_calls: list[object] = []
    released_reservations: list[set[str]] = []
    closed: list[object] = []
    logger_calls: list[dict] = []
    report_writes: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        cli,
        "load_configs",
        lambda _root: (
            {
                "output_dir": "out",
                "network": {
                    "ugos_http_port": 9999,
                    "service_ready_timeout": 1,
                },
                "browser": {"default_timeout_ms": 1},
                "admin": {"username": "admin", "password": "secret"},
            },
            {},
        ),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_ip_for_task",
        lambda *_args, **_kwargs: (TARGET_IP, {TARGET_IP}, None),
    )
    monkeypatch.setattr(cli, "_device_lock_for_ip", lambda _ip: lock)
    monkeypatch.setattr(cli, "wait_until_ready", lambda *_args, **_kwargs: None)

    def verify_identity(ip: str, requested_sn: str, **_kwargs) -> str:
        identity_calls.append((ip, requested_sn))
        result = next(identity_result_iter)
        if isinstance(result, Exception):
            raise result
        return str(result)

    monkeypatch.setattr(cli, "_verify_nas_identity_at_ip", verify_identity)
    monkeypatch.setattr(cli, "_read_report_file", lambda _path: {})
    monkeypatch.setattr(cli, "sync_playwright", _playwright)
    session = SimpleNamespace(page=_CleanupPage(page_url))
    monkeypatch.setattr(cli, "launch_managed_context", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(cli, "close_managed_context", lambda value: closed.append(value))
    monkeypatch.setattr(cli.login_flow, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)

    def run_destructive_cleanup(*_args, **_kwargs):
        cleanup_calls.append(session.page)
        return ["pool1"]

    monkeypatch.setattr(cli.cleanup_flow, "run", run_destructive_cleanup)
    monkeypatch.setattr(cli, "capture_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli,
        "setup_logger",
        lambda *_args, **kwargs: logger_calls.append(kwargs),
    )
    monkeypatch.setattr(
        cli,
        "_release_reserved_ips",
        lambda ips: released_reservations.append(set(ips)),
    )

    def write_report(*args, **kwargs) -> None:
        if report_write_error is not None:
            raise report_write_error
        report_writes.append((args, kwargs))

    monkeypatch.setattr(cli, "_write_json_atomic", write_report)
    return SimpleNamespace(
        lock=lock,
        identity_calls=identity_calls,
        cleanup_calls=cleanup_calls,
        released_reservations=released_reservations,
        closed=closed,
        logger_calls=logger_calls,
        report_writes=report_writes,
        session=session,
    )


def test_run_cleanup_releases_lock_and_reserved_ips_when_report_write_fails(
    monkeypatch,
    tmp_path,
) -> None:
    state = _configure_run_cleanup(
        monkeypatch,
        tmp_path,
        identity_results=[FULL_SN, FULL_SN],
        report_write_error=OSError("disk full"),
    )

    with pytest.raises(OSError, match="disk full"):
        cli.run_cleanup("611A", setup_file_log=True)

    assert state.identity_calls == [(TARGET_IP, "611A"), (TARGET_IP, FULL_SN)]
    assert state.cleanup_calls == [state.session.page]
    assert state.lock.released
    assert state.released_reservations == [{TARGET_IP}]
    assert state.closed == [state.session]
    assert state.logger_calls == [
        {
            "sn": FULL_SN,
            "filename": "cleanup.log",
            "extra_secrets": ("secret",),
        }
    ]


def test_run_cleanup_blocks_destructive_cleanup_when_final_identity_changes(
    monkeypatch,
    tmp_path,
) -> None:
    state = _configure_run_cleanup(
        monkeypatch,
        tmp_path,
        identity_results=[FULL_SN, WRONG_SN],
    )

    with pytest.raises(RuntimeError, match="identity changed before standalone cleanup"):
        cli.run_cleanup("611A", setup_file_log=False)

    assert state.identity_calls == [(TARGET_IP, "611A"), (TARGET_IP, FULL_SN)]
    assert state.cleanup_calls == []
    assert state.lock.released
    assert state.released_reservations == [{TARGET_IP}]


def test_run_cleanup_blocks_destructive_cleanup_when_page_host_changes(
    monkeypatch,
    tmp_path,
) -> None:
    state = _configure_run_cleanup(
        monkeypatch,
        tmp_path,
        identity_results=[FULL_SN, FULL_SN],
        page_url="http://192.0.2.99:9999/desktop",
    )

    with pytest.raises(RuntimeError, match="page address changed.*standalone cleanup"):
        cli.run_cleanup("611A", setup_file_log=False)

    assert state.identity_calls == [(TARGET_IP, "611A"), (TARGET_IP, FULL_SN)]
    assert state.cleanup_calls == []
    assert state.lock.released
    assert state.released_reservations == [{TARGET_IP}]
