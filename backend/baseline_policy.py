"""
Baseline Policy — Razorpay AI Revenue Recovery (Track 3)

A simple fixed recovery policy that does NOT use incident detection,
Recovery Twin, or strategy comparison. Used as a comparison baseline
to evaluate whether the Recovery Twin makes better decisions.

Rules:
  - incorrect_otp        → CUSTOMER_RETRY
  - insufficient_balance → SUGGEST_ALTERNATE_METHOD
  - other/technical      → RETRY_LATER

All chosen actions pass through the same Safety Gate used by the
Recovery Twin, making the comparison fair.

No ML, no LLM, no real payment actions.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from safety_gate import evaluate as safety_evaluate


# ---------- STRATEGY SELECTION ----------


def choose_strategy(payment: dict) -> str:
    """Choose a recovery strategy based on fixed rules.

    This is an INDEPENDENT baseline that does NOT use Recovery Twin
    scoring, incident detection, or strategy comparison.

    Args:
        payment: A simulated payment dict with error_reason.

    Returns:
        One of: CUSTOMER_RETRY, SUGGEST_ALTERNATE_METHOD, RETRY_LATER
    """
    reason = payment.get("error_reason", "none").lower()

    if reason == "incorrect_otp":
        return "CUSTOMER_RETRY"
    elif reason == "insufficient_balance":
        return "SUGGEST_ALTERNATE_METHOD"
    else:
        # All other failures (technical_error, timeout_error, etc.)
        return "RETRY_LATER"


def choose_and_check(payment: dict) -> dict:
    """Choose a strategy and run it through the Safety Gate.

    Returns:
        {
            "payment_id": str,
            "strategy": str,
            "gate_result": dict,  # from safety_gate.evaluate()
        }
    """
    strategy = choose_strategy(payment)
    gate_result = safety_evaluate(payment, strategy)

    return {
        "payment_id": payment.get("payment_id", "unknown"),
        "strategy": strategy,
        "gate_result": gate_result,
    }


def run_baseline(payments: list[dict]) -> list[dict]:
    """Run the baseline policy on a batch of payments.

    For each failed payment, chooses a strategy and checks the Safety Gate.

    Returns:
        A list of result dicts, one per failed payment.
    """
    results = []
    for payment in payments:
        if payment.get("status") != "failed":
            continue
        results.append(choose_and_check(payment))
    return results
