"""Order-independent named random streams for the economic runtime."""

from __future__ import annotations

import numpy as np

from ceo_arena.hashing import semantic_digest


class NamedRandom:
    """Create deterministic generators from a scenario seed and semantic path."""

    def __init__(self, seed: int) -> None:
        self._seed = seed

    def generator(self, *path: object) -> np.random.Generator:
        """Return a fresh generator whose state depends only on its semantic path."""
        digest = semantic_digest(self._seed, *path)
        return np.random.default_rng(int.from_bytes(digest[:16], "big"))

    def digest(self, *path: object, length: int = 16) -> str:
        """Return a stable identifier derived from the same namespace."""
        return semantic_digest(self._seed, *path).hex()[:length]
