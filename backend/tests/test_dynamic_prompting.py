import sys
import os
import pytest

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from models import PaymentEvent, Diagnosis
from agent.execute import generate_contextual_message, get_contextual_fallback


def test_insufficient_funds_offers_split_payment():
    """Verify that Insufficient Funds prompts offer a split-payment link or schedule."""
    pe = PaymentEvent(
        id="pe_test_nsf",
        customer_id="cust_nsf",
        amount=150000,
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        error_description="Card has insufficient funds",
    )
    diag = Diagnosis(
        id="diag_nsf",
        payment_event_id=pe.id,
        root_cause_category="soft_decline_retry",
        confidence=0.9,
    )

    msg = generate_contextual_message(pe, diag, day_step=1, payment_link="https://rzp.io/test-nsf")

    assert isinstance(msg, dict)
    assert "subject" in msg
    assert "message_body" in msg
    assert msg.get("recovery_action_type") == "send_email"
    
    # Must offer split payment or flexible options
    body_lower = msg["message_body"].lower()
    subject_lower = msg["subject"].lower()
    assert "split" in body_lower or "split" in subject_lower or "flexible" in body_lower


def test_bank_gateway_downtime_suggests_upi():
    """Verify that Bank Gateway Downtime prompts suggest an alternative payment method like UPI."""
    pe = PaymentEvent(
        id="pe_test_downtime",
        customer_id="cust_downtime",
        amount=250000,
        error_code="GATEWAY_ERROR",
        error_reason="gateway_timeout",
        error_description="Bank gateway timed out",
    )
    diag = Diagnosis(
        id="diag_downtime",
        payment_event_id=pe.id,
        root_cause_category="network_bank_issue",
        confidence=0.85,
    )

    msg = generate_contextual_message(pe, diag, day_step=1, payment_link="https://rzp.io/test-upi")

    assert isinstance(msg, dict)
    assert "subject" in msg
    assert "message_body" in msg
    assert msg.get("recovery_action_type") == "send_email"

    body_lower = msg["message_body"].lower()
    subject_lower = msg["subject"].lower()
    # Must mention UPI or alternative method and address bank/gateway issue
    assert "upi" in body_lower or "upi" in subject_lower or "alternative" in body_lower


def test_cart_saver_discount_applied_on_day3():
    """Verify that Cart Saver discount is reflected when discount_applied is True."""
    pe = PaymentEvent(
        id="pe_test_cart",
        customer_id="cust_cart",
        amount=500000,
        error_code=None,
        error_reason=None,
        status="created",
    )
    diag = Diagnosis(
        id="diag_cart",
        payment_event_id=pe.id,
        root_cause_category="customer_abandoned",
        confidence=0.95,
    )

    msg = generate_contextual_message(pe, diag, day_step=2, discount_applied=True, payment_link="https://rzp.io/test-discount")

    assert isinstance(msg, dict)
    body_lower = msg["message_body"].lower()
    subject_lower = msg["subject"].lower()
    assert "discount" in body_lower or "discount" in subject_lower or "5%" in body_lower


def test_polite_compliant_tone_enforced():
    """Verify output contains no aggressive or non-compliant debt collection terms."""
    categories = ["soft_decline_retry", "network_bank_issue", "hard_decline_new_method", "customer_abandoned"]
    forbidden_terms = ["legal action", "police", "arrest", "court", "debt collector", "penalty", "jail", "lawsuit"]

    for cat in categories:
        pe = PaymentEvent(id=f"pe_{cat}", amount=100000)
        diag = Diagnosis(id=f"diag_{cat}", root_cause_category=cat)
        msg = generate_contextual_message(pe, diag, day_step=1)

        body_lower = msg["message_body"].lower()
        subject_lower = msg["subject"].lower()

        for term in forbidden_terms:
            assert term not in body_lower, f"Forbidden term '{term}' found in body for {cat}"
            assert term not in subject_lower, f"Forbidden term '{term}' found in subject for {cat}"
