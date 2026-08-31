"""
AI Incident Diagnosis Layer — Razorpay AI Revenue Recovery (Track 1)

Uses an LLM (or deterministic mock) to help understand and explain the
likely root cause of a payment incident. The AI only diagnoses and explains
— it does NOT execute payment actions or bypass the Safety Gate.

Flow:
    Incident Detector → AI Diagnosis → Evidence Verification → Recovery Twin
"""

from __future__ import annotations

import os
import sys
import json
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(__file__))

# ---------- DATA MODEL ----------


@dataclass
class Diagnosis:
    """Structured AI diagnosis output."""

    likely_root_cause: str
    incident_scope: str
    confidence: str  # "high" / "medium" / "low"
    evidence: list[str]
    short_explanation: str
    is_mock: bool = False  # True when using mock provider


@dataclass
class Verification:
    """Result of verifying an AI diagnosis against actual incident data."""

    status: str  # "SUPPORTED" or "UNCERTAIN"
    reasons: list[str]


# ---------- VALID ROOT CAUSES ----------

VALID_ROOT_CAUSES = {
    "temporary_bank_degradation",
    "payment_method_outage",
    "merchant_configuration_error",
    "customer_data_issue",
    "connector_timeout",
    "rate_limiting",
    "fraud_detection_trigger",
    "insufficient_funds_cluster",
    "authentication_system_failure",
    "unknown",
}

# ---------- MOCK AI PROVIDER ----------


def _mock_diagnose(incident: dict) -> dict:
    """Deterministic mock diagnosis based on incident data.

    This provides a realistic AI-like response without calling any API.
    Used for testing and offline demos.
    """
    bank = incident.get("bank", "unknown")
    method = incident.get("payment_method", "unknown")
    error = incident.get("error_reason", "unknown")
    failed = incident.get("failed_payments", 0)
    total = incident.get("total_payments", 0)
    rate = incident.get("failure_rate", 0)

    # Determine root cause based on error pattern
    if error == "technical_error":
        root_cause = "temporary_bank_degradation"
        explanation = (
            f"The failures are concentrated on {bank} {method} payments within "
            f"a short period, suggesting a temporary bank-side technical issue."
        )
    elif error == "timeout_error":
        root_cause = "connector_timeout"
        explanation = (
            f"Multiple {bank} {method} payments are timing out, indicating "
            f"possible connector or network degradation."
        )
    elif error == "incorrect_otp":
        root_cause = "authentication_system_failure"
        explanation = (
            f"Multiple OTP failures on {bank} {method} suggest the "
            f"authentication system may be experiencing issues."
        )
    elif error == "insufficient_balance":
        root_cause = "insufficient_funds_cluster"
        explanation = (
            f"A cluster of insufficient balance errors on {bank} {method} "
            f"may indicate a data quality issue or unusual customer segment."
        )
    else:
        root_cause = "unknown"
        explanation = (
            f"Insufficient information to determine root cause for "
            f"{bank} {method} {error} failures."
        )

    # Confidence based on failure rate
    if rate >= 70:
        confidence = "high"
    elif rate >= 50:
        confidence = "medium"
    else:
        confidence = "low"

    # Build evidence list
    evidence = [
        f"{failed} of {total} {bank} {method} payments failed",
        f"Failure rate: {rate}%",
        f"Most common error: {error}",
    ]

    return {
        "likely_root_cause": root_cause,
        "incident_scope": f"{bank}_{method}",
        "confidence": confidence,
        "evidence": evidence,
        "short_explanation": explanation,
    }


# ---------- REAL LLM PROVIDER (placeholder) ----------


def _real_diagnose(incident: dict, api_key: str | None = None) -> dict:
    """Real LLM diagnosis using an API.

    This is a placeholder for future LLM integration.
    Requires an API key via environment variable.
    Raises RuntimeError if API key is not available.
    """
    key = api_key or os.environ.get("AI_DIAGNOSER_API_KEY")
    if not key:
        raise RuntimeError(
            "AI_DIAGNOSER_API_KEY environment variable not set. "
            "Set it to use the real LLM provider."
        )

    # Future: call OpenAI / Anthropic / etc.
    # For now, this is a stub that raises if the key exists but
    # no real implementation is wired up.
    raise NotImplementedError(
        "Real LLM provider not yet implemented. "
        "Set AI_DIAGNOSER_API_KEY and implement _real_diagnose()."
    )


