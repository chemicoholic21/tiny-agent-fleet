# DECISIONS

Industry: Healthcare Administration (RCM). CASE_ID (placeholder): **CEDX-DEMO1**
→ amendment **finance_controller @ 14000**.

## What I did NOT automate (and why)
- **Deterministic validation is code, not LLM.** Eligibility/timely-filing (STALE),
  missing charge (MISSING_INPUT), extreme charge (OUTLIER), prompt injection
  (INJECTION_BLOCKED), field-rename (SCHEMA_DRIFT), duplicate version
  (SUPERSEDED_VERSION) and unverifiable-figure conflicts (UNVERIFIED_ANOMALY) are
  pure rules (`fleet/rules.py`, `fleet/normalize.py`). They are cheaper, fully
  auditable, and generalize to unseen data. The LLM is reserved for the one step
  that needs judgement: drafting/summarising the coded claim.
- **The Verifier uses no LLM.** Grounding an emitted amount against the source is a
  comparison, not a reasoning task — using a model there would be slower, costlier
  and less trustworthy.
- **Human approvals stay human.** The pipeline *routes and gates*; it never
  auto-forges a compliance sign-off. In the demo the Operator records the
  maker-checker chain; in production these are real `make review` actions.

## Outlier & abstain thresholds (why they generalize)
- **Outlier:** robust **modified z-score** on the primary numeric field using the
  median and MAD (Iglewicz–Hoaglin, cutoff 3.5), computed per batch
  (`fleet/rules.py:compute_outlier_bounds`). It is distribution-shape based, not a
  hardcoded `== 250000`, so the held-out seed's different magnitude still trips it.
  On the dev seed the fence is ≈ [3703, 6297]; the planted 250000 scores mod-z ≈ 661.
- **Abstain / LOW_CONFIDENCE:** the Verifier routes to a human whenever the Worker
  sets `abstain` or `confidence < 0.55`. The pipeline *never guesses* — it abstains
  and routes. This is data-independent, so it holds on unseen ambiguous records.
- **UNVERIFIED_ANOMALY** is the deliberate catch-all: anything that fails validation
  but matches no specific rule (unknown category, notes asserting an unverifiable
  amount) routes here — this catches the held-out unknown anomaly.

## Router policy + cost numbers
- Cheap `gpt-4o-mini` by default; escalate to `gpt-4o` only on hardness signals
  (long/ambiguous notes, missing category) or a Verifier escalation. On the dev
  seed: **15 cheap** worker calls, **2 strong** (the REC-020 hallucination retry).
- Measured (dev seed, replay): **total $0.0098**, **avg $0.00045/record**,
  **p95 latency ≈ 120 ms/record**. **Projected $4.47 per 10,000 records.**
- Ceilings: `MAX_COST_USD_PER_RECORD=0.05`, `MAX_STEPS_PER_RECORD=8`. A record that
  would breach them raises `BUDGET_EXCEEDED` (downgrade-or-route) or `AGENT_LOOP`.

## How provenance survives a re-run
- Append-only, **hash-chained** audit (`chain_hash` per event). Timestamps derive
  from `PIPELINE_NOW + seq`, never the wall clock. The pipeline is deterministic and
  overwrites `out/` atomically, so run 2 is byte-identical to run 1
  (`make probe-idempotency`) and a killed run re-converges (`make probe-crash`).
- Every delivered field hashes back to a committed Worker transcript
  (`delivered_fields_hash` ↔ transcript), so lineage is reconstructable from the log
  alone (`make replay`).

## What breaks first at 10k records/day
- **The single-file `out/audit.json`.** At 10k/day it must become an append-only
  event store (Kafka/Kinesis + object storage), with the hash chain preserved per
  partition. The agent model is already event-driven, so this is a transport swap.
- **Synchronous per-record processing.** Records are independent → shard by
  `record_id` across workers; the Orchestrator becomes a queue consumer. The Router
  and budget ceilings keep spend bounded as volume grows.
- **PDF/email parsing variance** is the next reliability risk; it already routes
  unparseable records to `UNVERIFIED_ANOMALY` rather than guessing.
