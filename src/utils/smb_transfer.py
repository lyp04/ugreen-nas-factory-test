from __future__ import annotations

import base64
import ctypes
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ctypes import wintypes

from .logger import logger


TEN_GIB = 10 * 1024 * 1024 * 1024
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


if sys.platform.startswith("win"):
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
else:
    _KERNEL32 = None


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
    drive = _ps_literal(f"{cfg.drive_letter}:")
    share = _ps_literal(cfg.unc_share)
    if cfg.username:
        password = _ps_literal(cfg.password or "")
        user = _ps_literal(f"/user:{cfg.username}")
        return (
            f"& net.exe use {drive} {share} {password} {user} '/persistent:no'"
        )
    return f"& net.exe use {drive} {share} '/persistent:no'"


def build_unmount_script(cfg: SmbTransferConfig) -> str:
    return f"& net.exe use {_ps_literal(f'{cfg.drive_letter}:')} '/delete' '/y'"


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
    logger.info(f"Running PowerShell step ({len(script)} characters)")
    try:
        return subprocess.run(
            _powershell_command(),
            check=True,
            capture_output=True,
            input=_encoded_script(script),
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
    logger.info(f"Starting PowerShell step ({len(script)} characters)")
    proc = subprocess.Popen(
        _powershell_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_hidden_process_kwargs(),
    )
    try:
        _attach_kill_on_close_job(proc)
        if proc.stdin is None:
            raise OSError("PowerShell stdin pipe was not created")
        proc.stdin.write(_encoded_script(script))
        proc.stdin.close()
        # communicate() otherwise tries to flush the already-closed stream.
        proc.stdin = None
    except (BrokenPipeError, OSError) as exc:
        terminate_powershell(proc)
        raise SmbTransferError(f"Could not send script to PowerShell: {exc}") from exc
    return proc


def wait_powershell(proc: subprocess.Popen[str], timeout_s: int = 3600) -> subprocess.CompletedProcess[str]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        terminate_powershell(proc)
        raise SmbTransferError(f"PowerShell timed out after {timeout_s}s") from exc

    _close_kill_on_close_job(proc)
    if proc.returncode != 0:
        raise SmbTransferError((stderr or "").strip() or f"PowerShell failed with exit code {proc.returncode}")

    return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)


def terminate_powershell(proc: subprocess.Popen[str], timeout_s: int = 10) -> None:
    """Terminate the whole Windows transfer process tree and reap the parent.

    A killed launcher can otherwise leave an orphan PowerShell copy process holding
    a multi-gigabyte deleted file open. The attached Job Object is preferred;
    ``taskkill /T`` is the compatibility fallback scoped to the exact child PID.
    """
    # Closing a KILL_ON_JOB_CLOSE job is the only reliable way to cover both
    # normal cancellation and abrupt termination of the Python/GUI parent. The
    # taskkill fallback keeps compatibility with test doubles and older hosts
    # where the process was launched before job attachment was available.
    tree_kill_attempted = _close_kill_on_close_job(proc)
    pid = getattr(proc, "pid", None)
    if not tree_kill_attempted and sys.platform.startswith("win") and isinstance(pid, int) and pid > 0:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1, int(timeout_s)),
                **_hidden_process_kwargs(),
            )
            tree_kill_attempted = result.returncode == 0
        except Exception:
            tree_kill_attempted = False

    if not tree_kill_attempted:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.communicate(timeout=max(1, int(timeout_s)))
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass


def _attach_kill_on_close_job(proc: subprocess.Popen[str]) -> None:
    """Put a transfer PowerShell in a Windows job owned by this process.

    The script is sent only after this succeeds, so a failed assignment cannot
    leave an uncontained copy operation running. Windows closes our job handle
    on a hard crash and terminates PowerShell plus all of its descendants.
    """
    if not sys.platform.startswith("win"):
        return
    if _KERNEL32 is None:
        raise OSError("Windows Job Object API is unavailable")

    process_handle = getattr(proc, "_handle", None)
    if process_handle is None:
        raise OSError("PowerShell process handle is unavailable")

    job = _KERNEL32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _KERNEL32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not _KERNEL32.AssignProcessToJobObject(job, wintypes.HANDLE(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        _KERNEL32.CloseHandle(job)
        raise

    setattr(proc, "_ugreen_kill_job", job)


def _close_kill_on_close_job(proc: subprocess.Popen[str]) -> bool:
    job = getattr(proc, "_ugreen_kill_job", None)
    if not job or _KERNEL32 is None:
        return False
    setattr(proc, "_ugreen_kill_job", None)
    try:
        return bool(_KERNEL32.CloseHandle(job))
    except Exception:
        return False


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
        # Create (overwrite), not CreateNew: the Remove-Item above can lose to a leftover/locked
        # file (interrupted prior transfer, AV scan), and CreateNew then aborts with
        # "文件...已经存在". Create truncates whatever survives, matching the delete-then-write intent.
        "$outputStream = [System.IO.FileStream]::new($dst, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read, $buffer.Length, [System.IO.FileOptions]::WriteThrough); "
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


def _ps_literal(value: str) -> str:
    return f"'{_ps_quote(value)}'"


def _encoded_script(script: str) -> str:
    # stdin contains only ASCII, so Windows PowerShell 5.1 does not depend on the
    # console code page when paths contain Chinese characters. Keeping the script
    # off the command line also prevents SMB credentials from appearing in process
    # listings and diagnostic logs.
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _powershell_command() -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        (
            "$ErrorActionPreference = 'Stop'; "
            "$encoded = [Console]::In.ReadToEnd(); "
            "$script = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded)); "
            "& ([ScriptBlock]::Create($script)); "
            "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
        ),
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
