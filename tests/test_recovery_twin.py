"""
Tests for the Incident Recovery Twin.

Uses Python's built-in unittest module — no extra dependencies.
"""

import os
import sys
import unittest

# Import from backend
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "backend"))

from recovery_twin import (
    RecoveryOption,
    STRATEGIES,
    analyze_incident,
    run_twin_for_incident,
)

# Import incident detector for integration tests
from incident_detector import detect_incidents, group_payments, read_payments

SAMPLE_CSV = os.path.join(
    os.path.dirname(__file__), os.pardir, "data", "sample_payments.csv"
)

# ---------- FIXTURES ----------

# A temporary bank-side technical incident (like the BANK_X UPI cluster)
TECHNICAL_INCIDENT = {
    "bank": "BANK_X",
    "payment_method": "UPI",
    "error_reason": "technical_error",
    "total_payments": 10,
    "failed_payments": 7,
    "failure_rate": 70.0,
    "revenue_at_risk": 19750.00,
}

# A user error incident (wrong OTP)
OTP_INCIDENT = {
    "bank": "HDFC",
    "payment_method": "UPI",
    "error_reason": "incorrect_otp",
    "total_payments": 8,
    "failed_payments": 5,
    "failure_rate": 62.5,
    "revenue_at_risk": 8400.00,
}

# An insufficient balance incident
BALANCE_INCIDENT = {
    "bank": "SBI",
    "payment_method": "Card",
    "error_reason": "insufficient_balance",
    "total_payments": 12,
    "failed_payments": 6,
    "failure_rate": 50.0,
    "revenue_at_risk": 15000.00,
}

# A timeout incident (also temporary)
TIMEOUT_INCIDENT = {
    "bank": "ICICI",
    "payment_method": "NetBanking",
    "error_reason": "timeout_error",
    "total_payments": 10,
    "failed_payments": 8,
    "failure_rate": 80.0,
    "revenue_at_risk": 25000.00,
}


# ======================================================================
# 1. All four strategies are generated
# ======================================================================

class TestAllStrategiesPresent(unittest.TestCase):
    """Every analysis must produce exactly 4 strategies."""

    def test_four_strategies_returned(self):
        options = analyze_incident(TECHNICAL_INCIDENT)
        self.assertEqual(len(options), 4)

    def test_all_strategy_names_present(self):
        options = analyze_incident(TECHNICAL_INCIDENT)
        names = {o.strategy for o in options}
        for strategy in STRATEGIES:
            self.assertIn(strategy, names)


# ======================================================================
# 2. Scores stay between 0 and 100
# ======================================================================

class TestScoreBounds(unittest.TestCase):
    """Scores must always be in [0, 100] for any incident type."""

    def test_technical_incident_scores_valid(self):
        for opt in analyze_incident(TECHNICAL_INCIDENT):
            self.assertGreaterEqual(opt.score, 0)
            self.assertLessEqual(opt.score, 100)

    def test_otp_incident_scores_valid(self):
        for opt in analyze_incident(OTP_INCIDENT):
            self.assertGreaterEqual(opt.score, 0)
            self.assertLessEqual(opt.score, 100)

    def test_balance_incident_scores_valid(self):
        for opt in analyze_incident(BALANCE_INCIDENT):
            self.assertGreaterEqual(opt.score, 0)
            self.assertLessEqual(opt.score, 100)

    def test_timeout_incident_scores_valid(self):
        for opt in analyze_incident(TIMEOUT_INCIDENT):
            self.assertGreaterEqual(opt.score, 0)
            self.assertLessEqual(opt.score, 100)


# ======================================================================
# 3. Temporary technical incident prefers WAIT_AND_RECHECK
# ======================================================================

