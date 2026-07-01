"""Domain plug-in loader.

Everything domain-specific (the coding codebook, the valid category set) lives in an
external `domain_config.json`, selected by the `DOMAIN_CONFIG` env var. The pipeline
code — intake, normalization, rules, agents, audit — is domain-agnostic; swapping the
config + seed runs the *same* fleet on a different vertical (see `domain_config.alt.json`
+ `seed_alt/`). This is what proves generalization is structural, not RCM-worded.
"""
from __future__ import annotations
import json
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_cache: dict = {}


def config_path() -> Path:
    return Path(os.environ.get("DOMAIN_CONFIG", str(_ROOT / "domain_config.json")))


def load() -> dict:
    p = str(config_path())
    if p not in _cache:
        _cache[p] = json.loads(Path(p).read_text(encoding="utf-8"))
    return _cache[p]


def categories() -> set:
    return set(load().get("categories", []))


def codebook() -> dict:
    return load().get("codebook", {})


def domain_name() -> str:
    return load().get("name", "generic")
