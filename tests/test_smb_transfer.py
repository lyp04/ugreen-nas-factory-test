from __future__ import annotations

import base64
import ctypes
import io
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils import smb_transfer


def test_mount_script_quotes_powershell_metacharacters() -> None:
    cfg = smb_transfer.SmbTransferConfig(
        unc_share=r"\\192.0.2.10\测试共享",
        local_dir=Path("."),
        drive_letter="H",
        username="factory'user",
        password="pa$`ss'word",
    )

    script = smb_transfer.build_mount_script(cfg)

    assert "'pa$`ss''word'" in script
    assert "'/user:factory''user'" in script
    assert "'\\\\192.0.2.10\\测试共享'" in script


def test_run_powershell_keeps_script_off_command_line_and_logs(monkeypatch) -> None:
    secret = "field-test-secret"
    script = f"Write-Output '{secret}'"
    call: dict[str, object] = {}
    log_messages: list[str] = []

    def fake_run(args, **kwargs):
        call["args"] = args
        call["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, "ok\n", "")

    monkeypatch.setattr(smb_transfer.subprocess, "run", fake_run)
    monkeypatch.setattr(smb_transfer.logger, "info", log_messages.append)

    result = smb_transfer.run_powershell(script, timeout_s=12)

    assert result.stdout == "ok\n"
    assert secret not in " ".join(call["args"])
    assert all(secret not in message for message in log_messages)
    kwargs = call["kwargs"]
    encoded = kwargs["input"]
    assert isinstance(encoded, str)
    assert secret not in encoded
    assert base64.b64decode(encoded).decode("utf-16-le") == script
    assert kwargs["timeout"] == 12


def test_start_powershell_writes_encoded_script_to_stdin(monkeypatch) -> None:
    secret = "another-secret"
    script = f"Write-Output '{secret}'"
    written = io.StringIO()
    command: list[str] = []

    class RecordingInput:
        def write(self, value: str) -> int:
            return written.write(value)

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self, args: list[str]) -> None:
            self.args = args
            self.stdin = RecordingInput()

        def kill(self) -> None:
            return None

        def communicate(self):
            return "", ""

    def fake_popen(args, **_kwargs):
        command.extend(args)
        return FakeProcess(args)

    monkeypatch.setattr(smb_transfer.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(smb_transfer, "_attach_kill_on_close_job", lambda _proc: None)

    proc = smb_transfer.start_powershell(script)

    assert proc.stdin is None
    assert secret not in " ".join(command)
    assert secret not in written.getvalue()
    assert base64.b64decode(written.getvalue()).decode("utf-16-le") == script


def test_start_powershell_fails_closed_when_job_attachment_fails(monkeypatch) -> None:
    class FakeProcess:
        stdin = io.StringIO()
        killed = False

        def kill(self) -> None:
            self.killed = True

        def communicate(self, timeout: int):
            return "", ""

    proc = FakeProcess()
    monkeypatch.setattr(smb_transfer.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(
        smb_transfer,
        "_attach_kill_on_close_job",
        lambda _proc: (_ for _ in ()).throw(OSError("job denied")),
    )

    with pytest.raises(smb_transfer.SmbTransferError, match="Could not send script"):
        smb_transfer.start_powershell("Write-Output test")

    assert proc.killed is True


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="requires Windows PowerShell")
def test_powershell_transport_handles_unicode_paths_on_windows(tmp_path) -> None:
    sync_path = tmp_path / "同步传输.txt"
    async_path = tmp_path / "异步传输.txt"

    smb_transfer.run_powershell(
        f"[IO.File]::WriteAllText({smb_transfer._ps_literal(str(sync_path))}, 'sync')",
        timeout_s=30,
    )
    proc = smb_transfer.start_powershell(
        f"[IO.File]::WriteAllText({smb_transfer._ps_literal(str(async_path))}, 'async')"
    )
    smb_transfer.wait_powershell(proc, timeout_s=30)

    assert sync_path.read_text(encoding="utf-8-sig") == "sync"
    assert async_path.read_text(encoding="utf-8-sig") == "async"


def test_terminate_powershell_uses_windows_process_tree(monkeypatch) -> None:
    calls: list[list[str]] = []

    class FakeProcess:
        pid = 4242
        killed = False
        communicated = False

        def kill(self) -> None:
            self.killed = True

        def communicate(self, timeout: int):
            assert timeout == 10
            self.communicated = True
            return "", ""

    proc = FakeProcess()
    monkeypatch.setattr(smb_transfer.sys, "platform", "win32")
    monkeypatch.setattr(smb_transfer, "_hidden_process_kwargs", lambda: {})
    monkeypatch.setattr(
        smb_transfer.subprocess,
        "run",
        lambda args, **_kwargs: (
            calls.append(args)
            or subprocess.CompletedProcess(args, 0)
        ),
    )

    smb_transfer.terminate_powershell(proc)

    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]
    assert proc.communicated is True
    assert proc.killed is False


def test_terminate_powershell_prefers_attached_job(monkeypatch) -> None:
    class FakeProcess:
        pid = 4242
        communicated = False

        def kill(self) -> None:
            raise AssertionError("process kill fallback should not be used")

        def communicate(self, timeout: int):
            self.communicated = True
            return "", ""

    proc = FakeProcess()
    monkeypatch.setattr(smb_transfer, "_close_kill_on_close_job", lambda _proc: True)
    monkeypatch.setattr(
        smb_transfer.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("taskkill fallback should not be used")
        ),
    )

    smb_transfer.terminate_powershell(proc)

    assert proc.communicated is True


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="requires Windows process tree")
def test_terminate_powershell_reaps_real_windows_process() -> None:
    proc = smb_transfer.start_powershell("Start-Sleep -Seconds 60")

    smb_transfer.terminate_powershell(proc)

    assert proc.poll() is not None


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="requires Windows Job Objects")
def test_powershell_job_kills_child_when_python_parent_hard_exits(tmp_path) -> None:
    pid_file = tmp_path / "orphan.pid"
    repo_root = Path(__file__).resolve().parents[1]
    child_code = (
        "import os\n"
        "from pathlib import Path\n"
        "from src.utils.smb_transfer import start_powershell\n"
        "proc = start_powershell('Start-Sleep -Seconds 60')\n"
        f"Path({str(pid_file)!r}).write_text(str(proc.pid), encoding='ascii')\n"
        "os._exit(0)\n"
    )

    subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        check=True,
        timeout=30,
    )
    pid = int(pid_file.read_text(encoding="ascii"))

    synchronize = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return
    try:
        assert ctypes.windll.kernel32.WaitForSingleObject(handle, 5_000) == 0
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
