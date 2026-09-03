"""
Flask API Server — Razorpay AI Revenue Recovery Dashboard

Exposes the real Python backend to the React dashboard.
All calculations come from the authoritative Python implementation.

DEMO / SYNTHETIC DATA ONLY — never executes real financial recovery.
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, jsonify, request
from recovery_orchestrator import (
    RecoveryOrchestrator,
    RecoveryDecision,
    _make_razorpay_event,
)
from razorpay_adapter import InternalPayment

app = Flask(__name__)

# ─── Global orchestrator state (demo/synthetic only) ───
_orchestrator: RecoveryOrchestrator | None = None
_demo_events: list[dict] = []


def _get_orchestrator() -> RecoveryOrchestrator:
    """Get or create the global demo orchestrator.

    Uses real LLM when AI_DIAGNOSER_API_KEY is configured,
    falls back to mock AI otherwise.
    """
    global _orchestrator
    if _orchestrator is None:
        use_real = bool(os.environ.get("AI_DIAGNOSER_API_KEY"))
        _orchestrator = RecoveryOrchestrator(use_mock_ai=not use_real)
    return _orchestrator


def _reset_demo():
    """Reset demo state for a fresh simulation."""
    global _orchestrator, _demo_events
    # Always force mock AI in tests — never make external API calls
    _orchestrator = RecoveryOrchestrator(use_mock_ai=True)
    _demo_events = []


# ─── CORS ───

@app.after_request
def add_cors_headers(response):
    """Add minimal CORS headers for Vite dev server."""
    origin = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


# ─── Health ───

@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    orch = _get_orchestrator()
    return jsonify({
        "status": "healthy",
        "payments_stored": len(orch.payment_store.get_all()),
        "decisions_count": len(orch.decisions),
        "demo_available": True,
    })


# ─── Payments ───

@app.route("/payments", methods=["GET"])
def get_payments():
    """Return all stored payments as JSON."""
    orch = _get_orchestrator()
    payments = orch.payment_store.get_all()
    return jsonify({
        "payments": [p.to_dict() for p in payments],
        "count": len(payments),
    })


# ─── Decisions ───

@app.route("/decisions", methods=["GET"])
def get_decisions():
    """Return all recovery decisions as JSON."""
    orch = _get_orchestrator()
    decisions = orch.get_decisions()
    return jsonify({
        "decisions": decisions,
        "count": len(decisions),
    })


# ─── Audit Trail ───

@app.route("/audit", methods=["GET"])
def get_audit():
    """Return the full audit trail as JSON."""
    orch = _get_orchestrator()
    trail = orch.get_audit_trail()
    return jsonify({
        "entries": trail,
        "count": len(trail),
    })


# ─── Demo Simulation ───

def _build_demo_events() -> list[dict]:
    """Build the 10 synthetic BANK_X UPI events for the demo.

    These are the same events used by recovery_orchestrator.run_demo().
    """
    base_ts = 1725000000  # 2024-08-30 10:00:00 UTC
    events = []

    # 7 payment.failed events
    fail_offsets = [0, 30, 60, 120, 180, 240, 280]
    for i, offset in enumerate(fail_offsets):
        pid = f"pay_e2e_{i+1:03d}"
        events.append({
            "payment_id": pid,
            "order_id": f"order_e2e_{i+1:03d}",
            "amount_paise": 100000 + (i * 50000),
            "method": "upi",
            "bank": "BANK_X",
            "status": "failed",
            "event_type": "payment.failed",
            "error_code": "GATEWAY_ERROR",
            "error_reason": "technical_error",
            "error_source": "bank_api",
            "created_at": base_ts + offset,
        })

    # 3 payment.captured events
    cap_offsets = [90, 150, 270]
    for i, offset in enumerate(cap_offsets):
        pid = f"pay_e2e_s{i+1:03d}"
        events.append({
            "payment_id": pid,
            "order_id": f"order_e2e_{i+1:03d}",
            "amount_paise": 120000 + (i * 30000),
            "method": "upi",
            "bank": "BANK_X",
            "status": "captured",
            "event_type": "payment.captured",
            "created_at": base_ts + offset,
        })

    # Sort by time (interleaved)
    events.sort(key=lambda e: e["created_at"])
    return events


def _decision_to_full_json(decision: RecoveryDecision, orchestrator: RecoveryOrchestrator) -> dict:
    """Convert a RecoveryDecision to the full JSON structure the dashboard needs.

    Includes all plan details, safety gate info, and audit trail.
    """
    d = decision.to_dict()

    # Build plan results for the Recovery Twin display
    # Re-run the plans through the same logic the orchestrator used
    from recovery_plan import (
        ALL_PLANS,
        simulate_recovery_plan,
        score_plan,
        calculate_context_bonus,
        _plan_sort_key,
    )

    # Get the incident payments for this decision's scope
    incident_payments = []
    for p in orchestrator.payment_store.get_all():
        if (p.bank == decision.bank
                and p.payment_method == decision.payment_method
                and p.status == "failed"):
            incident_payments.append(p.to_dict())

    # Reconstruct RecoveryContext from the decision's AI diagnosis
    # so that evidence-gated context bonus is correctly applied
    recovery_context = None
    if decision.ai_evidence_verification == "SUPPORTED":
        from ai_diagnoser import RecoveryContext
        root_cause = decision.ai_root_cause
        recovery_context = RecoveryContext(
            description=decision.ai_explanation,
            prefer_wait_first=root_cause in (
                "temporary_bank_degradation", "connector_timeout",
                "payment_method_outage",
            ),
            avoid_immediate_customer_contact=root_cause in (
                "temporary_bank_degradation", "connector_timeout",
                "payment_method_outage",
            ),
            prefer_status_recheck=root_cause in (
                "temporary_bank_degradation", "connector_timeout",
            ),
        )

    plan_results = []
    for plan in ALL_PLANS:
        pr = simulate_recovery_plan(incident_payments, plan)
        base_score = score_plan(pr)
        ctx_bonus = calculate_context_bonus(plan, recovery_context)
        final_score = min(100, base_score + ctx_bonus)
        plan_results.append({
            "name": plan.name,
            "description": plan.description,
            "steps": plan.steps,
            "recovered": pr.recovered,
            "totalFailed": pr.total_failed,
            "revenueRecovered": pr.revenue_recovered,
            "revenueAtRisk": pr.revenue_at_risk,
            "unresolved": pr.unresolved,
            "recoveryAttempts": pr.recovery_attempts,
            "customerFacingActions": pr.customer_facing_actions,
            "blockedByGate": pr.blocked_by_gate,
            "simulationScore": base_score,
            "contextBonus": ctx_bonus,
            "finalScore": final_score,
            "isBest": False,
        })

    # Sort and mark best
    plan_results.sort(key=lambda p: (-p["finalScore"], len(p["steps"]),
                                      p["customerFacingActions"],
                                      p["recoveryAttempts"],
                                      p["name"]))
    if plan_results:
        plan_results[0]["isBest"] = True

    # Safety gate details
    from safety_gate import evaluate as safety_evaluate
    safety_results = []
    for p in incident_payments:
        gate = safety_evaluate(p, decision.selected_plan.split("_")[-1]
                               if decision.selected_plan else "WAIT_AND_RECHECK")
        if not gate["allowed"]:
            safety_results.append(gate)

    safety_detail = {
        "strategy": decision.selected_plan,
        "allowed": decision.safety_status != "blocked",
        "reasons": [],
    }
    if decision.safety_status == "allowed":
        safety_detail["reasons"] = [
            "All safety checks passed.",
            f"Recovery simulated: {decision.payments_recovered}/{decision.failed_payments} payments."
        ]
    elif decision.safety_status == "partial":
        safety_detail["reasons"] = ["Partial safety gate — some payments blocked."]
    else:
        safety_detail["reasons"] = ["Safety gate blocked recovery."]

    # Incident object for the dashboard
    incident_obj = {
        "id": decision.incident_scope,
        "bank": decision.bank,
        "paymentMethod": decision.payment_method,
        "errorReason": "technical_error",
        "totalPayments": decision.total_payments,
        "failedPayments": decision.failed_payments,
        "failureRate": decision.failure_rate,
        "revenueAtRisk": decision.revenue_at_risk,
        "affectedPaymentIds": decision.affected_payment_ids,
    }

    # Diagnosis object
    diagnosis_obj = {
        "provider": decision.ai_provider_used,
        "rootCause": decision.ai_root_cause,
        "confidence": decision.ai_confidence,
        "evidence": [
            f"{decision.failed_payments} of {decision.total_payments} {decision.bank} {decision.payment_method} payments failed",
            f"Failure rate: {decision.failure_rate}%",
            f"Most common error: technical_error",
        ],
        "explanation": decision.ai_explanation,
        "verificationStatus": decision.ai_evidence_verification,
    }

    # Audit trail
    audit_entries = []
    for record in orchestrator.audit_trail.get_all():
        audit_entries.append({
            "timestamp": record.received_at,
            "action": record.action,
            "detail": f"{record.event_type} → {record.payment_id}" if record.payment_id else record.event_type,
        })

    # Demo events for the payment feed
    demo_evts = _build_demo_events()
    payment_events = []
    for evt in demo_evts:
        payment_events.append({
            "id": evt["payment_id"],
            "orderId": evt["order_id"],
            "amountPaise": evt["amount_paise"],
            "method": evt["method"],
            "bank": evt["bank"],
            "status": evt["status"],
            "eventType": evt["event_type"],
            "errorCode": evt.get("error_code", ""),
            "errorReason": evt.get("error_reason", ""),
            "errorSource": evt.get("error_source", ""),
            "timestampMs": evt["created_at"] * 1000,
        })

    return {
        "decisionId": decision.decision_id,
        "incident": incident_obj,
        "diagnosis": diagnosis_obj,
        "plans": plan_results,
        "selectedPlan": decision.selected_plan,
        "safety": safety_detail,
        "recovered": decision.payments_recovered,
        "revenueRecovered": decision.revenue_recovered,
        "unresolvedRevenue": decision.unresolved_revenue,
        "auditTrail": audit_entries,
        "paymentEvents": payment_events,
    }


@app.route("/demo/simulate-incident", methods=["POST", "OPTIONS"])
def simulate_incident():
    """DEMO / SYNTHETIC DATA ONLY.

    Runs the existing BANK_X UPI end-to-end demo through the actual
    Python orchestrator. Returns the real Recovery Decision.

    Never executes real financial recovery.
    """
    if request.method == "OPTIONS":
        return "", 204

    # Reset and run a fresh demo
    # Use real LLM if API key is configured, mock otherwise
    global _orchestrator
    use_real = bool(os.environ.get("AI_DIAGNOSER_API_KEY"))
    _orchestrator = RecoveryOrchestrator(use_mock_ai=not use_real)
    orch = _get_orchestrator()

    # Generate the 10 synthetic events
    events = _build_demo_events()

    # Step 1: Store all events (batch mode)
    for i, evt in enumerate(events, 1):
        payment = _make_razorpay_event(**evt)
        orch.store_payment(payment, event_id=f"evt_api_{i:03d}")

    # Step 2: Finalize incident analysis
    decisions = orch.finalize_incidents(event_id="evt_api_finalize")

    if not decisions:
        return jsonify({
            "success": False,
            "error": "No incidents detected in demo data",
        }), 500

    # Return the full decision with all details
    decision = decisions[0]
    result = _decision_to_full_json(decision, orch)

    return jsonify({
        "success": True,
        "demo": True,
        "eventsProcessed": len(events),
        "decision": result,
    })


# ─── Main ───

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
