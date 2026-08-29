"""
Tests for the Incident Detector.

Uses Python's built-in unittest module — no extra dependencies needed.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta

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


# ---------- helper: build synthetic payment dicts ----------

def _make_payment(
    pid: str,
    amount: float,
    timestamp: str,
    status: str,
    method: str = "UPI",
    bank: str = "BANK_X",
    error_reason: str = "none",
) -> dict:
    """Build a payment dict that looks like a CSV row."""
    ts = timestamp.rstrip("Z")
    return {
        "payment_id": pid,
        "amount": amount,
        "timestamp": timestamp,
        "status": status,
        "payment_method": method,
        "bank": bank,
        "error_source": "none",
        "error_step": "none",
        "error_reason": error_reason,
        "_dt": datetime.fromisoformat(ts),
    }


# ======================================================================
# CSV reading
# ======================================================================

class TestReadPayments(unittest.TestCase):
    """Test that CSV reading works correctly."""

    def test_reads_all_rows(self):
        payments = read_payments(SAMPLE_CSV)
        self.assertEqual(len(payments), 35)

    def test_amount_is_float(self):
        for p in read_payments(SAMPLE_CSV):
            self.assertIsInstance(p["amount"], float)

    def test_timestamps_are_parsed(self):
        for p in read_payments(SAMPLE_CSV):
            self.assertIsInstance(p["_dt"], datetime)

    def test_status_values(self):
        statuses = {p["status"] for p in read_payments(SAMPLE_CSV)}
        self.assertTrue(statuses.issubset({"success", "failed"}))


# ======================================================================
# Grouping by (bank, method)
# ======================================================================

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

    def test_bank_x_upi_group_contains_all_bank_x_upi(self):
        """The key is now (bank, method) — no error_reason split."""
        key = ("BANK_X", "UPI")
        self.assertIn(key, self.groups)
        group = self.groups[key]
        # BANK_X UPI appears 15 times in the CSV (10 in cluster + 5 scattered)
        self.assertEqual(group["total"], 15)
        # 11 of those are failures (7 technical_error in cluster + 1 outside + 3 incorrect_otp)
        self.assertEqual(group["failed"], 11)

    def test_groups_sorted_by_time(self):
        """Payments inside each group should be sorted by timestamp."""
        for group in self.groups.values():
            times = [p["_dt"] for p in group["payments"]]
            self.assertEqual(times, sorted(times))


# ======================================================================
# Incident detection — correctness
# ======================================================================

class TestDetectIncidents(unittest.TestCase):
    """Test incident detection against the sample CSV."""

    def setUp(self):
        payments = read_payments(SAMPLE_CSV)
        self.groups = group_payments(payments)
        # Use a large window so all BANK_X UPI failures are captured
        self.incidents = detect_incidents(
            self.groups, time_window_minutes=120
        )

    def test_at_least_one_incident(self):
        self.assertGreater(len(self.incidents), 0)

    def test_incident_structure(self):
        for inc in self.incidents:
            for field in (
                "bank", "payment_method", "error_reason",
                "total_payments", "failed_payments",
                "failure_rate", "revenue_at_risk",
            ):
                self.assertIn(field, inc)

    def test_bank_x_is_detected(self):
        banks = {inc["bank"] for inc in self.incidents}
        self.assertIn("BANK_X", banks)

    def test_failure_rate_not_100_when_mix(self):
        """BANK_X UPI has 15 attempts and 11 failures → ~73.3%, not 100%."""
        bx = [i for i in self.incidents if i["bank"] == "BANK_X"][0]
        self.assertAlmostEqual(bx["failure_rate"], 73.3, places=1)
        self.assertEqual(bx["total_payments"], 15)
        self.assertEqual(bx["failed_payments"], 11)

    def test_incident_sorted_by_failure_rate(self):
        rates = [inc["failure_rate"] for inc in self.incidents]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_success_only_groups_not_flagged(self):
        for inc in self.incidents:
            self.assertNotEqual(inc["error_reason"], "none")

    def test_default_window_detects_bank_x_cluster(self):
        """With the default 5-min window, the BANK_X UPI cluster is detected."""
        payments = read_payments(SAMPLE_CSV)
        groups = group_payments(payments)
        # Default TIME_WINDOW_MINUTES = 5
        incidents = detect_incidents(groups)
        bx = [i for i in incidents if i["bank"] == "BANK_X"]
        self.assertGreaterEqual(len(bx), 1, "Expected at least 1 BANK_X incident")
        # The cluster incident should be first (highest failure rate)
        cluster = bx[0]
        self.assertEqual(cluster["total_payments"], 10)
        self.assertEqual(cluster["failed_payments"], 7)
        self.assertEqual(cluster["failure_rate"], 70.0)
        self.assertEqual(cluster["error_reason"], "technical_error")


# ======================================================================
# Failure rate calculation — precise 70% test
# ======================================================================

class TestFailureRateCalculation(unittest.TestCase):
    """Verify that 7 failures out of 10 attempts gives exactly 70.0%."""

    def test_seven_out_of_ten_is_seventy_percent(self):
        # 7 failures + 3 successes, all within a 5-minute window
        payments = [
            _make_payment("P1", 100, "2026-08-28T10:00:00", "failed", error_reason="technical_error"),
            _make_payment("P2", 200, "2026-08-28T10:00:30", "success"),
            _make_payment("P3", 300, "2026-08-28T10:01:00", "failed", error_reason="technical_error"),
            _make_payment("P4", 400, "2026-08-28T10:01:30", "success"),
            _make_payment("P5", 500, "2026-08-28T10:02:00", "failed", error_reason="technical_error"),
            _make_payment("P6", 600, "2026-08-28T10:02:30", "failed", error_reason="technical_error"),
            _make_payment("P7", 700, "2026-08-28T10:03:00", "success"),
            _make_payment("P8", 800, "2026-08-28T10:03:30", "failed", error_reason="technical_error"),
            _make_payment("P9", 900, "2026-08-28T10:04:00", "failed", error_reason="technical_error"),
            _make_payment("P10", 1000, "2026-08-28T10:04:30", "failed", error_reason="technical_error"),
        ]

        groups = group_payments(payments)
        incidents = detect_incidents(groups, time_window_minutes=5)

        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc["total_payments"], 10)
        self.assertEqual(inc["failed_payments"], 7)
        self.assertEqual(inc["failure_rate"], 70.0)


# ======================================================================
# Time-window detection
# ======================================================================

class TestTimeWindow(unittest.TestCase):
    """Verify that the time window controls which failures are grouped."""

    def test_failures_within_window_are_detected(self):
        """5 failures inside a 5-minute window → incident detected."""
        payments = [
            _make_payment("P1", 100, "2026-08-28T10:00:00", "failed", error_reason="timeout"),
            _make_payment("P2", 200, "2026-08-28T10:01:00", "failed", error_reason="timeout"),
            _make_payment("P3", 300, "2026-08-28T10:02:00", "failed", error_reason="timeout"),
            _make_payment("P4", 400, "2026-08-28T10:03:00", "failed", error_reason="timeout"),
            _make_payment("P5", 500, "2026-08-28T10:04:00", "failed", error_reason="timeout"),
        ]

        groups = group_payments(payments)
        incidents = detect_incidents(groups, time_window_minutes=5)

        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["failed_payments"], 5)
        self.assertEqual(incidents[0]["failure_rate"], 100.0)

    def test_failures_far_apart_are_not_one_incident(self):
        """2 failures 20 minutes apart with a 5-min window → not detected."""
        payments = [
            _make_payment("P1", 100, "2026-08-28T10:00:00", "failed", error_reason="timeout"),
            _make_payment("P2", 200, "2026-08-28T10:01:00", "success"),
            # 20 minutes later — outside the 5-minute window
            _make_payment("P3", 300, "2026-08-28T10:20:00", "failed", error_reason="timeout"),
            _make_payment("P4", 400, "2026-08-28T10:21:00", "success"),
        ]

        groups = group_payments(payments)
        # With 5-min window: only P1 and P2 are inside → 1 failure out of 2
        incidents = detect_incidents(groups, time_window_minutes=5)
        self.assertEqual(len(incidents), 0)

    def test_wider_window_captures_more(self):
        """Same data: 30-min window captures all failures → incident detected."""
        payments = [
            _make_payment("P1", 100, "2026-08-28T10:00:00", "failed", error_reason="timeout"),
            _make_payment("P2", 200, "2026-08-28T10:01:00", "success"),
            _make_payment("P3", 300, "2026-08-28T10:05:00", "failed", error_reason="timeout"),
            _make_payment("P4", 400, "2026-08-28T10:10:00", "failed", error_reason="timeout"),
            _make_payment("P5", 500, "2026-08-28T10:15:00", "success"),
            _make_payment("P6", 600, "2026-08-28T10:20:00", "failed", error_reason="timeout"),
            _make_payment("P7", 700, "2026-08-28T10:25:00", "success"),
        ]

        groups = group_payments(payments)

        # 5-min window: only P1 + P2 inside → 1 failure → not flagged
        short = detect_incidents(groups, time_window_minutes=5)
        self.assertEqual(len(short), 0)

        # 30-min window: all 7 inside → 4 failures / 7 total → 57.1%
        wide = detect_incidents(groups, time_window_minutes=30)
        self.assertEqual(len(wide), 1)
        self.assertEqual(wide[0]["failed_payments"], 4)
        self.assertEqual(wide[0]["total_payments"], 7)
        self.assertAlmostEqual(wide[0]["failure_rate"], 57.1, places=1)


# ======================================================================
# Amount formatting
# ======================================================================

class TestFormatAmount(unittest.TestCase):
    """Test Indian Rupee formatting."""

    def test_basic_format(self):
        self.assertEqual(format_amount(1000.00), "₹1,000.00")

    def test_large_amount(self):
        self.assertEqual(format_amount(123456.78), "₹1,23,456.78")

    def test_small_amount(self):
        self.assertEqual(format_amount(50.50), "₹50.50")


# ======================================================================
# Integration
# ======================================================================

class TestRunWithDefaults(unittest.TestCase):
    """Integration test — run the full pipeline."""

    def test_run_completes_without_error(self):
        """The pipeline runs end-to-end without raising."""
        incidents = run(SAMPLE_CSV)
        self.assertIsInstance(incidents, list)

    def test_run_wide_window_finds_incidents(self):
        """With a wide window, BANK_X UPI failures are detected."""
        payments = read_payments(SAMPLE_CSV)
        groups = group_payments(payments)
        incidents = detect_incidents(groups, time_window_minutes=120)
        self.assertGreater(len(incidents), 0)


if __name__ == "__main__":
    unittest.main()
