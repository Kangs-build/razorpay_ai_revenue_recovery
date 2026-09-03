import { useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  simulateIncident,
  checkHealth,
  type DecisionSnapshot,
  type PaymentEvent,
  type Incident,
  type Diagnosis,
  type PlanResult,
  type AuditEntry,
  type SafetyCheck,
} from "./lib/api";
import {
  formatINR,
  STEP_LABELS,
  createEventRevealTimer,
  getDemoPhaseSequence,
  createPhaseSequence,
} from "./lib/simulation";

/* ═══════════════════════════════════════════════════════════
   HEADER
   ═══════════════════════════════════════════════════════════ */

function Header({
  isRunning,
  onSimulate,
  backendStatus,
}: {
  isRunning: boolean;
  onSimulate: () => void;
  backendStatus: "connected" | "disconnected" | "checking";
}) {
  return (
    <header className="flex items-center justify-between px-8 py-5 border-b border-border-subtle bg-navy/60 backdrop-blur-xl sticky top-0 z-50">
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-cyan to-emerald flex items-center justify-center">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2L2 7l10 5 10-5-10-5z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
          </div>
          <span className="text-lg font-semibold text-text-primary tracking-tight">
            Revenue Recovery
          </span>
          <span className="text-xs font-medium text-cyan bg-cyan-dim px-2 py-0.5 rounded-full ml-1">
            AI
          </span>
        </div>
        <div className="h-5 w-px bg-border-subtle" />
        <span className="text-sm text-text-secondary">
          Razorpay AI Buildathon 2026 — Track 3
        </span>
        {/* Backend status indicator */}
        <div className="flex items-center gap-1.5 ml-2">
          <div
            className={`w-2 h-2 rounded-full ${
              backendStatus === "connected"
                ? "bg-emerald"
                : backendStatus === "disconnected"
                ? "bg-coral"
                : "bg-amber animate-pulse"
            }`}
          />
          <span className="text-[10px] text-text-muted">
            {backendStatus === "connected"
              ? "Backend connected"
              : backendStatus === "disconnected"
              ? "Backend disconnected"
              : "Checking…"}
          </span>
        </div>
      </div>
      <button
        onClick={onSimulate}
        disabled={isRunning || backendStatus !== "connected"}
        className="group relative px-5 py-2.5 rounded-xl font-medium text-sm transition-all duration-300 cursor-pointer disabled:cursor-not-allowed disabled:opacity-40"
        style={{
          background: isRunning
            ? "linear-gradient(135deg, #1a2340 0%, #151d35 100%)"
            : "linear-gradient(135deg, #00d4ff22 0%, #00e68a22 100%)",
          border: isRunning
            ? "1px solid #1e2a4a"
            : "1px solid #00d4ff44",
          color: isRunning ? "#5a6478" : "#00d4ff",
        }}
      >
        {isRunning ? (
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-cyan animate-pulse" />
            Running…
          </span>
        ) : (
          <span className="flex items-center gap-2">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="5 3 19 12 5 21 5 3" />
            </svg>
            Simulate BANK_X UPI Incident
          </span>
        )}
      </button>
    </header>
  );
}

/* ═══════════════════════════════════════════════════════════
   BACKEND DISCONNECTED STATE
   ═══════════════════════════════════════════════════════════ */

function BackendDisconnected({ onRetry }: { onRetry: () => void }) {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="flex flex-col items-center justify-center py-24"
    >
      <div className="text-center space-y-4 max-w-md">
        <div className="text-5xl mb-2">🔌</div>
        <h2 className="text-xl font-semibold text-coral">
          Backend Disconnected
        </h2>
        <p className="text-sm text-text-secondary">
          The Python backend API is not reachable. All calculation results
          come from the authoritative Python implementation — the dashboard
          cannot show fake data.
        </p>
        <p className="text-xs text-text-muted">
          Start the backend:{" "}
          <code className="text-cyan bg-navy-lighter px-1.5 py-0.5 rounded">
            python3 backend/api.py
          </code>
        </p>
        <button
          onClick={onRetry}
          className="mt-4 px-4 py-2 rounded-lg text-sm font-medium bg-cyan-dim text-cyan border border-cyan/20 hover:bg-cyan/20 transition-colors cursor-pointer"
        >
          Retry Connection
        </button>
      </div>
    </motion.div>
  );
}

