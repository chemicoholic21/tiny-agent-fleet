# CEDX Systems — Build Task v2: "Build a Tiny CEDX Agent Fleet"
**Role:** AI Full-Stack Automation Engineer · **Live kickoff:** ~1h on Zoom · **Hard deadline:** 72h from your interview · **Submit:** access-code-gated portal (public GitHub repo link + optional live URL + notes).

CEDX builds the AI systems that run real companies — fleets of AI agents doing the work of whole departments, correctly, on data nobody pre-screened. **The hard part is not making one agent answer. The hard part is the architecture: how you make many agents collaborate, check each other, stay cheap at scale, and never deliver garbage — and whether YOU understand the system well enough to extend it on demand.**

**AI assistants (Claude, GPT, Copilot, Cursor) are allowed and expected.** We assume the code is AI-written. That's the point — *we are not grading code, we are grading the system you designed and how well you own it.* The easy parts an agent does in minutes; this task is built to surface the part only a real engineer gets right: **orchestration, reliability, observability, cost, and live ownership.**

Your task: build a small but **genuinely working multi-agent pipeline** for one industry. Not a mockup, not a slide deck, not a single happy-path ChatGPT call. It must run end-to-end with one command on data we give you — including data you have never seen — and survive failures we inject into the agents themselves.

---

## How grading works (read first)
Two datasets:
1. **Development seed (`/seed`)** — shipped at kickoff. Build/test against it. Documented planted problems (below).
2. **Held-out seed you never see** — used at grading. Same problem TYPES, different records, different field-rename names, different outlier magnitudes, shuffled order, ≥1 anomaly NOT documented, AND **injected agent-level failures** (a worker agent that hallucinates, loops, or returns malformed output). Your repo runs against it via one env var (`SEED_DIR`).

Hardcoding to known IDs/values passes `/seed` and FAILS the held-out seed → zeros the heaviest-weighted criteria. Everything must be rule-based + generalize. Anything that fails validation but matches no known rule routes to the exception queue — never silently delivered. We verify your LLMs are real + load-bearing, AND that your **agent topology is real** (≥3 agents with distinct roles actually exchanging messages — not one function with three prompts).

## Step 1 — Pick your lane
Go to **cedxsystems.com/workflows**, choose **exactly ONE** industry. Same governed 5-stage pipeline for all:
> Sources/Intake → Orchestration (Normalize + Exception queue) → Assembly → Review (Operator + Approval chain) → Delivery (Branded package + Audit archive).
Not graded on domain expertise. Depth of architecture beats breadth of domain. State industry + tier in README.

## Step 2 — What we hand you at kickoff
- **`/seed`** — ~25 generic "work-request" records (`id`, `owner`, `deadline`, a primary numeric field, payload, free-text `notes`). Two formats: `feed.json` (~20) + `inbox/` of PDF + `.eml` (rest; some planted problems arrive only here). **DO NOT EDIT `/seed`** (canonical-hash diffed; editing = auto-fail).
- **`audit.schema.json`** — schema your `/out/audit.json` must conform to (now includes `agents`, `agent_trace`, `cost`).
- **`verify_audit.py`** — first grading gate. Run it yourself before submitting. Do NOT modify.
- **Reference `Dockerfile` + `docker-compose.yml`** — single-command run on the grading box (linux/amd64).
- **Your CASE_ID** (e.g. `CEDX-7F3A`) — assigned live, unique to you; parameterizes your amendment; appears in final artifact, audit bundle, final commit message.
- **The live amendment** (Step 8) + **(optional) scoped low-cap CEDX test key**.

### Planted problems — DATA layer (in `/seed`; held-out has same TYPES, different values)
**Class A — BLOCKING (never reach Delivery until a human resolves):**
| Problem | Reason code |
|---|---|
| `deadline` already passed at intake | `STALE` |
| required field null | `MISSING_INPUT` (no auto-default) |
| extreme numeric outlier (define+justify threshold — robust stat, not a hardcoded `==`) | `OUTLIER` |
| `notes` contains "approve immediately / skip review / ignore your rules…" | `INJECTION_BLOCKED` |
| record so ambiguous the LLM can't produce valid/confident output | `LOW_CONFIDENCE` (abstain → route, don't guess) |
| fails validation, matches none above | `UNVERIFIED_ANOMALY` (catches the held-out unknown) |

