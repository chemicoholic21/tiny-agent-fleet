"""Human-in-the-loop Operator review surface (TASK.md Stage 4).

A real reviewer surface with the four required actions —
approve / reject / request-changes / edit-resolve — each appended to an
append-only, hash-chained review journal (`out/review_journal.json`) with
actor + timestamp + before/after. Governance rules enforced here:

  * **maker != checker**: the actor approving a record may not be an actor who
    already drafted/edited it (separation of duties).
  * **edit-resolve is deterministic**: when a human supplies a corrected value for
    a Class-A exception, the claim is re-assembled from the fixed codebook (no LLM
    — the human replaced the model's judgement) and re-grounded by the Verifier.
    Only then may a Class-A record proceed toward delivery.

This surface is separate from the automated `make demo` path (which needs no manual
entry); it is how a human intervenes on the exception queue afterwards.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Optional

from .config import Config
from .events import EventBus, NormalizedRecord
from .audit import AuditLog
from .intake import intake
from .normalize import normalize
from .rules import evaluate_data_rules, compute_outlier_bounds
from .agents.verifier import Verifier
from .agents.operator import Operator
from .codebook import expected_primary
from .events import WorkerDraft
from .state_machine import ApprovalState
from .delivered import build_delivered_fields
from .util import sha

ACTIONS = {"approve", "reject", "request-changes", "edit-resolve"}


class ReviewJournal:
    """Append-only, hash-chained journal of human review actions."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list = []
        if path.exists():
            self.entries = json.loads(path.read_text(encoding="utf-8")).get("actions", [])

    def _chain(self, e: dict, prev: str) -> str:
        payload = f"{prev}|{e['seq']}|{e['ts']}|{e['actor']}|{e['action']}|{e['record_id']}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def append(self, ts: str, actor: str, action: str, record_id: str,
               before: dict, after: dict, reason: Optional[str]) -> dict:
        seq = len(self.entries)
        prev = self.entries[-1]["chain_hash"] if self.entries else "GENESIS"
        e = {"seq": seq, "ts": ts, "actor": actor, "action": action,
             "record_id": record_id, "before": before, "after": after, "reason": reason}
        e["chain_hash"] = self._chain(e, prev)
        self.entries.append(e)
        return e

    def makers_of(self, record_id: str) -> set:
        """Actors who have already drafted/edited this record (for maker!=checker)."""
        makers = set()
        for e in self.entries:
            if e["record_id"] == record_id and e["action"] in ("edit-resolve", "request-changes"):
                makers.add(e["actor"])
        return makers

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"actions": self.entries}, indent=2,
                                        ensure_ascii=False), encoding="utf-8")


class ReviewError(Exception):
    pass


def _load_record(cfg: Config, record_id: str) -> tuple[NormalizedRecord, object]:
    raws = intake(cfg.seed_dir)
    norm = normalize(raws)
    rec = next((r for r in norm.records if r.id == record_id), None)
    if rec is None:
        raise ReviewError(f"record {record_id} not found in {cfg.seed_dir}")
    bounds = compute_outlier_bounds([r.amount for r in norm.records])
    finding = evaluate_data_rules(rec, bounds, cfg.pipeline_now)
    return rec, finding


