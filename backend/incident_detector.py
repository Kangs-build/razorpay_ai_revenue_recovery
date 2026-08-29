"""
Incident Detector — Razorpay AI Revenue Recovery (Track 3)

Reads payment records from a CSV, groups them by (bank, payment_method),
and detects incidents using a sliding time window. A failure rate above
the configured threshold within the window flags an incident.

No ML, no external APIs — just clear, readable Python.
"""

import csv
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

# ---------- CONFIGURATION ----------

# Minimum number of failed payments in the time window to consider an incident
MIN_FAILURE_COUNT = 3

# Minimum failure rate (%) to flag a group as an incident
MIN_FAILURE_RATE = 50.0

# How far back (in minutes) to look from the first payment in a group
TIME_WINDOW_MINUTES = 5

# Path to the CSV (relative to this file)
DEFAULT_CSV_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "data", "sample_payments.csv"
)


# ---------- STEP 1: READ THE CSV ----------

def read_payments(csv_path: str) -> list[dict]:
    """Read every row from the CSV and return a list of dicts.
    Timestamps are parsed into datetime objects; amounts into floats."""
    payments = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["amount"] = float(row["amount"])
            # Parse ISO timestamp — strip trailing Z for Python <3.11
            ts = row["timestamp"].rstrip("Z")
            row["_dt"] = datetime.fromisoformat(ts)
            payments.append(row)
    return payments


# ---------- STEP 2: GROUP PAYMENTS ----------

def group_payments(payments: list[dict]) -> dict:
    """Group payments by (bank, payment_method).

    Returns:
        {
            ("BANK_X", "UPI"): {
                "payments": [...],       # all payments, sorted by time
                "total": 18,
                "failed": 15,
                "revenue_at_risk": ...,
            },
            ...
        }
    """
    groups: dict[tuple, dict] = defaultdict(
        lambda: {
            "payments": [],
            "total": 0,
            "failed": 0,
            "revenue_at_risk": 0.0,
        }
    )

    for p in payments:
        key = (p["bank"], p["payment_method"])
        group = groups[key]
        group["payments"].append(p)
        group["total"] += 1
        if p["status"] == "failed":
            group["failed"] += 1
            group["revenue_at_risk"] += p["amount"]

    # Sort each group by timestamp so the sliding window works correctly
    for group in groups.values():
        group["payments"].sort(key=lambda p: p["_dt"])

    return dict(groups)


# ---------- STEP 3: DETECT INCIDENTS ----------

def _window_start(payments_in_group: list[dict]) -> datetime:
    """Return the timestamp of the earliest payment in the group."""
    return payments_in_group[0]["_dt"]


def _most_common_error(payments: list[dict]) -> str:
    """Return the most common error_reason among failed payments.
    Returns 'unknown' if there are no failed payments."""
    reasons = [p["error_reason"] for p in payments if p["status"] == "failed"]
    if not reasons:
        return "unknown"
    return Counter(reasons).most_common(1)[0][0]


def detect_incidents(
    groups: dict,
    time_window_minutes: int = TIME_WINDOW_MINUTES,
    min_failure_count: int = MIN_FAILURE_COUNT,
    min_failure_rate: float = MIN_FAILURE_RATE,
) -> list[dict]:
    """Detect incidents by sliding a time window over each (bank, method) group.

    The window is anchored to the earliest payment in the group and spans
    `time_window_minutes` forward. Only payments inside this window are
    considered when computing the failure rate.

    A group is flagged when:
      - enough failed payments fall inside the window
      - the failure rate inside the window meets the threshold
    """
    incidents = []

    for (bank, method), data in groups.items():
        all_payments = data["payments"]
        if not all_payments:
            continue

        window_start = _window_start(all_payments)
        window_end = window_start + timedelta(minutes=time_window_minutes)

        # Collect payments that fall inside the sliding window
        window_payments = [
            p for p in all_payments if p["_dt"] <= window_end
        ]
        window_failed = [p for p in window_payments if p["status"] == "failed"]
        window_total = len(window_payments)
        window_fail_count = len(window_failed)

        # Need at least min_failure_count failures to consider an incident
        if window_fail_count < min_failure_count:
            continue

        # Compute failure rate inside the window
        failure_rate = (window_fail_count / window_total) * 100

        if failure_rate < min_failure_rate:
            continue

        # Find the dominant error among failed payments in the window
        error_reason = _most_common_error(window_failed)

        # Revenue at risk = sum of failed payment amounts inside the window
        revenue_at_risk = sum(p["amount"] for p in window_failed)

        incidents.append(
            {
                "bank": bank,
                "payment_method": method,
                "error_reason": error_reason,
                "total_payments": window_total,
                "failed_payments": window_fail_count,
                "failure_rate": round(failure_rate, 1),
                "revenue_at_risk": round(revenue_at_risk, 2),
            }
        )

    # Sort by failure rate (highest first)
    incidents.sort(key=lambda x: x["failure_rate"], reverse=True)
    return incidents


# ---------- STEP 4: PRINT RESULTS ----------

def format_amount(amount: float) -> str:
    """Format an amount as Indian Rupees with comma separators."""
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


def print_incidents(incidents: list[dict]) -> None:
    """Pretty-print detected incidents to the terminal."""
    if not incidents:
        print("\n✅ No payment incidents detected.\n")
        return

    print("\n" + "=" * 60)
    print(f"  🚨  PAYMENT INCIDENTS DETECTED: {len(incidents)}")
    print("=" * 60)

    for i, inc in enumerate(incidents, start=1):
        print(f"\n{'─' * 60}")
        print(f"  INCIDENT #{i}")
        print(f"{'─' * 60}")
        print(f"  Bank:           {inc['bank']}")
        print(f"  Payment Method: {inc['payment_method']}")
        print(f"  Failure Reason: {inc['error_reason']}")
        print(f"  Total Payments: {inc['total_payments']}")
        print(f"  Failed Payments:{inc['failed_payments']}")
        print(f"  Failure Rate:   {inc['failure_rate']}%")
        print(f"  Revenue At Risk:{format_amount(inc['revenue_at_risk'])}")
        print()

        print(f"  ℹ️  Explanation:")
        print(
            f"  Several {inc['bank']} {inc['payment_method']} payments failed "
            f"with the same error ({inc['error_reason']}) within a short period, "
            f"suggesting a possible common payment-system issue."
        )

    print(f"\n{'=' * 60}\n")


# ---------- MAIN ----------

def run(csv_path: str | None = None) -> list[dict]:
    """Full pipeline: read → group → detect → print.
    Returns the list of detected incidents for programmatic use."""
    path = csv_path or DEFAULT_CSV_PATH
    payments = read_payments(path)
    print(f"📂  Loaded {len(payments)} payment records from {os.path.basename(path)}")

    groups = group_payments(payments)
    print(f"📊  Found {len(groups)} unique (bank, method) groups")

    incidents = detect_incidents(groups)
    print(f"🔍  Detected {len(incidents)} potential incident(s)")

    print_incidents(incidents)
    return incidents


if __name__ == "__main__":
    run()
