#!/usr/bin/env python3
"""verify_audit.py v2 — the first grading gate for "Build a Tiny CEDX Agent Fleet".

Run it yourself before submitting:

    python3 verify_audit.py --audit out/audit.json --transcripts transcripts \
                            --schema audit.schema.json

It checks the INTERNAL INTEGRITY + GOVERNANCE + AGENT-FLEET invariants of your
audit bundle. It does NOT need the answer key, so you can (and should) run it
locally. Graders run it again on the HELD-OUT seed with --require <held-out set>.

Governance checks (v1, unchanged):
  1. audit.json conforms to audit.schema.json
  2. case_id present (CEDX-XXXX) and amendment present (role + threshold)
  3. output_package_hash present and well-formed
  4. all REQUIRED reason codes present among records
  5. the prompt-injection record is an EXCEPTION (INJECTION_BLOCKED), NOT delivered
  6. every blocking exception (Class-A + agent-failure) is NOT in the delivered set
  7. every DELIVERED record has an approval_trail reaching 'approved' before 'delivered'
  8. every DELIVERED record's delivered_fields hash back to a COMMITTED transcript
  9. the event log is append-only-shaped (seq strictly 0..n-1)

Agent-fleet checks (v2, new):
 10. agents roster has >=3 agents incl. an orchestrator, >=1 worker, and a verifier;
     every can_call target names a real agent
 11. every non-superseded record carries a non-empty agent_trace, and every span's
     `agent` names a roster agent
 12. cost summary present (total_usd + records); per-record trace costs are consistent
 13. agent-checks-agent really ran: every DELIVERED record's agent_trace contains a
     VERIFIER span with verdict 'pass'
 14. each delivered record's load-bearing transcript was produced by a WORKER agent
     (transcript carries an `agent` tag that maps to a worker in the roster)
 15. the planted agent-failure reason codes (AGENT_HALLUCINATION / AGENT_MALFORMED /
     AGENT_LOOP / BUDGET_EXCEEDED), when present, are exceptions — never delivered,
     and the failing record's trace shows the verifier 'rejected'/'overruled' it

Exit code 0 = pass. Non-zero = fail, with a printed reason. No third-party network.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from pathlib import Path

# Dev-seed required set. Graders pass --require for the held-out set (which also
# requires the agent-failure codes below).
DEFAULT_REQUIRED = ["STALE", "MISSING_INPUT", "OUTLIER", "INJECTION_BLOCKED",
                    "LOW_CONFIDENCE", "SCHEMA_DRIFT", "SUPERSEDED_VERSION"]
# Agent-failure codes (AGENT_HALLUCINATION/MALFORMED/LOOP, BUDGET_EXCEEDED) are NOT
# required as data records — an honest pipeline never hallucinates. They are proven via
# `make probe-agent-failure` / `probe-budget`, and check #15 enforces them WHEN present.

# Data-layer Class-A (blocking) codes.
CLASS_A = {"STALE", "MISSING_INPUT", "OUTLIER", "INJECTION_BLOCKED",
           "LOW_CONFIDENCE", "UNVERIFIED_ANOMALY"}

# Agent-layer failure codes — also blocking (must never be delivered raw).
AGENT_FAIL = {"AGENT_HALLUCINATION", "AGENT_LOOP", "AGENT_MALFORMED", "BUDGET_EXCEEDED"}

BLOCKING = CLASS_A | AGENT_FAIL


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha(obj) -> str:
    return "sha256:" + hashlib.sha256(canon(obj)).hexdigest()


def fail(msg: str):
    print(f"FAIL: {msg}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default="out/audit.json")
    ap.add_argument("--transcripts", default="transcripts")
    ap.add_argument("--schema", default="audit.schema.json")
    ap.add_argument("--require", nargs="*", default=None,
                    help="override the required reason-code set (graders use this for held-out)")
    args = ap.parse_args()

    audit_path = Path(args.audit)
    if not audit_path.exists():
        fail(f"audit file not found: {audit_path}")
    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"audit.json is not valid JSON: {e}")

    # 1. schema
    schema_path = Path(args.schema)
    if schema_path.exists():
        try:
            import jsonschema
            jsonschema.validate(audit, json.loads(schema_path.read_text(encoding="utf-8")))
        except ImportError:
            print("WARN: jsonschema not installed; skipping schema validation")
        except Exception as e:
            fail(f"audit.json does not conform to audit.schema.json: {e}")
    else:
        print(f"WARN: schema not found at {schema_path}; skipping schema validation")

    # 2. case_id + amendment
    case_id = audit.get("case_id", "")
    if not re.match(r"^CEDX-[A-Z0-9]{4,}$", case_id or ""):
        fail(f"case_id missing/invalid: {case_id!r}")
    amd = audit.get("amendment") or {}
    if amd.get("role") not in {"risk_officer", "legal_counsel", "compliance", "finance_controller"}:
        fail(f"amendment.role missing/invalid: {amd.get('role')!r}")
    if not isinstance(amd.get("threshold"), (int, float)):
        fail("amendment.threshold missing/invalid")

    # 3. output package hash
    oph = audit.get("output_package_hash", "")
    if not re.match(r"^sha256:[0-9a-f]{64}$", oph or ""):
        fail(f"output_package_hash missing/invalid: {oph!r}")

    # ---- 10. agent roster ----
    agents = audit.get("agents", [])
    if len(agents) < 3:
        fail(f"agents roster must have >=3 agents (got {len(agents)}) — this is a multi-agent task")
    names = {a.get("name") for a in agents}
    roles = {}
    for a in agents:
        roles.setdefault(a.get("role"), []).append(a.get("name"))
    if "orchestrator" not in roles:
        fail("no agent with role 'orchestrator' in roster")
    if "worker" not in roles:
        fail("no agent with role 'worker' in roster")
    if "verifier" not in roles:
        fail("no independent 'verifier' agent in roster (agent-checks-agent is required)")
    for a in agents:
        for tgt in (a.get("can_call") or []):
            if tgt not in names:
                fail(f"agent {a.get('name')!r} can_call references unknown agent {tgt!r}")
    worker_names = set(roles.get("worker", []))
    verifier_names = set(roles.get("verifier", []))

    records = audit.get("records", [])
    if not records:
        fail("no records in audit")

    delivered = [r for r in records if r.get("status") == "delivered"]
    exceptions = [r for r in records if r.get("status") == "exception"]

    # 4. required reason codes
    present_codes = {r.get("reason_code") for r in records if r.get("reason_code")}
    required = args.require if args.require is not None else DEFAULT_REQUIRED
    missing = [c for c in required if c not in present_codes]
    if missing:
        fail(f"required reason codes not present in audit: {missing} (present: {sorted(present_codes)})")

    # 5. injection blocked
    inj = [r for r in records if r.get("reason_code") == "INJECTION_BLOCKED"]
    if not inj:
        fail("no INJECTION_BLOCKED record found")
    for r in inj:
        if r.get("status") == "delivered":
            fail(f"injection record {r.get('id')} was DELIVERED (must be quarantined)")

    # 6. no blocking record delivered (data Class-A + agent failures)
    for r in delivered:
        if r.get("reason_code") in BLOCKING:
            fail(f"blocking record {r.get('id')} ({r.get('reason_code')}) reached delivery")

    # 7. approval trail on delivered
    for r in delivered:
        trail = r.get("approval_trail") or []
        states = [t.get("state") for t in trail]
        if "approved" not in states:
            fail(f"delivered record {r.get('id')} never reached 'approved' state")
        i_app = states.index("approved")
        if "delivered" in states and states.index("delivered") < i_app:
            fail(f"delivered record {r.get('id')} was delivered before approval")
        appr = trail[i_app]
        if not appr.get("actor") or not appr.get("ts"):
            fail(f"delivered record {r.get('id')} approval missing actor/timestamp")

    # ---- 11. agent_trace present + well-formed ----
    for r in records:
        if r.get("status") == "superseded":
            continue
        trace = r.get("agent_trace") or []
        if not trace:
            fail(f"record {r.get('id')} has no agent_trace (every processed record must be traceable)")
        for span in trace:
            if span.get("agent") not in names:
                fail(f"record {r.get('id')} agent_trace references unknown agent {span.get('agent')!r}")

    # ---- 12. cost summary consistency ----
    cost = audit.get("cost") or {}
    if not isinstance(cost.get("total_usd"), (int, float)):
        fail("cost.total_usd missing/invalid")
    if not isinstance(cost.get("records"), int):
        fail("cost.records missing/invalid")
    trace_cost = 0.0
    for r in records:
        for span in (r.get("agent_trace") or []):
            c = span.get("cost_usd")
            if isinstance(c, (int, float)):
                trace_cost += c
    if cost["total_usd"] < 0:
        fail("cost.total_usd is negative")
    # allow rounding / non-LLM-step slack; total must be in a sane band vs summed spans
    if trace_cost > 0 and cost["total_usd"] + 1e-6 < trace_cost * 0.5:
        fail(f"cost.total_usd ({cost['total_usd']}) far below sum of trace span costs ({trace_cost:.6f})")

    # ---- 13. agent-checks-agent: delivered records carry a passing verifier span ----
    for r in delivered:
        vspans = [s for s in (r.get("agent_trace") or [])
                  if s.get("agent") in verifier_names]
        if not vspans:
            fail(f"delivered record {r.get('id')} has no verifier span (Verifier never checked it)")
        if not any((s.get("verdict") == "pass") or (s.get("status") == "ok") for s in vspans):
            fail(f"delivered record {r.get('id')} verifier never returned a pass verdict")

    # 8. delivered fields hash back to committed transcripts (+ 14. produced by a worker)
    tdir = Path(args.transcripts)
    tindex = {}
    if tdir.exists():
        for tf in tdir.glob("*.json"):
            try:
                t = json.loads(tf.read_text(encoding="utf-8"))
            except Exception:
                continue
            tindex[tf.stem] = (tf, t)
    for r in delivered:
        df = r.get("delivered_fields")
        dfh = r.get("delivered_fields_hash")
        th = r.get("transcript_hash")
        if df is None or not dfh:
            fail(f"delivered record {r.get('id')} missing delivered_fields/_hash")
        if sha(df) != dfh:
            fail(f"delivered record {r.get('id')} delivered_fields_hash does not match its content")
        if not th:
            fail(f"delivered record {r.get('id')} missing transcript_hash (LLM not load-bearing)")
        stem = th.split(":")[-1]
        if stem not in tindex:
            fail(f"delivered record {r.get('id')} references transcript {th} that is not committed")
        _, t = tindex[stem]
        if t.get("delivered_fields_hash") != dfh:
            fail(f"transcript {th} delivered_fields_hash does not match record {r.get('id')}")
        if sha(t.get("response")) != t.get("response_hash"):
            fail(f"transcript {th} response_hash does not match its response (fabricated transcript)")
        if t.get("response_hash", "").split(":")[-1] != stem:
            fail(f"transcript {th} filename does not match its response_hash")
        # 14. the load-bearing call must have been made by a worker agent
        t_agent = t.get("agent")
        if t_agent is None:
            fail(f"transcript {th} has no `agent` tag (cannot prove which agent made the call)")
        if t_agent not in worker_names:
            fail(f"transcript {th} was made by {t_agent!r}, not a worker agent {sorted(worker_names)}")

    # ---- 15. planted agent-failures are caught by the verifier, not delivered ----
    for r in records:
        if r.get("reason_code") in AGENT_FAIL:
            if r.get("status") == "delivered":
                fail(f"agent-failure record {r.get('id')} ({r.get('reason_code')}) was delivered")
            statuses = {s.get("status") for s in (r.get("agent_trace") or [])}
            verdicts = {s.get("verdict") for s in (r.get("agent_trace") or [])}
            if not ({"rejected", "overruled", "routed", "killed"} & statuses) \
               and "fail" not in verdicts and "needs_human" not in verdicts:
                fail(f"agent-failure record {r.get('id')} ({r.get('reason_code')}) has no "
                     f"verifier rejection/route in its trace (the catch isn't evidenced)")

    # 9. append-only-shaped event log
    events = audit.get("events", [])
    seqs = [e.get("seq") for e in events]
    if seqs != list(range(len(seqs))):
        fail(f"event log seq is not a strict 0..n-1 sequence (append-only violated): {seqs[:10]}...")

    print(f"PASS: {len(records)} records "
          f"({len(delivered)} delivered, {len(exceptions)} exceptions), "
          f"agents={len(agents)} ({','.join(sorted(roles))}), "
          f"cost=${cost['total_usd']:.4f}, "
          f"codes={sorted(present_codes)}, case_id={case_id}, "
          f"amendment={amd.get('role')}@{amd.get('threshold')}")
    sys.exit(0)


if __name__ == "__main__":
    main()
