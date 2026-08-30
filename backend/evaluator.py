"""
Baseline vs Recovery Twin Evaluation — Razorpay AI Revenue Recovery (Track 3)

Compares a simple fixed baseline policy against the Recovery Twin using
the SAME simulated payment batch. This is a simulation experiment only.

No ML, no LLM, no real payment actions — just transparent deterministic rules.
"""

from __future__ import annotations

import copy
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from payment_simulator import (
    create_payment,
    format_amount,
    generate_bank_incident,
    simulate_recovery,
    simulate_late_success,
)
from incident_detector import detect_incidents, group_payments
from recovery_twin import analyze_incident, select_individual_recovery_strategy
from safety_gate import evaluate as safety_evaluate
from baseline_policy import run_baseline


# ---------- DATASET GENERATION ----------


def generate_evaluation_dataset() -> list[dict]:
    """Generate a reproducible synthetic batch of ~100 payment attempts.

    Contains a mix of:
      - Successful payments
      - incorrect_otp failures
      - insufficient_balance failures
      - Individual temporary failures (timeout_error)
      - One clear BANK_X UPI technical incident (10 payments, 5-min window)
      - Late-success payments (fail then capture)
      - Some payments that remain unresolved

    All timestamps are deterministic. Repeated calls produce identical data.
    """
    base = "2026-08-30T10:00:00"
    payments = []

    def _ts(offset_seconds: int) -> str:
        from datetime import datetime, timedelta
        t = datetime.fromisoformat(base) + timedelta(seconds=offset_seconds)
        return t.isoformat()

    idx = 0

    # --- Successful payments (20) ---
    for i in range(20):
        idx += 1
        banks = ["HDFC", "ICICI", "SBI", "Axis", "Kotak"]
        methods = ["UPI", "Card", "NetBanking"]
        payments.append(create_payment(
            payment_id=f"EVAL_{idx:03d}",
            amount=500.0 + (i * 100),
            payment_method=methods[i % 3],
            bank=banks[i % 5],
            status="success",
            timestamp=_ts(i * 30),
        ))

    # --- incorrect_otp failures (12) ---
    for i in range(12):
        idx += 1
        payments.append(create_payment(
            payment_id=f"EVAL_{idx:03d}",
            amount=1000.0 + (i * 200),
            payment_method="Card",
            bank=["ICICI", "HDFC", "SBI"][i % 3],
            status="failed",
            error_source="card_network",
            error_reason="incorrect_otp",
            timestamp=_ts(600 + i * 60),
        ))

    # --- insufficient_balance failures (8) ---
    for i in range(8):
        idx += 1
        payments.append(create_payment(
            payment_id=f"EVAL_{idx:03d}",
            amount=2000.0 + (i * 500),
            payment_method=["NetBanking", "Card"][i % 2],
            bank=["SBI", "HDFC", "ICICI"][i % 3],
            status="failed",
            error_source="bank_api",
            error_reason="insufficient_balance",
            timestamp=_ts(1500 + i * 60),
        ))

    # --- Individual timeout_error failures (10) ---
    for i in range(10):
        idx += 1
        payments.append(create_payment(
            payment_id=f"EVAL_{idx:03d}",
            amount=800.0 + (i * 150),
            payment_method="NetBanking",
            bank=["HDFC", "Axis", "Kotak"][i % 3],
            status="failed",
            error_source="connector",
            error_reason="timeout_error",
            timestamp=_ts(2200 + i * 45),
        ))

    # --- BANK_X UPI technical incident (10 payments, 5-min window) ---
    bank_incident = generate_bank_incident()
    for p in bank_incident:
        idx += 1
        p["payment_id"] = f"EVAL_{idx:03d}"
    payments.extend(bank_incident)

    # --- Late-success payments (8, IDs end with 'L') ---
    for i in range(8):
        idx += 1
        payments.append(create_payment(
            payment_id=f"EVAL_{idx:03d}L",
            amount=1500.0 + (i * 250),
            payment_method="UPI",
            bank=["BANK_X", "HDFC", "ICICI"][i % 3],
            status="failed",
            error_source="bank_api",
            error_reason="technical_error",
            timestamp=_ts(3000 + i * 30),
        ))

    # --- Some that remain unresolved (10) ---
    for i in range(10):
        idx += 1
        payments.append(create_payment(
            payment_id=f"EVAL_{idx:03d}",
            amount=900.0 + (i * 100),
            payment_method=["UPI", "Card"][i % 2],
            bank=["BANK_X", "SBI", "HDFC"][i % 3],
            status="failed",
            error_source="bank_api",
            error_reason="technical_error",
            timestamp=_ts(3500 + i * 60),
        ))

    return payments


