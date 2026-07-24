from contextlib import contextmanager

import pytest

from src import cli
from src.utils import browser_control


SN = "EC752VV42251611A"


class _Lock:
    def acquire(self, blocking: bool) -> bool:
        assert blocking is False
        return True

    def release(self) -> None:
        return None


class _PageLookupFailureSession:
    browser_pid = None

    @property
    def page(self):
        raise RuntimeError("page acquisition failed")


class _DefaultTimeoutFailurePage:
    def set_default_timeout(self, timeout_ms: int) -> None:
        assert timeout_ms == 123
        raise RuntimeError("default timeout failed")


class _DefaultTimeoutFailureSession:
    browser_pid = None
    page = _DefaultTimeoutFailurePage()


@contextmanager
def _playwright():
    yield object()


def _patch_cli_until_browser_launch(monkeypatch, tmp_path) -> None:
    config = {
        "output_dir": "out",
        "pages": [],
        "network": {
            "ugos_http_port": 9999,
            "service_ready_timeout": 1,
        },
        "browser": {"default_timeout_ms": 123},
        "admin": {"username": "factory", "password": "secret"},
        "fault_report": {"enabled": False},
    }
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "load_configs", lambda _root: (config, {}))
    monkeypatch.setattr(
        cli,
        "_resolve_ip_for_task",
        lambda *_args, **_kwargs: ("192.0.2.10", {"192.0.2.10"}, SN),
    )
    monkeypatch.setattr(cli, "_device_lock_for_ip", lambda _ip: _Lock())
    monkeypatch.setattr(cli, "_wait_until_ready_cancelable", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "wait_until_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_verify_nas_identity_at_ip", lambda *_args, **_kwargs: SN)
    monkeypatch.setattr(cli, "_read_report_file", lambda _path: {})
    monkeypatch.setattr(cli, "_write_json_atomic", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_attach_failure_diagnostics", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_maybe_autoreport_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_release_reserved_ips", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "setup_logger", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "sync_playwright", _playwright)


def test_window_management_is_a_noop_without_win32(monkeypatch) -> None:
    monkeypatch.setattr(browser_control, "_USER32", None)
    monkeypatch.setattr(browser_control, "_ENUM_WINDOWS_PROC", None)

    assert browser_control._top_level_windows_for_pid(1234) == []
    assert not browser_control.show_browser_windows(1234)
    assert not browser_control.hide_browser_windows(1234)


def test_run_test_closes_launched_session_when_page_acquisition_fails(monkeypatch, tmp_path) -> None:
    _patch_cli_until_browser_launch(monkeypatch, tmp_path)
    session = _PageLookupFailureSession()
    closed: list[object] = []
    monkeypatch.setattr(cli, "launch_managed_context", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(cli, "close_managed_context", closed.append)

    with pytest.raises(RuntimeError, match="page acquisition failed"):
        cli.run_test(SN, setup_file_log=False)

    assert closed == [session]


def test_run_cleanup_closes_launched_session_when_default_timeout_fails(monkeypatch, tmp_path) -> None:
    _patch_cli_until_browser_launch(monkeypatch, tmp_path)
    session = _DefaultTimeoutFailureSession()
    closed: list[object] = []
    monkeypatch.setattr(cli, "launch_managed_context", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(cli, "close_managed_context", closed.append)

    with pytest.raises(RuntimeError, match="default timeout failed"):
        cli.run_cleanup(SN, setup_file_log=False)

    assert closed == [session]


@pytest.mark.parametrize(
    ("session", "message"),
    [
        (_PageLookupFailureSession(), "page acquisition failed"),
        (_DefaultTimeoutFailureSession(), "default timeout failed"),
    ],
)
def test_ensure_browser_session_closes_replacement_when_page_initialization_fails(
    monkeypatch,
    session,
    message: str,
) -> None:
    old_session = object()
    closed: list[object] = []
    monkeypatch.setattr(cli, "launch_managed_context", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(cli, "close_managed_context", closed.append)
    monkeypatch.setattr(
        cli.login_flow,
        "run",
        lambda *_args, **_kwargs: pytest.fail("login must not run when page initialization fails"),
    )

    with pytest.raises(RuntimeError, match=message):
        cli._ensure_browser_session(
            object(),
            old_session,
            None,
            {"default_timeout_ms": 123},
            "task-id",
            None,
            "http://192.0.2.10:9999",
            {},
            {},
        )

    assert closed == [old_session, session]


def test_ensure_browser_session_closes_replacement_when_login_fails(monkeypatch) -> None:
    page = _DefaultTimeoutFailurePage()
    page.set_default_timeout = lambda _timeout_ms: None
    session = _DefaultTimeoutFailureSession()
    session.page = page
    old_session = object()
    closed: list[object] = []
    monkeypatch.setattr(cli, "launch_managed_context", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(cli, "close_managed_context", closed.append)
    monkeypatch.setattr(cli.login_flow, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("login failed")))

    with pytest.raises(RuntimeError, match="login failed"):
        cli._ensure_browser_session(
            object(),
            old_session,
            None,
            {"default_timeout_ms": 123},
            "task-id",
            None,
            "http://192.0.2.10:9999",
            {},
            {},
        )

    assert closed == [old_session, session]
