"""
Tests for the Safety Gate.

Uses Python's built-in unittest module — no extra dependencies.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "backend"))

from safety_gate import (
    MAX_CUSTOMER_CONTACTS,
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_COOLDOWN_MINUTES,
    evaluate,
)

# Import existing components to verify they still pass
from incident_detector import detect_incidents, group_payments, read_payments
from recovery_twin import analyze_incident

SAMPLE_CSV = os.path.join(
    os.path.dirname(__file__), os.pardir, "data", "sample_payments.csv"
)


# ---------- FIXTURES ----------

def _base_payment(**overrides) -> dict:
    """Build a minimal payment dict, overriding defaults as needed."""
    base = {
        "payment_id": "PAY001",
        "amount": 2500.0,
        "status": "failed",
        "payment_method": "UPI",
        "recovery_attempt_count": 0,
        "customer_contact_count": 0,
        "recovered_already": False,
        "last_recovery_attempt_time": None,
    }
    base.update(overrides)
    return base


# ======================================================================
# 1. Successful/captured payments are blocked
# ======================================================================

class TestSuccessfulPaymentBlocked(unittest.TestCase):

    def test_captured_status_blocked(self):
        payment = _base_payment(status="captured")
        result = evaluate(payment, "SEND_PAYMENT_LINK")
        self.assertFalse(result["allowed"])
        self.assertTrue(any("successful" in r.lower() for r in result["reasons"]))

    def test_successful_status_blocked(self):
        payment = _base_payment(status="successful")
        result = evaluate(payment, "RETRY_LATER")
        self.assertFalse(result["allowed"])

    def test_failed_status_not_blocked_by_this_check(self):
        payment = _base_payment(status="failed")
        result = evaluate(payment, "WAIT_AND_RECHECK")
        # Should not be blocked by original-already-successful check
        blocked_by_success = any(
            "already successful" in r.lower() for r in result["reasons"]
        )
        self.assertFalse(blocked_by_success)


# ======================================================================
# 2. Already recovered payments are blocked
# ======================================================================

class TestAlreadyRecoveredBlocked(unittest.TestCase):

    def test_recovered_already_true(self):
        payment = _base_payment(recovered_already=True)
        result = evaluate(payment, "WAIT_AND_RECHECK")
        self.assertFalse(result["allowed"])
        self.assertTrue(any("already been recovered" in r.lower() for r in result["reasons"]))

    def test_recovered_already_false_not_blocked(self):
        payment = _base_payment(recovered_already=False)
        result = evaluate(payment, "WAIT_AND_RECHECK")
        blocked_by_recovery = any(
            "already recovered" in r.lower() for r in result["reasons"]
        )
        self.assertFalse(blocked_by_recovery)


# ======================================================================
# 3. Recovery attempts >= MAX are blocked
# ======================================================================

class TestMaxRecoveryAttempts(unittest.TestCase):

    def test_at_limit_blocked(self):
        payment = _base_payment(recovery_attempt_count=MAX_RECOVERY_ATTEMPTS)
        result = evaluate(payment, "RETRY_LATER")
        self.assertFalse(result["allowed"])
        self.assertTrue(any("maximum" in r.lower() for r in result["reasons"]))

    def test_over_limit_blocked(self):
        payment = _base_payment(recovery_attempt_count=5)
        result = evaluate(payment, "RETRY_LATER")
        self.assertFalse(result["allowed"])

    def test_under_limit_not_blocked(self):
        payment = _base_payment(recovery_attempt_count=MAX_RECOVERY_ATTEMPTS - 1)
        result = evaluate(payment, "RETRY_LATER")
        blocked_by_attempts = any(
            "maximum" in r.lower() for r in result["reasons"]
        )
        self.assertFalse(blocked_by_attempts)


# ======================================================================
# 4. Customer-contact limit blocks customer-facing actions
# ======================================================================

class TestCustomerContactLimit(unittest.TestCase):

    def test_send_payment_link_blocked_at_limit(self):
        payment = _base_payment(customer_contact_count=MAX_CUSTOMER_CONTACTS)
        result = evaluate(payment, "SEND_PAYMENT_LINK")
        self.assertFalse(result["allowed"])
        self.assertTrue(any("contact limit" in r.lower() for r in result["reasons"]))

    def test_suggest_alternate_blocked_at_limit(self):
        payment = _base_payment(customer_contact_count=MAX_CUSTOMER_CONTACTS)
        result = evaluate(payment, "SUGGEST_ALTERNATE_METHOD")
        self.assertFalse(result["allowed"])

    def test_below_limit_not_blocked(self):
        payment = _base_payment(customer_contact_count=MAX_CUSTOMER_CONTACTS - 1)
        result = evaluate(payment, "SEND_PAYMENT_LINK")
        blocked_by_contacts = any(
            "contact limit" in r.lower() for r in result["reasons"]
        )
        self.assertFalse(blocked_by_contacts)


# ======================================================================
# 5. WAIT_AND_RECHECK allowed even at contact limit
# ======================================================================

class TestWaitAndRecheckBypassesContactLimit(unittest.TestCase):

    def test_wait_allowed_at_contact_limit(self):
        """WAIT_AND_RECHECK doesn't contact the customer, so it should be allowed."""
        payment = _base_payment(
            customer_contact_count=MAX_CUSTOMER_CONTACTS,
            recovery_attempt_count=0,
            recovered_already=False,
        )
        result = evaluate(payment, "WAIT_AND_RECHECK")
        self.assertTrue(result["allowed"])

    def test_wait_allowed_even_at_high_contact_count(self):
        payment = _base_payment(customer_contact_count=100)
        result = evaluate(payment, "WAIT_AND_RECHECK")
        self.assertTrue(result["allowed"])