# ---------- PROVIDER SELECTOR ----------


def _get_provider():
    """Return the appropriate diagnosis provider.

    Uses mock mode if AI_DIAGNOSER_MOCK=true (default for tests)
    or if no API key is configured.
    """
    mock_env = os.environ.get("AI_DIAGNOSER_MOCK", "").lower()
    api_key = os.environ.get("AI_DIAGNOSER_API_KEY")

    if mock_env == "true" or not api_key:
        return _mock_diagnose, True  # (provider_function, is_mock)
    else:
        return _real_diagnose, False


# ---------- DIAGNOSIS VALIDATION ----------

_REQUIRED_FIELDS = [
    "likely_root_cause",
    "incident_scope",
    "confidence",
    "evidence",
    "short_explanation",
]


def _validate_diagnosis(raw: dict) -> Diagnosis | None:
    """Validate that the raw AI response has all required fields.

    Returns a Diagnosis if valid, None if malformed.
    """
    if not isinstance(raw, dict):
        return None

    for field in _REQUIRED_FIELDS:
        if field not in raw:
            return None

    # Validate confidence level
    confidence = raw.get("confidence", "").lower()
    if confidence not in ("high", "medium", "low"):
        return None

    # Validate evidence is a list
    evidence = raw.get("evidence")
    if not isinstance(evidence, list):
        return None

    return Diagnosis(
        likely_root_cause=raw["likely_root_cause"],
        incident_scope=raw["incident_scope"],
        confidence=confidence,
        evidence=evidence,
        short_explanation=raw["short_explanation"],
    )


# ---------- EVIDENCE VERIFICATION ----------


def verify_evidence(diagnosis: Diagnosis, incident: dict) -> Verification:
    """Check the AI diagnosis against actual incident data.

    Returns:
        Verification with status "SUPPORTED" or "UNCERTAIN" and reasons.
    """
    reasons = []

    # Check 1: Does the scope match the incident?
    expected_scope = f"{incident.get('bank', '')}_{incident.get('payment_method', '')}"
    if diagnosis.incident_scope.upper() != expected_scope.upper():
        reasons.append(
            f"Scope mismatch: diagnosis says {diagnosis.incident_scope}, "
            f"but incident is {expected_scope}"
        )

    # Check 2: Is the failure rate actually elevated?
    rate = incident.get("failure_rate", 0)
    if rate < 50:
        reasons.append(
            f"Failure rate is only {rate}%, which is not elevated"
        )

    # Check 3: Are there enough failures?
    failed = incident.get("failed_payments", 0)
    if failed < 3:
        reasons.append(
            f"Only {failed} failed payments — too few to confirm an incident"
        )

    # Check 4: Is the root cause one we accept?
    if diagnosis.likely_root_cause not in VALID_ROOT_CAUSES:
        reasons.append(
            f"Unsupported root cause: {diagnosis.likely_root_cause}"
        )

    # Check 5: Does the evidence mention the actual bank?
    bank = incident.get("bank", "")
    evidence_text = " ".join(diagnosis.evidence).lower()
    if bank and bank.lower() not in evidence_text:
        reasons.append(
            f"Evidence does not mention the affected bank ({bank})"
        )

    # Check 6: Does the evidence mention the actual method?
    method = incident.get("payment_method", "")
    if method and method.lower() not in evidence_text:
        reasons.append(
            f"Evidence does not mention the affected method ({method})"
        )

    status = "SUPPORTED" if len(reasons) == 0 else "UNCERTAIN"

    if status == "SUPPORTED":
        reasons.append("All evidence checks passed against incident data")

    return Verification(status=status, reasons=reasons)


# ---------- RECOVERY CONTEXT ----------


@dataclass
class RecoveryContext:
    """Safe, structured context flags derived from a verified AI diagnosis.

    These flags influence plan scoring slightly but do NOT directly
    choose or execute recovery actions.
    """

    prefer_wait_first: bool = False
    avoid_immediate_customer_contact: bool = False
    prefer_status_recheck: bool = False
    description: str = ""


