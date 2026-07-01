"""Deterministic coding codebook.

The Worker (LLM) selects/justifies codes and writes the human-readable claim
summary, but the *allowed* code space per category is fixed. This gives the
Verifier a deterministic grounding surface: any CPT/ICD code the Worker emits that
is not in the category's allowed set is an ungrounded invention (AGENT_HALLUCINATION).
"""
from __future__ import annotations

# category -> {"cpt": [...], "icd10": [...]}
CODEBOOK = {
    "ONBOARDING": {"cpt": ["99201", "99202", "99203"], "icd10": ["Z00.00", "Z02.9"]},
    "RENEWAL":    {"cpt": ["99211", "99212", "99213"], "icd10": ["Z76.89", "Z02.9"]},
    "REVIEW":     {"cpt": ["99213", "99214", "99215"], "icd10": ["Z09", "Z13.9"]},
    "REPORT":     {"cpt": ["99080", "99358", "99359"], "icd10": ["Z02.9", "Z13.9"]},
    "INTAKE":     {"cpt": ["99204", "99205", "99385"], "icd10": ["Z00.00", "Z02.9"]},
}


def allowed_codes(category: str) -> dict:
    return CODEBOOK.get(category or "", {"cpt": [], "icd10": []})


def expected_primary(category: str) -> dict:
    """The canonical single-code choice a well-behaved Worker should pick."""
    cb = allowed_codes(category)
    return {
        "cpt_codes": cb["cpt"][:1],
        "icd10_codes": cb["icd10"][:1],
    }
