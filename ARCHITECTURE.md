# ARCHITECTURE — CEDX RCM Agent Fleet

Industry lane: **Healthcare Administration — AI-orchestrated Revenue Cycle Management (RCM)**.
The "agent" here is not a chatbot; it is an **orchestrator** that coordinates APIs,
enterprise data, an LLM, deterministic business rules, human approvals, retries,
logging and an external delivery step into one governed, event-driven process.

## 1. Topology (who talks to whom)

```
                         ┌──────────────────────────────────────────┐
   feed.json  ─┐         │              ORCHESTRATOR                 │
   inbox/*.eml ─┼─Intake─▶│  owns run · budgets · retries · routing  │
   inbox/*.pdf ─┘         │  approval state machine · delivery gate  │
                          └───┬──────────┬───────────┬──────────┬────┘
             can_call:        │          │           │          │
                              ▼          ▼           ▼          ▼
                          ┌───────┐  ┌────────────┐ ┌────────┐ ┌──────────┐
                          │Router │  │CodingWorker│ │Verifier│ │ Operator │
                          │cheap/ │  │  (LLM,     │ │(ground │ │ (human   │
                          │strong │  │ load-bear.)│ │ & OVER-│ │  approval│
                          │model  │  │            │ │ RULE)  │ │  chain)  │
                          └───────┘  └────────────┘ └────────┘ └──────────┘
```

Agents never call each other's methods ad hoc. They exchange **typed payloads**
(`fleet/events.py`: `NormalizedRecord`, `RouteDecision`, `WorkerDraft`,
`VerifierVerdict`) and publish **typed events** on an `EventBus`. Each agent
declares an `AgentSpec` with `role`, `models`, `prompt_version` and a `can_call`
allow-list (`fleet/agents/base.py`). The roster is emitted verbatim into
`out/audit.json → agents`, and `verify_audit.py` checks every `can_call` target is
a real agent.

| Agent | role | file | input → output | can_call | LLM? |
|---|---|---|---|---|---|
| Orchestrator | orchestrator | `agents/orchestrator.py` | NormalizedRecord → RecordResult | Router, CodingWorker, Verifier, Operator | no |
| Router | router | `agents/router.py` | NormalizedRecord → RouteDecision | — | no |
| CodingWorker | worker | `agents/worker.py` | NormalizedRecord → WorkerDraft | — | **yes (load-bearing)** |
| Verifier | verifier | `agents/verifier.py` | WorkerDraft → VerifierVerdict | — | no (deterministic grounding) |
| Operator | operator | `agents/operator.py` | WorkerDraft → ApprovalState | — | no |

## 2. Control flow — hub-and-spoke with a feedback loop (NOT a linear agent chain)

Two different things are easy to conflate. The **stage pipeline** (§3) is linear on the
happy path — `Intake → Normalize → Assembly → Review → Delivery`. The **agent control
flow** is not: it is a mediator/hub-and-spoke topology with a cycle and branches.

- **Hub-and-spoke, not a chain.** Agents never hand off to each other (no
  `Worker → Verifier → Operator` bucket brigade). Every message goes *through* the
  Orchestrator. This is encoded in the `can_call` allow-lists: only the Orchestrator
  has a non-empty `can_call`; every spoke has `can_call: []`.

- **A feedback loop.** Inside `Orchestrator.process()` the Worker↔Verifier step is a
  loop, not a straight line — the Verifier can OVERRULE the Worker and send the record
  back through the Router to re-draft on a stronger model (bounded by `MAX_RETRIES=2`):

  ```
      ┌──────────────────────────── retry (escalate=True) ───────────────────────────┐
      ▼                                                                               │
  Router.decide ─▶ budget gate ─▶ CodingWorker.draft ─▶ Verifier.verify ──────────────┤
                                                             │  pass        → Operator → deliver
                                                             │  needs_human → route LOW_CONFIDENCE
                                                             └  fail        → escalate + loop ↑
  ```
  (`make trace ID=REC-020` shows exactly this: draft → Verifier `rejected` → escalate →
  strong-model draft → Verifier `pass` → delivered.)

- **Conditional routing, not one path.** The flow forks at several points: a Class-A
  data finding short-circuits *before* assembly (Worker/Verifier never run); the
  Verifier verdict fans out to deliver / retry / abstain / route; the budget gate forks
  to downgrade-or-`BUDGET_EXCEEDED`; the delivery gate forks to `blocked` when approval
  or the CASE_ID amendment is unmet.

What it is **not**: a peer-to-peer agent swarm and not asynchronous — the Orchestrator
drives one record synchronously. For a *governed* RCM process that is deliberate:
determinism, a single budget/approval enforcement point, and reconstructable traces
beat emergent multi-agent chatter (see `DECISIONS.md`). Natural non-linear extensions
(a two-pass consensus Verifier for high-value claims, or a Redactor spoke before
delivery) are localized changes thanks to the typed-contract + event-bus design.

