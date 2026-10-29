"""Cross-process ownership lock for one mutable run directory."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Self

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _KERNEL32.GetExitCodeProcess.restype = wintypes.BOOL
else:
    import fcntl


_BINARY_FLAG = getattr(os, "O_BINARY", 0)
_MAX_LEASE_BYTES = 128
_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)
_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


class RunDirectoryLock:
    """Fail closed when another process already owns the same run."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = None

    def __enter__(self) -> Self:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        stream = self._open_private_file()
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            raise
        stream.seek(0)
        try:
            self._acquire(stream)
        except OSError as exc:
            stream.close()
            raise RuntimeError(f"run directory is already active: {self._path.parent}") from exc
        try:
            self._validate_private_file(self._path, stream)
            self._write_lease(stream, {"pid": os.getpid()})
        except BaseException:
            self._release_and_close(stream)
            raise
        self._stream = stream
        return self

    @staticmethod
    def is_active(path: Path) -> bool:
        """Probe the kernel lock instead of trusting a reusable process ID."""

        try:
            descriptor = os.open(path, os.O_RDWR | _BINARY_FLAG | _NOFOLLOW_FLAG)
            try:
                stream = os.fdopen(descriptor, "r+b")
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                RunDirectoryLock._validate_private_file(path, stream)
                stream.seek(0)
                try:
                    RunDirectoryLock._acquire(stream)
                except OSError:
                    return True
                RunDirectoryLock._release(stream)
                return False
        except FileNotFoundError:
            return False
        except PermissionError:
            return True
        except (OSError, RuntimeError):
            return False

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            self._validate_private_file(self._path, stream)
            self._write_lease(stream, None)
        finally:
            self._release_and_close(stream)

    def _open_private_file(self) -> BinaryIO:
        flags = os.O_RDWR | _BINARY_FLAG | _NOFOLLOW_FLAG
        try:
            descriptor = os.open(self._path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                descriptor = os.open(self._path, flags)
            except OSError as exc:
                raise RuntimeError("run lock must be a private regular file") from exc
        try:
            stream = os.fdopen(descriptor, "r+b")
        except BaseException:
            os.close(descriptor)
            raise
        try:
            self._validate_private_file(self._path, stream)
        except BaseException:
            stream.close()
            raise
        return stream

    @staticmethod
    def _validate_private_file(path: Path, stream: BinaryIO) -> None:
        try:
            directory_entry = path.lstat()
            opened_file = os.fstat(stream.fileno())
        except OSError as exc:
            raise RuntimeError("run lock must be a private regular file") from exc
        if (
            not stat.S_ISREG(directory_entry.st_mode)
            or not stat.S_ISREG(opened_file.st_mode)
            or directory_entry.st_nlink != 1
            or opened_file.st_nlink != 1
            or not os.path.samestat(directory_entry, opened_file)
            or bool(getattr(directory_entry, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)
        ):
            raise RuntimeError("run lock must be a private regular file")

    @staticmethod
    def _acquire(stream: BinaryIO) -> None:
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _release(stream: BinaryIO) -> None:
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _release_and_close(cls, stream: BinaryIO) -> None:
        try:
            stream.seek(0)
        finally:
            try:
                cls._release(stream)
            finally:
                stream.close()

    @staticmethod
    def _write_lease(stream: BinaryIO, payload: dict[str, int] | None) -> None:
        encoded = b"\0"
        if payload:
            encoded += json.dumps(payload, separators=(",", ":")).encode()
        stream.seek(0)
        stream.write(encoded)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        ctypes.set_last_error(0)
        process = _KERNEL32.OpenProcess(0x1000, False, pid)
        if process:
            exit_code = wintypes.DWORD()
            queried = _KERNEL32.GetExitCodeProcess(process, ctypes.byref(exit_code))
            _KERNEL32.CloseHandle(process)
            return bool(queried and exit_code.value == 259)
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
