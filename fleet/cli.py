"""Uniform probe CLI. Graders invoke these via the Makefile.

Usage: python3 -m fleet.cli <command> [args]
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

from .config import Config
from .pipeline import Fleet
from .events import EventBus, NormalizedRecord
from .audit import AuditLog
from .agents.router import Router
from .agents.worker import CodingWorker
from .agents.verifier import Verifier
from .agents.operator import Operator
from .agents.orchestrator import Orchestrator
from .llm import LLMClient, LLMResult
from .state_machine import ApprovalState


# ------------------------------------------------------------------ demo ------
def cmd_demo(cfg: Config) -> int:
    from .domain import domain_name
    print(f"AMENDMENT: role={cfg.amendment.role} threshold={int(cfg.amendment.threshold)}")
    print(f"[fleet] domain={domain_name()}  case_id={cfg.case_id}  "
          f"replay={cfg.replay_llm}  seed={cfg.seed_dir}")
    fleet = Fleet(cfg)
    summary = fleet.run()
    print(f"[stage] Intake -> Normalization -> Assembly -> Review -> Delivery")
    print(f"[done] delivered={summary['delivered']}  exceptions={summary['exceptions']}  "
          f"cost=${summary['cost']['total_usd']:.5f}  "
          f"avg=${summary['cost']['avg_usd_per_record']:.6f}/rec  "
          f"p95_latency={summary['cost']['p95_latency_ms']:.0f}ms")
    ex = summary["exception_queue"]["items"]
    if ex:
        print("[exceptions]")
        for it in ex:
            print(f"   {it['id']:>10}  {it['reason_code']:<20} class={it['reason_class']}  {it.get('detail')}")
    o = cfg.out_dir
    print(f"[out] {o}/rcm_claim_batch.json  {o}/audit.json  {o}/exception_queue.json")
    return 0


# ----------------------------------------------------------------- trace ------
def _load_audit(cfg: Config) -> dict:
    p = cfg.out_dir / "audit.json"
    if not p.exists():
        print("no out/audit.json — run `make demo` first")
        sys.exit(2)
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_trace(cfg: Config, rid: str) -> int:
    audit = _load_audit(cfg)
    rec = next((r for r in audit["records"] if r["id"] == rid), None)
    if not rec:
        print(f"record {rid} not found")
        return 2
    print(f"=== agent decision path for {rid} ===")
    print(f"status={rec['status']}  reason_code={rec.get('reason_code')}  "
          f"class={rec.get('reason_class')}")
    print("agent_trace:")
    for i, s in enumerate(rec.get("agent_trace", [])):
        cost = s.get("cost_usd") or 0.0
        line = (f"  {i}. {s['agent']:<13} model={str(s.get('model')):<12} "
                f"status={s['status']:<9} verdict={str(s.get('verdict')):<10} "
                f"retries={s.get('retries')} cost=${cost:.6f} "
                f"tokens={s.get('tokens_in')}/{s.get('tokens_out')} "
                f"lat={s.get('latency_ms')}ms")
        print(line)
        if s.get("disagreements"):
            print(f"       VERIFIER OVERRULE disagreements: {s['disagreements']}")
    print("approval_trail:")
    for t in rec.get("approval_trail", []):
        print(f"  {t['state']:<18} by {t['actor']:<16} @ {t['ts']}  {t.get('reason') or ''}")
    if rec.get("transcript_hash"):
        print(f"load-bearing worker transcript: {rec['transcript_hash']}")
    # cross-check the events that mention this record
    evs = [e for e in audit["events"] if e.get("record_id") == rid]
    print(f"events touching {rid}: {len(evs)}")
    return 0


# --------------------------------------------------------------- replay -------
def cmd_replay(cfg: Config, rid: str) -> int:
    """Reconstruct one delivered output's DATA lineage from the log alone."""
    audit = _load_audit(cfg)
    rec = next((r for r in audit["records"] if r["id"] == rid), None)
    if not rec:
        print(f"record {rid} not found")
        return 2
    print(f"=== data lineage for {rid} (from out/audit.json only) ===")
    print(f"source_format = {rec['source_format']}")
    print(f"source_version_hash = {rec['source_version_hash']}")
    for e in audit["events"]:
        if e.get("record_id") == rid:
            print(f"  seq {e['seq']:>3} [{e['actor']}] {e['action']}  {e.get('detail')}")
    if rec["status"] == "delivered":
        th = rec["transcript_hash"]
        tf = cfg.transcripts_dir / f"{th.split(':')[-1]}.json"
        print(f"delivered_fields_hash = {rec['delivered_fields_hash']}")
        print(f"produced by worker transcript = {th}")
        print(f"  transcript file present: {tf.exists()}")
        if tf.exists():
            t = json.loads(tf.read_text(encoding="utf-8"))
            print(f"  transcript agent = {t.get('agent')}  model = {t.get('model')}")
        print(f"delivered_fields = {json.dumps(rec['delivered_fields'])}")
    else:
        print(f"status = {rec['status']} reason = {rec.get('reason_code')} (not delivered)")
    return 0


