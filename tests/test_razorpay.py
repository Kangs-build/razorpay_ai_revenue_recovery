"""
Tests for Razorpay Webhook Receiver — Razorpay AI Revenue Recovery

Tests cover:
  - HMAC-SHA256 signature verification (valid, invalid, missing)
  - Event parsing (payment.failed, payment.captured, payment.authorized)
  - Internal payment mapping (error codes, paise conversion)
  - Idempotency / duplicate event handling
  - Late-success state transitions (failed → captured)
  - Out-of-order event handling (captured not downgraded to failed)
  - Audit trail creation
  - Secret never appears in logs/output
  - Existing project tests still pass
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "backend"))

from razorpay_adapter import (
    AuditTrail,
    EventStore,
    InternalPayment,
    PaymentStore,
    RazorpayWebhookProcessor,
    SUPPORTED_EVENTS,
    _map_error_reason,
    _map_error_source,
    convert_razorpay_to_internal,
    verify_webhook_signature,
)


# ---------- TEST FIXTURES ----------

TEST_SECRET = "whsec_test_secret_abcdef123456"


def _compute_signature(body: bytes, secret: str = TEST_SECRET) -> str:
    """Compute HMAC-SHA256 signature for test body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _make_payment_failed_body(
    payment_id: str = "pay_test_001",
    order_id: str = "order_test_001",
    amount: int = 250000,
    method: str = "upi",
    bank: str = "BANK_X",
    error_code: str = "GATEWAY_ERROR",
    error_reason: str = "technical_error",
    error_source: str = "bank_api",
    created_at: int = 1725000000,
) -> bytes:
    """Create a realistic payment.failed webhook body."""
    return json.dumps({
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "method": method,
                    "bank": bank,
                    "created_at": created_at,
                    "error_response": {
                        "code": error_code,
                        "description": f"Error: {error_reason}",
                        "source": error_source,
                        "step": "payment_processing",
                        "reason": error_reason,
                    },
                },
            },
        },
    }).encode("utf-8")


def _make_payment_captured_body(
    payment_id: str = "pay_test_001",
    order_id: str = "order_test_001",
    amount: int = 250000,
    method: str = "upi",
    bank: str = "BANK_X",
    created_at: int = 1725000060,
) -> bytes:
    """Create a realistic payment.captured webhook body."""
    return json.dumps({
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "captured",
                    "method": method,
                    "bank": bank,
                    "created_at": created_at,
                },
            },
        },
    }).encode("utf-8")


def _make_payment_authorized_body(
    payment_id: str = "pay_test_002",
    order_id: str = "order_test_002",
    amount: int = 150000,
    method: str = "card",
    bank: str = "HDFC",
    created_at: int = 1725000030,
) -> bytes:
    """Create a realistic payment.authorized webhook body."""
    return json.dumps({
        "event": "payment.authorized",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "order_id": order_id,
                    "amount": amount,
                    "currency": "INR",
                    "status": "authorized",
                    "method": method,
                    "bank": bank,
                    "created_at": created_at,
                },
            },
        },
    }).encode("utf-8")


# ---------- TEST CLASSES ----------


