"""Coding codebook — now loaded from the active domain config (see fleet/domain.py).

The Worker (LLM) selects/justifies codes and writes the summary, but the *allowed*
code space per category is a fixed, external map. That gives the Verifier a
deterministic grounding surface: any code the Worker emits that is not in the
category's allowed set is an ungrounded invention (AGENT_HALLUCINATION). Because the
map is config, the SAME grounding logic works for any vertical, not just RCM.
"""
from __future__ import annotations

from .domain import codebook as _codebook


def allowed_codes(category: str) -> dict:
    return _codebook().get(category or "", {"cpt": [], "icd10": []})


def expected_primary(category: str) -> dict:
    """The canonical single-code choice a well-behaved Worker should pick."""
    cb = allowed_codes(category)
    return {
        "cpt_codes": cb.get("cpt", [])[:1],
        "icd10_codes": cb.get("icd10", [])[:1],
    }
