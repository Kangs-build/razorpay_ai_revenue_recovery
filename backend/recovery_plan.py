"""
Multi-Step Incident Recovery Twin — Version 2
Razorpay AI Revenue Recovery (Track 3)

Instead of choosing a single recovery action, compares complete recovery
plans — each containing multiple steps tried in sequence. Simulates each
plan on the same incident and scores them transparently.

No ML, no LLM, no real payment actions — just rule-based simulation.
"""

from __future__ import annotations

import copy
import sys
import os
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(__file__))

from safety_gate import (
    evaluate as safety_evaluate,
    MAX_RECOVERY_ATTEMPTS,
)
from payment_simulator import simulate_recovery, simulate_late_success

# ---------- RECOVERY PLANS ----------


@dataclass
class RecoveryPlan:
    """A named recovery plan containing an ordered list of strategy steps."""

    name: str
    description: str
    steps: list[str]  # list of strategy names, tried in order


# Four predefined plans
PLAN_A_SAFE_WAIT = RecoveryPlan(
    name="PLAN_A_SAFE_WAIT",
    description="Wait first, then escalate carefully",
    steps=["WAIT_AND_RECHECK", "RETRY_LATER", "SUGGEST_ALTERNATE_METHOD"],
)

PLAN_B_FAST_RECOVERY = RecoveryPlan(
    name="PLAN_B_FAST_RECOVERY",
    description="Skip waiting, retry immediately then escalate",
    steps=["RETRY_LATER", "SUGGEST_ALTERNATE_METHOD"],
)

PLAN_C_CUSTOMER_ALTERNATE = RecoveryPlan(
    name="PLAN_C_CUSTOMER_ALTERNATE",
    description="Wait, then escalate to customer-facing actions",
    steps=["WAIT_AND_RECHECK", "SUGGEST_ALTERNATE_METHOD", "SEND_PAYMENT_LINK"],
)

PLAN_D_CONSERVATIVE = RecoveryPlan(
    name="PLAN_D_CONSERVATIVE",
    description="Wait and retry only — minimal customer disturbance",
    steps=["WAIT_AND_RECHECK", "RETRY_LATER"],
)

ALL_PLANS = [PLAN_A_SAFE_WAIT, PLAN_B_FAST_RECOVERY, PLAN_C_CUSTOMER_ALTERNATE, PLAN_D_CONSERVATIVE]

# ---------- PLAN SIMULATION RESULT ----------


@dataclass
class PlanResult:
    """Outcome of simulating a single plan on one incident."""

    plan_name: str
    recovered: int = 0
    total_failed: int = 0
    revenue_recovered: float = 0.0
    revenue_at_risk: float = 0.0
    unresolved: float = 0.0
    recovery_attempts: int = 0
    customer_facing_actions: int = 0
    blocked_by_gate: int = 0
    late_success_stops: int = 0
    per_payment: list[dict] = field(default_factory=list)


# ---------- PLAN SIMULATOR ----------