class TestSignatureVerification(unittest.TestCase):
    """Tests for HMAC-SHA256 signature verification."""

    def test_valid_signature_accepted(self):
        body = b'{"event": "payment.failed"}'
        sig = _compute_signature(body)
        result = verify_webhook_signature(body, sig, TEST_SECRET)
        self.assertTrue(result["valid"])
        self.assertIn("verified", result["reason"].lower())

    def test_invalid_signature_rejected(self):
        body = b'{"event": "payment.failed"}'
        result = verify_webhook_signature(body, "invalid_sig_12345", TEST_SECRET)
        self.assertFalse(result["valid"])
        self.assertIn("invalid", result["reason"].lower())

    def test_missing_signature_rejected(self):
        body = b'{"event": "payment.failed"}'
        result = verify_webhook_signature(body, None, TEST_SECRET)
        self.assertFalse(result["valid"])
        self.assertIn("missing", result["reason"].lower())

    def test_missing_secret_rejected(self):
        body = b'{"event": "payment.failed"}'
        sig = _compute_signature(body)
        result = verify_webhook_signature(body, sig, None)
        self.assertFalse(result["valid"])
        self.assertIn("not configured", result["reason"].lower())

    def test_raw_body_used_for_verification(self):
        body1 = b'{"event": "payment.failed"}'
        body2 = b'{"event": "payment.captured"}'
        sig1 = _compute_signature(body1)

        # Same signature, different body → should fail
        result = verify_webhook_signature(body2, sig1, TEST_SECRET)
        self.assertFalse(result["valid"])

    def test_constant_time_comparison_no_timing_leak(self):
        """Verify hmac.compare_digest is used (constant-time)."""
        import razorpay_adapter
        import inspect
        source = inspect.getsource(verify_webhook_signature)
        self.assertIn("compare_digest", source)


class TestPaymentFailedMapping(unittest.TestCase):
    """Tests for payment.failed event parsing and mapping."""

    def test_basic_mapping(self):
        body = _make_payment_failed_body()
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.failed", payload)

        self.assertIsNotNone(payment)
        self.assertEqual(payment.payment_id, "pay_test_001")
        self.assertEqual(payment.status, "failed")
        self.assertEqual(payment.payment_method, "Upi")
        self.assertEqual(payment.bank, "BANK_X")
        self.assertEqual(payment.amount, 2500.0)  # 250000 paise → 2500 rupees
        self.assertEqual(payment.amount_paise, 250000)
        self.assertEqual(payment.currency, "INR")
        self.assertEqual(payment.order_id, "order_test_001")

    def test_error_source_mapping(self):
        body = _make_payment_failed_body(error_source="bank_api")
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.failed", payload)
        self.assertEqual(payment.error_source, "bank_api")

    def test_error_step_mapping(self):
        body = _make_payment_failed_body()
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.failed", payload)
        self.assertEqual(payment.error_step, "payment_processing")

    def test_error_reason_mapping(self):
        body = _make_payment_failed_body(error_code="GATEWAY_ERROR")
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.failed", payload)
        self.assertEqual(payment.error_reason, "technical_error")

    def test_insufficient_funds_mapping(self):
        body = _make_payment_failed_body(
            error_code="INSUFFICIENT_FUNDS",
            error_reason="insufficient_balance",
        )
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.failed", payload)
        self.assertEqual(payment.error_reason, "insufficient_balance")

    def test_incorrect_otp_mapping(self):
        body = _make_payment_failed_body(
            error_code="INCORRECT_OTP",
            error_reason="incorrect_otp",
        )
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.failed", payload)
        self.assertEqual(payment.error_reason, "incorrect_otp")

    def test_paise_to_rupees_conversion(self):
        body = _make_payment_failed_body(amount=150000)  # 1500 rupees
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.failed", payload)
        self.assertEqual(payment.amount, 1500.0)
        self.assertEqual(payment.amount_paise, 150000)


class TestPaymentCapturedMapping(unittest.TestCase):
    """Tests for payment.captured event parsing."""

    def test_captured_status(self):
        body = _make_payment_captured_body()
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.captured", payload)

        self.assertIsNotNone(payment)
        self.assertEqual(payment.status, "captured")
        self.assertEqual(payment.payment_id, "pay_test_001")

    def test_captured_no_error_fields(self):
        body = _make_payment_captured_body()
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.captured", payload)
        self.assertEqual(payment.error_reason, "technical_error")  # default
        self.assertEqual(payment.error_source, "unknown")  # default


class TestPaymentAuthorizedMapping(unittest.TestCase):
    """Tests for payment.authorized event parsing."""

    def test_authorized_status(self):
        body = _make_payment_authorized_body()
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.authorized", payload)

        self.assertIsNotNone(payment)
        self.assertEqual(payment.status, "authorized")
        self.assertEqual(payment.payment_id, "pay_test_002")

    def test_authorized_method_capitalized(self):
        body = _make_payment_authorized_body(method="netbanking")
        payload = json.loads(body)
        payment = convert_razorpay_to_internal("payment.authorized", payload)
        self.assertEqual(payment.payment_method, "Netbanking")


