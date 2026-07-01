"""Verifier / Critic agent — the agent-checks-agent gate.

It INDEPENDENTLY grounds the Worker's draft against the source record and the fixed
codebook. It can OVERRULE a confident Worker. Its checks are deterministic (no LLM
needed to compare an emitted amount to the source amount), which is exactly the kind
of validation the design says must be code, not a model.

Verdicts:
  pass         draft is grounded and confident -> may proceed to review
  fail         draft contradicts the source or invents codes -> AGENT_HALLUCINATION
               / AGENT_MALFORMED (Worker is overruled; disagreement logged both ways)
  needs_human  Worker abstained or confidence too low -> LOW_CONFIDENCE (route)
"""
from __future__ import annotations

from .base import Agent, AgentSpec
from ..events import NormalizedRecord, WorkerDraft, VerifierVerdict
from ..codebook import allowed_codes

CONFIDENCE_FLOOR = 0.55
PROMPT_VERSION = "verifier-v2"


class Verifier(Agent):
    spec = AgentSpec(
        name="Verifier",
        role="verifier",
        models=[],  # deterministic grounding; no LLM spend
        prompt_version=PROMPT_VERSION,
        can_call=[],
        input_type="WorkerDraft",
        output_type="VerifierVerdict",
    )

    def verify(self, rec: NormalizedRecord, draft: WorkerDraft) -> VerifierVerdict:
        disagreements = []

        # 1. Abstain / low confidence -> route to a human (LOW_CONFIDENCE).
        if draft.abstain or draft.confidence < CONFIDENCE_FLOOR:
            verdict = VerifierVerdict(
                record_id=rec.id, verdict="needs_human", reason_code="LOW_CONFIDENCE",
                disagreements=[], notes=(
                    f"worker abstained/low confidence ({draft.confidence:.2f})"))
            self._emit(rec, draft, verdict)
            return verdict

        # 2. Factual grounding: amount + category must match the source exactly.
        if not _num_eq(draft.normalized_amount, rec.amount):
            disagreements.append({"field": "normalized_amount",
                                  "worker": draft.normalized_amount, "source": rec.amount})
        if (draft.category or None) != (rec.category or None):
            disagreements.append({"field": "category",
                                  "worker": draft.category, "source": rec.category})

        # 3. Code grounding: every emitted code must live in the category's codebook.
        cb = allowed_codes(rec.category)
        bad_cpt = [c for c in draft.cpt_codes if c not in cb["cpt"]]
        bad_icd = [c for c in draft.icd10_codes if c not in cb["icd10"]]
        for c in bad_cpt:
            disagreements.append({"field": "cpt_code", "worker": c, "source": cb["cpt"]})
        for c in bad_icd:
            disagreements.append({"field": "icd10_code", "worker": c, "source": cb["icd10"]})

        # 4. Structural emptiness -> malformed (repairable upstream, else abstain).
        if not draft.cpt_codes or not draft.icd10_codes:
            verdict = VerifierVerdict(
                record_id=rec.id, verdict="fail", reason_code="AGENT_MALFORMED",
                disagreements=disagreements + [{"field": "codes", "worker": [], "source": "non-empty"}],
                notes="worker produced empty code set")
            self._emit(rec, draft, verdict)
            return verdict

        if disagreements:
            verdict = VerifierVerdict(
                record_id=rec.id, verdict="fail", reason_code="AGENT_HALLUCINATION",
                disagreements=disagreements,
                notes="worker output not grounded in source; VERIFIER OVERRULES WORKER")
            self._emit(rec, draft, verdict)
            return verdict

        verdict = VerifierVerdict(record_id=rec.id, verdict="pass", reason_code=None,
                                  disagreements=[], notes="grounded and confident")
        self._emit(rec, draft, verdict)
        return verdict

    def _emit(self, rec, draft, verdict: VerifierVerdict):
        self.log("verify", record_id=rec.id, verdict=verdict.verdict,
                 reason_code=verdict.reason_code, disagreements=verdict.disagreements,
                 worker_confidence=draft.confidence)
        self.bus.publish("verifier.verdict", verdict)


def _num_eq(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False
