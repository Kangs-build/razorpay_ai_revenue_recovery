"""
AI Incident Diagnosis Layer — Razorpay AI Revenue Recovery (Track 1)

Uses an LLM (or deterministic mock) to help understand and explain the
likely root cause of a payment incident. The AI only diagnoses and explains
— it does NOT execute payment actions or bypass the Safety Gate.

Flow:
    Incident Detector → AI Diagnosis → Evidence Verification → Recovery Twin

Providers:
    - Mock: deterministic, no API key needed, used in tests
    - Real: OpenAI-compatible API, requires AI_DIAGNOSER_API_KEY env var
"""

from __future__ import annotations

import os
import sys
import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(__file__))

# ---------- .env LOADING ----------


def _load_env_file(path: str | None = None) -> None:
    """Load a .env file into os.environ without overriding existing values.

    This is a minimal parser for simple KEY=VALUE lines.
    Lines starting with # or blank lines are ignored.
    Values may be optionally quoted with single or double quotes.
    Only sets variables that are NOT already in the environment,
    so explicit shell exports always win.

    Args:
        path: Path to .env file. Defaults to .env in the project root
              (two directories up from this file, i.e. the repository root).
    """
    if path is None:
        # Project root = backend/../ = one level up from where this file lives
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
                # Remove surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                # Only set if not already in environment (explicit exports win)
                if key and key not in os.environ:
                    os.environ[key] = value
    except (OSError, UnicodeDecodeError):
        # If .env can't be read, silently continue — env vars may come from shell
        pass


# Load .env at import time so AI_DIAGNOSER_* vars are available
_load_env_file()


# ---------- DATA MODEL ----------


@dataclass
class Diagnosis:
    """Structured AI diagnosis output."""

    likely_root_cause: str
    incident_scope: str
    confidence: str  # "high" / "medium" / "low"
    evidence: list[str]
    short_explanation: str
    provider_used: str = "mock"  # "mock" / "real_llm" / "mock_fallback"


@dataclass
class Verification:
    """Result of verifying an AI diagnosis against actual incident data."""

    status: str  # "SUPPORTED" or "UNCERTAIN"
    reasons: list[str]


# ---------- VALID ROOT CAUSES ----------

VALID_ROOT_CAUSES = {
    "temporary_bank_degradation",
    "payment_method_outage",
    "merchant_configuration_error",
    "customer_data_issue",
    "connector_timeout",
    "rate_limiting",
    "fraud_detection_trigger",
    "insufficient_funds_cluster",
    "authentication_system_failure",
    "unknown",
}

# ---------- MOCK AI PROVIDER ----------


def _mock_diagnose(incident: dict) -> dict:
    """Deterministic mock diagnosis based on incident data.

    This provides a realistic AI-like response without calling any API.
    Used for testing and offline demos.
    """
    bank = incident.get("bank", "unknown")
    method = incident.get("payment_method", "unknown")
    error = incident.get("error_reason", "unknown")
    failed = incident.get("failed_payments", 0)
    total = incident.get("total_payments", 0)
    rate = incident.get("failure_rate", 0)

    # Determine root cause based on error pattern
    if error == "technical_error":
        root_cause = "temporary_bank_degradation"
        explanation = (
            f"The failures are concentrated on {bank} {method} payments within "
            f"a short period, suggesting a temporary bank-side technical issue."
        )
    elif error == "timeout_error":
        root_cause = "connector_timeout"
        explanation = (
            f"Multiple {bank} {method} payments are timing out, indicating "
            f"possible connector or network degradation."
        )
    elif error == "incorrect_otp":
        root_cause = "authentication_system_failure"
        explanation = (
            f"Multiple OTP failures on {bank} {method} suggest the "
            f"authentication system may be experiencing issues."
        )
    elif error == "insufficient_balance":
        root_cause = "insufficient_funds_cluster"
        explanation = (
            f"A cluster of insufficient balance errors on {bank} {method} "
            f"may indicate a data quality issue or unusual customer segment."
        )
    else:
        root_cause = "unknown"
        explanation = (
            f"Insufficient information to determine root cause for "
            f"{bank} {method} {error} failures."
        )

    # Confidence based on failure rate
    if rate >= 70:
        confidence = "high"
    elif rate >= 50:
        confidence = "medium"
    else:
        confidence = "low"

    # Build evidence list
    evidence = [
        f"{failed} of {total} {bank} {method} payments failed",
        f"Failure rate: {rate}%",
        f"Most common error: {error}",
    ]

    return {
        "likely_root_cause": root_cause,
        "incident_scope": f"{bank}_{method}",
        "confidence": confidence,
        "evidence": evidence,
        "short_explanation": explanation,
    }


