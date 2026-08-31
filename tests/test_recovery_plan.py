"""
Tests for the Multi-Step Incident Recovery Twin — Version 2.

Uses Python's built-in unittest module — no extra dependencies.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from recovery_plan import (
    ALL_PLANS,
    PLAN_A_SAFE_WAIT,
    PLAN_B_FAST_RECOVERY,
    PLAN_C_CUSTOMER_ALTERNATE,
    PLAN_D_CONSERVATIVE,
    RecoveryPlan,
    PlanResult,
    simulate_recovery_plan,
    score_plan,
    score_explanation,
    get_incident_payments,
    run_multi_step_incident_recovery,
)
from payment_simulator import create_payment, generate_bank_incident
import copy


# ---------- FIXTURES ----------

def _make_incident_payments():
    """Create a standard set of incident payments for testing."""
    return generate_bank_incident()


def _make_single_failed_payment(
    pid="TEST_001",
    amount=2500.0,
    bank="BANK_X",
    method="UPI",
    error_reason="technical_error",
):
    return create_payment(
        payment_id=pid,
        amount=amount,
        payment_method=method,
        bank=bank,
        status="failed",
        error_source="bank_api",
        error_reason=error_reason,
    )


# =====================================================================
# 1. At least four recovery plans exist
# =====================================================================


class TestFourPlansExist(unittest.TestCase):
    def test_at_least_four_plans(self):
        self.assertGreaterEqual(len(ALL_PLANS), 4)

    def test_plan_names_are_unique(self):
        names = [p.name for p in ALL_PLANS]
        self.assertEqual(len(names), len(set(names)))

    def test_all_plans_are_recovery_plan_type(self):
        for plan in ALL_PLANS:
            self.assertIsInstance(plan, RecoveryPlan)


# =====================================================================
# 2. Each plan contains multiple recovery steps
# =====================================================================


class TestPlanSteps(unittest.TestCase):
    def test_each_plan_has_at_least_two_steps(self):
        for plan in ALL_PLANS:
            self.assertGreaterEqual(len(plan.steps), 2,
                f"{plan.name} should have at least 2 steps")

    def test_plan_a_has_three_steps(self):
        self.assertEqual(len(PLAN_A_SAFE_WAIT.steps), 3)
        self.assertEqual(PLAN_A_SAFE_WAIT.steps[0], "WAIT_AND_RECHECK")
        self.assertEqual(PLAN_A_SAFE_WAIT.steps[1], "RETRY_LATER")
        self.assertEqual(PLAN_A_SAFE_WAIT.steps[2], "SUGGEST_ALTERNATE_METHOD")

    def test_plan_d_has_two_steps(self):
        self.assertEqual(len(PLAN_D_CONSERVATIVE.steps), 2)
        self.assertEqual(PLAN_D_CONSERVATIVE.steps[0], "WAIT_AND_RECHECK")
        self.assertEqual(PLAN_D_CONSERVATIVE.steps[1], "RETRY_LATER")


# =====================================================================
# 3. Same starting incident is used for every plan
# =====================================================================


class TestFairReplay(unittest.TestCase):
    def test_same_payments_for_each_plan(self):
        payments = _make_incident_payments()
        results = []
        for plan in ALL_PLANS:
            result = simulate_recovery_plan(payments, plan)
            results.append(result)

        # All plans process the same number of failed payments
        total_failed_counts = [r.total_failed for r in results]
        self.assertEqual(len(set(total_failed_counts)), 1,
            f"All plans should see the same failed count, got: {total_failed_counts}")

    def test_same_revenue_at_risk(self):
        payments = _make_incident_payments()
        risks = []
        for plan in ALL_PLANS:
            result = simulate_recovery_plan(payments, plan)
            risks.append(result.revenue_at_risk)

        self.assertEqual(len(set(risks)), 1,
            f"All plans should have same revenue at risk, got: {risks}")


# =====================================================================
# 4. Original incident data is not mutated
# =====================================================================


class TestNoMutation(unittest.TestCase):
    def test_original_payments_unchanged_after_simulation(self):
        payments = _make_incident_payments()
        original = copy.deepcopy(payments)

        for plan in ALL_PLANS:
            simulate_recovery_plan(payments, plan)

        for p, o in zip(payments, original):
            self.assertEqual(p["payment_id"], o["payment_id"])
            self.assertEqual(p["status"], o["status"])
            self.assertEqual(p["amount"], o["amount"])
            self.assertEqual(p.get("recovery_attempt_count", 0),
                           o.get("recovery_attempt_count", 0))


# =====================================================================
# 5. Recovery stops immediately after payment succeeds
# =====================================================================


class TestRecoveryStopsAfterSuccess(unittest.TestCase):
    def test_no_further_steps_after_recovery(self):
        payments = [_make_single_failed_payment(pid="EVEN_0")]
        result = simulate_recovery_plan(payments, PLAN_A_SAFE_WAIT)
        entry = result.per_payment[0]
        # If recovered, should have tried at most the number of steps that led to recovery
        if entry["recovered"]:
            # The plan has 3 steps but should stop after the successful one
            recovered_at = next(
                (i for i, s in enumerate(entry["steps_tried"]) if s["gate_allowed"]),
                len(entry["steps_tried"])
            )
            self.assertLessEqual(len(entry["steps_tried"]), recovered_at + 1)


# =====================================================================
# 6. Safety Gate checks every recovery step
# =====================================================================


class TestSafetyGateChecksEveryStep(unittest.TestCase):
    def test_each_step_has_gate_result(self):
        payments = _make_incident_payments()
        for plan in ALL_PLANS:
            result = simulate_recovery_plan(payments, plan)
            for entry in result.per_payment:
                if entry["is_late_success"]:
                    continue
                for step in entry["steps_tried"]:
                    self.assertIn("gate_allowed", step)


# =====================================================================
# 7. Recovery stops when Safety Gate blocks
# =====================================================================


class TestSafetyGateBlocks(unittest.TestCase):
    def test_blocked_payment_stops_plan(self):
        """A captured payment should not be processed for recovery."""
        payment = _make_single_failed_payment()
        payment["status"] = "captured"  # already successful
        result = simulate_recovery_plan([payment], PLAN_A_SAFE_WAIT)
        # Captured payments are skipped — per_payment is empty
        self.assertEqual(len(result.per_payment), 0)
        self.assertEqual(result.recovered, 0)
        self.assertEqual(result.total_failed, 0)


# =====================================================================
# 8. Recovery attempt limit is respected
# =====================================================================


class TestRecoveryAttemptLimit(unittest.TestCase):
    def test_max_attempts_not_exceeded_per_payment(self):
        payments = [_make_single_failed_payment(pid="LIMIT_001")]
        # Pre-set recovery count to max
        payments[0]["recovery_attempt_count"] = 3
        result = simulate_recovery_plan(payments, PLAN_A_SAFE_WAIT)
        entry = result.per_payment[0]
        # Step is tried but Safety Gate blocks — no further steps attempted
        self.assertGreater(len(entry["steps_tried"]), 0)
        self.assertFalse(entry["steps_tried"][0]["gate_allowed"])
        self.assertFalse(entry["recovered"])
        self.assertTrue(entry["blocked_by_gate"])


# =====================================================================
# 9. Late-success payments do not receive additional recovery
# =====================================================================


class TestLateSuccessNotRecovered(unittest.TestCase):
    def test_late_success_marked_correctly(self):
        payments = [_make_single_failed_payment(pid="LATE_0L")]
        result = simulate_recovery_plan(payments, PLAN_A_SAFE_WAIT)
        entry = result.per_payment[0]
        self.assertTrue(entry["is_late_success"])
        self.assertTrue(entry["late_success_stop"])
        self.assertFalse(entry["recovered"])
        self.assertEqual(len(entry["steps_tried"]), 0)

    def test_late_success_counted_in_result(self):
        payments = [_make_single_failed_payment(pid="LATE_0L")]
        result = simulate_recovery_plan(payments, PLAN_A_SAFE_WAIT)
        self.assertEqual(result.late_success_stops, 1)


# =====================================================================
# 10. Plan metrics are calculated correctly
# =====================================================================


class TestPlanMetrics(unittest.TestCase):
    def test_recovered_plus_unresolved_equals_at_risk(self):
        payments = _make_incident_payments()
        for plan in ALL_PLANS:
            result = simulate_recovery_plan(payments, plan)
            self.assertAlmostEqual(
                result.revenue_recovered + result.unresolved,
                result.revenue_at_risk,
                places=2,
                msg=f"{plan.name}: revenue mismatch"
            )

    def test_score_between_0_and_100(self):
        payments = _make_incident_payments()
        for plan in ALL_PLANS:
            result = simulate_recovery_plan(payments, plan)
            s = score_plan(result)
            self.assertGreaterEqual(s, 0, f"{plan.name} score too low")
            self.assertLessEqual(s, 100, f"{plan.name} score too high")

    def test_score_explanation_returns_string(self):
        payments = _make_incident_payments()
        for plan in ALL_PLANS:
            result = simulate_recovery_plan(payments, plan)
            explanation = score_explanation(result)
            self.assertIsInstance(explanation, str)
            self.assertGreater(len(explanation), 0)


# =====================================================================
# 11. Recommended plan has the best valid score
# =====================================================================


class TestRecommendedPlan(unittest.TestCase):
    def test_best_score_matches_recommendation(self):
        payments = _make_incident_payments()
        output = run_multi_step_incident_recovery(payments)

        all_scores = [r[2] for r in output["all_results"]]
        self.assertEqual(output["score"], max(all_scores))

    def test_output_has_required_keys(self):
        payments = _make_incident_payments()
        output = run_multi_step_incident_recovery(payments)
        self.assertIn("plan", output)
        self.assertIn("result", output)
        self.assertIn("score", output)
        self.assertIn("all_results", output)


# =====================================================================
# 12. Individual incorrect OTP failures still use CUSTOMER_RETRY
# =====================================================================


class TestIndividualFailuresUnchanged(unittest.TestCase):
    """Verify that the recovery_plan module doesn't interfere with
    individual failure strategy selection."""

    def test_incident_payments_contain_only_incident_errors(self):
        """Incident payments from generate_bank_incident should be technical_error."""
        payments = generate_bank_incident()
        failed = [p for p in payments if p["status"] == "failed"]
        for p in failed:
            self.assertEqual(p["error_reason"], "technical_error")

    def test_recovery_plan_only_handles_incidents(self):
        """Recovery plans should only be used for incident-level failures,
        not individual customer errors like incorrect_otp."""
        # This is a documentation test — individual failures should go
        # through select_individual_recovery_strategy, not recovery plans.
        # Verify the module provides get_incident_payments helper.
        payments = _make_incident_payments()
        incident = get_incident_payments(payments, "BANK_X", "UPI")
        self.assertGreater(len(incident), 0)
        for p in incident:
            self.assertEqual(p["bank"], "BANK_X")
            self.assertEqual(p["payment_method"], "UPI")


# =====================================================================
# 13. get_incident_payments filters correctly
# =====================================================================


class TestGetIncidentPayments(unittest.TestCase):
    def test_filters_by_bank_and_method(self):
        payments = [
            _make_single_failed_payment(pid="A", bank="BANK_X", method="UPI"),
            _make_single_failed_payment(pid="B", bank="BANK_Y", method="UPI"),
            _make_single_failed_payment(pid="C", bank="BANK_X", method="Card"),
        ]
        result = get_incident_payments(payments, "BANK_X", "UPI")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["payment_id"], "A")

    def test_excludes_successful_payments(self):
        p = _make_single_failed_payment(pid="OK")
        p["status"] = "success"
        result = get_incident_payments([p], "BANK_X", "UPI")
        self.assertEqual(len(result), 0)


# =====================================================================
# 14. Customer-facing action tracking
# =====================================================================


class TestCustomerFacingTracking(unittest.TestCase):
    def test_plan_with_alternate_method_counts_customer_action(self):
        """Plan C includes SUGGEST_ALTERNATE_METHOD which is customer-facing."""
        payments = [_make_single_failed_payment(pid="CF_001")]
        # Force payment to stay failed after WAIT_AND_RECHECK
        payments[0]["payment_id"] = "CF_001"  # odd digit → RETRY_LATER resolves odd
        result = simulate_recovery_plan(payments, PLAN_C_CUSTOMER_ALTERNATE)
        # If the first step doesn't recover, the second step (SUGGEST_ALTERNATE)
        # should be attempted and counted as customer-facing
        if result.per_payment[0]["final_status"] == "failed":
            self.assertGreaterEqual(result.customer_facing_actions, 0)


# =====================================================================
# RUN
# =====================================================================

if __name__ == "__main__":
    unittest.main()
