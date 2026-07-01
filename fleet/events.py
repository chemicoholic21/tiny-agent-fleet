"""Typed events — the fleet's nervous system.

Agents never call each other's methods directly. They emit and consume *typed*
events through the EventBus. This keeps the topology observable: every state
transition is an event, and every event is auditable.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional


# ---- Agent-handoff contracts (typed payloads exchanged between agents) --------

@dataclass
class NormalizedRecord:
    """Canonical internal representation produced by Normalization."""
    id: str
    version: int
    owner: Optional[str]
    deadline: Optional[str]
    category: Optional[str]
    amount: Optional[float]
    notes: str
    source_format: str
    source_version_hash: str
    schema_drift: bool = False
    raw: dict = field(default_factory=dict)

    def factual(self) -> dict:
        """The subset a Verifier can ground against the source (no LLM opinion)."""
        return {
            "record_id": self.id,
            "owner": self.owner,
            "deadline": self.deadline,
            "category": self.category,
            "normalized_amount": self.amount,
        }


@dataclass
class WorkerDraft:
    """Typed output of a Worker agent (the LLM-heavy assembly step)."""
    record_id: str
    cpt_codes: list
    icd10_codes: list
    normalized_amount: Optional[float]
    category: Optional[str]
    claim_summary: str
    confidence: float
    abstain: bool
    model: str
    prompt_version: str


@dataclass
class VerifierVerdict:
    """Typed output of the Verifier agent. It can OVERRULE the Worker."""
    record_id: str
    verdict: str          # "pass" | "fail" | "needs_human"
    reason_code: Optional[str]
    disagreements: list   # [{field, worker, source}]
    notes: str


@dataclass
class RouteDecision:
    """Typed output of the Router agent (model selection)."""
    record_id: str
    model: str
    tier: str             # "cheap" | "strong"
    rationale: str
    est_cost_usd: float


# ---- Observability events (appended to the audit log) ------------------------

@dataclass
class Event:
    seq: int
    ts: str
    actor: str
    action: str
    record_id: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class EventBus:
    """Minimal synchronous typed pub/sub. Deterministic ordering by subscription."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Any], None]]] = {}
        self.history: list[tuple[str, Any]] = []

    def subscribe(self, topic: str, handler: Callable[[Any], None]) -> None:
        self._subs.setdefault(topic, []).append(handler)

    def publish(self, topic: str, payload: Any) -> None:
        self.history.append((topic, payload))
        for h in list(self._subs.get(topic, [])):
            h(payload)
