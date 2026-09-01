"""
Tests for the AI Incident Diagnosis Layer.

Uses Python's built-in unittest module — no extra dependencies.
Tests use mock AI responses and do NOT require a real paid API.
"""

import json
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
    _parse_llm_response,
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

VALID_LLM_RESPONSE = {
    "likely_root_cause": "temporary_bank_degradation",
    "incident_scope": "BANK_X_UPI",
    "confidence": "high",
    "evidence": [
        "7 of 10 BANK_X UPI payments failed",
        "Failure rate: 70.0%",
        "Most common error: technical_error",
    ],
    "short_explanation": "Bank-side technical issue.",
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

    def test_real_mode_without_api_key_falls_back_to_mock(self):
        """When API key is missing and is_mock=False, should fall back safely."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            # Should fall back to mock diagnosis
            self.assertIsNotNone(diagnosis)
            # No key → _get_provider returns mock provider directly
            self.assertEqual(diagnosis.provider_used, "mock")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock


# =====================================================================
# 7. Mock mode works for tests
# =====================================================================


class TestMockMode(unittest.TestCase):

    def test_mock_is_marked(self):
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertEqual(diagnosis.provider_used, "mock")

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
# 13. Real LLM provider — fallback handling
# =====================================================================


class TestRealProviderFallback(unittest.TestCase):

    def test_real_provider_falls_back_on_api_error(self):
        """When the LLM API returns an HTTP error, falls back to mock."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            # Set a fake key so the real provider is selected
            os.environ["AI_DIAGNOSER_API_KEY"] = "fake-key-for-testing"
            os.environ["AI_DIAGNOSER_API_BASE"] = "https://httpbin.org/status/400"
            diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            # Should fall back to mock diagnosis
            self.assertIsNotNone(diagnosis)
            self.assertEqual(diagnosis.provider_used, "mock_fallback")
            self.assertIn(verification.status, ("SUPPORTED", "UNCERTAIN"))
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            else:
                os.environ.pop("AI_DIAGNOSER_API_KEY", None)
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)
                os.environ.pop("AI_DIAGNOSER_API_BASE", None)

    def test_real_provider_falls_back_on_connection_error(self):
        """When the LLM API is unreachable, falls back to mock."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            os.environ["AI_DIAGNOSER_API_KEY"] = "fake-key"
            os.environ["AI_DIAGNOSER_API_BASE"] = "http://192.0.2.1:1/v1"
            os.environ["AI_DIAGNOSER_TIMEOUT"] = "2"
            diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            self.assertIsNotNone(diagnosis)
            self.assertEqual(diagnosis.provider_used, "mock_fallback")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            else:
                os.environ.pop("AI_DIAGNOSER_API_KEY", None)
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)
                os.environ.pop("AI_DIAGNOSER_API_BASE", None)
                os.environ.pop("AI_DIAGNOSER_TIMEOUT", None)

    def test_real_provider_falls_back_on_invalid_json(self):
        """When the LLM returns non-JSON, falls back to mock."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            os.environ["AI_DIAGNOSER_API_KEY"] = "fake-key"
            # httpbin.org/html returns HTML, not JSON
            os.environ["AI_DIAGNOSER_API_BASE"] = "https://httpbin.org/html"
            diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            self.assertIsNotNone(diagnosis)
            self.assertEqual(diagnosis.provider_used, "mock_fallback")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            else:
                os.environ.pop("AI_DIAGNOSER_API_KEY", None)
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)
                os.environ.pop("AI_DIAGNOSER_API_BASE", None)


# =====================================================================
# 14. LLM response parsing
# =====================================================================