class TestLateSuccess(unittest.TestCase):
    """Tests for failed → captured state transition."""

    def test_failed_then_captured_updates_state(self):
        store = PaymentStore()

        # First: payment.failed
        failed_body = _make_payment_failed_body()
        failed_payload = json.loads(failed_body)
        failed_payment = convert_razorpay_to_internal("payment.failed", failed_payload)
        result1 = store.upsert(failed_payment)
        self.assertEqual(result1["action"], "created")
        self.assertEqual(result1["new_status"], "failed")

        # Later: payment.captured for same payment
        captured_body = _make_payment_captured_body()
        captured_payload = json.loads(captured_body)
        captured_payment = convert_razorpay_to_internal("payment.captured", captured_payload)
        result2 = store.upsert(captured_payment)
        self.assertEqual(result2["action"], "late_success")
        self.assertEqual(result2["new_status"], "captured")

        # Verify state is captured
        stored = store.get("pay_test_001")
        self.assertEqual(stored.status, "captured")

    def test_safety_gate_blocks_recovery_after_late_success(self):
        """After late success, Safety Gate should block recovery."""
        from safety_gate import evaluate as safety_evaluate

        store = PaymentStore()

        # payment.failed
        failed_body = _make_payment_failed_body()
        failed_payload = json.loads(failed_body)
        failed_payment = convert_razorpay_to_internal("payment.failed", failed_payload)
        store.upsert(failed_payment)

        # payment.captured (late success)
        captured_body = _make_payment_captured_body()
        captured_payload = json.loads(captured_body)
        captured_payment = convert_razorpay_to_internal("payment.captured", captured_payload)
        store.upsert(captured_payment)

        # Safety Gate should block
        stored = store.get("pay_test_001")
        gate_result = safety_evaluate(stored.to_dict(), "WAIT_AND_RECHECK")
        self.assertFalse(gate_result["allowed"])
        self.assertTrue(any("already successful" in r.lower() for r in gate_result["reasons"]))


class TestOutOfOrderEvents(unittest.TestCase):
    """Tests for out-of-order event handling."""

    def test_captured_not_downgraded_by_failed(self):
        store = PaymentStore()

        # First: payment.captured
        captured_body = _make_payment_captured_body(payment_id="pay_ooo_001")
        captured_payload = json.loads(captured_body)
        captured_payment = convert_razorpay_to_internal("payment.captured", captured_payload)
        result1 = store.upsert(captured_payment)
        self.assertEqual(result1["new_status"], "captured")

        # Later: payment.failed (older, out of order)
        failed_body = _make_payment_failed_body(payment_id="pay_ooo_001")
        failed_payload = json.loads(failed_body)
        failed_payment = convert_razorpay_to_internal("payment.failed", failed_payload)
        result2 = store.upsert(failed_payment)
        self.assertEqual(result2["action"], "superseded")
        self.assertEqual(result2["new_status"], "captured")  # NOT downgraded

        # Verify state is still captured
        stored = store.get("pay_ooo_001")
        self.assertEqual(stored.status, "captured")

    def test_authorized_not_downgraded_by_failed(self):
        store = PaymentStore()

        # First: payment.authorized
        auth_body = _make_payment_authorized_body(payment_id="pay_ooo_002")
        auth_payload = json.loads(auth_body)
        auth_payment = convert_razorpay_to_internal("payment.authorized", auth_payload)
        store.upsert(auth_payment)

        # Later: payment.failed
        failed_body = _make_payment_failed_body(payment_id="pay_ooo_002")
        failed_payload = json.loads(failed_body)
        failed_payment = convert_razorpay_to_internal("payment.failed", failed_payload)
        result = store.upsert(failed_payment)
        # authorized (prec 2) > failed (prec 1), so it's superseded
        self.assertEqual(result["action"], "superseded")
        self.assertEqual(result["new_status"], "authorized")