# Mapping from verified root cause to safe recovery context flags
_ROOT_CAUSE_CONTEXT_MAP = {
    "temporary_bank_degradation": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=True,
        description="Prefer wait-first plans; bank issue may self-resolve",
    ),
    "connector_timeout": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=True,
        description="Prefer wait-first plans; timeout may be transient",
    ),
    "payment_method_outage": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=False,
        description="Avoid immediate customer contact; method may recover",
    ),
    "authentication_system_failure": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=True,
        description="Allow customer retry; recheck status after attempt",
    ),
    "insufficient_funds_cluster": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Suggest alternate methods; balance issue needs action",
    ),
    "customer_data_issue": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Customer action likely needed",
    ),
    "rate_limiting": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=True,
        description="Prefer wait-first; rate limits are temporary",
    ),
    "merchant_configuration_error": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Configuration issue; customer action may not help",
    ),
    "fraud_detection_trigger": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Fraud flags require manual review",
    ),
}


def build_recovery_context(
    diagnosis: Diagnosis,
    verification: Verification,
) -> RecoveryContext | None:
    """Convert a verified AI diagnosis into safe recovery context flags.

    Only returns a context if verification status is SUPPORTED.
    For UNCERTAIN diagnoses, returns None (no AI influence on planning).
    """
    if verification.status != "SUPPORTED":
        return None

    # Look up known context for this root cause
    context = _ROOT_CAUSE_CONTEXT_MAP.get(diagnosis.likely_root_cause)
    if context is None:
        # Unknown root cause — no context flags
        return None

    return context


# ---------- MAIN API ----------


def diagnose_incident(incident: dict, is_mock: bool = True) -> tuple[Diagnosis, Verification]:
    """Run the full AI diagnosis pipeline on an incident.

    Args:
        incident: Incident dict from incident_detector.detect_incidents()
        is_mock: If True, use mock provider (for tests). If False, try real API.

    Returns:
        (Diagnosis, Verification) tuple.
    """
    if is_mock:
        provider = _mock_diagnose
        mock_mode = True
    else:
        provider, mock_mode = _get_provider()

    # Get raw diagnosis from provider
    raw = provider(incident)

    # Validate the response
    diagnosis = _validate_diagnosis(raw)
    if diagnosis is None:
        # Malformed response — return a safe fallback
        diagnosis = Diagnosis(
            likely_root_cause="unknown",
            incident_scope="unknown",
            confidence="low",
            evidence=["AI response was malformed or incomplete"],
            short_explanation="Unable to determine root cause from AI response.",
            is_mock=mock_mode,
        )
    else:
        diagnosis.is_mock = mock_mode

    # Verify against actual incident data
    verification = verify_evidence(diagnosis, incident)

    return diagnosis, verification


def diagnose_with_context(
    incident: dict, is_mock: bool = True
) -> tuple[Diagnosis, Verification, RecoveryContext | None]:
    """Run diagnosis and build recovery context in one call.

    Returns:
        (Diagnosis, Verification, RecoveryContext or None)
    """
    diagnosis, verification = diagnose_incident(incident, is_mock=is_mock)
    context = build_recovery_context(diagnosis, verification)
    return diagnosis, verification, context


# ---------- CONSOLE DEMO ----------


def _format_amount(amount: float) -> str:
    """Format amount as Indian Rupees."""
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