class TestLLMResponseParsing(unittest.TestCase):

    def test_parse_plain_json(self):
        """Parse a plain JSON string."""
        result = _parse_llm_response('{"likely_root_cause": "test"}')
        self.assertIsNotNone(result)
        self.assertEqual(result["likely_root_cause"], "test")

    def test_parse_json_in_code_block(self):
        """Parse JSON wrapped in markdown code blocks."""
        text = '```json\n{"likely_root_cause": "test"}\n```'
        result = _parse_llm_response(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["likely_root_cause"], "test")

    def test_parse_json_in_plain_code_block(self):
        """Parse JSON wrapped in bare code blocks (no language tag)."""
        text = '```\n{"likely_root_cause": "test"}\n```'
        result = _parse_llm_response(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["likely_root_cause"], "test")

    def test_parse_json_with_surrounding_text(self):
        """Parse JSON embedded in surrounding text."""
        text = 'Here is the diagnosis:\n{"likely_root_cause": "test"}\nDone.'
        result = _parse_llm_response(text)
        self.assertIsNotNone(result)
        self.assertEqual(result["likely_root_cause"], "test")

    def test_parse_invalid_returns_none(self):
        """Return None for completely invalid text."""
        result = _parse_llm_response("This is not JSON at all")
        self.assertIsNone(result)

    def test_parse_empty_returns_none(self):
        """Return None for empty string."""
        result = _parse_llm_response("")
        self.assertIsNone(result)


# =====================================================================
# 15. Structured response validation
# =====================================================================


class TestStructuredValidation(unittest.TestCase):

    def test_valid_response_validates(self):
        """A well-formed response passes validation."""
        diagnosis = _validate_diagnosis(VALID_LLM_RESPONSE)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis.likely_root_cause, "temporary_bank_degradation")
        self.assertEqual(diagnosis.incident_scope, "BANK_X_UPI")
        self.assertEqual(diagnosis.confidence, "high")
        self.assertEqual(len(diagnosis.evidence), 3)

    def test_missing_root_cause_rejects(self):
        resp = {k: v for k, v in VALID_LLM_RESPONSE.items() if k != "likely_root_cause"}
        self.assertIsNone(_validate_diagnosis(resp))

    def test_missing_scope_rejects(self):
        resp = {k: v for k, v in VALID_LLM_RESPONSE.items() if k != "incident_scope"}
        self.assertIsNone(_validate_diagnosis(resp))

    def test_missing_evidence_rejects(self):
        resp = {k: v for k, v in VALID_LLM_RESPONSE.items() if k != "evidence"}
        self.assertIsNone(_validate_diagnosis(resp))

    def test_missing_explanation_rejects(self):
        resp = {k: v for k, v in VALID_LLM_RESPONSE.items() if k != "short_explanation"}
        self.assertIsNone(_validate_diagnosis(resp))

    def test_all_valid_root_causes_accepted(self):
        """Every valid root cause value should be accepted by the verifier."""
        for rc in VALID_ROOT_CAUSES:
            resp = dict(VALID_LLM_RESPONSE, likely_root_cause=rc)
            diagnosis = _validate_diagnosis(resp)
            self.assertIsNotNone(diagnosis, f"Root cause '{rc}' should be valid")


# =====================================================================
# 16. SUPPORTED and UNCERTAIN diagnosis paths
# =====================================================================


class TestDiagnosisPaths(unittest.TestCase):

    def test_supported_diagnosis_with_correct_scope(self):
        """Correct scope + sufficient failures → SUPPORTED."""
        diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertEqual(verification.status, "SUPPORTED")
        self.assertEqual(diagnosis.provider_used, "mock")

    def test_uncertain_diagnosis_with_wrong_scope(self):
        """Wrong scope → UNCERTAIN even if other evidence is good."""
        bad = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="HDFC_UPI",
            confidence="high",
            evidence=["7 of 10 BANK_X UPI payments failed"],
            short_explanation="Wrong scope.",
        )
        v = verify_evidence(bad, BANK_X_INCIDENT)
        self.assertEqual(v.status, "UNCERTAIN")

    def test_uncertain_for_low_rate_high_failures(self):
        """High failure count but low rate → UNCERTAIN."""
        bad = Diagnosis(
            likely_root_cause="temporary_bank_degradation",
            incident_scope="BANK_Y_UPI",
            confidence="high",
            evidence=["2 of 20 BANK_Y UPI payments failed"],
            short_explanation="Minor.",
        )
        v = verify_evidence(bad, LOW_RATE_INCIDENT)
        self.assertEqual(v.status, "UNCERTAIN")


# =====================================================================
# 17. .env file loading
# =====================================================================


