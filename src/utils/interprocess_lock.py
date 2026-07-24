from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_WINDOWS_WAIT_OBJECT_0 = 0x00000000
_WINDOWS_WAIT_ABANDONED = 0x00000080
_WINDOWS_WAIT_TIMEOUT = 0x00000102


class InterProcessLockError(RuntimeError):
    """Raised when the operating-system lock primitive itself fails."""


@dataclass(frozen=True, slots=True)
class InterProcessLockToken:
    """Capability proving which process and thread owns one lock acquisition."""

    lock_id: str
    owner_pid: int
    owner_thread_id: int
    nonce: str


class _LockBackend(Protocol):
    def try_acquire(self) -> bool: ...

    def release(self) -> None: ...

    def close_inherited(self) -> None: ...


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _reset_local_locks_after_fork() -> None:
    """A child must not inherit the parent's locked threading.Lock objects."""

    global _LOCAL_LOCKS, _LOCAL_LOCKS_GUARD
    _LOCAL_LOCKS = {}
    _LOCAL_LOCKS_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_local_locks_after_fork)


def _local_lock_for(lock_id: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(lock_id)
        if lock is None:
            lock = threading.Lock()
            _LOCAL_LOCKS[lock_id] = lock
        return lock


def _lock_id(namespace: str, key: str) -> str:
    payload = f"{namespace}\0{key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _windows_wait_result_is_acquired(result: int) -> bool:
    # WAIT_ABANDONED means the prior owner died without releasing the mutex. The
    # calling thread now owns it and must release it normally.
    return result in {_WINDOWS_WAIT_OBJECT_0, _WINDOWS_WAIT_ABANDONED}


class _WindowsMutexBackend:
    def __init__(self, lock_id: str) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        self._ctypes = ctypes
        self._kernel32 = kernel32
        # Device and transfer leases protect machine-wide hardware resources.
        # ``Local\\`` would permit one owner per Windows logon/RDP session, so
        # use the global kernel namespace. If the account cannot create/open the
        # object, CreateMutexW fails and we propagate the error (fail closed); a
        # process-local fallback would silently remove the safety guarantee.
        self._name = _windows_mutex_name(lock_id)
        self._handle = None

    def try_acquire(self) -> bool:
        handle = self._kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            error = self._ctypes.get_last_error()
            raise InterProcessLockError(f"CreateMutexW failed with Windows error {error}")

        result = int(self._kernel32.WaitForSingleObject(handle, 0))
        if _windows_wait_result_is_acquired(result):
            self._handle = handle
            return True
        if result == _WINDOWS_WAIT_TIMEOUT:
            self._kernel32.CloseHandle(handle)
            return False

        error = self._ctypes.get_last_error()
        self._kernel32.CloseHandle(handle)
        raise InterProcessLockError(
            f"WaitForSingleObject returned 0x{result:08X} (Windows error {error})"
        )

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            raise InterProcessLockError("Windows mutex is not acquired")
        if not self._kernel32.ReleaseMutex(handle):
            error = self._ctypes.get_last_error()
            raise InterProcessLockError(f"ReleaseMutex failed with Windows error {error}")

        # ReleaseMutex already made the mutex available. Closing the now-unowned
        # handle is resource cleanup, so a CloseHandle diagnostic must not leave
        # the Python-side lock falsely marked as held.
        self._handle = None
        self._kernel32.CloseHandle(handle)

    def close_inherited(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            self._kernel32.CloseHandle(handle)


class _PosixFlockBackend:
    def __init__(self, lock_id: str, lock_dir: Path | None) -> None:
        self._lock_id = lock_id
        self._lock_dir = lock_dir or Path(tempfile.gettempdir()) / "ugreen-nas-factory-test-locks"
        self._file = None

    def try_acquire(self) -> bool:
        import errno
        import fcntl

        try:
            self._lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self._lock_dir / f"{self._lock_id}.lock"
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise InterProcessLockError(f"Could not open POSIX lock file: {exc}") from exc

        lock_file = os.fdopen(fd, "a+b", buffering=0)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise InterProcessLockError(f"flock acquire failed: {exc}") from exc

        self._file = lock_file
        return True

    def release(self) -> None:
        import fcntl

        lock_file = self._file
        if lock_file is None:
            raise InterProcessLockError("POSIX file lock is not acquired")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise InterProcessLockError(f"flock release failed: {exc}") from exc
        self._file = None
        try:
            lock_file.close()
        except OSError:
            # LOCK_UN already made the lease available. Do not leave the
            # process-local layer falsely marked as held because close failed.
            pass

    def close_inherited(self) -> None:
        lock_file = self._file
        self._file = None
        if lock_file is not None:
            lock_file.close()


def _windows_mutex_name(lock_id: str) -> str:
    return f"Global\\UGREEN_NAS_FACTORY_LOCK_V1_{lock_id}"


class InterProcessLock:
    """Non-reentrant, crash-recoverable lock shared by local application processes.

    ``try_acquire`` returns an explicit token on success and ``None`` when busy.
    Only the process and thread represented by that exact token may release the
    lock. A repeated release or a release using a foreign/forged token returns
    ``False`` and leaves the real owner's lock untouched.

    Windows uses a machine-wide named mutex. POSIX systems use ``flock`` on a
    stable file under the system temporary directory. A small process-local
    ``threading.Lock`` layer keeps both backends non-reentrant within one Python
    process as well.
    """

    def __init__(
        self,
        key: str,
        *,
        namespace: str = "ugreen-nas-factory-test",
        lock_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("lock key must be a non-empty string")
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("lock namespace must be a non-empty string")

        self.key = key
        self.namespace = namespace
        self.lock_id = _lock_id(namespace, key)
        self._lock_dir = Path(lock_dir).resolve() if lock_dir is not None else None
        self._local_lock = _local_lock_for(self.lock_id)
        self._state_guard = threading.Lock()
        self._backend: _LockBackend | None = None
        self._token: InterProcessLockToken | None = None
        self._instance_pid = os.getpid()

    @property
    def token(self) -> InterProcessLockToken | None:
        with self._state_guard:
            self._discard_inherited_state_if_needed()
            return self._token

    @property
    def acquired(self) -> bool:
        return self.token is not None

    def try_acquire(self) -> InterProcessLockToken | None:
        """Try once without blocking and return an ownership token on success."""

        with self._state_guard:
            self._discard_inherited_state_if_needed()
            if self._token is not None:
                return None
            if not self._local_lock.acquire(blocking=False):
                return None

            try:
                backend = self._new_backend()
                acquired = backend.try_acquire()
            except Exception:
                self._local_lock.release()
                raise
            if not acquired:
                self._local_lock.release()
                return None

            token = InterProcessLockToken(
                lock_id=self.lock_id,
                owner_pid=os.getpid(),
                owner_thread_id=threading.get_ident(),
                nonce=uuid.uuid4().hex,
            )
            self._backend = backend
            self._token = token
            return token

    def release(self, token: InterProcessLockToken) -> bool:
        """Release only when ``token`` is the current thread's exact token.

        Returning ``False`` for a stale, repeated, cross-process, cross-thread, or
        otherwise foreign token is intentional: the actual owner's lock remains
        held and is never accidentally released.
        """

        with self._state_guard:
            self._discard_inherited_state_if_needed()
            if not isinstance(token, InterProcessLockToken):
                return False
            current = self._token
            if (
                current is None
                or token != current
                or token.owner_pid != os.getpid()
                or token.owner_thread_id != threading.get_ident()
            ):
                return False

            backend = self._backend
            if backend is None:
                return False
            backend.release()
            self._backend = None
            self._token = None
            self._local_lock.release()
            return True

    def _new_backend(self) -> _LockBackend:
        if os.name == "nt":
            return _WindowsMutexBackend(self.lock_id)
        return _PosixFlockBackend(self.lock_id, self._lock_dir)

    def _discard_inherited_state_if_needed(self) -> None:
        current_pid = os.getpid()
        if current_pid == self._instance_pid:
            return

        # ``fork`` duplicates file descriptors/handles and Python object state.
        # Closing the child's copy does not release the parent's independent OS
        # ownership, and prevents a child from using the parent's token.
        if self._backend is not None:
            self._backend.close_inherited()
        self._backend = None
        self._token = None
        self._instance_pid = current_pid
        self._local_lock = _local_lock_for(self.lock_id)


__all__ = [
    "InterProcessLock",
    "InterProcessLockError",
    "InterProcessLockToken",
]
