"""Operator agent — the human-in-the-loop review surface.

In the automated demo it drives the RCM maker-checker chain (coder -> billing
specialist -> billing manager -> compliance) and, when the CASE_ID amendment
applies, records the extra approver role R. In a live deployment these are real
human actions taken via `make review`; here they are recorded with actor + ts so
the approval_trail is a genuine state machine, not a boolean.
"""
from __future__ import annotations

from .base import Agent, AgentSpec
from ..state_machine import RCM_APPROVAL_CHAIN


class Operator(Agent):
    spec = AgentSpec(
        name="Operator",
        role="operator",
        models=[],
        prompt_version="operator-v1",
        can_call=[],
        input_type="WorkerDraft",
        output_type="ApprovalState",
    )

    def __init__(self, bus, audit, amendment, honor_amendment: bool = True):
        super().__init__(bus, audit)
        self.amendment = amendment
        self.honor_amendment = honor_amendment

    def run_chain(self, rec, appr, audit) -> None:
        def ts():
            return audit._ts(len(audit.events()))

        appr.trail.append({"state": "draft", "actor": self.spec.name,
                           "ts": ts(), "reason": "claim assembled"})
        appr.transition("in_review", "coder", ts(), "coder review opened")

        for role in RCM_APPROVAL_CHAIN:
            appr.record_approval(role)
            self.log("approval_step", record_id=rec.id, role=role, decision="approve")

        # CASE_ID amendment: high-value claims need role R in addition.
        if (self.honor_amendment and rec.amount is not None
                and rec.amount >= self.amendment.threshold):
            appr.record_approval(self.amendment.role)
            appr.transition("in_review", self.amendment.role, ts(),
                            f"amendment approval (>= {self.amendment.threshold})")
            self.log("amendment_approval", record_id=rec.id, role=self.amendment.role)

        appr.transition("approved", "compliance", ts(), "approval chain complete")
        self.bus.publish("record.approved", rec.id)
