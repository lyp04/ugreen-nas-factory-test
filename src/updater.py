from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from loguru import logger

from .version import PACKAGE_NAME, VERSION_CODE, VERSION_NAME

CONFIG_FILE = "update-config.json"
DEFAULT_MANIFEST_ASSET = "update.json"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "UGREEN-NAS-FactoryTest-Updater"
FOREGROUND_CHECK_INTERVAL_S = 10 * 60
HTTP_CONNECT_TIMEOUT_S = 30
HTTP_READ_TIMEOUT_S = 120
STATE_FILE = "update_state.json"


@dataclass
class _Config:
    enabled: bool
    owner: str
    repo: str
    token: str
    manifest_asset: str
    release_tag: str


@dataclass
class UpdateInfo:
    config: _Config
    version_code: int
    version_name: str
    notes: str
    exe_asset: str
    exe_url: str
    sha256: str


class UpdateManager:
    """Background GitHub-release updater modelled after an internal UpdateManager.

    The check runs on a daemon thread so a slow network never blocks the GUI.
    When a newer release is found, a `update_available` event is pushed onto
    the supplied ui_queue and the GUI is expected to confirm with the user
    before calling :meth:`download_and_install`.
    """

    def __init__(self, project_root: Path, ui_queue: "queue.Queue[dict]") -> None:
        self.project_root = Path(project_root)
        self.ui_queue = ui_queue
        self._checked_this_process = False
        self._state_lock = threading.Lock()
        self._opener = urllib.request.build_opener(_NoFollowRedirect)

    # ------------------------------------------------------------------ public
    def check_on_startup(self) -> None:
        self._spawn_check(force=False)

    def check_on_foreground(self) -> None:
        now = time.time()
        last = self._read_state().get("last_check_s", 0.0)
        if now - float(last) < FOREGROUND_CHECK_INTERVAL_S:
            return
        self._spawn_check(force=True)

    def check_now(self) -> None:
        self._spawn_check(force=True)

    def download_and_install(self, update: UpdateInfo) -> None:
        """Spawn a thread that downloads the new exe and chains the swap."""
        thread = threading.Thread(
            target=self._download_and_install_worker,
            args=(update,),
            name="updater-download",
            daemon=True,
        )
        thread.start()

    # ------------------------------------------------------------------ check
    def _spawn_check(self, *, force: bool) -> None:
        if not force and self._checked_this_process:
            return
        self._checked_this_process = True
        self._write_state(last_check_s=time.time())
        thread = threading.Thread(
            target=self._check_worker, name="updater-check", daemon=True
        )
        thread.start()

    def _check_worker(self) -> None:
        try:
            config = self._load_config()
            if not config.enabled:
                return
            update = self._find_update(config)
            if update is None:
                return
            self.ui_queue.put({"type": "update_available", "update": update})
        except Exception as exc:  # noqa: BLE001 — update checks must never raise
            logger.debug("Update check failed: {}", exc)

    # ----------------------------------------------------------------- config
    def _config_path(self) -> Path:
        return self.project_root / "config" / CONFIG_FILE

    def _load_config(self) -> _Config:
        path = self._config_path()
        if not path.exists():
            return _Config(False, "", "", "", DEFAULT_MANIFEST_ASSET, "")
        # utf-8-sig: tolerate a UTF-8 BOM (PowerShell Set-Content -Encoding utf8 adds one),
        # otherwise json.load chokes on the BOM and updates silently never fire.
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        owner = str(data.get("owner", "")).strip()
        repo = str(data.get("repo", "")).strip()
        token = str(data.get("token", "")).strip()
        manifest_asset = str(data.get("manifestAsset", "") or DEFAULT_MANIFEST_ASSET).strip()
        if not manifest_asset:
            manifest_asset = DEFAULT_MANIFEST_ASSET
        release_tag = str(data.get("releaseTag", "")).strip()
        enabled = bool(data.get("enabled", False))
        # token is optional: a public repo serves release assets unauthenticated,
        # so updates work without one. When present it's still used (Bearer auth
        # for a private repo); when empty, requests just go out anonymous.
        if not (owner and repo):
            enabled = False
        return _Config(enabled, owner, repo, token, manifest_asset, release_tag)

    # ------------------------------------------------------------------ probe
    def _find_update(self, config: _Config) -> Optional[UpdateInfo]:
        release_url = self._release_url(config)
        release = json.loads(
            self._get_text(release_url, config.token, "application/vnd.github+json")
        )
        assets = release.get("assets") or []
        manifest_asset = self._find_asset(assets, config.manifest_asset)
        if manifest_asset is None:
            return None
        manifest = json.loads(
            self._get_text(manifest_asset["url"], config.token, "application/octet-stream")
        )
        package_name = str(manifest.get("packageName", ""))
        if package_name and package_name != PACKAGE_NAME:
            raise RuntimeError(f"Update package mismatch: {package_name}")
        remote_version = int(manifest.get("versionCode", 0) or 0)
        remote_version_name = str(manifest.get("versionName", str(remote_version)))
        # versionCode comes from `git rev-list --count HEAD` in CI, so two tags
        # on the same commit produce identical versionCode. Compare versionName
        # numerically as a tiebreaker so a re-tag still triggers an update.
        if remote_version < VERSION_CODE:
            return None
        if remote_version == VERSION_CODE and _version_tuple(remote_version_name) <= _version_tuple(VERSION_NAME):
            return None
        exe_asset_name = str(manifest.get("exeAsset", "") or manifest.get("apkAsset", "")).strip()
        if not exe_asset_name:
            raise RuntimeError("update.json is missing exeAsset")
        exe_asset = self._find_asset(assets, exe_asset_name)
        if exe_asset is None:
            raise RuntimeError(f"Release asset not found: {exe_asset_name}")
        sha256 = str(manifest.get("sha256", "")).lower().replace("sha256:", "").strip()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise RuntimeError("update.json is missing a valid 64-character SHA-256")
        return UpdateInfo(
            config=config,
            version_code=remote_version,
            version_name=remote_version_name,
            notes=str(manifest.get("notes", "")),
            exe_asset=exe_asset_name,
            exe_url=exe_asset["url"],
            sha256=sha256,
        )

    def _release_url(self, config: _Config) -> str:
        base = f"https://api.github.com/repos/{config.owner}/{config.repo}/releases"
        if not config.release_tag:
            return base + "/latest"
        return base + "/tags/" + urllib.parse.quote(config.release_tag, safe="")

    @staticmethod
    def _find_asset(assets: list, name: str) -> Optional[dict]:
        for asset in assets:
            if asset.get("name") == name:
                return asset
        return None

    # --------------------------------------------------------------- download
    def _download_and_install_worker(self, update: UpdateInfo) -> None:
        try:
            self.ui_queue.put(
                {"type": "update_progress", "message": f"开始下载 {update.exe_asset}…"}
            )
            target = self._update_dir() / update.exe_asset
            self._download_asset(update.exe_url, update.config.token, target)
            self._validate_download(update, target)
            self.ui_queue.put(
                {"type": "update_progress", "message": "校验通过，正在准备安装…"}
            )
            self._perform_install(update, target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Update install failed: {}", exc)
            self.ui_queue.put({"type": "update_failed", "message": str(exc)})

    def _download_asset(self, url: str, token: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        with self._open(url, token, "application/octet-stream") as resp:
            with output_path.open("wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)

    def _validate_download(self, update: UpdateInfo, path: Path) -> None:
        if path.stat().st_size <= 0:
            raise RuntimeError("下载文件为空")
        expected = update.sha256.lower().replace("sha256:", "").strip()
        if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
            raise RuntimeError("更新清单缺少有效的 64 位 SHA-256，拒绝安装")
        actual = _sha256(path)
        if actual.lower() != expected:
            raise RuntimeError(
                f"SHA-256 不匹配\n期望: {expected}\n实际: {actual}"
            )

    # ---------------------------------------------------------------- install
    def _perform_install(self, update: UpdateInfo, new_exe: Path) -> None:
        if not getattr(sys, "frozen", False):
            self.ui_queue.put(
                {
                    "type": "update_progress",
                    "message": (
                        f"源码模式下不会替换可执行文件，已保存到 {new_exe}\n"
                        f"请关闭应用后手动替换或 git pull。"
                    ),
                }
            )
            return

        old_exe = Path(sys.executable).resolve()
        log_path = self._update_dir() / "updater.log"
        script_path = self._update_dir() / "updater.ps1"
        script_path.write_text(_SWAP_SCRIPT, encoding="utf-8")

        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-PidToWait",
            str(os.getpid()),
            "-NewPath",
            str(new_exe),
            "-OldPath",
            str(old_exe),
            "-LogFile",
            str(log_path),
        ]
        # CREATE_NO_WINDOW keeps PowerShell out of any visible console (the
        # GUI parent has none anyway). DETACHED_PROCESS would seem natural but
        # actually breaks PowerShell's Add-Content / file I/O on Windows 10 —
        # the swap script simply never runs. CREATE_NEW_PROCESS_GROUP lets the
        # child outlive the parent exiting in a moment.
        CREATE_NO_WINDOW = 0x08000000
        flags = CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        subprocess.Popen(  # noqa: S603 — args are constructed locally, no shell
            cmd,
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ui_queue.put({"type": "update_installing", "version_name": update.version_name})

    # -------------------------------------------------------------- http core
    def _get_text(self, url: str, token: str, accept: str) -> str:
        with self._open(url, token, accept) as resp:
            return resp.read().decode("utf-8")

    def _open(self, url: str, token: str, accept: str):
        """Walk 30x redirects manually so Authorization is rebuilt per-hop.

        Private-repo release-asset URLs (``api.github.com/.../releases/assets/<id>``)
        302 to ``objects.githubusercontent.com``; that S3-backed host rejects
        requests that arrive with both AWS-signed query params and an
        ``Authorization: Bearer`` header. An internal UpdateManager handles this
        the same way: disable auto-redirect, then re-decide on each hop whether
        the new host is ``api.github.com`` (token attached) or anything else
        (token stripped).
        """
        current = url
        for _ in range(5):
            req = urllib.request.Request(current)
            req.add_header("Accept", accept)
            req.add_header("User-Agent", USER_AGENT)
            host = (urllib.parse.urlparse(current).hostname or "").lower()
            if host == "api.github.com" and token:
                req.add_header("Authorization", f"Bearer {token}")
                req.add_header("X-GitHub-Api-Version", GITHUB_API_VERSION)
            try:
                return self._opener.open(req, timeout=HTTP_READ_TIMEOUT_S)
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    location = exc.headers.get("Location") if exc.headers else None
                    try:
                        exc.close()
                    except Exception:
                        pass
                    if not location:
                        raise RuntimeError(f"Redirect without location: {exc.code}")
                    current = urllib.parse.urljoin(current, location)
                    continue
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from exc
        raise RuntimeError("Too many redirects")

    # ------------------------------------------------------------------ state
    def _update_dir(self) -> Path:
        candidate = self.project_root / "state" / "updates"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            fallback = Path(tempfile.gettempdir()) / "ugreen-nas-factory-test" / "updates"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback

    def _state_path(self) -> Path:
        return self.project_root / "state" / STATE_FILE

    def _read_state(self) -> dict:
        path = self._state_path()
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _write_state(self, **fields) -> None:
        path = self._state_path()
        with self._state_lock:
            data = self._read_state()
            data.update(fields)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(data, fh)
            except OSError:
                pass


class _NoFollowRedirect(urllib.request.HTTPRedirectHandler):
    """Returning None from redirect_request makes urllib surface 3xx as HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _version_tuple(name: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(name or "").lstrip("v").split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            return ()
    return tuple(parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(64 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def format_version() -> str:
    return f"{VERSION_NAME} ({VERSION_CODE})"


_SWAP_SCRIPT = r"""param(
    [Parameter(Mandatory=$true)][int]$PidToWait,
    [Parameter(Mandatory=$true)][string]$NewPath,
    [Parameter(Mandatory=$true)][string]$OldPath,
    [string]$LogFile = ""
)

function Write-LogLine([string]$line) {
    if ($LogFile -ne "") {
        try { Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8 } catch {}
    }
}

function Get-ShortHash([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return "missing" }
    try { return (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.Substring(0, 16) }
    catch { return ("err:" + $_.Exception.Message) }
}

Write-LogLine ("[" + (Get-Date -Format o) + "] swap start pid=" + $PidToWait)
Write-LogLine ("  NewPath=" + $NewPath + " sha=" + (Get-ShortHash $NewPath))
Write-LogLine ("  OldPath=" + $OldPath + " sha=" + (Get-ShortHash $OldPath))

try {
    Wait-Process -Id $PidToWait -Timeout 60 -ErrorAction SilentlyContinue
} catch {}
Start-Sleep -Milliseconds 500

$ok = $false
$backupPath = $OldPath + ".update-backup"
for ($i = 0; $i -lt 20; $i++) {
    try {
        # Use .NET File APIs instead of Move-Item: Move-Item -Force has been
        # observed to report success while leaving both source and destination
        # untouched when launched as a detached subprocess child. The .NET
        # primitives raise on failure, so we can detect it properly.
        if ([System.IO.File]::Exists($backupPath)) { [System.IO.File]::Delete($backupPath) }
        if ([System.IO.File]::Exists($OldPath)) { [System.IO.File]::Move($OldPath, $backupPath) }
        [System.IO.File]::Move($NewPath, $OldPath)
        $ok = $true
        try {
            if ([System.IO.File]::Exists($backupPath)) { [System.IO.File]::Delete($backupPath) }
        } catch {
            Write-LogLine ("[" + (Get-Date -Format o) + "] backup cleanup warning: " + $_.Exception.Message)
        }
        break
    } catch {
        Write-LogLine ("[" + (Get-Date -Format o) + "] swap retry " + $i + ": " + $_.Exception.Message)
        if ((-not [System.IO.File]::Exists($OldPath)) -and [System.IO.File]::Exists($backupPath)) {
            try {
                [System.IO.File]::Move($backupPath, $OldPath)
                Write-LogLine ("[" + (Get-Date -Format o) + "] restored original executable")
            } catch {
                Write-LogLine ("[" + (Get-Date -Format o) + "] CRITICAL restore failed: " + $_.Exception.Message)
            }
        }
        Start-Sleep -Milliseconds 500
    }
}

if (-not $ok) {
    Write-LogLine ("[" + (Get-Date -Format o) + "] swap failed, leaving new file at $NewPath")
    exit 1
}

Write-LogLine ("[" + (Get-Date -Format o) + "] swap done OldPath sha=" + (Get-ShortHash $OldPath))

# Deliberately do NOT auto-launch the new exe. PyInstaller --onefile unpacks
# python312.dll into _MEI* on startup, and Windows Defender's first-execution
# scan on a freshly-written 50MB binary often holds the image image longer than
# any reasonable Start-Sleep can wait out, deleting the extracted DLL before the
# bootstrap loads it (ERROR_MOD_NOT_FOUND, "找不到指定的模块"). The user just
# clicked "下载并安装" so they're at the keyboard — let them re-launch when the
# UI tells them to. Subsequent double-clicks read the post-scan file fine.
exit 0
"""


__all__ = ["UpdateManager", "UpdateInfo", "format_version"]
