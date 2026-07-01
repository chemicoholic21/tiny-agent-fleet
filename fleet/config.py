"""Runtime configuration + the CASE_ID-bound live amendment.

Everything the fleet needs to know about the environment lives here so the rest of
the code stays deterministic and testable. No wall-clock time is ever read — all
timestamps derive from PIPELINE_NOW so a re-run is byte-identical (idempotency).
"""
from __future__ import annotations
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

PIPELINE_VERSION = "rcm-fleet-1.0.0"

# Industry lane: Healthcare Administration — AI-orchestrated Revenue Cycle Management.
INDUSTRY = "Healthcare Administration (Revenue Cycle Management)"
TIER = "Mid-market hospital / physician-group RCM"

AMENDMENT_ROLES = ["risk_officer", "legal_counsel", "compliance", "finance_controller"]


def compute_amendment(case_id: str) -> "Amendment":
    """Maker-checker second gate, parameterised by CASE_ID (TASK.md Step 8)."""
    h = hashlib.sha256(case_id.encode("utf-8")).hexdigest()
    role = AMENDMENT_ROLES[int(h[0], 16) % 4]
    threshold = 10000 + (int(h[1:3], 16) % 50) * 1000
    return Amendment(role=role, threshold=float(threshold))


@dataclass(frozen=True)
class Amendment:
    role: str
    threshold: float


@dataclass
class Config:
    case_id: str = "CEDX-DEMO1"
    seed_dir: Path = Path("seed")
    out_dir: Path = Path("out")
    transcripts_dir: Path = Path("transcripts")
    replay_llm: bool = True
    pipeline_now: str = "2026-06-26"
    # Budget ceilings (per record). A record whose projected spend exceeds these
    # raises BUDGET_EXCEEDED rather than silently overspending.
    max_cost_usd_per_record: float = 0.05
    max_steps_per_record: int = 8
    # Real-LLM path (used only when replay_llm is False).
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_base_url: str = ""
    amendment: Amendment = field(init=False)

    def __post_init__(self):
        self.amendment = compute_amendment(self.case_id)

    @classmethod
    def from_env(cls) -> "Config":
        def _b(name: str, default: bool) -> bool:
            v = os.environ.get(name)
            if v is None:
                return default
            return v.strip().lower() in ("1", "true", "yes", "on")

        cfg = cls(
            case_id=os.environ.get("CASE_ID", "CEDX-DEMO1") or "CEDX-DEMO1",
            seed_dir=Path(os.environ.get("SEED_DIR", "seed")),
            out_dir=Path(os.environ.get("OUT_DIR", "out")),
            transcripts_dir=Path(os.environ.get("TRANSCRIPTS_DIR", "transcripts")),
            replay_llm=_b("REPLAY_LLM", True),
            pipeline_now=os.environ.get("PIPELINE_NOW", "2026-06-26"),
            max_cost_usd_per_record=float(os.environ.get("MAX_COST_USD_PER_RECORD", "0.05")),
            max_steps_per_record=int(os.environ.get("MAX_STEPS_PER_RECORD", "8")),
            llm_api_key=os.environ.get("LLM_API_KEY", ""),
            llm_model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
            llm_base_url=os.environ.get("LLM_BASE_URL", ""),
        )
        return cfg
