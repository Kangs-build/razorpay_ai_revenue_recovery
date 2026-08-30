"""
Tests for Payment & Recovery Simulator — Version 1

Covers:
1.  Normal-success scenario
2.  Incorrect-OTP scenario
3.  Insufficient-balance scenario
4.  Temporary BANK_X incident generation
5.  Incident Detector detects the generated incident
6.  Late-success payment changes from failed to captured
7.  Recovery stops after late success
8.  Payment amounts remain consistent
9.  Existing Incident Detector tests still pass (tested by running full suite)
10. Existing Recovery Twin tests still pass (tested by running full suite)
11. Existing Safety Gate tests still pass (tested by running full suite)
"""

import sys
import os
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from payment_simulator import (
    create_payment,
    scenario_normal_success,
    scenario_incorrect_otp,
    scenario_insufficient_balance,
    scenario_temporary_bank_failure,
    scenario_late_success,
    generate_bank_incident,
    simulate_recovery,
    simulate_late_success,
    run_pipeline,
)
from incident_detector import detect_incidents, group_payments
from recovery_twin import analyze_incident
from safety_gate import evaluate as safety_evaluate


# ── Test 1: Normal-success scenario ──


class TestNormalSuccessScenario(unittest.TestCase):
    """Verify that the normal-success scenario creates a successful payment."""

    def test_status_is_success(self):
        payment = scenario_normal_success()
        self.assertEqual(payment["status"], "success")

    def test_no_error_reason(self):
        payment = scenario_normal_success()
        self.assertEqual(payment["error_reason"], "none")


# ── Test 2: Incorrect-OTP scenario ──


class TestIncorrectOtpScenario(unittest.TestCase):
    """Verify that the incorrect-OTP scenario has the correct failure reason."""

    def test_status_is_failed(self):
        payment = scenario_incorrect_otp()
        self.assertEqual(payment["status"], "failed")

    def test_error_reason(self):
        payment = scenario_incorrect_otp()
        self.assertEqual(payment["error_reason"], "incorrect_otp")


# ── Test 3: Insufficient-balance scenario ──


class TestInsufficientBalanceScenario(unittest.TestCase):
    """Verify that the insufficient-balance scenario has the correct failure."""

    def test_status_is_failed(self):
        payment = scenario_insufficient_balance()
        self.assertEqual(payment["status"], "failed")

    def test_error_reason(self):
        payment = scenario_insufficient_balance()
        self.assertEqual(payment["error_reason"], "insufficient_balance")


# ── Test 4: Bank incident generation ──


class TestBankIncidentGeneration(unittest.TestCase):
    """Verify that generate_bank_incident creates correct data."""

    def test_generates_10_payments(self):
        payments = generate_bank_incident()
        self.assertEqual(len(payments), 10)

    def test_all_are_bank_x_upi(self):
        payments = generate_bank_incident()
        for p in payments:
            self.assertEqual(p["bank"], "BANK_X")
            self.assertEqual(p["payment_method"], "UPI")

    def test_7_failures_3_successes(self):
        payments = generate_bank_incident()
        failed = [p for p in payments if p["status"] == "failed"]
        succeeded = [p for p in payments if p["status"] == "success"]
        self.assertEqual(len(failed), 7)
        self.assertEqual(len(succeeded), 3)

    def test_all_failures_are_technical_error(self):
        payments = generate_bank_incident()
        for p in payments:
            if p["status"] == "failed":
                self.assertEqual(p["error_reason"], "technical_error")

    def test_within_5_minutes(self):
        payments = generate_bank_incident()
        timestamps = [
            datetime.fromisoformat(p["timestamp"]) for p in payments
        ]
        span = (max(timestamps) - min(timestamps)).total_seconds()
        self.assertLessEqual(span, 5 * 60)


# ── Test 5: Incident Detector detects the generated incident ──