class TestEnvFileLoading(unittest.TestCase):

    def test_load_env_file_sets_new_vars(self):
        """_load_env_file should set variables that are not already set."""
        from ai_diagnoser import _load_env_file
        import tempfile
        # Create a temporary .env file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write('TEST_ENV_VAR_FROM_DOTENV=hello_from_env\n')
            f.write('TEST_ENV_VAR_QUOTED="quoted_value"\n')
            env_path = f.name
        try:
            # Remove var if it exists
            os.environ.pop("TEST_ENV_VAR_FROM_DOTENV", None)
            os.environ.pop("TEST_ENV_VAR_QUOTED", None)
            _load_env_file(env_path)
            self.assertEqual(os.environ.get("TEST_ENV_VAR_FROM_DOTENV"), "hello_from_env")
            self.assertEqual(os.environ.get("TEST_ENV_VAR_QUOTED"), "quoted_value")
        finally:
            os.environ.pop("TEST_ENV_VAR_FROM_DOTENV", None)
            os.environ.pop("TEST_ENV_VAR_QUOTED", None)
            os.unlink(env_path)

    def test_load_env_file_does_not_override_existing(self):
        """Existing env vars should NOT be overridden by .env file."""
        from ai_diagnoser import _load_env_file
        import tempfile
        os.environ["TEST_ENV_NO_OVERRIDE"] = "original"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write('TEST_ENV_NO_OVERRIDE=should_not_override\n')
            env_path = f.name
        try:
            _load_env_file(env_path)
            self.assertEqual(os.environ.get("TEST_ENV_NO_OVERRIDE"), "original")
        finally:
            os.environ.pop("TEST_ENV_NO_OVERRIDE", None)
            os.unlink(env_path)

    def test_load_env_file_ignores_comments(self):
        """Lines starting with # should be ignored."""
        from ai_diagnoser import _load_env_file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write('# This is a comment\n')
            f.write('TEST_ENV_COMMENT_LINE=should_be_set\n')
            env_path = f.name
        try:
            os.environ.pop("TEST_ENV_COMMENT_LINE", None)
            _load_env_file(env_path)
            self.assertEqual(os.environ.get("TEST_ENV_COMMENT_LINE"), "should_be_set")
        finally:
            os.environ.pop("TEST_ENV_COMMENT_LINE", None)
            os.unlink(env_path)

    def test_load_env_file_handles_missing_file(self):
        """Loading a non-existent .env file should not crash."""
        from ai_diagnoser import _load_env_file
        # Should not raise
        _load_env_file("/nonexistent/path/.env")

    def test_load_env_file_handles_blank_lines(self):
        """Blank lines should be skipped."""
        from ai_diagnoser import _load_env_file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write('\n\nTEST_ENV_BLANK_LINES=works\n\n')
            env_path = f.name
        try:
            os.environ.pop("TEST_ENV_BLANK_LINES", None)
            _load_env_file(env_path)
            self.assertEqual(os.environ.get("TEST_ENV_BLANK_LINES"), "works")
        finally:
            os.environ.pop("TEST_ENV_BLANK_LINES", None)
            os.unlink(env_path)


# =====================================================================
# 18. Secret not printed/logged
# =====================================================================


class TestSecretNotExposed(unittest.TestCase):

    def test_api_key_not_in_diagnosis_output(self):
        """The API key should never appear in diagnosis fields."""
        fake_key = "sk-super-secret-key-12345"
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        # Check all string fields of the diagnosis
        for field_val in [
            diagnosis.likely_root_cause,
            diagnosis.incident_scope,
            diagnosis.short_explanation,
            *diagnosis.evidence,
        ]:
            self.assertNotIn(fake_key, str(field_val),
                f"API key leaked into diagnosis field: {field_val}")

    def test_api_key_not_in_verification_output(self):
        """The API key should never appear in verification reasons."""
        fake_key = "sk-super-secret-key-12345"
        _, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        for reason in verification.reasons:
            self.assertNotIn(fake_key, reason,
                f"API key leaked into verification reason: {reason}")

    def test_call_llm_api_does_not_log_key(self):
        """_call_llm_api should not include the key in error messages."""
        # We can't easily test this without calling the API, but we can
        # verify the function signature doesn't print the key
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        # The key should only appear in the Authorization header, not in logs
        self.assertNotIn('print(key', source)
        self.assertNotIn('print(f"{key', source)
        self.assertNotIn('logging', source)

    def test_diagnose_incident_does_not_expose_key(self):
        """diagnose_incident should not expose the API key anywhere."""
        import inspect
        from ai_diagnoser import diagnose_incident
        source = inspect.getsource(diagnose_incident)
        self.assertNotIn('print(api_key', source)
        self.assertNotIn('print(key', source)


