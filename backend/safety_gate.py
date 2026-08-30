"""
Safety Gate — Razorpay AI Revenue Recovery (Track 3)

Before any recovery action is allowed, the Safety Gate checks whether
that action is safe. Returns allowed/blocked with reasons.

No real payment actions — just validation rules.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# ---------- CONFIGURATION ----------

MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_COOLDOWN_MINUTES = 10
MAX_CUSTOMER_CONTACTS = 3

# Strategies that contact the customer directly
CUSTOMER_FACING_STRATEGIES = {"SUGGEST_ALTERNATE_METHOD", "SEND_PAYMENT_LINK"}


# ---------- SAFETY CHECKS ----------

def check_original_already_successful(payment: dict) -> str | None:
    """Block if the original payment already succeeded."""
    status = payment.get("status", "").lower()
    if status in ("captured", "successful"):
        return "Original payment is already successful."
    return None


def check_already_recovered(payment: dict) -> str | None:
    """Block if the payment has already been recovered."""
    if payment.get("recovered_already", False):
        return "Payment has already been recovered."
    return None


def check_max_recovery_attempts(payment: dict) -> str | None:
    """Block if max recovery attempts reached."""
    count = payment.get("recovery_attempt_count", 0)
    if count >= MAX_RECOVERY_ATTEMPTS:
        return "Maximum recovery attempts reached."
    return None


def check_cooldown_period(payment: dict) -> str | None:
    """Block if previous attempt was too recent."""
    last_attempt = payment.get("last_recovery_attempt_time")
    if last_attempt is None:
        return None

    now = datetime.now()
    # Accept both datetime objects and ISO strings
    if isinstance(last_attempt, str):
        last_attempt = datetime.fromisoformat(last_attempt)

    elapsed = now - last_attempt
    if elapsed < timedelta(minutes=RECOVERY_COOLDOWN_MINUTES):
        minutes_left = RECOVERY_COOLDOWN_MINUTES - elapsed.total_seconds() / 60
        return (
            f"Recovery blocked temporarily. The previous attempt happened "
            f"{int(RECOVERY_COOLDOWN_MINUTES - minutes_left)} minutes ago. "
            f"Wait {int(minutes_left) + 1} more minutes."
        )
    return None


def check_customer_contact_limit(
    payment: dict, strategy: str
) -> str | None:
    """Block customer-facing actions if contact limit reached."""
    if strategy not in CUSTOMER_FACING_STRATEGIES:
        return None  # non-customer-facing actions are not affected

    count = payment.get("customer_contact_count", 0)
    if count >= MAX_CUSTOMER_CONTACTS:
        return (
            f"Customer contact limit reached ({count}/{MAX_CUSTOMER_CONTACTS}). "
            f"Strategy '{strategy}' requires customer interaction."
        )
    return None


# ---------- MAIN GATE ----------

_CHECKS = [
    check_original_already_successful,
    check_already_recovered,
    check_max_recovery_attempts,
    check_cooldown_period,
]


def evaluate(
    payment: dict,
    strategy: str,
) -> dict:
    """Run all safety checks and return the result.

    Args:
        payment: A dict with payment info (status, recovered_already,
                 recovery_attempt_count, last_recovery_attempt_time,
                 customer_contact_count, etc.)
        strategy: The recovery strategy name (e.g. "WAIT_AND_RECHECK").

    Returns:
        {"allowed": bool, "strategy": str, "reasons": [str]}
    """
    reasons: list[str] = []

    # Run universal checks
    for check in _CHECKS:
        reason = check(payment)
        if reason:
            reasons.append(reason)

    # Run customer-contact check (strategy-aware)
    contact_reason = check_customer_contact_limit(payment, strategy)
    if contact_reason:
        reasons.append(contact_reason)

    allowed = len(reasons) == 0

    if allowed:
        reasons.append("All safety checks passed.")

    return {
        "allowed": allowed,
        "strategy": strategy,
        "reasons": reasons,
    }


# ---------- CONSOLE DEMO ----------

def _format_result(result: dict) -> str:
    """Format a safety gate result for display."""
    status = "✅ ALLOWED" if result["allowed"] else "🚫 BLOCKED"
    lines = [
        f"  Strategy: {result['strategy']}",
        f"  Status:   {status}",
        f"  Reasons:",
    ]
    for r in result["reasons"]:
        lines.append(f"    • {r}")
    return "\n".join(lines)


def run_demo() -> None:
    """Run the 5 demonstration scenarios."""
    now = datetime.now()

    scenarios = [
        {
            "name": "Scenario 1: Normal failed payment, no prior recovery",
            "payment": {
                "payment_id": "PAY001",
                "amount": 2500.0,
                "status": "failed",
                "payment_method": "UPI",
                "recovery_attempt_count": 0,
                "customer_contact_count": 0,
                "recovered_already": False,
                "last_recovery_attempt_time": None,
            },
            "strategy": "WAIT_AND_RECHECK",
            "expected": True,
        },
        {
            "name": "Scenario 2: Original payment is captured",
            "payment": {
                "payment_id": "PAY002",
                "amount": 1800.0,
                "status": "captured",
                "payment_method": "Card",
                "recovery_attempt_count": 0,
                "customer_contact_count": 0,
                "recovered_already": False,
                "last_recovery_attempt_time": None,
            },
            "strategy": "SEND_PAYMENT_LINK",
            "expected": False,
        },
        {
            "name": "Scenario 3: Max recovery attempts reached",
            "payment": {
                "payment_id": "PAY003",
                "amount": 3200.0,
                "status": "failed",
                "payment_method": "UPI",
                "recovery_attempt_count": 3,
                "customer_contact_count": 1,
                "recovered_already": False,
                "last_recovery_attempt_time": None,
            },
            "strategy": "RETRY_LATER",
            "expected": False,
        },
        {
            "name": "Scenario 4: Customer contact limit reached",
            "payment": {
                "payment_id": "PAY004",
                "amount": 950.0,
                "status": "failed",
                "payment_method": "NetBanking",
                "recovery_attempt_count": 2,
                "customer_contact_count": 3,
                "recovered_already": False,
                "last_recovery_attempt_time": None,
            },
            "strategy": "SEND_PAYMENT_LINK",
            "expected": False,
        },
        {
            "name": "Scenario 5: Cooldown — previous attempt 5 minutes ago",
            "payment": {
                "payment_id": "PAY005",
                "amount": 4100.0,
                "status": "failed",
                "payment_method": "UPI",
                "recovery_attempt_count": 1,
                "customer_contact_count": 1,
                "recovered_already": False,
                "last_recovery_attempt_time": now - timedelta(minutes=5),
            },
            "strategy": "RETRY_LATER",
            "expected": False,
        },
    ]

    print()
    print("=" * 60)
    print("  SAFETY GATE — DEMO")
    print("=" * 60)

    for s in scenarios:
        result = evaluate(s["payment"], s["strategy"])
        print()
        print(f"  {s['name']}")
        print("  " + "─" * 56)
        print(_format_result(result))
        # Quick assertion for demo correctness
        assert result["allowed"] == s["expected"], (
            f"Expected allowed={s['expected']}, got {result['allowed']}"
        )

    print()
    print("  All scenarios passed as expected.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    run_demo()