class TestIncidentDetectorDetectsGenerated(unittest.TestCase):
    """Verify that the Incident Detector flags the generated BANK_X incident."""

    def setUp(self):
        self.payments = generate_bank_incident()
        self.groups = group_payments(self.payments)
        self.incidents = detect_incidents(self.groups)

    def test_at_least_one_incident(self):
        self.assertGreater(len(self.incidents), 0)

    def test_incident_is_bank_x_upi(self):
        inc = self.incidents[0]
        self.assertEqual(inc["bank"], "BANK_X")
        self.assertEqual(inc["payment_method"], "UPI")

    def test_total_payments_is_10(self):
        inc = self.incidents[0]
        self.assertEqual(inc["total_payments"], 10)

    def test_failed_payments_is_7(self):
        inc = self.incidents[0]
        self.assertEqual(inc["failed_payments"], 7)

    def test_failure_rate_is_70_percent(self):
        inc = self.incidents[0]
        self.assertAlmostEqual(inc["failure_rate"], 70.0, places=1)


# ── Test 6: Late-success payment ──


class TestLateSuccess(unittest.TestCase):
    """Verify that a late-success payment transitions from failed to captured."""

    def test_becomes_captured(self):
        payment = scenario_late_success()
        self.assertEqual(payment["status"], "failed")
        simulate_late_success(payment)
        self.assertEqual(payment["status"], "captured")

    def test_already_captured_stays_captured(self):
        payment = create_payment(
            payment_id="PAY_123",
            amount=1000,
            payment_method="UPI",
            bank="BANK_X",
            status="captured",
        )
        simulate_late_success(payment)
        self.assertEqual(payment["status"], "captured")


# ── Test 7: Recovery stops after late success ──


class TestRecoveryStopsAfterLateSuccess(unittest.TestCase):
    """Verify that the Safety Gate blocks recovery after a late success."""

    def test_safety_gate_blocks_after_late_success(self):
        payment = scenario_late_success()
        self.assertEqual(payment["status"], "failed")

        # Before late success: recovery is allowed
        gate_before = safety_evaluate(payment, "WAIT_AND_RECHECK")
        self.assertTrue(gate_before["allowed"])

        # Simulate late success
        simulate_late_success(payment)
        self.assertEqual(payment["status"], "captured")

        # After late success: recovery is blocked
        gate_after = safety_evaluate(payment, "WAIT_AND_RECHECK")
        self.assertFalse(gate_after["allowed"])

    def test_recovery_not_continued_on_captured(self):
        """Simulate recovery on a captured payment — should be a no-op."""
        payment = create_payment(
            payment_id="PAY_777",
            amount=2000,
            payment_method="UPI",
            bank="BANK_X",
            status="captured",
        )
        original_status = payment["status"]
        simulate_recovery(payment, "WAIT_AND_RECHECK")
        self.assertEqual(payment["status"], original_status)


# ── Test 8: Payment amounts remain consistent ──


class TestAmountConsistency(unittest.TestCase):
    """Verify that amounts don't change during simulation."""

    def test_amount_unchanged_after_recovery(self):
        payment = scenario_temporary_bank_failure()
        original_amount = payment["amount"]
        simulate_recovery(payment, "WAIT_AND_RECHECK")
        self.assertEqual(payment["amount"], original_amount)

    def test_amount_unchanged_after_late_success(self):
        payment = scenario_late_success()
        original_amount = payment["amount"]
        simulate_late_success(payment)
        self.assertEqual(payment["amount"], original_amount)

    def test_amounts_in_bank_incident(self):
        payments = generate_bank_incident()
        for p in payments:
            self.assertIsInstance(p["amount"], float)
            self.assertGreater(p["amount"], 0)


# ── Test 9: Create payment helper ──


class TestCreatePayment(unittest.TestCase):
    """Verify that create_payment produces valid payment dicts."""

    def test_has_all_required_fields(self):
        p = create_payment(
            payment_id="PAY_TEST",
            amount=100,
            payment_method="UPI",
            bank="BANK_A",
            status="success",
        )
        required = [
            "payment_id",
            "amount",
            "timestamp",
            "status",
            "payment_method",
            "bank",
            "error_source",
            "error_step",
            "error_reason",
            "recovery_attempt_count",
            "customer_contact_count",
            "recovered_already",
            "last_recovery_attempt_time",
        ]
        for field in required:
            self.assertIn(field, p)

    def test_default_timestamp_is_set(self):
        p = create_payment(
            payment_id="PAY_TS",
            amount=100,
            payment_method="UPI",
            bank="BANK_A",
            status="success",
        )
        self.assertIsNotNone(p["timestamp"])
        # Should be parseable
        datetime.fromisoformat(p["timestamp"])


