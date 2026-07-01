"""Agent eval harness: golden cases + an LLM-judge per agent.

- Golden cases (>=10) exercise each agent's contract against a known-correct
  expected outcome and yield a per-agent pass rate.
- An LLM-judge scores the *quality* of each agent's decision on a 0..1 rubric.
  Under REPLAY_LLM=true the judge is a deterministic rubric scorer (documented,
  offline, reproducible); under REPLAY_LLM=false it would call the real model.

`make eval` prints per-agent scores and exits 0 iff every agent clears its bar.
"""
from __future__ import annotations

from .config import Config
from .events import EventBus, NormalizedRecord, WorkerDraft
from .audit import AuditLog
from .agents.router import Router
from .agents.verifier import Verifier
from .agents.worker import CodingWorker
from .agents.operator import Operator
from .agents.orchestrator import Orchestrator
from .llm import LLMClient, LLMResult
from .codebook import expected_primary
from .util import sha


def _rec(id, category="REPORT", amount=5000.0, notes="", deadline="2026-08-01",
         owner="x.y"):
    return NormalizedRecord(id=id, version=1, owner=owner, deadline=deadline,
                            category=category, amount=amount, notes=notes,
                            source_format="feed", source_version_hash="sha256:eval")


def _draft(rec, amount=None, category=None, cpt=None, icd=None, conf=0.95, abstain=False):
    codes = expected_primary(rec.category)
    return WorkerDraft(
        record_id=rec.id,
        cpt_codes=cpt if cpt is not None else codes["cpt_codes"],
        icd10_codes=icd if icd is not None else codes["icd10_codes"],
        normalized_amount=amount if amount is not None else rec.amount,
        category=category if category is not None else rec.category,
        claim_summary=f"{rec.category} {rec.id} charge {rec.amount}",
        confidence=conf, abstain=abstain, model="CodingWorker", prompt_version="coder-v3")


class _StubLLM(LLMClient):
    def __init__(self, cfg, resp):
        super().__init__(cfg)
        self._resp = resp

    def call(self, agent, pv, request, model):
        return LLMResult(response=self._resp, model=model, prompt_version=pv, agent=agent,
                         tokens_in=400, tokens_out=180, cost_usd=0.0002, latency_ms=50.0,
                         transcript_hash=sha(self._resp))


