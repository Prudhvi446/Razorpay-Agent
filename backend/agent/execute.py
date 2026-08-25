"""
execute.py — Execute recovery actions by dispatching to the appropriate channel.

Function: execute(recovery_action, db)

Handlers:
  - retry_payment_link → Create Razorpay Payment Link
  - retry_subscription → Create Payment Link for subscription amount
  - send_email         → Send via Resend HTTP API
  - escalate_human     → Log and stop (no external call)
"""

import time
from datetime import datetime

import razorpay
import requests
from sqlalchemy.orm import Session

from models import RecoveryAction, Diagnosis, PaymentEvent, AuditLog
from config import (
    RAZORPAY_KEY_ID,
    RAZORPAY_KEY_SECRET,
    RESEND_API_KEY,
    RESEND_FROM_EMAIL,
)

rzp_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ── Email Templates ───────────────────────────────────────

EMAIL_TEMPLATES = {
    "soft_decline_retry": {
        "subject": "Action needed: Your payment of {amount} couldn't be processed",
        "body": """<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
<h2 style="color: #1e293b;">Payment Retry Available</h2>
<p>Hi,</p>
<p>Your recent payment of <strong>{amount}</strong> could not be processed due to insufficient funds.</p>
<p>We've created a new payment link for you to complete the transaction at your convenience:</p>
<p><a href="{payment_link}" style="display: inline-block; padding: 12px 24px; background-color: #10b981; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Complete Payment</a></p>
<p>This link will expire in 7 days.</p>
<p>If you have any questions, please contact our support team.</p>
</div>""",
    },
    "hard_decline_new_method": {
        "subject": "Payment failed — please update your payment method",
        "body": """<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
<h2 style="color: #1e293b;">Payment Method Update Required</h2>
<p>Hi,</p>
<p>Your payment of <strong>{amount}</strong> was declined. This may be because your card has expired or is no longer active.</p>
<p>Please use a different payment method to complete your transaction:</p>
<p><a href="{payment_link}" style="display: inline-block; padding: 12px 24px; background-color: #10b981; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Pay with a Different Method</a></p>
</div>""",
    },
    "auth_failure_3ds": {
        "subject": "Payment requires authentication — please try again",
        "body": """<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
<h2 style="color: #1e293b;">Authentication Required</h2>
<p>Hi,</p>
<p>Your payment of <strong>{amount}</strong> could not be completed because the bank authentication (OTP/3D Secure) was not successful.</p>
<p>Please try again — make sure to complete the OTP verification step:</p>
<p><a href="{payment_link}" style="display: inline-block; padding: 12px 24px; background-color: #10b981; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Retry Payment</a></p>
</div>""",
    },
    "mandate_issue": {
        "subject": "Mandate authorization needed for your subscription",
        "body": """<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
<h2 style="color: #1e293b;">Mandate Re-authorization Needed</h2>
<p>Hi,</p>
<p>Your subscription payment of <strong>{amount}</strong> could not be processed because the mandate/auto-debit authorization is pending.</p>
<p>Please authorize the mandate to continue your subscription:</p>
<p><a href="{payment_link}" style="display: inline-block; padding: 12px 24px; background-color: #10b981; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Authorize Mandate</a></p>
</div>""",
    },
    "customer_abandoned": {
        "subject": "You left something behind — complete your payment",
        "body": """<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
<h2 style="color: #1e293b;">Complete Your Payment</h2>
<p>Hi,</p>
<p>It looks like your recent order of <strong>{amount}</strong> wasn't completed.</p>
<p>You can pick up right where you left off:</p>
<p><a href="{payment_link}" style="display: inline-block; padding: 12px 24px; background-color: #10b981; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Complete Payment</a></p>
<p>If you've already completed this payment, please ignore this email.</p>
</div>""",
    },
    "network_bank_issue": {
        "subject": "Payment issue resolved — please retry your payment",
        "body": """<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
<h2 style="color: #1e293b;">Ready to Retry</h2>
<p>Hi,</p>
<p>Your payment of <strong>{amount}</strong> couldn't go through earlier due to a temporary bank/network issue.</p>
<p>The issue should be resolved now. Please try again:</p>
<p><a href="{payment_link}" style="display: inline-block; padding: 12px 24px; background-color: #10b981; color: white; text-decoration: none; border-radius: 6px; font-weight: bold;">Retry Payment</a></p>
</div>""",
    },
}


def get_payment_event(action: RecoveryAction, db: Session) -> PaymentEvent:
    """Traverse RecoveryAction → Diagnosis → PaymentEvent."""
    diag = db.query(Diagnosis).filter(Diagnosis.id == action.diagnosis_id).first()
    if not diag:
        return None
    return db.query(PaymentEvent).filter(PaymentEvent.id == diag.payment_event_id).first()