# ======================================================================
# 6. Cooldown rule
# ======================================================================

class TestCooldownPeriod(unittest.TestCase):

    def test_too_recent_blocked(self):
        """Attempt 5 minutes ago → blocked (cooldown is 10 min)."""
        payment = _base_payment(
            last_recovery_attempt_time=datetime.now() - timedelta(minutes=5)
        )
        result = evaluate(payment, "RETRY_LATER")
        self.assertFalse(result["allowed"])
        self.assertTrue(any("temporarily" in r.lower() for r in result["reasons"]))

    def test_just_outside_cooldown_allowed(self):
        """Attempt 11 minutes ago → allowed (past 10-min cooldown)."""
        payment = _base_payment(
            last_recovery_attempt_time=datetime.now() - timedelta(minutes=11)
        )
        result = evaluate(payment, "RETRY_LATER")
        blocked_by_cooldown = any(
            "temporarily" in r.lower() for r in result["reasons"]
        )
        self.assertFalse(blocked_by_cooldown)

    def test_no_previous_attempt_not_blocked(self):
        payment = _base_payment(last_recovery_attempt_time=None)
        result = evaluate(payment, "RETRY_LATER")
        blocked_by_cooldown = any(
            "temporarily" in r.lower() for r in result["reasons"]
        )
        self.assertFalse(blocked_by_cooldown)


# ======================================================================
# 7. Normal failed payment with no safety problems is allowed
# ======================================================================

class TestNormalPaymentAllowed(unittest.TestCase):

    def test_clean_failed_payment_allowed(self):
        payment = _base_payment()
        result = evaluate(payment, "WAIT_AND_RECHECK")
        self.assertTrue(result["allowed"])
        self.assertIn("All safety checks passed.", result["reasons"])

    def test_result_structure(self):
        payment = _base_payment()
        result = evaluate(payment, "WAIT_AND_RECHECK")
        self.assertIn("allowed", result)
        self.assertIn("strategy", result)
        self.assertIn("reasons", result)
        self.assertIsInstance(result["reasons"], list)


# ======================================================================
# 8. Integration with Incident Detector and Recovery Twin
# ======================================================================

class TestIntegrationWithExistingComponents(unittest.TestCase):
    """Existing components still work alongside the Safety Gate."""

    def test_incident_detector_still_works(self):
        payments = read_payments(SAMPLE_CSV)
        groups = group_payments(payments)
        incidents = detect_incidents(groups)
        self.assertIsInstance(incidents, list)

    def test_recovery_twin_still_works(self):
        incident = {
            "bank": "BANK_X",
            "payment_method": "UPI",
            "error_reason": "technical_error",
            "total_payments": 10,
            "failed_payments": 7,
            "failure_rate": 70.0,
            "revenue_at_risk": 19750.00,
        }
        options = analyze_incident(incident)
        self.assertEqual(len(options), 4)

    def test_full_pipeline_detector_to_twin_to_gate(self):
        """Detect → Twin recommend → Gate check (normal case should pass)."""
        payments = read_payments(SAMPLE_CSV)
        groups = group_payments(payments)
        incidents = detect_incidents(groups, time_window_minutes=60)
        self.assertGreater(len(incidents), 0)

        # Get Recovery Twin recommendation
        options = analyze_incident(incidents[0])
        best_strategy = options[0].strategy

        # Check with Safety Gate (clean failed payment)
        payment = _base_payment()
        result = evaluate(payment, best_strategy)
        self.assertTrue(result["allowed"])

    def test_full_pipeline_blocked_scenario(self):
        """Detect → Twin recommend → Gate blocks (captured payment)."""
        payment = _base_payment(status="captured")
        result = evaluate(payment, "SEND_PAYMENT_LINK")
        self.assertFalse(result["allowed"])


if __name__ == "__main__":
    unittest.main()
