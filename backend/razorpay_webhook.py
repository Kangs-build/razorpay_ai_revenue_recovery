"""
Razorpay Webhook Server — Razorpay AI Revenue Recovery

Flask-based HTTP server that receives Razorpay webhook events.

Endpoints:
    POST /webhooks/razorpay  — Razorpay webhook receiver
    GET  /health              — Health check

Flow:
    1. Receive raw request body
    2. Verify HMAC-SHA256 signature (before parsing JSON)
    3. Check idempotency (event deduplication)
    4. Parse and convert to internal payment format
    5. Update payment store (late-success protection)
    6. Record audit trail
    7. Return 2xx or appropriate error

Run:
    python backend/razorpay_webhook.py
"""

from __future__ import annotations

import json
import os
import sys

from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(__file__))

from razorpay_adapter import (
    RazorpayWebhookProcessor,
    SUPPORTED_EVENTS,
)

app = Flask(__name__)

# Global processor instance (in-memory stores)
# In production, replace with database-backed implementations
processor = RazorpayWebhookProcessor()


@app.route("/webhooks/razorpay", methods=["POST"])
def razorpay_webhook():
    """Receive and process Razorpay webhook events.

    Important: The raw request body is used for signature verification
    BEFORE any JSON parsing occurs.
    """
    # Get raw body bytes (must be used for signature verification)
    raw_body = request.get_data()

    # Extract headers
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("x-razorpay-event-id", "")

    # Parse JSON to get event type (but signature is verified on raw body)
    try:
        body_json = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "Invalid JSON payload"}), 400

    event_type = body_json.get("event", "")

    # Process through the adapter
    result = processor.process_webhook(
        raw_body=raw_body,
        signature=signature,
        event_id=event_id,
        event_type=event_type,
    )

    # Return appropriate HTTP response
    response = {
        "status": "ok" if result["accepted"] else "error",
        "message": result["reason"],
    }
    if result["payment_id"]:
        response["payment_id"] = result["payment_id"]
    if result["resulting_status"]:
        response["internal_status"] = result["resulting_status"]

    return jsonify(response), result["status_code"]


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "payments_tracked": len(processor.payment_store.get_all()),
        "events_processed": len(processor.audit_trail.get_all()),
    }), 200


@app.route("/payments", methods=["GET"])
def list_payments():
    """List all tracked internal payments (for debugging)."""
    payments = processor.payment_store.get_all()
    result = []
    for p in payments:
        result.append({
            "payment_id": p.payment_id,
            "amount": p.amount,
            "status": p.status,
            "payment_method": p.payment_method,
            "bank": p.bank,
            "error_reason": p.error_reason,
        })
    return jsonify({"payments": result}), 200


@app.route("/audit", methods=["GET"])
def list_audit():
    """List audit trail (for debugging)."""
    records = processor.audit_trail.get_all()
    result = []
    for r in records:
        result.append({
            "received_at": r.received_at,
            "event_id": r.event_id,
            "event_type": r.event_type,
            "payment_id": r.payment_id,
            "signature_valid": r.signature_valid,
            "is_duplicate": r.is_duplicate,
            "action": r.action,
        })
    return jsonify({"audit_records": result}), 200


def create_app(webhook_secret: str | None = None) -> Flask:
    """Factory function for creating the Flask app with custom config.

    Args:
        webhook_secret: Override the webhook secret (useful for testing).
    """
    global processor
    if webhook_secret:
        processor = RazorpayWebhookProcessor(webhook_secret=webhook_secret)
    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Razorpay Webhook Receiver")
    parser.add_argument(
        "--port", type=int, default=5000,
        help="Port to listen on (default: 5000)"
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug mode"
    )
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  RAZORPAY WEBHOOK RECEIVER")
    print("=" * 60)
    print(f"  Listening on http://{args.host}:{args.port}")
    print(f"  Webhook endpoint: POST /webhooks/razorpay")
    print(f"  Health check:     GET  /health")
    print("=" * 60)
    print()

    app.run(host=args.host, port=args.port, debug=args.debug)
