/**
 * API Client — communicates with the real Python backend.
 *
 * All calculation results come from the authoritative Python implementation.
 * The frontend only visualizes what the backend produces.
 *
 * During development, Vite proxies /api/* → http://127.0.0.1:5001/*
 * In production, set VITE_API_URL to the backend URL.
 */

const API_BASE = import.meta.env.VITE_API_URL || "/api";

export interface PaymentEvent {
  id: string;
  orderId: string;
  amountPaise: number;
  method: string;
  bank: string;
  status: "failed" | "captured" | "authorized";
  eventType: string;
  errorCode?: string;
  errorReason?: string;
  errorSource?: string;
  timestampMs: number;
}

export interface Incident {
  id: string;
  bank: string;
  paymentMethod: string;
  errorReason: string;
  totalPayments: number;
  failedPayments: number;
  failureRate: number;
  revenueAtRisk: number;
  affectedPaymentIds: string[];
}

export interface Diagnosis {
  provider: string;
  rootCause: string;
  confidence: string;
  evidence: string[];
  explanation: string;
  verificationStatus: string;
}

export interface PlanResult {
  name: string;
  description: string;
  steps: string[];
  recovered: number;
  totalFailed: number;
  revenueRecovered: number;
  revenueAtRisk: number;
  unresolved: number;
  recoveryAttempts: number;
  customerFacingActions: number;
  blockedByGate: number;
  simulationScore: number;
  contextBonus: number;
  finalScore: number;
  isBest: boolean;
}

export interface SafetyCheck {
  strategy: string;
  allowed: boolean;
  reasons: string[];
}

export interface AuditEntry {
  timestamp: string;
  action: string;
  detail: string;
}

export interface DecisionSnapshot {
  decisionId: string;
  incident: Incident;
  diagnosis: Diagnosis;
  plans: PlanResult[];
  selectedPlan: string;
  safety: SafetyCheck;
  recovered: number;
  revenueRecovered: number;
  unresolvedRevenue: number;
  auditTrail: AuditEntry[];
  paymentEvents: PaymentEvent[];
}

export interface HealthResponse {
  status: string;
  payments_stored: number;
  decisions_count: number;
  demo_available: boolean;
}

// ─── API calls ───

export async function checkHealth(): Promise<HealthResponse> {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) throw new Error(`Backend health check failed: ${res.status}`);
  return res.json();
}

export async function simulateIncident(): Promise<{
  success: boolean;
  demo: boolean;
  eventsProcessed: number;
  decision: DecisionSnapshot;
}> {
  const res = await fetch(`${API_BASE}/demo/simulate-incident`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: "Unknown error" }));
    throw new Error(body.error || `Demo failed: ${res.status}`);
  }
  return res.json();
}

export async function getPayments(): Promise<{
  payments: Record<string, unknown>[];
  count: number;
}> {
  const res = await fetch(`${API_BASE}/payments`);
  if (!res.ok) throw new Error(`Failed to fetch payments: ${res.status}`);
  return res.json();
}

export async function getDecisions(): Promise<{
  decisions: Record<string, unknown>[];
  count: number;
}> {
  const res = await fetch(`${API_BASE}/decisions`);
  if (!res.ok) throw new Error(`Failed to fetch decisions: ${res.status}`);
  return res.json();
}

export async function getAudit(): Promise<{
  entries: Record<string, unknown>[];
  count: number;
}> {
  const res = await fetch(`${API_BASE}/audit`);
  if (!res.ok) throw new Error(`Failed to fetch audit: ${res.status}`);
  return res.json();
}