# ---------- HELPERS ----------


def _copy_payments(payments: list[dict]) -> list[dict]:
    """Deep-copy a payment batch so policies don't interfere."""
    return copy.deepcopy(payments)


def _is_late_success_candidate(payment: dict) -> bool:
    """Check if a payment is a late-success candidate (ID ends with 'L')."""
    return payment.get("payment_id", "").endswith("L")


# ---------- RECOVERY TWIN POLICY ----------


def run_recovery_twin_policy(payments: list[dict]) -> list[dict]:
    """Run the full Recovery Twin pipeline on a batch of payments.

    1. Simulate late-success FIRST (so Safety Gate observes captured payments)
    2. Group payments and detect incidents
    3. For incident failures: analyze with Recovery Twin, check gate, simulate
    4. For individual failures: use select_individual_recovery_strategy

    Returns:
        A list of per-payment result dicts.
    """
    # Step 1: Simulate late-success BEFORE recovery so Safety Gate blocks them
    late_success_ids = set()
    for payment in payments:
        if payment.get("status") == "failed" and _is_late_success_candidate(payment):
            simulate_late_success(payment)
            late_success_ids.add(payment["payment_id"])

    results = []
    processed_ids = set()

    # Step 2: First handle late-success candidates (now captured)
    for payment in payments:
        pid = payment["payment_id"]
        if pid not in late_success_ids:
            continue
        processed_ids.add(pid)
        strategy = select_individual_recovery_strategy(payment)
        gate_result = safety_evaluate(payment, strategy)
        results.append({
            "payment_id": pid,
            "amount": payment["amount"],
            "error_reason": payment.get("error_reason", "none"),
            "strategy": strategy,
            "gate_allowed": gate_result["allowed"],
            "final_status": payment["status"],
            "recovered": False,
            "customer_facing": False,
            "late_success_stop": not gate_result["allowed"],
        })

    # Step 3: Detect incidents for remaining failed payments
    groups = group_payments(payments)
    incidents = detect_incidents(groups)

    # Process incident payments
    for incident in incidents:
        options = analyze_incident(incident)
        strategy = options[0].strategy

        for payment in payments:
            if payment.get("status") != "failed":
                continue
            if (payment["bank"], payment["payment_method"]) != (
                incident["bank"], incident["payment_method"]
            ):
                continue
            if payment.get("error_reason", "none") == "none":
                continue

            pid = payment["payment_id"]
            if pid in processed_ids:
                continue
            processed_ids.add(pid)

            original_reason = payment.get("error_reason", "none")
            gate_result = safety_evaluate(payment, strategy)
            recovered = False
            if gate_result["allowed"]:
                before_status = payment["status"]
                simulate_recovery(payment, strategy)
                if payment["status"] != before_status:
                    recovered = True

            results.append({
                "payment_id": pid,
                "amount": payment["amount"],
                "error_reason": original_reason,
                "strategy": strategy,
                "gate_allowed": gate_result["allowed"],
                "final_status": payment["status"],
                "recovered": recovered,
                "customer_facing": strategy in {"SUGGEST_ALTERNATE_METHOD", "SEND_PAYMENT_LINK"},
                "late_success_stop": False,
            })

    # Step 4: Process individual failures (not in any incident)
    for payment in payments:
        if payment.get("status") != "failed":
            continue
        pid = payment["payment_id"]
        if pid in processed_ids:
            continue
        processed_ids.add(pid)

        original_reason = payment.get("error_reason", "none")
        strategy = select_individual_recovery_strategy(payment)
        gate_result = safety_evaluate(payment, strategy)
        recovered = False

        if gate_result["allowed"]:
            before_status = payment["status"]
            simulate_recovery(payment, strategy)
            if payment["status"] != before_status:
                recovered = True

        results.append({
            "payment_id": pid,
            "amount": payment["amount"],
            "error_reason": original_reason,
            "strategy": strategy,
            "gate_allowed": gate_result["allowed"],
            "final_status": payment["status"],
            "recovered": recovered,
            "customer_facing": False,
            "late_success_stop": False,
        })

    return results


# ---------- BASELINE POLICY ----------


