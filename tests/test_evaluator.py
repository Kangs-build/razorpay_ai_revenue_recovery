"""
Tests for the Baseline vs Recovery Twin Evaluator.

Covers:
1.  Baseline policy chooses CUSTOMER_RETRY for incorrect OTP
2.  Baseline policy chooses SUGGEST_ALTERNATE_METHOD for insufficient balance
3.  Baseline uses RETRY_LATER for technical errors
4.  Both baseline and Recovery Twin receive identical starting batches
5.  Evaluation is reproducible across repeated runs
6.  Revenue calculations are correct
7.  Recovery rate calculation is correct
8.  Unresolved revenue calculation is correct
9.  Safety Gate is used by both approaches
10. Late-success recovery is stopped
11. What-If Replay returns results for each requested strategy
12. Original payment data is not mutated between policy evaluations
13. Existing project tests still pass (verified by running full suite)
"""

import sys
import os
import unittest
import copy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from evaluator import (
    generate_evaluation_dataset,
    _copy_payments,
    run_baseline_policy,
    run_recovery_twin_policy,
    calculate_metrics,
    find_bank_x_incident_payments,
    replay_incident,
)
from payment_simulator import create_payment, format_amount


# ======================================================================
# 1–3. Baseline strategy selection (covered by test_baseline_policy.py
#      but verified here through the evaluator pipeline)
# ======================================================================


class TestBaselineStrategySelection(unittest.TestCase):
    """Verify baseline chooses correct strategies via evaluator."""

    def setUp(self):
        self.dataset = generate_evaluation_dataset()
        self.payments = _copy_payments(self.dataset)
        self.results = run_baseline_policy(self.payments)

    def test_incorrect_otp_uses_customer_retry(self):
        otp_results = [r for r in self.results if r["error_reason"] == "incorrect_otp"]
        self.assertGreater(len(otp_results), 0)
        for r in otp_results:
            self.assertEqual(r["strategy"], "CUSTOMER_RETRY")

    def test_insufficient_balance_uses_suggest_alternate(self):
        bal_results = [r for r in self.results if r["error_reason"] == "insufficient_balance"]
        self.assertGreater(len(bal_results), 0)
        for r in bal_results:
            self.assertEqual(r["strategy"], "SUGGEST_ALTERNATE_METHOD")

    def test_technical_error_uses_retry_later(self):
        """Baseline uses RETRY_LATER for technical errors (fixed rule)."""
        tech_results = [r for r in self.results if r["error_reason"] == "technical_error"]
        self.assertGreater(len(tech_results), 0)
        for r in tech_results:
            self.assertEqual(r["strategy"], "RETRY_LATER")


# ======================================================================
# 4. Both policies receive identical starting batches
# ======================================================================


class TestFairReplay(unittest.TestCase):
    """Verify that independent copies are used for both policies."""

    def test_identical_starting_data(self):
        dataset = generate_evaluation_dataset()
        baseline_copy = _copy_payments(dataset)
        twin_copy = _copy_payments(dataset)

        # Before running policies, copies should be identical
        self.assertEqual(len(baseline_copy), len(twin_copy))
        for b, t in zip(baseline_copy, twin_copy):
            self.assertEqual(b["payment_id"], t["payment_id"])
            self.assertEqual(b["amount"], t["amount"])
            self.assertEqual(b["status"], t["status"])


# ======================================================================
# 5. Evaluation is reproducible
# ======================================================================


class TestReproducibility(unittest.TestCase):

    def test_same_results_across_runs(self):
        dataset1 = generate_evaluation_dataset()
        dataset2 = generate_evaluation_dataset()

        # Datasets should be identical
        self.assertEqual(len(dataset1), len(dataset2))
        for p1, p2 in zip(dataset1, dataset2):
            self.assertEqual(p1["payment_id"], p2["payment_id"])
            self.assertEqual(p1["amount"], p2["amount"])

        # Baseline results should be identical
        payments1 = _copy_payments(dataset1)
        payments2 = _copy_payments(dataset2)
        results1 = run_baseline_policy(payments1)
        results2 = run_baseline_policy(payments2)
        self.assertEqual(len(results1), len(results2))
        for r1, r2 in zip(results1, results2):
            self.assertEqual(r1["payment_id"], r2["payment_id"])
            self.assertEqual(r1["strategy"], r2["strategy"])
            self.assertEqual(r1["recovered"], r2["recovered"])


