"""
webhooks.py — Razorpay webhook receiver with HMAC SHA256 signature verification.

Route: POST /webhooks/razorpay

Handles:
  - payment.failed
  - payment.authorized
  - subscription.charged.failed
  - subscription.cancelled
  - order.paid
"""

import hmac
import hashlib
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from sqlalchemy.orm import Session

from database import SessionLocal
from models import PaymentEvent, AuditLog
from config import RAZORPAY_WEBHOOK_SECRET

router = APIRouter()

HANDLED_EVENTS = {
    "payment.failed",
    "payment.authorized",
    "subscription.charged.failed",
    "subscription.cancelled",
    "order.paid",
}


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify Razorpay webhook HMAC-SHA256 signature."""
    expected = hmac.new(
        key=secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_payment_data(event_type: str, payload: dict) -> dict:
    """Extract payment/subscription data from the webhook payload."""
    if event_type.startswith("payment."):
        entity = payload.get("payment", {}).get("entity", {})
        return {
            "razorpay_payment_id": entity.get("id"),
            "order_id": entity.get("order_id"),
            "customer_id": entity.get("customer_id"),
            "customer_email": entity.get("email"),
            "customer_contact": entity.get("contact"),
            "amount": entity.get("amount", 0),
            "currency": entity.get("currency", "INR"),
            "status": entity.get("status", "unknown"),
            "method": entity.get("method"),
            "error_code": entity.get("error_code"),
            "error_description": entity.get("error_description"),
            "error_reason": entity.get("error_reason"),
        }
    elif event_type.startswith("subscription."):
        entity = payload.get("subscription", {}).get("entity", {})
        return {
            "razorpay_payment_id": entity.get("payment_id"),
            "order_id": entity.get("id"),  # subscription ID as order reference
            "customer_id": entity.get("customer_id"),
            "customer_email": entity.get("customer_email"),
            "customer_contact": entity.get("customer_contact"),
            "amount": entity.get("current_amount", 0),
            "currency": "INR",
            "status": "failed" if "failed" in event_type else entity.get("status", "unknown"),
            "method": "subscription",
            "error_code": entity.get("error_code"),
            "error_description": entity.get("error_description"),
            "error_reason": entity.get("error_reason"),
        }
    elif event_type == "order.paid":
        entity = payload.get("order", {}).get("entity", {})
        return {
            "razorpay_payment_id": None,
            "order_id": entity.get("id"),
            "customer_id": None,
            "customer_email": None,
            "customer_contact": None,
            "amount": entity.get("amount", 0),
            "currency": entity.get("currency", "INR"),
            "status": "paid",
            "method": None,
            "error_code": None,
            "error_description": None,
            "error_reason": None,
        }
    return {}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    """Receive and verify Razorpay webhook events."""
    # Read raw body for signature verification
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    # Verify signature
    if RAZORPAY_WEBHOOK_SECRET and signature:
        if not verify_signature(body, signature, RAZORPAY_WEBHOOK_SECRET):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    # Parse payload
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = data.get("event", "")
    if event_type not in HANDLED_EVENTS:
        return {"status": "ignored", "event": event_type}

    payload = data.get("payload", {})

    # Extract and persist
    db = SessionLocal()
    try:
        payment_data = extract_payment_data(event_type, payload)

        pe = PaymentEvent(
            event_type=event_type,
            raw_payload=data,
            **payment_data,
        )
        db.add(pe)

        db.add(AuditLog(
            actor="system",
            action="webhook_received",
            reasoning=f"Received {event_type} webhook for payment {payment_data.get('razorpay_payment_id', 'N/A')}, "
                      f"amount ₹{payment_data.get('amount', 0) / 100:.2f}, "
                      f"error: {payment_data.get('error_reason', 'none')}",
            related_entity_type="PaymentEvent",
            related_entity_id=pe.id,
        ))

        db.commit()
        return {"status": "ok", "event": event_type, "payment_event_id": pe.id}

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