# ---------- REAL LLM PROVIDER ----------

# Default API configuration
_DEFAULT_API_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_TIMEOUT = 30  # seconds
_MAX_RESPONSE_CHARS = 8000

# ---------- JSON SCHEMA FOR STRUCTURED OUTPUT ----------

_DIAGNOSIS_JSON_SCHEMA = {
    "name": "payment_diagnosis",
    "strict": True,
    "description": "Structured diagnosis of a payment incident",
    "schema": {
        "type": "object",
        "properties": {
            "likely_root_cause": {
                "type": "string",
                "description": "The likely root cause of the incident",
                "enum": [
                    "temporary_bank_degradation",
                    "payment_method_outage",
                    "merchant_configuration_error",
                    "customer_data_issue",
                    "connector_timeout",
                    "rate_limiting",
                    "fraud_detection_trigger",
                    "insufficient_funds_cluster",
                    "authentication_system_failure",
                    "unknown",
                ],
            },
            "incident_scope": {
                "type": "string",
                "description": "Scope of the incident in BANK_METHOD format, e.g. BANK_X_UPI",
            },
            "confidence": {
                "type": "string",
                "description": "Confidence level: high, medium, or low",
                "enum": ["high", "medium", "low"],
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of evidence points supporting the diagnosis",
            },
            "short_explanation": {
                "type": "string",
                "description": "1-2 sentence explanation of the diagnosis",
            },
        },
        "required": [
            "likely_root_cause",
            "incident_scope",
            "confidence",
            "evidence",
            "short_explanation",
        ],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = """You are a payment incident analyst for a fintech company.
Given summarized payment incident data, diagnose the likely root cause.

You MUST return ONLY a valid JSON object with these exact fields:
{
  "likely_root_cause": "<one of: temporary_bank_degradation, payment_method_outage, merchant_configuration_error, customer_data_issue, connector_timeout, rate_limiting, fraud_detection_trigger, insufficient_funds_cluster, authentication_system_failure, unknown>",
  "incident_scope": "<BANK_METHOD format, e.g. BANK_X_UPI>",
  "confidence": "<high, medium, or low>",
  "evidence": ["<evidence point 1>", "<evidence point 2>", ...],
  "short_explanation": "<1-2 sentence explanation>"
}

Rules:
- evidence must be a list of strings supported by the incident data
- confidence: high if failure_rate >= 70%, medium if >= 50%, low otherwise
- incident_scope must match the affected bank and method
- Do NOT include any text outside the JSON object
- Do NOT execute any payment actions
"""


def _build_user_message(incident: dict) -> str:
    """Build a concise user message from incident data for the LLM."""
    bank = incident.get("bank", "unknown")
    method = incident.get("payment_method", "unknown")
    error = incident.get("error_reason", "unknown")
    failed = incident.get("failed_payments", 0)
    total = incident.get("total_payments", 0)
    rate = incident.get("failure_rate", 0)
    revenue = incident.get("revenue_at_risk", 0)

    # Include time-window if available
    time_info = ""
    if "time_window_minutes" in incident:
        time_info = f"\nTime window: {incident['time_window_minutes']} minutes"
    elif "start_time" in incident and "end_time" in incident:
        time_info = (
            f"\nTime window: {incident.get('start_time', '?')} to "
            f"{incident.get('end_time', '?')}"
        )

    return (
        f"Payment incident data:\n"
        f"- Affected bank: {bank}\n"
        f"- Payment method: {method}\n"
        f"- Failure rate: {rate}%\n"
        f"- Failed payments: {failed} of {total}\n"
        f"- Dominant error: {error}\n"
        f"- Revenue at risk: {revenue}{time_info}\n"
        f"\nDiagnose the likely root cause and return structured JSON."
    )


def _call_llm_api(
    api_key: str,
    api_base: str,
    model: str,
    user_message: str,
    timeout: int,
) -> str:
    """Make a single LLM API call using OpenAI-compatible chat completions.

    Uses only stdlib urllib — no external HTTP library required.

    Returns:
        The assistant's response text.

    Raises:
        RuntimeError: On API errors, timeouts, or invalid responses.
    """
    url = f"{api_base.rstrip('/')}/chat/completions"

    payload_dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
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
    }

    # Try to disable reasoning/thinking to save output tokens for the
    # actual JSON diagnosis.  Not all providers support this — if the
    # provider rejects it the caller will fall back to mock anyway.
    payload_dict["reasoning"] = {"effort": "none", "exclude": True}

    payload = json.dumps(payload_dict).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")[:_MAX_RESPONSE_CHARS]
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise RuntimeError(
            f"LLM API returned HTTP {e.code}: {error_body}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"LLM API connection failed: {e.reason}"
        ) from e
    except TimeoutError:
        raise RuntimeError(
            f"LLM API call timed out after {timeout}s"
        )

    # Parse the outer OpenRouter/OpenAI response body
    try:
        response_json = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM API returned invalid JSON: {e}") from e

    # Debug info (never prints API key)
    returned_model = response_json.get("model", "unknown")
    choices = response_json.get("choices", [])
    if choices:
        _finish = choices[0].get("finish_reason", "unknown")
        _content_len = len(choices[0].get("message", {}).get("content", ""))
        print(f"  ℹ️  OpenRouter response: model={returned_model}  "
              f"finish_reason={_finish}  content_length={_content_len}")
    else:
        print(f"  ℹ️  OpenRouter response: model={returned_model}  (no choices)")

    # Extract assistant message content from choices[0].message.content
    if not choices:
        raise RuntimeError("LLM API returned no choices")

    choice = choices[0]
    finish_reason = choice.get("finish_reason", "")

    # Check for truncation
    if finish_reason == "length":
        raise RuntimeError(
            "REAL LLM RESPONSE TRUNCATED — USING MOCK FALLBACK  "
            f"(model={returned_model}, finish_reason=length)"
        )

    content = choice.get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("LLM API returned empty message content")

    return content.strip()


def _parse_llm_response(response_text: str) -> dict | None:
    """Parse the LLM response text into a structured dict.

    Handles cases where the LLM wraps JSON in markdown code blocks
    or adds extra text around the JSON.

    Returns:
        Parsed dict or None if parsing fails.
    """
    text = response_text.strip()

    # Try to extract JSON from markdown code blocks
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            cleaned = part.strip()
            # Remove language prefix (json, etc.)
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            elif cleaned.startswith("JSON"):
                cleaned = cleaned[3:].strip()
            try:
                return json.loads(cleaned)
            except (json.JSONDecodeError, ValueError):
                continue

    # Try direct JSON parse
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Try to find JSON object in text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _real_diagnose(incident: dict, api_key: str | None = None) -> dict:
    """Real LLM diagnosis using an OpenAI-compatible API.

    Sends summarized incident data to the LLM and parses the structured
    response. Falls back to RuntimeError if any step fails — the caller
    (diagnose_incident) handles fallback to mock behavior.

    Requires:
        - AI_DIAGNOSER_API_KEY environment variable (or api_key param)
        - Network access to the LLM API endpoint

    Environment variables:
        - AI_DIAGNOSER_API_KEY: API key (required)
        - AI_DIAGNOSER_API_BASE: Custom API base URL (optional, defaults to OpenAI)
        - AI_DIAGNOSER_MODEL: Model name (optional, defaults to gpt-4o-mini)
        - AI_DIAGNOSER_TIMEOUT: Timeout in seconds (optional, defaults to 30)
    """
    key = api_key or os.environ.get("AI_DIAGNOSER_API_KEY")
    if not key:
        raise RuntimeError(
            "AI_DIAGNOSER_API_KEY environment variable not set. "
            "Set it to use the real LLM provider."
        )

    api_base = os.environ.get("AI_DIAGNOSER_API_BASE", _DEFAULT_API_BASE)
    model = os.environ.get("AI_DIAGNOSER_MODEL", _DEFAULT_MODEL)
    timeout = int(os.environ.get("AI_DIAGNOSER_TIMEOUT", str(_DEFAULT_TIMEOUT)))

    # Build the user message from incident data
    user_message = _build_user_message(incident)

    # Call the LLM API
    response_text = _call_llm_api(key, api_base, model, user_message, timeout)

    # Parse the response
    raw = _parse_llm_response(response_text)
    if raw is None:
        raise RuntimeError(
            f"Could not parse LLM response as JSON: "
            f"{response_text[:200]}"
        )

    return raw


# ---------- PROVIDER SELECTOR ----------


def _get_provider():
    """Return the appropriate diagnosis provider.

    Uses mock mode if AI_DIAGNOSER_MOCK=true (default for tests)
    or if no API key is configured.
    """
    mock_env = os.environ.get("AI_DIAGNOSER_MOCK", "").lower()
    api_key = os.environ.get("AI_DIAGNOSER_API_KEY")

    if mock_env == "true" or not api_key:
        return _mock_diagnose, "mock"  # (provider_function, provider_used)
    else:
        return _real_diagnose, "real_llm"


# ---------- DIAGNOSIS VALIDATION ----------

_REQUIRED_FIELDS = [
    "likely_root_cause",
    "incident_scope",
    "confidence",
    "evidence",
    "short_explanation",
]


def _validate_diagnosis(raw: dict) -> Diagnosis | None:
    """Validate that the raw AI response has all required fields.

    Returns a Diagnosis if valid, None if malformed.
    """
    if not isinstance(raw, dict):
        return None

    for field in _REQUIRED_FIELDS:
        if field not in raw:
            return None

    # Validate confidence level
    confidence = raw.get("confidence", "").lower()
    if confidence not in ("high", "medium", "low"):
        return None

    # Validate evidence is a list
    evidence = raw.get("evidence")
    if not isinstance(evidence, list):
        return None

    return Diagnosis(
        likely_root_cause=raw["likely_root_cause"],
        incident_scope=raw["incident_scope"],
        confidence=confidence,
        evidence=evidence,
        short_explanation=raw["short_explanation"],
    )


# ---------- EVIDENCE VERIFICATION ----------


def verify_evidence(diagnosis: Diagnosis, incident: dict) -> Verification:
    """Check the AI diagnosis against actual incident data.

    Returns:
        Verification with status "SUPPORTED" or "UNCERTAIN" and reasons.
    """
    reasons = []

    # Check 1: Does the scope match the incident?
    expected_scope = f"{incident.get('bank', '')}_{incident.get('payment_method', '')}"
    if diagnosis.incident_scope.upper() != expected_scope.upper():
        reasons.append(
            f"Scope mismatch: diagnosis says {diagnosis.incident_scope}, "
            f"but incident is {expected_scope}"
        )

    # Check 2: Is the failure rate actually elevated?
    rate = incident.get("failure_rate", 0)
    if rate < 50:
        reasons.append(
            f"Failure rate is only {rate}%, which is not elevated"
        )

    # Check 3: Are there enough failures?
    failed = incident.get("failed_payments", 0)
    if failed < 3:
        reasons.append(
            f"Only {failed} failed payments — too few to confirm an incident"
        )

    # Check 4: Is the root cause one we accept?
    if diagnosis.likely_root_cause not in VALID_ROOT_CAUSES:
        reasons.append(
            f"Unsupported root cause: {diagnosis.likely_root_cause}"
        )

    # Check 5: Does the evidence mention the actual bank?
    bank = incident.get("bank", "")
    evidence_text = " ".join(diagnosis.evidence).lower()
    if bank and bank.lower() not in evidence_text:
        reasons.append(
            f"Evidence does not mention the affected bank ({bank})"
        )

    # Check 6: Does the evidence mention the actual method?
    method = incident.get("payment_method", "")
    if method and method.lower() not in evidence_text:
        reasons.append(
            f"Evidence does not mention the affected method ({method})"
        )

    status = "SUPPORTED" if len(reasons) == 0 else "UNCERTAIN"

    if status == "SUPPORTED":
        reasons.append("All evidence checks passed against incident data")

    return Verification(status=status, reasons=reasons)


# ---------- RECOVERY CONTEXT ----------


@dataclass
class RecoveryContext:
    """Safe, structured context flags derived from a verified AI diagnosis.

    These flags influence plan scoring slightly but do NOT directly
    choose or execute recovery actions.
    """

    prefer_wait_first: bool = False
    avoid_immediate_customer_contact: bool = False
    prefer_status_recheck: bool = False
    description: str = ""


# Mapping from verified root cause to safe recovery context flags
_ROOT_CAUSE_CONTEXT_MAP = {
    "temporary_bank_degradation": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=True,
        description="Prefer wait-first plans; bank issue may self-resolve",
    ),
    "connector_timeout": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=True,
        description="Prefer wait-first plans; timeout may be transient",
    ),
    "payment_method_outage": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=False,
        description="Avoid immediate customer contact; method may recover",
    ),
    "authentication_system_failure": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=True,
        description="Allow customer retry; recheck status after attempt",
    ),
    "insufficient_funds_cluster": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Suggest alternate methods; balance issue needs action",
    ),
    "customer_data_issue": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Customer action likely needed",
    ),
    "rate_limiting": RecoveryContext(
        prefer_wait_first=True,
        avoid_immediate_customer_contact=True,
        prefer_status_recheck=True,
        description="Prefer wait-first; rate limits are temporary",
    ),
    "merchant_configuration_error": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Configuration issue; customer action may not help",
    ),
    "fraud_detection_trigger": RecoveryContext(
        prefer_wait_first=False,
        avoid_immediate_customer_contact=False,
        prefer_status_recheck=False,
        description="Fraud flags require manual review",
    ),
}