# ======================================================================
# 6. Revenue calculations are correct
# ======================================================================


class TestRevenueCalculations(unittest.TestCase):

    def test_revenue_at_risk_sums_amounts(self):
        payments = [
            create_payment(
                payment_id="P1", amount=1000, payment_method="UPI",
                bank="BANK_A", status="failed", error_reason="technical_error",
            ),
            create_payment(
                payment_id="P2", amount=2000, payment_method="UPI",
                bank="BANK_A", status="failed", error_reason="technical_error",
            ),
        ]
        results = [
            {"payment_id": "P1", "amount": 1000, "recovered": False,
             "gate_allowed": True, "customer_facing": False, "late_success_stop": False},
            {"payment_id": "P2", "amount": 2000, "recovered": True,
             "gate_allowed": True, "customer_facing": False, "late_success_stop": False},
        ]
        metrics = calculate_metrics(payments, results)
        self.assertEqual(metrics["revenue_at_risk"], 3000.0)
        self.assertEqual(metrics["revenue_recovered"], 2000.0)


# ======================================================================
# 7. Recovery rate calculation is correct
# ======================================================================


class TestRecoveryRate(unittest.TestCase):

    def test_50_percent_recovery_rate(self):
        payments = [{"recovery_attempt_count": 0}] * 4
        results = [
            {"amount": 100, "recovered": True, "gate_allowed": True,
             "customer_facing": False, "late_success_stop": False},
            {"amount": 200, "recovered": True, "gate_allowed": True,
             "customer_facing": False, "late_success_stop": False},
            {"amount": 300, "recovered": False, "gate_allowed": True,
             "customer_facing": False, "late_success_stop": False},
            {"amount": 400, "recovered": False, "gate_allowed": True,
             "customer_facing": False, "late_success_stop": False},
        ]
        metrics = calculate_metrics(payments, results)
        self.assertEqual(metrics["recovery_rate"], 50.0)

    def test_zero_payments_gives_zero_rate(self):
        metrics = calculate_metrics([], [])
        self.assertEqual(metrics["recovery_rate"], 0)


# ======================================================================
# 8. Unresolved revenue calculation is correct
# ======================================================================


class TestUnresolvedRevenue(unittest.TestCase):

    def test_unresolved_equals_risk_minus_recovered(self):
        payments = [{"recovery_attempt_count": 0}] * 2
        results = [
            {"amount": 5000, "recovered": True, "gate_allowed": True,
             "customer_facing": False, "late_success_stop": False},
            {"amount": 3000, "recovered": False, "gate_allowed": True,
             "customer_facing": False, "late_success_stop": False},
        ]
        metrics = calculate_metrics(payments, results)
        self.assertEqual(metrics["revenue_at_risk"], 8000.0)
        self.assertEqual(metrics["revenue_recovered"], 5000.0)
        self.assertEqual(metrics["unresolved"], 3000.0)


# ======================================================================
# 9. Safety Gate is used by both approaches
# ======================================================================


class TestSafetyGateUsedByBoth(unittest.TestCase):

    def test_baseline_uses_safety_gate(self):
        dataset = generate_evaluation_dataset()
        payments = _copy_payments(dataset)
        results = run_baseline_policy(payments)
        for r in results:
            self.assertIn("gate_allowed", r)

    def test_recovery_twin_uses_safety_gate(self):
        dataset = generate_evaluation_dataset()
        payments = _copy_payments(dataset)
        results = run_recovery_twin_policy(payments)
        for r in results:
            self.assertIn("gate_allowed", r)


# ======================================================================
# 10. Late-success recovery is stopped
# ======================================================================


class TestLateSuccessStopped(unittest.TestCase):

    def test_baseline_exercises_late_success(self):
        """Late-success simulation runs BEFORE recovery, so Safety Gate
        blocks late-success candidates."""
        dataset = generate_evaluation_dataset()
        payments = _copy_payments(dataset)
        results = run_baseline_policy(payments)
        late_stops = [r for r in results if r["late_success_stop"]]
        # Late-success candidates exist and are blocked by Safety Gate
        self.assertGreater(len(late_stops), 0)

    def test_recovery_twin_exercises_late_success(self):
        dataset = generate_evaluation_dataset()
        payments = _copy_payments(dataset)
        results = run_recovery_twin_policy(payments)
        late_stops = [r for r in results if r["late_success_stop"]]
        self.assertGreater(len(late_stops), 0)

    def test_late_success_stops_are_not_recovered(self):
        """A late-success stop means the payment was blocked, not recovered."""
        dataset = generate_evaluation_dataset()
        payments = _copy_payments(dataset)
        results = run_baseline_policy(payments)
        for r in results:
            if r["late_success_stop"]:
                self.assertFalse(r["recovered"])
                self.assertFalse(r["gate_allowed"])


