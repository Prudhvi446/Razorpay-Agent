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


def check_dispute_or_fraud(payload: dict, entity: dict) -> tuple[bool, bool]:
    """Parse incoming payload for disputed or fraud_suspected flags."""
    notes = entity.get("notes", {}) if isinstance(entity, dict) else {}
    disputed = bool(
        payload.get("disputed") is True
        or entity.get("disputed") is True
        or notes.get("disputed") in (True, "true", "True", 1, "1")
    )
    fraud_suspected = bool(
        payload.get("fraud_suspected") is True
        or entity.get("fraud_suspected") is True
        or notes.get("fraud_suspected") in (True, "true", "True", 1, "1")
    )
    return disputed, fraud_suspected


def extract_payment_data(event_type: str, payload: dict) -> dict:
    """Extract payment/subscription data from the webhook payload."""
    if event_type.startswith("payment."):
        entity = payload.get("payment", {}).get("entity", {})
        disputed, fraud_suspected = check_dispute_or_fraud(payload, entity)
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
            "disputed": disputed,
            "fraud_suspected": fraud_suspected,
        }
    elif event_type.startswith("subscription."):
        entity = payload.get("subscription", {}).get("entity", {})
        disputed, fraud_suspected = check_dispute_or_fraud(payload, entity)
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
            "disputed": disputed,
            "fraud_suspected": fraud_suspected,
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


def check_opt_out_in_payload(payload: dict, entity: dict) -> bool:
    """Check if customer opted out via webhook notes or text."""
    import re
    notes = entity.get("notes", {}) if isinstance(entity, dict) else {}
    if not isinstance(notes, dict):
        notes = {}
    top_notes = payload.get("notes", {}) if isinstance(payload.get("notes"), dict) else {}

    combined_text = " ".join([
        str(v) for v in list(notes.values()) + list(top_notes.values())
        + [payload.get("customer_response", ""), entity.get("description", "")]
    ]).upper()

    return bool(re.search(r'\b(STOP|UNSUBSCRIBE|DND)\b', combined_text))