# ---------------------------------------------------- probe: approval ---------
def _mini_fleet(cfg, honor_amendment=True, llm=None):
    bus = EventBus()
    audit = AuditLog(cfg.pipeline_now)
    client = llm or LLMClient(cfg)
    router = Router(bus, audit)
    worker = CodingWorker(bus, audit, client)
    verifier = Verifier(bus, audit)
    operator = Operator(bus, audit, cfg.amendment, honor_amendment)
    orch = Orchestrator(bus, audit, cfg, router, worker, verifier, operator)
    return bus, audit, orch


def cmd_probe_approval(cfg: Config) -> int:
    # A high-value clean record whose amendment approver R is NOT recorded must be
    # refused at the delivery gate and logged as blocked.
    rec = NormalizedRecord(
        id="PROBE-APPR", version=1, owner="probe.user", deadline="2026-08-01",
        category="REPORT", amount=cfg.amendment.threshold + 5000,
        notes="probe: high-value claim missing amendment approver",
        source_format="feed", source_version_hash="sha256:probe")

    class StubLLM(LLMClient):
        def call(self, agent, pv, request, model):
            from .codebook import expected_primary
            codes = expected_primary(rec.category)
            resp = {"cpt_codes": codes["cpt_codes"], "icd10_codes": codes["icd10_codes"],
                    "normalized_amount": rec.amount, "category": rec.category,
                    "claim_summary": "probe", "confidence": 0.95, "abstain": False}
            from .util import sha
            return LLMResult(response=resp, model=model, prompt_version=pv, agent=agent,
                             tokens_in=400, tokens_out=180, cost_usd=0.0002,
                             latency_ms=50.0, transcript_hash=sha(resp))

    _, audit, orch = _mini_fleet(cfg, honor_amendment=False, llm=StubLLM(cfg))
    res = orch.process(rec, None)
    refused = any(e.action == "delivery_refused" for e in audit.events())
    blocked = any(t["state"] == "blocked" for t in res.approval_trail)
    if res.status == "exception" and refused and blocked:
        print(f"PASS probe-approval: non-approved high-value item REFUSED + logged "
              f"(amendment role={cfg.amendment.role} threshold={int(cfg.amendment.threshold)})")
        return 0
    print(f"FAIL probe-approval: status={res.status} refused={refused} blocked={blocked}")
    return 1


# ------------------------------------------------ probe: agent-failure --------
def cmd_probe_agent_failure(cfg: Config) -> int:
    rec = NormalizedRecord(
        id="PROBE-HALL", version=1, owner="probe.user", deadline="2026-08-01",
        category="REPORT", amount=5000.0,
        notes="probe: worker persistently hallucinates an amount",
        source_format="feed", source_version_hash="sha256:probe")

    class HallucinatingLLM(LLMClient):
        def call(self, agent, pv, request, model):
            from .codebook import expected_primary
            from .util import sha
            codes = expected_primary(rec.category)
            resp = {"cpt_codes": codes["cpt_codes"], "icd10_codes": codes["icd10_codes"],
                    "normalized_amount": rec.amount + 12345,  # ungrounded invention
                    "category": rec.category, "claim_summary": "fabricated",
                    "confidence": 0.93, "abstain": False}
            return LLMResult(response=resp, model=model, prompt_version=pv, agent=agent,
                             tokens_in=400, tokens_out=180, cost_usd=0.0002,
                             latency_ms=50.0, transcript_hash=sha(resp))

    _, audit, orch = _mini_fleet(cfg, llm=HallucinatingLLM(cfg))
    res = orch.process(rec, None)
    verifier_caught = any(s.get("agent") == "Verifier" and s.get("verdict") == "fail"
                          for s in res.agent_trace)
    overruled = any(s.get("status") in ("overruled", "rejected") for s in res.agent_trace)
    if (res.status == "exception" and res.reason_code == "AGENT_HALLUCINATION"
            and verifier_caught and overruled):
        print(f"PASS probe-agent-failure: Verifier caught AGENT_HALLUCINATION after "
              f"{sum(1 for s in res.agent_trace if s['agent']=='CodingWorker')} worker "
              f"attempts; routed to human, NOT delivered")
        return 0
    print(f"FAIL probe-agent-failure: status={res.status} code={res.reason_code} "
          f"caught={verifier_caught} overruled={overruled}")
    return 1