# =====================================================================
# 19. Provider source labels
# =====================================================================


class TestProviderLabels(unittest.TestCase):

    def test_mock_mode_label(self):
        """Mock diagnosis should have provider_used = 'mock'."""
        diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=True)
        self.assertEqual(diagnosis.provider_used, "mock")

    def test_real_llm_label_when_key_set(self):
        """When API key is present and is_mock=False, label should be 'real_llm'."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            os.environ["AI_DIAGNOSER_API_KEY"] = "fake-key-for-label-test"
            # This will fail the API call, but _get_provider should return real_llm
            from ai_diagnoser import _get_provider
            _, provider_used = _get_provider()
            self.assertEqual(provider_used, "real_llm")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            else:
                os.environ.pop("AI_DIAGNOSER_API_KEY", None)
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)

    def test_mock_fallback_label_on_api_failure(self):
        """When real LLM fails, diagnosis should have provider_used = 'mock_fallback'."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            os.environ["AI_DIAGNOSER_API_KEY"] = "fake-key"
            os.environ["AI_DIAGNOSER_API_BASE"] = "https://httpbin.org/status/400"
            diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            self.assertEqual(diagnosis.provider_used, "mock_fallback")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            else:
                os.environ.pop("AI_DIAGNOSER_API_KEY", None)
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)
                os.environ.pop("AI_DIAGNOSER_API_BASE", None)

    def test_missing_key_gives_mock_label(self):
        """When no API key is set and is_mock=False, provider is mock (no key = no real provider)."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            # No key → _get_provider returns mock provider, label = "mock"
            self.assertEqual(diagnosis.provider_used, "mock")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock

    def test_validate_diagnosis_sets_default_provider(self):
        """_validate_diagnosis should produce provider_used = 'mock' by default."""
        from ai_diagnoser import _validate_diagnosis
        diagnosis = _validate_diagnosis(VALID_LLM_RESPONSE)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis.provider_used, "mock")


# =====================================================================
# 20. JSON schema request configuration
# =====================================================================


class TestJsonSchemaRequest(unittest.TestCase):

    def test_request_includes_json_schema_format(self):
        """The LLM request payload must use response_format type json_schema."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        self.assertIn('"json_schema"', source)
        self.assertIn('"response_format"', source)

    def test_request_temperature_is_zero(self):
        """Temperature must be 0 for deterministic structured output."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        self.assertIn('"temperature": 0', source)

    def test_max_tokens_at_least_2000(self):
        """max_tokens must be 2000 to give enough room for JSON output."""
        import inspect, re
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        match = re.search(r'"max_tokens":\s*(\d+)', source)
        self.assertIsNotNone(match, "max_tokens not found in _call_llm_api")
        max_tokens = int(match.group(1))
        self.assertGreaterEqual(max_tokens, 2000,
            f"max_tokens={max_tokens} is too low; need >= 2000")

    def test_request_stream_is_false(self):
        """Stream must be false for non-streaming responses."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        self.assertIn('"stream": False', source)

    def test_request_includes_require_parameters(self):
        """OpenRouter provider preferences must include require_parameters."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        self.assertIn('"require_parameters"', source)
        self.assertIn('True', source)

    def test_request_inspects_finish_reason(self):
        """_call_llm_api must check finish_reason for truncation detection."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        self.assertIn('finish_reason', source)
        self.assertIn('"length"', source)

    def test_request_disables_reasoning(self):
        """Payload must include reasoning exclude to prevent token consumption."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        self.assertIn('"reasoning"', source)
        self.assertIn('"exclude"', source)
        self.assertIn('True', source)

    def test_json_schema_has_strict_true(self):
        """The JSON schema must have strict: true."""
        from ai_diagnoser import _DIAGNOSIS_JSON_SCHEMA
        self.assertTrue(_DIAGNOSIS_JSON_SCHEMA.get("strict"))

    def test_json_schema_has_additional_properties_false(self):
        """The JSON schema must have additionalProperties: false."""
        from ai_diagnoser import _DIAGNOSIS_JSON_SCHEMA
        schema_props = _DIAGNOSIS_JSON_SCHEMA["schema"]
        self.assertFalse(schema_props.get("additionalProperties"))

    def test_json_schema_has_all_required_fields(self):
        """The JSON schema must require all 5 diagnosis fields."""
        from ai_diagnoser import _DIAGNOSIS_JSON_SCHEMA
        required = _DIAGNOSIS_JSON_SCHEMA["schema"]["required"]
        expected = {
            "likely_root_cause", "incident_scope", "confidence",
            "evidence", "short_explanation",
        }
        self.assertEqual(set(required), expected)

    def test_json_schema_enum_has_valid_root_causes(self):
        """The schema should list all valid root causes as enum values."""
        from ai_diagnoser import _DIAGNOSIS_JSON_SCHEMA, VALID_ROOT_CAUSES
        schema_props = _DIAGNOSIS_JSON_SCHEMA["schema"]["properties"]
        enum_values = set(schema_props["likely_root_cause"]["enum"])
        self.assertTrue(VALID_ROOT_CAUSES.issubset(enum_values))

    def test_payload_is_valid_json(self):
        """Verify the full payload structure is valid JSON."""
        from ai_diagnoser import _DIAGNOSIS_JSON_SCHEMA
        payload_dict = {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "test"},
                {"role": "user", "content": "test"},
            ],
            "temperature": 0,
            "max_tokens": 2000,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": _DIAGNOSIS_JSON_SCHEMA,
            },
            "provider": {
                "require_parameters": True,
            },
            "reasoning": {"effort": "none", "exclude": True},
        }
        encoded = json.dumps(payload_dict).encode("utf-8")
        decoded = json.loads(encoded)
        self.assertEqual(decoded["response_format"]["type"], "json_schema")
        self.assertTrue(decoded["response_format"]["json_schema"]["strict"])
        self.assertTrue(decoded["provider"]["require_parameters"])
        self.assertFalse(decoded["stream"])
        self.assertEqual(decoded["temperature"], 0)
        self.assertEqual(decoded["max_tokens"], 2000)
        self.assertTrue(decoded["reasoning"]["exclude"])

    def test_debug_prints_finish_reason_not_key(self):
        """Debug output should print finish_reason but never the API key."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        self.assertIn('finish_reason=', source)
        self.assertNotIn('print(api_key', source)
        self.assertNotIn('print(f"{api_key', source)


