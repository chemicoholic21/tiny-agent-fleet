"""Base agent contract. Every agent declares a typed input/output and a `can_call`
list — the only agents it is permitted to invoke. This is what makes the fleet a
real topology rather than one god-function with three prompts.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class AgentSpec:
    name: str
    role: str            # orchestrator | worker | verifier | router | operator
    models: list
    prompt_version: str
    can_call: list       # names of agents this agent may call
    input_type: str
    output_type: str

    def roster_entry(self) -> dict:
        return {
            "name": self.name,
            "role": self.role,
            "models": self.models,
            "prompt_version": self.prompt_version,
            "can_call": self.can_call,
            "input_type": self.input_type,
            "output_type": self.output_type,
        }


class Agent:
    spec: AgentSpec

    def __init__(self, bus, audit):
        self.bus = bus
        self.audit = audit

    def log(self, action: str, record_id=None, **detail):
        self.audit.append(self.spec.name, action, record_id=record_id, detail=detail)