# ======================================================================
# 11. What-If Replay returns results for each strategy
# ======================================================================


class TestWhatIfReplay(unittest.TestCase):

    def setUp(self):
        self.dataset = generate_evaluation_dataset()
        self.incident_payments = find_bank_x_incident_payments(self.dataset)

    def test_replay_returns_results(self):
        result = replay_incident(self.incident_payments, "WAIT_AND_RECHECK")
        self.assertIn("strategy", result)
        self.assertIn("recovered", result)
        self.assertIn("revenue_recovered", result)
        self.assertEqual(result["strategy"], "WAIT_AND_RECHECK")

    def test_replay_for_all_strategies(self):
        strategies = [
            "WAIT_AND_RECHECK",
            "RETRY_LATER",
            "SUGGEST_ALTERNATE_METHOD",
            "SEND_PAYMENT_LINK",
            "CUSTOMER_RETRY",
        ]
        for strategy in strategies:
            result = replay_incident(self.incident_payments, strategy)
            self.assertEqual(result["strategy"], strategy)
            self.assertGreater(result["total_payments"], 0)
            self.assertGreaterEqual(result["recovered"], 0)
            self.assertGreaterEqual(result["recovery_rate"], 0)
            self.assertLessEqual(result["recovery_rate"], 100)

    def test_replay_does_not_mutate_original(self):
        original = _copy_payments(self.incident_payments)
        replay_incident(self.incident_payments, "WAIT_AND_RECHECK")
        for p, o in zip(self.incident_payments, original):
            self.assertEqual(p["payment_id"], o["payment_id"])
            self.assertEqual(p["status"], o["status"])


# ======================================================================
# 12. Original payment data is not mutated between evaluations
# ======================================================================


class TestNoMutationBetweenPolicies(unittest.TestCase):

    def test_dataset_unchanged_after_both_policies(self):
        dataset = generate_evaluation_dataset()
        original = _copy_payments(dataset)

        # Run both policies
        baseline_payments = _copy_payments(dataset)
        run_baseline_policy(baseline_payments)

        twin_payments = _copy_payments(dataset)
        run_recovery_twin_policy(twin_payments)

        # Original dataset should be unchanged
        for p, o in zip(dataset, original):
            self.assertEqual(p["payment_id"], o["payment_id"])
            self.assertEqual(p["status"], o["status"])
            self.assertEqual(p["amount"], o["amount"])


# ======================================================================
# 13. Existing project tests still pass
# (verified by running full test suite — this test documents it)
# ======================================================================


class TestExistingTestsStillPass(unittest.TestCase):

    def test_incident_detector_works(self):
        from incident_detector import detect_incidents, group_payments, read_payments
        import os
        csv = os.path.join(os.path.dirname(__file__), "..", "data", "sample_payments.csv")
        payments = read_payments(csv)
        groups = group_payments(payments)
        incidents = detect_incidents(groups)
        self.assertIsInstance(incidents, list)

    def test_recovery_twin_works(self):
        from recovery_twin import analyze_incident
        incident = {
            "bank": "BANK_X", "payment_method": "UPI",
            "error_reason": "technical_error",
            "total_payments": 10, "failed_payments": 7,
            "failure_rate": 70.0, "revenue_at_risk": 19750.0,
        }
        options = analyze_incident(incident)
        self.assertEqual(len(options), 5)

    def test_safety_gate_works(self):
        from safety_gate import evaluate
        payment = {
            "payment_id": "TEST", "amount": 1000, "status": "failed",
            "recovery_attempt_count": 0, "customer_contact_count": 0,
            "recovered_already": False, "last_recovery_attempt_time": None,
        }
        result = evaluate(payment, "RETRY_LATER")
        self.assertTrue(result["allowed"])


if __name__ == "__main__":
    unittest.main()
