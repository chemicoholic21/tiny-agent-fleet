"""Deterministic business rules → reason codes (Stage 2, exception detection).

These are the RCM "decision points": eligibility / timely-filing / medical-necessity
style checks. They are pure CODE — no LLM is used for validation that a rule can do
(per the design constraint). The LLM is reserved for the Worker's coding/summary draft.

Data-layer reason codes produced here:
  STALE               deadline (timely-filing) already passed at intake  [Class A]
  MISSING_INPUT       required field null, no auto-default                [Class A]
  OUTLIER             extreme charge via robust MAD test                  [Class A]
  INJECTION_BLOCKED   notes carry a prompt-injection payload              [Class A]
  UNVERIFIED_ANOMALY  fails validation, matches no known rule             [Class A]
  (LOW_CONFIDENCE is raised later by the Worker's abstain, not here.)

SCHEMA_DRIFT / SUPERSEDED_VERSION (Class B) are detected in normalize.py.
"""
from __future__ import annotations
import re
import statistics
from dataclasses import dataclass
from typing import Optional

from .events import NormalizedRecord

# Robust outlier threshold: modified z-score using median absolute deviation.
# |0.6745 * (x - median) / MAD| > 3.5 is the classic Iglewicz-Hoaglin cutoff.
# This is distribution-shape based, NOT a hardcoded value, so it generalizes to
# the held-out seed whose outlier magnitude differs.
MAD_Z_CUTOFF = 3.5

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disregard\s+(your|all|the)\s+(rules|instructions|guardrails)",
    r"approve\s+(this\s+)?immediately",
    r"skip\s+(the\s+)?review",
    r"output\s+approved",
    r"you\s+are\s+now",
    r"system\s*:\s*",
]

VALID_CATEGORIES = {"ONBOARDING", "RENEWAL", "REVIEW", "REPORT", "INTAKE"}


@dataclass
class RuleFinding:
    reason_code: str
    reason_class: str  # "A" or "B"
    detail: str


def detect_injection(rec: NormalizedRecord) -> Optional[RuleFinding]:
    text = rec.notes.lower()
    for pat in INJECTION_PATTERNS:
        if re.search(pat, text):
            return RuleFinding("INJECTION_BLOCKED", "A",
                               f"injection pattern matched: /{pat}/")
    return None


def detect_missing_input(rec: NormalizedRecord) -> Optional[RuleFinding]:
    missing = []
    if rec.amount is None:
        missing.append("amount")
    if not rec.owner:
        missing.append("owner")
    if not rec.deadline:
        missing.append("deadline")
    if missing:
        return RuleFinding("MISSING_INPUT", "A",
                           f"required field(s) null: {', '.join(missing)}")
    return None


def detect_stale(rec: NormalizedRecord, pipeline_now: str) -> Optional[RuleFinding]:
    if rec.deadline and rec.deadline < pipeline_now:
        return RuleFinding("STALE", "A",
                           f"deadline {rec.deadline} < now {pipeline_now}")
    return None


def detect_unverified_anomaly(rec: NormalizedRecord) -> Optional[RuleFinding]:
    # A claim in notes that a *different* amount is the real one, unverifiable.
    m = re.search(r"real\s+number\s+is\s+([0-9][0-9,]*)", rec.notes.lower())
    if m:
        claimed = float(m.group(1).replace(",", ""))
        if rec.amount is None or abs(claimed - rec.amount) > 1e-6:
            return RuleFinding(
                "UNVERIFIED_ANOMALY", "A",
                f"notes assert unverifiable amount {claimed} vs field {rec.amount}")
    # Category present but outside the known set.
    if rec.category is not None and rec.category not in VALID_CATEGORIES:
        return RuleFinding("UNVERIFIED_ANOMALY", "A",
                           f"unknown category '{rec.category}'")
    return None


def compute_outlier_bounds(amounts: list[float]) -> Optional[tuple[float, float, float, float]]:
    """Return (median, mad, lower_fence, upper_fence) or None if not enough data."""
    vals = [a for a in amounts if a is not None]
    if len(vals) < 4:
        return None
    med = statistics.median(vals)
    mad = statistics.median([abs(a - med) for a in vals])
    if mad == 0:
        # Degenerate spread; fall back to a scaled IQR so we still catch extremes.
        mad = (max(vals) - min(vals)) / 6.0 or 1.0
    span = (MAD_Z_CUTOFF * mad) / 0.6745
    return med, mad, med - span, med + span


def detect_outlier(rec: NormalizedRecord, bounds) -> Optional[RuleFinding]:
    if bounds is None or rec.amount is None:
        return None
    med, mad, lo, hi = bounds
    if rec.amount < lo or rec.amount > hi:
        z = abs(0.6745 * (rec.amount - med) / mad) if mad else float("inf")
        return RuleFinding("OUTLIER", "A",
                           f"amount {rec.amount} outside robust fence "
                           f"[{lo:.0f},{hi:.0f}] (mod z={z:.1f})")
    return None


def evaluate_data_rules(rec: NormalizedRecord, bounds, pipeline_now: str) -> Optional[RuleFinding]:
    """Run the deterministic Class-A data checks in priority order."""
    for detector in (
        lambda: detect_injection(rec),
        lambda: detect_missing_input(rec),
        lambda: detect_stale(rec, pipeline_now),
        lambda: detect_outlier(rec, bounds),
        lambda: detect_unverified_anomaly(rec),
    ):
        finding = detector()
        if finding:
            return finding
    return None
