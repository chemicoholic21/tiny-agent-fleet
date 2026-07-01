"""Orchestrator / Planner agent — owns the run.

It holds no business logic of its own; it DELEGATES to the Router, Worker and
Verifier and enforces the governance envelope:
  * per-record step + cost budgets (BUDGET_EXCEEDED / AGENT_LOOP)
  * bounded retry with escalation when the Verifier rejects the Worker
  * routing every failure to the exception queue with the right reason code + class
  * the approval state machine + the CASE_ID amendment gate before delivery

The output is a fully-populated per-record result carrying the agent_trace,
approval_trail, delivered_fields and provenance hashes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from .base import Agent, AgentSpec
from .router import Router
from .worker import CodingWorker, MalformedDraft
from .verifier import Verifier
from ..events import NormalizedRecord, WorkerDraft
from ..rules import RuleFinding
from ..state_machine import ApprovalState, RCM_APPROVAL_CHAIN
from ..delivered import build_delivered_fields
from ..util import sha
from ..codebook import expected_primary

MAX_RETRIES = 2  # bounded retries after a Verifier rejection


@dataclass
class RecordResult:
    id: str
    version: int
    source_format: str
    source_version_hash: str
    status: str = "exception"                # delivered | exception | superseded
    reason_code: Optional[str] = None
    reason_class: Optional[str] = None
    transcript_hash: Optional[str] = None
    delivered_fields: Optional[dict] = None
    delivered_fields_hash: Optional[str] = None
    agent_trace: list = field(default_factory=list)
    approval_trail: list = field(default_factory=list)
    amount: Optional[float] = None
    claim_summary: Optional[str] = None


class Orchestrator(Agent):
    spec = AgentSpec(
        name="Orchestrator",
        role="orchestrator",
        models=[],
        prompt_version="orchestrator-v1",
        can_call=["Router", "CodingWorker", "Verifier", "Operator"],
        input_type="NormalizedRecord",
        output_type="RecordResult",
    )

    def __init__(self, bus, audit, cfg, router: Router, worker: CodingWorker,
                 verifier: Verifier, operator):
        super().__init__(bus, audit)
        self.cfg = cfg
        self.router = router
        self.worker = worker
        self.verifier = verifier
        self.operator = operator

    # -- exception helper ------------------------------------------------------
    def _route_exception(self, rec: NormalizedRecord, res: RecordResult,
                         reason_code: str, reason_class: str, detail: str,
                         span_status: str = "routed", verdict=None):
        res.status = "exception"
        res.reason_code = reason_code
        res.reason_class = reason_class
        res.agent_trace.append({
            "agent": self.spec.name, "model": None, "prompt_version": self.spec.prompt_version,
            "status": span_status, "verdict": verdict, "cost_usd": 0.0, "latency_ms": 1.0,
            "retries": 0, "detail": detail})
        self.log("route_exception", record_id=rec.id, reason_code=reason_code,
                 reason_class=reason_class, detail=detail)
        self.bus.publish("record.exception", res)
        return res

    # -- main per-record entry point ------------------------------------------
    def process(self, rec: NormalizedRecord, data_finding: Optional[RuleFinding]) -> RecordResult:
        res = RecordResult(id=rec.id, version=rec.version, source_format=rec.source_format,
                           source_version_hash=rec.source_version_hash, amount=rec.amount)
        self.log("intake_record", record_id=rec.id, source=rec.source_format,
                 version=rec.version)

        # Class-A data-layer exceptions never reach assembly.
        if data_finding is not None:
            return self._route_exception(rec, res, data_finding.reason_code,
                                         data_finding.reason_class, data_finding.detail)

        # ---- Assembly with budget + bounded escalation ----------------------
        spent = 0.0
        steps = 0
        escalate = False
        attempt = 0
        last_verdict = None
        feedback = None  # Verifier disagreements carried into the next Worker attempt
        while attempt < MAX_RETRIES + 1:
            attempt += 1
            decision = self.router.decide(rec, escalate=escalate)
            steps += 1

            # Budget gate BEFORE spending on the model call.
            if steps > self.cfg.max_steps_per_record:
                return self._route_exception(rec, res, "AGENT_LOOP", "A",
                    f"step budget {self.cfg.max_steps_per_record} exceeded", "killed")
            if spent + decision.est_cost_usd > self.cfg.max_cost_usd_per_record:
                # Try to downgrade first; if already cheap, route as BUDGET_EXCEEDED.
                if decision.tier == "strong":
                    decision = self.router.decide(rec, escalate=False)
                if spent + decision.est_cost_usd > self.cfg.max_cost_usd_per_record:
                    return self._route_exception(rec, res, "BUDGET_EXCEEDED", "A",
                        f"projected spend {spent + decision.est_cost_usd:.6f} > ceiling "
                        f"{self.cfg.max_cost_usd_per_record}", "routed")

            # Worker draft, given any feedback from the Verifier's prior rejection.
            if feedback:
                self.log("feedback_to_worker", record_id=rec.id, attempt=attempt,
                         feedback=feedback)
            try:
                draft, llm = self.worker.draft(rec, attempt, decision.model, feedback)
            except MalformedDraft as e:
                steps += 1
                res.agent_trace.append(_span(self.worker.spec.name, decision.model,
                    self.worker.spec.prompt_version, status="rejected", retries=attempt - 1,
                    verdict=None, detail=f"malformed: {e}"))
                if attempt <= MAX_RETRIES:
                    escalate = True
                    feedback = [{"field": "structure",
                                 "issue": f"unparseable output: {e}",
                                 "fix": "return a single valid JSON object with all required keys"}]
                    continue
                return self._route_exception(rec, res, "AGENT_MALFORMED", "A",
                    f"worker malformed after {attempt} attempts", "abstained")

            spent += llm.cost_usd
            steps += 1
            verdict = self.verifier.verify(rec, draft)
            last_verdict = verdict

            worker_status = "ok" if verdict.verdict == "pass" else (
                "overruled" if verdict.reason_code == "AGENT_HALLUCINATION" else "rejected")
            res.agent_trace.append(_span(self.worker.spec.name, llm.model,
                llm.prompt_version, status=worker_status, retries=attempt - 1,
                tokens_in=llm.tokens_in, tokens_out=llm.tokens_out, cost_usd=llm.cost_usd,
                latency_ms=llm.latency_ms, transcript_hash=llm.transcript_hash,
                detail=(f"confidence={draft.confidence}"
                        + ("; applied verifier feedback" if feedback else ""))))
            res.agent_trace.append(_span(self.verifier.spec.name, None,
                self.verifier.spec.prompt_version, status=(
                    "ok" if verdict.verdict == "pass" else (
                        "abstained" if verdict.verdict == "needs_human" else "rejected")),
                verdict=verdict.verdict, cost_usd=0.0, latency_ms=2.0,
                detail=verdict.notes, disagreements=verdict.disagreements))

            if verdict.verdict == "pass":
                return self._deliver(rec, res, draft, llm, spent)

            if verdict.verdict == "needs_human":  # LOW_CONFIDENCE abstain
                return self._route_exception(rec, res, "LOW_CONFIDENCE", "A",
                    verdict.notes, "abstained", verdict="needs_human")

            # fail -> hallucination / malformed: hand the Verifier's disagreements
            # back to the Worker as actionable feedback, escalate the model, retry.
            if attempt <= MAX_RETRIES:
                escalate = True
                feedback = [{"field": d["field"], "you_said": d.get("worker"),
                             "source_truth": d.get("source"),
                             "fix": "replace with the source-grounded value"}
                            for d in verdict.disagreements]
                continue
            return self._route_exception(rec, res, verdict.reason_code, "A",
                f"verifier rejected worker after {attempt} attempts: {verdict.notes}",
                "routed", verdict="fail")

        # Should be unreachable; safety net.
        return self._route_exception(rec, res, "AGENT_LOOP", "A",
            "exhausted attempts without resolution", "killed")

    # -- delivery path (review + approval + gate) -----------------------------
    def _deliver(self, rec, res: RecordResult, draft: WorkerDraft, llm, spent) -> RecordResult:
        appr = ApprovalState(rec.id)
        # Human-in-the-loop review chain, run through the Operator surface.
        self.operator.run_chain(rec, appr, self.audit)

        allowed, refusal = appr.can_deliver(
            rec.amount, self.cfg.amendment.role, self.cfg.amendment.threshold)
        if not allowed:
            ts = self.audit._ts(len(self.audit.events()))
            appr.transition("blocked", "delivery_gate", ts, refusal)
            res.approval_trail = appr.trail
            self.log("delivery_refused", record_id=rec.id, reason=refusal)
            return self._route_exception(rec, res, "LOW_CONFIDENCE", "A",
                f"delivery gate refused: {refusal}", "routed")

        ts = self.audit._ts(len(self.audit.events()))
        appr.transition("delivered", "delivery_gate", ts, "approval + amendment satisfied")
        res.approval_trail = appr.trail

        df = build_delivered_fields(rec, draft)
        res.status = "delivered"
        res.reason_code = None
        res.reason_class = None
        res.delivered_fields = df
        res.delivered_fields_hash = sha(df)
        res.transcript_hash = llm.transcript_hash
        res.claim_summary = draft.claim_summary
        self.log("record_delivered", record_id=rec.id,
                 delivered_fields_hash=res.delivered_fields_hash,
                 transcript_hash=res.transcript_hash, cost_usd=spent)
        self.bus.publish("record.delivered", res)
        return res


def _span(agent, model, prompt_version, status, retries=0, verdict=None,
          tokens_in=None, tokens_out=None, cost_usd=0.0, latency_ms=1.0,
          transcript_hash=None, detail=None, disagreements=None):
    span = {"agent": agent, "model": model, "prompt_version": prompt_version,
            "status": status, "verdict": verdict, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "cost_usd": cost_usd, "latency_ms": latency_ms,
            "retries": retries, "transcript_hash": transcript_hash}
    if detail is not None:
        span["detail"] = detail
    if disagreements:
        span["disagreements"] = disagreements
    return span
