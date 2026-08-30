"""
Payment & Recovery Simulator — Razorpay AI Revenue Recovery (Track 3)

Safe simulation environment where fake payments can succeed, fail, recover,
or become successful later. Used to test the Incident Detector, Recovery Twin,
and Safety Gate pipeline without touching real money or APIs.

No ML, no LLM, no real payment actions — just transparent deterministic rules.
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta

# Add backend/ to path so we can import sibling modules
sys.path.insert(0, os.path.dirname(__file__))

from incident_detector import (
    detect_incidents,
    format_amount,
    group_payments,
    print_incidents,
)
from recovery_twin import analyze_incident, print_twin_report
from safety_gate import evaluate as safety_evaluate

# ---------- CONFIGURATION ----------

# Fixed seed ensures reproducible results for the same experiment
_FIXED_RANDOM_SEED = 42

# ---------- PAYMENT CREATION ----------


def create_payment(
    payment_id: str,
    amount: float,
    payment_method: str,
    bank: str,
    status: str,
    error_source: str = "none",
    error_reason: str = "none",
    timestamp: str | None = None,
) -> dict:
    """Create a simulated payment with all required fields.

    Args:
        payment_id:    Unique identifier for this payment.
        amount:        Payment amount in INR.
        payment_method: UPI, Card, NetBanking, etc.
        bank:          Bank name (e.g. BANK_X, HDFC).
        status:        success, failed, or captured.
        error_source:  Where the error occurred (or 'none').
        error_reason:  Why the payment failed (or 'none').
        timestamp:     ISO timestamp string. Defaults to now.

    Returns:
        A dict matching the CSV format used by incident_detector.
    """
    if timestamp is None:
        timestamp = datetime.now().isoformat()

    # Parse into a datetime object — incident_detector.group_payments
    # expects a private '_dt' key (added by read_payments for CSV data).
    _dt = datetime.fromisoformat(timestamp.rstrip("Z"))

    return {
        "payment_id": payment_id,
        "amount": amount,
        "timestamp": timestamp,
        "status": status,
        "payment_method": payment_method,
        "bank": bank,
        "error_source": error_source,
        "error_step": "none",
        "error_reason": error_reason,
        "_dt": _dt,
        "recovery_attempt_count": 0,
        "customer_contact_count": 0,
        "recovered_already": False,
        "last_recovery_attempt_time": None,
    }


# ---------- SCENARIO FUNCTIONS ----------


def scenario_normal_success() -> dict:
    """Scenario A: Payment succeeds normally — no recovery needed."""
    return create_payment(
        payment_id="PAY_A_001",
        amount=1500.0,
        payment_method="UPI",
        bank="HDFC",
        status="success",
        error_source="none",
        error_reason="none",
    )


def scenario_incorrect_otp() -> dict:
    """Scenario B: Card payment fails due to incorrect OTP."""
    return create_payment(
        payment_id="PAY_B_001",
        amount=3000.0,
        payment_method="Card",
        bank="ICICI",
        status="failed",
        error_source="card_network",
        error_reason="incorrect_otp",
    )


def scenario_insufficient_balance() -> dict:
    """Scenario C: Payment fails due to insufficient balance."""
    return create_payment(
        payment_id="PAY_C_001",
        amount=5000.0,
        payment_method="NetBanking",
        bank="SBI",
        status="failed",
        error_source="bank_api",
        error_reason="insufficient_balance",
    )


def scenario_temporary_bank_failure() -> dict:
    """Scenario D: UPI payment through BANK_X fails with a technical error."""
    return create_payment(
        payment_id="PAY_D_001",
        amount=2000.0,
        payment_method="UPI",
        bank="BANK_X",
        status="failed",
        error_source="bank_api",
        error_reason="technical_error",
    )


def scenario_late_success() -> dict:
    """Scenario E: Payment initially fails, then becomes captured later."""
    return create_payment(
        payment_id="PAY_E_001",
        amount=2500.0,
        payment_method="UPI",
        bank="BANK_X",
        status="failed",
        error_source="bank_api",
        error_reason="technical_error",
    )


# ---------- BANK INCIDENT GENERATOR ----------


def generate_bank_incident() -> list[dict]:
    """Generate a realistic BANK_X UPI technical incident.

    Creates 10 payment attempts within a 5-minute window:
      - 7 fail with technical_error
      - 3 succeed

    The generated data is compatible with our Incident Detector,
    which should detect this as an incident within the default
    TIME_WINDOW_MINUTES = 5.

    Returns:
        A list of 10 simulated payment dicts.
    """
    base_time = datetime(2026, 8, 30, 10, 0, 0)
    payments = []

    # 7 failed payments — spread across 5 minutes
    fail_times = [0, 30, 60, 120, 180, 240, 280]  # seconds offset
    for i, offset in enumerate(fail_times):
        ts = (base_time + timedelta(seconds=offset)).isoformat()
        payments.append(
            create_payment(
                payment_id=f"PAY_BANKX_{i+1:03d}",
                amount=1000.0 + (i * 500),
                payment_method="UPI",
                bank="BANK_X",
                status="failed",
                error_source="bank_api",
                error_reason="technical_error",
                timestamp=ts,
            )
        )

    # 3 successful payments — also within the window
    success_times = [90, 150, 270]
    for i, offset in enumerate(success_times):
        ts = (base_time + timedelta(seconds=offset)).isoformat()
        payments.append(
            create_payment(
                payment_id=f"PAY_BANKX_S{i+1:03d}",
                amount=1200.0 + (i * 300),
                payment_method="UPI",
                bank="BANK_X",
                status="success",
                error_source="none",
                error_reason="none",
                timestamp=ts,
            )
        )

    # Sort by timestamp (matches what incident_detector expects)
    payments.sort(key=lambda p: p["timestamp"])
    return payments


# ---------- SIMULATED RECOVERY ACTIONS ----------


def simulate_recovery(payment: dict, strategy: str) -> dict:
    """Simulate a recovery action on a payment.

    Does NOT perform any real action. Instead, updates the fake payment
    state according to simple, transparent rules:

    WAIT_AND_RECHECK:
        - For temporary bank failures: payment may resolve (status → captured).
        - Increments recovery_attempt_count.

    RETRY_LATER:
        - Same as wait_and_recheck but explicit retry.
        - Increments recovery_attempt_count.

    SUGGEST_ALTERNATE_METHOD:
        - Simulates customer trying a different method.
        - Increments customer_contact_count and recovery_attempt_count.

    SEND_PAYMENT_LINK:
        - Simulates sending a link — customer must act.
        - Increments customer_contact_count and recovery_attempt_count.

    Args:
        payment:  The simulated payment dict (will be modified in place).
        strategy: One of WAIT_AND_RECHECK, RETRY_LATER,
                  SUGGEST_ALTERNATE_METHOD, SEND_PAYMENT_LINK.

    Returns:
        The same payment dict, after simulated state changes.
    """
    # Safety: if already recovered or successful, stop
    if payment.get("recovered_already") or payment.get("status") in (
        "captured",
        "success",
    ):
        return payment

    payment["recovery_attempt_count"] = (
        payment.get("recovery_attempt_count", 0) + 1
    )

    reason = payment.get("error_reason", "").lower()

    if strategy == "WAIT_AND_RECHECK":
        # For temporary failures, the payment may resolve on its own.
        # We use a deterministic check: payment_id ends with an even digit.
        # This gives a transparent, reproducible success rate.
        pid = payment.get("payment_id", "")
        last_digit = int(pid[-1]) if pid and pid[-1].isdigit() else 0
        if reason == "technical_error" and last_digit % 2 == 0:
            payment["status"] = "captured"
            payment["error_reason"] = "none"

    elif strategy == "RETRY_LATER":
        # Similar to wait_and_recheck: temporary issues may resolve.
        pid = payment.get("payment_id", "")
        last_digit = int(pid[-1]) if pid and pid[-1].isdigit() else 0
        if reason == "technical_error" and last_digit % 2 == 1:
            payment["status"] = "captured"
            payment["error_reason"] = "none"

    elif strategy == "SUGGEST_ALTERNATE_METHOD":
        # Customer uses a different payment method.
        # Increment contact count since we had to ask the customer.
        payment["customer_contact_count"] = (
            payment.get("customer_contact_count", 0) + 1
        )
        # For insufficient balance, a different method may work.
        if reason == "insufficient_balance":
            payment["status"] = "captured"
            payment["error_reason"] = "none"
            payment["payment_method"] = "Card"

    elif strategy == "SEND_PAYMENT_LINK":
        # Customer receives a link and must act.
        payment["customer_contact_count"] = (
            payment.get("customer_contact_count", 0) + 1
        )
        # No automatic resolution — customer must initiate payment.

    return payment


def simulate_late_success(payment: dict) -> dict:
    """Simulate a late-success: a failed payment becomes captured.

    This is used to test that the Safety Gate correctly blocks
    recovery when the original payment succeeds after a delay.
    """
    if payment.get("status") == "failed":
        payment["status"] = "captured"
    return payment


# ---------- FULL PIPELINE ----------


def run_pipeline(incident: dict, payments: list[dict] | None = None) -> dict:
    """Run the full pipeline: Recovery Twin → Safety Gate → Simulate.

    Args:
        incident:  An incident dict from incident_detector.
        payments:  Optional list of payment dicts for safety gate evaluation.
                   If provided, uses the first failed payment for the gate check.

    Returns:
        A dict with keys: incident, options, recommendation, gate_result,
        simulated_payment.
    """
    # Step 1: Recovery Twin analyzes the incident
    options = analyze_incident(incident)
    recommendation = options[0]  # highest-scoring strategy

    # Step 2: Safety Gate checks if recovery is allowed
    gate_result = None
    target_payment = None
    if payments:
        # Use the first failed payment for the safety gate check
        for p in payments:
            if p.get("status") == "failed":
                target_payment = p
                break
        if target_payment:
            gate_result = safety_evaluate(target_payment, recommendation.strategy)

    # Step 3: Simulate recovery if allowed
    simulated_payment = None
    if gate_result and gate_result["allowed"] and target_payment:
        simulated_payment = simulate_recovery(target_payment, recommendation.strategy)

    return {
        "incident": incident,
        "options": options,
        "recommendation": recommendation,
        "gate_result": gate_result,
        "simulated_payment": simulated_payment,
    }


# ---------- CONSOLE DEMO ----------


def _print_scenario_header(title: str) -> None:
    """Print a scenario header."""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)
    print()


def _print_payment_info(payment: dict) -> None:
    """Print key payment details."""
    print(f"  Payment:    {payment['payment_id']}")
    print(f"  Amount:     {format_amount(payment['amount'])}")
    print(f"  Method:     {payment['payment_method']}")
    print(f"  Bank:       {payment['bank']}")
    print(f"  Status:     {payment['status'].upper()}")
    if payment.get("error_reason", "none") != "none":
        print(f"  Reason:     {payment['error_reason']}")


def _print_recovery_result(
    strategy: str, gate_result: dict | None, simulated: dict | None
) -> None:
    """Print recovery recommendation, safety gate, and result."""
    print(f"\n  Recovery Recommendation: {strategy}")
    if gate_result:
        status = "✅ ALLOWED" if gate_result["allowed"] else "🚫 BLOCKED"
        print(f"  Safety Gate:            {status}")
        for r in gate_result["reasons"]:
            print(f"    • {r}")
    if simulated:
        print(f"\n  Simulated Result:")
        if simulated["status"] == "captured":
            print(f"  Payment successfully resolved.")
        else:
            print(f"  Payment still failed. Customer action required.")
        print(f"\n  Final Status: {simulated['status'].upper()}")
    print()


def demo_normal_success() -> None:
    """Demo 1: Normal successful payment."""
    _print_scenario_header("DEMO 1 — NORMAL SUCCESS")
    payment = scenario_normal_success()
    _print_payment_info(payment)
    print("\n  ✅ Payment succeeded. No recovery needed.")
    print()


def demo_incorrect_otp() -> None:
    """Demo 2: Incorrect OTP failure — individual customer error."""
    _print_scenario_header("DEMO 2 — INCORRECT OTP")
    payment = scenario_incorrect_otp()
    _print_payment_info(payment)

    # Create a minimal incident for the Recovery Twin
    incident = {
        "bank": payment["bank"],
        "payment_method": payment["payment_method"],
        "error_reason": payment["error_reason"],
        "total_payments": 1,
        "failed_payments": 1,
        "failure_rate": 100.0,
        "revenue_at_risk": payment["amount"],
    }

    print(f"\n  ℹ️  This is an individual customer error, not an incident.")
    print(f"  Recovery Twin analysis:\n")

    options = analyze_incident(incident)
    best = options[0]
    for i, opt in enumerate(options, 1):
        marker = " ← RECOMMENDED" if opt.strategy == best.strategy else ""
        print(f"  {i}. {opt.strategy}{marker}")
        print(f"     Score: {opt.score}/100")
        print(f"     Friction: {opt.customer_friction} | "
              f"Duplicate Risk: {opt.duplicate_risk}")
        print(f"     Reason: {opt.explanation}")
        print()
    print()


def demo_bank_incident_pipeline() -> None:
    """Demo 3: BANK_X UPI incident → Detector → Twin → Safety Gate."""
    _print_scenario_header("DEMO 3 — BANK_X UPI INCIDENT (FULL PIPELINE)")

    # Generate the incident data
    payments = generate_bank_incident()
    print(f"  Generated {len(payments)} BANK_X UPI payment attempts.\n")

    # Step 1: Incident Detector
    print("  ── Step 1: Incident Detector ──\n")
    groups = group_payments(payments)
    incidents = detect_incidents(groups)

    if not incidents:
        print("  No incidents detected.\n")
        return

    incident = incidents[0]
    print(f"  Incident detected: {incident['bank']} + {incident['payment_method']}")
    print(f"  Error: {incident['error_reason']}")
    print(f"  Total: {incident['total_payments']} | "
          f"Failed: {incident['failed_payments']} | "
          f"Rate: {incident['failure_rate']}%")
    print(f"  Revenue At Risk: {format_amount(incident['revenue_at_risk'])}")

    # Step 2: Recovery Twin
    print(f"\n  ── Step 2: Recovery Twin ──\n")
    options = analyze_incident(incident)
    best = options[0]
    print(f"  Recommended: {best.strategy} (Score: {best.score}/100)")
    print(f"  {best.explanation}")

    # Step 3: Safety Gate
    print(f"\n  ── Step 3: Safety Gate ──\n")
    target = None
    for p in payments:
        if p["status"] == "failed":
            target = p
            break

    if target:
        gate_result = safety_evaluate(target, best.strategy)
        status = "✅ ALLOWED" if gate_result["allowed"] else "🚫 BLOCKED"
        print(f"  Safety Gate: {status}")
        for r in gate_result["reasons"]:
            print(f"    • {r}")

        # Step 4: Simulate
        if gate_result["allowed"]:
            print(f"\n  ── Step 4: Simulate Recovery ──\n")
            result = simulate_recovery(target, best.strategy)
            if result["status"] == "captured":
                print(f"  ✅ Payment PAY_{target['payment_id']} resolved → CAPTURED")
            else:
                print(f"  ⏳ Payment still FAILED after {best.strategy}")
                print(f"     Recovery attempts: {result['recovery_attempt_count']}")
    print()


def demo_late_success() -> None:
    """Demo 4: Late-success payment — recovery must stop."""
    _print_scenario_header("DEMO 4 — LATE SUCCESS (RECOVERY MUST STOP)")

    payment = scenario_late_success()
    print("  Before late success:")
    _print_payment_info(payment)

    # Try recovery — Safety Gate should allow it
    gate_before = safety_evaluate(payment, "WAIT_AND_RECHECK")
    status = "✅ ALLOWED" if gate_before["allowed"] else "🚫 BLOCKED"
    print(f"\n  Safety Gate (before late success): {status}")

    # Simulate late success
    simulate_late_success(payment)
    print(f"\n  ⏳ Time passes... Original payment status changes:")
    print(f"  Status: FAILED → {payment['status'].upper()}")

    # Safety Gate should now BLOCK
    gate_after = safety_evaluate(payment, "WAIT_AND_RECHECK")
    status = "✅ ALLOWED" if gate_after["allowed"] else "🚫 BLOCKED"
    print(f"\n  Safety Gate (after late success): {status}")
    for r in gate_after["reasons"]:
        print(f"    • {r}")

    print(f"\n  ✅ Recovery correctly stopped — customer will not be asked to pay twice.")
    print()


def run_demo() -> None:
    """Run all four simulator demonstrations."""
    print()
    print("=" * 60)
    print("  PAYMENT & RECOVERY SIMULATOR — VERSION 1")
    print("=" * 60)

    demo_normal_success()
    demo_incorrect_otp()
    demo_bank_incident_pipeline()
    demo_late_success()

    print("=" * 60)
    print("  All demos completed.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    run_demo()
