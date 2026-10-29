"""Canonical hashing and named random-stream seeds."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel


def canonical_model_hash(model: BaseModel) -> str:
    """Hash a Pydantic model through sorted canonical JSON."""
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


stable_model_hash = canonical_model_hash


def provider_endpoint_fingerprint(base_url: str) -> str:
    """Hash a normalized, secret-free provider endpoint identity."""

    parsed = urlsplit(base_url.rstrip("/"))
    normalized = urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def semantic_digest(root_seed: int, *parts: object) -> bytes:
    """Return one SHA-256 digest for a semantic random-stream path."""
    payload = "\x1f".join((str(root_seed), *(str(part) for part in parts))).encode("utf-8")
    return hashlib.sha256(payload).digest()


def named_seed(root_seed: int, *parts: object) -> int:
    """Derive an order-independent NumPy seed for one economic event domain."""
    return int.from_bytes(semantic_digest(root_seed, *parts)[:8], "big", signed=False)