def run_demo() -> None:
    """Run the AI diagnosis demo with the BANK_X UPI incident."""
    from payment_simulator import generate_bank_incident
    from incident_detector import detect_incidents, group_payments, format_amount
    from recovery_plan import run_demo as run_plan_demo

    print()
    print("=" * 60)
    print("  AI INCIDENT DIAGNOSIS — DEMO")
    print("=" * 60)
    print()

    # Generate incident
    payments = generate_bank_incident()
    groups = group_payments(payments)
    incidents = detect_incidents(groups)

    if not incidents:
        print("  No incidents detected.\n")
        return

    incident = incidents[0]

    # Step 1: AI Diagnosis
    print("  STEP 1: AI Root Cause Analysis")
    print("  " + "─" * 50)
    diagnosis, verification = diagnose_incident(incident, is_mock=True)

    print(f"  Affected Bank:    {incident['bank']}")
    print(f"  Method:           {incident['payment_method']}")
    print(f"  Failure Rate:     {incident['failure_rate']}%")
    print(f"  Failed Payments:  {incident['failed_payments']}/{incident['total_payments']}")
    print(f"  Revenue At Risk:  {format_amount(incident['revenue_at_risk'])}")
    print()
    print(f"  AI Root Cause:    {diagnosis.likely_root_cause}")
    print(f"  Confidence:       {diagnosis.confidence.upper()}")
    print()
    print("  Evidence:")
    for e in diagnosis.evidence:
        print(f"    - {e}")
    print()
    print(f"  Explanation: {diagnosis.short_explanation}")
    print()

    # Step 2: Evidence Verification
    print("  STEP 2: Evidence Verification")
    print("  " + "─" * 50)
    status_icon = "✅" if verification.status == "SUPPORTED" else "⚠️"
    print(f"  Status: {status_icon} {verification.status}")
    for r in verification.reasons:
        print(f"    - {r}")
    print()

    # Step 3: Build recovery context from verified diagnosis
    print("  STEP 3: Recovery Context")
    print("  " + "─" * 50)
    context = build_recovery_context(diagnosis, verification)
    if context:
        print(f"  Status: AI context applied")
        print(f"  prefer_wait_first: {context.prefer_wait_first}")
        print(f"  avoid_immediate_customer_contact: {context.avoid_immediate_customer_contact}")
        print(f"  prefer_status_recheck: {context.prefer_status_recheck}")
    else:
        print(f"  Status: AI context ignored — diagnosis not supported")
    print()

    # Step 4: Recovery Twin with AI context
    print("  STEP 4: Multi-Step Recovery Twin (with AI context)")
    print("  " + "─" * 50)
    from recovery_plan import (
        ALL_PLANS,
        simulate_recovery_plan,
        score_plan,
        calculate_context_bonus,
        _plan_sort_key,
    )

    incident_payments = [p for p in payments if p["status"] == "failed"]
    results = []
    for plan in ALL_PLANS:
        result = simulate_recovery_plan(incident_payments, plan)
        base_score = score_plan(result)
        ctx_bonus = calculate_context_bonus(plan, context)
        final_score = min(100, base_score + ctx_bonus)
        results.append((plan, result, final_score, base_score, ctx_bonus))
    results.sort(key=_plan_sort_key)
    best_plan, best_result, best_score, best_base, best_bonus = results[0]
    print(f"  Recommended Plan: {best_plan.name}")
    if best_bonus > 0:
        print(f"  Simulation Score: {best_base}/100")
        print(f"  AI Context Bonus: +{best_bonus}")
        print(f"  Final Score:      {best_score}/100")
    else:
        print(f"  Plan Score: {best_score}/100")
    print(f"  Recovered: {best_result.recovered}/{best_result.total_failed} payments")
    print()

    # Step 5: Demo with intentionally incorrect diagnosis
    print("  STEP 5: Incorrect AI Diagnosis (Verification Catches It)")
    print("  " + "─" * 50)
    bad_incident = {
        "bank": "BANK_X",
        "payment_method": "UPI",
        "error_reason": "technical_error",
        "failed_payments": 7,
        "total_payments": 10,
        "failure_rate": 70.0,
    }
    bad_diagnosis = Diagnosis(
        likely_root_cause="customer_data_issue",
        incident_scope="HDFC_Card",  # WRONG — says HDFC Card instead of BANK_X UPI
        confidence="high",
        evidence=["HDFC Card payments failed due to customer data issues"],
        short_explanation="This appears to be a customer data issue on HDFC Card.",
    )
    bad_verification = verify_evidence(bad_diagnosis, bad_incident)
    print(f"  AI says: {bad_diagnosis.incident_scope} — {bad_diagnosis.likely_root_cause}")
    print(f"  Status:  {bad_verification.status}")
    for r in bad_verification.reasons:
        print(f"    - {r}")
    print()

    print("  NOTE: AI diagnosis is for explanation only.")
    print("  Recovery decisions are made by the Recovery Twin + Safety Gate.")
    print()
    print("=" * 60)
    print()


if __name__ == "__main__":
    run_demo()