# ── Test 10: Simulate recovery — strategy behaviors ──


class TestSimulateRecovery(unittest.TestCase):
    """Test the simulated recovery action behaviors."""

    def test_wait_and_recheck_may_resolve_technical(self):
        """Technical error payments may resolve after wait_and_recheck."""
        payment = create_payment(
            payment_id="PAY_002",  # even last digit → resolves
            amount=2000,
            payment_method="UPI",
            bank="BANK_X",
            status="failed",
            error_reason="technical_error",
        )
        simulate_recovery(payment, "WAIT_AND_RECHECK")
        self.assertEqual(payment["status"], "captured")

    def test_wait_and_recheck_increments_count(self):
        payment = create_payment(
            payment_id="PAY_001",
            amount=2000,
            payment_method="UPI",
            bank="BANK_X",
            status="failed",
            error_reason="technical_error",
        )
        simulate_recovery(payment, "WAIT_AND_RECHECK")
        self.assertEqual(payment["recovery_attempt_count"], 1)

    def test_incorrect_otp_not_resolved_by_wait(self):
        """Incorrect OTP should not resolve by waiting."""
        payment = create_payment(
            payment_id="PAY_OTP",
            amount=1000,
            payment_method="Card",
            bank="ICICI",
            status="failed",
            error_reason="incorrect_otp",
        )
        simulate_recovery(payment, "WAIT_AND_RECHECK")
        self.assertEqual(payment["status"], "failed")

    def test_insufficient_balance_resolved_by_alternate(self):
        """Insufficient balance may resolve with alternate method."""
        payment = create_payment(
            payment_id="PAY_BAL",
            amount=3000,
            payment_method="NetBanking",
            bank="SBI",
            status="failed",
            error_reason="insufficient_balance",
        )
        simulate_recovery(payment, "SUGGEST_ALTERNATE_METHOD")
        self.assertEqual(payment["status"], "captured")
        self.assertEqual(payment["payment_method"], "Card")

    def test_send_payment_link_increments_contact(self):
        payment = create_payment(
            payment_id="PAY_LINK",
            amount=1500,
            payment_method="UPI",
            bank="HDFC",
            status="failed",
            error_reason="incorrect_otp",
        )
        simulate_recovery(payment, "SEND_PAYMENT_LINK")
        self.assertEqual(payment["customer_contact_count"], 1)
        self.assertEqual(payment["status"], "failed")  # not auto-resolved


# ── Test 11: Full pipeline integration ──


class TestFullPipeline(unittest.TestCase):
    """Test the full pipeline: generate → detect → twin → gate → simulate."""

    def test_pipeline_produces_all_fields(self):
        payments = generate_bank_incident()
        groups = group_payments(payments)
        incidents = detect_incidents(groups)
        result = run_pipeline(incidents[0], payments)

        self.assertIn("incident", result)
        self.assertIn("options", result)
        self.assertIn("recommendation", result)
        self.assertIn("gate_result", result)
        self.assertIn("simulated_payment", result)

    def test_recommendation_is_strategy_name(self):
        payments = generate_bank_incident()
        groups = group_payments(payments)
        incidents = detect_incidents(groups)
        result = run_pipeline(incidents[0], payments)

        self.assertIn(result["recommendation"].strategy, [
            "WAIT_AND_RECHECK",
            "RETRY_LATER",
            "SUGGEST_ALTERNATE_METHOD",
            "SEND_PAYMENT_LINK",
            "CUSTOMER_RETRY",
        ])

    def test_gate_result_for_fresh_payment(self):
        """A fresh failed payment with no history should pass the gate."""
        payments = generate_bank_incident()
        groups = group_payments(payments)
        incidents = detect_incidents(groups)
        result = run_pipeline(incidents[0], payments)

        self.assertTrue(result["gate_result"]["allowed"])


if __name__ == "__main__":
    unittest.main()
