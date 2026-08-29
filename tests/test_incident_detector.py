"""
Tests for the Incident Detector.

Uses Python's built-in unittest module — no extra dependencies needed.
"""

import os
import sys
import unittest

# Make sure we can import from the backend package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "backend"))

from incident_detector import (
    detect_incidents,
    format_amount,
    group_payments,
    read_payments,
    run,
)

SAMPLE_CSV = os.path.join(
    os.path.dirname(__file__), os.pardir, "data", "sample_payments.csv"
)


class TestReadPayments(unittest.TestCase):
    """Test that CSV reading works correctly."""

    def test_reads_all_rows(self):
        payments = read_payments(SAMPLE_CSV)
        # We created 35 rows
        self.assertEqual(len(payments), 35)

    def test_amount_is_float(self):
        payments = read_payments(SAMPLE_CSV)
        for p in payments:
            self.assertIsInstance(p["amount"], float)

    def test_status_values(self):
        payments = read_payments(SAMPLE_CSV)
        statuses = {p["status"] for p in payments}
        self.assertTrue(statuses.issubset({"success", "failed"}))


class TestGroupPayments(unittest.TestCase):
    """Test that grouping logic works correctly."""

    def setUp(self):
        self.payments = read_payments(SAMPLE_CSV)
        self.groups = group_payments(self.payments)

    def test_groups_exist(self):
        self.assertGreater(len(self.groups), 0)

    def test_total_across_groups_equals_payment_count(self):
        total = sum(g["total"] for g in self.groups.values())
        self.assertEqual(total, len(self.payments))

    def test_bank_x_upi_technical_error_group(self):
        key = ("BANK_X", "UPI", "technical_error")
        self.assertIn(key, self.groups)
        group = self.groups[key]
        # We created exactly 12 BANK_X UPI technical_error failures
        self.assertEqual(group["failed"], 12)
        self.assertEqual(group["total"], 12)


class TestDetectIncidents(unittest.TestCase):
    """Test incident detection logic."""

    def setUp(self):
        payments = read_payments(SAMPLE_CSV)
        self.groups = group_payments(payments)
        self.incidents = detect_incidents(self.groups)

    def test_at_least_one_incident(self):
        self.assertGreater(len(self.incidents), 0)

    def test_incident_structure(self):
        for inc in self.incidents:
            self.assertIn("bank", inc)
            self.assertIn("payment_method", inc)
            self.assertIn("error_reason", inc)
            self.assertIn("failure_rate", inc)
            self.assertIn("revenue_at_risk", inc)

    def test_bank_x_is_detected(self):
        banks = {inc["bank"] for inc in self.incidents}
        self.assertIn("BANK_X", banks)

    def test_incident_sorted_by_failure_rate(self):
        rates = [inc["failure_rate"] for inc in self.incidents]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_success_only_groups_not_flagged(self):
        """Groups with no failures should not appear as incidents."""
        for inc in self.incidents:
            self.assertNotEqual(inc["error_reason"], "none")


class TestFormatAmount(unittest.TestCase):
    """Test Indian Rupee formatting."""

    def test_basic_format(self):
        self.assertEqual(format_amount(1000.00), "₹1,000.00")

    def test_large_amount(self):
        self.assertEqual(format_amount(123456.78), "₹1,23,456.78")

    def test_small_amount(self):
        self.assertEqual(format_amount(50.50), "₹50.50")


class TestRunWithDefaults(unittest.TestCase):
    """Integration test — run the full pipeline."""

    def test_run_returns_incidents(self):
        incidents = run(SAMPLE_CSV)
        self.assertIsInstance(incidents, list)
        self.assertGreater(len(incidents), 0)


if __name__ == "__main__":
    unittest.main()