def create_payment_link(pe: PaymentEvent) -> dict:
    """Create a Razorpay payment link in test mode."""
    expire_by = int(time.time()) + (7 * 24 * 3600)  # 7 days from now

    payload = {
        "amount": pe.amount,
        "currency": pe.currency or "INR",
        "description": f"Recovery payment for order {pe.order_id or 'N/A'}",
        "customer": {},
        "notify": {"sms": False, "email": False},  # We handle notifications ourselves
        "expire_by": expire_by,
        "notes": {
            "original_order_id": pe.order_id or "",
            "recovery_for": pe.razorpay_payment_id or "",
            "source": "revenue_recovery_agent",
        },
    }

    # Add customer info if available
    if pe.customer_email:
        payload["customer"]["email"] = pe.customer_email
    if pe.customer_contact:
        payload["customer"]["contact"] = pe.customer_contact
    if pe.customer_id:
        payload["customer"]["name"] = pe.customer_id

    try:
        link = rzp_client.payment_link.create(payload)
        return {"id": link["id"], "short_url": link.get("short_url", ""), "status": "created"}
    except Exception as e:
        return {"id": None, "short_url": "", "status": "error", "error": str(e)}


def send_email_via_resend(to_email: str, subject: str, html_body: str) -> dict:
    """Send an email using Resend's HTTP API."""
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [to_email],
                "subject": subject,
                "html": html_body,
            },
            timeout=10,
        )
        data = resp.json()
        if resp.status_code in (200, 201):
            return {"status": "sent", "id": data.get("id", ""), "error": None}
        else:
            return {"status": "failed", "id": None, "error": data.get("message", str(data))}
    except Exception as e:
        return {"status": "failed", "id": None, "error": str(e)}


def execute(recovery_action: RecoveryAction, db: Session):
    """
    Execute a recovery action by dispatching to the appropriate handler.
    Updates the action's status and outcome.
    """
    pe = get_payment_event(recovery_action, db)
    diag = db.query(Diagnosis).filter(Diagnosis.id == recovery_action.diagnosis_id).first()
    category = diag.root_cause_category if diag else "unknown"

    amount_display = f"₹{pe.amount / 100:,.2f}" if pe else "₹0"

    try:
        if recovery_action.action_type in ("retry_payment_link", "retry_subscription"):
            # ── Create Payment Link ───────────────────────
            if not pe:
                raise ValueError("No PaymentEvent found for this action")

            link_result = create_payment_link(pe)
            recovery_action.payment_link_url = link_result.get("short_url", "")

            if link_result["status"] == "created":
                recovery_action.status = "executed"
                recovery_action.outcome = (
                    f"Payment link created: {link_result['short_url']} "
                    f"(link_id: {link_result['id']})"
                )
                audit_action = "payment_link_created"
            else:
                recovery_action.status = "failed"
                recovery_action.outcome = f"Failed to create payment link: {link_result.get('error', 'unknown')}"
                audit_action = "action_failed"

            # If this is an email-required action, also send email with the link
            if recovery_action.action_type == "retry_payment_link" and link_result["status"] == "created":
                # Email is optional for retry_payment_link; we still create the link
                pass

        elif recovery_action.action_type == "send_email":
            # ── Send Email ────────────────────────────────
            if not pe or not pe.customer_email:
                recovery_action.status = "failed"
                recovery_action.outcome = "No customer email available"
                db.add(AuditLog(
                    actor="agent",
                    action="action_failed",
                    reasoning="Cannot send email — no customer email on record.",
                    related_entity_type="RecoveryAction",
                    related_entity_id=recovery_action.id,
                ))
                recovery_action.executed_at = datetime.utcnow()
                db.flush()
                return

            # First create a payment link to include in the email
            link_result = create_payment_link(pe)
            payment_link_url = link_result.get("short_url", "https://rzp.io/placeholder")
            recovery_action.payment_link_url = payment_link_url

            # Get email template
            template = EMAIL_TEMPLATES.get(category, EMAIL_TEMPLATES["soft_decline_retry"])
            subject = template["subject"].format(amount=amount_display)
            body = template["body"].format(amount=amount_display, payment_link=payment_link_url)

            email_result = send_email_via_resend(pe.customer_email, subject, body)

            if email_result["status"] == "sent":
                recovery_action.status = "executed"
                recovery_action.outcome = (
                    f"Email sent to {pe.customer_email} (resend_id: {email_result['id']}). "
                    f"Payment link: {payment_link_url}"
                )
                audit_action = "email_sent"
            else:
                recovery_action.status = "failed"
                recovery_action.outcome = f"Email failed: {email_result.get('error', 'unknown')}"
                audit_action = "action_failed"

        elif recovery_action.action_type == "escalate_human":
            # ── Escalate — no external call ───────────────
            recovery_action.status = "executed"
            if not recovery_action.outcome:
                recovery_action.outcome = f"STOPPED — escalated to human review. Category: {category}"
            audit_action = "escalation"

        else:
            recovery_action.status = "failed"
            recovery_action.outcome = f"Unknown action type: {recovery_action.action_type}"
            audit_action = "action_failed"

    except Exception as e:
        recovery_action.status = "failed"
        recovery_action.outcome = f"Execution error: {str(e)}"
        audit_action = "action_failed"

    # Update timestamp
    recovery_action.executed_at = datetime.utcnow()

    # Audit log
    db.add(AuditLog(
        actor="agent",
        action=audit_action,
        reasoning=(
            f"Executed {recovery_action.action_type} for {category}. "
            f"Amount: {amount_display}. "
            f"Outcome: {recovery_action.outcome}"
        ),
        related_entity_type="RecoveryAction",
        related_entity_id=recovery_action.id,
    ))

    db.flush()
