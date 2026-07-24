from __future__ import annotations

import json

import pytest

from src import cli


SN = "EC752VV42251611A"


def _write_previous_report(tmp_path, report: dict) -> None:
    report_dir = tmp_path / "out" / SN
    report_dir.mkdir(parents=True)
    (report_dir / "test_report.json").write_text(
        json.dumps(report),
        encoding="utf-8",
    )


def _configure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        cli,
        "load_configs",
        lambda _root: (
            {
                "output_dir": "out",
                "pages": [],
                "network": {"ugos_http_port": 9999},
                "fault_report": {"enabled": False},
            },
            {},
        ),
    )


def test_confirmed_reset_checkpoint_recovers_success_without_reconnecting(
    monkeypatch,
    tmp_path,
) -> None:
    _configure(monkeypatch, tmp_path)
    _write_previous_report(
        tmp_path,
        {
            "sn": SN,
            "status": "running",
            "factory_reset": "confirmed",
            "factory_reset_verification": "confirmed",
            "nas_ip": "192.0.2.10",
            "nas_ip_after_reset": "192.0.2.44",
            "error": "process stopped after the durable checkpoint",
        },
    )
    monkeypatch.setattr(
        cli,
        "_resolve_ip_for_task",
        lambda *_args, **_kwargs: pytest.fail("recovery must not reconnect"),
    )
    events: list[dict] = []

    report = cli.run_test(
        SN,
        setup_file_log=False,
        progress_cb=events.append,
    )

    assert report["status"] == "success"
    assert report["recovered_after_factory_reset"] is True
    assert "error" not in report
    assert events[-1]["status"] == "success"
    assert events[-1]["nas_ip"] == "192.0.2.44"


@pytest.mark.parametrize(
    ("status", "reset_state"),
    [
        ("running", "starting"),
        ("failed", "initiated"),
        ("cancelled", "uncertain"),
    ],
)
def test_possible_prior_reset_submission_blocks_every_retest_status(
    monkeypatch,
    tmp_path,
    status: str,
    reset_state: str,
) -> None:
    _configure(monkeypatch, tmp_path)
    _write_previous_report(
        tmp_path,
        {
            "sn": SN,
            "status": status,
            "factory_reset": reset_state,
            "factory_reset_before_finish": True,
        },
    )
    monkeypatch.setattr(
        cli,
        "_resolve_ip_for_task",
        lambda *_args, **_kwargs: pytest.fail("unsafe resume must stop before discovery"),
    )

    with pytest.raises(cli.reset_factory_flow.FactoryResetUnconfirmed):
        cli.run_test(SN, setup_file_log=False)


def test_confirmed_without_verification_checkpoint_blocks_repeat(
    monkeypatch,
    tmp_path,
) -> None:
    _configure(monkeypatch, tmp_path)
    _write_previous_report(
        tmp_path,
        {
            "sn": SN,
            "status": "running",
            "factory_reset": "confirmed",
        },
    )
    monkeypatch.setattr(
        cli,
        "_resolve_ip_for_task",
        lambda *_args, **_kwargs: pytest.fail("unsafe resume must stop before discovery"),
    )

    with pytest.raises(cli.reset_factory_flow.FactoryResetRetryBlocked):
        cli.run_test(SN, setup_file_log=False)


def test_atomic_report_write_redacts_structured_and_explicit_secrets(tmp_path) -> None:
    secret = "factory-admin-secret"
    report = {
        "status": "failed",
        "error": f"external command echoed {secret}",
        "details": {"password": secret, "authorization": "Basic abc123"},
        "verified_device_macs": ["AA:BB:CC:DD:EE:FF"],
    }
    destination = tmp_path / "test_report.json"

    cli._write_json_atomic(destination, report, extra_secrets=(secret,))

    persisted = destination.read_text(encoding="utf-8")
    assert secret not in persisted
    assert "abc123" not in persisted
    assert json.loads(persisted)["details"]["password"] == "<redacted>"
    assert json.loads(persisted)["verified_device_macs"] == ["AA:BB:CC:DD:EE:FF"]
    # Sanitization must not mutate the in-memory report used by the running task.
    assert report["details"]["password"] == secret


def test_sanitized_exception_preserves_type_and_hides_config_password() -> None:
    secret = "factory-admin-secret"
    config = {"admin": {"password": secret}, "fault_report": {"token": ""}}
    original = cli.TaskCancelled(f"cancelled after tool echoed {secret}")

    sanitized = cli._sanitized_exception(original, config)

    assert isinstance(sanitized, cli.TaskCancelled)
    assert secret not in str(sanitized)
