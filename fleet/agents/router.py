"""Router agent — deterministic model selection (cost economics).

Cheap model by default; escalate to the strong model ONLY when a record shows
hardness signals or the Verifier flagged the previous attempt. This is the policy
that keeps the fleet cheap by design, not by luck.
"""
from __future__ import annotations

from .base import Agent, AgentSpec
from ..events import NormalizedRecord, RouteDecision
from ..llm import cost_of

CHEAP_MODEL = "gpt-4o-mini"
STRONG_MODEL = "gpt-4o"

# hardness signals that justify spending more on a record
LONG_NOTES = 120
AMBIGUITY_HINTS = ("unclear", "inconsistent", "could be", "not attached",
                   "ambiguous", "tbd", "side letter")


class Router(Agent):
    spec = AgentSpec(
        name="Router",
        role="router",
        models=[CHEAP_MODEL, STRONG_MODEL],
        prompt_version="router-v1",
        can_call=[],
        input_type="NormalizedRecord",
        output_type="RouteDecision",
    )

    def decide(self, rec: NormalizedRecord, escalate: bool = False) -> RouteDecision:
        signals = []
        if escalate:
            signals.append("verifier_escalation")
        notes = (rec.notes or "").lower()
        if len(notes) > LONG_NOTES:
            signals.append("long_notes")
        if any(h in notes for h in AMBIGUITY_HINTS):
            signals.append("ambiguity_hint")
        if rec.category is None:
            signals.append("missing_category")

        strong = bool(signals)
        model = STRONG_MODEL if strong else CHEAP_MODEL
        tier = "strong" if strong else "cheap"
        est = cost_of(model, 500 if strong else 400, 260 if strong else 180)
        rationale = ("escalated: " + ",".join(signals)) if strong else "clean record -> cheap model"
        decision = RouteDecision(record_id=rec.id, model=model, tier=tier,
                                 rationale=rationale, est_cost_usd=est)
        self.log("route_decision", record_id=rec.id, model=model, tier=tier,
                 signals=signals, est_cost_usd=est)
        self.bus.publish("route.decided", decision)
        return decision