def validate_webhook_payload(event_type: str, payment_data: dict) -> tuple[bool, str]:
    """Strict validation for incoming webhook data. Catch missing or invalid critical keys."""
    amount = payment_data.get("amount")
    if amount is None or not isinstance(amount, (int, float)) or amount <= 0:
        return False, "Invalid or missing amount: must be greater than 0 paise"

    if event_type.startswith("payment."):
        if not payment_data.get("razorpay_payment_id"):
            return False, "Missing critical key: payment_id (entity.id)"
        if not (payment_data.get("customer_id") or payment_data.get("customer_email") or payment_data.get("customer_contact")):
            return False, "Missing critical key: customer identifier (customer_id, email, or contact)"

    elif event_type.startswith("subscription."):
        if not (payment_data.get("razorpay_payment_id") or payment_data.get("order_id")):
            return False, "Missing critical key: payment_id or subscription_id"
        if not (payment_data.get("customer_id") or payment_data.get("customer_email") or payment_data.get("customer_contact")):
            return False, "Missing critical key: customer identifier"

    elif event_type == "order.paid":
        if not payment_data.get("order_id"):
            return False, "Missing critical key: order_id"

    return True, ""


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request):
    """Receive and verify Razorpay webhook events with idempotency and defensive validation."""
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

    # Check Idempotency: header or payload ID or payload body hash
    event_id = (
        request.headers.get("X-Razorpay-Event-Id")
        or request.headers.get("x-razorpay-event-id")
        or data.get("event_id")
        or data.get("id")
    )
    if not event_id:
        event_id = hashlib.md5(body).hexdigest()

    db = SessionLocal()
    try:
        from models import ProcessedWebhook, RecoveryAction, CustomerProfile, TransactionStatus

        # ── 1. Idempotency Check ───────────────────────────────
        already_processed = (
            db.query(ProcessedWebhook).filter(ProcessedWebhook.event_id == event_id).first()
            or db.query(PaymentEvent).filter(PaymentEvent.webhook_event_id == event_id).first()
        )
        if already_processed:
            return {"status": "duplicate_event_ignored", "event_id": event_id, "event": event_type}

        payment_data = extract_payment_data(event_type, payload)

        # ── 2. Defensive Validation for Malformed/Partial Payloads ──
        is_valid, validation_error = validate_webhook_payload(event_type, payment_data)
        if not is_valid:
            db.add(AuditLog(
                actor="system",
                action="MALFORMED_PAYLOAD",
                reasoning=f"Malformed webhook payload ({validation_error}) for event {event_type}, event_id: {event_id}",
                related_entity_type="WebhookPayload",
                related_entity_id=event_id,
            ))
            db.commit()
            raise HTTPException(
                status_code=422,
                detail={"status": "MALFORMED_PAYLOAD", "error": validation_error, "event_id": event_id}
            )

        # ── 3. Race Condition Guard: Check if payment is already PAID or SETTLED ──
        rzp_pid = payment_data.get("razorpay_payment_id")
        order_id = payment_data.get("order_id")

        existing_pe = None
        if rzp_pid:
            existing_pe = db.query(PaymentEvent).filter(PaymentEvent.razorpay_payment_id == rzp_pid).first()
        if not existing_pe and order_id:
            existing_pe = db.query(PaymentEvent).filter(PaymentEvent.order_id == order_id).first()

        # If incoming event is payment.captured or order.paid:
        if event_type in ("order.paid", "payment.authorized") or payment_data.get("status") in ("paid", "captured", "settled"):
            if existing_pe:
                existing_pe.status = "paid"
                existing_pe.lifecycle_status = TransactionStatus.PAID
                # Abort any active recovery actions for this payment event
                from models import Diagnosis
                diag = db.query(Diagnosis).filter(Diagnosis.payment_event_id == existing_pe.id).first()
                if diag:
                    actions = db.query(RecoveryAction).filter(
                        RecoveryAction.diagnosis_id == diag.id,
                        RecoveryAction.status.in_(["pending", "scheduled", "QUEUED_FOR_MORNING_WINDOW"])
                    ).all()
                    for act in actions:
                        act.status = "ALREADY_RESOLVED"
                        act.outcome = f"ALREADY_RESOLVED: Payment completed via {event_type} webhook"

                db.add(AuditLog(
                    actor="system",
                    action="ALREADY_RESOLVED",
                    reasoning=f"Payment {existing_pe.id[:8]} marked as PAID and active recovery aborted due to {event_type}.",
                    related_entity_type="PaymentEvent",
                    related_entity_id=existing_pe.id,
                ))

        # If incoming is failure event, but payment was already PAID or SETTLED:
        if existing_pe and (
            str(existing_pe.status).upper() in ("PAID", "SETTLED", "CAPTURED")
            or getattr(existing_pe, "lifecycle_status", "").upper() in ("PAID", "SETTLED")
        ):
            db.add(AuditLog(
                actor="system",
                action="ALREADY_RESOLVED",
                reasoning=f"Incoming failure webhook {event_type} ignored because payment {existing_pe.id[:8]} is already resolved as {existing_pe.status}.",
                related_entity_type="PaymentEvent",
                related_entity_id=existing_pe.id,
            ))
            db.commit()
            return {"status": "ALREADY_RESOLVED", "message": "Payment already resolved as PAID or SETTLED", "event": event_type}

        # ── 4. Opt-Out Check from Webhook Payload / Notes ──────
        inner_entity = payload.get("payment", {}).get("entity", {}) if "payment" in payload else payload.get("subscription", {}).get("entity", {})
        customer_opted_out = check_opt_out_in_payload(payload, inner_entity)

        cust_id = payment_data.get("customer_id")
        if cust_id:
            profile = db.query(CustomerProfile).filter(CustomerProfile.customer_id == cust_id).first()
            if profile and profile.opted_out:
                customer_opted_out = True
            elif customer_opted_out:
                if not profile:
                    profile = CustomerProfile(
                        customer_id=cust_id,
                        customer_email=payment_data.get("customer_email"),
                        customer_contact=payment_data.get("customer_contact"),
                        opted_out=True,
                        opted_out_at=datetime.utcnow(),
                    )
                    db.add(profile)
                else:
                    profile.opted_out = True
                    profile.opted_out_at = datetime.utcnow()

        # Mark as processed in ProcessedWebhook
        db.add(ProcessedWebhook(event_id=event_id, event_type=event_type))

        lifecycle_status = TransactionStatus.OPTED_OUT if customer_opted_out else (
            TransactionStatus.PAID if payment_data.get("status") in ("paid", "captured", "settled") else TransactionStatus.PENDING
        )

        pe = PaymentEvent(
            webhook_event_id=event_id,
            event_type=event_type,
            raw_payload=data,
            lifecycle_status=lifecycle_status,
            opted_out=customer_opted_out,
            **payment_data,
        )
        db.add(pe)

        if customer_opted_out:
            db.add(AuditLog(
                actor="customer",
                action="OPT_OUT_RECORDED",
                reasoning=f"Customer {cust_id or 'unknown'} sent opt-out keyword in webhook notes. Terminating recovery.",
                related_entity_type="PaymentEvent",
                related_entity_id=pe.id,
            ))

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

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
