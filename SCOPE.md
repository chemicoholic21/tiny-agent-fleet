# SCOPE — CEDX "Build a Tiny CEDX Agent Fleet"

- **Candidate name:** (candidate)
- **CASE_ID (assigned live):** CEDX-DEMO1  *(placeholder; replace with the live-assigned id)*
- **Industry chosen (from cedxsystems.com/workflows):** Healthcare Administration — AI-orchestrated Revenue Cycle Management (RCM)
- **Tier:** Mid-market hospital / physician-group RCM
- **Stack / language:** Python 3.11 (stdlib + jsonschema + pypdf), Docker, Make

## Amendment (computed from CASE_ID)
```
H = sha256(CASE_ID)
role R      = ["risk_officer","legal_counsel","compliance","finance_controller"][ int(H[0],16) % 4 ]
threshold T = 10000 + (int(H[1:3],16) % 50) * 1000
```
- **CEDX-DEMO1 → role R:** finance_controller
- **CEDX-DEMO1 → threshold T:** 14000

(The amendment is recomputed at runtime from `$CASE_ID`, printed at startup as
`AMENDMENT: role=<R> threshold=<T>`, stored under `amendment` in `out/audit.json`,
and enforced by `make probe-approval`.)

## The 5 governed stages (mapped to RCM)
- [x] Sources/Intake — parse `feed.json` + `inbox/` PDF/email (encounter/charge intake)
- [x] Orchestration — declarative normalize (`field_map.json` + `normalization.schema.json`) + exception queue with all reason codes; Class-A never proceeds
- [x] Assembly — CodingWorker (LLM) drafts structured claim codes + summary; abstain path
- [x] Review — Verifier (agent-checks-agent) + Operator approval state machine + CASE_ID amendment
- [x] Delivery — branded 837 claim batch + append-only hash-chained audit + replay

## The ≥3 agents (typed contracts)
1. **Orchestrator** (`fleet/agents/orchestrator.py`) — owns the run, budgets, routing, retries
2. **Router** (`fleet/agents/router.py`) — cheap/strong model selection
3. **CodingWorker** (`fleet/agents/worker.py`) — the load-bearing LLM assembly step
4. **Verifier** (`fleet/agents/verifier.py`) — independent grounding; OVERRULES the Worker
5. **Operator** (`fleet/agents/operator.py`) — human-in-the-loop approval surface

## What I deliberately did NOT build (and why)
- No LLM for deterministic validation (eligibility/stale/outlier/injection are pure code — cheaper, auditable, generalizes).
- No database — file-based append-only audit is enough at this scale; DECISIONS.md notes what breaks first at 10k/day.