def simulate_recovery_plan(
    incident_payments: list[dict],
    plan: RecoveryPlan,
) -> PlanResult:
    """Replay the same incident using a multi-step recovery plan.

    For each failed payment:
      1. Run late-success simulation first (if applicable).
      2. Try each step in the plan sequentially.
      3. After each step, check the simulated payment status.
      4. Stop if: payment captured, Safety Gate blocks, max attempts reached.

    Args:
        incident_payments: list of payment dicts (will be deep-copied).
        plan: A RecoveryPlan with an ordered list of strategies.

    Returns:
        A PlanResult with per-payment detail and aggregate metrics.
    """
    # Deep copy so we never mutate the original
    payments = copy.deepcopy(incident_payments)

    plan_result = PlanResult(plan_name=plan.name)

    # Identify late-success candidates (ID ends with 'L')
    late_success_ids = set()
    for p in payments:
        if p.get("payment_id", "").endswith("L") and p.get("status") == "failed":
            simulate_late_success(p)
            late_success_ids.add(p["payment_id"])

    # Process each payment
    for payment in payments:
        pid = payment["payment_id"]
        amount = payment["amount"]
        is_late = pid in late_success_ids

        payment_entry = {
            "payment_id": pid,
            "amount": amount,
            "original_status": "failed",
            "is_late_success": is_late,
            "steps_tried": [],
            "final_status": payment["status"],
            "recovered": False,
            "blocked_by_gate": False,
            "late_success_stop": False,
        }

        if is_late:
            # Late-success: already captured, Safety Gate will block
            payment_entry["late_success_stop"] = True
            plan_result.late_success_stops += 1
            plan_result.per_payment.append(payment_entry)
            continue

        if payment.get("status") != "failed":
            continue

        plan_result.total_failed += 1
        plan_result.revenue_at_risk += amount

        # Try each step in the plan
        for step_strategy in plan.steps:
            # Check if already recovered or max attempts reached
            if payment.get("status") in ("captured", "success"):
                break
            if payment.get("recovered_already", False):
                break

            # Safety Gate check
            gate_result = safety_evaluate(payment, step_strategy)
            plan_result.recovery_attempts += 1
            payment_entry["steps_tried"].append({
                "strategy": step_strategy,
                "gate_allowed": gate_result["allowed"],
            })

            if not gate_result["allowed"]:
                payment_entry["blocked_by_gate"] = True
                plan_result.blocked_by_gate += 1
                break

            # Check if strategy contacts customer
            if step_strategy in ("SUGGEST_ALTERNATE_METHOD", "SEND_PAYMENT_LINK"):
                plan_result.customer_facing_actions += 1

            # Simulate recovery
            before_status = payment["status"]
            simulate_recovery(payment, step_strategy)

            if payment["status"] != before_status and payment["status"] in ("captured", "success"):
                payment_entry["recovered"] = True
                plan_result.recovered += 1
                plan_result.revenue_recovered += amount
                break

        payment_entry["final_status"] = payment["status"]
        plan_result.per_payment.append(payment_entry)

    plan_result.unresolved = plan_result.revenue_at_risk - plan_result.revenue_recovered
    return plan_result


# ---------- PLAN SCORING ----------


def score_plan(result: PlanResult) -> int:
    """Calculate a transparent suitability score (0–100) from simulated results.

    Scoring formula (shown explicitly in output):
      base_score = recovery_rate (0–40 points)
      revenue_bonus = revenue_recovery_rate (0–30 points)
      efficiency_bonus = (1 - attempts_per_recovered) scaled (0–15 points)
      safety_bonus = no_blocks_bonus (0–10 points)
      customer_penalty = -2 per customer-facing action (0 to -10 points)

    All results come from the simulator — no hard-coded percentages.
    """
    if result.total_failed == 0:
        return 0

    # 1. Recovery rate (0–40): percentage of failed payments recovered
    recovery_rate = result.recovered / result.total_failed
    base_score = int(recovery_rate * 40)

    # 2. Revenue recovery rate (0–30): proportion of at-risk revenue recovered
    if result.revenue_at_risk > 0:
        rev_rate = result.revenue_recovered / result.revenue_at_risk
    else:
        rev_rate = 0
    revenue_bonus = int(rev_rate * 30)

    # 3. Efficiency bonus (0–15): fewer attempts per recovered payment is better
    if result.recovered > 0:
        attempts_per_recovered = result.recovery_attempts / result.recovered
        # 1.0 attempt per recovery = best (15 pts), 3.0+ = worst (0 pts)
        efficiency = max(0, min(1, (3.0 - attempts_per_recovered) / 2.0))
    else:
        efficiency = 0
    efficiency_bonus = int(efficiency * 15)

    # 4. Safety bonus (0–10): no blocked actions = full bonus
    if result.total_failed > 0 and result.blocked_by_gate == 0:
        safety_bonus = 10
    elif result.total_failed > 0:
        # Partial: fewer blocks = more bonus
        block_ratio = result.blocked_by_gate / result.total_failed
        safety_bonus = int((1 - block_ratio) * 10)
    else:
        safety_bonus = 0

    # 5. Customer penalty: -2 per customer-facing action (min -10)
    customer_penalty = max(-10, -(result.customer_facing_actions * 2))

    total = base_score + revenue_bonus + efficiency_bonus + safety_bonus + customer_penalty
    return max(0, min(100, total))


