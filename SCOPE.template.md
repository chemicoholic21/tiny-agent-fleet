# SCOPE — push this during the live call (tracer checkpoint)

> Rename to `SCOPE.md`, fill in, commit + push during the Zoom. We record your
> GitHub **push receive-time** server-side as your authorship anchor.

- **Candidate name:**
- **CASE_ID (assigned live):** CEDX-XXXX
- **Industry chosen (from cedxsystems.com/workflows):**
- **Tier:**
- **Stack / language:**

## Amendment (compute from your CASE_ID)
```
H = sha256(CASE_ID)
role R      = ["risk_officer","legal_counsel","compliance","finance_controller"][ int(H[0],16) % 4 ]
threshold T = 10000 + (int(H[1:3],16) % 50) * 1000
```
- **My role R:**
- **My threshold T:**

## What I will build (the 5 governed stages)
- [ ] Sources/Intake (parse feed.json + inbox PDF/email)
- [ ] Orchestration (declarative normalize + exception queue, all reason codes)
- [ ] Assembly (LLM structured output + abstain path)
- [ ] Review (operator surface + approval state machine + my CASE_ID amendment)
- [ ] Delivery (branded package + append-only audit + replay)

## What I will deliberately NOT build (and why)
-