def run_baseline_policy(payments: list[dict]) -> list[dict]:
    """Run the baseline policy with late-success simulation.

    Late-success simulation runs BEFORE the recovery loop so the
    Safety Gate observes captured payments and blocks recovery.

    Returns:
        A list of per-payment result dicts.
    """
    # Step 1: Simulate late-success BEFORE recovery so Safety Gate blocks them
    late_success_ids = set()
    for payment in payments:
        if payment.get("status") == "failed" and _is_late_success_candidate(payment):
            simulate_late_success(payment)
            late_success_ids.add(payment["payment_id"])

    # Step 2: Process all payments (including those now captured by late-success)
    results = []
    for payment in payments:
        pid = payment["payment_id"]
        is_late = pid in late_success_ids

        if is_late:
            # Late-success candidate: already captured, Safety Gate blocks
            from baseline_policy import choose_strategy
            strategy = choose_strategy(payment)
            gate_result = safety_evaluate(payment, strategy)
            results.append({
                "payment_id": pid,
                "amount": payment["amount"],
                "error_reason": payment.get("error_reason", "none"),
                "strategy": strategy,
                "gate_allowed": gate_result["allowed"],
                "final_status": payment["status"],
                "recovered": False,
                "customer_facing": strategy in {"SUGGEST_ALTERNATE_METHOD", "SEND_PAYMENT_LINK"},
                "late_success_stop": not gate_result["allowed"],
            })
            continue

        if payment.get("status") != "failed":
            continue

        # Normal failed payment: process through recovery
        original_reason = payment.get("error_reason", "none")
        from baseline_policy import choose_strategy
        strategy = choose_strategy(payment)
        gate_result = safety_evaluate(payment, strategy)
        recovered = False

        if gate_result["allowed"]:
            before_status = payment["status"]
            simulate_recovery(payment, strategy)
            if payment["status"] != before_status:
                recovered = True

        results.append({
            "payment_id": pid,
            "amount": payment["amount"],
            "error_reason": original_reason,
            "strategy": strategy,
            "gate_allowed": gate_result["allowed"],
            "final_status": payment["status"],
            "recovered": recovered,
            "customer_facing": strategy in {"SUGGEST_ALTERNATE_METHOD", "SEND_PAYMENT_LINK"},
            "late_success_stop": False,
        })

    return results


# ---------- METRICS ----------


def calculate_metrics(payments: list[dict], results: list[dict]) -> dict:
    """Calculate evaluation metrics from policy results.

    Returns:
        A dict of metric_name → value.
    """
    failed = [r for r in results]
    total_failed = len(failed)
    recovered = sum(1 for r in failed if r["recovered"])
    revenue_at_risk = sum(r["amount"] for r in failed)
    revenue_recovered = sum(r["amount"] for r in failed if r["recovered"])
    unresolved = revenue_at_risk - revenue_recovered
    attempts = sum(
        1 for p in payments
        if p.get("recovery_attempt_count", 0) > 0
    )
    customer_facing = sum(1 for r in failed if r["customer_facing"])
    blocked = sum(1 for r in failed if not r["gate_allowed"])
    late_stops = sum(1 for r in failed if r["late_success_stop"])

    return {
        "total_failed": total_failed,
        "recovered": recovered,
        "recovery_rate": round((recovered / total_failed * 100), 1) if total_failed > 0 else 0,
        "revenue_at_risk": revenue_at_risk,
        "revenue_recovered": revenue_recovered,
        "unresolved": unresolved,
        "attempts": attempts,
        "customer_facing": customer_facing,
        "blocked": blocked,
        "late_stops": late_stops,
    }


# ---------- COMPARISON OUTPUT ----------


