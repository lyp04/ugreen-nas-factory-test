from __future__ import annotations

import io
import json
import queue
import urllib.error
from pathlib import Path

import pytest

from src.updater import (
    DEFAULT_MANIFEST_ASSET,
    UpdateInfo,
    UpdateManager,
    _Config,
    _sha256,
    _version_tuple,
    format_version,
)
from src.version import PACKAGE_NAME, VERSION_CODE, VERSION_NAME


def _make_manager(tmp_path: Path) -> UpdateManager:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    return UpdateManager(tmp_path, queue.Queue())


def test_load_config_returns_disabled_when_file_missing(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    cfg = mgr._load_config()
    assert cfg.enabled is False
    assert cfg.manifest_asset == DEFAULT_MANIFEST_ASSET


def test_load_config_disables_when_secrets_missing(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._config_path().write_text(json.dumps({"enabled": True, "owner": "o", "repo": "r"}))
    cfg = mgr._load_config()
    assert cfg.enabled is False  # token missing → forced off
    assert cfg.owner == "o"


def test_load_config_keeps_enabled_when_complete(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._config_path().write_text(
        json.dumps(
            {
                "enabled": True,
                "owner": "o",
                "repo": "r",
                "token": "t",
                "manifestAsset": "",  # empty → falls back to default
                "releaseTag": "beta",
            }
        )
    )
    cfg = mgr._load_config()
    assert cfg.enabled
    assert cfg.manifest_asset == DEFAULT_MANIFEST_ASSET
    assert cfg.release_tag == "beta"


def test_release_url_uses_latest_when_no_tag(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    cfg = _Config(True, "owner", "repo", "tok", DEFAULT_MANIFEST_ASSET, "")
    assert mgr._release_url(cfg) == "https://api.github.com/repos/owner/repo/releases/latest"


def test_release_url_escapes_tag(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    cfg = _Config(True, "owner", "repo", "tok", DEFAULT_MANIFEST_ASSET, "v1.0/beta")
    assert (
        mgr._release_url(cfg)
        == "https://api.github.com/repos/owner/repo/releases/tags/v1.0%2Fbeta"
    )


def test_find_asset_picks_by_name() -> None:
    assets = [{"name": "other"}, {"name": "update.json", "url": "u"}]
    found = UpdateManager._find_asset(assets, "update.json")
    assert found is not None and found["url"] == "u"
    assert UpdateManager._find_asset(assets, "missing.json") is None


def test_sha256_of_file(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"hello world")
    # sha256("hello world")
    assert _sha256(target) == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_validate_download_rejects_mismatched_sha(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    target = tmp_path / "exe.bin"
    target.write_bytes(b"payload")
    info = UpdateInfo(
        config=_Config(True, "o", "r", "t", DEFAULT_MANIFEST_ASSET, ""),
        version_code=VERSION_CODE + 1,
        version_name="x",
        notes="",
        exe_asset="exe.bin",
        exe_url="https://example.invalid/exe.bin",
        sha256="deadbeef",
    )
    with pytest.raises(RuntimeError, match="SHA-256"):
        mgr._validate_download(info, target)


def test_validate_download_rejects_empty_file(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    target = tmp_path / "exe.bin"
    target.write_bytes(b"")
    info = UpdateInfo(
        config=_Config(True, "o", "r", "t", DEFAULT_MANIFEST_ASSET, ""),
        version_code=VERSION_CODE + 1,
        version_name="x",
        notes="",
        exe_asset="exe.bin",
        exe_url="https://example.invalid/exe.bin",
        sha256="",
    )
    with pytest.raises(RuntimeError, match="空"):
        mgr._validate_download(info, target)


def test_state_round_trip(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    mgr._write_state(last_check_s=123.0)
    assert mgr._read_state()["last_check_s"] == 123.0
    mgr._write_state(other="x")
    state = mgr._read_state()
    assert state["last_check_s"] == 123.0
    assert state["other"] == "x"


def test_format_version_matches_version_module() -> None:
    assert format_version() == f"{VERSION_NAME} ({VERSION_CODE})"
    assert PACKAGE_NAME  # exists


def test_version_tuple_parses_dotted_numbers() -> None:
    assert _version_tuple("0.1.12") == (0, 1, 12)
    assert _version_tuple("v0.1.13") == (0, 1, 13)
    assert _version_tuple("1.0") == (1, 0)
    assert _version_tuple("") == ()
    # Non-numeric segments fall back to empty so versionCode stays authoritative.
    assert _version_tuple("0.1.13-beta") == ()


def test_version_tuple_orders_patches_correctly() -> None:
    assert _version_tuple("0.1.13") > _version_tuple("0.1.12")
    assert _version_tuple("0.2.0") > _version_tuple("0.1.99")
    assert not (_version_tuple("0.1.12") > _version_tuple("0.1.12"))  # equal — strict gt is False


def test_find_update_treats_same_code_higher_name_as_newer(tmp_path: Path, monkeypatch) -> None:
    # Repro for the bug where re-tagging the same commit (e.g. v0.1.11 → v0.1.12)
    # produced an identical versionCode and the updater silently dropped the
    # release. The fix: when versionCode ties, fall back to versionName.
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr("src.updater.VERSION_CODE", 33)
    monkeypatch.setattr("src.updater.VERSION_NAME", "0.1.11")
    manifest = {
        "packageName": PACKAGE_NAME,
        "versionCode": 33,
        "versionName": "0.1.13",
        "exeAsset": "UGREEN-NAS-Test.exe",
        "sha256": "deadbeef",
    }
    release = {"assets": [
        {"name": "update.json", "url": "https://api.github.com/manifest"},
        {"name": "UGREEN-NAS-Test.exe", "url": "https://api.github.com/exe"},
    ]}
    _install_fake_opener(mgr, monkeypatch, [
        {"body": json.dumps(release).encode("utf-8")},
        {"body": json.dumps(manifest).encode("utf-8")},
    ])
    cfg = _Config(True, "o", "r", "t", DEFAULT_MANIFEST_ASSET, "")
    info = mgr._find_update(cfg)
    assert info is not None
    assert info.version_name == "0.1.13"
    assert info.version_code == 33


def test_find_update_skips_when_remote_name_not_newer(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    monkeypatch.setattr("src.updater.VERSION_CODE", 33)
    monkeypatch.setattr("src.updater.VERSION_NAME", "0.1.13")
    manifest = {
        "packageName": PACKAGE_NAME,
        "versionCode": 33,
        "versionName": "0.1.13",
        "exeAsset": "UGREEN-NAS-Test.exe",
        "sha256": "deadbeef",
    }
    release = {"assets": [
        {"name": "update.json", "url": "https://api.github.com/manifest"},
    ]}
    _install_fake_opener(mgr, monkeypatch, [
        {"body": json.dumps(release).encode("utf-8")},
        {"body": json.dumps(manifest).encode("utf-8")},
    ])
    cfg = _Config(True, "o", "r", "t", DEFAULT_MANIFEST_ASSET, "")
    assert mgr._find_update(cfg) is None


# --- _open redirect/auth handling -------------------------------------------


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            data, self._body = self._body, b""
        else:
            data, self._body = self._body[:size], self._body[size:]
        return data

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _install_fake_opener(mgr: UpdateManager, monkeypatch, scenario):
    """Replace mgr._opener.open with a stub that drives the scenario.

    scenario is a list of dicts. For each request the stub:
      - records {url, auth}
      - if dict has "redirect", raises HTTPError(302) with Location
      - if dict has "status" 4xx/5xx, raises HTTPError with that status + body
      - else returns _FakeResp("body")
    """
    calls: list[dict] = []
    remaining = list(scenario)

    def fake_open(req, timeout=None):
        step = remaining.pop(0)
        calls.append({"url": req.full_url, "auth": req.headers.get("Authorization")})
        if "redirect" in step:
            raise urllib.error.HTTPError(
                req.full_url,
                step.get("status", 302),
                "Found",
                {"Location": step["redirect"]},
                io.BytesIO(b""),
            )
        if step.get("status", 200) >= 400:
            raise urllib.error.HTTPError(
                req.full_url,
                step["status"],
                "Err",
                {},
                io.BytesIO(step.get("body", b"")),
            )
        return _FakeResp(step.get("body", b"body"))

    monkeypatch.setattr(mgr._opener, "open", fake_open)
    return calls


def test_open_carries_bearer_on_github_api(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    calls = _install_fake_opener(mgr, monkeypatch, [{"body": b"OK"}])
    resp = mgr._open(
        "https://api.github.com/repos/o/r/releases/latest",
        "TOKEN-XYZ",
        "application/vnd.github+json",
    )
    assert resp.read() == b"OK"
    assert len(calls) == 1
    assert calls[0]["auth"] == "Bearer TOKEN-XYZ"


def test_open_strips_auth_on_cross_host_redirect(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    calls = _install_fake_opener(
        mgr,
        monkeypatch,
        [
            {"redirect": "https://objects.githubusercontent.com/blob?sig=abc"},
            {"body": b"PAYLOAD"},
        ],
    )
    resp = mgr._open(
        "https://api.github.com/repos/o/r/releases/assets/123",
        "TOKEN-XYZ",
        "application/octet-stream",
    )
    assert resp.read() == b"PAYLOAD"
    assert len(calls) == 2
    assert calls[0]["auth"] == "Bearer TOKEN-XYZ"
    # Cross-host hop must NOT carry the token, or S3 returns 400.
    assert calls[1]["auth"] is None
    assert calls[1]["url"].startswith("https://objects.githubusercontent.com/")


def test_open_keeps_auth_on_same_host_redirect(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    calls = _install_fake_opener(
        mgr,
        monkeypatch,
        [
            {"redirect": "https://api.github.com/elsewhere"},
            {"body": b"OK"},
        ],
    )
    mgr._open("https://api.github.com/start", "TOKEN", "application/json").read()
    assert calls[0]["auth"] == "Bearer TOKEN"
    assert calls[1]["auth"] == "Bearer TOKEN"


def test_open_resolves_relative_redirect(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    calls = _install_fake_opener(
        mgr, monkeypatch, [{"redirect": "/v2/elsewhere"}, {"body": b"OK"}]
    )
    mgr._open("https://api.github.com/start", "TOKEN", "application/json").read()
    assert calls[1]["url"] == "https://api.github.com/v2/elsewhere"
    assert calls[1]["auth"] == "Bearer TOKEN"


def test_open_aborts_after_too_many_redirects(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    _install_fake_opener(
        mgr,
        monkeypatch,
        [{"redirect": "https://api.github.com/loop"} for _ in range(6)],
    )
    with pytest.raises(RuntimeError, match="Too many redirects"):
        mgr._open("https://api.github.com/start", "TOKEN", "application/json")


def test_open_raises_runtime_error_on_http_error(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    _install_fake_opener(mgr, monkeypatch, [{"status": 404, "body": b"nope"}])
    with pytest.raises(RuntimeError, match="HTTP 404"):
        mgr._open("https://api.github.com/missing", "TOKEN", "application/json")


def test_open_skips_token_when_empty(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    calls = _install_fake_opener(mgr, monkeypatch, [{"body": b"OK"}])
    mgr._open("https://api.github.com/public", "", "application/json").read()
    assert calls[0]["auth"] is None