/* ═══════════════════════════════════════════════════════════
   HERO — Revenue At Risk + Failure Rate
   ═══════════════════════════════════════════════════════════ */

function HeroMetrics({ incident }: { incident: Incident | null }) {
  const successCount = incident ? incident.totalPayments - incident.failedPayments : 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: incident ? 1 : 0.3, y: 0 }}
      transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
      className="grid grid-cols-1 md:grid-cols-4 gap-4 px-8 py-6"
    >
      <MetricCard
        label="Revenue At Risk"
        value={incident ? formatINR(incident.revenueAtRisk) : "₹0"}
        sub={`${incident?.affectedPaymentIds.length || 0} failed payments`}
        color="amber"
        large
      />
      <MetricCard
        label="Failure Rate"
        value={incident ? `${incident.failureRate}%` : "0%"}
        sub={`${incident?.failedPayments || 0} of ${incident?.totalPayments || 0}`}
        color="coral"
        large
      />
      <MetricCard
        label="Payments Processed"
        value={String(incident?.totalPayments || 0)}
        sub={`${successCount} successful`}
        color="cyan"
      />
      <MetricCard
        label="Incident Status"
        value={incident ? "DETECTED" : "MONITORING"}
        sub={incident ? `${incident.bank} ${incident.paymentMethod.toUpperCase()}` : "No active incidents"}
        color={incident ? "coral" : "emerald"}
      />
    </motion.div>
  );
}

