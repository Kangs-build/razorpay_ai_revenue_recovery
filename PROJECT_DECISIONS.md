# 📋 Project Decisions

**Razorpay AI Revenue Recovery — Track 3: AI Revenue Recovery**

This document records the key architectural and product decisions made at the start of the project.

---

## Decision 1: Track Selection

> **Track 3 — AI Revenue Recovery**

We chose Track 3 because payment failure recovery is a real, high-impact problem.
Most payment systems today retry failed payments in the same way, ignoring the root cause.
An AI-driven approach that understands *why* payments fail — and groups related failures —
can recover significantly more revenue than naive retry logic.

---

## Decision 2: Not All Failures Are Equal

> **We will not treat all failed payments the same way.**

A failed UPI payment due to "incorrect OTP" is very different from a batch failure caused by
a bank's API going down. Treating them identically leads to wasted retry attempts and poor
user experience. Our system will distinguish between failure types and respond accordingly.

---

## Decision 3: Group Related Failures into Incidents

> **We will study groups of related failures to detect payment incidents.**

A single failed payment is usually an individual issue. But when 10 payments from the same
bank using the same method fail with the same error in a short window, that's an **incident**
— a systemic problem that needs a coordinated response. Our Incident Detector is the first
step: it identifies these clusters before we build the recovery logic on top.

---

## Decision 4: Incident Recovery Twin

> **Our signature feature is the Incident Recovery Twin, which will later compare multiple recovery strategies before executing one.**

Inspired by the "digital twin" concept, the Incident Recovery Twin will simulate different
recovery plans for a detected incident (e.g., retry with delay, switch payment route,
notify user, escalate to bank) and predict which strategy will recover the most revenue
with the least user friction — *before actually executing it*. This is the core AI/ML
component we will build in future phases.

---

*Last updated: 2026-08-28*
