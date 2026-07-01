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

clean:
	rm -rf out
