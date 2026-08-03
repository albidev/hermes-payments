"""Local human approval boundary for payment execution.

The model may prepare a payment, but it cannot authorize settlement.  In a
Hermes gateway session this module uses the gateway's blocking approval queue;
in a direct interactive CLI it uses Hermes' one-shot prompt.  Non-interactive
contexts fail closed.
"""
from __future__ import annotations

import hashlib
import sys
from typing import Any


def _gateway_callback(approval_module: Any, session_key: str) -> Any:
    """Return Hermes' registered gateway notifier without replacing it."""
    lock = getattr(approval_module, "_lock", None)
    callbacks = getattr(approval_module, "_gateway_notify_cbs", None)
    if lock is None or not isinstance(callbacks, dict):
        return None
    with lock:
        return callbacks.get(session_key)


def request_human_approval(
    *,
    intent_id: str,
    quote_id: str,
    prepared_hash: str,
    amount_sat: int,
    fee_sat: int,
    recipient_pubkey: str,
    purpose: str,
) -> bool:
    """Request one local human approval bound to the exact payment triple.

    Only the ``once`` decision is accepted.  Session-wide and permanent
    approvals are deliberately not valid for a payment.
    """
    triple = f"{intent_id}:{quote_id}:{prepared_hash}"
    pattern_key = "hermes-payments:" + hashlib.sha256(triple.encode()).hexdigest()
    command = (
        "hermes-payments execute "
        f"--intent-id {intent_id} --quote-id {quote_id} "
        f"--prepared-hash {prepared_hash}"
    )
    description = (
        f"Approve ONE payment: {amount_sat} sat + {fee_sat} sat fee to "
        f"{recipient_pubkey}. Purpose: {purpose}. "
        f"Binding: {intent_id} / {quote_id} / {prepared_hash}."
    )

    try:
        from tools import approval as approval_module
    except ImportError:
        return False

    session_key = approval_module.get_current_session_key(default="")
    if session_key:
        callback = _gateway_callback(approval_module, session_key)
        if callback is not None:
            result = approval_module._await_gateway_decision(
                session_key,
                callback,
                {
                    "command": command,
                    "description": description,
                    "pattern_key": pattern_key,
                    "pattern_keys": [pattern_key],
                    "allow_permanent": False,
                    "allow_session": False,
                },
                surface="gateway",
            )
            return bool(result.get("resolved") and result.get("choice") == "once")

    # A gateway thread without a notifier must not fall back to stdin: that
    # would hang the worker and there would be no visible approval request.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False

    choice = approval_module.prompt_dangerous_approval(
        command,
        description,
        allow_permanent=False,
    )
    return choice == "once"


__all__ = ["request_human_approval"]
