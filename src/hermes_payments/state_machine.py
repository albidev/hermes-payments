"""
Hermes Payments — payment state machine (v0).

Finite automaton for a single PaymentIntent lifecycle.

    DRAFT → SUBMITTED → QUOTED → PREPARED → APPROVED → EXECUTING → SETTLED
                                 ↓           ↓          ↓
                             CANCELLED   REJECTED    EXPIRED / FAILED
                   ↓
               REJECTED / EXPIRED / FAILED

Every transition is deterministic given an input event.  The state
machine is the single source of truth for whether an adapter call
is permitted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Set, Tuple

from .models import PaymentState


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------
# Each entry: (current_state, trigger) → next_state.
# Triggers are string labels (event kinds), not wire types.
# ---------------------------------------------------------------------------

TRANSITIONS: Dict[Tuple[PaymentState, str], PaymentState] = {
    # Happy path
    (PaymentState.DRAFT, "submit"): PaymentState.SUBMITTED,
    (PaymentState.SUBMITTED, "quote_received"): PaymentState.QUOTED,
    (PaymentState.QUOTED, "prepared"): PaymentState.PREPARED,
    (PaymentState.PREPARED, "approved"): PaymentState.APPROVED,
    (PaymentState.APPROVED, "executing"): PaymentState.EXECUTING,
    (PaymentState.EXECUTING, "settled"): PaymentState.SETTLED,

    # Early exits (sender-initiated)
    (PaymentState.DRAFT, "cancel"): PaymentState.CANCELLED,
    (PaymentState.SUBMITTED, "cancel"): PaymentState.CANCELLED,
    (PaymentState.QUOTED, "cancel"): PaymentState.CANCELLED,
    (PaymentState.PREPARED, "cancel"): PaymentState.CANCELLED,

    # Rejection
    (PaymentState.SUBMITTED, "rejected"): PaymentState.REJECTED,
    (PaymentState.QUOTED, "rejected"): PaymentState.REJECTED,
    (PaymentState.PREPARED, "rejected"): PaymentState.REJECTED,
    (PaymentState.APPROVED, "rejected"): PaymentState.REJECTED,

    # Expiry (any non-terminal state → EXPIRED)
    (PaymentState.SUBMITTED, "expired"): PaymentState.EXPIRED,
    (PaymentState.QUOTED, "expired"): PaymentState.EXPIRED,
    (PaymentState.PREPARED, "expired"): PaymentState.EXPIRED,
    (PaymentState.APPROVED, "expired"): PaymentState.EXPIRED,
    (PaymentState.EXECUTING, "expired"): PaymentState.EXPIRED,

    # Adapter failure (any active state → FAILED)
    (PaymentState.QUOTED, "adapter_error"): PaymentState.FAILED,
    (PaymentState.PREPARED, "adapter_error"): PaymentState.FAILED,
    (PaymentState.APPROVED, "adapter_error"): PaymentState.FAILED,
    (PaymentState.EXECUTING, "adapter_error"): PaymentState.FAILED,
}


# ---------------------------------------------------------------------------
# Terminal states — no further transitions
# ---------------------------------------------------------------------------

TERMINAL_STATES: FrozenSet[PaymentState] = frozenset({
    PaymentState.SETTLED,
    PaymentState.FAILED,
    PaymentState.REJECTED,
    PaymentState.EXPIRED,
    PaymentState.CANCELLED,
})


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

INVARIANT_DESCRIPTIONS = {
    "no_double_settle": "Once SETTLED, no further transition is possible.",
    "no_replay_approval": "Each (intent_id, quote_id, prepared_hash) tuple may be approved at most once.",
    "expiry_guard": "Any non-terminal state transitions to EXPIRED when expires_at is reached.",
    "adapter_boundary": "Adapter errors are the only non-human transition into FAILED from EXECUTING.",
    "approval_requires_prepared": "APPROVED can only be reached from PREPARED (prepare must have run).",
}


# ---------------------------------------------------------------------------
# State machine API
# ---------------------------------------------------------------------------


@dataclass
class TransitionResult:
    ok: bool
    new_state: PaymentState
    error: Optional[str] = None  # noqa: F821 (forward ref OK in dataclass)


def can_transition(current: PaymentState, trigger: str) -> bool:
    """Check if a transition is valid without mutating state."""
    return (current, trigger) in TRANSITIONS


def transition(current: PaymentState, trigger: str) -> TransitionResult:
    """Execute a state transition.  Returns the result without side effects."""
    if current in TERMINAL_STATES:
        return TransitionResult(
            ok=False,
            new_state=current,
            error=f"state {current.value} is terminal; no transitions allowed",
        )

    next_state = TRANSITIONS.get((current, trigger))
    if next_state is None:
        return TransitionResult(
            ok=False,
            new_state=current,
            error=f"no transition from {current.value} on trigger '{trigger}'",
        )

    return TransitionResult(ok=True, new_state=next_state)


def reachable_states() -> Set[PaymentState]:
    """All states reachable from DRAFT via the happy path."""
    visited: Set[PaymentState] = set()
    frontier = {PaymentState.DRAFT}
    while frontier:
        s = frontier.pop()
        if s in visited:
            continue
        visited.add(s)
        for (cur, _trig), nxt in TRANSITIONS.items():
            if cur == s:
                frontier.add(nxt)
    return visited
