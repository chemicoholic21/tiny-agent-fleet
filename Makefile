# Uniform probe interface — graders invoke THESE targets identically on every repo.
# Healthcare Administration (RCM) agent fleet. Thin wrapper over the Python package.
SEED_DIR ?= seed
PY ?= python3

.PHONY: demo verify trace eval replay probe-approval probe-agent-failure probe-budget \
        probe-append-only probe-idempotency probe-crash transcripts clean

# Full multi-agent pipeline, offline replay, on $(SEED_DIR).
demo:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli demo

# Run the PROVIDED gate on your audit bundle. Do not modify verify_audit.py.
verify:
	$(PY) verify_audit.py --audit out/audit.json --transcripts transcripts --schema audit.schema.json

# Print one record's FULL agent decision path from the log alone.
trace:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli trace $(ID)

# Run the agent eval harness: >=10 golden cases + an LLM-judge per agent.
eval:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli eval

# Reconstruct one delivered output's DATA lineage from the append-only log alone.
replay:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli replay $(ID)

# Human-in-the-loop Operator surface: approve/reject/request-changes/edit-resolve.
# e.g. make review ARGS="REC-012 edit-resolve dr.reyes amount 5200"
review:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli review $(ARGS)

# Prove REPLAY reproduces delivered outputs byte-for-byte from transcripts alone.
verify-replay:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli verify-replay

probe-approval:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli probe-approval

probe-agent-failure:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli probe-agent-failure

probe-budget:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli probe-budget

probe-append-only:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli probe-append-only

probe-idempotency:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli probe-idempotency

probe-crash:
	SEED_DIR=$(SEED_DIR) $(PY) -m fleet.cli probe-crash

# Regenerate committed replay transcripts from the seed (dev-time recording step).
transcripts:
	SEED_DIR=$(SEED_DIR) $(PY) tools/gen_transcripts.py

# GENERALIZATION PROOF: run the SAME fleet on a totally different vertical
# (freight-invoice auditing) via a swapped domain config + seed. No code changes.
transcripts-alt:
	DOMAIN_CONFIG=domain_config.alt.json SEED_DIR=seed_alt TRANSCRIPTS_DIR=transcripts_alt \
	  GEN_ABSTAIN_IDS=ALT-10 GEN_HALLUCINATE_IDS= $(PY) tools/gen_transcripts.py

demo-alt:
	DOMAIN_CONFIG=domain_config.alt.json SEED_DIR=seed_alt TRANSCRIPTS_DIR=transcripts_alt \
	  OUT_DIR=out_alt CASE_ID=$${CASE_ID:-CEDX-DEMO1} $(PY) -m fleet.cli demo
	$(PY) verify_audit.py --audit out_alt/audit.json --transcripts transcripts_alt --schema audit.schema.json

clean:
	rm -rf out