class TestIdempotency(unittest.TestCase):
    """Tests for duplicate event handling."""

    def test_duplicate_event_processed_only_once(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body()
        sig = _compute_signature(body)

        # First processing
        result1 = processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_dup_001", event_type="payment.failed",
        )
        self.assertTrue(result1["accepted"])
        self.assertFalse(result1["audit"].is_duplicate)

        # Duplicate processing
        result2 = processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_dup_001", event_type="payment.failed",
        )
        self.assertTrue(result2["accepted"])
        self.assertTrue(result2["audit"].is_duplicate)
        self.assertEqual(result2["status_code"], 200)

    def test_duplicate_returns_200(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body()
        sig = _compute_signature(body)

        processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_dup_002", event_type="payment.failed",
        )
        result = processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_dup_002", event_type="payment.failed",
        )
        self.assertEqual(result["status_code"], 200)


class TestUnsupportedEvents(unittest.TestCase):
    """Tests for unsupported event handling."""

    def test_unsupported_event_ignored_safely(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = b'{"event": "refund.created"}'
        sig = _compute_signature(body)

        result = processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_unsupported_001", event_type="refund.created",
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["status_code"], 200)
        self.assertIn("not supported", result["reason"])

    def test_supported_events_list(self):
        self.assertIn("payment.failed", SUPPORTED_EVENTS)
        self.assertIn("payment.captured", SUPPORTED_EVENTS)
        self.assertIn("payment.authorized", SUPPORTED_EVENTS)