class TestTemporaryIncidentPreference(unittest.TestCase):
    """A bank-side technical error should make WAIT_AND_RECHECK the top choice."""

    def test_wait_and_recheck_is_top_for_technical_error(self):
        options = analyze_incident(TECHNICAL_INCIDENT)
        self.assertEqual(options[0].strategy, "WAIT_AND_RECHECK")

    def test_wait_and_recheck_scores_high_for_technical_error(self):
        options = analyze_incident(TECHNICAL_INCIDENT)
        wait = next(o for o in options if o.strategy == "WAIT_AND_RECHECK")
        self.assertGreaterEqual(wait.score, 80)

    def test_timeout_also_prefers_wait(self):
        options = analyze_incident(TIMEOUT_INCIDENT)
        self.assertEqual(options[0].strategy, "WAIT_AND_RECHECK")


# ======================================================================
# 4. Final recommendation is the highest-scoring option
# ======================================================================

class TestRecommendationIsHighest(unittest.TestCase):
    """The first option in the returned list must have the highest score."""

    def test_sorted_descending(self):
        for incident in [TECHNICAL_INCIDENT, OTP_INCIDENT, BALANCE_INCIDENT]:
            options = analyze_incident(incident)
            scores = [o.score for o in options]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_top_score_matches_max(self):
        options = analyze_incident(TECHNICAL_INCIDENT)
        self.assertEqual(options[0].score, max(o.score for o in options))


# ======================================================================
# 5. Every option contains an explanation
# ======================================================================

class TestExplanationsPresent(unittest.TestCase):
    """Each RecoveryOption must have a non-empty explanation string."""

    def test_all_explanations_non_empty(self):
        for incident in [TECHNICAL_INCIDENT, OTP_INCIDENT, BALANCE_INCIDENT]:
            options = analyze_incident(incident)
            for opt in options:
                self.assertIsInstance(opt.explanation, str)
                self.assertGreater(len(opt.explanation), 0)


# ======================================================================
# 6. Customer friction and duplicate-payment risk are present
# ======================================================================

class TestFrictionAndRisk(unittest.TestCase):
    """Every option must have customer_friction and duplicate_risk set."""

    VALID_FRICTION = {"Low", "Medium", "High"}
    VALID_RISK = {"Low", "Medium", "High"}

    def test_friction_is_valid(self):
        for incident in [TECHNICAL_INCIDENT, OTP_INCIDENT, BALANCE_INCIDENT]:
            for opt in analyze_incident(incident):
                self.assertIn(opt.customer_friction, self.VALID_FRICTION)

    def test_duplicate_risk_is_valid(self):
        for incident in [TECHNICAL_INCIDENT, OTP_INCIDENT, BALANCE_INCIDENT]:
            for opt in analyze_incident(incident):
                self.assertIn(opt.duplicate_risk, self.VALID_RISK)

    def test_wait_and_recheck_has_low_friction(self):
        """WAIT_AND_RECHECK should be low friction — it asks nothing of the customer."""
        options = analyze_incident(TECHNICAL_INCIDENT)
        wait = next(o for o in options if o.strategy == "WAIT_AND_RECHECK")
        self.assertEqual(wait.customer_friction, "Low")


# ======================================================================
# 7. Integration with incident_detector
# ======================================================================

class TestIntegrationWithDetector(unittest.TestCase):
    """Recovery Twin accepts incident dicts from the Incident Detector."""

    def test_twin_works_with_detector_output(self):
        """Detect incidents from the sample CSV, then analyze the first one."""
        payments = read_payments(SAMPLE_CSV)
        groups = group_payments(payments)
        incidents = detect_incidents(groups, time_window_minutes=60)

        # The sample CSV should have at least one incident with a wide window
        self.assertGreater(len(incidents), 0)

        # Analyze the first incident
        options = analyze_incident(incidents[0])
        self.assertEqual(len(options), 4)
        self.assertGreaterEqual(options[0].score, 0)
        self.assertLessEqual(options[0].score, 100)

    def test_run_twin_for_incident_returns_options(self):
        """run_twin_for_incident should return a list of RecoveryOption."""
        options = run_twin_for_incident(TECHNICAL_INCIDENT)
        self.assertIsInstance(options, list)
        self.assertEqual(len(options), 4)
        for opt in options:
            self.assertIsInstance(opt, RecoveryOption)


# ======================================================================
# RUN
# ======================================================================

if __name__ == "__main__":
    unittest.main()
