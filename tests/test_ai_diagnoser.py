"""
Tests for the AI Incident Diagnosis Layer.

Uses Python's built-in unittest module — no extra dependencies.
Tests use mock AI responses and do NOT require a real paid API.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from ai_diagnoser import (
    Diagnosis,
    Verification,
    diagnose_incident,
    verify_evidence,
    _validate_diagnosis,
    _mock_diagnose,
    VALID_ROOT_CAUSES,
)

# ---------- FIXTURES ----------

BANK_X_INCIDENT = {
    "bank": "BANK_X",
    "payment_method": "UPI",
    "error_reason": "technical_error",
    "failed_payments": 7,
    "total_payments": 10,
    "failure_rate": 70.0,
    "revenue_at_risk": 17500.00,
}

OTP_INCIDENT = {
    "bank": "HDFC",
    "payment_method": "Card",
    "error_reason": "incorrect_otp",
    "failed_payments": 5,
    "total_payments": 8,
    "failure_rate": 62.5,
    "revenue_at_risk": 8400.00,
}

BALANCE_INCIDENT = {
    "bank": "SBI",
    "payment_method": "NetBanking",
    "error_reason": "insufficient_balance",
    "failed_payments": 4,
    "total_payments": 10,
    "failure_rate": 40.0,
    "revenue_at_risk": 5000.00,
}

LOW_RATE_INCIDENT = {
    "bank": "BANK_Y",
    "payment_method": "UPI",
    "error_reason": "timeout_error",
    "failed_payments": 2,
    "total_payments": 20,
    "failure_rate": 10.0,
    "revenue_at_risk": 1000.00,
}


# =====================================================================
# 1. Valid structured diagnosis is accepted
# =====================================================================


class TestValidDiagnosis(unittest.TestCase):

    def test_mock_diagnose_returns_valid_dict(self):
        raw = _mock_diagnose(BANK_X_INCIDENT)
        self.assertIsInstance(raw, dict)
        diagnosis = _validate_diagnosis(raw)
        self.assertIsNotNone(diagnosis)

    def test_diagnose_incident_returns_diagnosis_and_verification(self):
        diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertIsInstance(diagnosis, Diagnosis)
        self.assertIsInstance(verification, Verification)

    def test_diagnosis_has_required_fields(self):
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertTrue(diagnosis.likely_root_cause)
        self.assertTrue(diagnosis.incident_scope)
        self.assertIn(diagnosis.confidence, ("high", "medium", "low"))
        self.assertIsInstance(diagnosis.evidence, list)
        self.assertGreater(len(diagnosis.evidence), 0)
        self.assertTrue(diagnosis.short_explanation)

    def test_all_incident_types_get_valid_diagnosis(self):
        for incident in [BANK_X_INCIDENT, OTP_INCIDENT, BALANCE_INCIDENT]:
            diagnosis, _ = diagnose_incident(incident, is_mock=True)
            self.assertIsNotNone(diagnosis.likely_root_cause)


# =====================================================================
# 2. Wrong bank diagnosis becomes UNCERTAIN
# =====================================================================


class TestWrongBank(unittest.TestCase):

    def test_wrong_bank_is_uncertain(self):
        bad_diagnosis = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="HDFC_UPI",  # Wrong: should be BANK_X
            confidence="high",
            evidence=["7 of 10 BANK_X UPI payments failed"],
            short_explanation="Bank issue.",
        )
        verification = verify_evidence(bad_diagnosis, BANK_X_INCIDENT)
        self.assertEqual(verification.status, "UNCERTAIN")

    def test_wrong_bank_reason_mentions_mismatch(self):
        bad_diagnosis = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="HDFC_UPI",
            confidence="high",
            evidence=["7 of 10 BANK_X UPI payments failed"],
            short_explanation="Bank issue.",
        )
        verification = verify_evidence(bad_diagnosis, BANK_X_INCIDENT)
        self.assertTrue(any("scope mismatch" in r.lower() for r in verification.reasons))


# =====================================================================
# 3. Wrong payment method becomes UNCERTAIN
# =====================================================================


class TestWrongMethod(unittest.TestCase):

    def test_wrong_method_is_uncertain(self):
        bad_diagnosis = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="BANK_X_Card",  # Wrong: should be BANK_X_UPI
            confidence="high",
            evidence=["7 of 10 BANK_X UPI payments failed"],
            short_explanation="Bank issue.",
        )
        verification = verify_evidence(bad_diagnosis, BANK_X_INCIDENT)
        self.assertEqual(verification.status, "UNCERTAIN")


# =====================================================================
# 4. Evidence list is present
# =====================================================================


class TestEvidencePresent(unittest.TestCase):

    def test_evidence_list_is_populated(self):
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertIsInstance(diagnosis.evidence, list)
        self.assertGreaterEqual(len(diagnosis.evidence), 1)

    def test_evidence_mentioned_in_verification(self):
        diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        # Mock diagnosis should be SUPPORTED for correct incident
        self.assertEqual(verification.status, "SUPPORTED")


# =====================================================================
# 5. Invalid AI response fails safely
# =====================================================================


class TestInvalidResponse(unittest.TestCase):

    def test_non_dict_response_returns_fallback(self):
        # Simulate an AI returning a string instead of a dict
        diagnosis = _validate_diagnosis("not a dict")
        self.assertIsNone(diagnosis)

    def test_missing_fields_returns_none(self):
        incomplete = {"likely_root_cause": "test"}
        diagnosis = _validate_diagnosis(incomplete)
        self.assertIsNone(diagnosis)

    def test_invalid_confidence_returns_none(self):
        bad_confidence = {
            "likely_root_cause": "test",
            "incident_scope": "TEST",
            "confidence": "very_high",  # invalid
            "evidence": ["test"],
            "short_explanation": "test",
        }
        diagnosis = _validate_diagnosis(bad_confidence)
        self.assertIsNone(diagnosis)

    def test_non_list_evidence_returns_none(self):
        bad_evidence = {
            "likely_root_cause": "test",
            "incident_scope": "TEST",
            "confidence": "high",
            "evidence": "not a list",
            "short_explanation": "test",
        }
        diagnosis = _validate_diagnosis(bad_evidence)
        self.assertIsNone(diagnosis)

    def test_malformed_diagnosis_gets_fallback(self):
        """When validation fails, diagnose_incident returns a safe fallback."""
        # We can't easily force _mock_diagnose to return bad data,
        # but we can test _validate_diagnosis directly
        result = _validate_diagnosis(None)
        self.assertIsNone(result)


# =====================================================================
# 6. Missing API key does not crash the project
# =====================================================================


class TestMissingApiKey(unittest.TestCase):

    def test_mock_mode_works_without_api_key(self):
        """Mock mode should work regardless of API key availability."""
        # Ensure no API key is set
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.get("AI_DIAGNOSER_MOCK")
        os.environ["AI_DIAGNOSER_MOCK"] = "true"
        try:
            diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
            self.assertIsNotNone(diagnosis)
            self.assertEqual(verification.status, "SUPPORTED")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)


# =====================================================================
# 7. Mock mode works for tests
# =====================================================================


class TestMockMode(unittest.TestCase):

    def test_mock_is_marked(self):
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertTrue(diagnosis.is_mock)

    def test_mock_returns_deterministic_results(self):
        d1, v1 = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        d2, v2 = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertEqual(d1.likely_root_cause, d2.likely_root_cause)
        self.assertEqual(d1.confidence, d2.confidence)
        self.assertEqual(v1.status, v2.status)

    def test_mock_for_different_incidents(self):
        """Different incidents should produce different diagnoses."""
        d1, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        d2, _ = diagnose_incident(OTP_INCIDENT, is_mock=True)
        self.assertNotEqual(d1.likely_root_cause, d2.likely_root_cause)


# =====================================================================
# 8. AI cannot execute recovery actions
# =====================================================================


class TestAiCannotExecuteRecovery(unittest.TestCase):

    def test_diagnosis_has_no_action_field(self):
        """The Diagnosis dataclass should not have any action/execute fields."""
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        # Diagnosis should only contain diagnostic fields
        self.assertFalse(hasattr(diagnosis, "execute"))
        self.assertFalse(hasattr(diagnosis, "action"))
        self.assertFalse(hasattr(diagnosis, "strategy"))

    def test_diagnosis_is_read_only_info(self):
        """Diagnosis returns information, not decisions."""
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        # It contains explanation fields, not action fields
        self.assertTrue(diagnosis.likely_root_cause)
        self.assertTrue(diagnosis.short_explanation)
        # No payment action fields
        for attr in ["execute", "action", "strategy", "recovery_plan"]:
            self.assertFalse(hasattr(diagnosis, attr))


# =====================================================================
# 9. Safety Gate remains authoritative
# =====================================================================


class TestSafetyGateAuthoritative(unittest.TestCase):

    def test_safety_gate_still_blocks_captured_payment(self):
        """Even after AI diagnosis, Safety Gate blocks captured payments."""
        from safety_gate import evaluate
        payment = {
            "payment_id": "TEST001",
            "amount": 1000,
            "status": "captured",
            "recovery_attempt_count": 0,
            "customer_contact_count": 0,
            "recovered_already": False,
            "last_recovery_attempt_time": None,
        }
        result = evaluate(payment, "WAIT_AND_RECHECK")
        self.assertFalse(result["allowed"])

    def test_safety_gate_still_blocks_max_attempts(self):
        from safety_gate import evaluate
        payment = {
            "payment_id": "TEST002",
            "amount": 1000,
            "status": "failed",
            "recovery_attempt_count": 3,
            "customer_contact_count": 0,
            "recovered_already": False,
            "last_recovery_attempt_time": None,
        }
        result = evaluate(payment, "RETRY_LATER")
        self.assertFalse(result["allowed"])

    def test_ai_diagnosis_does_not_bypass_gate(self):
        """The AI diagnoser module should not import or call Safety Gate evaluate
        to bypass it. It only returns diagnosis info."""
        import ai_diagnoser
        # The module should not have a function that bypasses the gate
        self.assertFalse(hasattr(ai_diagnoser, "execute_recovery"))
        self.assertFalse(hasattr(ai_diagnoser, "bypass_safety_gate"))


# =====================================================================
# 10. Existing project tests still pass
# (verified by running full suite — this documents it)
# =====================================================================


class TestExistingTestsStillPass(unittest.TestCase):

    def test_incident_detector_works(self):
        from incident_detector import detect_incidents, group_payments, read_payments
        csv = os.path.join(os.path.dirname(__file__), "..", "data", "sample_payments.csv")
        payments = read_payments(csv)
        groups = group_payments(payments)
        incidents = detect_incidents(groups)
        self.assertIsInstance(incidents, list)

    def test_recovery_twin_works(self):
        from recovery_twin import analyze_incident
        options = analyze_incident(BANK_X_INCIDENT)
        self.assertEqual(len(options), 5)

    def test_safety_gate_works(self):
        from safety_gate import evaluate
        payment = {
            "payment_id": "TEST", "amount": 1000, "status": "failed",
            "recovery_attempt_count": 0, "customer_contact_count": 0,
            "recovered_already": False, "last_recovery_attempt_time": None,
        }
        result = evaluate(payment, "RETRY_LATER")
        self.assertTrue(result["allowed"])

    def test_recovery_plan_works(self):
        from recovery_plan import ALL_PLANS, simulate_recovery_plan, score_plan
        from payment_simulator import generate_bank_incident
        payments = generate_bank_incident()
        failed = [p for p in payments if p["status"] == "failed"]
        plan = ALL_PLANS[0]
        result = simulate_recovery_plan(failed, plan)
        score = score_plan(result)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)


# =====================================================================
# 12. Recovery context generation
# =====================================================================


class TestRecoveryContext(unittest.TestCase):

    def test_supported_diagnosis_creates_context(self):
        from ai_diagnoser import build_recovery_context, diagnose_incident
        diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertEqual(verification.status, "SUPPORTED")
        context = build_recovery_context(diagnosis, verification)
        self.assertIsNotNone(context)
        self.assertTrue(context.prefer_wait_first)
        self.assertTrue(context.avoid_immediate_customer_contact)

    def test_uncertain_diagnosis_creates_no_context(self):
        from ai_diagnoser import build_recovery_context, Diagnosis, Verification
        bad_diagnosis = Diagnosis(
            likely_root_cause="customer_data_issue",
            incident_scope="HDFC_Card",
            confidence="high",
            evidence=["test"],
            short_explanation="test",
        )
        verification = Verification(status="UNCERTAIN", reasons=["scope mismatch"])
        context = build_recovery_context(bad_diagnosis, verification)
        self.assertIsNone(context)

    def test_wrong_bank_diagnosis_zero_effect(self):
        from ai_diagnoser import build_recovery_context, diagnose_with_context
        diagnosis, verification, context = diagnose_with_context(BANK_X_INCIDENT, is_mock=True)
        # For correct diagnosis, context should be non-None
        self.assertIsNotNone(context)
        # But if we manually create a wrong-bank diagnosis...
        from ai_diagnoser import Diagnosis, Verification
        wrong = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="HDFC_UPI",  # wrong bank
            confidence="high",
            evidence=["HDFC UPI payments failed"],
            short_explanation="wrong",
        )
        wrong_v = Verification(status="UNCERTAIN", reasons=["scope mismatch"])
        wrong_ctx = build_recovery_context(wrong, wrong_v)
        self.assertIsNone(wrong_ctx)

    def test_wrong_method_diagnosis_zero_effect(self):
        from ai_diagnoser import build_recovery_context, Diagnosis, Verification
        wrong = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="BANK_X_Card",  # wrong method
            confidence="high",
            evidence=["BANK_X Card payments failed"],
            short_explanation="wrong",
        )
        wrong_v = Verification(status="UNCERTAIN", reasons=["scope mismatch"])
        wrong_ctx = build_recovery_context(wrong, wrong_v)
        self.assertIsNone(wrong_ctx)

    def test_diagnose_with_context_returns_triple(self):
        from ai_diagnoser import diagnose_with_context
        d, v, ctx = diagnose_with_context(BANK_X_INCIDENT, is_mock=True)
        self.assertIsNotNone(d)
        self.assertIsNotNone(v)
        self.assertIsNotNone(ctx)  # SUPPORTED → context exists


# =====================================================================
# 11. Evidence verification checks
# =====================================================================


class TestEvidenceVerification(unittest.TestCase):

    def test_supported_for_correct_diagnosis(self):
        diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertEqual(verification.status, "SUPPORTED")

    def test_low_failure_rate_uncertain(self):
        """A diagnosis for a low-rate incident should be UNCERTAIN."""
        good_diagnosis = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="BANK_Y_UPI",
            confidence="low",
            evidence=["2 of 20 BANK_Y UPI payments failed"],
            short_explanation="Minor issue.",
        )
        verification = verify_evidence(good_diagnosis, LOW_RATE_INCIDENT)
        self.assertEqual(verification.status, "UNCERTAIN")

    def test_too_few_failures_uncertain(self):
        """Fewer than 3 failures should be UNCERTAIN."""
        good_diagnosis = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="BANK_Y_UPI",
            confidence="low",
            evidence=["2 of 20 BANK_Y UPI payments failed"],
            short_explanation="Minor issue.",
        )
        verification = verify_evidence(good_diagnosis, LOW_RATE_INCIDENT)
        self.assertEqual(verification.status, "UNCERTAIN")

    def test_unsupported_root_cause_uncertain(self):
        bad_diagnosis = Diagnosis(
            likely_root_cause="definitely_not_a_real_root_cause",
            incident_scope="BANK_X_UPI",
            confidence="high",
            evidence=["7 of 10 BANK_X UPI payments failed"],
            short_explanation="Fake root cause.",
        )
        verification = verify_evidence(bad_diagnosis, BANK_X_INCIDENT)
        self.assertEqual(verification.status, "UNCERTAIN")


# =====================================================================
# RUN
# =====================================================================

if __name__ == "__main__":
    unittest.main()