def score_explanation(result: PlanResult) -> str:
    """Return a human-readable breakdown of how the score was calculated."""
    if result.total_failed == 0:
        return "No failed payments to evaluate."

    recovery_rate = result.recovered / result.total_failed
    if result.revenue_at_risk > 0:
        rev_rate = result.revenue_recovered / result.revenue_at_risk
    else:
        rev_rate = 0

    if result.recovered > 0:
        attempts_per_recovered = result.recovery_attempts / result.recovered
        efficiency = max(0, min(1, (3.0 - attempts_per_recovered) / 2.0))
    else:
        efficiency = 0

    if result.total_failed > 0 and result.blocked_by_gate == 0:
        safety_bonus = 10
    elif result.total_failed > 0:
        block_ratio = result.blocked_by_gate / result.total_failed
        safety_bonus = int((1 - block_ratio) * 10)
    else:
        safety_bonus = 0

    customer_penalty = max(-10, -(result.customer_facing_actions * 2))

    lines = [
        f"  Score Breakdown:",
        f"    Recovery Rate:        {result.recovered}/{result.total_failed} = "
        f"{recovery_rate*100:.1f}% → {int(recovery_rate * 40)}/40 pts",
        f"    Revenue Recovery:     {result.revenue_recovered:.0f}/{result.revenue_at_risk:.0f} = "
        f"{rev_rate*100:.1f}% → {int(rev_rate * 30)}/30 pts",
        f"    Efficiency:           {attempts_per_recovered:.2f} attempts/recovery → "
        f"{int(efficiency * 15)}/15 pts" if result.recovered > 0 else
        f"    Efficiency:           No recoveries → 0/15 pts",
        f"    Safety:               {result.blocked_by_gate} blocks → {safety_bonus}/10 pts",
        f"    Customer Penalty:     {result.customer_facing_actions} actions × -2 → "
        f"{customer_penalty} pts",
    ]
    return "\n".join(lines)# ---------- DETERMINISTIC TIE-BREAKING ----------


def _plan_sort_key(plan_result_tuple: tuple) -> tuple:
    """Return a sort key that implements deterministic tie-breaking.

    Tie-break order:
      1. Higher score wins
      2. Fewer recovery steps (shorter plan)
      3. Fewer customer-facing actions
      4. Fewer total recovery attempts
      5. Alphabetical by plan name
    """
    plan, result, score = plan_result_tuple
    # Negate score (higher is better), others (lower is better)
    return (
        -score,
        len(plan.steps),
        result.customer_facing_actions,
        result.recovery_attempts,
        plan.name,
    )


def _explain_tie_break(winner: RecoveryPlan, loser: RecoveryPlan,
                        winner_result: PlanResult, loser_result: PlanResult,
                        winner_score: int, loser_score: int) -> str | None:
    """Explain why one plan was chosen over another when they tie on score.

    Returns None if no tie occurred (winner scored higher).
    """
    if winner_score != loser_score:
        return None

    # Same score — explain the tie-break
    if len(winner.steps) != len(loser.steps):
        return (
            f"{winner.name} selected over {loser.name} because both scored "
            f"{winner_score}, but {winner.name} uses fewer recovery steps "
            f"({len(winner.steps)} vs {len(loser.steps)})."
        )
    if winner_result.customer_facing_actions != loser_result.customer_facing_actions:
        return (
            f"{winner.name} selected over {loser.name} because both scored "
            f"{winner_score} with {len(winner.steps)} steps, but "
            f"{winner.name} has fewer customer-facing actions "
            f"({winner_result.customer_facing_actions} vs "
            f"{loser_result.customer_facing_actions})."
        )
    if winner_result.recovery_attempts != loser_result.recovery_attempts:
        return (
            f"{winner.name} selected over {loser.name} because both scored "
            f"{winner_score} with {len(winner.steps)} steps and "
            f"{winner_result.customer_facing_actions} customer actions, but "
            f"{winner.name} used fewer recovery attempts "
            f"({winner_result.recovery_attempts} vs "
            f"{loser_result.recovery_attempts})."
        )
    # Alphabetical fallback
    return (
        f"{winner.name} selected over {loser.name} because both scored "
        f"{winner_score} with identical metrics, and {winner.name} "
        f"comes first alphabetically."
    )


# ---------- FIND INCIDENT PAYMENTS ----------

def get_incident_payments(
    payments: list[dict],
    bank: str,
    method: str,
) -> list[dict]:
    """Extract failed payments for a specific (bank, method) pair."""
    return [
        p for p in payments
        if p["bank"] == bank
        and p["payment_method"] == method
        and p.get("status") == "failed"
    ]


# ---------- CONSOLE DEMO ----------


def _format_amount(amount: float) -> str:
    """Format amount as Indian Rupees."""
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


