"""
diagnose.py — Root cause diagnosis using hybrid deterministic rules + Gemini LLM.

Function: diagnose(payment_event, db) -> Diagnosis

Pipeline:
  1. Apply deterministic rules based on Razorpay error_code/error_reason
  2. Call Gemini for enriched reasoning + confidence score
  3. If LLM disagrees with deterministic AND confidence > 0.85, use LLM's category
  4. Persist Diagnosis + AuditLog
"""

import json
import traceback
from datetime import datetime

from sqlalchemy.orm import Session
import google.generativeai as genai

from models import PaymentEvent, Diagnosis, RecoveryAction, AuditLog
from config import GEMINI_API_KEY, GEMINI_MODEL

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

# ── Deterministic Rules ──────────────────────────────────

ERROR_REASON_MAP = {
    "insufficient_funds":       "soft_decline_retry",
    "card_declined":            "hard_decline_new_method",
    "expired_card":             "hard_decline_new_method",
    "invalid_card_number":      "hard_decline_new_method",
    "card_not_supported":       "hard_decline_new_method",
    "debit_instrument_inactive":"hard_decline_new_method",
    "debit_instrument_blocked": "hard_decline_new_method",
    "international_not_allowed":"hard_decline_new_method",
    "gateway_timeout":          "network_bank_issue",
    "gateway_error":            "network_bank_issue",
    "bank_declined":            "network_bank_issue",
    "payment_timed_out":        "network_bank_issue",
    "authentication_failed":    "auth_failure_3ds",
    "3ds_authentication_failed":"auth_failure_3ds",
    "incorrect_otp":            "auth_failure_3ds",
    "otp_expired":              "auth_failure_3ds",
    "mandate_not_approved":     "mandate_issue",
    "payment_cancelled":        "customer_abandoned",
    "server_error":             "unrecoverable",
}


def deterministic_classify(pe: PaymentEvent) -> str:
    """Apply rule-based classification from error_reason/error_code."""
    # Abandoned carts: order created but no payment attempt
    if pe.status == "created" and pe.error_code is None:
        return "customer_abandoned"

    # Look up error_reason first (most specific)
    if pe.error_reason and pe.error_reason in ERROR_REASON_MAP:
        return ERROR_REASON_MAP[pe.error_reason]

    # Fallback: check error_code category
    if pe.error_code == "GATEWAY_ERROR":
        return "network_bank_issue"
    if pe.error_code == "SERVER_ERROR":
        return "unrecoverable"

    return "unrecoverable"


def get_retry_count(pe: PaymentEvent, db: Session) -> int:
    """Count how many recovery actions exist for this payment event."""
    diag = db.query(Diagnosis).filter(Diagnosis.payment_event_id == pe.id).first()
    if not diag:
        return 0
    return db.query(RecoveryAction).filter(RecoveryAction.diagnosis_id == diag.id).count()


# ── LLM Diagnosis ────────────────────────────────────────

DIAGNOSIS_PROMPT = """You are an AI payment failure analyst for an Indian payment gateway (Razorpay).

Analyze this failed payment and classify the root cause. Respond with STRICT JSON only.

Payment details:
- Error Code: {error_code}
- Error Reason: {error_reason}
- Error Description: {error_description}
- Amount: ₹{amount_rupees}
- Payment Method: {method}
- Payment Status: {status}
- Prior Retry Count: {retry_count}
- Deterministic Pre-classification: {deterministic_category}

Classify into EXACTLY ONE of these categories:
- soft_decline_retry: Temporary issue (insufficient funds, daily limits) — likely to succeed on retry
- hard_decline_new_method: Permanent card issue (expired, blocked, not supported) — need different payment method
- network_bank_issue: Bank/gateway timeout or error — infrastructure issue, retry may work
- auth_failure_3ds: 3D Secure / OTP authentication failed — customer needs to complete auth
- mandate_issue: eMandate/NACH not approved — customer needs to re-authorize
- customer_abandoned: Customer left checkout without completing payment
- unrecoverable: Permanent failure with no recovery path

Respond with this exact JSON structure:
{{"root_cause_category": "one_of_the_categories_above", "confidence": 0.0_to_1.0, "reasoning": "brief explanation of why this category", "recommended_action": "what should be done next"}}
"""


def llm_classify(pe: PaymentEvent, deterministic_cat: str, retry_count: int) -> dict:
    """Call Gemini for enriched diagnosis."""
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        prompt = DIAGNOSIS_PROMPT.format(
            error_code=pe.error_code or "None",
            error_reason=pe.error_reason or "None",
            error_description=pe.error_description or "No description",
            amount_rupees=pe.amount / 100 if pe.amount else 0,
            method=pe.method or "unknown",
            status=pe.status or "unknown",
            retry_count=retry_count,
            deterministic_category=deterministic_cat,
        )

        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )

        result = json.loads(response.text)
        return {
            "root_cause_category": result.get("root_cause_category", deterministic_cat),
            "confidence": float(result.get("confidence", 0.5)),
            "reasoning": result.get("reasoning", ""),
            "recommended_action": result.get("recommended_action", ""),
        }

    except Exception as e:
        # LLM failure should not block the pipeline — fall back to deterministic
        return {
            "root_cause_category": deterministic_cat,
            "confidence": 0.6,
            "reasoning": f"LLM call failed ({str(e)[:100]}), using deterministic classification.",
            "recommended_action": "proceed with deterministic action",
        }


# ── Main Diagnosis Function ──────────────────────────────

def diagnose(payment_event: PaymentEvent, db: Session) -> Diagnosis:
    """
    Diagnose a payment event's root cause.
    
    Returns a persisted Diagnosis object.
    """
    # Step 1: Deterministic pre-classification
    det_category = deterministic_classify(payment_event)
    retry_count = get_retry_count(payment_event, db)

    # Step 2: LLM enrichment
    llm_result = llm_classify(payment_event, det_category, retry_count)

    # Step 3: Resolve disagreements
    # Trust LLM only if it disagrees AND has high confidence
    final_category = det_category
    if llm_result["root_cause_category"] != det_category and llm_result["confidence"] > 0.85:
        final_category = llm_result["root_cause_category"]

    # Build reasoning text for audit trail
    reasoning = (
        f"Deterministic classification: {det_category}. "
        f"LLM classification: {llm_result['root_cause_category']} "
        f"(confidence: {llm_result['confidence']:.2f}). "
        f"Final: {final_category}. "
        f"LLM reasoning: {llm_result['reasoning']} "
        f"Recommended: {llm_result.get('recommended_action', 'N/A')}"
    )

    # Step 4: Persist Diagnosis
    diag = Diagnosis(
        payment_event_id=payment_event.id,
        root_cause_category=final_category,
        confidence=llm_result["confidence"],
        llm_reasoning=reasoning,
    )
    db.add(diag)

    # Step 5: Audit log
    db.add(AuditLog(
        actor="agent",
        action="diagnosis_completed",
        reasoning=reasoning,
        related_entity_type="Diagnosis",
        related_entity_id=diag.id,
    ))

    db.flush()  # Ensure IDs are assigned
    return diag