## 3. The 5 governed stages (underneath the fleet)

1. **Intake** (`fleet/intake.py`) — parses BOTH `feed.json` and `inbox/*.{eml,pdf}`
   (pypdf for PDFs). Every raw record persists its `source_format` and a
   `source_version_hash` (content hash) — provenance anchored at first touch.
2. **Orchestration / Normalization** (`fleet/normalize.py`, `fleet/rules.py`) —
   declarative mapping via a **separate field-map** (`field_map.json`) into a
   **versioned output schema** (`normalization.schema.json`). Deterministic rules
   produce every data-layer reason code. The exception queue catches all Class-A
   problems (they never proceed) and logs Class-B (`SCHEMA_DRIFT`,
   `SUPERSEDED_VERSION`) while continuing.
3. **Assembly** (`agents/worker.py`) — the CodingWorker drafts a structured claim
   (CPT/ICD codes + summary + confidence + abstain). Structured output is enforced
   with bounded repair → abstain. Input hash, model and prompt version are recorded.
4. **Review** (`agents/verifier.py` + `agents/operator.py` + `state_machine.py`) —
   the Verifier independently grounds the draft and can OVERRULE the Worker; the
   Operator drives the approval **state machine**
   (`draft→in_review→approved→delivered`, `→blocked` on refusal).
5. **Delivery** (`fleet/pipeline.py`) — a branded **837 claim batch**
   (`out/rcm_claim_batch.json`) + append-only **hash-chained** `out/audit.json`;
   CASE_ID present throughout.

## 4. Where the Verifier overrules the Worker

`Orchestrator.process()` calls `Verifier.verify(rec, draft)` after every Worker
draft. The Verifier is **pure code** — it compares the emitted `normalized_amount`
and `category` to the source, and checks every CPT/ICD code against the fixed
`fleet/codebook.py`. On a mismatch it returns `verdict="fail"` with the
`disagreements` (both sides) and a reason code:

- ungrounded amount/category/code → `AGENT_HALLUCINATION` (worker span `status=overruled`)
- empty code set → `AGENT_MALFORMED`
- abstain / low confidence → `needs_human` → `LOW_CONFIDENCE`

The Orchestrator then **escalates** (Router picks the strong model) and retries up
to `MAX_RETRIES=2`. If still bad, it routes to a human exception. The disagreement
is logged in the record's `agent_trace` (see `make trace ID=REC-020` — a Worker
hallucinates the amount, the Verifier overrules it, and the strong-model retry
recovers).

## 5. Where budget / router decisions live

- **Router** (`agents/router.py`): cheap `gpt-4o-mini` by default; escalates to
  `gpt-4o` only on hardness signals (long/ambiguous notes, missing category, or a
  Verifier escalation). Policy + cost numbers in DECISIONS.md.
- **Budget** (`agents/orchestrator.py`): per-record step and cost ceilings
  (`MAX_STEPS_PER_RECORD`, `MAX_COST_USD_PER_RECORD`). Before each model call the
  Orchestrator projects spend; if it would exceed the ceiling it first tries to
  downgrade, else raises `BUDGET_EXCEEDED`. A runaway record trips `AGENT_LOOP` on
  the step cap. Nothing is ever silently overspent.

## 6. Observability

Every record carries an ordered `agent_trace` (one span per agent step:
agent, model, prompt_version, tokens, cost, latency, retries, status, verdict,
disagreements). The audit's top-level `agents` roster and `cost` summary
(total / avg-per-record / p95 latency / projected-per-10k) complete the picture.
`make trace ID=<id>` and `make replay ID=<id>` reconstruct a record's full
decision path and data lineage **from the log alone**.

## 7. Provenance & append-only

`fleet/audit.py` is append-only (no update/delete API) and every event carries a
`chain_hash = sha256(prev_hash | seq | ts | actor | action | record_id)`. Any
mutation or deletion of a past entry breaks the chain — `make probe-append-only`
proves both are detected. Timestamps derive from `PIPELINE_NOW + seq` (never the
wall clock), so a re-run is byte-identical (`make probe-idempotency`).

## 8. LLM replay contract

`fleet/llm.py`: in `REPLAY_LLM=true` (default) only the model call is replaced — a
committed transcript whose canonical **request** hash matches is returned; each
transcript is tagged with the calling `agent`, so the gate proves the load-bearing
call was a Worker. In `REPLAY_LLM=false` it calls an OpenAI-compatible endpoint and
records a transcript in the same shape. Deterministic steps (intake, normalize,
rules, router, verifier, state machine, audit) are **never** stubbed.
