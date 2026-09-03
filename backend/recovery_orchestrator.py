"""
End-to-End Recovery Orchestrator — Razorpay AI Revenue Recovery

Coordinates existing components into one complete pipeline:

    Razorpay internal payment record
    → store/update payment history
    → Incident Detector
    → if incident: AI Diagnoser → Evidence Verification → Recovery Context
    → Multi-Step Recovery Twin → Safety Gate
    → Recovery Decision
    → Audit Trail

This module is coordination only — no business logic redesign.
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from razorpay_adapter import (
    InternalPayment,
    PaymentStore,
    EventStore,
    AuditRecord,
    AuditTrail,
    RazorpayWebhookProcessor,
)
from incident_detector import (
    detect_incidents,
    group_payments,
    format_amount,
    TIME_WINDOW_MINUTES,
)
from ai_diagnoser import (
    diagnose_with_context,
    build_recovery_context,
    Diagnosis,
    Verification,
    RecoveryContext,
)
from recovery_plan import (
    ALL_PLANS,
    simulate_recovery_plan,
    score_plan,
    calculate_context_bonus,
    _plan_sort_key,
    RecoveryPlan,
    PlanResult,
)
from safety_gate import evaluate as safety_evaluate
from recovery_twin import select_individual_recovery_strategy


# ---------- RECOVERY DECISION ----------

@dataclass
class RecoveryDecision:
    """Structured result of the full recovery pipeline for one incident."""

    decision_id: str
    timestamp: str

    # Incident info
    incident_scope: str  # e.g. "BANK_X_UPI"
    bank: str
    payment_method: str
    failure_rate: float
    revenue_at_risk: float
    total_payments: int
    failed_payments: int
    affected_payment_ids: list[str]

    # AI diagnosis
    ai_provider_used: str  # "mock", "real_llm", "mock_fallback", "skipped"
    ai_root_cause: str
    ai_confidence: str
    ai_evidence_verification: str  # "SUPPORTED", "UNCERTAIN", "skipped"
    ai_explanation: str

    # Recovery plan
    selected_plan: str
    plans_evaluated: int
    simulation_score: int
    ai_context_bonus: int
    final_score: int

    # Simulated results
    payments_recovered: int
    revenue_recovered: float
    unresolved_revenue: float
    total_recovery_attempts: int
    customer_facing_actions: int
    safety_blocks: int
    late_success_stops: int

    # Safety
    safety_status: str  # "allowed", "blocked", "partial"

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "timestamp": self.timestamp,
            "incident_scope": self.incident_scope,
            "bank": self.bank,
            "payment_method": self.payment_method,
            "failure_rate": self.failure_rate,
            "revenue_at_risk": self.revenue_at_risk,
            "total_payments": self.total_payments,
            "failed_payments": self.failed_payments,
            "affected_payment_ids": self.affected_payment_ids,
            "ai_provider_used": self.ai_provider_used,
            "ai_root_cause": self.ai_root_cause,
            "ai_confidence": self.ai_confidence,
            "ai_evidence_verification": self.ai_evidence_verification,
            "ai_explanation": self.ai_explanation,
            "selected_plan": self.selected_plan,
            "plans_evaluated": self.plans_evaluated,
            "simulation_score": self.simulation_score,
            "ai_context_bonus": self.ai_context_bonus,
            "final_score": self.final_score,
            "payments_recovered": self.payments_recovered,
            "revenue_recovered": self.revenue_recovered,
            "unresolved_revenue": self.unresolved_revenue,
            "total_recovery_attempts": self.total_recovery_attempts,
            "customer_facing_actions": self.customer_facing_actions,
            "safety_blocks": self.safety_blocks,
            "late_success_stops": self.late_success_stops,
            "safety_status": self.safety_status,
        }


# ---------- ORCHESTRATOR ----------

class RecoveryOrchestrator:
    """Coordinates all components into a complete recovery pipeline.

    Two processing modes:

    1. Real-time (process_payment_event):
       Each webhook event is stored immediately.
       Incident detection runs after each event but only creates a decision
       once per scope, using the latest payment store state.

    2. Batch (process_batch + finalize_incidents):
       All events are stored first, then incident analysis runs once
       on the complete state. Used by the demo for correct metrics.

    Flow:
        1. Accept internal payment records (from webhook adapter)
        2. Run Incident Detector on recent payments
        3. For detected incidents:
           a. AI Diagnoser (mock or real)
           b. Evidence Verification
           c. Build Recovery Context (if SUPPORTED)
           d. Multi-Step Recovery Twin
           e. Safety Gate
           f. Create Recovery Decision
        4. For individual failures:
           a. Select individual recovery strategy
           b. Safety Gate check
        5. Record audit trail
    """

    def __init__(self, use_mock_ai: bool = True):
        self.payment_store = PaymentStore()
        self.decisions: list[RecoveryDecision] = []
        self.audit_trail = AuditTrail()
        self._decision_counter = 0
        self.use_mock_ai = use_mock_ai
        # Track the current active decision per incident scope
        # Prevents duplicate decision IDs while allowing metric updates
        self._active_decisions: dict[str, RecoveryDecision] = {}

    def _next_decision_id(self) -> str:
        self._decision_counter += 1
        return f"DEC_{self._decision_counter:04d}"

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _add_audit(self, event_id: str, event_type: str, payment_id: str,
                   action: str, detail: str = "") -> None:
        """Record an audit milestone."""
        self.audit_trail.add(AuditRecord(
            received_at=self._now(),
            event_id=event_id,
            event_type=event_type,
            payment_id=payment_id,
            signature_valid=True,
            signature_reason="",
            is_duplicate=False,
            resulting_status="",
            action=f"{action}: {detail}" if detail else action,
        ))

    # ----- Real-time mode -----

    def process_payment_event(
        self,
        internal_payment: InternalPayment,
        event_id: str = "",
    ) -> dict:
        """Process a single payment event through the orchestrator.

        Stores the payment and runs incident detection on the current state.
        Only creates a decision once per incident scope.

        Args:
            internal_payment: Converted payment from razorpay_adapter.
            event_id: Razorpay event ID for audit trail.

        Returns:
            {
                "payment_id": str,
                "action": "created" | "late_success" | "updated" | "superseded",
                "incident_detected": bool,
                "decisions": list[RecoveryDecision],
                "individual_results": list[dict],
            }
        """
        # Step 1: Store/update payment
        upsert_result = self.payment_store.upsert(internal_payment)
        action = upsert_result["action"]

        self._add_audit(event_id, "payment_stored", internal_payment.payment_id,
                        action, f"status={upsert_result['new_status']}")

        # Step 2: If this is a late-success, stop recovery for this payment
        if action == "late_success":
            self._add_audit(event_id, "late_success_detected", internal_payment.payment_id,
                            "late_success_stop", "recovery blocked — payment captured")
            # Update any active incident that contains this payment
            self._update_affected_incidents(internal_payment, event_id)
            return {
                "payment_id": internal_payment.payment_id,
                "action": action,
                "incident_detected": False,
                "decisions": [],
                "individual_results": [{
                    "payment_id": internal_payment.payment_id,
                    "strategy": "none",
                    "gate_allowed": False,
                    "reason": "late_success — payment already captured",
                }],
            }

        # Step 3: Run incident detection on current payment store
        decisions = self._check_for_incidents(event_id)

        # Step 4: If no incident was detected, check individual failures
        individual_results = []
        if not decisions:
            individual_results = self._handle_individual_failures(event_id)

        return {
            "payment_id": internal_payment.payment_id,
            "action": action,
            "incident_detected": len(decisions) > 0,
            "decisions": decisions,
            "individual_results": individual_results,
        }

    # ----- Batch mode -----

    def store_payment(self, internal_payment: InternalPayment, event_id: str = "") -> dict:
        """Store a payment without running incident detection.

        Used in batch mode where all events are stored first, then
        finalize_incidents() runs detection once on the full state.
        """
        upsert_result = self.payment_store.upsert(internal_payment)
        self._add_audit(event_id, "payment_stored", internal_payment.payment_id,
                        upsert_result["action"],
                        f"status={upsert_result['new_status']}")
        return upsert_result

    def finalize_incidents(self, event_id: str = "") -> list[RecoveryDecision]:
        """Run incident detection on the full current payment store state.

        Creates Recovery Decisions for any newly detected incidents.
        Skips scopes that already have a decision.

        Returns:
            List of newly created RecoveryDecision objects.
        """
        return self._check_for_incidents(event_id)    # ----- Shared logic -----

    def _check_for_incidents(self, event_id: str) -> list[RecoveryDecision]:
        """Run Incident Detector on current payment store.

        For NEW incidents: creates a decision and tracks it in _active_decisions.
        For EXISTING incidents: updates the current decision with new metrics.
        Returns a list of decisions that were created or updated.
        """
        all_payments = [p.to_dict() for p in self.payment_store.get_all()]

        # Need at least a few payments for meaningful detection
        if len(all_payments) < 3:
            return []

        groups = group_payments(all_payments)
        incidents = detect_incidents(groups)

        if not incidents:
            return []

        updated_decisions = []

        for incident in incidents:
            # --- Use the detector-provided time window for scope identity ---
            # The detector returns window_start/window_end with each incident,
            # ensuring different time windows produce distinct scope keys.
            window_start_str = incident.get("window_start", "")
            window_end_str = incident.get("window_end", "")

            # IDs of payments that fall within the incident's time window
            in_window_ids = {
                p["payment_id"] for p in all_payments
                if p.get("bank") == incident["bank"]
                and p.get("payment_method") == incident["payment_method"]
                and p.get("created_at", "") >= window_start_str
                and p.get("created_at", "") <= window_end_str
            }

            # Scope key includes window start so different time windows
            # for the same bank+method produce distinct incident identities
            window_key = window_start_str[:19] if window_start_str else "unknown"
            scope = (
                f"{incident['bank']}_{incident['payment_method']}"
                f"_{window_key}"
            )

            # affected = failed payments WITHIN the window only
            affected_ids = [
                p["payment_id"] for p in all_payments
                if p.get("bank") == incident["bank"]
                and p.get("payment_method") == incident["payment_method"]
                and p.get("status") == "failed"
                and p["payment_id"] in in_window_ids
            ]

            if scope in self._active_decisions:
                # EXISTING incident — update the current decision
                existing = self._active_decisions[scope]
                self._update_incident_decision(existing, incident, affected_ids, event_id)
                updated_decisions.append(existing)
            else:
                # NEW incident — create a decision
                self._add_audit(event_id, "incident_detected", "",
                                "incident", f"{scope} rate={incident['failure_rate']}%")

                decision = self._run_incident_pipeline(incident, affected_ids, event_id, scope=scope)
                self._active_decisions[scope] = decision
                updated_decisions.append(decision)

        return updated_decisions

    def _run_incident_pipeline(
        self,
        incident: dict,
        affected_ids: list[str],
        event_id: str,
        scope: str = "",
    ) -> RecoveryDecision:
        """Run the complete pipeline for one detected incident."""

        decision_id = self._next_decision_id()
        now = self._now()
        if not scope:
            scope = f"{incident['bank']}_{incident['payment_method']}"

        # Step A: AI Diagnosis
        ai_provider = "skipped"
        ai_root_cause = "unknown"
        ai_confidence = "low"
        ai_verification = "skipped"
        ai_explanation = ""
        recovery_context = None

        if self.use_mock_ai:
            diagnosis, verification, recovery_context = diagnose_with_context(
                incident, is_mock=True
            )
            ai_provider = diagnosis.provider_used
            ai_root_cause = diagnosis.likely_root_cause
            ai_confidence = diagnosis.confidence
            ai_verification = verification.status
            ai_explanation = diagnosis.short_explanation
        else:
            diagnosis, verification, recovery_context = diagnose_with_context(
                incident, is_mock=False
            )
            ai_provider = diagnosis.provider_used
            ai_root_cause = diagnosis.likely_root_cause
            ai_confidence = diagnosis.confidence
            ai_verification = verification.status
            ai_explanation = diagnosis.short_explanation

        self._add_audit(event_id, "ai_diagnosis", "",
                        "ai_diagnosis",
                        f"provider={ai_provider} root={ai_root_cause} "
                        f"verification={ai_verification}")

        # Step B: Get incident payments for plan simulation
        incident_payments = []
        for p in self.payment_store.get_all():
            if (p.bank == incident["bank"]
                    and p.payment_method == incident["payment_method"]
                    and p.status == "failed"):
                incident_payments.append(p.to_dict())

        # Step C: Multi-Step Recovery Twin — evaluate all plans
        plan_results = []
        for plan in ALL_PLANS:
            pr = simulate_recovery_plan(incident_payments, plan)
            base_score = score_plan(pr)
            ctx_bonus = calculate_context_bonus(plan, recovery_context)
            final_score = min(100, base_score + ctx_bonus)
            plan_results.append((plan, pr, final_score, base_score, ctx_bonus))

        plan_results.sort(key=_plan_sort_key)
        best_plan, best_result, best_score, best_base, best_bonus = plan_results[0]

        self._add_audit(event_id, "recovery_plan_selected", "",
                        "recovery_plan",
                        f"plan={best_plan.name} score={best_score}")

        # Step D: Safety Gate — check if recovery is allowed for each payment
        safety_status = "allowed"
        for p in incident_payments:
            gate = safety_evaluate(p, best_plan.steps[0])
            if not gate["allowed"]:
                safety_status = "partial"
                break

        self._add_audit(event_id, "safety_check", "",
                        "safety_gate",
                        f"status={safety_status}")

        # Build the decision
        decision = RecoveryDecision(
            decision_id=decision_id,
            timestamp=now,
            incident_scope=scope,
            bank=incident["bank"],
            payment_method=incident["payment_method"],
            failure_rate=incident["failure_rate"],
            revenue_at_risk=incident["revenue_at_risk"],
            total_payments=incident["total_payments"],
            failed_payments=incident["failed_payments"],
            affected_payment_ids=affected_ids,
            ai_provider_used=ai_provider,
            ai_root_cause=ai_root_cause,
            ai_confidence=ai_confidence,
            ai_evidence_verification=ai_verification,
            ai_explanation=ai_explanation,
            selected_plan=best_plan.name,
            plans_evaluated=len(ALL_PLANS),
            simulation_score=best_base,
            ai_context_bonus=best_bonus,
            final_score=best_score,
            payments_recovered=best_result.recovered,
            revenue_recovered=best_result.revenue_recovered,
            unresolved_revenue=best_result.unresolved,
            total_recovery_attempts=best_result.recovery_attempts,
            customer_facing_actions=best_result.customer_facing_actions,
            safety_blocks=best_result.blocked_by_gate,
            late_success_stops=best_result.late_success_stops,
            safety_status=safety_status,
        )

        self.decisions.append(decision)

        self._add_audit(event_id, "decision_recorded", "",
                        "decision_recorded",
                        f"decision_id={decision_id}")

        return decision

    def _update_incident_decision(
        self,
        existing: RecoveryDecision,
        incident: dict,
        affected_ids: list[str],
        event_id: str,
    ) -> None:
        """Update an existing incident decision with new metrics.

        Updates incident metrics, affected payment IDs, and reruns
        the Recovery Twin if the input set has materially changed.
        """
        # Check if inputs materially changed
        old_ids = set(existing.affected_payment_ids)
        new_ids = set(affected_ids)
        metrics_changed = (
            existing.total_payments != incident["total_payments"]
            or existing.failed_payments != incident["failed_payments"]
            or existing.failure_rate != incident["failure_rate"]
            or old_ids != new_ids
        )

        # Always update core metrics
        existing.total_payments = incident["total_payments"]
        existing.failed_payments = incident["failed_payments"]
        existing.failure_rate = incident["failure_rate"]
        existing.revenue_at_risk = incident["revenue_at_risk"]
        existing.affected_payment_ids = affected_ids
        existing.timestamp = self._now()

        if metrics_changed:
            self._add_audit(event_id, "incident_updated", "",
                            "incident_updated",
                            f"{existing.incident_scope} rate={incident['failure_rate']}% "
                            f"failed={incident['failed_payments']}/{incident['total_payments']}")

            # Rerun Recovery Twin with the window-filtered affected payments
            affected_set = set(affected_ids)
            incident_payments = [
                p.to_dict() for p in self.payment_store.get_all()
                if p.payment_id in affected_set and p.status == "failed"
            ]

            plan_results = []
            for plan in ALL_PLANS:
                pr = simulate_recovery_plan(incident_payments, plan)
                base_score = score_plan(pr)
                ctx_bonus = calculate_context_bonus(plan, None)
                final_score = min(100, base_score + ctx_bonus)
                plan_results.append((plan, pr, final_score, base_score, ctx_bonus))

            plan_results.sort(key=_plan_sort_key)
            best_plan, best_result, best_score, best_base, best_bonus = plan_results[0]

            existing.selected_plan = best_plan.name
            existing.simulation_score = best_base
            existing.ai_context_bonus = best_bonus
            existing.final_score = best_score
            existing.payments_recovered = best_result.recovered
            existing.revenue_recovered = best_result.revenue_recovered
            existing.unresolved_revenue = best_result.unresolved
            existing.total_recovery_attempts = best_result.recovery_attempts
            existing.customer_facing_actions = best_result.customer_facing_actions
            existing.safety_blocks = best_result.blocked_by_gate
            existing.late_success_stops = best_result.late_success_stops

            self._add_audit(event_id, "incident_updated_plan", "",
                            "recovery_plan_updated",
                            f"plan={best_plan.name} score={best_score}")

    def _update_affected_incidents(
        self,
        payment: InternalPayment,
        event_id: str,
    ) -> None:
        """Update any active incident that contains this payment (late-success).

        Directly updates the decision's failed count and affected IDs
        without re-running the incident detector (which may no longer
        find the incident if failures drop below threshold).
        """
        for scope, decision in self._active_decisions.items():
            if payment.payment_id in decision.affected_payment_ids:
                # Remove from affected list
                decision.affected_payment_ids = [
                    pid for pid in decision.affected_payment_ids
                    if pid != payment.payment_id
                ]
                # Update failed count and revenue
                decision.failed_payments = len(decision.affected_payment_ids)
                decision.revenue_at_risk = sum(
                    p.amount for p in self.payment_store.get_all()
                    if p.payment_id in decision.affected_payment_ids
                )
                # Update failure rate
                if decision.total_payments > 0:
                    decision.failure_rate = round(
                        (decision.failed_payments / decision.total_payments) * 100, 1
                    )
                decision.timestamp = self._now()

                self._add_audit(event_id, "incident_late_success_update", "",
                                "incident_late_success",
                                f"{scope} failed={decision.failed_payments}/"
                                f"{decision.total_payments} rate={decision.failure_rate}%")
                break

    def _handle_individual_failures(self, event_id: str) -> list[dict]:
        """Handle individual (non-incident) failed payments.

        Returns results for individual payment recovery attempts.
        """
        results = []

        for payment in self.payment_store.get_all():
            if payment.status != "failed":
                continue

            # Check if this payment is part of an active incident
            in_incident = False
            for decision in self._active_decisions.values():
                if payment.payment_id in decision.affected_payment_ids:
                    in_incident = True
                    break
            if in_incident:
                continue

            # Individual recovery strategy
            strategy = select_individual_recovery_strategy(payment.to_dict())
            gate_result = safety_evaluate(payment.to_dict(), strategy)

            results.append({
                "payment_id": payment.payment_id,
                "strategy": strategy,
                "gate_allowed": gate_result["allowed"],
                "gate_reasons": gate_result["reasons"],
            })

        return results

    def get_decisions(self) -> list[dict]:
        """Return all recovery decisions as dicts (for /decisions endpoint)."""
        return [d.to_dict() for d in self.decisions]

    def get_audit_trail(self) -> list[dict]:
        """Return the full audit trail as dicts."""
        return [{
            "received_at": r.received_at,
            "event_id": r.event_id,
            "event_type": r.event_type,
            "payment_id": r.payment_id,
            "action": r.action,
        } for r in self.audit_trail.get_all()]


# ---------- CONSOLE DEMO ----------

def _format_amount(amount: float) -> str:
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


def _make_razorpay_event(
    payment_id: str,
    order_id: str,
    amount_paise: int,
    method: str,
    bank: str,
    status: str,
    event_type: str,
    error_code: str = "",
    error_reason: str = "",
    error_source: str = "",
    created_at: int = 0,
) -> InternalPayment:
    """Create an InternalPayment directly (simulating what the adapter produces)."""
    from razorpay_adapter import _map_error_reason, _map_error_source

    amount_rupees = amount_paise / 100.0
    error_r = _map_error_reason(error_code, error_reason)
    error_s = _map_error_source(error_source)

    if event_type == "payment.failed":
        status = "failed"

    from datetime import datetime as dt, timezone as tz
    if created_at:
        created_at_str = dt.fromtimestamp(created_at, tz=tz.utc).isoformat()
    else:
        created_at_str = dt.now(tz=tz.utc).isoformat()

    return InternalPayment(
        payment_id=payment_id,
        amount=amount_rupees,
        amount_paise=amount_paise,
        currency="INR",
        status=status,
        payment_method=method.capitalize(),
        bank=bank.upper(),
        error_source=error_s,
        error_step="payment_processing",
        error_reason=error_r,
        order_id=order_id,
        wallet="",
        vpa="",
        created_at=created_at_str,
        internal_updated_at=created_at_str,
    )


def run_demo():
    """End-to-end demo: 10 BANK_X UPI payments → incident → AI → recovery → decision.

    Uses batch mode: all events stored first, then incident analysis runs
    once on the complete 5-minute window to get correct metrics.
    """
    print()
    print("=" * 60)
    print("  END-TO-END RECOVERY ORCHESTRATOR — DEMO")
    print("=" * 60)
    print()

    # Force mock AI for demo
    orchestrator = RecoveryOrchestrator(use_mock_ai=True)

    # Base timestamp: 2026-08-30 10:00:00 UTC
    base_ts = 1725000000

    # --- Generate 10 BANK_X UPI payments in 5 minutes ---
    print("─" * 60)
    print("  RAZORPAY EVENTS RECEIVED")
    print("─" * 60)
    print()

    events = []

    # 7 payment.failed events
    fail_offsets = [0, 30, 60, 120, 180, 240, 280]
    for i, offset in enumerate(fail_offsets):
        pid = f"pay_e2e_{i+1:03d}"
        events.append({
            "payment_id": pid,
            "order_id": f"order_e2e_{i+1:03d}",
            "amount_paise": 100000 + (i * 50000),  # 1000 + i*500 rupees
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
            "order_id": f"order_e2e_s{i+1:03d}",
            "amount_paise": 120000 + (i * 30000),
            "method": "upi",
            "bank": "BANK_X",
            "status": "captured",
            "event_type": "payment.captured",
            "created_at": base_ts + offset,
        })

    # Sort by time (interleaved failed and captured)
    events.sort(key=lambda e: e["created_at"])

    print(f"  Processing {len(events)} Razorpay webhook events (batch mode)...")
    print()

    # Step 1: Store all events first (batch mode)
    for i, evt in enumerate(events, 1):
        payment = _make_razorpay_event(**evt)
        result = orchestrator.store_payment(payment, event_id=f"evt_e2e_{i:03d}")
        status_icon = "✅" if evt["status"] == "captured" else "❌"
        print(f"  {i:2d}. {evt['event_type']:<22} {evt['payment_id']:<16} "
              f"{status_icon} → stored ({result['new_status']})")

    print()
    print(f"  {len(events)} payments stored.")
    print()

    # Step 2: Finalize incident analysis on the complete state
    print("─" * 60)
    print("  RUNNING INCIDENT ANALYSIS ON COMPLETE STATE")
    print("─" * 60)
    print()

    decisions = orchestrator.finalize_incidents(event_id="evt_finalize")

    # --- Show incidents detected ---
    if decisions:
        for decision in decisions:
            print("─" * 60)
            print("  INCIDENT DETECTED 🚨")
            print("─" * 60)
            print(f"  Scope:            {decision.incident_scope}")
            print(f"  Total Attempts:   {decision.total_payments}")
            print(f"  Failed:           {decision.failed_payments}")
            successful = decision.total_payments - decision.failed_payments
            print(f"  Successful:       {successful}")
            print(f"  Failure Rate:     {decision.failure_rate}%")
            print(f"  Revenue At Risk:  {_format_amount(decision.revenue_at_risk)}")
            print()

            # AI Diagnosis
            print("  AI DIAGNOSIS")
            print(f"  Provider:         {decision.ai_provider_used}")
            print(f"  Root Cause:       {decision.ai_root_cause}")
            print(f"  Confidence:       {decision.ai_confidence.upper()}")
            print(f"  Verification:     {decision.ai_evidence_verification}")
            print(f"  Explanation:      {decision.ai_explanation}")
            print()

            # Recovery Twin
            print("  RECOVERY TWIN")
            print(f"  Affected Failed Payments: {decision.failed_payments}")
            print(f"  Plans Evaluated:  {decision.plans_evaluated}")
            print(f"  Selected Plan:    {decision.selected_plan}")
            print(f"  Simulation Score: {decision.simulation_score}/100")
            if decision.ai_context_bonus > 0:
                print(f"  AI Context Bonus: +{decision.ai_context_bonus}")
            print(f"  Final Score:      {decision.final_score}/100")
            print(f"  Recovered:        {decision.payments_recovered}/{decision.failed_payments} payments")
            print(f"  Revenue Recovered:{_format_amount(decision.revenue_recovered)}")
            print()

            # Safety Gate
            print("  SAFETY GATE")
            print(f"  Status: {decision.safety_status}")
            print()

            # Decision
            print("  DECISION RECORDED ✅")
            print(f"  Decision ID: {decision.decision_id}")
            print()
    else:
        print("  No incidents detected.\n")

    # --- Late-success demo ---
    print("─" * 60)
    print("  LATE SUCCESS DEMONSTRATION")
    print("─" * 60)
    print()

    # Process a new failed payment, then a captured event for the same payment
    late_payment = _make_razorpay_event(
        payment_id="pay_late_001",
        order_id="order_late_001",
        amount_paise=300000,
        method="upi",
        bank="BANK_X",
        status="failed",
        event_type="payment.failed",
        error_code="GATEWAY_ERROR",
        error_reason="technical_error",
        error_source="bank_api",
        created_at=base_ts + 400,
    )
    r1 = orchestrator.process_payment_event(late_payment, event_id="evt_late_001")
    print(f"  1. payment.failed  → pay_late_001 → status = failed")
    print(f"     Action: {r1['action']}")

    late_captured = _make_razorpay_event(
        payment_id="pay_late_001",
        order_id="order_late_001",
        amount_paise=300000,
        method="upi",
        bank="BANK_X",
        status="captured",
        event_type="payment.captured",
        created_at=base_ts + 500,
    )
    r2 = orchestrator.process_payment_event(late_captured, event_id="evt_late_002")
    print(f"  2. payment.captured → pay_late_001 → status = captured")
    print(f"     Action: {r2['action']}")
    print()
    print(f"  LATE SUCCESS — RECOVERY BLOCKED ✅")
    print(f"  Reason: Payment already captured. Safety Gate prevents duplicate recovery.")
    print()

    # --- /decisions output ---
    print("─" * 60)
    print("  /decisions — STRUCTURED OUTPUT (JSON preview)")
    print("─" * 60)
    print()
    decisions = orchestrator.get_decisions()
    for d in decisions:
        print(f"  Decision ID:    {d['decision_id']}")
        print(f"  Scope:          {d['incident_scope']}")
        print(f"  Total Payments: {d['total_payments']}")
        print(f"  Failed:         {d['failed_payments']}")
        print(f"  Failure Rate:   {d['failure_rate']}%")
        print(f"  Plan:           {d['selected_plan']}")
        print(f"  Score:          {d['final_score']}/100")
        print(f"  Recovered:      {d['payments_recovered']}/{d['failed_payments']}")
        print(f"  AI Provider:    {d['ai_provider_used']}")
        print(f"  Verification:   {d['ai_evidence_verification']}")
        print()

    # --- Audit Trail ---
    print("─" * 60)
    print("  AUDIT TRAIL")
    print("─" * 60)
    print()
    for record in orchestrator.get_audit_trail():
        print(f"  {record['received_at'][:19]}  {record['action'][:55]}")

    print()
    print("=" * 60)
    print("  End-to-end demo complete.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    run_demo()
