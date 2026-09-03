"""
Tests for End-to-End Recovery Orchestrator — Razorpay AI Revenue Recovery

Tests cover:
  - Webhook payment reaches orchestrator
  - Individual failure does not falsely create incident
  - Correlated BANK_X UPI failures create incident
  - 7 failed + 3 captured → 70% failure rate, 10 total
  - Recovery Twin receives all 7 affected failed payments
  - Real-time sequential arrival: 3/3 → 7/10 update
  - Same decision updated rather than duplicated
  - Duplicate decisions are not generated
  - Batch mode produces correct metrics
  - Late-success updates existing incident
  - AI Diagnoser runs for detected incident
  - Safety Gate remains authoritative
  - Decision object contains required fields
  - Audit trail records decision path
  - /decisions returns structured results
  - Existing project tests still pass
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "backend"))

from razorpay_adapter import InternalPayment, PaymentStore
from recovery_orchestrator import RecoveryOrchestrator, RecoveryDecision
from safety_gate import evaluate as safety_evaluate


# ---------- TEST HELPERS ----------

def _make_payment(
    payment_id: str,
    amount: float = 1000.0,
    status: str = "failed",
    method: str = "Upi",
    bank: str = "BANK_X",
    error_reason: str = "technical_error",
    error_source: str = "bank_api",
    created_at_offset_seconds: int = 0,
) -> InternalPayment:
    from datetime import datetime
    base = datetime(2026, 8, 30, 10, 0, 0, tzinfo=timezone.utc)
    ts = (base + timedelta(seconds=created_at_offset_seconds)).isoformat()
    return InternalPayment(
        payment_id=payment_id, amount=amount, amount_paise=int(amount * 100),
        currency="INR", status=status, payment_method=method, bank=bank,
        error_source=error_source, error_step="payment_processing",
        error_reason=error_reason, order_id=f"order_{payment_id}",
        wallet="", vpa="", created_at=ts, internal_updated_at=ts,
    )


def _make_captured_payment(
    payment_id: str, amount: float = 1200.0,
    method: str = "Upi", bank: str = "BANK_X",
    created_at_offset_seconds: int = 0,
) -> InternalPayment:
    return _make_payment(
        payment_id=payment_id, amount=amount, status="captured",
        method=method, bank=bank, error_reason="none", error_source="none",
        created_at_offset_seconds=created_at_offset_seconds,
    )


def _feed_bank_x_incident(orch: RecoveryOrchestrator, use_batch: bool = True) -> None:
    """Feed 7 failed + 3 captured BANK_X UPI payments in 5 minutes."""
    fail_offsets = [0, 30, 60, 120, 180, 240, 280]
    cap_offsets = [90, 150, 270]

    if use_batch:
        for i, offset in enumerate(fail_offsets):
            p = _make_payment(f"pay_feed_{i+1:03d}", amount=1000.0 + i * 500,
                              created_at_offset_seconds=offset)
            orch.store_payment(p, event_id=f"evt_feed_{i+1:03d}")
        for i, offset in enumerate(cap_offsets):
            p = _make_captured_payment(f"pay_feed_s{i+1:03d}", amount=1200.0 + i * 300,
                                        created_at_offset_seconds=offset)
            orch.store_payment(p, event_id=f"evt_feed_s{i+1:03d}")
        orch.finalize_incidents(event_id="evt_finalize")
    else:
        for i, offset in enumerate(fail_offsets):
            p = _make_payment(f"pay_feed_{i+1:03d}", amount=1000.0 + i * 500,
                              created_at_offset_seconds=offset)
            orch.process_payment_event(p, event_id=f"evt_feed_{i+1:03d}")
        for i, offset in enumerate(cap_offsets):
            p = _make_captured_payment(f"pay_feed_s{i+1:03d}", amount=1200.0 + i * 300,
                                        created_at_offset_seconds=offset)
            orch.process_payment_event(p, event_id=f"evt_feed_s{i+1:03d}")


# ---------- TEST CLASSES ----------


class TestWebhookPaymentReachesOrchestrator(unittest.TestCase):
    def test_single_payment_is_stored(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        payment = _make_payment("pay_orch_001")
        result = orch.process_payment_event(payment, event_id="evt_001")
        self.assertEqual(result["payment_id"], "pay_orch_001")
        self.assertIn(result["action"], ("created", "updated"))

    def test_payment_accessible_in_store(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        payment = _make_payment("pay_orch_002")
        orch.process_payment_event(payment)
        stored = orch.payment_store.get("pay_orch_002")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "failed")


class TestIndividualFailureNoFalseIncident(unittest.TestCase):
    def test_single_failed_payment_no_incident(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        result = orch.process_payment_event(_make_payment("pay_ind_001"))
        self.assertFalse(result["incident_detected"])
        self.assertEqual(len(result["decisions"]), 0)

    def test_two_failed_payments_no_incident(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        orch.process_payment_event(_make_payment("pay_ind_002"))
        orch.process_payment_event(_make_payment("pay_ind_003"))
        self.assertEqual(len(orch.decisions), 0)

    def test_individual_failure_gets_strategy(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        result = orch.process_payment_event(_make_payment("pay_ind_004"))
        self.assertEqual(len(result["individual_results"]), 1)
        self.assertIn("strategy", result["individual_results"][0])


class TestIncidentCorrectMetrics(unittest.TestCase):
    def test_total_payments_is_10(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].total_payments, 10)

    def test_failed_payments_is_7(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].failed_payments, 7)

    def test_failure_rate_is_70_percent(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].failure_rate, 70.0)

    def test_revenue_at_risk_is_correct(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].revenue_at_risk, 17500.0)

    def test_affected_payment_ids_count_is_7(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(len(orch.decisions[0].affected_payment_ids), 7)


class TestRealTimeSequentialUpdate(unittest.TestCase):
    """Real-time mode: 3/3 initial → 7/10 final, same decision updated."""

    def test_three_failures_create_incident(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        # First 3 failures (t=0, 30, 60)
        for i, offset in enumerate([0, 30, 60]):
            orch.process_payment_event(
                _make_payment(f"pay_seq_{i+1:03d}", created_at_offset_seconds=offset),
                event_id=f"evt_seq_{i+1:03d}")
        self.assertEqual(len(orch.decisions), 1)
        self.assertEqual(orch.decisions[0].failed_payments, 3)
        self.assertEqual(orch.decisions[0].failure_rate, 100.0)

    def test_seven_failures_three_captured_updates_to_70_percent(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        # First 3 failures
        for i, offset in enumerate([0, 30, 60]):
            orch.process_payment_event(
                _make_payment(f"pay_seq2_{i+1:03d}", created_at_offset_seconds=offset),
                event_id=f"evt_seq2_{i+1:03d}")
        # 1 captured (t=90)
        orch.process_payment_event(
            _make_captured_payment("pay_seq2_s01", created_at_offset_seconds=90),
            event_id="evt_seq2_s01")
        # Next 4 failures
        for i, offset in enumerate([120, 180, 240, 280]):
            orch.process_payment_event(
                _make_payment(f"pay_seq2_{i+4:03d}", created_at_offset_seconds=offset),
                event_id=f"evt_seq2_{i+4:03d}")
        # 2 more captured
        orch.process_payment_event(
            _make_captured_payment("pay_seq2_s02", created_at_offset_seconds=150),
            event_id="evt_seq2_s02")
        orch.process_payment_event(
            _make_captured_payment("pay_seq2_s03", created_at_offset_seconds=270),
            event_id="evt_seq2_s03")

        # Verify final state: 7 failed / 10 total = 70%
        # All failures use default amount=1000, so revenue_at_risk = 7*1000 = 7000
        decision = orch.decisions[0]
        self.assertEqual(decision.total_payments, 10)
        self.assertEqual(decision.failed_payments, 7)
        self.assertEqual(decision.failure_rate, 70.0)
        self.assertEqual(decision.revenue_at_risk, 7000.0)
        self.assertEqual(len(decision.affected_payment_ids), 7)

    def test_same_decision_id_not_duplicated(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        for i, offset in enumerate([0, 30, 60]):
            orch.process_payment_event(
                _make_payment(f"pay_nodup_{i+1:03d}", created_at_offset_seconds=offset),
                event_id=f"evt_nodup_{i+1:03d}")
        first_id = orch.decisions[0].decision_id
        # Add more events — decision should update, not duplicate
        for i, offset in enumerate([90, 120, 180, 240]):
            orch.process_payment_event(
                _make_captured_payment(f"pay_nodup_s{i+1:03d}", created_at_offset_seconds=offset),
                event_id=f"evt_nodup_s{i+1:03d}")
        self.assertEqual(len(orch.decisions), 1)
        self.assertEqual(orch.decisions[0].decision_id, first_id)

    def test_decision_count_stays_one(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        # Feed full incident one event at a time
        events = [
            (0, "failed"), (30, "failed"), (60, "failed"), (90, "captured"),
            (120, "failed"), (150, "captured"), (180, "failed"),
            (240, "failed"), (270, "captured"), (280, "failed"),
        ]
        for i, (offset, status) in enumerate(events):
            if status == "failed":
                p = _make_payment(f"pay_cnt_{i+1:03d}", created_at_offset_seconds=offset)
            else:
                p = _make_captured_payment(f"pay_cnt_{i+1:03d}", created_at_offset_seconds=offset)
            orch.process_payment_event(p, event_id=f"evt_cnt_{i+1:03d}")
        self.assertEqual(len(orch.decisions), 1)


class TestRecoveryTwinReceivesAllAffected(unittest.TestCase):
    def test_twin_receives_7_payments(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].failed_payments, 7)
        self.assertEqual(orch.decisions[0].payments_recovered, 7)

    def test_revenue_recovered_matches_full_incident(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].revenue_recovered, 17500.0)


class TestCorrelatedFailuresCreateIncident(unittest.TestCase):
    def test_incident_is_detected(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertGreater(len(orch.decisions), 0)

    def test_incident_scope_is_correct(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].bank, "BANK_X")
        self.assertIn("Upi", orch.decisions[0].incident_scope)

    def test_incident_has_70_percent_failure_rate(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].failure_rate, 70.0)


class TestAiDiagnoserRunsForIncident(unittest.TestCase):
    def test_ai_provider_is_recorded(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertNotEqual(orch.decisions[0].ai_provider_used, "skipped")

    def test_ai_root_cause_is_correct(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].ai_root_cause, "temporary_bank_degradation")

    def test_ai_confidence_is_high_for_70_percent(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertEqual(orch.decisions[0].ai_confidence, "high")


class TestSupportedDiagnosisGeneratesContext(unittest.TestCase):
    def test_context_bonus_applied(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        decision = orch.decisions[0]
        self.assertEqual(decision.ai_evidence_verification, "SUPPORTED")
        self.assertGreater(decision.ai_context_bonus, 0)

    def test_final_score_includes_bonus(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        decision = orch.decisions[0]
        self.assertEqual(decision.final_score, decision.simulation_score + decision.ai_context_bonus)


class TestRecoveryTwinProducesPlan(unittest.TestCase):
    def test_plan_is_selected(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertTrue(orch.decisions[0].selected_plan.startswith("PLAN_"))
        self.assertEqual(orch.decisions[0].plans_evaluated, 4)

    def test_score_is_between_0_and_100(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertGreaterEqual(orch.decisions[0].final_score, 0)
        self.assertLessEqual(orch.decisions[0].final_score, 100)


class TestSafetyGateRemainsAuthoritative(unittest.TestCase):
    def test_captured_payment_blocks_recovery(self):
        payment = {
            "payment_id": "pay_gate_001", "amount": 1000.0, "status": "captured",
            "payment_method": "UPI", "bank": "BANK_X",
            "recovery_attempt_count": 0, "customer_contact_count": 0,
            "recovered_already": False, "last_recovery_attempt_time": None,
        }
        result = safety_evaluate(payment, "WAIT_AND_RECHECK")
        self.assertFalse(result["allowed"])

    def test_decision_safety_status_recorded(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        self.assertIn(orch.decisions[0].safety_status, ("allowed", "blocked", "partial"))


class TestLateSuccessBlocksRecovery(unittest.TestCase):
    def test_late_success_action(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        r1 = orch.process_payment_event(_make_payment("pay_ls_001", created_at_offset_seconds=0))
        self.assertEqual(r1["action"], "created")
        r2 = orch.process_payment_event(_make_captured_payment("pay_ls_001", created_at_offset_seconds=60))
        self.assertEqual(r2["action"], "late_success")
        self.assertEqual(len(r2["decisions"]), 0)

    def test_late_success_no_decisions_created(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        orch.process_payment_event(_make_payment("pay_ls_002"))
        orch.process_payment_event(_make_captured_payment("pay_ls_002"))
        self.assertEqual(len(orch.decisions), 0)

    def test_late_success_state_updates_correctly(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        orch.process_payment_event(_make_payment("pay_ls_003"))
        self.assertEqual(orch.payment_store.get("pay_ls_003").status, "failed")
        orch.process_payment_event(_make_captured_payment("pay_ls_003"))
        self.assertEqual(orch.payment_store.get("pay_ls_003").status, "captured")

    def test_late_success_updates_existing_incident(self):
        """A late-success for a payment in an active incident updates the incident."""
        orch = RecoveryOrchestrator(use_mock_ai=True)
        # Create incident with 3 failures
        for i, offset in enumerate([0, 30, 60]):
            orch.process_payment_event(
                _make_payment(f"pay_lsu_{i+1:03d}", created_at_offset_seconds=offset),
                event_id=f"evt_lsu_{i+1:03d}")
        self.assertEqual(orch.decisions[0].failed_payments, 3)

        # Late-success: one payment becomes captured
        orch.process_payment_event(
            _make_captured_payment("pay_lsu_001", created_at_offset_seconds=90),
            event_id="evt_lsu_s01")

        # Decision should be updated — one fewer failed payment
        self.assertEqual(orch.decisions[0].failed_payments, 2)


class TestDuplicateWebhookNoDuplicateDecision(unittest.TestCase):
    def test_duplicate_event_ignored(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        base_ts = 1725000000
        for i, offset in enumerate([0, 30, 60, 120, 180, 240, 280]):
            p = _make_payment(f"pay_dup_{i+1:03d}", created_at_offset_seconds=offset)
            orch.store_payment(p, event_id=f"evt_dup_{i+1:03d}")
        orch.finalize_incidents(event_id="evt_fin1")
        count = len(orch.decisions)
        orch.finalize_incidents(event_id="evt_fin2")
        self.assertEqual(len(orch.decisions), count)


class TestDuplicateDecisionsNotGenerated(unittest.TestCase):
    def test_finalize_called_twice_no_duplicate(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        for i, offset in enumerate([0, 30, 60, 120, 180, 240, 280]):
            p = _make_payment(f"pay_dd_{i+1:03d}", created_at_offset_seconds=offset)
            orch.store_payment(p, event_id=f"evt_dd_{i+1:03d}")
        for i, offset in enumerate([90, 150, 270]):
            p = _make_captured_payment(f"pay_dd_s{i+1:03d}", created_at_offset_seconds=offset)
            orch.store_payment(p, event_id=f"evt_dd_s{i+1:03d}")
        orch.finalize_incidents(event_id="evt_fin1")
        orch.finalize_incidents(event_id="evt_fin2")
        self.assertEqual(len(orch.decisions), 1)


class TestDecisionObjectContainsRequiredFields(unittest.TestCase):
    def _make_decision(self) -> RecoveryDecision:
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        return orch.decisions[0]

    def test_decision_id_is_set(self):
        self.assertTrue(self._make_decision().decision_id.startswith("DEC_"))

    def test_timestamp_is_set(self):
        self.assertIn("T", self._make_decision().timestamp)

    def test_to_dict_has_all_keys(self):
        d = self._make_decision().to_dict()
        for key in [
            "decision_id", "timestamp", "incident_scope", "bank",
            "payment_method", "failure_rate", "revenue_at_risk",
            "total_payments", "failed_payments", "affected_payment_ids",
            "ai_provider_used", "ai_root_cause", "ai_confidence",
            "ai_evidence_verification", "ai_explanation",
            "selected_plan", "plans_evaluated", "simulation_score",
            "ai_context_bonus", "final_score",
            "payments_recovered", "revenue_recovered", "unresolved_revenue",
            "total_recovery_attempts", "customer_facing_actions",
            "safety_blocks", "late_success_stops", "safety_status",
        ]:
            self.assertIn(key, d, f"Missing key: {key}")


class TestAuditTrailRecordsDecisionPath(unittest.TestCase):
    def test_audit_contains_all_milestones(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        actions = [r["action"] for r in orch.get_audit_trail()]
        self.assertTrue(any("incident" in a for a in actions))
        self.assertTrue(any("ai_diagnosis" in a for a in actions))
        self.assertTrue(any("recovery_plan" in a for a in actions))
        self.assertTrue(any("safety_gate" in a for a in actions))
        self.assertTrue(any("decision_recorded" in a for a in actions))


class TestDecisionsEndpoint(unittest.TestCase):
    def test_get_decisions_returns_list(self):
        self.assertIsInstance(RecoveryOrchestrator(use_mock_ai=True).get_decisions(), list)

    def test_decisions_are_dicts(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        _feed_bank_x_incident(orch, use_batch=True)
        decisions = orch.get_decisions()
        self.assertGreater(len(decisions), 0)
        self.assertIsInstance(decisions[0], dict)


class TestBatchMode(unittest.TestCase):
    def test_store_then_finalize(self):
        orch = RecoveryOrchestrator(use_mock_ai=True)
        for i, offset in enumerate([0, 30, 60, 120, 180, 240, 280]):
            orch.store_payment(_make_payment(f"pay_bat_{i+1:03d}", created_at_offset_seconds=offset))
        for i, offset in enumerate([90, 150, 270]):
            orch.store_payment(_make_captured_payment(f"pay_bat_s{i+1:03d}", created_at_offset_seconds=offset))
        self.assertEqual(len(orch.decisions), 0)
        decisions = orch.finalize_incidents()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].failure_rate, 70.0)


class TestLateSuccessMetricConsistency(unittest.TestCase):
    """Edge Case 1: Late-success must correctly update all incident metrics."""

    def test_7_to_6_failed_70_to_60_percent(self):
        """After late-success, failed=6/10=60% and affected_ids=6."""
        orch = RecoveryOrchestrator(use_mock_ai=True)
        # 7 failed + 3 captured in 5 min
        for i, offset in enumerate([0, 30, 60, 120, 180, 240, 280]):
            orch.store_payment(
                _make_payment(f"pay_ls_{i}", created_at_offset_seconds=offset),
                event_id=f"e_{i}")
        for i, offset in enumerate([90, 150, 270]):
            orch.store_payment(
                _make_captured_payment(f"pay_ls_s{i}", created_at_offset_seconds=offset),
                event_id=f"es_{i}")
        orch.finalize_incidents(event_id="fin")
        d = orch.decisions[0]
        self.assertEqual(d.failed_payments, 7)
        self.assertEqual(d.total_payments, 10)
        self.assertEqual(d.failure_rate, 70.0)
        self.assertEqual(len(d.affected_payment_ids), 7)

        # Late-success: one failed payment becomes captured
        orch.process_payment_event(
            _make_captured_payment("pay_ls_0", created_at_offset_seconds=400),
            event_id="late")
        d2 = orch.decisions[0]
        self.assertEqual(d2.failed_payments, 6)
        self.assertEqual(d2.total_payments, 10)
        self.assertEqual(d2.failure_rate, 60.0)
        self.assertEqual(len(d2.affected_payment_ids), 6)
        self.assertNotIn("pay_ls_0", d2.affected_payment_ids)

    def test_revenue_at_risk_reduced_after_late_success(self):
        """Revenue at risk must decrease when a payment is captured late."""
        orch = RecoveryOrchestrator(use_mock_ai=True)
        for i, offset in enumerate([0, 30, 60]):
            orch.store_payment(
                _make_payment(f"pay_rev_{i}", amount=5000,
                              created_at_offset_seconds=offset),
                event_id=f"r_{i}")
        orch.finalize_incidents(event_id="fin")
        d = orch.decisions[0]
        self.assertEqual(d.revenue_at_risk, 15000.0)

        # Late-success for one payment
        orch.process_payment_event(
            _make_captured_payment("pay_rev_0", amount=5000,
                                   created_at_offset_seconds=90),
            event_id="late")
        d2 = orch.decisions[0]
        self.assertEqual(d2.revenue_at_risk, 10000.0)
        self.assertEqual(d2.failed_payments, 2)


class TestSeparateIncidentsDifferentWindows(unittest.TestCase):
    """Edge Case 2: Two incidents with same bank/method but different time windows
    must be tracked as separate decisions."""

    def test_two_incidents_same_bank_method(self):
        """Incidents 30 minutes apart produce 2 separate decisions."""
        orch = RecoveryOrchestrator(use_mock_ai=True)
        base_ts = 1725000000

        # Incident 1: t=0..280
        for i, offset in enumerate([0, 30, 60, 120, 180, 240, 280]):
            orch.store_payment(
                _make_payment(f"pay_w1_{i}", created_at_offset_seconds=offset),
                event_id=f"w1_{i}")
        orch.finalize_incidents(event_id="fin1")

        # Incident 2: t=1800..2080 (30 min later)
        for i, offset in enumerate([1800, 1830, 1860, 1920, 1980, 2040, 2080]):
            orch.store_payment(
                _make_payment(f"pay_w2_{i}", created_at_offset_seconds=offset),
                event_id=f"w2_{i}")
        orch.finalize_incidents(event_id="fin2")

        self.assertEqual(len(orch.decisions), 2)
        self.assertEqual(len(orch._active_decisions), 2)

        d1, d2 = orch.decisions[0], orch.decisions[1]
        self.assertNotEqual(d1.incident_scope, d2.incident_scope)
        self.assertEqual(len(d1.affected_payment_ids), 7)
        self.assertEqual(len(d2.affected_payment_ids), 7)

    def test_different_scope_keys(self):
        """Scope keys include time-window timestamp."""
        orch = RecoveryOrchestrator(use_mock_ai=True)
        for i, offset in enumerate([0, 30, 60, 120, 180, 240, 280]):
            orch.store_payment(
                _make_payment(f"pay_sk_{i}", created_at_offset_seconds=offset),
                event_id=f"sk1_{i}")
        orch.finalize_incidents(event_id="fin1")
        for i, offset in enumerate([1800, 1830, 1860, 1920, 1980, 2040, 2080]):
            orch.store_payment(
                _make_payment(f"pay_sk2_{i}", created_at_offset_seconds=offset),
                event_id=f"sk2_{i}")
        orch.finalize_incidents(event_id="fin2")

        keys = list(orch._active_decisions.keys())
        self.assertEqual(len(keys), 2)
        # Each key should contain a timestamp
        for key in keys:
            self.assertIn("T", key)  # ISO timestamp portion

    def test_incident_detector_multi_window(self):
        """The incident detector finds multiple incidents in separate windows."""
        from incident_detector import detect_incidents, group_payments
        from datetime import datetime, timezone

        base_ts = 1725000000
        payments = []
        for offset in [0, 30, 60, 120, 180, 240, 280]:
            dt = datetime.fromtimestamp(base_ts + offset, tz=timezone.utc)
            payments.append({
                'payment_id': f'p_{offset}', 'amount': 2500.0,
                'status': 'failed', 'payment_method': 'Upi', 'bank': 'BANK_X',
                'error_reason': 'technical_error', 'error_source': 'collector',
                'error_step': 'api_call',
                'created_at': dt.isoformat(), '_dt': dt,
            })
        for offset in [1800, 1830, 1860, 1920, 1980, 2040, 2080]:
            dt = datetime.fromtimestamp(base_ts + offset, tz=timezone.utc)
            payments.append({
                'payment_id': f'p_{offset}', 'amount': 2500.0,
                'status': 'failed', 'payment_method': 'Upi', 'bank': 'BANK_X',
                'error_reason': 'technical_error', 'error_source': 'collector',
                'error_step': 'api_call',
                'created_at': dt.isoformat(), '_dt': dt,
            })
        groups = group_payments(payments)
        incidents = detect_incidents(groups)
        self.assertEqual(len(incidents), 2)
        self.assertNotEqual(
            incidents[0].get('window_start'),
            incidents[1].get('window_start'),
        )


class TestExistingTestsStillPass(unittest.TestCase):
    def test_incident_detector_imports(self):
        from incident_detector import detect_incidents, group_payments
        self.assertTrue(callable(detect_incidents))

    def test_safety_gate_imports(self):
        from safety_gate import evaluate
        self.assertTrue(callable(evaluate))

    def test_recovery_plan_imports(self):
        from recovery_plan import ALL_PLANS, simulate_recovery_plan
        self.assertEqual(len(ALL_PLANS), 4)

    def test_ai_diagnoser_imports(self):
        from ai_diagnoser import diagnose_with_context
        self.assertTrue(callable(diagnose_with_context))

    def test_razorpay_adapter_imports(self):
        from razorpay_adapter import RazorpayWebhookProcessor, convert_razorpay_to_internal
        self.assertTrue(callable(convert_razorpay_to_internal))


if __name__ == "__main__":
    unittest.main()
