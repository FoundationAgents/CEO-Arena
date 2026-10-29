"""Crash-safe persistence primitives for immutable Pydantic records."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


class AtomicModelFile[ModelT: BaseModel]:
    """Atomically replace one JSON document while preserving the previous file."""

    def __init__(self, path: Path, model_type: type[ModelT]) -> None:
        self.path = path
        self._model_type = model_type

    def load(self) -> ModelT | None:
        """Load and validate the document, or return None when absent."""

        if not self.path.exists():
            return None
        return self._model_type.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, model: ModelT) -> None:
        """Write, fsync, and atomically publish a complete JSON document."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(model.model_dump_json())
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            self._fsync_directory()
        finally:
            temporary_path.unlink(missing_ok=True)

    def _fsync_directory(self) -> None:
        """Persist the directory entry on platforms that support directory fsync."""

        if os.name == "nt":
            return
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