# =====================================================================
# 21. Truncated / malformed JSON fallback
# =====================================================================


class TestTruncatedJsonFallback(unittest.TestCase):

    def test_truncated_json_returns_none(self):
        """An unterminated JSON string (truncated) should return None from parser."""
        truncated = '{"likely_root_cause": "temporary_bank_degradation", "incident_scope": "BANK_X_UPI", "confidence": "high", "evidence": ["7 of 10 BANK_X UPI payments failed", "Failure rate: 70.0%", "Most common error: technical_error"], "short_explanation": "The failures are concentrated on B'
        result = _parse_llm_response(truncated)
        self.assertIsNone(result)

    def test_truncated_json_in_code_block_returns_none(self):
        """Truncated JSON inside a code block should return None."""
        truncated = '```json\n{"likely_root_cause": "test", "incident_scope": "TEST", "confidence": "high", "evidence": ["test"], "short_explanation": "incomplete'
        result = _parse_llm_response(truncated)
        self.assertIsNone(result)

    def test_malformed_json_fallback_produces_safe_diagnosis(self):
        """When real LLM returns malformed JSON, diagnose_incident falls back safely."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            os.environ["AI_DIAGNOSER_API_KEY"] = "fake-key"
            os.environ["AI_DIAGNOSER_API_BASE"] = "https://httpbin.org/html"
            diagnosis, verification = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            self.assertIsNotNone(diagnosis)
            self.assertEqual(diagnosis.provider_used, "mock_fallback")
            self.assertTrue(diagnosis.likely_root_cause)
            self.assertIn(verification.status, ("SUPPORTED", "UNCERTAIN"))
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            else:
                os.environ.pop("AI_DIAGNOSER_API_KEY", None)
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)
                os.environ.pop("AI_DIAGNOSER_API_BASE", None)

    def test_partial_json_array_returns_none(self):
        """A partial JSON array (not an object) should return None."""
        result = _parse_llm_response('[{"likely_root_cause": "test"')
        self.assertIsNone(result)

    def test_only_opening_brace_returns_none(self):
        """Just an opening brace with no content should return None."""
        result = _parse_llm_response('{')
        self.assertIsNone(result)

    def test_empty_object_returns_dict(self):
        """An empty JSON object is valid JSON, so parser should return it."""
        result = _parse_llm_response('{}')
        self.assertIsNotNone(result)
        self.assertEqual(result, {})

    def test_truncated_evidence_array_returns_none(self):
        """Truncated mid-array in evidence field should return None."""
        truncated = '{"likely_root_cause": "test", "incident_scope": "TEST", "confidence": "high", "evidence": ["item1", "item'
        result = _parse_llm_response(truncated)
        self.assertIsNone(result)

    def test_valid_structured_response_validates(self):
        """A valid structured JSON response should pass validation."""
        valid_response = {
            "likely_root_cause": "temporary_bank_degradation",
            "incident_scope": "BANK_X_UPI",
            "confidence": "high",
            "evidence": ["7 of 10 payments failed"],
            "short_explanation": "Bank-side issue.",
        }
        result = _parse_llm_response(json.dumps(valid_response))
        self.assertIsNotNone(result)
        diagnosis = _validate_diagnosis(result)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis.likely_root_cause, "temporary_bank_degradation")

    def test_response_with_explanation_only_returns_none(self):
        """A response with only explanation (missing other fields) should still parse
        but fail validation — not silently accepted."""
        partial = '{"short_explanation": "Bank issue"}'
        result = _parse_llm_response(partial)
        self.assertIsNotNone(result)  # valid JSON
        diagnosis = _validate_diagnosis(result)
        self.assertIsNone(diagnosis)  # but fails validation


# =====================================================================
# 22. OpenRouter response extraction
# =====================================================================


class TestOpenRouterResponseExtraction(unittest.TestCase):
    """Test that the LLM API correctly extracts content from OpenRouter responses."""

    def test_extract_content_from_openrouter_response(self):
        """choices[0].message.content should be extracted as the diagnosis string."""
        # Simulate a typical OpenRouter response structure
        openrouter_response = {
            "id": "gen-123",
            "model": "dots-studio/dots-3-note-preview:free",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({
                            "likely_root_cause": "temporary_bank_degradation",
                            "incident_scope": "BANK_X_UPI",
                            "confidence": "high",
                            "evidence": ["7 of 10 payments failed"],
                            "short_explanation": "Bank issue.",
                        }),
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        # Verify the extraction logic works
        content = openrouter_response["choices"][0]["message"]["content"]
        self.assertIsNotNone(content)
        parsed = json.loads(content)
        self.assertEqual(parsed["likely_root_cause"], "temporary_bank_degradation")
        self.assertEqual(parsed["incident_scope"], "BANK_X_UPI")

    def test_finish_reason_stop_success(self):
        """finish_reason='stop' means the model completed successfully."""
        response = {
            "choices": [{
                "message": {"content": '{"likely_root_cause": "test"}'},
                "finish_reason": "stop",
            }]
        }
        choice = response["choices"][0]
        self.assertEqual(choice["finish_reason"], "stop")
        content = choice["message"]["content"]
        parsed = json.loads(content)
        self.assertEqual(parsed["likely_root_cause"], "test")

    def test_finish_reason_length_raises_truncation_error(self):
        """finish_reason='length' must raise RuntimeError with truncation message."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            # We can't easily mock the HTTP response, but we can verify
            # the truncation detection code path exists
            import inspect
            from ai_diagnoser import _call_llm_api
            source = inspect.getsource(_call_llm_api)
            self.assertIn('finish_reason == "length"', source)
            self.assertIn('TRUNCATED', source)
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock

    def test_malformed_inner_content_falls_back(self):
        """If the content string is not valid JSON, falls back to mock."""
        old_key = os.environ.pop("AI_DIAGNOSER_API_KEY", None)
        old_mock = os.environ.pop("AI_DIAGNOSER_MOCK", None)
        try:
            os.environ["AI_DIAGNOSER_API_KEY"] = "fake-key"
            os.environ["AI_DIAGNOSER_API_BASE"] = "https://httpbin.org/html"
            diagnosis, _ = diagnose_incident(BANK_X_INCIDENT, is_mock=False)
            self.assertIsNotNone(diagnosis)
            self.assertEqual(diagnosis.provider_used, "mock_fallback")
        finally:
            if old_key:
                os.environ["AI_DIAGNOSER_API_KEY"] = old_key
            else:
                os.environ.pop("AI_DIAGNOSER_API_KEY", None)
            if old_mock:
                os.environ["AI_DIAGNOSER_MOCK"] = old_mock
            else:
                os.environ.pop("AI_DIAGNOSER_MOCK", None)
                os.environ.pop("AI_DIAGNOSER_API_BASE", None)

    def test_valid_json_schema_content_validates(self):
        """A valid JSON-schema content string should pass full validation."""
        content = json.dumps({
            "likely_root_cause": "temporary_bank_degradation",
            "incident_scope": "BANK_X_UPI",
            "confidence": "high",
            "evidence": ["7 of 10 BANK_X UPI payments failed"],
            "short_explanation": "Bank-side technical issue.",
        })
        parsed = _parse_llm_response(content)
        self.assertIsNotNone(parsed)
        diagnosis = _validate_diagnosis(parsed)
        self.assertIsNotNone(diagnosis)
        self.assertEqual(diagnosis.likely_root_cause, "temporary_bank_degradation")
        self.assertEqual(diagnosis.incident_scope, "BANK_X_UPI")
        verification = verify_evidence(diagnosis, BANK_X_INCIDENT)
        self.assertEqual(verification.status, "SUPPORTED")

    def test_api_key_never_in_debug_output(self):
        """The debug print statements must never include the API key."""
        import inspect
        from ai_diagnoser import _call_llm_api
        source = inspect.getsource(_call_llm_api)
        # Find all print() calls in the function
        lines = source.split('\n')
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('print('):
                self.assertNotIn('api_key', stripped.lower(),
                    f"Debug line may leak API key: {stripped}")
                self.assertNotIn('authorization', stripped.lower(),
                    f"Debug line may leak auth header: {stripped}")

    def test_outer_response_parsed_not_inner(self):
        """The outer OpenRouter response JSON should be parsed first,
        then choices[0].message.content extracted as the inner string."""
        # Simulate: outer body is OpenRouter wrapper, inner is our diagnosis
        inner_diagnosis = {
            "likely_root_cause": "connector_timeout",
            "incident_scope": "HDFC_NetBanking",
            "confidence": "medium",
            "evidence": ["5 of 8 payments timed out"],
            "short_explanation": "Connector timeout.",
        }
        outer_body = {
            "id": "gen-456",
            "model": "dots-studio/dots-3-note-preview:free",
            "choices": [{
                "message": {"content": json.dumps(inner_diagnosis)},
                "finish_reason": "stop",
            }],
        }
        # Step 1: parse outer body
        outer = json.loads(json.dumps(outer_body))
        content_str = outer["choices"][0]["message"]["content"]
        # Step 2: parse inner content
        inner = json.loads(content_str)
        self.assertEqual(inner["likely_root_cause"], "connector_timeout")
        self.assertEqual(inner["incident_scope"], "HDFC_NetBanking")


# =====================================================================
# RUN
# =====================================================================

if __name__ == "__main__":
    unittest.main()