function MetricCard({
  label,
  value,
  sub,
  color,
  large,
}: {
  label: string;
  value: string;
  sub: string;
  color: "cyan" | "emerald" | "amber" | "coral";
  large?: boolean;
}) {
  const colorMap = {
    cyan: { text: "text-cyan", bg: "bg-cyan-dim", border: "border-cyan/20" },
    emerald: { text: "text-emerald", bg: "bg-emerald-dim", border: "border-emerald/20" },
    amber: { text: "text-amber", bg: "bg-amber-dim", border: "border-amber/20" },
    coral: { text: "text-coral", bg: "bg-coral-dim", border: "border-coral/20" },
  };
  const c = colorMap[color];

  return (
    <div className={`card-glass rounded-2xl p-5 ${large ? "md:col-span-1" : ""}`}>
      <div className="text-xs font-medium text-text-secondary uppercase tracking-wider mb-2">
        {label}
      </div>
      <div className={`${c.text} ${large ? "text-3xl" : "text-2xl"} font-bold tracking-tight`}>
        {value}
      </div>
      <div className="text-xs text-text-muted mt-1">{sub}</div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   PAYMENT EVENT FEED
   ═══════════════════════════════════════════════════════════ */

function PaymentEventFeed({
  events,
  visibleCount,
}: {
  events: PaymentEvent[];
  visibleCount: number;
}) {
  const visible = events.slice(0, visibleCount);

  return (
    <div className="card-glass rounded-2xl overflow-hidden">
      <div className="px-5 py-4 border-b border-border-subtle flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-cyan animate-pulse" />
          <span className="text-sm font-semibold text-text-primary">Payment Events</span>
        </div>
        <span className="text-xs text-text-muted">
          {visibleCount}/{events.length} received
        </span>
      </div>
      <div className="max-h-[400px] overflow-y-auto">
        <AnimatePresence>
          {visible.map((event, i) => (
            <motion.div
              key={event.id}
              initial={{ opacity: 0, x: -20 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.3, delay: i * 0.02 }}
              className="flex items-center gap-3 px-5 py-2.5 border-b border-border-subtle/50 hover:bg-navy-hover/30 transition-colors"
            >
              <div
                className={`w-2 h-2 rounded-full flex-shrink-0 ${
                  event.status === "captured" ? "bg-emerald" : "bg-coral"
                }`}
              />
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-mono text-text-primary">{event.id}</span>
                  <span
                    className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
                      event.status === "captured"
                        ? "bg-emerald-dim text-emerald"
                        : "bg-coral-dim text-coral"
                    }`}
                  >
                    {event.status.toUpperCase()}
                  </span>
                </div>
                <div className="text-[11px] text-text-muted">
                  {event.eventType} · {event.bank} · {event.method.toUpperCase()} ·{" "}
                  {formatINR(event.amountPaise / 100)}
                  {event.errorReason && event.errorReason !== "none" && (
                    <span className="text-coral/70"> · {event.errorReason}</span>
                  )}
                </div>
              </div>
              <div className="text-[10px] text-text-dim font-mono">
                {new Date(event.timestampMs).toLocaleTimeString("en-US", {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                })}
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   INCIDENT CARD
   ═══════════════════════════════════════════════════════════ */

function IncidentCard({ incident }: { incident: Incident }) {
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
      className="card-glass-active rounded-2xl overflow-hidden"
    >
      <div className="px-5 py-4 border-b border-border-subtle bg-coral-glow">
        <div className="flex items-center gap-2">
          <span className="text-base">🚨</span>
          <span className="text-sm font-bold text-coral">INCIDENT DETECTED</span>
        </div>
      </div>
      <div className="p-5 space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <div>
            <div className="text-[11px] text-text-muted uppercase tracking-wider">Bank</div>
            <div className="text-lg font-bold text-text-primary">{incident.bank}</div>
          </div>
          <div>
            <div className="text-[11px] text-text-muted uppercase tracking-wider">Payment Method</div>
            <div className="text-lg font-bold text-text-primary">{incident.paymentMethod.toUpperCase()}</div>
          </div>
          <div>
            <div className="text-[11px] text-text-muted uppercase tracking-wider">Error Reason</div>
            <div className="text-sm font-medium text-coral">{incident.errorReason}</div>
          </div>
          <div>
            <div className="text-[11px] text-text-muted uppercase tracking-wider">Failure Rate</div>
            <div className="text-lg font-bold text-amber">{incident.failureRate}%</div>
          </div>
        </div>

        {/* Failure Rate Bar */}
        <div>
          <div className="flex justify-between text-[11px] text-text-muted mb-1">
            <span>Failed: {incident.failedPayments}/{incident.totalPayments}</span>
            <span>{formatINR(incident.revenueAtRisk)} at risk</span>
          </div>
          <div className="h-2.5 bg-navy-lighter rounded-full overflow-hidden">
            <motion.div
              initial={{ width: 0 }}
              animate={{ width: `${incident.failureRate}%` }}
              transition={{ duration: 1, ease: [0.22, 1, 0.36, 1] }}
              className="h-full rounded-full"
              style={{
                background: "linear-gradient(90deg, #ff4d6a 0%, #ffb020 100%)",
              }}
            />
          </div>
        </div>

        <div className="text-xs text-text-secondary leading-relaxed bg-navy-lighter/50 rounded-xl p-3 border border-border-subtle">
          Several {incident.bank} {incident.paymentMethod.toUpperCase()} payments failed with the same technical error
          within a short period, suggesting a possible common payment-system issue.
        </div>
      </div>
    </motion.div>
  );
}

/* ═══════════════════════════════════════════════════════════
   AI DIAGNOSIS CARD
   ═══════════════════════════════════════════════════════════ */

function DiagnosisCard({ diagnosis }: { diagnosis: Diagnosis }) {
  // Dynamic provider badge based on actual backend provider
  const providerBadge = (() => {
    switch (diagnosis.provider) {
      case "real_llm":
        return { label: "LIVE AI", color: "text-emerald bg-emerald-dim" };
      case "mock_fallback":
        return { label: "AI FALLBACK", color: "text-amber bg-amber-dim" };
      case "mock":
        return { label: "MOCK AI", color: "text-text-muted bg-navy-lighter" };
      default:
        return { label: diagnosis.provider, color: "text-text-muted bg-navy-lighter" };
    }
  })();

  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
      className="card-glass rounded-2xl overflow-hidden"
    >
      <div className="px-5 py-4 border-b border-border-subtle bg-cyan-glow">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#00d4ff" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a10 10 0 1 0 10 10" />
              <path d="M12 2v10l7-3" />
            </svg>
            <span className="text-sm font-bold text-cyan">AI DIAGNOSIS</span>
          </div>
          <span className={`text-[10px] font-bold px-2 py-0.5 rounded ${providerBadge.color}`}>
            {providerBadge.label}
          </span>
        </div>
      </div>
      <div className="p-5 space-y-4">
        <div className="grid grid-cols-3 gap-3">
          <div className="bg-navy-lighter/50 rounded-xl p-3 border border-border-subtle">
            <div className="text-[10px] text-text-muted uppercase tracking-wider mb-1">Root Cause</div>
            <div className="text-sm font-semibold text-cyan">
              {diagnosis.rootCause.replace(/_/g, " ")}
            </div>
          </div>
          <div className="bg-navy-lighter/50 rounded-xl p-3 border border-border-subtle">
            <div className="text-[10px] text-text-muted uppercase tracking-wider mb-1">Confidence</div>
            <div className={`text-sm font-semibold ${
              diagnosis.confidence === "high"
                ? "text-emerald"
                : diagnosis.confidence === "medium"
                ? "text-amber"
                : "text-coral"
            }`}>
              {diagnosis.confidence.toUpperCase()}
            </div>
          </div>
          <div className="bg-navy-lighter/50 rounded-xl p-3 border border-border-subtle">
            <div className="text-[10px] text-text-muted uppercase tracking-wider mb-1">Verification</div>
            <div className={`text-sm font-semibold ${
              diagnosis.verificationStatus === "SUPPORTED"
                ? "text-emerald"
                : "text-amber"
            }`}>
              {diagnosis.verificationStatus}
            </div>
          </div>
        </div>

        <div>
          <div className="text-[11px] text-text-muted uppercase tracking-wider mb-2">Evidence</div>
          <div className="space-y-1.5">
            {diagnosis.evidence.map((e, i) => (
              <div key={i} className="flex items-start gap-2 text-xs text-text-secondary">
                <span className="text-cyan mt-0.5">›</span>
                <span>{e}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="text-xs text-text-secondary leading-relaxed bg-navy-lighter/50 rounded-xl p-3 border border-border-subtle">
          {diagnosis.explanation}
        </div>
      </div>
    </motion.div>
  );
}

/* ═══════════════════════════════════════════════════════════
   RECOVERY TWIN — The hero component
   ═══════════════════════════════════════════════════════════ */

function RecoveryTwin({ plans }: { plans: PlanResult[] }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
    >
      <div className="flex items-center gap-2 mb-4">
        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-purple to-cyan flex items-center justify-center">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M8 14s1.5 2 4 2 4-2 4-2" />
            <line x1="9" y1="9" x2="9.01" y2="9" />
            <line x1="15" y1="9" x2="15.01" y2="9" />
          </svg>
        </div>
        <span className="text-sm font-bold text-text-primary">RECOVERY TWIN</span>
        <span className="text-xs text-text-muted">— comparing {plans.length} recovery plans</span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {plans.map((plan, i) => (
          <PlanCard key={plan.name} plan={plan} rank={i + 1} />
        ))}
      </div>
    </motion.div>
  );
}

function PlanCard({ plan, rank }: { plan: PlanResult; rank: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5, delay: rank * 0.1, ease: [0.22, 1, 0.36, 1] }}
      className={`rounded-2xl overflow-hidden transition-all duration-500 ${
        plan.isBest ? "plan-selected" : "card-glass"
      }`}
    >
      {/* Plan Header */}
      <div
        className={`px-4 py-3 border-b ${
          plan.isBest
            ? "border-emerald/30 bg-emerald-glow"
            : "border-border-subtle bg-navy-light/50"
        }`}
      >
        <div className="flex items-center justify-between mb-1">
          <span className={`text-xs font-bold ${plan.isBest ? "text-emerald" : "text-text-secondary"}`}>
            {plan.name.replace("PLAN_", "PLAN ")}
          </span>
          {plan.isBest && (
            <span className="text-[9px] font-bold text-emerald bg-emerald-dim px-1.5 py-0.5 rounded">
              ✓ SELECTED
            </span>
          )}
        </div>
        <div className="text-[11px] text-text-muted">{plan.description}</div>
      </div>

      {/* Step Flow */}
      <div className="px-4 py-3 border-b border-border-subtle/50">
        <div className="text-[9px] text-text-dim uppercase tracking-wider mb-2">Recovery Flow</div>
        <div className="flex items-center gap-1 flex-wrap">
          {plan.steps.map((step, si) => {
            const s = STEP_LABELS[step] || { label: step, icon: "?" };
            return (
              <div key={si} className="flex items-center gap-1">
                {si > 0 && <span className="step-arrow">→</span>}
                <div
                  className={`text-[10px] font-medium px-2 py-1 rounded-lg border ${
                    plan.isBest
                      ? "border-emerald/20 bg-emerald-glow text-emerald"
                      : "border-border-subtle bg-navy-lighter text-text-secondary"
                  }`}
                >
                  {s.icon} {s.label}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Scores — ALL from backend */}
      <div className="px-4 py-3 space-y-2">
        <div className="flex justify-between items-center">
          <span className="text-[10px] text-text-muted">Simulation</span>
          <span className="text-xs font-semibold text-text-primary">{plan.simulationScore}/100</span>
        </div>
        {plan.contextBonus > 0 && (
          <div className="flex justify-between items-center">
            <span className="text-[10px] text-text-muted">AI Context</span>
            <span className="text-xs font-semibold text-cyan">+{plan.contextBonus}</span>
          </div>
        )}
        <div className="flex justify-between items-center">
          <span className="text-[10px] text-text-muted font-semibold">Final Score</span>
          <span className={`text-lg font-bold ${plan.isBest ? "text-emerald" : "text-text-primary"}`}>
            {plan.finalScore}
          </span>
        </div>

        {/* Score bar */}
        <div className="h-1.5 bg-navy-lighter rounded-full overflow-hidden">
          <motion.div
            initial={{ width: 0 }}
            animate={{ width: `${plan.finalScore}%` }}
            transition={{ duration: 0.8, delay: rank * 0.15, ease: [0.22, 1, 0.36, 1] }}
            className={`h-full rounded-full ${plan.isBest ? "bg-emerald" : "bg-cyan/60"}`}
          />
        </div>

        {/* Recovery stats — ALL from backend */}
        <div className="grid grid-cols-2 gap-2 mt-2">
          <div className="bg-navy-lighter/50 rounded-lg p-2 text-center">
            <div className="text-[10px] text-text-muted">Sim. Recovered</div>
            <div className={`text-sm font-bold ${plan.isBest ? "text-emerald" : "text-text-primary"}`}>
              {plan.recovered}/{plan.totalFailed}
            </div>
          </div>
          <div className="bg-navy-lighter/50 rounded-lg p-2 text-center">
            <div className="text-[10px] text-text-muted">Attempts</div>
            <div className="text-sm font-bold text-text-primary">{plan.recoveryAttempts}</div>
          </div>
        </div>

        <div className="flex justify-between text-[10px] text-text-muted pt-1">
          <span>Customer: {plan.customerFacingActions}</span>
          <span>Blocked: {plan.blockedByGate}</span>
        </div>
      </div>
    </motion.div>
  );
}

/* ═══════════════════════════════════════════════════════════
   SAFETY GATE
   ═══════════════════════════════════════════════════════════ */

function SafetyGateCard({ safety }: { safety: SafetyCheck }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
      className="card-glass rounded-2xl overflow-hidden"
    >
      <div className={`px-5 py-4 border-b border-border-subtle ${
        safety.allowed ? "bg-emerald-glow" : "bg-coral-glow"
      }`}>
        <div className="flex items-center gap-2">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke={safety.allowed ? "#00e68a" : "#ff4d6a"} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          </svg>
          <span className={`text-sm font-bold ${safety.allowed ? "text-emerald" : "text-coral"}`}>
            SAFETY GATE — {safety.allowed ? "ALLOWED" : "BLOCKED"}
          </span>
        </div>
      </div>
      <div className="p-5">
        <div className="space-y-1.5">
          {safety.reasons.map((reason, i) => (
            <div key={i} className="flex items-start gap-2 text-xs text-text-secondary">
              <span className={safety.allowed ? "text-emerald" : "text-coral"}>›</span>
              <span>{reason}</span>
            </div>
          ))}
        </div>
      </div>
    </motion.div>
  );
}

/* ═══════════════════════════════════════════════════════════
   AUDIT TRAIL
   ═══════════════════════════════════════════════════════════ */

function AuditTrailComponent({ entries }: { entries: AuditEntry[] }) {
  return (
    <div className="card-glass rounded-2xl overflow-hidden">
      <div className="px-5 py-4 border-b border-border-subtle">
        <span className="text-sm font-semibold text-text-primary">Audit Trail</span>
      </div>
      <div className="max-h-[300px] overflow-y-auto">
        {entries.map((entry, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.2, delay: i * 0.03 }}
            className="flex items-start gap-3 px-5 py-2.5 border-b border-border-subtle/30 hover:bg-navy-hover/20 transition-colors"
          >
            <div className="w-1.5 h-1.5 rounded-full bg-cyan/50 mt-1.5 flex-shrink-0" />
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium text-text-primary">{entry.action}</div>
              <div className="text-[11px] text-text-muted truncate">{entry.detail}</div>
            </div>
            <div className="text-[10px] text-text-dim font-mono flex-shrink-0">
              {entry.timestamp.slice(11, 19)}
            </div>
          </motion.div>
        ))}
      </div>
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   DECISION SUMMARY
   ═══════════════════════════════════════════════════════════ */

function DecisionSummary({ decision }: { decision: DecisionSnapshot }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5 }}
      className="card-glass-active rounded-2xl overflow-hidden"
    >
      <div className="px-5 py-4 border-b border-emerald/20 bg-emerald-glow">
        <div className="flex items-center gap-2">
          <span className="text-base">✅</span>
          <span className="text-sm font-bold text-emerald">DECISION RECORDED</span>
          <span className="text-xs text-text-muted font-mono ml-2">{decision.decisionId}</span>
        </div>
      </div>
      <div className="p-5">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <div>
            <div className="text-[10px] text-text-muted uppercase tracking-wider">Selected Plan</div>
            <div className="text-sm font-bold text-emerald">{decision.selectedPlan.replace("PLAN_", "")}</div>
          </div>
          <div>
            <div className="text-[10px] text-text-muted uppercase tracking-wider">Simulated Recovered</div>
            <div className="text-sm font-bold text-emerald">
              {decision.recovered}/{decision.incident.failedPayments}
            </div>
          </div>
          <div>
            <div className="text-[10px] text-text-muted uppercase tracking-wider">Simulated Revenue Recovered</div>
            <div className="text-sm font-bold text-emerald">{formatINR(decision.revenueRecovered)}</div>
          </div>
          <div>
            <div className="text-[10px] text-text-muted uppercase tracking-wider">Final Score</div>
            <div className="text-sm font-bold text-emerald">{decision.plans[0]?.finalScore}/100</div>
          </div>
        </div>
      </div>
    </motion.div>
  );
}

/* ═══════════════════════════════════════════════════════════
   PROGRESS PIPELINE
   ═══════════════════════════════════════════════════════════ */

const PIPELINE_STAGES = [
  { key: "events", label: "Payments", icon: "💳" },
  { key: "incident", label: "Incident", icon: "🚨" },
  { key: "diagnosis", label: "AI", icon: "🧠" },
  { key: "twin", label: "Twin", icon: "🔄" },
  { key: "safety", label: "Safety", icon: "🛡️" },
  { key: "decision", label: "Decision", icon: "✅" },
];

function ProgressPipeline({ activeStages }: { activeStages: string[] }) {
  return (
    <div className="flex items-center justify-center gap-2 px-8 py-4">
      {PIPELINE_STAGES.map((stage, i) => {
        const isActive = activeStages.includes(stage.key);
        return (
          <div key={stage.key} className="flex items-center gap-2">
            {i > 0 && (
              <div
                className={`w-8 h-px transition-colors duration-500 ${
                  isActive ? "bg-cyan" : "bg-border-subtle"
                }`}
              />
            )}
            <div
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium transition-all duration-500 ${
                isActive
                  ? "bg-cyan-dim text-cyan border border-cyan/20"
                  : "bg-navy-lighter text-text-dim border border-border-subtle"
              }`}
            >
              <span>{stage.icon}</span>
              <span>{stage.label}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ═══════════════════════════════════════════════════════════
   MAIN APP
   ═══════════════════════════════════════════════════════════ */

type Phase = "idle" | "events" | "incident" | "ai" | "twin" | "safety" | "done";

export default function App() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [events, setEvents] = useState<PaymentEvent[]>([]);
  const [visibleEvents, setVisibleEvents] = useState(0);
  const [decision, setDecision] = useState<DecisionSnapshot | null>(null);
  const [activeStages, setActiveStages] = useState<string[]>([]);
  const [backendStatus, setBackendStatus] = useState<"connected" | "disconnected" | "checking">("checking");

  // Check backend health on mount
  const checkBackend = useCallback(async () => {
    setBackendStatus("checking");
    try {
      await checkHealth();
      setBackendStatus("connected");
    } catch {
      setBackendStatus("disconnected");
    }
  }, []);

  // Initial health check
  useState(() => {
    checkBackend();
  });

  const runSimulation = useCallback(async () => {
    if (backendStatus !== "connected") return;

    // Reset UI state
    setPhase("idle");
    setDecision(null);
    setVisibleEvents(0);
    setActiveStages([]);

    try {
      // Call the real backend — this runs the actual Python orchestrator
      const result = await simulateIncident();

      if (!result.success || !result.decision) {
        console.error("Demo simulation failed:", result);
        return;
      }

      const dec = result.decision;
      const paymentEvts = dec.paymentEvents || [];

      setEvents(paymentEvts);

      // Phase 1: Feed events one by one (UI timing only)
      setPhase("events");
      setActiveStages(["events"]);

      const cleanup = createEventRevealTimer(
        paymentEvts.length,
        (count) => setVisibleEvents(count),
        150
      );

      // After events are revealed, progressively show backend results
      const phaseCleanup = createPhaseSequence(
        getDemoPhaseSequence(paymentEvts.length),
        (p, stages) => {
          setPhase(p as Phase);
          setActiveStages(stages);
          if (p === "twin") {
            setDecision(dec);
          }
        }
      );

      // Cleanup timers on unmount (React will handle this via cleanup)
      return () => {
        cleanup();
        phaseCleanup();
      };
    } catch (err) {
      console.error("Failed to run demo:", err);
      setBackendStatus("disconnected");
    }
  }, [backendStatus]);

  if (backendStatus === "disconnected") {
    return (
      <div className="min-h-screen bg-midnight">
        <Header
          isRunning={false}
          onSimulate={() => {}}
          backendStatus="disconnected"
        />
        <BackendDisconnected onRetry={checkBackend} />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-midnight">
      <Header
        isRunning={phase !== "idle" && phase !== "done"}
        onSimulate={runSimulation}
        backendStatus={backendStatus}
      />
      <ProgressPipeline activeStages={activeStages} />
      <HeroMetrics incident={decision?.incident ?? null} />

      <div className="px-8 pb-12 space-y-6">
        {/* Event Feed */}
        {events.length > 0 && (
          <PaymentEventFeed events={events} visibleCount={visibleEvents} />
        )}

        {/* Incident Card */}
        {(phase === "incident" || phase === "ai" || phase === "twin" || phase === "safety" || phase === "done") &&
          decision?.incident && <IncidentCard incident={decision.incident} />}

        {/* AI Diagnosis */}
        {(phase === "ai" || phase === "twin" || phase === "safety" || phase === "done") &&
          decision?.diagnosis && <DiagnosisCard diagnosis={decision.diagnosis} />}

        {/* Recovery Twin */}
        {(phase === "twin" || phase === "safety" || phase === "done") &&
          decision?.plans && <RecoveryTwin plans={decision.plans} />}

        {/* Safety Gate */}
        {(phase === "safety" || phase === "done") && decision?.safety && (
          <SafetyGateCard safety={decision.safety} />
        )}

        {/* Decision Summary */}
        {phase === "done" && decision && <DecisionSummary decision={decision} />}

        {/* Audit Trail */}
        {phase === "done" && decision?.auditTrail && (
          <AuditTrailComponent entries={decision.auditTrail} />
        )}

        {/* Idle State */}
        {phase === "idle" && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="flex flex-col items-center justify-center py-24"
          >
            <div className="text-center space-y-4">
              <div className="text-6xl mb-4">🔐</div>
              <h2 className="text-xl font-semibold text-text-primary">
                AI Revenue Recovery Dashboard
              </h2>
              <p className="text-sm text-text-secondary max-w-md">
                Click <span className="text-cyan font-medium">"Simulate BANK_X UPI Incident"</span> to
                watch the <span className="text-text-primary font-medium">real Python backend</span> detect
                a payment incident, diagnose it with AI,
                compare recovery plans, and make a safety-verified decision.
              </p>
              <div className="flex items-center justify-center gap-6 mt-6 text-xs text-text-muted">
                <span>💳 Payments</span>
                <span className="text-border-subtle">→</span>
                <span>🚨 Incident</span>
                <span className="text-border-subtle">→</span>
                <span>🧠 AI</span>
                <span className="text-border-subtle">→</span>
                <span>🔄 Twin</span>
                <span className="text-border-subtle">→</span>
                <span>🛡️ Safety</span>
                <span className="text-border-subtle">→</span>
                <span>✅ Decision</span>
              </div>
            </div>
          </motion.div>
        )}
      </div>
    </div>
  );
}
