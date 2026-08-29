"""
Incident Detector — Razorpay AI Revenue Recovery (Track 3)

Reads payment records from a CSV file, groups failed payments by
bank + payment_method + error_reason, and flags groups where the
failure rate is suspiciously high (possible payment-system incident).

No ML, no external APIs — just clear, readable Python.
"""

import csv
import os
from collections import defaultdict

# ---------- CONFIGURATION ----------

# Minimum number of failed payments in a group to consider it an incident
MIN_FAILURE_COUNT = 3

# Minimum failure rate (%) to flag a group as an incident
MIN_FAILURE_RATE = 50.0

# Path to the CSV (relative to the project root)
DEFAULT_CSV_PATH = os.path.join(
    os.path.dirname(__file__), os.pardir, "data", "sample_payments.csv"
)


# ---------- STEP 1: READ THE CSV ----------

def read_payments(csv_path: str) -> list[dict]:
    """Read every row from the CSV and return a list of dicts."""
    payments = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Convert amount to float for calculations
            row["amount"] = float(row["amount"])
            payments.append(row)
    return payments


# ---------- STEP 2: GROUP PAYMENTS ----------

def group_payments(payments: list[dict]) -> dict:
    """
    Group all payments by (bank, payment_method, error_reason).

    Returns a dict like:
        {
            ("BANK_X", "UPI", "technical_error"): {
                "payments": [...],
                "total": 8,
                "failed": 8,
                "revenue_at_risk": 24500.00,
            },
            ...
        }
    """
    groups: dict[tuple, dict] = defaultdict(
        lambda: {"payments": [], "total": 0, "failed": 0, "revenue_at_risk": 0.0}
    )

    for p in payments:
        key = (p["bank"], p["payment_method"], p["error_reason"])
        group = groups[key]
        group["payments"].append(p)
        group["total"] += 1
        if p["status"] == "failed":
            group["failed"] += 1
            group["revenue_at_risk"] += p["amount"]

    return dict(groups)


# ---------- STEP 3: DETECT INCIDENTS ----------

def detect_incidents(groups: dict) -> list[dict]:
    """
    Check each group and return those that look like incidents.

    A group is flagged when:
      - it has at least MIN_FAILURE_COUNT failed payments
      - its failure rate is >= MIN_FAILURE_RATE %
      - the error_reason is not "none" (successful payments)
    """
    incidents = []

    for (bank, method, reason), data in groups.items():
        # Skip groups where nothing actually failed
        if reason == "none" or data["failed"] == 0:
            continue

        failure_rate = (data["failed"] / data["total"]) * 100

        if data["failed"] >= MIN_FAILURE_COUNT and failure_rate >= MIN_FAILURE_RATE:
            incidents.append(
                {
                    "bank": bank,
                    "payment_method": method,
                    "error_reason": reason,
                    "total_payments": data["total"],
                    "failed_payments": data["failed"],
                    "failure_rate": round(failure_rate, 1),
                    "revenue_at_risk": round(data["revenue_at_risk"], 2),
                }
            )

    # Sort by failure rate (highest first)
    incidents.sort(key=lambda x: x["failure_rate"], reverse=True)
    return incidents


# ---------- STEP 4: PRINT RESULTS ----------

def format_amount(amount: float) -> str:
    """Format an amount as Indian Rupees with comma separators."""
    # Simple Indian-style formatting: ₹XX,XX,XXX
    s = f"{amount:.2f}"
    integer_part, decimal_part = s.split(".")
    if len(integer_part) <= 3:
        return f"₹{integer_part}.{decimal_part}"
    last_three = integer_part[-3:]
    remaining = integer_part[:-3]
    # Add commas every two digits for the remaining part (Indian grouping)
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

        # Simple explanation
        print(f"  ℹ️  Explanation:")
        print(
            f"  Several {inc['bank']} {inc['payment_method']} payments failed "
            f"with the same error ({inc['error_reason']}) within a short period, "
            f"suggesting a possible common payment-system issue."
        )

    print(f"\n{'=' * 60}\n")


# ---------- MAIN ----------

def run(csv_path: str | None = None) -> list[dict]:
    """
    Full pipeline: read → group → detect → print.
    Returns the list of detected incidents for programmatic use.
    """
    path = csv_path or DEFAULT_CSV_PATH
    payments = read_payments(path)
    print(f"📂  Loaded {len(payments)} payment records from {os.path.basename(path)}")

    groups = group_payments(payments)
    print(f"📊  Found {len(groups)} unique (bank, method, reason) groups")

    incidents = detect_incidents(groups)
    print(f"🔍  Detected {len(incidents)} potential incident(s)")

    print_incidents(incidents)
    return incidents


if __name__ == "__main__":
    run()
