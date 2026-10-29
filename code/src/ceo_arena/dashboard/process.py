"""Detached Campaign process seam used by the local dashboard control plane."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

type CampaignProcessMode = Literal["run", "resume"]

_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
_BINARY_FLAG = getattr(os, "O_BINARY", 0)


@dataclass(frozen=True, slots=True)
class CampaignProcessRequest:
    """Private filesystem inputs required to launch one Campaign worker."""

    campaign_id: str
    mode: CampaignProcessMode
    spec_path: Path
    output: Path


class CampaignProcessHandle(Protocol):
    """Minimal liveness view retained only as a startup hint."""

    def is_running(self) -> bool:
        """Return whether the launched worker has not exited."""

    def exit_code(self) -> int | None:
        """Return the terminal exit code, or None while still running."""


class CampaignProcessPort(Protocol):
    """Internal seam for starting a detached Campaign worker."""

    def start(self, request: CampaignProcessRequest) -> CampaignProcessHandle:
        """Start one worker without waiting for the Campaign to finish."""


@dataclass(slots=True)
class _PopenHandle:
    """Production liveness hint backed by one local child process."""

    process: subprocess.Popen[bytes]

    def is_running(self) -> bool:
        """Return whether the child has not exited."""

        return self.process.poll() is None

    def exit_code(self) -> int | None:
        """Return the child exit code without waiting."""

        return self.process.poll()


class DetachedProcessAdapter:
    """Launch Campaign CLI workers independently from the Dashboard lifecycle."""

    def __init__(self, python_executable: Path | None = None) -> None:
        self._python_executable = (python_executable or Path(sys.executable)).resolve()

    def start(self, request: CampaignProcessRequest) -> CampaignProcessHandle:
        """Start a hidden process and redirect both streams to private files."""

        log_directory = request.output / ".control-logs"
        _ensure_private_directory(log_directory, request.output)
        token = uuid.uuid4().hex
        stdout = _open_private_log(log_directory / f"worker-{token}.stdout.log")
        try:
            stderr = _open_private_log(log_directory / f"worker-{token}.stderr.log")
        except BaseException:
            stdout.close()
            raise
        command = (
            str(self._python_executable),
            "-m",
            "ceo_arena.cli",
            "campaign",
            request.mode,
            "--spec",
            str(request.spec_path),
            "--output",
            str(request.output),
        )
        creation_flags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                shell=False,
                close_fds=True,
                creationflags=creation_flags,
                start_new_session=start_new_session,
            )
        finally:
            stdout.close()
            stderr.close()
        return _PopenHandle(process)


def _ensure_private_directory(directory: Path, output: Path) -> None:
    """Create a non-linked log directory contained by the Campaign output."""

    safe_output = output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    safe_directory = directory.resolve()
    metadata = directory.lstat()
    if (
        not safe_directory.is_relative_to(safe_output)
        or not stat.S_ISDIR(metadata.st_mode)
        or bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_FLAG)
        or directory.is_symlink()
    ):
        raise RuntimeError("Campaign control log directory is unsafe")


def _open_private_log(path: Path) -> BinaryIO:
    """Open one new owner-only binary log without following an existing link."""

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY_FLAG, 0o600)
    return os.fdopen(descriptor, "wb")


__all__ = [
    "CampaignProcessHandle",
    "CampaignProcessMode",
    "CampaignProcessPort",
    "CampaignProcessRequest",
    "DetachedProcessAdapter",
]
