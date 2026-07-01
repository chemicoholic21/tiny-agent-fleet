# CEDX "Build a Tiny CEDX Agent Fleet" — Candidate Kit (v2)

This is everything you get at kickoff. Read **TASK.md** first — it is the full brief.

**What changed in v2:** you are not building one LLM call wrapped in plumbing. You are building a **multi-agent fleet** (≥3 agents: Orchestrator + Worker(s) + Verifier) that catches *agents* misbehaving, stays cheap at scale, is fully traceable, and that **you must extend live on a call**. We assume AI wrote your code — we grade the architecture and whether you own it.

## What's in here
```
TASK.md              The full task brief, rubric, and submission requirements (READ FIRST)
seed/                The DEV dataset you build against
  feed.json          ~14 structured work-request records
  inbox/             email (.eml) + PDF records (more records; some planted problems live ONLY here)
  SEED_HASH.txt      canonical hash — DO NOT EDIT seed/ (we diff against this; editing = auto-fail)
audit.schema.json    The schema your out/audit.json must conform to — now requires `agents`,
                     per-record `agent_trace`, and a `cost` summary
verify_audit.py      The first grading gate. Run it yourself before submitting. Do NOT modify it.
Dockerfile           Reference build skeleton (adapt to your stack; keep it single-command)
docker-compose.yml   `docker compose up` must run your whole fleet + self-verify
Makefile             The uniform probe targets graders invoke (wire each to your code) — v2 adds
                     trace, eval, probe-agent-failure, probe-budget
SCOPE.template.md    Rename to SCOPE.md, fill in, push during the live call (authorship anchor)
requirements.txt     Deps for verify_audit.py (+ a Python reference); replace if you use another language
```

## The fleet contract (v2 — non-negotiable)
- **≥3 real agents** with typed input/output contracts and a declared `can_call` list. One god-function with three prompts = auto-fail this criterion.
- An **independent Verifier agent** checks the Worker before delivery and can OVERRULE it; the disagreement is logged.
- A **model router**: cheap model for easy records, escalate only when needed. Enforce a per-record cost + step ceiling (`BUDGET_EXCEEDED`).
- Every record emits an **`agent_trace`** (one span per agent step: agent, model, tokens, cost, latency, retries, status/verdict).

## The run contract (non-negotiable)
- ONE command on a fresh, network-restricted machine: `docker compose up` (or `make demo`).
- Offline path uses `REPLAY_LLM=true` and your committed `transcripts/` — no paid key needed by the grader. Each transcript is tagged with the agent that made the call.
- Reads the seed from `SEED_DIR` (default `seed`). We swap it for a **held-out** seed at grading — same problem TYPES, different values, PLUS injected agent-level failures (hallucinating / looping / malformed worker). Your detectors AND your Verifier must GENERALIZE.
- Writes `out/<branded_package>`, `out/audit.json`, `out/exception_queue.json` in < ~5 min.

## Quick start
1. `cp SCOPE.template.md SCOPE.md` and fill it in; pick your industry at cedxsystems.com/workflows.
2. Stand up the 3 agents (Orchestrator, Worker, Verifier) with typed contracts; build the 5 stages underneath (TASK.md §3). Record every agent's LLM calls into `transcripts/` so `REPLAY_LLM=true` is deterministic.
3. `make demo` then `make verify` — verify must print `PASS`.
4. Run the probes: `make probe-approval`, `make probe-agent-failure`, `make probe-budget`, `make probe-append-only`, `make probe-idempotency`. And `make trace ID=<id>`, `make eval`.
5. For the real path: `REPLAY_LLM=false LLM_API_KEY=... LLM_MODEL=gpt-4o-mini make demo` (free tiers are fine).
6. Write ARCHITECTURE.md (topology) + DECISIONS.md + README (11 sections) + record a 3–5 min Loom, submit the **public GitHub link** in the portal.
7. **Be ready for the live extension call** — you'll add a new agent/rule to your own code in ~20 min, screen-shared. This is the real test.

## Env vars
| Var | Meaning |
|---|---|
| `REPLAY_LLM` | `true` = replay committed transcripts (default); `false` = call a real model |
| `SEED_DIR` | dataset dir (default `seed`; graders swap in the held-out seed) |
| `CASE_ID` | your live-assigned id; drives the amendment (role R + threshold T) |
| `PIPELINE_NOW` | intake "now" for STALE detection (default `2026-06-26`) |
| `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | real-LLM path (gpt-4o-mini / claude-3-5-haiku / gemini-1.5-flash) |
| `MAX_COST_USD_PER_RECORD` / `MAX_STEPS_PER_RECORD` | your budget ceilings (raise `BUDGET_EXCEEDED` past them) |

Build us a tiny CEDX **fleet** that holds the line on data it has never seen — and that you can change live without it falling over.