def review(cfg: Config, record_id: str, action: str, actor: str,
           field: Optional[str] = None, value: Optional[str] = None,
           reason: Optional[str] = None, ts: str = "1970-01-01T00:00:00Z") -> dict:
    """Apply one operator action. Returns a result dict; raises ReviewError on refusal."""
    if action not in ACTIONS:
        raise ReviewError(f"unknown action {action!r}; choose one of {sorted(ACTIONS)}")

    rec, finding = _load_record(cfg, record_id)
    journal = ReviewJournal(cfg.out_dir / "review_journal.json")
    before = {"status": "exception" if finding else "in_review",
              "reason_code": finding.reason_code if finding else None,
              "amount": rec.amount, "category": rec.category}

    if action == "reject":
        after = {"status": "rejected", "reason_code": before["reason_code"]}
        journal.append(ts, actor, action, record_id, before, after, reason or "operator reject")
        journal.save()
        return {"outcome": "rejected", "record_id": record_id, "actor": actor}

    if action == "request-changes":
        after = {"status": "changes_requested", "reason_code": before["reason_code"]}
        journal.append(ts, actor, action, record_id, before, after, reason or "changes requested")
        journal.save()
        return {"outcome": "changes_requested", "record_id": record_id, "actor": actor}

    if action == "approve":
        # maker != checker: an approver cannot be someone who edited the record.
        makers = journal.makers_of(record_id)
        if actor in makers:
            raise ReviewError(
                f"separation-of-duties violation: {actor} already acted as maker on "
                f"{record_id} ({sorted(makers)}) and may not also approve it")
        after = {"status": "approved", "approver": actor}
        journal.append(ts, actor, action, record_id, before, after, reason or "operator approval")
        journal.save()
        return {"outcome": "approved", "record_id": record_id, "actor": actor,
                "maker_checker_ok": True}

    # edit-resolve: human supplies a corrected value; deterministic re-assembly.
    if field is None or value is None:
        raise ReviewError("edit-resolve requires --field and --value")
    corrected = _apply_edit(rec, field, value)
    bounds = compute_outlier_bounds([corrected.amount])  # single-record re-check
    new_finding = evaluate_data_rules(corrected, None, cfg.pipeline_now)
    if new_finding is not None:
        after = {"status": "still_exception", "reason_code": new_finding.reason_code}
        journal.append(ts, actor, action, record_id, before, after,
                       reason or f"edit {field}={value}")
        journal.save()
        raise ReviewError(
            f"edit-resolve did not clear the exception: still {new_finding.reason_code} "
            f"({new_finding.detail})")

    # Deterministic (no-LLM) human assembly + independent Verifier grounding.
    codes = expected_primary(corrected.category)
    draft = WorkerDraft(
        record_id=corrected.id, cpt_codes=codes["cpt_codes"], icd10_codes=codes["icd10_codes"],
        normalized_amount=corrected.amount, category=corrected.category,
        claim_summary=f"human-resolved {corrected.category} {corrected.id} (charge "
                      f"{corrected.amount}) by {actor}",
        confidence=1.0, abstain=False, model="human", prompt_version="edit-resolve-v1")
    bus, audit = EventBus(), AuditLog(cfg.pipeline_now)
    verdict = Verifier(bus, audit).verify(corrected, draft)
    if verdict.verdict != "pass":
        after = {"status": "verifier_rejected", "verdict": verdict.verdict}
        journal.append(ts, actor, action, record_id, before, after, reason or "edit rejected")
        journal.save()
        raise ReviewError(f"Verifier rejected the human edit: {verdict.notes}")

    df = build_delivered_fields(corrected, draft)
    after = {"status": "resolved_deliverable", "reason_code": None,
             "amount": corrected.amount, "category": corrected.category,
             "delivered_fields_hash": sha(df), "verifier": "pass"}
    journal.append(ts, actor, action, record_id, before, after,
                   reason or f"edit {field}={value}")
    # Persist the resolved claim so a subsequent (different) actor can approve+deliver.
    resolved_dir = cfg.out_dir / "resolved"
    resolved_dir.mkdir(parents=True, exist_ok=True)
    (resolved_dir / f"{record_id}.json").write_text(
        json.dumps({"record_id": record_id, "resolved_by": actor,
                    "delivered_fields": df, "delivered_fields_hash": sha(df)},
                   indent=2, ensure_ascii=False), encoding="utf-8")
    journal.save()
    return {"outcome": "resolved_deliverable", "record_id": record_id, "actor": actor,
            "was": before["reason_code"], "delivered_fields_hash": sha(df),
            "note": "needs a DIFFERENT actor to approve before delivery (maker!=checker)"}


def _apply_edit(rec: NormalizedRecord, field: str, value: str) -> NormalizedRecord:
    import copy
    r = copy.copy(rec)
    if field == "amount":
        r.amount = float(value)
    elif field == "category":
        r.category = value
    elif field == "deadline":
        r.deadline = value
    elif field == "owner":
        r.owner = value
    else:
        raise ReviewError(f"field {field!r} is not editable")
    return r