def build_recovery_context(
    diagnosis: Diagnosis,
    verification: Verification,
) -> RecoveryContext | None:
    """Convert a verified AI diagnosis into safe recovery context flags.

    Only returns a context if verification status is SUPPORTED.
    For UNCERTAIN diagnoses, returns None (no AI influence on planning).
    """
    if verification.status != "SUPPORTED":
        return None

    # Look up known context for this root cause
    context = _ROOT_CAUSE_CONTEXT_MAP.get(diagnosis.likely_root_cause)
    if context is None:
        # Unknown root cause — no context flags
        return None

    return context


# ---------- MAIN API ----------


def diagnose_incident(incident: dict, is_mock: bool = True) -> tuple[Diagnosis, Verification]:
    """Run the full AI diagnosis pipeline on an incident.

    Args:
        incident: Incident dict from incident_detector.detect_incidents()
        is_mock: If True, use mock provider (for tests). If False, try real API.

    Returns:
        (Diagnosis, Verification) tuple.

    Error handling:
        If the real provider fails (API error, timeout, malformed response,
        missing key), falls back to mock-safe diagnosis and logs the fallback.
    """
    provider_used = "mock"  # default

    if is_mock:
        provider = _mock_diagnose
    else:
        provider, provider_used = _get_provider()

    # Get raw diagnosis from provider
    try:
        raw = provider(incident)
    except (RuntimeError, NotImplementedError) as e:
        # Real provider failed — fall back to mock
        print(f"  ⚠️  LLM provider failed: {e}")
        print(f"  ⚠️  REAL LLM FAILED — USING MOCK FALLBACK")
        raw = _mock_diagnose(incident)
        provider_used = "mock_fallback"

    # Validate the response
    diagnosis = _validate_diagnosis(raw)
    if diagnosis is None:
        # Malformed response — return a safe fallback
        print(f"  ⚠️  AI response was malformed. Using safe fallback diagnosis.")
        diagnosis = Diagnosis(
            likely_root_cause="unknown",
            incident_scope="unknown",
            confidence="low",
            evidence=["AI response was malformed or incomplete"],
            short_explanation="Unable to determine root cause from AI response.",
            provider_used="mock_fallback",
        )
    else:
        diagnosis.provider_used = provider_used

    # Verify against actual incident data
    verification = verify_evidence(diagnosis, incident)

    return diagnosis, verification


