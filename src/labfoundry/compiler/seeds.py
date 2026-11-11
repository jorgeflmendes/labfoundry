"""Stable namespaced seed derivation."""

from __future__ import annotations

import hashlib


def derive_seed(root_seed: int, namespace: str) -> int:
    """Derive a stable unsigned 64-bit seed for a subsystem namespace.

    Namespaced derivation prevents unrelated compiler changes from shifting every
    pseudo-random choice in a scenario.
    """
    if root_seed < 0:
        raise ValueError("root_seed must be non-negative")
    payload = f"labfoundry:v1:{root_seed}:{namespace}".encode()
    digest = hashlib.blake2b(payload, digest_size=8, person=b"lf-seed1").digest()
    return int.from_bytes(digest, "big", signed=False)