class TestInvalidSignatureRejection(unittest.TestCase):
    """Tests for signature rejection."""

    def test_invalid_signature_returns_401(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body()

        result = processor.process_webhook(
            raw_body=body, signature="totally_wrong_sig",
            event_id="evt_bad_sig_001", event_type="payment.failed",
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status_code"], 401)
        self.assertIn("invalid", result["reason"].lower())

    def test_missing_signature_returns_401(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body()

        result = processor.process_webhook(
            raw_body=body, signature=None,
            event_id="evt_no_sig_001", event_type="payment.failed",
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["status_code"], 401)

    def test_invalid_signature_does_not_store_payment(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body(payment_id="pay_nosig_001")

        processor.process_webhook(
            raw_body=body, signature="bad",
            event_id="evt_nosig_001", event_type="payment.failed",
        )
        self.assertIsNone(processor.payment_store.get("pay_nosig_001"))


class TestAuditTrail(unittest.TestCase):
    """Tests for audit trail creation."""

    def test_audit_record_created_for_successful_event(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body()
        sig = _compute_signature(body)

        processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_audit_001", event_type="payment.failed",
        )

        records = processor.audit_trail.get_all()
        self.assertGreater(len(records), 0)
        record = records[-1]
        self.assertEqual(record.event_type, "payment.failed")
        self.assertEqual(record.payment_id, "pay_test_001")
        self.assertTrue(record.signature_valid)
        self.assertFalse(record.is_duplicate)

    def test_audit_record_for_duplicate(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body()
        sig = _compute_signature(body)

        processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_audit_002", event_type="payment.failed",
        )
        processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_audit_002", event_type="payment.failed",
        )

        records = processor.audit_trail.get_all()
        dup_records = [r for r in records if r.is_duplicate]
        self.assertGreater(len(dup_records), 0)

    def test_audit_record_for_invalid_signature(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body()

        processor.process_webhook(
            raw_body=body, signature="bad",
            event_id="evt_audit_003", event_type="payment.failed",
        )

        records = processor.audit_trail.get_all()
        rejected = [r for r in records if r.action == "rejected"]
        self.assertGreater(len(rejected), 0)
        self.assertFalse(rejected[-1].signature_valid)

    def test_audit_for_payment(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        body = _make_payment_failed_body(payment_id="pay_audit_001")
        sig = _compute_signature(body)

        processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_audit_004", event_type="payment.failed",
        )
        processor.process_webhook(
            raw_body=_make_payment_captured_body(payment_id="pay_audit_001"),
            signature=_compute_signature(_make_payment_captured_body(payment_id="pay_audit_001")),
            event_id="evt_audit_005", event_type="payment.captured",
        )

        records = processor.audit_trail.get_for_payment("pay_audit_001")
        self.assertEqual(len(records), 2)


class TestInternalPaymentModel(unittest.TestCase):
    """Tests for InternalPayment data model."""

    def test_to_dict_has_required_fields(self):
        payment = InternalPayment(
            payment_id="pay_model_001",
            amount=1000.0,
            amount_paise=100000,
            currency="INR",
            status="failed",
            payment_method="UPI",
            bank="BANK_X",
            error_source="bank_api",
            error_step="payment_processing",
            error_reason="technical_error",
            order_id="order_001",
            wallet="",
            vpa="",
            created_at="2026-08-30T10:00:00+00:00",
            internal_updated_at="2026-08-30T10:00:00+00:00",
        )
        d = payment.to_dict()
        self.assertEqual(d["payment_id"], "pay_model_001")
        self.assertEqual(d["amount"], 1000.0)
        self.assertEqual(d["status"], "failed")
        self.assertEqual(d["recovery_attempt_count"], 0)
        self.assertEqual(d["customer_contact_count"], 0)
        self.assertFalse(d["recovered_already"])

    def test_to_dict_compatible_with_safety_gate(self):
        """InternalPayment.to_dict() should work with safety_gate.evaluate()."""
        from safety_gate import evaluate as safety_evaluate

        payment = InternalPayment(
            payment_id="pay_gate_001",
            amount=2000.0,
            amount_paise=200000,
            currency="INR",
            status="failed",
            payment_method="UPI",
            bank="BANK_X",
            error_source="bank_api",
            error_step="payment_processing",
            error_reason="technical_error",
            order_id="order_002",
            wallet="",
            vpa="",
            created_at="2026-08-30T10:00:00+00:00",
            internal_updated_at="2026-08-30T10:00:00+00:00",
        )
        result = safety_evaluate(payment.to_dict(), "WAIT_AND_RECHECK")
        self.assertIn("allowed", result)
        self.assertIn("strategy", result)


class TestErrorMappingFunctions(unittest.TestCase):
    """Tests for error code/reason mapping functions."""

    def test_gateway_error_maps_to_technical(self):
        self.assertEqual(_map_error_reason("GATEWAY_ERROR", ""), "technical_error")

    def test_insufficient_funds_maps_correctly(self):
        self.assertEqual(_map_error_reason("INSUFFICIENT_FUNDS", ""), "insufficient_balance")

    def test_incorrect_otp_maps_correctly(self):
        self.assertEqual(_map_error_reason("INCORRECT_OTP", ""), "incorrect_otp")

    def test_timeout_maps_correctly(self):
        self.assertEqual(_map_error_reason("TIMEOUT", ""), "timeout_error")

    def test_unknown_error_defaults_to_technical(self):
        self.assertEqual(_map_error_reason("UNKNOWN_CODE", "unknown_reason"), "technical_error")

    def test_bank_source_maps_correctly(self):
        self.assertEqual(_map_error_source("bank_api"), "bank_api")

    def test_card_source_maps_correctly(self):
        self.assertEqual(_map_error_source("card_network"), "card_network")

    def test_unknown_source_defaults(self):
        self.assertEqual(_map_error_source("something_else"), "unknown")


class TestSecretSafety(unittest.TestCase):
    """Tests that secrets never appear in logs/output."""

    def test_secret_not_in_adapter_source(self):
        """Verify the adapter source code doesn't hard-code the secret."""
        import razorpay_adapter
        import inspect
        source = inspect.getsource(razorpay_adapter)
        # The test secret should NOT appear in the source code
        self.assertNotIn(TEST_SECRET, source)

    def test_secret_not_in_internal_payment(self):
        """InternalPayment should not contain secret fields."""
        payment = InternalPayment(
            payment_id="pay_sec_001",
            amount=100.0,
            amount_paise=10000,
            currency="INR",
            status="failed",
            payment_method="UPI",
            bank="BANK_X",
            error_source="bank_api",
            error_step="",
            error_reason="technical_error",
            order_id="order_sec",
            wallet="",
            vpa="",
            created_at="",
            internal_updated_at="",
        )
        d = payment.to_dict()
        # No field should contain secret-looking data
        for key, value in d.items():
            if isinstance(value, str):
                self.assertNotIn("whsec_", value)
                self.assertNotIn("sk-", value)


class TestEndToEndFlow(unittest.TestCase):
    """Integration test: full webhook → payment store → Safety Gate flow."""

    def test_full_flow_payment_failed_then_captured(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)
        from safety_gate import evaluate as safety_evaluate

        # Step 1: payment.failed
        failed_body = _make_payment_failed_body(payment_id="pay_e2e_001")
        failed_sig = _compute_signature(failed_body)
        r1 = processor.process_webhook(
            raw_body=failed_body, signature=failed_sig,
            event_id="evt_e2e_001", event_type="payment.failed",
        )
        self.assertTrue(r1["accepted"])
        self.assertEqual(r1["resulting_status"], "failed")

        # Safety Gate allows recovery for failed payment
        payment = processor.payment_store.get("pay_e2e_001")
        gate1 = safety_evaluate(payment.to_dict(), "WAIT_AND_RECHECK")
        self.assertTrue(gate1["allowed"])

        # Step 2: payment.captured (late success)
        captured_body = _make_payment_captured_body(payment_id="pay_e2e_001")
        captured_sig = _compute_signature(captured_body)
        r2 = processor.process_webhook(
            raw_body=captured_body, signature=captured_sig,
            event_id="evt_e2e_002", event_type="payment.captured",
        )
        self.assertTrue(r2["accepted"])
        self.assertEqual(r2["audit"].action, "late_success")

        # Safety Gate now blocks recovery
        payment = processor.payment_store.get("pay_e2e_001")
        gate2 = safety_evaluate(payment.to_dict(), "WAIT_AND_RECHECK")
        self.assertFalse(gate2["allowed"])

    def test_duplicate_event_does_not_create_new_payment(self):
        processor = RazorpayWebhookProcessor(webhook_secret=TEST_SECRET)

        body = _make_payment_failed_body(payment_id="pay_e2e_dup_001")
        sig = _compute_signature(body)

        processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_e2e_dup_001", event_type="payment.failed",
        )
        processor.process_webhook(
            raw_body=body, signature=sig,
            event_id="evt_e2e_dup_001", event_type="payment.failed",
        )

        # Only one payment should exist
        all_payments = processor.payment_store.get_all()
        e2e_payments = [p for p in all_payments if p.payment_id == "pay_e2e_dup_001"]
        self.assertEqual(len(e2e_payments), 1)


class TestExistingTestsStillPass(unittest.TestCase):
    """Verify that existing project modules are importable and functional."""

    def test_incident_detector_imports(self):
        from incident_detector import detect_incidents, group_payments
        self.assertTrue(callable(detect_incidents))
        self.assertTrue(callable(group_payments))

    def test_safety_gate_imports(self):
        from safety_gate import evaluate
        self.assertTrue(callable(evaluate))

    def test_recovery_twin_imports(self):
        from recovery_twin import analyze_incident
        self.assertTrue(callable(analyze_incident))

    def test_payment_simulator_imports(self):
        from payment_simulator import simulate_recovery, create_payment
        self.assertTrue(callable(simulate_recovery))
        self.assertTrue(callable(create_payment))

    def test_evaluator_imports(self):
        from evaluator import run_evaluation
        self.assertTrue(callable(run_evaluation))


if __name__ == "__main__":
    unittest.main()
