"""Approval chain as an explicit state machine (not a boolean).

States: draft -> in_review -> changes_requested -> approved -> delivered
                                         \-> blocked

Human approvals are workflow states with an actor + timestamp + before/after.
Delivery is refused server-side for any item not in `approved` (and, when the
CASE_ID amendment applies, without the required extra approver role).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

# In the RCM domain the maker-checker chain is:
#   coder review -> billing specialist -> billing manager -> compliance.
RCM_APPROVAL_CHAIN = ["coder", "billing_specialist", "billing_manager", "compliance"]

ALLOWED = {
    "draft": {"in_review", "blocked"},
    "in_review": {"in_review", "approved", "changes_requested", "blocked"},
    "changes_requested": {"in_review", "blocked"},
    "approved": {"delivered", "blocked"},
    "delivered": set(),
    "blocked": set(),
}


class ApprovalError(Exception):
    pass


@dataclass
class ApprovalState:
    record_id: str
    state: str = "draft"
    trail: list = field(default_factory=list)
    approvers: set = field(default_factory=set)  # roles that recorded an approval

    def transition(self, to_state: str, actor: str, ts: str,
                   reason: Optional[str] = None) -> None:
        if to_state not in ALLOWED.get(self.state, set()):
            raise ApprovalError(
                f"illegal transition {self.state} -> {to_state} for {self.record_id}")
        self.trail.append({"state": to_state, "actor": actor, "ts": ts, "reason": reason})
        self.state = to_state

    def record_approval(self, role: str) -> None:
        self.approvers.add(role)

    def can_deliver(self, amount: Optional[float], amendment_role: str,
                    amendment_threshold: float) -> tuple[bool, Optional[str]]:
        """Server-side delivery gate. Returns (allowed, refusal_reason)."""
        if self.state != "approved":
            return False, f"not approved (state={self.state})"
        # Amendment: high-value records need the extra approver role R.
        if amount is not None and amount >= amendment_threshold:
            if amendment_role not in self.approvers:
                return False, (
                    f"amendment requires approval by role '{amendment_role}' for "
                    f"amount {amount} >= {amendment_threshold}")
        return True, None
