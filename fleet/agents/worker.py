"""CodingWorker agent — the LLM-heavy Assembly step.

Given a normalized encounter it drafts a structured claim (CPT/ICD codes + summary)
with a confidence and an abstain flag. This is the ONLY load-bearing LLM in the
pipeline. Structured output is enforced with a bounded repair/retry; if it still
can't produce valid structure it abstains (-> routed to a human).
"""
from __future__ import annotations

from .base import Agent, AgentSpec
from ..events import NormalizedRecord, WorkerDraft
from ..llm import LLMClient, LLMResult

PROMPT_VERSION = "coder-v3"


def build_worker_request(rec: NormalizedRecord, attempt: int, model: str) -> dict:
    """Deterministic request payload — also the replay key."""
    return {
        "agent": "CodingWorker",
        "prompt_version": PROMPT_VERSION,
        "record_id": rec.id,
        "attempt": attempt,
        "model": model,
        "encounter": {
            "id": rec.id,
            "owner": rec.owner,
            "deadline": rec.deadline,
            "category": rec.category,
            "amount": rec.amount,
            "notes": rec.notes,
        },
    }


class MalformedDraft(Exception):
    pass


class CodingWorker(Agent):
    spec = AgentSpec(
        name="CodingWorker",
        role="worker",
        models=["gpt-4o-mini", "gpt-4o"],
        prompt_version=PROMPT_VERSION,
        can_call=[],
        input_type="NormalizedRecord",
        output_type="WorkerDraft",
    )

    def __init__(self, bus, audit, llm: LLMClient):
        super().__init__(bus, audit)
        self.llm = llm

    def draft(self, rec: NormalizedRecord, attempt: int, model: str) -> tuple[WorkerDraft, LLMResult]:
        request = build_worker_request(rec, attempt, model)
        result = self.llm.call(self.spec.name, PROMPT_VERSION, request, model)
        draft = self._parse(rec, result.response)
        self.log("worker_draft", record_id=rec.id, attempt=attempt, model=result.model,
                 abstain=draft.abstain, confidence=draft.confidence,
                 transcript_hash=result.transcript_hash)
        self.bus.publish("worker.drafted", draft)
        return draft, result

    def _parse(self, rec: NormalizedRecord, resp) -> WorkerDraft:
        if not isinstance(resp, dict):
            raise MalformedDraft("worker response is not a JSON object")
        required = ("cpt_codes", "icd10_codes", "normalized_amount",
                    "category", "claim_summary", "confidence", "abstain")
        missing = [k for k in required if k not in resp]
        if missing:
            raise MalformedDraft(f"worker draft missing keys: {missing}")
        if not isinstance(resp["cpt_codes"], list) or not isinstance(resp["icd10_codes"], list):
            raise MalformedDraft("codes must be arrays")
        return WorkerDraft(
            record_id=rec.id,
            cpt_codes=[str(c) for c in resp["cpt_codes"]],
            icd10_codes=[str(c) for c in resp["icd10_codes"]],
            normalized_amount=resp["normalized_amount"],
            category=resp["category"],
            claim_summary=str(resp["claim_summary"]),
            confidence=float(resp["confidence"]),
            abstain=bool(resp["abstain"]),
            model=self.spec.name,
            prompt_version=PROMPT_VERSION,
        )