def print_comparison(baseline: dict, twin: dict) -> None:
    """Print the side-by-side comparison table."""
    print()
    print("=" * 60)
    print("  SIMULATED RECOVERY EVALUATION")
    print("=" * 60)
    print()
    print("  Same starting payment batch used for both approaches.")
    print()
    print("  NOTE: All results below are SIMULATED RESULTS from a")
    print("  controlled experiment. They do NOT represent real-world")
    print("  recovery rates or production performance.")
    print()

    def _row(label: str, b_val, t_val, is_money: bool = False) -> None:
        fmt = format_amount if is_money else str
        b_str = fmt(b_val) if is_money else str(b_val)
        t_str = fmt(t_val) if is_money else str(t_val)
        print(f"  {label:<28} {b_str:>14} {t_str:>14}")

    _row("", "Baseline", "Recovery Twin")
    print("  " + "─" * 56)
    _row("Failed Payments", baseline["total_failed"], twin["total_failed"])
    _row("Payments Recovered", baseline["recovered"], twin["recovered"])
    _row("Recovery Rate", f"{baseline['recovery_rate']}%", f"{twin['recovery_rate']}%")
    _row("Revenue At Risk", baseline["revenue_at_risk"], twin["revenue_at_risk"], True)
    _row("Revenue Recovered", baseline["revenue_recovered"], twin["revenue_recovered"], True)
    _row("Unresolved Revenue", baseline["unresolved"], twin["unresolved"], True)
    _row("Recovery Attempts", baseline["attempts"], twin["attempts"])
    _row("Customer-facing Actions", baseline["customer_facing"], twin["customer_facing"])
    _row("Unsafe Actions Blocked", baseline["blocked"], twin["blocked"])
    _row("Late-success Stops", baseline["late_stops"], twin["late_stops"])

    # Difference
    rev_diff = twin["revenue_recovered"] - baseline["revenue_recovered"]
    att_diff = baseline["attempts"] - twin["attempts"]
    cust_diff = baseline["customer_facing"] - twin["customer_facing"]
    rev_sign = "+" if rev_diff >= 0 else ""

    print()
    print("  DIFFERENCE (Recovery Twin vs Baseline)")
    print("  " + "─" * 56)
    print(f"  Additional simulated revenue recovered:  {rev_sign}{format_amount(rev_diff)}")
    print(f"  Recovery attempts avoided:               {att_diff}")
    print(f"  Customer-facing actions avoided:         {cust_diff}")
    print()


# ---------- INCIDENT-LEVEL COMPARISON ----------


def find_bank_x_incident_payments(payments: list[dict]) -> list[dict]:
    """Find the BANK_X UPI incident payments in the dataset."""
    return [
        p for p in payments
        if p["bank"] == "BANK_X"
        and p["payment_method"] == "UPI"
        and p.get("error_reason", "none") != "none"
    ]


def print_incident_comparison(
    incident_payments: list[dict],
    baseline_results: list[dict],
    twin_results: list[dict],
) -> None:
    """Show how baseline and Recovery Twin handle the BANK_X UPI incident."""
    print()
    print("=" * 60)
    print("  INCIDENT-LEVEL COMPARISON: BANK_X UPI")
    print("=" * 60)
    print()

    incident_ids = {p["payment_id"] for p in incident_payments}
    b_results = [r for r in baseline_results if r["payment_id"] in incident_ids]
    t_results = [r for r in twin_results if r["payment_id"] in incident_ids]

    if not b_results and not t_results:
        print("  No matching results found for BANK_X UPI incident.")
        return

    # Determine strategies used
    b_strategy = b_results[0]["strategy"] if b_results else "N/A"
    t_strategy = t_results[0]["strategy"] if t_results else "N/A"

    print(f"  Baseline strategy:     {b_strategy}")
    print(f"  Recovery Twin strategy: {t_strategy}")
    print()

    # Revenue at risk for this incident
    total_risk = sum(p["amount"] for p in incident_payments)
    b_recovered = sum(r["amount"] for r in b_results if r["recovered"])
    t_recovered = sum(r["amount"] for r in t_results if r["recovered"])
    b_blocked = sum(1 for r in b_results if not r["gate_allowed"])
    t_blocked = sum(1 for r in t_results if not r["gate_allowed"])

    print(f"  Total payments in incident:   {len(incident_payments)}")
    print(f"  Revenue at risk:              {format_amount(total_risk)}")
    print()
    print(f"  {'Metric':<30} {'Baseline':>12} {'Recovery Twin':>12}")
    print(f"  {'─' * 54}")
    print(f"  {'Payments recovered':<30} {sum(1 for r in b_results if r['recovered']):>12} {sum(1 for r in t_results if r['recovered']):>12}")
    print(f"  {'Revenue recovered':<30} {format_amount(b_recovered):>12} {format_amount(t_recovered):>12}")
    print(f"  {'Actions blocked by gate':<30} {b_blocked:>12} {t_blocked:>12}")
    print()


# ---------- WHAT-IF REPLAY ----------