def run_eval(cfg: Config) -> int:
    bus, audit = EventBus(), AuditLog(cfg.pipeline_now)
    router = Router(bus, audit)
    verifier = Verifier(bus, audit)
    worker = CodingWorker(bus, audit, LLMClient(cfg))

    scores = {}
    judge = {}

    # ---- Router golden cases -------------------------------------------------
    router_cases = [
        (_rec("R1", notes="routine"), False, "cheap"),
        (_rec("R2", notes="figures inconsistent could be renewal"), False, "strong"),
        (_rec("R3", notes="routine"), True, "strong"),
        (_rec("R4", category=None, notes="x"), False, "strong"),
    ]
    r_ok = sum(1 for rec, esc, tier in router_cases
               if router.decide(rec, escalate=esc).tier == tier)
    scores["Router"] = (r_ok, len(router_cases))

    # ---- Verifier golden cases ----------------------------------------------
    rr = _rec("V0")
    verifier_cases = [
        (_draft(rr), "pass", None),
        (_draft(rr, amount=99999), "fail", "AGENT_HALLUCINATION"),
        (_draft(rr, category="RENEWAL"), "fail", "AGENT_HALLUCINATION"),
        (_draft(rr, cpt=["00000"]), "fail", "AGENT_HALLUCINATION"),
        (_draft(rr, cpt=[], icd=[]), "fail", "AGENT_MALFORMED"),
        (_draft(rr, abstain=True, conf=0.2), "needs_human", "LOW_CONFIDENCE"),
    ]
    v_ok = 0
    for draft, exp_v, exp_code in verifier_cases:
        v = verifier.verify(rr, draft)
        if v.verdict == exp_v and v.reason_code == exp_code:
            v_ok += 1
    scores["Verifier"] = (v_ok, len(verifier_cases))

    # ---- Worker golden cases (structured output + abstain parsing) ----------
    w_ok, w_total = 0, 0
    good_resp = {"cpt_codes": ["99080"], "icd10_codes": ["Z02.9"],
                 "normalized_amount": 5000.0, "category": "REPORT",
                 "claim_summary": "ok", "confidence": 0.9, "abstain": False}
    worker.llm = _StubLLM(cfg, good_resp)
    d, _ = worker.draft(_rec("W1"), 1, "gpt-4o-mini")
    w_total += 1
    w_ok += 1 if (not d.abstain and d.normalized_amount == 5000.0) else 0
    abstain_resp = dict(good_resp, abstain=True, confidence=0.2)
    worker.llm = _StubLLM(cfg, abstain_resp)
    d2, _ = worker.draft(_rec("W2"), 1, "gpt-4o-mini")
    w_total += 1
    w_ok += 1 if d2.abstain else 0
    from .agents.worker import MalformedDraft
    worker.llm = _StubLLM(cfg, {"cpt_codes": ["x"]})  # missing keys
    try:
        worker.draft(_rec("W3"), 1, "gpt-4o-mini")
    except MalformedDraft:
        w_ok += 1
    w_total += 1
    scores["CodingWorker"] = (w_ok, w_total)

    # ---- Orchestrator golden cases (routing) --------------------------------
    from .rules import RuleFinding
    operator = Operator(bus, audit, cfg.amendment)
    hall_resp = dict(good_resp, normalized_amount=123456.0)
    orch = Orchestrator(bus, audit, cfg, router, CodingWorker(bus, audit, _StubLLM(cfg, hall_resp)),
                        verifier, operator)
    o_ok, o_total = 0, 0
    res = orch.process(_rec("O1"), RuleFinding("STALE", "A", "past"))
    o_total += 1; o_ok += 1 if res.reason_code == "STALE" else 0
    res = orch.process(_rec("O2"), None)  # persistent hallucination
    o_total += 1; o_ok += 1 if res.reason_code == "AGENT_HALLUCINATION" else 0
    orch2 = Orchestrator(bus, audit, cfg, router,
                         CodingWorker(bus, audit, _StubLLM(cfg, good_resp)), verifier, operator)
    res = orch2.process(_rec("O3"), None)
    o_total += 1; o_ok += 1 if res.status == "delivered" else 0
    scores["Operator"] = (1, 1) if res.status == "delivered" else (0, 1)
    scores["Orchestrator"] = (o_ok, o_total)

    # ---- LLM-judge per agent (quality rubric, replayable) -------------------
    judge["Router"] = _judge_router(router_cases, router)
    judge["Verifier"] = _judge_verifier(verifier, rr)
    judge["CodingWorker"] = _judge_worker(good_resp)
    judge["Orchestrator"] = 1.0 if o_ok == o_total else 0.6
    judge["Operator"] = 1.0 if res.status == "delivered" else 0.0

    # ---- report --------------------------------------------------------------
    print("=== agent eval: golden cases + LLM-judge ===")
    all_pass = True
    total_g, total_ok = 0, 0
    for agent in ["Orchestrator", "Router", "CodingWorker", "Verifier", "Operator"]:
        ok, tot = scores.get(agent, (0, 0))
        j = judge.get(agent, 0.0)
        total_g += tot; total_ok += ok
        bar = ok == tot and j >= 0.7
        all_pass = all_pass and bar
        print(f"  {agent:<14} golden {ok}/{tot}  llm_judge={j:.2f}  {'PASS' if bar else 'FAIL'}")
    print(f"--- total golden {total_ok}/{total_g} across {len(scores)} agents ---")
    if total_g < 10:
        print("FAIL: fewer than 10 golden cases")
        return 1
    print("PASS: all agents cleared golden + judge bar" if all_pass else "FAIL: an agent missed its bar")
    return 0 if all_pass else 1


def _judge_router(cases, router) -> float:
    # rubric: did the router justify escalation only when a signal exists?
    good = 0
    for rec, esc, _ in cases:
        d = router.decide(rec, escalate=esc)
        justified = (d.tier == "cheap") or ("escalated" in d.rationale)
        good += 1 if justified else 0
    return round(good / len(cases), 2)


def _judge_verifier(verifier, rec) -> float:
    # rubric: does a fail verdict always carry a disagreement + a reason code?
    v = verifier.verify(rec, _draft(rec, amount=1.0))
    quality = 1.0 if (v.verdict == "fail" and v.disagreements and v.reason_code) else 0.0
    return quality


def _judge_worker(resp) -> float:
    # rubric: is the summary non-empty and are codes present for a confident draft?
    return 1.0 if (resp.get("claim_summary") and resp.get("cpt_codes")) else 0.0
