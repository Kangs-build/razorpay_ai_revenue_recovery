/**
 * UI Timing Helpers — Presentation layer only.
 *
 * This file contains ONLY:
 * - UI timing/animation helpers
 * - INR formatting utilities
 * - Type re-exports from the API layer
 *
 * Business logic is entirely in the Python backend (backend/recovery_orchestrator.py).
 * Do NOT add incident detection, scoring, safety gate, or recovery logic here.
 *
 * DEMO FALLBACK DATA: This file contains NO fallback/sample data.
 * All data comes from the backend API.
 */

import type {
  DecisionSnapshot,
  PaymentEvent,
  Incident,
  Diagnosis,
  PlanResult,
  AuditEntry,
  SafetyCheck,
} from "./api";

// Re-export types for convenience
export type {
  DecisionSnapshot,
  PaymentEvent,
  Incident,
  Diagnosis,
  PlanResult,
  AuditEntry,
  SafetyCheck,
};

// ─── INR Formatting ───

export function formatINR(amount: number): string {
  const s = amount.toFixed(2);
  const [integerPart, decimalPart] = s.split(".");
  if (integerPart.length <= 3) return `₹${integerPart}.${decimalPart}`;
  const lastThree = integerPart.slice(-3);
  const remaining = integerPart.slice(0, -3);
  const groups: string[] = [];
  let r = remaining;
  while (r.length > 0) {
    groups.unshift(r.slice(-2));
    r = r.slice(0, -2);
  }
  return `₹${groups.join(",")},${lastThree}.${decimalPart}`;
}

export function formatINRShort(amount: number): string {
  if (amount >= 100000) return `₹${(amount / 100000).toFixed(1)}L`;
  if (amount >= 1000) return `₹${(amount / 1000).toFixed(1)}K`;
  return `₹${amount.toFixed(0)}`;
}

// ─── UI Timing Helpers ───

/**
 * Progressively reveal payment events with a staggered animation.
 * Returns a cleanup function to cancel the animation.
 */
export function createEventRevealTimer(
  totalEvents: number,
  onReveal: (count: number) => void,
  intervalMs: number = 150
): () => void {
  let step = 0;
  const timer = setInterval(() => {
    step++;
    onReveal(step);
    if (step >= totalEvents) {
      clearInterval(timer);
    }
  }, intervalMs);
  return () => clearInterval(timer);
}

/**
 * Create a sequence of timed phase transitions.
 * Returns a cleanup function to cancel all pending transitions.
 */
export function createPhaseSequence(
  phases: Array<{ delay: number; phase: string; stages: string[] }>,
  onPhase: (phase: string, stages: string[]) => void
): () => void {
  const timers: ReturnType<typeof setTimeout>[] = [];
  phases.forEach(({ delay, phase, stages }) => {
    timers.push(setTimeout(() => onPhase(phase, stages), delay));
  });
  return () => timers.forEach(clearTimeout);
}

/**
 * Build the standard demo phase sequence.
 * This controls UI timing only — all data comes from the backend.
 */
export function getDemoPhaseSequence(eventCount: number): Array<{
  delay: number;
  phase: string;
  stages: string[];
}> {
  // Events take eventCount * 150ms to reveal
  const eventsDone = eventCount * 150 + 200;

  return [
    { delay: eventsDone, phase: "incident", stages: ["events", "incident"] },
    { delay: eventsDone + 800, phase: "ai", stages: ["events", "incident", "diagnosis"] },
    { delay: eventsDone + 1800, phase: "twin", stages: ["events", "incident", "diagnosis", "twin"] },
    { delay: eventsDone + 2800, phase: "safety", stages: ["events", "incident", "diagnosis", "twin", "safety"] },
    { delay: eventsDone + 3600, phase: "done", stages: ["events", "incident", "diagnosis", "twin", "safety", "decision"] },
  ];
}

// ─── Step Labels for Recovery Twin ───

export const STEP_LABELS: Record<string, { label: string; icon: string }> = {
  WAIT_AND_RECHECK: { label: "WAIT", icon: "⏳" },
  RETRY_LATER: { label: "RETRY", icon: "🔄" },
  SUGGEST_ALTERNATE_METHOD: { label: "ALTERNATE", icon: "🔀" },
  SEND_PAYMENT_LINK: { label: "LINK", icon: "🔗" },
  CUSTOMER_RETRY: { label: "RETRY", icon: "👤" },
};