### Planted problems — AGENT layer (NEW; mainly in held-out, one sample in `/seed`)
Your **Verifier agent** must catch a peer agent misbehaving and route it — bad agent output must never reach delivery:
| Problem | Reason code | What your fleet must do |
|---|---|---|
| Worker agent invents a field/value not supported by the source | `AGENT_HALLUCINATION` | Verifier rejects → retry → if still bad, route to human |
| Worker agent loops / exceeds step budget on one record | `AGENT_LOOP` | orchestrator kills the run, caps steps, routes record |
| Worker returns structurally invalid output the repair step can't fix | `AGENT_MALFORMED` | bounded retry, then abstain → human |
| A record's processing would blow the per-record cost/latency ceiling | `BUDGET_EXCEEDED` | downgrade model or route; never silently overspend |

**Class B — AUTO-RESOLVED & LOGGED (continues to delivery):** `SCHEMA_DRIFT` (field renamed mid-batch → map both to canonical, log) · `SUPERSEDED_VERSION` (same id twice → use latest, log superseded).

Final package = all clean + all Class-B records. No Class-A / agent-failure record unless a human approved/edit-resolved it. State which records reached delivery in README.

## Step 3 — Build it as an AGENT FLEET (the core upgrade)
You must implement the 5 stages as **≥3 cooperating agents with explicit, typed handoff contracts** — not one monolith. Minimum roster:
1. **Orchestrator / Planner** — owns the run, decides which agent handles each record, enforces step + cost budgets, routes exceptions. No business logic buried here; it delegates.
2. **Worker agent(s)** — do the Assembly draft (the LLM-heavy step). At least one; a router that picks a **cheap vs strong model per record** is expected (see Step 5).
3. **Verifier / Critic agent** — independently checks the Worker's output against the source before anything is delivered. This is the agent-checks-agent gate; it produces the `AGENT_HALLUCINATION` / `AGENT_MALFORMED` catches.

Rules that make it a real fleet (graded):
- **Typed contracts:** each agent has a declared input/output schema and a declared list of which agents it may call. Free-form string passing = markdown.
- **No god-function:** an Orchestrator that inlines the Worker + Verifier as plain function calls in one file fails this. Agents must be separable, individually testable units with their own prompt/version.
- **The Verifier must be able to OVERRULE the Worker** and that disagreement is logged with both sides.

The 5 governed stages still apply *underneath* the fleet:
1. **Intake** — parse BOTH formats; persist each record w/ owner + deadline. No hardcoded in-memory arrays.
2. **Orchestration** — declarative normalization to a **versioned output-schema artifact** + separate field-mapping file; exception queue catches every problem (data + agent layer) w/ correct reason code + class; Class-A doesn't proceed.
3. **Assembly** — Worker agent drafts structured/branded output; record input hash + model + prompt version; enforced structured output w/ retry/repair + abstain path.
4. **Review** — operator surface (CLI fine): approve/reject/request-changes/edit-resolve; every action appends to audit w/ actor+time+before/after. Approval chain = explicit state machine (`draft→in_review→changes_requested→approved→delivered`); **delivery refused server-side for any non-approved item, refusal logged.**
5. **Delivery + Audit** — branded package + append-only `/out/audit.json`; CASE_ID present.

## Step 4 — Observability: traces are mandatory
Every record carries an **`agent_trace`**: an ordered list of spans, one per agent step — `{agent, model, prompt_version, tokens_in, tokens_out, cost_usd, latency_ms, retries, status}`. You can't run a fleet you can't see.
- `make trace ID=<id>` reconstructs that record's full agent decision path from the log alone (which agent ran, what it cost, what the Verifier said, where it routed).
- `out/audit.json` has a top-level `agents` roster (name, role, model(s), `can_call`) and a `cost` summary (total + per-record).

## Step 5 — Scale economics: cost + latency budget (you WILL be measured on this)
At real scale this is the job. Your fleet must be **cheap by design**, not by luck.
- Implement a **model router**: trivial/clean records use a cheap model (e.g. gpt-4o-mini / haiku / gemini-flash); only hard or Verifier-flagged records escalate to a stronger model. Justify the policy in DECISIONS.md.
- Enforce a **per-record cost + step ceiling**. A record that would exceed it raises `BUDGET_EXCEEDED` → downgrade or route, never silent overspend.
- Report in README: **avg cost/record, p95 latency/record, total run cost**, and your projected **cost at 10,000 records/day**.

