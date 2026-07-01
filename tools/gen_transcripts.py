"""Record the Worker's LLM calls into committed transcripts for REPLAY_LLM=true.

This plays the role of "we ran the model once and saved the raw responses". It runs
the real intake + normalization + rules so the request keys are identical to what the
runtime builds, then writes one transcript per Worker call:

  * clean records          -> a grounded draft (Verifier will pass)
  * REC-015 (ambiguous)    -> an abstain draft (Verifier -> LOW_CONFIDENCE)
  * REC-020 (agent-failure)-> attempt 1 HALLUCINATES the amount (Verifier overrules),
                              attempt 2 is grounded (recovers on the strong model)

Authoring these responses is dev-time data authoring, NOT detection logic: the
pipeline decides abstain/hallucination purely from the response at runtime, so the
same code generalizes to the held-out seed under the real-LLM path.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fleet.config import Config
from fleet.events import EventBus, WorkerDraft
from fleet.audit import AuditLog
from fleet.intake import intake
from fleet.normalize import normalize
from fleet.rules import evaluate_data_rules, compute_outlier_bounds
from fleet.agents.router import Router
from fleet.agents.verifier import Verifier
from fleet.agents.worker import build_worker_request, PROMPT_VERSION
from fleet.codebook import expected_primary
from fleet.delivered import build_delivered_fields
from fleet.llm import cost_of
from fleet.util import sha, hexof

def _idset(env, default):
    raw = os.environ.get(env, default)
    return {x.strip() for x in raw.split(",") if x.strip()}


# Which records the recorded LLM abstains on / hallucinates once on. Overridable per
# seed so the same generator records transcripts for ANY domain (see `make demo-alt`).
ABSTAIN_IDS = _idset("GEN_ABSTAIN_IDS", "REC-015")
HALLUCINATE_ONCE_IDS = _idset("GEN_HALLUCINATE_IDS", "REC-020")

TDIR = Path(os.environ.get("TRANSCRIPTS_DIR",
            str(Path(__file__).resolve().parent.parent / "transcripts")))


def draft_from(rec, resp) -> WorkerDraft:
    return WorkerDraft(
        record_id=rec.id, cpt_codes=[str(c) for c in resp["cpt_codes"]],
        icd10_codes=[str(c) for c in resp["icd10_codes"]],
        normalized_amount=resp["normalized_amount"], category=resp["category"],
        claim_summary=resp["claim_summary"], confidence=resp["confidence"],
        abstain=resp["abstain"], model="CodingWorker", prompt_version=PROMPT_VERSION)


def grounded_response(rec) -> dict:
    codes = expected_primary(rec.category)
    return {
        "cpt_codes": codes["cpt_codes"],
        "icd10_codes": codes["icd10_codes"],
        "normalized_amount": rec.amount,
        "category": rec.category,
        "claim_summary": f"{rec.category} encounter {rec.id} for {rec.owner}; "
                         f"charge {rec.amount}. Codes justified by category.",
        "confidence": 0.96,
        "abstain": False,
    }


def abstain_response(rec) -> dict:
    return {
        "cpt_codes": [],
        "icd10_codes": [],
        "normalized_amount": rec.amount,
        "category": rec.category,
        "claim_summary": f"Ambiguous encounter {rec.id}: inputs inconsistent; "
                         "coder cannot assign codes confidently.",
        "confidence": 0.28,
        "abstain": True,
    }


def hallucinated_response(rec) -> dict:
    r = grounded_response(rec)
    # Invent an amount NOT supported by the source -> Verifier must overrule.
    r["normalized_amount"] = (rec.amount or 0) + 9999
    r["claim_summary"] = f"Encounter {rec.id}: fabricated charge for upcoding demo."
    r["confidence"] = 0.91
    return r


def write_transcript(agent, model, request, resp, deliverable: bool):
    doc = {
        "agent": agent,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "request": request,
        "response": resp,
        "response_hash": sha(resp),
        "tokens_in": 480 if model == "gpt-4o" else 410,
        "tokens_out": 240 if model == "gpt-4o" else 190,
    }
    doc["cost_usd"] = cost_of(model, doc["tokens_in"], doc["tokens_out"])
    doc["latency_ms"] = 120.0 if model == "gpt-4o" else 60.0
    if deliverable:
        rec = _CURRENT_REC
        doc["delivered_fields_hash"] = sha(build_delivered_fields(rec, draft_from(rec, resp)))
    TDIR.mkdir(exist_ok=True)
    (TDIR / f"{hexof(sha(resp))}.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


_CURRENT_REC = None


def main():
    cfg = Config.from_env()
    bus, audit = EventBus(), AuditLog(cfg.pipeline_now)
    router = Router(bus, audit)

    raws = intake(cfg.seed_dir)
    norm = normalize(raws)
    bounds = compute_outlier_bounds([r.amount for r in norm.records])

    written = 0
    for rec in sorted(norm.records, key=lambda r: r.id):
        finding = evaluate_data_rules(rec, bounds, cfg.pipeline_now)
        if finding is not None:
            continue  # data-layer exception; Worker never called
        global _CURRENT_REC
        _CURRENT_REC = rec

        if rec.id in ABSTAIN_IDS:
            model = router.decide(rec, escalate=False).model
            req = build_worker_request(rec, 1, model)
            write_transcript("CodingWorker", model, req, abstain_response(rec), False)
            written += 1
            continue

        if rec.id in HALLUCINATE_ONCE_IDS:
            m1 = router.decide(rec, escalate=False).model
            hall = hallucinated_response(rec)
            write_transcript("CodingWorker", m1, build_worker_request(rec, 1, m1),
                             hall, False)
            # Recompute the exact feedback the Orchestrator will hand back: run the
            # real Verifier on the hallucinated draft and mirror its disagreements.
            verdict = Verifier(bus, audit).verify(rec, draft_from(rec, hall))
            feedback = [{"field": d["field"], "you_said": d.get("worker"),
                         "source_truth": d.get("source"),
                         "fix": "replace with the source-grounded value"}
                        for d in verdict.disagreements]
            m2 = router.decide(rec, escalate=True).model
            write_transcript("CodingWorker", m2, build_worker_request(rec, 2, m2, feedback),
                             grounded_response(rec), True)
            written += 2
            continue

        model = router.decide(rec, escalate=False).model
        req = build_worker_request(rec, 1, model)
        write_transcript("CodingWorker", model, req, grounded_response(rec), True)
        written += 1

    print(f"wrote {written} worker transcripts to {TDIR}")


if __name__ == "__main__":
    main()