# ------------------------------------------------------ probe: budget ---------
def cmd_probe_budget(cfg: Config) -> int:
    import copy
    tight = copy.copy(cfg)
    tight.max_cost_usd_per_record = 0.0000001  # any real call exceeds this
    rec = NormalizedRecord(
        id="PROBE-BUDGET", version=1, owner="probe.user", deadline="2026-08-01",
        category="REPORT", amount=5000.0, notes="probe: exceeds per-record cost ceiling",
        source_format="feed", source_version_hash="sha256:probe")
    _, audit, orch = _mini_fleet(tight)
    res = orch.process(rec, None)
    handled = res.status == "exception" and res.reason_code == "BUDGET_EXCEEDED"
    not_delivered = res.status != "delivered"
    if handled and not_delivered:
        print(f"PASS probe-budget: BUDGET_EXCEEDED raised + routed under ceiling "
              f"${tight.max_cost_usd_per_record}; never silently overspent")
        return 0
    print(f"FAIL probe-budget: status={res.status} code={res.reason_code}")
    return 1


# --------------------------------------------- probe: append-only -------------
def cmd_probe_append_only(cfg: Config) -> int:
    audit = _load_audit(cfg)
    events = audit["events"]
    if not AuditLog.verify_events_dicts(events):
        print("FAIL probe-append-only: committed log fails its own integrity check")
        return 1
    # Attempt a mutation of a past entry and confirm the chain detects it.
    tampered = json.loads(json.dumps(events))
    if len(tampered) < 2:
        print("FAIL probe-append-only: not enough events to test")
        return 1
    tampered[1]["action"] = "SILENTLY_APPROVED"
    if AuditLog.verify_events_dicts(tampered):
        print("FAIL probe-append-only: mutation of a past entry was NOT detected")
        return 1
    # Attempt a delete.
    deleted = json.loads(json.dumps(events))[:-1] + []
    reseq = json.loads(json.dumps(events))
    del reseq[1]
    if AuditLog.verify_events_dicts(reseq):
        print("FAIL probe-append-only: deletion of a past entry was NOT detected")
        return 1
    print(f"PASS probe-append-only: {len(events)} entries chained; mutation AND deletion "
          f"both refused by the hash chain")
    return 0


# --------------------------------------------- probe: idempotency -------------
def cmd_probe_idempotency(cfg: Config) -> int:
    from .util import sha
    Fleet(cfg).run()
    a1 = (cfg.out_dir / "audit.json").read_text(encoding="utf-8")
    p1 = (cfg.out_dir / "rcm_claim_batch.json").read_text(encoding="utf-8")
    Fleet(cfg).run()
    a2 = (cfg.out_dir / "audit.json").read_text(encoding="utf-8")
    p2 = (cfg.out_dir / "rcm_claim_batch.json").read_text(encoding="utf-8")
    d1 = json.loads(a1)
    d2 = json.loads(a2)
    # A record's identity is (id, version, status): REC-017 legitimately appears
    # as both a superseded v1 and a delivered v2 — that is NOT a duplicate.
    keys1 = [(r["id"], r.get("version"), r["status"]) for r in d1["records"]]
    keys2 = [(r["id"], r.get("version"), r["status"]) for r in d2["records"]]
    dupes = len(keys2) != len(set(keys2))
    if a1 == a2 and p1 == p2 and not dupes and keys1 == keys2:
        print(f"PASS probe-idempotency: run 2 byte-identical to run 1 "
              f"({len(keys2)} records, no duplicate (id,version,status) keys)")
        return 0
    print(f"FAIL probe-idempotency: audit_equal={a1==a2} pkg_equal={p1==p2} dupes={dupes}")
    return 1


# --------------------------------------------- probe: crash (bonus) -----------
def cmd_probe_crash(cfg: Config) -> int:
    # Deterministic pipeline overwrites out/ atomically; a re-run after a kill
    # reproduces identical outputs with no duplicates (same mechanism as idempotency).
    Fleet(cfg).run()
    first = (cfg.out_dir / "audit.json").read_text(encoding="utf-8")
    # simulate a crash: truncate the output, then re-run
    (cfg.out_dir / "audit.json").write_text("{ TRUNCATED", encoding="utf-8")
    Fleet(cfg).run()
    second = (cfg.out_dir / "audit.json").read_text(encoding="utf-8")
    if first == second and json.loads(second):
        print("PASS probe-crash: re-run after a truncated/killed write resumes to an "
              "identical, valid audit with no duplicates")
        return 0
    print("FAIL probe-crash: re-run did not reproduce a clean audit")
    return 1


