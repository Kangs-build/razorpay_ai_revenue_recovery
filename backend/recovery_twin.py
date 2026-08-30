"""
Incident Recovery Twin — Razorpay AI Revenue Recovery (Track 3)

Compares recovery strategies for a detected payment incident using a
transparent, rule-based Suitability Score (0–100). No ML, no LLM,
no real payment actions — just comparison and recommendation.

Usage:
    from recovery_twin import analyze_incident

    incident = {
        "bank": "BANK_X",
        "payment_method": "UPI",
        "error_reason": "technical_error",
        "total_payments": 10,
        "failed_payments": 7,
        "failure_rate": 70.0,
        "revenue_at_risk": 19750.00,
    }

    results = analyze_incident(incident)
    print_twin_report(incident, results)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ---------- CONSTANTS ----------

STRATEGIES = [
    "WAIT_AND_RECHECK",
    "RETRY_LATER",
    "SUGGEST_ALTERNATE_METHOD",
    "SEND_PAYMENT_LINK",
    "CUSTOMER_RETRY",
]


# ---------- DATA MODEL ----------

@dataclass
class RecoveryOption:
    """One recovery strategy with its score and explanation."""

    strategy: str
    score: int          # 0–100
    explanation: str
    customer_friction: str    # "Low" / "Medium" / "High"
    duplicate_risk: str       # "Low" / "Medium" / "High"


# ---------- STEP 1: UNDERSTAND THE INCIDENT ----------

def _classify_incident(incident: dict) -> dict:
    """Extract simple yes/no facts from the incident dict.

    This makes the scoring logic easier to read:
        facts["is_temporary"]  → True if the error looks transient
        facts["is_user_error"] → True if caused by user input
        facts["is_balance_issue"] → True if customer lacks funds
    """
    reason = incident.get("error_reason", "").lower()
    failure_rate = incident.get("failure_rate", 0)

    return {
        "is_temporary": reason in ("technical_error", "timeout_error"),
        "is_user_error": reason in ("incorrect_otp", "invalid_details"),
        "is_balance_issue": reason == "insufficient_balance",
        "high_failure_rate": failure_rate >= 80,
        "failure_rate": failure_rate,
    }


# ---------- STEP 2: SCORE EACH STRATEGY ----------

def _score_wait_and_recheck(facts: dict) -> tuple[int, str]:
    """Score WAIT_AND_RECHECK: wait briefly and check if payments resolved.

    Best when: temporary bank-side issue, high failure rate.
    Worst when: user error (waiting won't fix wrong OTP) or balance issue.
    """
    score = 50

    if facts["is_temporary"]:
        score += 30  # bank issue likely to resolve on its own
    if facts["high_failure_rate"]:
        score += 10  # many failures = systemic → waiting is safer
    if facts["is_user_error"]:
        score -= 30  # user won't magically enter correct OTP by waiting
    if facts["is_balance_issue"]:
        score -= 20  # balance won't increase on its own

    explanation = (
        "Wait briefly and re-check the original payment status. "
        "If the issue is temporary (e.g., bank API down), the payment "
        "may resolve without any customer action."
    )

    if facts["is_temporary"]:
        explanation += (
            " This failure pattern suggests a temporary bank-side issue, "
            "making this the lowest-friction option."
        )
    elif facts["is_user_error"]:
        explanation += (
            " However, this error requires customer action, so waiting "
            "alone is unlikely to resolve it."
        )

    return score, explanation


def _score_retry_later(facts: dict) -> tuple[int, str]:
    """Score RETRY_LATER: automatically retry the same payment after a delay.

    Best when: temporary technical failure that may resolve on its own.
    Less useful when: balance issue or user input error.
    """
    score = 40

    if facts["is_temporary"]:
        score += 20  # technical issue may resolve, retry could succeed
    if facts["is_user_error"]:
        score += 10  # customer may correct their input
    if facts["is_balance_issue"]:
        score -= 20  # retrying with same low balance → same failure
    if facts["high_failure_rate"]:
        score -= 10  # high failure rate means retry likely fails too

    explanation = (
        "Retry the same payment method after a short delay. "
        "This is useful for temporary or transient technical failures "
        "that may resolve on their own (e.g., bank API recovering)."
    )

    if facts["is_user_error"]:
        explanation += (
            " This error may also be a one-time mistake, making a retry "
            "reasonable after informing the customer."
        )
    elif facts["is_balance_issue"]:
        explanation += (
            " However, retrying with the same payment method when the "
            "customer has insufficient funds will likely fail again."
        )

    return score, explanation


def _score_suggest_alternate(facts: dict) -> tuple[int, str]:
    """Score SUGGEST_ALTERNATE_METHOD: offer a different payment method.

    Best when: balance issue (try a different source) or method-specific failure.
    Less useful when: all methods affected by same bank outage.
    """
    score = 55

    if facts["is_balance_issue"]:
        score += 25  # different method = different account/card
    if facts["is_user_error"]:
        score += 10  # OTP issue on UPI → try Card instead
    if facts["is_temporary"] and facts["high_failure_rate"]:
        score -= 10  # bank outage may affect all methods

    explanation = (
        "Suggest a different payment method to the customer. "
        "This is useful when the failure is method-specific and "
        "an alternative channel is available."
    )

    if facts["is_balance_issue"]:
        explanation += (
            " Since the issue is insufficient balance, a different "
            "payment method linked to another account may succeed."
        )
    elif facts["is_temporary"]:
        explanation += (
            " However, if the bank is experiencing a widespread outage, "
            "other methods through the same bank may also fail."
        )

    return score, explanation


def _score_send_payment_link(facts: dict) -> tuple[int, str]:
    """Score SEND_PAYMENT_LINK: send a fresh payment link to the customer.

    Best when: no other option works, or customer needs to restart.
    Less ideal when: quicker options exist.
    """
    score = 35

    if facts["is_balance_issue"]:
        score += 15  # gives customer time to arrange funds
    if facts["is_user_error"]:
        score += 10  # fresh start with correct details
    if facts["is_temporary"] and facts["high_failure_rate"]:
        score += 10  # if bank is down, a link lets customer retry when ready

    explanation = (
        "Send a fresh payment link to the customer's registered contact. "
        "This gives the customer full control to retry when ready, "
        "but requires them to take action."
    )

    if facts["is_balance_issue"]:
        explanation += (
            " This gives the customer time to arrange funds before "
            "attempting the payment again."
        )

    return score, explanation


def _score_customer_retry(facts: dict) -> tuple[int, str]:
    """Score CUSTOMER_RETRY: prompt the customer to retry with correct details.

    Best when: the failure is caused by incorrect customer input
    (e.g., wrong OTP/PIN) that the customer can correct themselves.
    Less useful when: the problem is on the bank side or a balance issue.
    """
    score = 40

    if facts["is_user_error"]:
        score += 35  # customer can fix this by entering correct OTP/PIN
    if facts["is_temporary"]:
        score -= 15  # bank issue — customer retry won't help
    if facts["is_balance_issue"]:
        score -= 10  # retrying with same low balance won't help
    # Note: high_failure_rate is NOT penalized here. A single OTP payment
    # naturally has 100% failure rate, which is expected, not systemic.

    explanation = (
        "The payment failed because the customer entered incorrect "
        "authentication details. Allow the customer to retry the same "
        "payment with the correct OTP/PIN before suggesting another "
        "payment method."
    )

    if facts["is_user_error"]:
        explanation += (
            " This is a customer-correctable error, making a direct retry "
            "the most appropriate next step."
        )
    elif facts["is_temporary"]:
        explanation += (
            " However, this failure appears to be a bank-side issue, so "
            "waiting for resolution may be more effective."
        )

    return score, explanation


# ---------- STEP 3: ANALYZE & RANK ----------

_STRATEGY_SCORERS = {
    "WAIT_AND_RECHECK": _score_wait_and_recheck,
    "RETRY_LATER": _score_retry_later,
    "SUGGEST_ALTERNATE_METHOD": _score_suggest_alternate,
    "SEND_PAYMENT_LINK": _score_send_payment_link,
    "CUSTOMER_RETRY": _score_customer_retry,
}

_FRICTION_MAP = {
    "WAIT_AND_RECHECK": "Low",
    "RETRY_LATER": "Low",
    "SUGGEST_ALTERNATE_METHOD": "Medium",
    "SEND_PAYMENT_LINK": "Medium",
    "CUSTOMER_RETRY": "Low",
}

_DUPLICATE_RISK_MAP = {
    "WAIT_AND_RECHECK": "Low",
    "RETRY_LATER": "Medium",
    "SUGGEST_ALTERNATE_METHOD": "Medium",
    "SEND_PAYMENT_LINK": "Medium",
    "CUSTOMER_RETRY": "Low",
}


def analyze_incident(incident: dict) -> list[RecoveryOption]:
    """Compare all recovery strategies and return them ranked by suitability.

    Args:
        incident: A dict from incident_detector.detect_incidents() containing
                  bank, payment_method, error_reason, failure_rate, etc.

    Returns:
        A list of RecoveryOption objects, sorted highest score first.
    """
    facts = _classify_incident(incident)

    options: list[RecoveryOption] = []
    for strategy in STRATEGIES:
        scorer = _STRATEGY_SCORERS[strategy]
        raw_score, explanation = scorer(facts)

        # Clamp score to 0–100
        score = max(0, min(100, raw_score))

        options.append(
            RecoveryOption(
                strategy=strategy,
                score=score,
                explanation=explanation,
                customer_friction=_FRICTION_MAP[strategy],
                duplicate_risk=_DUPLICATE_RISK_MAP[strategy],
            )
        )

    # Sort by score descending
    options.sort(key=lambda o: o.score, reverse=True)
    return options


# ---------- STEP 4: PRINT THE TWIN REPORT ----------

def _format_amount(amount: float) -> str:
    """Format an amount as Indian Rupees."""
    s = f"{amount:.2f}"
    integer_part, decimal_part = s.split(".")
    if len(integer_part) <= 3:
        return f"₹{integer_part}.{decimal_part}"
    last_three = integer_part[-3:]
    remaining = integer_part[:-3]
    groups = []
    while remaining:
        groups.append(remaining[-2:])
        remaining = remaining[:-2]
    formatted = ",".join(reversed(groups)) + "," + last_three
    return f"₹{formatted}.{decimal_part}"


def print_twin_report(incident: dict, options: list[RecoveryOption]) -> None:
    """Pretty-print the Recovery Twin comparison report."""
    print()
    print("=" * 60)
    print("  INCIDENT RECOVERY TWIN")
    print("=" * 60)
    print()
    print(f"  Incident:")
    print(f"  {incident['bank']} + {incident['payment_method']}"
          f" + {incident['error_reason']}")
    print(f"  Revenue At Risk: {_format_amount(incident['revenue_at_risk'])}")
    print()

    print("  RECOVERY OPTIONS")
    print("  " + "─" * 56)
    print()

    for i, opt in enumerate(options, start=1):
        print(f"  {i}. {opt.strategy}")
        print(f"     Score: {opt.score}/100")
        print(f"     Customer Friction: {opt.customer_friction}")
        print(f"     Duplicate Risk:    {opt.duplicate_risk}")
        print(f"     Reason: {opt.explanation}")
        print()

    best = options[0]
    print("  " + "─" * 56)
    print()
    print(f"  RECOMMENDED STRATEGY:")
    print(f"  {best.strategy}")
    print()
    print(f"  WHY:")
    print(f"  {best.explanation}")
    print()
    print("=" * 60)
    print()


# ---------- INTEGRATION: run detector then twin ----------

def run_twin_for_incident(incident: dict) -> list[RecoveryOption]:
    """Analyze one incident and print the full twin report.

    This is the simple integration point: pass an incident dict from
    incident_detector.detect_incidents() and get back ranked options.
    """
    options = analyze_incident(incident)
    print_twin_report(incident, options)
    return options


if __name__ == "__main__":
    # Standalone demo: detect an incident, then analyze it
    from incident_detector import (
        detect_incidents,
        group_payments,
        read_payments,
    )

    csv_path = os.path.join(
        os.path.dirname(__file__), os.pardir, "data", "sample_payments.csv"
    )

    payments = read_payments(csv_path)
    groups = group_payments(payments)

    # Use the default 5-minute window — matches incident_detector settings
    incidents = detect_incidents(groups)

    if incidents:
        print("\n🔍 Detected incident — passing to Recovery Twin...\n")
        run_twin_for_incident(incidents[0])
    else:
        print("\n✅ No incidents detected — nothing to analyze.\n")