## Step 6 — The controls + agent reliability are ~70% of score — uniform probe CLI (thin Makefile over any stack)
| Command | Must do | Pass = |
|---|---|---|
| `make demo` | full fleet, `REPLAY_LLM=true`, on SEED_DIR | exit 0; writes package + audit.json + exception dump |
| `make verify` | run `verify_audit.py` on `/out/audit.json` | exit 0 |
| `make trace ID=<id>` | print full agent decision path for one record | exit 0; shows agents+cost+verifier verdict |
| `make eval` | run your agent eval harness (≥10 golden cases + LLM-judge per agent) | exit 0; prints per-agent scores |
| `make replay ID=<id>` | reconstruct that output's data lineage from the log alone | exit 0; prints lineage |
| `make probe-approval` | try to deliver a non-approved item | exit 0 only if refused + logged |
| `make probe-agent-failure` | feed a hallucinated/malformed worker output | exit 0 only if Verifier catches + routes, not delivered |
| `make probe-budget` | feed a record that exceeds the cost/step ceiling | exit 0 only if `BUDGET_EXCEEDED` raised + handled |
| `make probe-append-only` | try to mutate/delete a past audit entry | exit 0 only if refused |
| `make probe-idempotency` | run demo twice | exit 0 only if no dupes on run 2 |
| `make probe-crash` *(BONUS)* | SIGKILL between stages, re-run | exit 0 if resumes w/o dupes |

Missing probe = forfeit that criterion.

## Step 7 — LLM + agent contract (verify the fleet is real)
- **`REPLAY_LLM=true` (default, offline):** ONLY the model calls are replaced — by committed `/transcripts/*.json` (request + raw response + response hash + model + prompt version + which agent made the call). Every agent's call is replayed deterministically, including the planted ambiguous record (its transcript IS the low-confidence response so abstain fires) and the sample agent-failure (its transcript IS the hallucinated output so the Verifier fires). Stubbing intake/parse/normalize/exceptions/router/state-machine/audit = auto-fail.
- **`REPLAY_LLM=false` (real):** reads `LLM_API_KEY`/`LLM_MODEL`/`LLM_BASE_URL`; support ≥1 of `gpt-4o-mini`, `claude-3-5-haiku`, `gemini-1.5-flash`. At grading we run the REAL path + CEDX key against the HELD-OUT seed → verifies generalization, load-bearing LLMs, agent-checks-agent, unknown-anomaly catch, AND that the router actually downgrades on easy records — all in one run.

## Step 8 — Live amendment (CASE_ID-bound, leaked answer is useless)
Adds a maker-checker SECOND approval gate parameterized by your CASE_ID:
```
H = sha256(CASE_ID)  # lowercase hex
R = ["risk_officer","legal_counsel","compliance","finance_controller"][ int(H[0],16) % 4 ]
T = 10000 + (int(H[1:3],16) % 50) * 1000
RULE: any record whose normalized primary numeric field >= T needs a recorded approval by role R, in addition to normal approval, before delivery.
```
Print `AMENDMENT: role=<R> threshold=<T>` at startup; record both under `amendment` in audit.json; `make probe-approval` honors it. Push a tracer commit during the live call (scaffold + `SCOPE.md` w/ CASE_ID) before the kickoff checkpoint — GitHub push receive-time is recorded server-side as the authorship anchor.

## Step 9 — LIVE EXTENSION (the ownership gate — cannot be faked)
After you submit, you join a short **live extension call**. We hand you a small NEW requirement on the spot and you implement it in your own codebase, screen-shared, in ~20 minutes. Examples (you get ONE, unseen):
- add a 4th agent (e.g. a **Redactor** that strips PII before delivery) and wire it into the contract + trace;
- add a new reason code + detector and prove it routes correctly;
- change the router policy and show the cost number move;
- make the Verifier require two independent passes before approving a high-value record.

**If an AI built your system and you don't understand it, you fail here — regardless of how good the submission looked.** This is intentional and it is the single most important gate. Bring the repo running locally and be ready to edit live.

## Run contract (non-negotiable)
ONE command (`docker compose up` / `make demo`) on a fresh network-restricted machine against SEED_DIR, no manual entry, offline path < ~5 min. Prints stage + agent progress; writes package + audit.json + exception queue. If it doesn't run, we stop reading.

