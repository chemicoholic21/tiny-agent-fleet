"""Stage 1 — Intake. Parse BOTH formats (feed.json + inbox/*.eml + inbox/*.pdf).

Each raw record is persisted with its owner, deadline, source format and a
source_version_hash (content hash) so provenance is anchored from the very first
touch. No hardcoded in-memory arrays: everything is read from SEED_DIR at runtime.
"""
from __future__ import annotations
import email
import json
import re
from pathlib import Path
from typing import Optional

from .util import sha_bytes, sha


class RawRecord:
    def __init__(self, fields: dict, source_format: str, source_version_hash: str,
                 source_path: str):
        self.fields = fields
        self.source_format = source_format
        self.source_version_hash = source_version_hash
        self.source_path = source_path

    @property
    def id(self) -> Optional[str]:
        return self.fields.get("id")


def _parse_kv_text(text: str) -> dict:
    """Parse 'Key: value' lines (used by .eml body and extracted PDF text)."""
    out: dict = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z _]*?)\s*:\s*(.*)$", line)
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        val = m.group(2).strip()
        if key in ("id", "owner", "deadline", "amount", "value", "category",
                   "version", "notes"):
            out[key] = val
    return out


def _coerce(fields: dict) -> dict:
    out = dict(fields)
    if "version" in out and isinstance(out["version"], str):
        try:
            out["version"] = int(out["version"])
        except ValueError:
            pass
    for k in ("amount", "value"):
        if k in out and isinstance(out[k], str):
            v = out[k].strip()
            if v == "" or v.lower() in ("null", "none", "tbd"):
                out[k] = None
            else:
                try:
                    out[k] = float(v) if "." in v else int(v)
                except ValueError:
                    pass
    return out


def _extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as e:  # pragma: no cover
        raise RuntimeError("pypdf is required to parse PDF intake") from e
    reader = PdfReader(str(path))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


def intake(seed_dir: Path) -> list[RawRecord]:
    records: list[RawRecord] = []

    feed = seed_dir / "feed.json"
    if feed.exists():
        raw = feed.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        for item in data:
            fields = _coerce({k.lower(): v for k, v in item.items()})
            records.append(RawRecord(
                fields=fields, source_format="feed",
                source_version_hash=sha(item), source_path=str(feed)))

    inbox = seed_dir / "inbox"
    if inbox.exists():
        for p in sorted(inbox.iterdir()):
            if p.suffix.lower() == ".eml":
                raw = p.read_bytes()
                msg = email.message_from_bytes(raw)
                body = _get_email_body(msg)
                fields = _coerce(_parse_kv_text(body))
                records.append(RawRecord(
                    fields=fields, source_format="eml",
                    source_version_hash=sha_bytes(raw), source_path=str(p)))
            elif p.suffix.lower() == ".pdf":
                raw = p.read_bytes()
                text = _extract_pdf_text(p)
                fields = _coerce(_parse_kv_text(text))
                records.append(RawRecord(
                    fields=fields, source_format="pdf",
                    source_version_hash=sha_bytes(raw), source_path=str(p)))
    return records


def _get_email_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode("utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        return msg.get_payload()
    return payload.decode("utf-8", "replace")