def replay_incident(
    incident_payments: list[dict],
    strategy: str,
) -> dict:
    """Replay the same incident under a specific strategy.

    Args:
        incident_payments: List of payment dicts representing the incident.
        strategy: The recovery strategy to apply.

    Returns:
        {
            "strategy": str,
            "total_payments": int,
            "recovered": int,
            "recovery_rate": float,
            "revenue_at_risk": float,
            "revenue_recovered": float,
            "unresolved": float,
            "blocked_by_gate": int,
            "results": list[dict],
        }
    """
    # Deep copy to avoid mutation
    payments = _copy_payments(incident_payments)
    results = []

    for payment in payments:
        if payment.get("status") != "failed":
            continue

        gate_result = safety_evaluate(payment, strategy)
        recovered = False

        if gate_result["allowed"]:
            before_status = payment["status"]
            simulate_recovery(payment, strategy)
            if payment["status"] != before_status:
                recovered = True

        results.append({
            "payment_id": payment["payment_id"],
            "amount": payment["amount"],
            "gate_allowed": gate_result["allowed"],
            "final_status": payment["status"],
            "recovered": recovered,
        })

    total = len(results)
    rec = sum(1 for r in results if r["recovered"])
    risk = sum(r["amount"] for r in results)
    recovered_amount = sum(r["amount"] for r in results if r["recovered"])
    blocked = sum(1 for r in results if not r["gate_allowed"])

    return {
        "strategy": strategy,
        "total_payments": total,
        "recovered": rec,
        "recovery_rate": round((rec / total * 100), 1) if total > 0 else 0,
        "revenue_at_risk": risk,
        "revenue_recovered": recovered_amount,
        "unresolved": risk - recovered_amount,
        "blocked_by_gate": blocked,
        "results": results,
    }


def print_replay_results(incident_payments: list[dict]) -> None:
    """Run what-if replay for all strategies and print results."""
    print()
    print("=" * 60)
    print("  WHAT-IF REPLAY: BANK_X UPI INCIDENT")
    print("=" * 60)
    print()
    print("  Replaying the same incident under each strategy.")
    print("  All results are SIMULATED RESULTS.")
    print()

    strategies = [
        "WAIT_AND_RECHECK",
        "RETRY_LATER",
        "SUGGEST_ALTERNATE_METHOD",
        "SEND_PAYMENT_LINK",
        "CUSTOMER_RETRY",
    ]

    all_results = []
    for strategy in strategies:
        result = replay_incident(incident_payments, strategy)
        all_results.append(result)

    # Print comparison table
    print(f"  {'Strategy':<30} {'Recovered':>10} {'Rate':>8} {'Revenue Rec.':>14}")
    print(f"  {'─' * 62}")
    for r in all_results:
        print(
            f"  {r['strategy']:<30} "
            f"{r['recovered']}/{r['total_payments']:>7} "
            f"{r['recovery_rate']:>7.1f}% "
            f"{format_amount(r['revenue_recovered']):>14}"
        )

    # Best strategy
    best = max(all_results, key=lambda x: x["recovered"])
    print()
    print(f"  Best simulated strategy: {best['strategy']}")
    print(f"  Recovered {best['recovered']}/{best['total_payments']} payments")
    print(f"  ({format_amount(best['revenue_recovered'])} of {format_amount(best['revenue_at_risk'])})")
    print()


# ---------- FULL EVALUATION ----------


def run_evaluation() -> None:
    """Run the complete baseline vs Recovery Twin evaluation."""
    print()
    print("=" * 60)
    print("  BASELINE vs RECOVERY TWIN — SIMULATED EVALUATION")
    print("=" * 60)

    # Generate dataset
    dataset = generate_evaluation_dataset()
    print(f"\n  Generated {len(dataset)} payment records.\n")

    # Fair replay: independent copies
    baseline_payments = _copy_payments(dataset)
    twin_payments = _copy_payments(dataset)

    # Run baseline
    baseline_results = run_baseline_policy(baseline_payments)
    baseline_metrics = calculate_metrics(baseline_payments, baseline_results)

    # Run Recovery Twin
    twin_results = run_recovery_twin_policy(twin_payments)
    twin_metrics = calculate_metrics(twin_payments, twin_results)

    # Print comparison
    print_comparison(baseline_metrics, twin_metrics)

    # Incident-level comparison
    incident_payments = find_bank_x_incident_payments(dataset)
    print_incident_comparison(incident_payments, baseline_results, twin_results)

    # What-If Replay
    print_replay_results(incident_payments)

    print("=" * 60)
    print("  Evaluation complete.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    run_evaluation()
