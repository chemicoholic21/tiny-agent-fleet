"""Stage 2 (part) — Declarative normalization.

Raw records from heterogeneous sources are mapped to the canonical schema using a
*separate* field-mapping file (field_map.json) and validated against a *versioned*
output-schema artifact (normalization.schema.json). Two Class-B conditions are
detected and logged here (they still proceed to delivery):

  SCHEMA_DRIFT       — a field arrived under a renamed alias (e.g. "Value" -> amount)
  SUPERSEDED_VERSION — the same id arrived twice; keep the latest, log the older
"""
from __future__ import annotations
import json
from pathlib import Path

from .events import NormalizedRecord
from .intake import RawRecord

_HERE = Path(__file__).resolve().parent.parent


def load_field_map() -> dict:
    return json.loads((_HERE / "field_map.json").read_text(encoding="utf-8"))


def load_output_schema() -> dict:
    return json.loads((_HERE / "normalization.schema.json").read_text(encoding="utf-8"))


class NormalizationResult:
    def __init__(self):
        self.records: list[NormalizedRecord] = []
        self.superseded: list[tuple[NormalizedRecord, int]] = []  # (rec, winning_version)
        self.drift: list[str] = []  # record ids that had a renamed field


def normalize(raws: list[RawRecord]) -> NormalizationResult:
    fm = load_field_map()
    canonical_of = fm["canonical_of"]
    drift_aliases = set(fm["drift_aliases"])

    result = NormalizationResult()
    by_id: dict[str, NormalizedRecord] = {}

    for raw in raws:
        canon_fields: dict = {}
        had_drift = False
        for key, val in raw.fields.items():
            target = canonical_of.get(key, key)
            if key in drift_aliases:
                had_drift = True
            # First writer wins per canonical field unless empty.
            if target not in canon_fields or canon_fields[target] in (None, ""):
                canon_fields[target] = val

        rid = canon_fields.get("id")
        if not rid:
            # Unidentifiable record — still surface it as an anomaly downstream.
            rid = f"UNKNOWN-{raw.source_version_hash[-8:]}"
            canon_fields["id"] = rid

        rec = NormalizedRecord(
            id=rid,
            version=int(canon_fields.get("version", 1) or 1),
            owner=canon_fields.get("owner"),
            deadline=canon_fields.get("deadline"),
            category=(canon_fields.get("category") or None),
            amount=_num(canon_fields.get("amount")),
            notes=str(canon_fields.get("notes") or ""),
            source_format=raw.source_format,
            source_version_hash=raw.source_version_hash,
            schema_drift=had_drift,
            raw=dict(raw.fields),
        )
        if had_drift:
            result.drift.append(rid)

        if rid in by_id:
            prev = by_id[rid]
            if rec.version >= prev.version:
                result.superseded.append((prev, rec.version))
                by_id[rid] = rec
            else:
                result.superseded.append((rec, prev.version))
        else:
            by_id[rid] = rec

    result.records = list(by_id.values())
    return result


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None
