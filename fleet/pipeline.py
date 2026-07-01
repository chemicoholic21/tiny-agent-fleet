"""The governed 5-stage pipeline, wired as an agent fleet.

Intake -> Normalization/Exception -> Assembly (Worker) -> Review (Verifier +
Operator + approval state machine) -> Delivery (branded 837 claim batch + append-only
audit). The Orchestrator drives per-record processing; this module owns wiring,
batch-level statistics (the robust outlier fence, cost/latency roll-up) and output
serialization.
"""
from __future__ import annotations
import json
from pathlib import Path

from .config import Config, PIPELINE_VERSION, INDUSTRY, TIER
from .events import EventBus
from .audit import AuditLog
from .intake import intake
from .normalize import normalize
from .rules import evaluate_data_rules, compute_outlier_bounds
from .agents.router import Router
from .agents.worker import CodingWorker
from .agents.verifier import Verifier
from .agents.operator import Operator
from .agents.orchestrator import Orchestrator, RecordResult
from .llm import LLMClient
from .util import sha


class Fleet:
    def __init__(self, cfg: Config, honor_amendment: bool = True):
        self.cfg = cfg
        self.bus = EventBus()
        self.audit = AuditLog(cfg.pipeline_now)
        self.llm = LLMClient(cfg)
        self.router = Router(self.bus, self.audit)
        self.worker = CodingWorker(self.bus, self.audit, self.llm)
        self.verifier = Verifier(self.bus, self.audit)
        self.operator = Operator(self.bus, self.audit, cfg.amendment, honor_amendment)
        self.orchestrator = Orchestrator(self.bus, self.audit, cfg, self.router,
                                         self.worker, self.verifier, self.operator)
        self.agents = [self.orchestrator, self.router, self.worker,
                       self.verifier, self.operator]

    def roster(self) -> list:
        return [a.spec.roster_entry() for a in self.agents]

    # -- run -------------------------------------------------------------------
    def run(self) -> dict:
        self.audit.append("Fleet", "run_start", detail={
            "case_id": self.cfg.case_id, "seed_dir": str(self.cfg.seed_dir),
            "amendment_role": self.cfg.amendment.role,
            "amendment_threshold": self.cfg.amendment.threshold})

        raws = intake(self.cfg.seed_dir)
        self.audit.append("Intake", "intake_complete", detail={"count": len(raws)})

        norm = normalize(raws)
        for rid in norm.drift:
            self.audit.append("Normalization", "schema_drift_mapped", record_id=rid,
                              detail={"class": "B"})
        results: list[RecordResult] = []

        # Superseded (Class-B): emit a record, do not reprocess.
        for old, winning in norm.superseded:
            self.audit.append("Normalization", "superseded_version", record_id=old.id,
                              detail={"kept_version": winning, "class": "B"})
            results.append(RecordResult(
                id=old.id, version=old.version, source_format=old.source_format,
                source_version_hash=old.source_version_hash, status="superseded",
                reason_code="SUPERSEDED_VERSION", reason_class="B",
                agent_trace=[], approval_trail=[]))

        amounts = [r.amount for r in norm.records]
        bounds = compute_outlier_bounds(amounts)
        if bounds:
            self.audit.append("Normalization", "outlier_fence", detail={
                "median": round(bounds[0], 2), "mad": round(bounds[1], 2),
                "lower": round(bounds[2], 2), "upper": round(bounds[3], 2)})

        for rec in sorted(norm.records, key=lambda r: r.id):
            finding = evaluate_data_rules(rec, bounds, self.cfg.pipeline_now)
            res = self.orchestrator.process(rec, finding)
            # Tag Class-B schema drift on delivered records (logged, still delivered).
            if res.status == "delivered" and rec.schema_drift:
                res.reason_code = "SCHEMA_DRIFT"
                res.reason_class = "B"
            results.append(res)

        return self._finalize(results)

    # -- output ----------------------------------------------------------------
    def _finalize(self, results: list[RecordResult]) -> dict:
        delivered = [r for r in results if r.status == "delivered"]
        exceptions = [r for r in results if r.status == "exception"]

        # Branded package: the RCM 837 claim batch.
        package = {
            "package": "CEDX RCM 837 Claim Batch",
            "case_id": self.cfg.case_id,
            "industry": INDUSTRY,
            "pipeline_version": PIPELINE_VERSION,
            "claims": [
                {"id": r.id, "delivered_fields": r.delivered_fields,
                 "delivered_fields_hash": r.delivered_fields_hash,
                 "transcript_hash": r.transcript_hash}
                for r in sorted(delivered, key=lambda x: x.id)
            ],
        }
        output_package_hash = sha(package)

        # Cost + latency roll-up from the agent traces.
        total_cost = 0.0
        per_record_latency = []
        for r in results:
            lat = 0.0
            for span in r.agent_trace:
                c = span.get("cost_usd")
                if isinstance(c, (int, float)):
                    total_cost += c
                l = span.get("latency_ms")
                if isinstance(l, (int, float)):
                    lat += l
            if r.agent_trace:
                per_record_latency.append(lat)
        n = max(len(delivered) + len(exceptions), 1)
        p95 = _percentile(per_record_latency, 95) if per_record_latency else 0.0
        cost = {
            "total_usd": round(total_cost, 8),
            "avg_usd_per_record": round(total_cost / n, 8),
            "p95_latency_ms": round(p95, 2),
            "records": n,
            "projected_usd_per_10k": round((total_cost / n) * 10000, 4),
        }

        self.audit.append("Delivery", "package_sealed", detail={
            "delivered": len(delivered), "exceptions": len(exceptions),
            "output_package_hash": output_package_hash})

        audit_bundle = {
            "case_id": self.cfg.case_id,
            "pipeline_version": PIPELINE_VERSION,
            "generated_at": f"{self.cfg.pipeline_now}T00:00:00Z",
            "seed_dir": str(self.cfg.seed_dir),
            "pipeline_now": self.cfg.pipeline_now,
            "industry": INDUSTRY,
            "tier": TIER,
            "amendment": {"role": self.cfg.amendment.role,
                          "threshold": self.cfg.amendment.threshold,
                          "rule": ("records whose normalized amount >= threshold need a "
                                   f"recorded approval by role '{self.cfg.amendment.role}'")},
            "agents": self.roster(),
            "cost": cost,
            "output_package_hash": output_package_hash,
            "records": [self._record_dict(r) for r in results],
            "events": self.audit.chained_dicts(),
        }

        exception_queue = {
            "case_id": self.cfg.case_id,
            "count": len(exceptions),
            "items": [
                {"id": r.id, "reason_code": r.reason_code, "reason_class": r.reason_class,
                 "source_format": r.source_format,
                 "detail": next((s.get("detail") for s in r.agent_trace
                                 if s.get("detail")), None)}
                for r in exceptions
            ],
        }

        out = self.cfg.out_dir
        out.mkdir(parents=True, exist_ok=True)
        _write_json(out / "rcm_claim_batch.json", package)
        _write_json(out / "audit.json", audit_bundle)
        _write_json(out / "exception_queue.json", exception_queue)

        return {"package": package, "audit": audit_bundle,
                "exception_queue": exception_queue,
                "delivered": len(delivered), "exceptions": len(exceptions),
                "cost": cost}

    def _record_dict(self, r: RecordResult) -> dict:
        return {
            "id": r.id,
            "version": r.version,
            "source_format": r.source_format,
            "source_version_hash": r.source_version_hash,
            "status": r.status,
            "reason_code": r.reason_code,
            "reason_class": r.reason_class,
            "transcript_hash": r.transcript_hash,
            "delivered_fields": r.delivered_fields,
            "delivered_fields_hash": r.delivered_fields_hash,
            "agent_trace": r.agent_trace,
            "approval_trail": r.approval_trail,
        }


def _percentile(data: list, pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _write_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
