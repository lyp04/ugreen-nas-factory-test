from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .logger import logger


TEN_GIB = 10 * 1024 * 1024 * 1024


class SmbTransferError(RuntimeError):
    pass


@dataclass(slots=True)
class SmbTransferConfig:
    unc_share: str
    local_dir: Path
    drive_letter: str = "Z"
    # When source_file is set, we upload that real file instead of a
    # sparse fake. remote_file is named after source_file.name in that case.
    source_file: Path | None = None
    file_name: str = "factory_10gb.bin"
    file_size_bytes: int = TEN_GIB
    username: str | None = None
    password: str | None = None

    @property
    def local_file(self) -> Path:
        if self.source_file is not None:
            return self.source_file
        return self.local_dir / self.file_name

    @property
    def mounted_root(self) -> str:
        return f"{self.drive_letter}:"

    @property
    def remote_file_name(self) -> str:
        if self.source_file is not None:
            return self.source_file.name
        return self.file_name

    @property
    def remote_file(self) -> str:
        return f"{self.mounted_root}\\{self.remote_file_name}"

    @property
    def download_file(self) -> Path:
        """Local path where a downloaded copy lands during read tests."""
        return self.local_dir / f"download_{self.remote_file_name}"

    @property
    def progress_file(self) -> Path:
        return self.local_dir / f"{self.remote_file_name}.progress"


def build_prepare_sparse_file_script(cfg: SmbTransferConfig) -> str:
    return (
        f"$dir = '{_ps_quote(str(cfg.local_dir))}'; "
        "New-Item -ItemType Directory -Force -Path $dir | Out-Null; "
        f"$file = '{_ps_quote(str(cfg.local_file))}'; "
        f"if (-not (Test-Path -LiteralPath $file)) {{ fsutil file createnew $file {cfg.file_size_bytes} | Out-Null }}"
    )


def build_mount_script(cfg: SmbTransferConfig) -> str:
    if cfg.username:
        password = _cmd_quote(cfg.password or "")
        return (
            f"net use {cfg.drive_letter}: {_cmd_quote(cfg.unc_share)} "
            f"{password} /user:{_cmd_quote(cfg.username)} /persistent:no"
        )
    return f"net use {cfg.drive_letter}: {_cmd_quote(cfg.unc_share)} /persistent:no"


def build_unmount_script(cfg: SmbTransferConfig) -> str:
    return f"net use {cfg.drive_letter}: /delete /y"


def build_upload_script(cfg: SmbTransferConfig) -> str:
    return _build_chunked_copy_script(str(cfg.local_file), cfg.remote_file, cfg.progress_file)


def build_download_script(cfg: SmbTransferConfig) -> str:
    return _build_chunked_copy_script(cfg.remote_file, str(cfg.download_file), cfg.progress_file)


def build_cleanup_script(cfg: SmbTransferConfig) -> str:
    parts = [
        f"$download = '{_ps_quote(str(cfg.download_file))}'; ",
        f"$progress = '{_ps_quote(str(cfg.progress_file))}'; ",
        "if (Test-Path -LiteralPath $download) { Remove-Item -LiteralPath $download -Force }; ",
        "if (Test-Path -LiteralPath $progress) { Remove-Item -LiteralPath $progress -Force }; ",
    ]
    if cfg.source_file is None:
        parts.extend(
            [
                f"$local = '{_ps_quote(str(cfg.local_file))}'; ",
                "if (Test-Path -LiteralPath $local) { Remove-Item -LiteralPath $local -Force }",
            ]
        )
    return "".join(parts)


def build_remote_cleanup_script(cfg: SmbTransferConfig) -> str:
    return (
        f"$remote = '{_ps_quote(cfg.remote_file)}'; "
        "if (Test-Path -LiteralPath $remote) { Remove-Item -LiteralPath $remote -Force }"
    )


def run_powershell(script: str, timeout_s: int = 3600) -> subprocess.CompletedProcess[str]:
    logger.info(f"Running PowerShell script: {script}")
    try:
        return subprocess.run(
            _powershell_command(script),
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            **_hidden_process_kwargs(),
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise SmbTransferError(stderr or f"PowerShell failed with exit code {exc.returncode}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SmbTransferError(f"PowerShell timed out after {timeout_s}s") from exc


def start_powershell(script: str) -> subprocess.Popen[str]:
    logger.info(f"Starting PowerShell script: {script}")
    return subprocess.Popen(
        _powershell_command(script),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_process_kwargs(),
    )


def wait_powershell(proc: subprocess.Popen[str], timeout_s: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise SmbTransferError(f"PowerShell timed out after {timeout_s}s") from exc

    if proc.returncode != 0:
        raise SmbTransferError((stderr or "").strip() or f"PowerShell failed with exit code {proc.returncode}")

    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def _build_chunked_copy_script(src: str, dst: str, progress_file: Path) -> str:
    return (
        f"$src = '{_ps_quote(src)}'; "
        f"$dst = '{_ps_quote(dst)}'; "
        f"$progress = '{_ps_quote(str(progress_file))}'; "
        "$progressDir = Split-Path -Parent $progress; "
        "if ($progressDir) { New-Item -ItemType Directory -Force -Path $progressDir | Out-Null }; "
        "if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force }; "
        "if (Test-Path -LiteralPath $progress) { Remove-Item -LiteralPath $progress -Force }; "
        "$buffer = New-Object byte[] (8 * 1024 * 1024); "
        "$total = [Int64]0; "
        "$inputStream = [System.IO.FileStream]::new($src, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read, $buffer.Length, [System.IO.FileOptions]::SequentialScan); "
        "$outputStream = [System.IO.FileStream]::new($dst, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read, $buffer.Length, [System.IO.FileOptions]::WriteThrough); "
        "try { "
        "while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) { "
        "$outputStream.Write($buffer, 0, $read); "
        "$total += $read; "
        "try { [System.IO.File]::WriteAllText($progress, [string]$total) } "
        "catch { "
        "if ($progressDir) { New-Item -ItemType Directory -Force -Path $progressDir -ErrorAction SilentlyContinue | Out-Null }; "
        "try { [System.IO.File]::WriteAllText($progress, [string]$total) } catch { } "
        "} "
        "} "
        "} finally { "
        "$outputStream.Close(); "
        "$inputStream.Close(); "
        "}"
    )


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _cmd_quote(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _powershell_command(script: str) -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        script,
    ]


def _hidden_process_kwargs() -> dict:
    if not sys.platform.startswith("win"):
        return {}

    kwargs: dict = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    startf_use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    sw_hide = getattr(subprocess, "SW_HIDE", 0)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= startf_use_show_window
        startupinfo.wShowWindow = sw_hide
        kwargs["startupinfo"] = startupinfo
    return kwargs
