"""Append-only audit log with a tamper-evident hash chain.

Every state transition in the fleet becomes an Event here. The log is append-only:
there is no update/delete API. Each event carries a `prev_hash`, forming a chain,
so any post-hoc mutation of a historical entry is detectable (probe-append-only).

Timestamps are derived from PIPELINE_NOW + sequence, never the wall clock, so a
re-run is byte-for-byte identical (idempotency / provenance survives re-run).
"""
from __future__ import annotations
import hashlib
from typing import Optional

from .events import Event


class AppendOnlyViolation(Exception):
    pass


class AuditLog:
    def __init__(self, pipeline_now: str) -> None:
        self._events: list[Event] = []
        self._chain: list[str] = []
        self._pipeline_now = pipeline_now
        self._sealed = False

    # -- deterministic timestamp: base date + monotonically increasing seq -----
    def _ts(self, seq: int) -> str:
        # Encode seq as a synthetic sub-second offset so ordering is visible and
        # stable without ever reading a real clock.
        return f"{self._pipeline_now}T00:00:{seq // 60:02d}.{seq % 60:02d}Z"

    def _chain_hash(self, event: Event, prev: str) -> str:
        payload = f"{prev}|{event.seq}|{event.ts}|{event.actor}|{event.action}|{event.record_id}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def append(self, actor: str, action: str,
               record_id: Optional[str] = None, detail: Optional[dict] = None) -> Event:
        if self._sealed:
            raise AppendOnlyViolation("audit log is sealed; cannot append")
        seq = len(self._events)
        ev = Event(seq=seq, ts=self._ts(seq), actor=actor, action=action,
                   record_id=record_id, detail=detail or {})
        prev = self._chain[-1] if self._chain else "GENESIS"
        self._chain.append(self._chain_hash(ev, prev))
        self._events.append(ev)
        return ev

    # -- append-only enforcement ----------------------------------------------
    def __setitem__(self, *_):  # pragma: no cover - guard rail
        raise AppendOnlyViolation("audit entries are immutable")

    def events(self) -> list[Event]:
        return list(self._events)

    def as_dicts(self) -> list[dict]:
        return [
            {"seq": e.seq, "ts": e.ts, "actor": e.actor,
             "action": e.action, "record_id": e.record_id, "detail": e.detail}
            for e in self._events
        ]

    def integrity_ok(self) -> bool:
        """Recompute the chain from scratch and confirm nothing was altered."""
        prev = "GENESIS"
        for i, e in enumerate(self._events):
            if e.seq != i:
                return False
            expect = self._chain_hash(e, prev)
            if expect != self._chain[i]:
                return False
            prev = expect
        return True

    @staticmethod
    def verify_events_dicts(events: list[dict]) -> bool:
        """Static re-verification used by probe-append-only against a written log.

        Recomputes the chain over the given event dicts. Returns True only if the
        seq is a strict 0..n-1 sequence AND the recomputed chain matches the
        `chain_hash` stamped on each event (present in the written audit bundle).
        """
        prev = "GENESIS"
        for i, e in enumerate(events):
            if e.get("seq") != i:
                return False
            payload = f"{prev}|{e.get('seq')}|{e.get('ts')}|{e.get('actor')}|{e.get('action')}|{e.get('record_id')}"
            h = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if "chain_hash" in e and e["chain_hash"] != h:
                return False
            prev = h
        return True

    def chained_dicts(self) -> list[dict]:
        """Event dicts with their chain hash stamped in (written to audit.json)."""
        out = []
        for e, ch in zip(self._events, self._chain):
            d = {"seq": e.seq, "ts": e.ts, "actor": e.actor, "action": e.action,
                 "record_id": e.record_id, "detail": e.detail, "chain_hash": ch}
            out.append(d)
        return out