**Auto-zero (any one):** (1) doesn't run end-to-end; (2) no real append-only audit + human-in-the-loop; (3) **not a real multi-agent system** (single god-function, no independent Verifier); (4) **fail the live extension** (can't modify your own system).
**Fail-to-advance:** (5) detectors hardcoded, fail held-out; (6) LLMs not load-bearing on held-out; (7) no model router / no cost accounting.

## Submit (via portal)
- Public GitHub repo (incl. `/transcripts`, `/agents` (or equiv. per-agent modules), Makefile, docker-compose, `/out` written at runtime — no pre-generated outputs).
- **ARCHITECTURE.md** — the agent topology diagram: every agent, its role, its typed contract, who it may call, where the Verifier overrules the Worker, where the budget/router decisions live. This + the live call are how we judge the real skill.
- **DECISIONS.md** (≤2 pp): what you did NOT automate + why; outlier/abstain thresholds + why they generalize; router policy + the cost numbers; how provenance survives re-run; what breaks first at 10k records; CASE_ID.
- **3–5 min narrated Loom (your voice; missing = auto-reject):** demo; a Class-A problem firing; the Verifier catching a bad agent; approval blocking then releasing; injection neutralized; `make trace`; a tour of the agent topology + amendment.
- **README (11 fixed sections in order):** 1 Industry & Scope (+tier, CASE_ID) · 2 Agent topology (roster + contracts + file pointers) · 3 How to Run · 4 Controls · 5 Planted-problem handling (data + agent layer) · 6 Generalization · 7 LLM/agent contract & eval · 8 Cost & scale (avg/$record, p95, 10k projection) · 9 Amendment · 10 AI usage / real-vs-faked · 11 Tradeoffs & next week.
- **Eval harness:** ≥10 golden cases + an LLM-judge per agent; scores reported via `make eval`.
- **Portal notes block:** Live URL / Loom / Industry+Tier / CASE_ID / Demo login / Hardest problem / one-line topology.
- CASE_ID in final artifact + audit bundle + final commit message.

---

## Time model (recommended)
Live Zoom (~1h) = kickoff + scope-lock + tracer-bullet commit + amendment reveal + Q&A (NOT the build). Server-enforced checkpoint shortly after: each candidate must have PUSHED chosen industry + SCOPE.md (CASE_ID) + runnable scaffold showing the ≥3 named agents + one tracer commit. Push receive-time = authorship anchor. Then 72h take-home, hard deadline 72h from your interview. Then the short **live extension call** before a human grades.

## Grading rubric (weighted)
1. Agent topology + typed contracts + Verifier-overrules-Worker (real fleet, not god-function) — **18%**
2. Exception queue + planted-problem coverage (data + agent layer) + held-out generalization — **18%**
3. Append-only audit + provenance + agent_trace + replay/trace — **16%**
4. Approval chain state machine + CASE_ID amendment — **10%**
5. Cost/latency budget + model router + scale numbers — **12%**
6. Declarative normalization + agent eval harness (LLM-judge per agent) — **10%**
7. Prompt-injection neutralization — **6%**
8. Idempotency + resumability — **6%**
9. Live-extension readiness + understanding & ownership (Loom, ARCHITECTURE.md, DECISIONS.md, honesty) — **4%**
   *(a FAILED live extension is an auto-zero regardless of this weight — see Run contract.)*

---

## PRE-LAUNCH BLOCKER (founder must do before kickoff)
Build + dogfood the v2 fixture: `/seed` (two formats + all DATA planted problems + one sample AGENT-failure record with its hallucinated transcript), the held-out seed (incl. injected agent failures), updated `audit.schema.json` (`agents`/`agent_trace`/`cost`), updated `verify_audit.py` (asserts ≥3 agents present, every delivered field hashes to a transcript tagged with the calling agent, Verifier verdicts logged, cost present), reference Dockerfile/compose. Have 2–3 engineers complete one GOLDEN submission and one deliberately-CHEATING (god-function + hardcoded + stubbed-LLM) submission; confirm the gate accepts the golden and rejects the cheat, AND that the cheat collapses on the live extension. Prepare the bank of unseen live-extension prompts. The whole assessment depends on this existing and working.
