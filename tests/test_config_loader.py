import json
from pathlib import Path

import pytest

from src.utils.config_loader import ConfigLoadError, load_yaml


def test_invalid_yaml_error_does_not_echo_secret_source_line(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "config.yml"
    secret = "synthetic-admin-secret"
    path.write_text(f'admin:\n  password: "{secret}\n', encoding="utf-8")

    with pytest.raises(ConfigLoadError) as exc_info:
        load_yaml(path)

    message = str(exc_info.value)
    assert secret not in message
    assert "password:" not in message
    assert "config/config.yml" in message
    assert "line" in message and "column" in message
    assert exc_info.value.__suppress_context__ is True


def test_load_yaml_preserves_non_yaml_io_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_yaml(tmp_path / "missing.yml")


def test_public_configuration_examples_are_safe_by_default(monkeypatch) -> None:
    monkeypatch.delenv("FAULT_REPORT_TOKEN", raising=False)
    root = Path(__file__).resolve().parent.parent
    config = load_yaml(root / "config" / "config.example.yml")
    update = json.loads(
        (root / "config" / "update-config.example.json").read_text(encoding="utf-8")
    )

    assert config["fault_report"] == {
        "enabled": False,
        "owner": "",
        "repo": "",
        "token": "",
        "release_tag": "fault-reports",
        "run_log_tail_lines": 200,
        "dedup_window_minutes": 5,
    }
    assert update["enabled"] is False
    assert update["owner"] == ""
    assert update["repo"] == ""
    assert update["token"] == ""