def diagnose_with_context(
    incident: dict, is_mock: bool = True
) -> tuple[Diagnosis, Verification, RecoveryContext | None]:
    """Run diagnosis and build recovery context in one call.

    Returns:
        (Diagnosis, Verification, RecoveryContext or None)
    """
    diagnosis, verification = diagnose_incident(incident, is_mock=is_mock)
    context = build_recovery_context(diagnosis, verification)
    return diagnosis, verification, context


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


def run_demo(use_real_llm: bool = False) -> None:
    """Run the AI diagnosis demo with the BANK_X UPI incident.

    Args:
        use_real_llm: If True, attempt real LLM API call (requires API key).
                     If False (default), use mock provider.
    """
    from payment_simulator import generate_bank_incident
    from incident_detector import detect_incidents, group_payments, format_amount

    print()
    print("=" * 60)
    if use_real_llm:
        print("  AI INCIDENT DIAGNOSIS — REAL LLM DEMO")
    else:
        print("  AI INCIDENT DIAGNOSIS — MOCK DEMO")
    print("=" * 60)
    print()

    # Generate incident
    payments = generate_bank_incident()
    groups = group_payments(payments)
    incidents = detect_incidents(groups)

    if not incidents:
        print("  No incidents detected.\n")
        return

    incident = incidents[0]

    # Step 1: AI Diagnosis
    print("  STEP 1: AI Root Cause Analysis")
    print("  " + "─" * 50)
    diagnosis, verification = diagnose_incident(incident, is_mock=not use_real_llm)

    print(f"  Affected Bank:    {incident['bank']}")
    print(f"  Method:           {incident['payment_method']}")
    print(f"  Failure Rate:     {incident['failure_rate']}%")
    print(f"  Failed Payments:  {incident['failed_payments']}/{incident['total_payments']}")
    print(f"  Revenue At Risk:  {format_amount(incident['revenue_at_risk'])}")
    print()
    # Map provider_used to display label
    _provider_labels = {
        "mock": "MOCK AI",
        "real_llm": "REAL LLM",
        "mock_fallback": "MOCK FALLBACK (real LLM failed)",
    }
    mode_label = _provider_labels.get(diagnosis.provider_used, diagnosis.provider_used)
    print(f"  AI Provider:      [{mode_label}]")
    print(f"  AI Root Cause:    {diagnosis.likely_root_cause}")
    print(f"  Confidence:       {diagnosis.confidence.upper()}")
    print()
    print("  Evidence:")
    for e in diagnosis.evidence:
        print(f"    - {e}")
    print()
    print(f"  Explanation: {diagnosis.short_explanation}")
    print()

    # Step 2: Evidence Verification
    print("  STEP 2: Evidence Verification")
    print("  " + "─" * 50)
    status_icon = "✅" if verification.status == "SUPPORTED" else "⚠️"
    print(f"  Status: {status_icon} {verification.status}")
    for r in verification.reasons:
        print(f"    - {r}")
    print()

    # Step 3: Build recovery context from verified diagnosis
    print("  STEP 3: Recovery Context")
    print("  " + "─" * 50)
    context = build_recovery_context(diagnosis, verification)
    if context:
        print(f"  Status: AI context applied")
        print(f"  prefer_wait_first: {context.prefer_wait_first}")
        print(f"  avoid_immediate_customer_contact: {context.avoid_immediate_customer_contact}")
        print(f"  prefer_status_recheck: {context.prefer_status_recheck}")
    else:
        print(f"  Status: AI context ignored — diagnosis not supported")
    print()

    # Step 4: Recovery Twin with AI context
    print("  STEP 4: Multi-Step Recovery Twin (with AI context)")
    print("  " + "─" * 50)
    from recovery_plan import (
        ALL_PLANS,
        simulate_recovery_plan,
        score_plan,
        calculate_context_bonus,
        _plan_sort_key,
    )

    incident_payments = [p for p in payments if p["status"] == "failed"]
    results = []
    for plan in ALL_PLANS:
        result = simulate_recovery_plan(incident_payments, plan)
        base_score = score_plan(result)
        ctx_bonus = calculate_context_bonus(plan, context)
        final_score = min(100, base_score + ctx_bonus)
        results.append((plan, result, final_score, base_score, ctx_bonus))
    results.sort(key=_plan_sort_key)
    best_plan, best_result, best_score, best_base, best_bonus = results[0]
    print(f"  Recommended Plan: {best_plan.name}")
    if best_bonus > 0:
        print(f"  Simulation Score: {best_base}/100")
        print(f"  AI Context Bonus: +{best_bonus}")
        print(f"  Final Score:      {best_score}/100")
    else:
        print(f"  Plan Score: {best_score}/100")
    print(f"  Recovered: {best_result.recovered}/{best_result.total_failed} payments")
    print()

    # Step 5: Demo with intentionally incorrect diagnosis
    print("  STEP 5: Incorrect AI Diagnosis (Verification Catches It)")
    print("  " + "─" * 50)
    bad_incident = {
        "bank": "BANK_X",
        "payment_method": "UPI",
        "error_reason": "technical_error",
        "failed_payments": 7,
        "total_payments": 10,
        "failure_rate": 70.0,
    }
    bad_diagnosis = Diagnosis(
        likely_root_cause="customer_data_issue",
        incident_scope="HDFC_Card",  # WRONG — says HDFC Card instead of BANK_X UPI
        confidence="high",
        evidence=["HDFC Card payments failed due to customer data issues"],
        short_explanation="This appears to be a customer data issue on HDFC Card.",
    )
    bad_verification = verify_evidence(bad_diagnosis, bad_incident)
    print(f"  AI says: {bad_diagnosis.incident_scope} — {bad_diagnosis.likely_root_cause}")
    print(f"  Status:  {bad_verification.status}")
    for r in bad_verification.reasons:
        print(f"    - {r}")
    print()

    print("  NOTE: AI diagnosis is for explanation only.")
    print("  Recovery decisions are made by the Recovery Twin + Safety Gate.")
    print()
    print("=" * 60)
    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AI Incident Diagnosis Demo")
    parser.add_argument(
        "--real", action="store_true",
        help="Use real LLM API (requires AI_DIAGNOSER_API_KEY env var)"
    )
    args = parser.parse_args()

    run_demo(use_real_llm=args.real)
