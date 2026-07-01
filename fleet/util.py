"""Shared deterministic hashing utilities.

The canonicalisation here MUST match verify_audit.py exactly, because the grading
gate re-hashes delivered_fields and transcript responses with the same algorithm.
"""
from __future__ import annotations
import hashlib
import json
from typing import Any


def canon(obj: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, tight separators, unicode preserved."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha(obj: Any) -> str:
    """sha256: prefixed hash of the canonical JSON form of an object."""
    return "sha256:" + hashlib.sha256(canon(obj)).hexdigest()


def sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha_text(text: str) -> str:
    return sha_bytes(text.encode("utf-8"))


def hexof(hashstr: str) -> str:
    """Return the bare hex digest from a 'sha256:...' string."""
    return hashstr.split(":")[-1]
