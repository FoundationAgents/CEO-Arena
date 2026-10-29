"""Find dashboard metadata without traversing model histories or backups."""

from collections.abc import Iterator
from pathlib import Path

_PRIVATE_DIRECTORIES = frozenset(
    {".git", "agent-memory", "agent-traces", "allowances", "recovery-patches"}
)


def metadata_files(root: Path, filename: str) -> Iterator[Path]:
    """Walk only metadata-bearing directories, without following links."""
    for directory, children, files in root.walk(follow_symlinks=False):
        children[:] = [name for name in children if name.casefold() not in _PRIVATE_DIRECTORIES]
        if filename in files:
            yield directory / filename
