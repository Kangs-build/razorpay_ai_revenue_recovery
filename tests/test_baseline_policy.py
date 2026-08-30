"""
Tests for the Baseline Policy.

Verifies that the fixed baseline policy chooses the correct strategy
for each failure type.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from baseline_policy import choose_strategy, choose_and_check, run_baseline
from payment_simulator import create_payment


def _make_payment(reason: str, **overrides) -> dict:
    """Helper to create a payment with a specific error reason."""
    defaults = {
        "payment_id": "PAY_TEST_001",
        "amount": 1000.0,
        "payment_method": "UPI",
        "bank": "HDFC",
        "status": "failed",
        "error_source": "test",
        "error_reason": reason,
    }
    defaults.update(overrides)
    return create_payment(**defaults)


# ======================================================================
# 1. Baseline chooses CUSTOMER_RETRY for incorrect OTP
# ======================================================================


class TestIncorrectOtp(unittest.TestCase):

    def test_choose_strategy(self):
        payment = _make_payment("incorrect_otp")
        self.assertEqual(choose_strategy(payment), "CUSTOMER_RETRY")

    def test_choose_and_check_returns_correct_strategy(self):
        payment = _make_payment("incorrect_otp")
        result = choose_and_check(payment)
        self.assertEqual(result["strategy"], "CUSTOMER_RETRY")


# ======================================================================
# 2. Baseline chooses SUGGEST_ALTERNATE_METHOD for insufficient balance
# ======================================================================


class TestInsufficientBalance(unittest.TestCase):

    def test_choose_strategy(self):
        payment = _make_payment("insufficient_balance")
        self.assertEqual(choose_strategy(payment), "SUGGEST_ALTERNATE_METHOD")

    def test_choose_and_check_returns_correct_strategy(self):
        payment = _make_payment("insufficient_balance")
        result = choose_and_check(payment)
        self.assertEqual(result["strategy"], "SUGGEST_ALTERNATE_METHOD")


# ======================================================================
# 3. Baseline uses RETRY_LATER for technical errors
# ======================================================================


class TestTechnicalError(unittest.TestCase):

    def test_choose_strategy(self):
        """Baseline uses RETRY_LATER for technical errors (fixed rule)."""
        payment = _make_payment("technical_error")
        self.assertEqual(choose_strategy(payment), "RETRY_LATER")

    def test_timeout_also_uses_retry_later(self):
        payment = _make_payment("timeout_error")
        self.assertEqual(choose_strategy(payment), "RETRY_LATER")

    def test_unknown_error_uses_retry_later(self):
        payment = _make_payment("some_other_error")
        self.assertEqual(choose_strategy(payment), "RETRY_LATER")


# ======================================================================
# 4. Safety Gate is used by baseline
# ======================================================================


class TestSafetyGateUsed(unittest.TestCase):

    def test_gate_blocks_captured_payment(self):
        payment = _make_payment("technical_error", status="captured")
        result = choose_and_check(payment)
        self.assertFalse(result["gate_result"]["allowed"])

    def test_gate_allows_normal_failure(self):
        payment = _make_payment("technical_error")
        result = choose_and_check(payment)
        self.assertTrue(result["gate_result"]["allowed"])


# ======================================================================
# 5. run_baseline processes only failed payments
# ======================================================================


class TestRunBaseline(unittest.TestCase):

    def test_only_failed_payments_processed(self):
        payments = [
            _make_payment("technical_error"),
            create_payment(
                payment_id="PAY_OK", amount=500, payment_method="UPI",
                bank="HDFC", status="success",
            ),
            _make_payment("incorrect_otp"),
        ]
        results = run_baseline(payments)
        self.assertEqual(len(results), 2)

    def test_returns_list_of_dicts(self):
        payments = [_make_payment("technical_error")]
        results = run_baseline(payments)
        self.assertEqual(len(results), 1)
        self.assertIn("strategy", results[0])
        self.assertIn("gate_result", results[0])


if __name__ == "__main__":
    unittest.main()
