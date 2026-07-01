"""LLM client: deterministic replay (default) or real API call.

REPLAY_LLM=true (graded offline path): ONLY the model call is replaced. Each call
builds a canonical `request` dict; we look up a committed transcript whose request
canon-hash matches and return its recorded response. Every transcript is tagged with
the agent that made the call, so the gate can prove which agent was load-bearing.

REPLAY_LLM=false: calls an OpenAI-compatible /chat/completions endpoint using
LLM_API_KEY / LLM_MODEL / LLM_BASE_URL and records a transcript in the same shape.

Only genuinely-reasoning steps use this (the Worker's coding/summarisation draft).
Deterministic validation is done in code, never via an LLM.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .util import canon, sha, hexof

# Token pricing (USD per 1M tokens). Two tiers so the router can trade cost for power.
PRICING = {
    "gpt-4o-mini":      {"in": 0.15, "out": 0.60},
    "gpt-4o":           {"in": 2.50, "out": 10.00},
    "claude-3-5-haiku": {"in": 0.80, "out": 4.00},
    "claude-3-5-sonnet": {"in": 3.00, "out": 15.00},
    "gemini-1.5-flash": {"in": 0.075, "out": 0.30},
}
DEFAULT_PRICE = {"in": 0.50, "out": 1.50}


def price_for(model: str) -> dict:
    return PRICING.get(model, DEFAULT_PRICE)


def cost_of(model: str, tokens_in: int, tokens_out: int) -> float:
    p = price_for(model)
    return round((tokens_in * p["in"] + tokens_out * p["out"]) / 1_000_000, 8)


@dataclass
class LLMResult:
    response: Any
    model: str
    prompt_version: str
    agent: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: float
    transcript_hash: str
    delivered_fields_hash: Optional[str] = None


class TranscriptStore:
    """Index of committed transcripts, keyed by the canon-hash of their request."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_request: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for tf in sorted(self.path.glob("*.json")):
            try:
                t = json.loads(tf.read_text(encoding="utf-8"))
            except Exception:
                continue
            req = t.get("request")
            if req is not None:
                self._by_request[hexof(sha(req))] = t

    def lookup(self, request: dict) -> Optional[dict]:
        return self._by_request.get(hexof(sha(request)))


class ReplayMiss(Exception):
    pass


class LLMClient:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.replay = cfg.replay_llm
        self.store = TranscriptStore(cfg.transcripts_dir)
        self._latency_seq = 0

    def _fixed_latency(self) -> float:
        # Deterministic pseudo-latency so replay runs are reproducible.
        self._latency_seq += 1
        return 40.0 + (self._latency_seq % 7) * 11.0

    def call(self, agent: str, prompt_version: str, request: dict,
             model: str) -> LLMResult:
        """Make (or replay) one model call. `request` must be deterministic."""
        if self.replay:
            return self._replay(agent, prompt_version, request, model)
        return self._real(agent, prompt_version, request, model)

    # ---- replay -------------------------------------------------------------
    def _replay(self, agent, prompt_version, request, model) -> LLMResult:
        t = self.store.lookup(request)
        if t is None:
            raise ReplayMiss(
                f"no committed transcript for {agent} call on "
                f"{request.get('record_id')} (attempt {request.get('attempt')})")
        resp = t["response"]
        tin = int(t.get("tokens_in", 400))
        tout = int(t.get("tokens_out", 180))
        used_model = t.get("model", model)
        return LLMResult(
            response=resp,
            model=used_model,
            prompt_version=t.get("prompt_version", prompt_version),
            agent=t.get("agent", agent),
            tokens_in=tin,
            tokens_out=tout,
            cost_usd=float(t.get("cost_usd", cost_of(used_model, tin, tout))),
            latency_ms=float(t.get("latency_ms", self._fixed_latency())),
            transcript_hash=t.get("response_hash", sha(resp)),
            delivered_fields_hash=t.get("delivered_fields_hash"),
        )

    # ---- real ---------------------------------------------------------------
    def _real(self, agent, prompt_version, request, model) -> LLMResult:
        import urllib.request

        base = self.cfg.llm_base_url or "https://api.openai.com/v1"
        url = base.rstrip("/") + "/chat/completions"
        system, user = build_prompt(agent, prompt_version, request)
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {self.cfg.llm_api_key}",
                     "Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        latency = (time.time() - t0) * 1000.0
        content = data["choices"][0]["message"]["content"]
        resp = _parse_json_lenient(content)
        usage = data.get("usage", {})
        tin = int(usage.get("prompt_tokens", 400))
        tout = int(usage.get("completion_tokens", 180))
        result = LLMResult(
            response=resp, model=model, prompt_version=prompt_version, agent=agent,
            tokens_in=tin, tokens_out=tout, cost_usd=cost_of(model, tin, tout),
            latency_ms=latency, transcript_hash=sha(resp))
        # Record the transcript so a real run is replayable afterwards.
        _record_transcript(self.cfg.transcripts_dir, request, result)
        return result


def _parse_json_lenient(content: str) -> Any:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    try:
        return json.loads(content)
    except Exception:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def _record_transcript(tdir: Path, request: dict, result: LLMResult) -> None:
    tdir.mkdir(parents=True, exist_ok=True)
    doc = {
        "agent": result.agent,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "request": request,
        "response": result.response,
        "response_hash": result.transcript_hash,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
    }
    if result.delivered_fields_hash:
        doc["delivered_fields_hash"] = result.delivered_fields_hash
    (tdir / f"{hexof(result.transcript_hash)}.json").write_text(
        json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")


def build_prompt(agent: str, prompt_version: str, request: dict) -> tuple[str, str]:
    """Prompt used for the REAL path. The Worker is the only load-bearing LLM."""
    system = (
        "You are a medical-coding assistant in a Revenue Cycle Management pipeline. "
        "Given a normalized work-request/encounter, produce a structured claim draft. "
        "Only use facts present in the input. If the request is genuinely ambiguous or "
        "underspecified, abstain. Respond with a single JSON object with keys: "
        "cpt_codes (array of strings), icd10_codes (array of strings), "
        "normalized_amount (number or null, copied exactly from input amount), "
        "category (string, copied from input), claim_summary (short string), "
        "confidence (0..1), abstain (boolean). Never invent amounts or categories."
    )
    user = json.dumps(request.get("encounter", request), ensure_ascii=False)
    return system, user
