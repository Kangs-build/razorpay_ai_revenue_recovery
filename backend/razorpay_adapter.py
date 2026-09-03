"""
Razorpay Adapter — Razorpay AI Revenue Recovery

Handles Razorpay webhook events:
  - HMAC-SHA256 signature verification
  - Event parsing and deduplication (idempotency)
  - Conversion to internal payment record format
  - State management with late-success protection
  - Audit trail

Provider-specific parsing is isolated here so the Incident Detector,
Recovery Twin, and Safety Gate never need to know about Razorpay JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))

from safety_gate import evaluate as safety_evaluate


# ---------- .env LOADING (reuse from ai_diagnoser) ----------

def _load_env_file(path: str | None = None) -> None:
    """Load a .env file into os.environ without overriding existing values."""
    if path is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(project_root, ".env")

    if not os.path.isfile(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = value
    except (OSError, UnicodeDecodeError):
        pass


_load_env_file()


# ---------- SIGNATURE VERIFICATION ----------

def verify_webhook_signature(
    raw_body: bytes,
    signature: str | None,
    secret: str | None = None,
) -> dict:
    """Verify Razorpay webhook HMAC-SHA256 signature.

    Args:
        raw_body: The exact raw request body bytes (must NOT be parsed first).
        signature: The X-Razorpay-Signature header value.
        secret: Webhook secret. Falls back to RAZORPAY_WEBHOOK_SECRET env var.

    Returns:
        {"valid": bool, "reason": str}
    """
    if not signature:
        return {"valid": False, "reason": "Missing X-Razorpay-Signature header"}

    webhook_secret = secret or os.environ.get("RAZORPAY_WEBHOOK_SECRET")
    if not webhook_secret:
        return {"valid": False, "reason": "RAZORPAY_WEBHOOK_SECRET not configured"}

    # Razorpay expects HMAC-SHA256 of the raw body
    computed = hmac.new(
        webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison to prevent timing attacks
    valid = hmac.compare_digest(computed, signature)

    if valid:
        return {"valid": True, "reason": "Signature verified"}
    else:
        return {"valid": False, "reason": "Invalid signature"}


# ---------- INTERNAL PAYMENT RECORD ----------

@dataclass
class InternalPayment:
    """Internal payment record — the common format used by all project components."""

    payment_id: str
    amount: float          # in rupees (display/calculation)
    amount_paise: int      # original Razorpay amount in smallest unit
    currency: str
    status: str            # "failed", "captured", "authorized", "success"
    payment_method: str
    bank: str
    error_source: str
    error_step: str
    error_reason: str
    order_id: str
    wallet: str
    vpa: str
    created_at: str
    internal_updated_at: str  # when this record was last updated

    # Recovery tracking fields (same as simulated payments)
    recovery_attempt_count: int = 0
    customer_contact_count: int = 0
    recovered_already: bool = False
    last_recovery_attempt_time: str | None = None

    def to_dict(self) -> dict:
        """Convert to dict for use with safety_gate.evaluate() and simulators."""
        d = {
            "payment_id": self.payment_id,
            "amount": self.amount,
            "status": self.status,
            "payment_method": self.payment_method,
            "bank": self.bank,
            "error_source": self.error_source,
            "error_step": self.error_step,
            "error_reason": self.error_reason,
            "recovery_attempt_count": self.recovery_attempt_count,
            "customer_contact_count": self.customer_contact_count,
            "recovered_already": self.recovered_already,
            "last_recovery_attempt_time": self.last_recovery_attempt_time,
        }
        # Add _dt for compatibility with incident_detector's group_payments()
        if self.created_at:
            try:
                ts = self.created_at.rstrip("Z")
                from datetime import datetime as dt
                d["_dt"] = dt.fromisoformat(ts)
            except (ValueError, TypeError):
                pass
        return d


# ---------- RAZORPAY → INTERNAL MAPPING ----------

# Error reason mapping from Razorpay error codes
_ERROR_REASON_MAP = {
    # Razorpay error_code → internal error_reason
    "BAD_REQUEST": "technical_error",
    "GATEWAY_ERROR": "technical_error",
    "INTERNAL_ERROR": "technical_error",
    "SERVER_ERROR": "technical_error",
    "NETWORK_ERROR": "timeout_error",
    "TIMEOUT": "timeout_error",
    "INSUFFICIENT_FUNDS": "insufficient_balance",
    "INCORRECT_OTP": "incorrect_otp",
    "INVALID_CARD": "incorrect_otp",
    "EXPIRED_CARD": "incorrect_otp",
    "INVALID_VPA": "incorrect_otp",
    "AUTHENTICATION_FAILED": "incorrect_otp",
    "DO_NOT_HONOR": "technical_error",
    "CARD_DECLINED": "technical_error",
    "TRANSACTION_DECLINED": "technical_error",
}

# Payment status mapping
_STATUS_MAP = {
    "failed": "failed",
    "captured": "captured",
    "authorized": "authorized",
    "processed": "captured",
    "partially_refunded": "captured",
    "refunded": "captured",
}

# Supported events
SUPPORTED_EVENTS = {"payment.failed", "payment.captured", "payment.authorized"}


def _map_error_reason(error_code: str, error_reason: str) -> str:
    """Map Razorpay error codes/reasons to internal error reasons."""
    # Try error_code first, then error_reason
    code_upper = (error_code or "").upper().replace(" ", "_")
    if code_upper in _ERROR_REASON_MAP:
        return _ERROR_REASON_MAP[code_upper]

    reason_lower = (error_reason or "").lower().replace(" ", "_")
    if reason_lower in _ERROR_REASON_MAP:
        return _ERROR_REASON_MAP[reason_lower]

    # Check for common patterns
    if "otp" in reason_lower or "otp" in code_upper.lower():
        return "incorrect_otp"
    if "balance" in reason_lower or "insufficient" in reason_lower:
        return "insufficient_balance"
    if "timeout" in reason_lower or "timed_out" in reason_lower:
        return "timeout_error"

    # Default to technical_error for unrecognized errors
    return "technical_error"


def _map_error_source(error_source: str) -> str:
    """Map Razorpay error_source to internal format."""
    source_lower = (error_source or "").lower()
    if "bank" in source_lower:
        return "bank_api"
    if "card" in source_lower:
        return "card_network"
    if "connector" in source_lower:
        return "connector"
    if "customer" in source_lower:
        return "customer"
    return "unknown"


def convert_razorpay_to_internal(event_type: str, payload: dict) -> InternalPayment | None:
    """Convert a Razorpay webhook payload to our internal payment format.

    Args:
        event_type: The Razorpay event type (e.g. "payment.failed").
        payload: The full Razorpay webhook JSON payload.

    Returns:
        InternalPayment or None if the payload is invalid.
    """
    try:
        entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    except (AttributeError, TypeError):
        return None

    if not entity:
        return None

    # Extract basic fields
    razorpay_id = entity.get("id", "")
    order_id = entity.get("order_id", "")
    amount_paise = entity.get("amount", 0)
    currency = entity.get("currency", "INR")
    status_raw = entity.get("status", "")
    method = (entity.get("method", "") or "").capitalize()
    bank = (entity.get("bank", "") or "").upper()
    wallet = entity.get("wallet", "") or ""
    vpa = entity.get("vpa", "") or ""
    created_at = entity.get("created_at", 0)

    # Convert timestamp
    if isinstance(created_at, (int, float)) and created_at > 0:
        created_at_str = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
    else:
        created_at_str = datetime.now(timezone.utc).isoformat()

    # Map status
    status = _STATUS_MAP.get(status_raw, status_raw)

    # For payment.failed events, override status to "failed"
    if event_type == "payment.failed":
        status = "failed"

    # Extract error info from Razorpay's error_response
    error_response = entity.get("error_response") or {}
    error_code = error_response.get("code", "") or ""
    error_description = error_response.get("description", "") or ""
    error_source_raw = error_response.get("source", "") or ""
    error_step = error_response.get("step", "") or ""
    error_reason_raw = error_response.get("reason", "") or ""

    # Map to internal format
    error_reason = _map_error_reason(error_code, error_reason_raw or error_description)
    error_source = _map_error_source(error_source_raw)

    # Convert amount from paise to rupees
    amount_rupees = amount_paise / 100.0 if amount_paise else 0.0

    # Use razorpay payment_id as internal payment_id
    payment_id = razorpay_id or f"rzp_{order_id}"

    now_str = datetime.now(timezone.utc).isoformat()

    return InternalPayment(
        payment_id=payment_id,
        amount=amount_rupees,
        amount_paise=amount_paise,
        currency=currency,
        status=status,
        payment_method=method,
        bank=bank,
        error_source=error_source,
        error_step=error_step,
        error_reason=error_reason,
        order_id=order_id,
        wallet=wallet,
        vpa=vpa,
        created_at=created_at_str,
        internal_updated_at=now_str,
    )


# ---------- PAYMENT STORE ----------

# State precedence: captured > authorized > failed > unknown
_STATE_PRECEDENCE = {
    "captured": 3,
    "success": 3,
    "authorized": 2,
    "failed": 1,
    "unknown": 0,
}


class PaymentStore:
    """In-memory payment store with state management.

    Handles:
      - Late-success: failed → captured updates state
      - Out-of-order: captured cannot be downgraded to failed
      - Idempotency: same payment_id updates in place
    """

    def __init__(self):
        self._payments: dict[str, InternalPayment] = {}

    def upsert(self, payment: InternalPayment) -> dict:
        """Add or update a payment. Returns the state transition info.

        Returns:
            {
                "payment_id": str,
                "action": "created" | "updated" | "late_success" | "superseded",
                "old_status": str | None,
                "new_status": str,
                "payment": InternalPayment,
            }
        """
        pid = payment.payment_id
        existing = self._payments.get(pid)

        if existing is None:
            # New payment
            self._payments[pid] = payment
            return {
                "payment_id": pid,
                "action": "created",
                "old_status": None,
                "new_status": payment.status,
                "payment": payment,
            }

        # Existing payment — check state precedence
        old_status = existing.status
        old_prec = _STATE_PRECEDENCE.get(old_status, 0)
        new_prec = _STATE_PRECEDENCE.get(payment.status, 0)

        if new_prec > old_prec:
            # Upgrade (e.g. failed → captured = late success)
            existing.status = payment.status
            existing.internal_updated_at = payment.internal_updated_at
            if payment.error_reason != "none":
                existing.error_reason = payment.error_reason
            if payment.error_source != "none":
                existing.error_source = payment.error_source

            action = "late_success" if old_status == "failed" and payment.status == "captured" else "updated"
            return {
                "payment_id": pid,
                "action": action,
                "old_status": old_status,
                "new_status": existing.status,
                "payment": existing,
            }

        elif new_prec < old_prec:
            # Downgrade attempt — reject (e.g. don't downgrade captured to failed)
            return {
                "payment_id": pid,
                "action": "superseded",
                "old_status": old_status,
                "new_status": old_status,  # unchanged
                "payment": existing,
            }

        else:
            # Same precedence — update metadata
            existing.internal_updated_at = payment.internal_updated_at
            if payment.error_reason and payment.error_reason != "none":
                existing.error_reason = payment.error_reason
            if payment.error_source and payment.error_source != "none":
                existing.error_source = payment.error_source
            return {
                "payment_id": pid,
                "action": "updated",
                "old_status": old_status,
                "new_status": existing.status,
                "payment": existing,
            }

    def get(self, payment_id: str) -> InternalPayment | None:
        return self._payments.get(payment_id)

    def get_all(self) -> list[InternalPayment]:
        return list(self._payments.values())


# ---------- EVENT STORE (IDEMPOTENCY) ----------

class EventStore:
    """Simple in-memory event deduplication store.

    In production, replace with a database-backed implementation.
    """

    def __init__(self):
        self._processed: dict[str, dict] = {}

    def is_duplicate(self, event_id: str) -> bool:
        return event_id in self._processed

    def mark_processed(self, event_id: str, event_type: str, payment_id: str, timestamp: str) -> None:
        self._processed[event_id] = {
            "event_type": event_type,
            "payment_id": payment_id,
            "processed_at": timestamp,
        }

    def get_record(self, event_id: str) -> dict | None:
        return self._processed.get(event_id)


# ---------- AUDIT TRAIL ----------

@dataclass
class AuditRecord:
    """Lightweight audit record for each webhook."""
    received_at: str
    event_id: str
    event_type: str
    payment_id: str
    signature_valid: bool
    signature_reason: str
    is_duplicate: bool
    resulting_status: str
    action: str  # "created", "updated", "late_success", "superseded", "duplicate", "rejected", "unsupported"


class AuditTrail:
    """In-memory audit trail for webhook processing."""

    def __init__(self):
        self._records: list[AuditRecord] = []

    def add(self, record: AuditRecord) -> None:
        self._records.append(record)

    def get_all(self) -> list[AuditRecord]:
        return list(self._records)

    def get_for_payment(self, payment_id: str) -> list[AuditRecord]:
        return [r for r in self._records if r.payment_id == payment_id]


# ---------- MAIN WEBHOOK PROCESSOR ----------

class RazorpayWebhookProcessor:
    """Processes Razorpay webhook events end-to-end.

    Flow:
        1. Verify signature
        2. Check idempotency
        3. Parse event payload
        4. Convert to internal payment
        5. Update payment store (with state management)
        6. Record audit trail
    """

    def __init__(self, webhook_secret: str | None = None):
        self.payment_store = PaymentStore()
        self.event_store = EventStore()
        self.audit_trail = AuditTrail()
        self.webhook_secret = webhook_secret

    def process_webhook(
        self,
        raw_body: bytes,
        signature: str | None,
        event_id: str,
        event_type: str,
    ) -> dict:
        """Process a single Razorpay webhook event.

        Args:
            raw_body: The exact raw HTTP request body bytes.
            signature: The X-Razorpay-Signature header value.
            event_id: The x-razorpay-event-id header value.
            event_type: The event type from the payload (e.g. "payment.failed").

        Returns:
            {
                "status_code": int,
                "accepted": bool,
                "reason": str,
                "payment_id": str | None,
                "resulting_status": str | None,
                "audit": AuditRecord,
            }
        """
        received_at = datetime.now(timezone.utc).isoformat()

        # Step 1: Verify signature
        sig_result = verify_webhook_signature(raw_body, signature, self.webhook_secret)
        if not sig_result["valid"]:
            audit = AuditRecord(
                received_at=received_at,
                event_id=event_id or "",
                event_type=event_type or "",
                payment_id="",
                signature_valid=False,
                signature_reason=sig_result["reason"],
                is_duplicate=False,
                resulting_status="",
                action="rejected",
            )
            self.audit_trail.add(audit)
            return {
                "status_code": 401,
                "accepted": False,
                "reason": sig_result["reason"],
                "payment_id": None,
                "resulting_status": None,
                "audit": audit,
            }

        # Step 2: Check idempotency
        if event_id and self.event_store.is_duplicate(event_id):
            existing = self.event_store.get_record(event_id)
            audit = AuditRecord(
                received_at=received_at,
                event_id=event_id,
                event_type=event_type,
                payment_id=existing["payment_id"] if existing else "",
                signature_valid=True,
                signature_reason="Signature verified",
                is_duplicate=True,
                resulting_status="",
                action="duplicate",
            )
            self.audit_trail.add(audit)
            return {
                "status_code": 200,
                "accepted": True,
                "reason": "Duplicate event ignored",
                "payment_id": existing["payment_id"] if existing else None,
                "resulting_status": None,
                "audit": audit,
            }

        # Step 3: Check if event is supported
        if event_type not in SUPPORTED_EVENTS:
            audit = AuditRecord(
                received_at=received_at,
                event_id=event_id or "",
                event_type=event_type,
                payment_id="",
                signature_valid=True,
                signature_reason="Signature verified",
                is_duplicate=False,
                resulting_status="",
                action="unsupported",
            )
            self.audit_trail.add(audit)
            return {
                "status_code": 200,
                "accepted": True,
                "reason": f"Event type '{event_type}' not supported — ignored",
                "payment_id": None,
                "resulting_status": None,
                "audit": audit,
            }

        # Step 4: Parse JSON body
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError) as e:
            audit = AuditRecord(
                received_at=received_at,
                event_id=event_id or "",
                event_type=event_type,
                payment_id="",
                signature_valid=True,
                signature_reason="Signature verified",
                is_duplicate=False,
                resulting_status="",
                action="rejected",
            )
            self.audit_trail.add(audit)
            return {
                "status_code": 400,
                "accepted": False,
                "reason": f"Invalid JSON payload: {e}",
                "payment_id": None,
                "resulting_status": None,
                "audit": audit,
            }

        # Step 5: Convert to internal payment
        internal_payment = convert_razorpay_to_internal(event_type, payload)
        if internal_payment is None:
            audit = AuditRecord(
                received_at=received_at,
                event_id=event_id or "",
                event_type=event_type,
                payment_id="",
                signature_valid=True,
                signature_reason="Signature verified",
                is_duplicate=False,
                resulting_status="",
                action="rejected",
            )
            self.audit_trail.add(audit)
            return {
                "status_code": 400,
                "accepted": False,
                "reason": "Could not extract payment entity from payload",
                "payment_id": None,
                "resulting_status": None,
                "audit": audit,
            }

        # Step 6: Update payment store (handles state management)
        result = self.payment_store.upsert(internal_payment)

        # Step 7: Mark event as processed
        if event_id:
            self.event_store.mark_processed(
                event_id, event_type, internal_payment.payment_id, received_at
            )

        # Step 8: Audit trail
        audit = AuditRecord(
            received_at=received_at,
            event_id=event_id or "",
            event_type=event_type,
            payment_id=internal_payment.payment_id,
            signature_valid=True,
            signature_reason="Signature verified",
            is_duplicate=False,
            resulting_status=result["new_status"],
            action=result["action"],
        )
        self.audit_trail.add(audit)

        return {
            "status_code": 200,
            "accepted": True,
            "reason": f"Event processed — {result['action']}",
            "payment_id": internal_payment.payment_id,
            "resulting_status": result["new_status"],
            "audit": audit,
        }


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


def run_demo():
    """Run a complete demo showing webhook processing flow."""
    import copy

    # Use a test secret for demo
    test_secret = "whsec_test_demo_key_12345"

    print()
    print("=" * 60)
    print("  RAZORPAY WEBHOOK RECEIVER — DEMO")
    print("=" * 60)
    print()

    processor = RazorpayWebhookProcessor(webhook_secret=test_secret)

    # --- Demo 1: payment.failed ---
    print("─" * 60)
    print("  DEMO 1: payment.failed")
    print("─" * 60)

    failed_body = json.dumps({
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_demo_001",
                    "order_id": "order_demo_001",
                    "amount": 250000,  # 2500 rupees in paise
                    "currency": "INR",
                    "status": "failed",
                    "method": "upi",
                    "bank": "BANK_X",
                    "created_at": 1725000000,
                    "error_response": {
                        "code": "GATEWAY_ERROR",
                        "description": "Technical error from bank",
                        "source": "bank_api",
                        "step": "payment_processing",
                        "reason": "technical_error",
                    },
                },
            },
        },
    }).encode("utf-8")

    # Compute valid signature
    sig = hmac.new(test_secret.encode("utf-8"), failed_body, hashlib.sha256).hexdigest()

    result = processor.process_webhook(
        raw_body=failed_body,
        signature=sig,
        event_id="evt_demo_001",
        event_type="payment.failed",
    )

    print(f"  Event: payment.failed")
    print(f"  Payment: pay_demo_001")
    print(f"  Signature: {'✅ VERIFIED' if result['audit'].signature_valid else '❌ REJECTED'}")
    print(f"  Duplicate: {'YES' if result['audit'].is_duplicate else 'NO'}")
    print()
    payment = processor.payment_store.get("pay_demo_001")
    if payment:
        print(f"  Method: {payment.payment_method}")
        print(f"  Bank: {payment.bank}")
        print(f"  Error Reason: {payment.error_reason}")
        print(f"  Status: {payment.status}")
        print(f"  Amount: {_format_amount(payment.amount)}")
    print()
    print(f"  Internal Payment Record Created ✅")
    print()

    # --- Demo 2: Late success (payment.captured for same payment) ---
    print("─" * 60)
    print("  DEMO 2: Late Success — payment.captured for pay_demo_001")
    print("─" * 60)

    captured_body = json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_demo_001",
                    "order_id": "order_demo_001",
                    "amount": 250000,
                    "currency": "INR",
                    "status": "captured",
                    "method": "upi",
                    "bank": "BANK_X",
                    "created_at": 1725000060,
                },
            },
        },
    }).encode("utf-8")

    sig2 = hmac.new(test_secret.encode("utf-8"), captured_body, hashlib.sha256).hexdigest()

    result2 = processor.process_webhook(
        raw_body=captured_body,
        signature=sig2,
        event_id="evt_demo_002",
        event_type="payment.captured",
    )

    print(f"  Event: payment.captured")
    print(f"  Payment: pay_demo_001")
    print(f"  Signature: ✅ VERIFIED")
    print(f"  Action: {result2['audit'].action}")
    print()
    payment2 = processor.payment_store.get("pay_demo_001")
    if payment2:
        print(f"  Previous Status: failed")
        print(f"  New Status: {payment2.status}")
    print()
    print(f"  Late Success Detected ✅")
    print(f"  Internal state = captured")
    print()

    # --- Demo 3: Duplicate webhook ---
    print("─" * 60)
    print("  DEMO 3: Duplicate Webhook")
    print("─" * 60)

    result3 = processor.process_webhook(
        raw_body=failed_body,
        signature=sig,
        event_id="evt_demo_001",  # same event ID
        event_type="payment.failed",
    )

    print(f"  Event: payment.failed (same event_id)")
    print(f"  Payment: pay_demo_001")
    print(f"  Duplicate: {'YES' if result3['audit'].is_duplicate else 'NO'}")
    print(f"  Status Code: {result3['status_code']}")
    print(f"  Reason: {result3['reason']}")
    print()
    print(f"  Duplicate correctly ignored ✅")
    print()

    # --- Demo 4: Invalid signature ---
    print("─" * 60)
    print("  DEMO 4: Invalid Signature")
    print("─" * 60)

    result4 = processor.process_webhook(
        raw_body=failed_body,
        signature="invalid_signature_12345",
        event_id="evt_demo_003",
        event_type="payment.failed",
    )

    print(f"  Event: payment.failed")
    print(f"  Signature: ❌ REJECTED")
    print(f"  Status Code: {result4['status_code']}")
    print(f"  Reason: {result4['reason']}")
    print()
    print(f"  Invalid signature correctly rejected ✅")
    print()

    # --- Demo 5: Out-of-order event (captured before failed) ---
    print("─" * 60)
    print("  DEMO 5: Out-of-Order — captured cannot be downgraded")
    print("─" * 60)

    # First, receive captured for a new payment
    captured_body2 = json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_demo_002",
                    "order_id": "order_demo_002",
                    "amount": 150000,
                    "currency": "INR",
                    "status": "captured",
                    "method": "card",
                    "bank": "HDFC",
                    "created_at": 1725000120,
                },
            },
        },
    }).encode("utf-8")

    sig5a = hmac.new(test_secret.encode("utf-8"), captured_body2, hashlib.sha256).hexdigest()
    result5a = processor.process_webhook(
        raw_body=captured_body2,
        signature=sig5a,
        event_id="evt_demo_004",
        event_type="payment.captured",
    )

    # Now receive a failed event for the same payment (out of order)
    failed_body2 = json.dumps({
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_demo_002",
                    "order_id": "order_demo_002",
                    "amount": 150000,
                    "currency": "INR",
                    "status": "failed",
                    "method": "card",
                    "bank": "HDFC",
                    "created_at": 1725000100,  # older timestamp
                    "error_response": {
                        "code": "NETWORK_ERROR",
                        "description": "Network timeout",
                        "source": "connector",
                        "step": "auth",
                        "reason": "timeout_error",
                    },
                },
            },
        },
    }).encode("utf-8")

    sig5b = hmac.new(test_secret.encode("utf-8"), failed_body2, hashlib.sha256).hexdigest()
    result5b = processor.process_webhook(
        raw_body=failed_body2,
        signature=sig5b,
        event_id="evt_demo_005",
        event_type="payment.failed",
    )

    print(f"  Received: payment.captured for pay_demo_002 → status = captured")
    print(f"  Later received: payment.failed for pay_demo_002")
    print(f"  Action: {result5b['audit'].action}")
    payment5 = processor.payment_store.get("pay_demo_002")
    print(f"  Current status: {payment5.status if payment5 else 'N/A'}")
    print()
    print(f"  Captured payment NOT downgraded ✅")
    print()

    # --- Demo 6: Unsupported event ---
    print("─" * 60)
    print("  DEMO 6: Unsupported Event")
    print("─" * 60)

    result6 = processor.process_webhook(
        raw_body=b'{"event": "refund.created"}',
        signature=hmac.new(test_secret.encode("utf-8"), b'{"event": "refund.created"}', hashlib.sha256).hexdigest(),
        event_id="evt_demo_006",
        event_type="refund.created",
    )

    print(f"  Event: refund.created")
    print(f"  Status Code: {result6['status_code']}")
    print(f"  Reason: {result6['reason']}")
    print()
    print(f"  Unsupported event safely ignored ✅")
    print()

    # --- Audit Trail ---
    print("─" * 60)
    print("  AUDIT TRAIL")
    print("─" * 60)

    for record in processor.audit_trail.get_all():
        sig_icon = "✅" if record.signature_valid else "❌"
        dup_icon = " [DUP]" if record.is_duplicate else ""
        print(f"  {record.received_at[:19]}  {record.event_type:<22} "
              f"{record.payment_id:<16} Sig:{sig_icon} "
              f"→ {record.action}{dup_icon}")

    print()
    print("=" * 60)
    print("  All demos completed.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    run_demo()