def run_demo(
    payments: list[dict] | None = None,
    bank: str = "BANK_X",
    method: str = "UPI",
) -> None:
    """Run the Multi-Step Recovery Twin demo.

    If payments is None, generates a bank incident using the simulator.
    """
    if payments is None:
        from payment_simulator import generate_bank_incident
        payments = generate_bank_incident()

    incident_payments = get_incident_payments(payments, bank, method)

    if not incident_payments:
        print(f"\n  No failed {bank} {method} payments found.\n")
        return

    # Calculate revenue at risk
    total_risk = sum(p["amount"] for p in incident_payments)

    print()
    print("=" * 60)
    print("  MULTI-STEP INCIDENT RECOVERY TWIN")
    print("=" * 60)
    print()
    print(f"  Incident:")
    print(f"  {bank} + {method} + technical_error")
    print(f"  Revenue At Risk: {_format_amount(total_risk)}")
    print(f"  Failed Payments: {len(incident_payments)}")
    print()
    print("  SIMULATED RESULTS — Each plan replayed independently")
    print()

    # Simulate each plan
    results = []
    for plan in ALL_PLANS:
        result = simulate_recovery_plan(incident_payments, plan)
        score = score_plan(result)
        results.append((plan, result, score))

    # Sort with deterministic tie-breaking
    results.sort(key=_plan_sort_key)

    # Detect and explain ties
    tie_explanations = []
    for i in range(1, len(results)):
        explanation = _explain_tie_break(
            winner=results[0][0], loser=results[i][0],
            winner_result=results[0][1], loser_result=results[i][1],
            winner_score=results[0][2], loser_score=results[i][2],
        )
        if explanation:
            tie_explanations.append(explanation)
            break  # only explain against the winner

    # Print each plan
    for i, (plan, result, score) in enumerate(results, 1):
        print(f"  PLAN {i} — {plan.name}")
        print(f"  {plan.description}")
        print(f"  Steps: {' → '.join(plan.steps)}")
        print(f"  {'─' * 50}")
        print(f"  Payments Recovered:    {result.recovered}/{result.total_failed}")
        print(f"  Revenue Recovered:     {_format_amount(result.revenue_recovered)}")
        print(f"  Unresolved:            {_format_amount(result.unresolved)}")
        print(f"  Recovery Attempts:     {result.recovery_attempts}")
        print(f"  Customer-facing:       {result.customer_facing_actions}")
        print(f"  Safety Blocks:         {result.blocked_by_gate}")
        print(f"  Late-success Stops:    {result.late_success_stops}")
        print(f"  Plan Score:            {score}/100")
        print()
        print(score_explanation(result))
        print()

    # Recommendation
    best_plan, best_result, best_score = results[0]
    print("  " + "─" * 50)
    print()
    print(f"  RECOMMENDED PLAN:")
    print(f"  {best_plan.name}")
    print()
    if tie_explanations:
        print(f"  TIE-BREAK:")
        for exp in tie_explanations:
            print(f"  {exp}")
        print()
    print(f"  WHY:")
    print(f"  This plan produced the best balance between simulated revenue")
    print(f"  recovery ({best_result.recovered}/{best_result.total_failed} payments, "
          f"{_format_amount(best_result.revenue_recovered)}),")
    print(f"  customer disturbance ({best_result.customer_facing_actions} customer-facing actions),")
    print(f"  and payment safety ({best_result.blocked_by_gate} safety blocks).")
    print()
    print("=" * 60)
    print()


# ---------- INTEGRATION WITH EVALUATOR ----------


def run_multi_step_incident_recovery(
    incident_payments: list[dict],
) -> dict:
    """Run multi-step recovery on incident payments and return the best plan.

    Used by the evaluator to compare against the baseline.

    Returns:
        {
            "plan": RecoveryPlan,
            "result": PlanResult,
            "score": int,
            "all_results": list of (plan, result, score) tuples
        }
    """
    results = []
    for plan in ALL_PLANS:
        result = simulate_recovery_plan(incident_payments, plan)
        score_val = score_plan(result)
        results.append((plan, result, score_val))

    results.sort(key=_plan_sort_key)
    best_plan, best_result, best_score = results[0]

    return {
        "plan": best_plan,
        "result": best_result,
        "score": best_score,
        "all_results": results,
    }


if __name__ == "__main__":
    run_demo()
