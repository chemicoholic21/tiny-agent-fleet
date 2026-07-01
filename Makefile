# Uniform probe interface — graders invoke THESE targets identically on every repo,
# whatever language you build in. Wire each to your implementation. Exit codes matter.
# v2: adds agent-fleet targets (trace, eval, probe-agent-failure, probe-budget).
SEED_DIR ?= seed

.PHONY: demo verify trace eval replay probe-approval probe-agent-failure probe-budget \
        probe-append-only probe-idempotency probe-crash clean

# Full multi-agent pipeline, offline replay, on $(SEED_DIR). Must write out/<package>,
# out/audit.json (incl. agents roster + per-record agent_trace + cost), out/exception_queue.json.
demo:
	@echo "TODO: run your agent fleet (REPLAY_LLM=true) on $(SEED_DIR)"; false

# Run the PROVIDED gate on your audit bundle. Do not modify verify_audit.py.
verify:
	python3 verify_audit.py --audit out/audit.json --transcripts transcripts --schema audit.schema.json

# Print one record's FULL agent decision path from the log alone:
# which agent ran, model, tokens/cost, retries, Verifier verdict, where it routed.
trace:
	@echo "TODO: print agent_trace for ID=$(ID) from out/audit.json"; false

# Run your agent eval harness: >=10 golden cases + an LLM-judge per agent. Print per-agent scores.
eval:
	@echo "TODO: run agent eval harness, print per-agent scores"; false

# Reconstruct one delivered output's DATA lineage from the append-only log alone.
replay:
	@echo "TODO: print lineage for ID=$(ID) from the audit log"; false

# Exit 0 ONLY if delivery of a NON-approved item (incl. CASE_ID amendment role) is refused + logged.
probe-approval:
	@echo "TODO"; false

# Exit 0 ONLY if a hallucinated/malformed WORKER output is caught by the Verifier and routed
# (AGENT_HALLUCINATION / AGENT_MALFORMED) — never delivered.
probe-agent-failure:
	@echo "TODO"; false

# Exit 0 ONLY if a record exceeding the per-record cost/step ceiling raises BUDGET_EXCEEDED
# and is downgraded or routed — never silently overspent.
probe-budget:
	@echo "TODO"; false

# Exit 0 ONLY if mutating/deleting a past audit entry is refused.
probe-append-only:
	@echo "TODO"; false

# Exit 0 ONLY if running demo twice produces no duplicate outputs/exceptions/approvals.
probe-idempotency:
	@echo "TODO"; false

# BONUS. Exit 0 if the pipeline resumes from the last completed stage after a SIGKILL.
probe-crash:
	@echo "TODO (bonus)"; false

clean:
	rm -rf out