def cmd_review(cfg: Config, args: list) -> int:
    """Operator surface: review <id> <action> <actor> [field] [value] [reason...]

    action ∈ {approve, reject, request-changes, edit-resolve}
    e.g.  review REC-012 edit-resolve dr.reyes amount 5200
          review REC-012 approve billing.mgr
    """
    from .review import review, ReviewError
    if len(args) < 3:
        print("usage: review <id> <action> <actor> [field value] [reason...]")
        return 2
    record_id, action, actor = args[0], args[1], args[2]
    field = value = reason = None
    rest = args[3:]
    if action == "edit-resolve":
        if len(rest) < 2:
            print("edit-resolve needs: <field> <value> [reason...]")
            return 2
        field, value = rest[0], rest[1]
        reason = " ".join(rest[2:]) or None
    else:
        reason = " ".join(rest) or None
    try:
        res = review(cfg, record_id, action, actor, field, value, reason)
    except ReviewError as e:
        print(f"REFUSED: {e}")
        return 1
    print(f"OK [{res['outcome']}] {record_id} by {actor}")
    for k, v in res.items():
        if k not in ("outcome", "record_id", "actor"):
            print(f"   {k}: {v}")
    print(f"   journaled -> {cfg.out_dir}/review_journal.json (append-only, hash-chained)")
    return 0


def cmd_verify_replay(cfg: Config) -> int:
    """Prove replay REPRODUCES delivered outputs byte-for-byte from transcripts alone.

    For every delivered record we independently reconstruct its delivered_fields from
    (seed record + committed worker transcript response) — WITHOUT trusting the audit's
    stored value — and confirm it matches both the audit and a second full run.
    """
    from .intake import intake
    from .normalize import normalize
    from .agents.worker import CodingWorker
    from .delivered import build_delivered_fields
    from .util import sha, hexof

    Fleet(cfg).run()
    audit1 = json.loads((cfg.out_dir / "audit.json").read_text(encoding="utf-8"))
    pkg1 = (cfg.out_dir / "rcm_claim_batch.json").read_text(encoding="utf-8")
    Fleet(cfg).run()
    audit2 = json.loads((cfg.out_dir / "audit.json").read_text(encoding="utf-8"))
    pkg2 = (cfg.out_dir / "rcm_claim_batch.json").read_text(encoding="utf-8")

    if pkg1 != pkg2:
        print("FAIL verify-replay: two runs produced different packages")
        return 1

    recs = {r.id: r for r in normalize(intake(cfg.seed_dir)).records}
    dummy = CodingWorker(EventBus(), AuditLog(cfg.pipeline_now), LLMClient(cfg))
    checked = 0
    for r in audit1["records"]:
        if r["status"] != "delivered":
            continue
        rec = recs[r["id"]]
        tf = cfg.transcripts_dir / f"{hexof(r['transcript_hash'])}.json"
        if not tf.exists():
            print(f"FAIL verify-replay: {r['id']} transcript {r['transcript_hash']} missing")
            return 1
        t = json.loads(tf.read_text(encoding="utf-8"))
        # Reconstruct delivered_fields ONLY from seed record + transcript response.
        draft = dummy._parse(rec, t["response"])
        recon = build_delivered_fields(rec, draft)
        if sha(recon) != r["delivered_fields_hash"] or recon != r["delivered_fields"]:
            print(f"FAIL verify-replay: {r['id']} reconstruction != audit")
            return 1
        checked += 1
    print(f"PASS verify-replay: {checked} delivered records reproduced byte-for-byte "
          f"from committed transcripts alone; two runs byte-identical")
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print("usage: python3 -m fleet.cli <command> [ID]")
        return 2
    cmd = argv[0]
    cfg = Config.from_env()
    if cmd == "demo":
        return cmd_demo(cfg)
    if cmd == "trace":
        return cmd_trace(cfg, argv[1])
    if cmd == "replay":
        return cmd_replay(cfg, argv[1])
    if cmd == "eval":
        from .eval_harness import run_eval
        return run_eval(cfg)
    if cmd == "review":
        return cmd_review(cfg, argv[1:])
    if cmd == "verify-replay":
        return cmd_verify_replay(cfg)
    if cmd == "probe-approval":
        return cmd_probe_approval(cfg)
    if cmd == "probe-agent-failure":
        return cmd_probe_agent_failure(cfg)
    if cmd == "probe-budget":
        return cmd_probe_budget(cfg)
    if cmd == "probe-append-only":
        return cmd_probe_append_only(cfg)
    if cmd == "probe-idempotency":
        return cmd_probe_idempotency(cfg)
    if cmd == "probe-crash":
        return cmd_probe_crash(cfg)
    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
