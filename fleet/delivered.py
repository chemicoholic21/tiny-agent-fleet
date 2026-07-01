"""Canonical delivered-fields builder.

Used identically by the Orchestrator (at delivery time) and by the transcript
generator (at record time), so the delivered_fields_hash stamped in a committed
worker transcript always matches the hash the runtime recomputes. This is what lets
verify_audit.py prove the delivered output hashes back to a worker's LLM call.
"""
from __future__ import annotations
from typing import Optional

from .events import NormalizedRecord, WorkerDraft
from .util import sha


def build_delivered_fields(rec: NormalizedRecord, draft: WorkerDraft) -> dict:
    return {
        "record_id": rec.id,
        "owner": rec.owner,
        "deadline": rec.deadline,
        "category": rec.category,
        "normalized_amount": rec.amount,
        "cpt_codes": list(draft.cpt_codes),
        "icd10_codes": list(draft.icd10_codes),
        "claim_summary": draft.claim_summary,
        "source_format": rec.source_format,
    }


def delivered_fields_hash(rec: NormalizedRecord, draft: WorkerDraft) -> str:
    return sha(build_delivered_fields(rec, draft))
