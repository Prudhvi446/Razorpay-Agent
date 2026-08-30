"""
seed_failures.py — Populate the database with a deterministic, rich dataset
that demonstrates all core recovery features, A/B evaluation benchmarks,
and defensive edge cases for pitch presentations and live dashboard demos.

Usage:
    cd backend
    python -m scripts.seed_failures [--reset]

Populates:
    1. A/B Evaluation Benchmark Set: 50 Control (static rules, 26.6% ~ 28% recovery)
       vs 50 AI Treatment (dynamic recovery, 70.1% recovery -> +163.1% lift).
    2. Detailed Root Causes & Dynamic Interventions:
       - Case A: Bank Gateway Downtime (₹8,499, UPI Intent fallback)
       - Case B: Insufficient Funds (₹14,999, 3-part split payment)
       - Case C: Cart Saver (₹3,200, Day 3 follow-up, 10% discount checkout link)
    3. Defensive Guardrails & Edge Cases:
       - Case D: Fraud / Dispute Kill Switch (Escalated_to_Human / HALTED_FRAUD_DISPUTE)
       - Case E: Frequency Capping Rate Limit (contact_count = 2 / 12h, RATE_LIMIT_EXCEEDED)
       - Case F: Customer DND Opt-Out (reply 'STOP', OPTED_OUT)
       - Case G: Promise-to-Pay (PROMISE_SCHEDULED vs PROMISE_BREACHED)
       - Case H: Quiet Hours Delay (2:30 AM IST -> QUEUED_FOR_MORNING_WINDOW at 08:30 AM IST)
"""

import json
import os
import sys
import uuid
import argparse
from datetime import datetime, timedelta
import pytz

# Add parent dir to path so backend modules are importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET
from database import SessionLocal, engine, Base, migrate_schema
from models import (
    PaymentEvent, AuditLog, CustomerProfile,
    PromiseToPay, RecoveryAction, Diagnosis, TransactionStatus,
    ProcessedWebhook, DeadLetterQueue
)

import razorpay

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Reconfigure stdout to utf-8 on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Customer Pool & Synthetic Data Helpers ────────────────────────

CUSTOMER_POOL = [
    {"name": "Aarav Sharma", "email": "aarav.sharma@example.com", "contact": "+919876543001"},
    {"name": "Priya Patel", "email": "priya.patel@example.com", "contact": "+919876543002"},
    {"name": "Rahul Mehta", "email": "rahul.mehta@example.com", "contact": "+919876543003"},
    {"name": "Sneha Gupta", "email": "sneha.gupta@example.com", "contact": "+919876543004"},
    {"name": "Vikram Singh", "email": "vikram.singh@example.com", "contact": "+919876543005"},
    {"name": "Ananya Reddy", "email": "ananya.reddy@example.com", "contact": "+919876543006"},
    {"name": "Karthik Nair", "email": "karthik.nair@example.com", "contact": "+919876543007"},
    {"name": "Deepa Iyer", "email": "deepa.iyer@example.com", "contact": "+919876543008"},
    {"name": "Rohit Joshi", "email": "rohit.joshi@example.com", "contact": "+919876543009"},
    {"name": "Meera Krishnan", "email": "meera.krishnan@example.com", "contact": "+919876543010"},
    {"name": "Arjun Verma", "email": "arjun.verma@example.com", "contact": "+919876543011"},
    {"name": "Neha Deshmukh", "email": "neha.deshmukh@example.com", "contact": "+919876543012"},
    {"name": "Aditya Kulkarni", "email": "aditya.kulkarni@example.com", "contact": "+919876543013"},
    {"name": "Kavita Rao", "email": "kavita.rao@example.com", "contact": "+919876543014"},
    {"name": "Siddharth Menon", "email": "siddharth.menon@example.com", "contact": "+919876543015"},
    {"name": "Pooja Banerjee", "email": "pooja.banerjee@example.com", "contact": "+919876543016"},
    {"name": "Manish Tiwari", "email": "manish.tiwari@example.com", "contact": "+919876543017"},
    {"name": "Swati Saxena", "email": "swati.saxena@example.com", "contact": "+919876543018"},
    {"name": "Varun Kapoor", "email": "varun.kapoor@example.com", "contact": "+919876543019"},
    {"name": "Divya Nambiar", "email": "divya.nambiar@example.com", "contact": "+919876543020"},
]

FAILURE_SCENARIOS = [
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Payment failed because the card has insufficient funds.", "soft_decline_retry", 8),
    ("BAD_REQUEST_ERROR", "card_declined", "The card was declined by the issuing bank.", "hard_decline_new_method", 6),
    ("BAD_REQUEST_ERROR", "expired_card", "The card has expired. Please use a valid card.", "hard_decline_new_method", 5),
    ("GATEWAY_ERROR", "gateway_timeout", "Payment could not be completed due to a bank gateway timeout. Please retry.", "network_bank_issue", 4),
    ("GATEWAY_ERROR", "gateway_error", "The bank's payment gateway returned an error.", "network_bank_issue", 3),
    ("BAD_REQUEST_ERROR", "authentication_failed", "3D Secure authentication was not completed by the cardholder.", "auth_failure_3ds", 4),
    ("BAD_REQUEST_ERROR", "mandate_not_approved", "The eMandate/NACH mandate was not approved by the customer's bank.", "mandate_issue", 4),
    (None, None, None, "customer_abandoned", 5),
    ("GATEWAY_ERROR", "bank_declined", "The transaction was declined by the issuing bank.", "network_bank_issue", 2),
    ("BAD_REQUEST_ERROR", "payment_cancelled", "Customer cancelled the payment on the checkout page.", "customer_abandoned", 3),
]


def build_weighted_scenarios(target_count=40):
    """Build a list of scenarios weighted by configured distribution (maintained for test compatibility)."""
    pool = []
    for scenario in FAILURE_SCENARIOS:
        pool.extend([scenario] * scenario[4])
    return pool[:target_count]


# Deterministic 50 baseline scenarios used for both Control and AI groups:
# 15 Insufficient Funds, 12 Card Expiration/Decline, 10 Network/Bank, 5 3DS Auth, 4 Mandate, 4 Abandoned
BASELINE_50_SCENARIOS = [
    # 15 Insufficient Funds (soft_decline_retry)
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Payment failed because the card has insufficient funds.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Debit card balance deficit for online transaction.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Insufficient funds on linked bank account.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Payment failed: Account balance below purchase threshold.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Card decline: Available balance is insufficient.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Payment decline: Insufficient balance on debit card.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Transaction failed due to insufficient funds.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Payment declined: Account limit / low balance.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Insufficient balance to process payment.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Declined: Insufficient funds on customer card.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Card transaction declined due to low balance.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Payment could not proceed due to insufficient funds.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Customer card balance insufficient for amount.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Insufficient balance for transaction completion.", "soft_decline_retry", "card"),
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Temporary fund deficit on payment method.", "soft_decline_retry", "card"),

    # 12 Card Expiration / Decline (hard_decline_new_method)
    ("BAD_REQUEST_ERROR", "expired_card", "The card has expired. Please use a valid card.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "expired_card", "Card expiry date has lapsed.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "expired_card", "Credit card past expiration date.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "expired_card", "Payment rejected: Card validity period has expired.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "card_declined", "The card was declined by the issuing bank.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "card_declined", "Bank declined debit instrument. Card status inactive.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "card_declined", "Card blocked by issuing bank for online transactions.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "card_declined", "Issuing bank declined the transaction request.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "card_not_supported", "Payment method not supported for recurring billing.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "debit_instrument_blocked", "Debit card instrument blocked by issuer.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "expired_card", "The card has expired. Replacement card required.", "hard_decline_new_method", "card"),
    ("BAD_REQUEST_ERROR", "card_declined", "Issuer permanently declined card transaction.", "hard_decline_new_method", "card"),

    # 10 Network & Gateway Issues (network_bank_issue)
    ("GATEWAY_ERROR", "gateway_timeout", "Payment could not be completed due to bank gateway timeout.", "network_bank_issue", "netbanking"),
    ("GATEWAY_ERROR", "gateway_error", "The bank's payment gateway returned an error.", "network_bank_issue", "netbanking"),
    ("GATEWAY_ERROR", "bank_declined", "Transaction failed due to upstream bank switch connectivity issue.", "network_bank_issue", "netbanking"),
    ("GATEWAY_ERROR", "gateway_timeout", "Upstream server timeout while connecting to bank gateway.", "network_bank_issue", "netbanking"),
    ("GATEWAY_ERROR", "gateway_error", "Bank switch timeout. Response not received in window.", "network_bank_issue", "upi"),
    ("GATEWAY_ERROR", "gateway_timeout", "Payment gateway timed out waiting for issuer response.", "network_bank_issue", "netbanking"),
    ("GATEWAY_ERROR", "bank_declined", "Temporary network failure between merchant and gateway.", "network_bank_issue", "netbanking"),
    ("GATEWAY_ERROR", "gateway_error", "Payment switch encountered an internal communications error.", "network_bank_issue", "upi"),
    ("GATEWAY_ERROR", "gateway_timeout", "Bank infrastructure unreachable. Timeout occurred.", "network_bank_issue", "netbanking"),
    ("GATEWAY_ERROR", "gateway_error", "Gateway response code: Bank service temporarily unavailable.", "network_bank_issue", "netbanking"),

    # 5 3D Secure / Authentication Failures (auth_failure_3ds)
    ("BAD_REQUEST_ERROR", "authentication_failed", "3D Secure authentication was not completed by the cardholder.", "auth_failure_3ds", "card"),
    ("BAD_REQUEST_ERROR", "3ds_authentication_failed", "Customer did not enter the bank OTP in time.", "auth_failure_3ds", "card"),
    ("BAD_REQUEST_ERROR", "incorrect_otp", "The OTP entered by the customer was incorrect.", "auth_failure_3ds", "card"),
    ("BAD_REQUEST_ERROR", "otp_expired", "Bank OTP expired before customer verification was complete.", "auth_failure_3ds", "card"),
    ("BAD_REQUEST_ERROR", "authentication_failed", "Cardholder 3DS authentication session expired.", "auth_failure_3ds", "card"),

    # 4 Mandate Issues (mandate_issue)
    ("BAD_REQUEST_ERROR", "mandate_not_approved", "The eMandate/NACH mandate was not approved by customer's bank.", "mandate_issue", "emandate"),
    ("BAD_REQUEST_ERROR", "mandate_not_approved", "Subscription mandate registration rejected by issuer.", "mandate_issue", "emandate"),
    ("BAD_REQUEST_ERROR", "mandate_not_approved", "Recurring auto-debit authorization pending customer approval.", "mandate_issue", "emandate"),
    ("BAD_REQUEST_ERROR", "mandate_not_approved", "Customer bank declined recurring mandate setup.", "mandate_issue", "emandate"),

    # 4 Cart Abandonment (customer_abandoned)
    (None, None, None, "customer_abandoned", None),
    ("BAD_REQUEST_ERROR", "payment_cancelled", "Customer cancelled the payment on checkout page.", "customer_abandoned", None),
    (None, None, None, "customer_abandoned", None),
    ("BAD_REQUEST_ERROR", "payment_cancelled", "User dismissed payment modal before completing transaction.", "customer_abandoned", None),
]

# 50 Deterministic Amounts in Paise (Average ~₹15,045)
AMOUNTS_50 = [
    129900, 149900, 199900, 249900, 299900, 349900, 399900, 449900, 499900, 549900,
    599900, 649900, 699900, 749900, 799900, 849900, 899900, 949900, 999900, 1099900,
    1199900, 1249900, 1299900, 1349900, 1399900, 1499900, 1599900, 1699900, 1749900, 1799900,
    1849900, 1899900, 1949900, 1999900, 2099900, 2199900, 2249900, 2299900, 2349900, 2399900,
    2449900, 2499900, 2599900, 2699900, 2749900, 2799900, 2849900, 2899900, 2949900, 2999900,
]

# Control group: Exactly 14 recovered out of 50 = 28.0% count, 26.6% currency recovery rate
CONTROL_RECOVERED_INDICES = {0, 3, 7, 10, 14, 18, 21, 25, 29, 32, 36, 40, 43, 47}

# AI group: Exactly 35 recovered out of 50 = 70.0% count, 70.1% currency recovery rate
AI_UNRECOVERED_INDICES = {0, 3, 6, 9, 13, 16, 20, 24, 28, 32, 36, 40, 43, 46, 49}
AI_RECOVERED_INDICES = set(range(50)) - AI_UNRECOVERED_INDICES


def wipe_database(db):
    """Cleanly wipe previous records across all tables."""
    print("  [Reset] Wiping existing data across all tables...")
    db.query(RecoveryAction).delete()
    db.query(Diagnosis).delete()
    db.query(PromiseToPay).delete()
    db.query(AuditLog).delete()
    db.query(CustomerProfile).delete()
    db.query(ProcessedWebhook).delete()
    db.query(DeadLetterQueue).delete()
    db.query(PaymentEvent).delete()
    db.commit()
    print("  [Reset] All tables cleared cleanly.")


def seed(reset=True):
    """Main seeding function populating deterministic evaluation & demo datasets."""
    print("=" * 70)
    print("  Razorpay Revenue Recovery Agent — Deterministic Data Seeder")
    print("=" * 70)

    migrate_schema(engine)
    db = SessionLocal()

    if reset:
        wipe_database(db)

    ground_truth = {}
    total_seeded = 0

    print("\n[1/4] Seeding A/B Evaluation Benchmark Set (50 Control vs 50 AI Treatment)...")

    # ─────────────────────────────────────────────────────────────────
    # 1. Control Group (50 records, ab_group="control", 26.6% recovery)
    # ─────────────────────────────────────────────────────────────────
    control_recovered_count = 0
    for i in range(50):
        error_code, error_reason, error_desc, gt_cat, method = BASELINE_50_SCENARIOS[i]
        amount = AMOUNTS_50[i]
        customer = CUSTOMER_POOL[i % len(CUSTOMER_POOL)]
        is_recovered = (i in CONTROL_RECOVERED_INDICES)
        is_abandoned = (gt_cat == "customer_abandoned" and error_code is None)

        pe_id = f"pe_ctrl_{i+1:02d}_{uuid.uuid4().hex[:6]}"
        order_id = f"order_ctrl_{i+1:02d}_{uuid.uuid4().hex[:8]}"
        rzp_pay_id = f"pay_ctrl_{i+1:02d}_{uuid.uuid4().hex[:8]}" if not is_abandoned else None
        cust_id = f"cust_ctrl_{i+1:02d}"

        created_ts = datetime.utcnow() - timedelta(hours=24, minutes=i * 15)

        pe = PaymentEvent(
            id=pe_id,
            webhook_event_id=f"evt_ctrl_{i+1:02d}_{uuid.uuid4().hex[:8]}",
            razorpay_payment_id=rzp_pay_id,
            order_id=order_id,
            customer_id=cust_id,
            customer_email=customer["email"],
            customer_contact=customer["contact"],
            amount=amount,
            currency="INR",
            status="created" if is_abandoned else "failed",
            lifecycle_status=TransactionStatus.PAID if is_recovered else TransactionStatus.PENDING,
            opted_out=False,
            method=method,
            error_code=error_code,
            error_description=error_desc,
            error_reason=error_reason,
            event_type="order.created" if is_abandoned else "payment.failed",
            disputed=False,
            fraud_suspected=False,
            ab_group="control",
            escalation_stage=1,
            created_at=created_ts,
            raw_payload={
                "source": "seed_failures",
                "ab_group": "control",
                "mode": "static_checkout_rules",
                "customer": customer,
            },
        )
        db.add(pe)

        # In Control Group, diagnosis follows static rules (~82% accuracy due to ambiguous declines)
        is_diag_correct = (i % 6 != 0)  # 41 of 50 correct = 82% accuracy
        diag_category = gt_cat if is_diag_correct else "hard_decline_new_method"

        diag = Diagnosis(
            payment_event_id=pe.id,
            root_cause_category=diag_category,
            confidence=0.78,
            llm_reasoning=f"Control group static rule classification: {diag_category}.",
            created_at=created_ts + timedelta(minutes=1),
        )
        db.add(diag)
        db.flush()

        # Recovery Action: Generic static repeated checkout link
        action_status = "executed" if is_recovered else "failed"
        action_outcome = (
            "Static repeated checkout link dispatched. Customer completed payment on generic retry."
            if is_recovered
            else "Static repeated checkout link expired after 3 automated attempts without conversion."
        )
        action = RecoveryAction(
            diagnosis_id=diag.id,
            action_type="retry_payment_link",
            status=action_status,
            template_used="control_static",
            payment_link_url=f"https://rzp.io/i/ctrl_retry_{uuid.uuid4().hex[:8]}",
            scheduled_at=created_ts + timedelta(minutes=5),
            executed_at=created_ts + timedelta(minutes=10) if is_recovered else None,
            outcome=action_outcome,
            created_at=created_ts + timedelta(minutes=2),
        )
        db.add(action)

        db.add(AuditLog(
            actor="system",
            action="action_executed" if is_recovered else "action_failed",
            reasoning=f"Control Group (Static Rule): {action_outcome} [Amount: ₹{amount/100:,.2f}]",
            related_entity_type="RecoveryAction",
            related_entity_id=action.id,
            timestamp=created_ts + timedelta(minutes=10),
        ))

        ground_truth[pe.id] = {"category": gt_cat, "ab_group": "control"}
        if is_recovered:
            control_recovered_count += 1
        total_seeded += 1

    print(f"  [OK] Control Group: 50 records seeded. Recovered: {control_recovered_count}/50 ({control_recovered_count/50*100:.1f}%)")

    # ─────────────────────────────────────────────────────────────────
    # 2. AI Treatment Group (50 records, ab_group="ai", 70.1% recovery)
    # ─────────────────────────────────────────────────────────────────
    ai_recovered_count = 0
    for i in range(50):
        error_code, error_reason, error_desc, gt_cat, method = BASELINE_50_SCENARIOS[i]
        amount = AMOUNTS_50[i]
        customer = CUSTOMER_POOL[(i + 7) % len(CUSTOMER_POOL)]
        is_recovered = (i in AI_RECOVERED_INDICES)
        is_abandoned = (gt_cat == "customer_abandoned" and error_code is None)

        pe_id = f"pe_ai_{i+1:02d}_{uuid.uuid4().hex[:6]}"
        order_id = f"order_ai_{i+1:02d}_{uuid.uuid4().hex[:8]}"
        rzp_pay_id = f"pay_ai_{i+1:02d}_{uuid.uuid4().hex[:8]}" if not is_abandoned else None
        cust_id = f"cust_ai_{i+1:02d}"

        created_ts = datetime.utcnow() - timedelta(hours=18, minutes=i * 12)

        pe = PaymentEvent(
            id=pe_id,
            webhook_event_id=f"evt_ai_{i+1:02d}_{uuid.uuid4().hex[:8]}",
            razorpay_payment_id=rzp_pay_id,
            order_id=order_id,
            customer_id=cust_id,
            customer_email=customer["email"],
            customer_contact=customer["contact"],
            amount=amount,
            currency="INR",
            status="created" if is_abandoned else "failed",
            lifecycle_status=TransactionStatus.PAID if is_recovered else TransactionStatus.RECOVERY_IN_PROGRESS,
            opted_out=False,
            method=method,
            error_code=error_code,
            error_description=error_desc,
            error_reason=error_reason,
            event_type="order.created" if is_abandoned else "payment.failed",
            disputed=False,
            fraud_suspected=False,
            ab_group="ai",
            escalation_stage=1,
            created_at=created_ts,
            raw_payload={
                "source": "seed_failures",
                "ab_group": "ai",
                "mode": "agent_contextual_recovery",
                "customer": customer,
            },
        )
        db.add(pe)

        # AI Agent group diagnosis: 49 of 50 correct = 98.0% classification accuracy
        is_diag_correct = (i != 48)
        diag_category = gt_cat if is_diag_correct else "unrecoverable"

        diag = Diagnosis(
            payment_event_id=pe.id,
            root_cause_category=diag_category,
            confidence=0.95,
            llm_reasoning=f"Gemini LLM enriched root-cause diagnosis: {diag_category} (confidence 0.95). Contextual intent confirmed.",
            created_at=created_ts + timedelta(minutes=1),
        )
        db.add(diag)
        db.flush()

        # Outcome distribution: 35 executed, 6 pending, 4 scheduled, 3 escalated, 2 failed
        if is_recovered:
            act_status = "executed"
            act_type = "retry_payment_link" if gt_cat in ("soft_decline_retry", "network_bank_issue") else "send_email"
            act_outcome = f"AI dynamic intervention successful: Contextual outreach for {gt_cat} completed. Payment captured."
        elif i in (0, 3, 6, 9, 13, 16):
            act_status = "pending"
            act_type = "retry_payment_link"
            act_outcome = "Contextual recovery link generated; waiting for customer session."
        elif i in (20, 24, 28, 32):
            act_status = "scheduled"
            act_type = "send_email"
            act_outcome = "Multi-day escalation scheduled for Day 2 optimal window."
        elif i in (36, 40, 43):
            act_status = "Escalated_to_Human"
            act_type = "escalate_human"
            act_outcome = "Unresolved after automated channel exhaustion. Escalated to retention specialist."
        else:
            act_status = "failed"
            act_type = "retry_payment_link"
            act_outcome = "Temporary communication channel error."

        action = RecoveryAction(
            diagnosis_id=diag.id,
            action_type=act_type,
            status=act_status,
            template_used="ai_dynamic_contextual",
            payment_link_url=f"https://rzp.io/i/ai_dyn_{uuid.uuid4().hex[:8]}",
            scheduled_at=created_ts + timedelta(minutes=5),
            executed_at=created_ts + timedelta(minutes=8) if act_status == "executed" else None,
            outcome=act_outcome,
            created_at=created_ts + timedelta(minutes=2),
        )
        db.add(action)

        db.add(AuditLog(
            actor="agent",
            action="action_executed" if act_status == "executed" else "decision_made",
            reasoning=f"AI Recovery Agent: {act_outcome} [Amount: ₹{amount/100:,.2f}]",
            related_entity_type="RecoveryAction",
            related_entity_id=action.id,
            timestamp=created_ts + timedelta(minutes=8),
        ))

        ground_truth[pe.id] = {"category": gt_cat, "ab_group": "ai"}
        if is_recovered:
            ai_recovered_count += 1
        total_seeded += 1

    print(f"  [OK] AI Agent Group: 50 records seeded. Recovered: {ai_recovered_count}/50 ({ai_recovered_count/50*100:.1f}%)")

    # ─────────────────────────────────────────────────────────────────
    # 2. Detailed Root Causes & Contextual Interventions (Cases A, B, C)
    # ─────────────────────────────────────────────────────────────────
    print("\n[2/4] Seeding Detailed Root Cause Scenarios (Cases A, B, C)...")

    # Case A: Bank Gateway Downtime (₹8,499)
    pe_case_a = PaymentEvent(
        id="pe_demo_case_a_gateway_downtime",
        webhook_event_id=f"evt_case_a_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_a_{uuid.uuid4().hex[:8]}",
        order_id="order_case_a_8499",
        customer_id="cust_case_a_hdfc",
        customer_email="vikram.aditya@example.com",
        customer_contact="+919876543101",
        amount=849900,  # ₹8,499
        currency="INR",
        status="failed",
        lifecycle_status=TransactionStatus.RECOVERY_IN_PROGRESS,
        method="netbanking",
        error_code="GATEWAY_ERROR",
        error_reason="gateway_downtime",
        error_description="HDFC/SBI Netbanking downtime detected by payment gateway (HTTP 504 Gateway Timeout)",
        event_type="payment.failed",
        ab_group="edge_cases",
        escalation_stage=1,
        created_at=datetime.utcnow() - timedelta(hours=3),
        raw_payload={
            "source": "seed_failures",
            "scenario": "Case A: Bank Gateway Downtime",
            "bank": "HDFC",
            "secondary_bank": "SBI",
            "channel": "netbanking",
            "root_cause": "GATEWAY_DOWNTIME",
        },
    )
    db.add(pe_case_a)
    diag_case_a = Diagnosis(
        payment_event_id=pe_case_a.id,
        root_cause_category="network_bank_issue",
        confidence=0.98,
        llm_reasoning="Diagnosed HDFC/SBI Netbanking route downtime. Bank server unresponsive across merchant accounts. Recommended instantaneous switch to UPI Intent fallback.",
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=58),
    )
    db.add(diag_case_a)
    db.flush()
    action_case_a = RecoveryAction(
        diagnosis_id=diag_case_a.id,
        action_type="retry_payment_link",
        status="executed",
        template_used="ai_dynamic_contextual",
        payment_link_url="https://rzp.io/i/upi_fallback_8499",
        outcome="Dynamic UPI Intent fallback link generated (upi://pay?pa=razorpay@icici&am=8499.00). Customer notified: 'HDFC/SBI Netbanking route is currently experiencing downtime. Switch to Instant UPI (GPay/PhonePe/Paytm) to complete your ₹8,499 transaction seamlessly.'",
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=55),
        executed_at=datetime.utcnow() - timedelta(hours=2, minutes=50),
    )
    db.add(action_case_a)
    db.add(AuditLog(
        actor="agent",
        action="action_executed",
        reasoning="[Case A: Bank Gateway Downtime] Executed Dynamic UPI Intent fallback link for ₹8,499 transaction. Bypassed degraded HDFC/SBI Netbanking route.",
        related_entity_type="RecoveryAction",
        related_entity_id=action_case_a.id,
        timestamp=datetime.utcnow() - timedelta(hours=2, minutes=50),
    ))
    ground_truth[pe_case_a.id] = {"category": "network_bank_issue", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case A: Bank Gateway Downtime (₹8,499, UPI Intent fallback)")

    # Case B: Insufficient Funds (₹14,999)
    pe_case_b = PaymentEvent(
        id="pe_demo_case_b_insufficient_funds",
        webhook_event_id=f"evt_case_b_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_b_{uuid.uuid4().hex[:8]}",
        order_id="order_case_b_14999",
        customer_id="cust_case_b_deficit",
        customer_email="ananya.deshmukh@example.com",
        customer_contact="+919876543102",
        amount=1499900,  # ₹14,999
        currency="INR",
        status="failed",
        lifecycle_status=TransactionStatus.RECOVERY_IN_PROGRESS,
        method="card",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        error_description="Payment declined due to insufficient funds on customer card account.",
        event_type="payment.failed",
        ab_group="edge_cases",
        escalation_stage=1,
        created_at=datetime.utcnow() - timedelta(hours=5),
        raw_payload={
            "source": "seed_failures",
            "scenario": "Case B: Insufficient Funds",
            "root_cause": "INSUFFICIENT_FUNDS",
            "balance_deficit": True,
            "cart_total": 1499900,
        },
    )
    db.add(pe_case_b)
    diag_case_b = Diagnosis(
        payment_event_id=pe_case_b.id,
        root_cause_category="soft_decline_retry",
        confidence=0.96,
        llm_reasoning="Diagnosed temporary liquidity deficit for high-ticket transaction (₹14,999). 3-part split payment installment link workflow activated to eliminate customer checkout friction.",
        created_at=datetime.utcnow() - timedelta(hours=4, minutes=58),
    )
    db.add(diag_case_b)
    db.flush()
    action_case_b = RecoveryAction(
        diagnosis_id=diag_case_b.id,
        action_type="retry_payment_link",
        status="executed",
        template_used="ai_dynamic_contextual",
        payment_link_url="https://rzp.io/i/split_part1_14999",
        outcome="3-part split payment installment link generated (Part 1: ₹5,000, Part 2: ₹5,000, Part 3: ₹4,999). Customer notified with flexible payment installment options.",
        created_at=datetime.utcnow() - timedelta(hours=4, minutes=55),
        executed_at=datetime.utcnow() - timedelta(hours=4, minutes=50),
    )
    db.add(action_case_b)
    db.add(AuditLog(
        actor="agent",
        action="decision_made",
        reasoning="[Case B: Insufficient Funds] Soft decline on ₹14,999 order converted to 3-part split installment payment links (3x ₹5,000 installments). Recovery dispatched.",
        related_entity_type="RecoveryAction",
        related_entity_id=action_case_b.id,
        timestamp=datetime.utcnow() - timedelta(hours=4, minutes=50),
    ))
    ground_truth[pe_case_b.id] = {"category": "soft_decline_retry", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case B: Insufficient Funds (₹14,999, 3-part split payment installment link)")

    # Case C: Cart Saver / Checkout Drop-off (₹3,200, Day 3 follow-up)
    pe_case_c = PaymentEvent(
        id="pe_demo_case_c_cart_saver",
        webhook_event_id=f"evt_case_c_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=None,
        order_id="order_case_c_3200",
        customer_id="cust_case_c_dropoff",
        customer_email="rohan.verma@example.com",
        customer_contact="+919876543103",
        amount=320000,  # ₹3,200
        currency="INR",
        status="created",
        lifecycle_status=TransactionStatus.PENDING,
        method=None,
        error_code=None,
        error_reason=None,
        error_description=None,
        event_type="order.created",
        ab_group="edge_cases",
        escalation_stage=2,  # Day 3 follow-up
        created_at=datetime.utcnow() - timedelta(days=3),
        raw_payload={
            "source": "seed_failures",
            "scenario": "Case C: Cart Saver / Checkout Abandoned",
            "root_cause": "CHECKOUT_ABANDONED",
            "days_dormant": 3,
            "cart_items": ["Mechanical Keyboard Pro RGB"],
        },
    )
    db.add(pe_case_c)
    diag_case_c = Diagnosis(
        payment_event_id=pe_case_c.id,
        root_cause_category="customer_abandoned",
        confidence=0.94,
        llm_reasoning="Day 3 checkout abandonment detected. Customer initiated order creation without payment attempt. Cart Saver Engine activated with time-limited 10% discount checkout link.",
        created_at=datetime.utcnow() - timedelta(days=3, minutes=-5),
    )
    db.add(diag_case_c)
    db.flush()
    action_case_c = RecoveryAction(
        diagnosis_id=diag_case_c.id,
        action_type="send_email",
        status="executed",
        discount_applied=True,
        template_used="ai_dynamic_contextual",
        payment_link_url="https://rzp.io/i/cart_saver_discount10",
        outcome="Time-limited 10% discount checkout link generated (New Total: ₹2,880, saved ₹320). Day 3 Cart Saver nudge email dispatched.",
        created_at=datetime.utcnow() - timedelta(days=3, minutes=-10),
        executed_at=datetime.utcnow() - timedelta(days=3, minutes=-15),
    )
    db.add(action_case_c)
    db.add(AuditLog(
        actor="agent",
        action="email_sent",
        reasoning="[Case C: Cart Saver] Dispatched Day 3 follow-up checkout link with 10% time-limited discount incentive (₹2,880 net total). Retention workflow completed.",
        related_entity_type="RecoveryAction",
        related_entity_id=action_case_c.id,
        timestamp=datetime.utcnow() - timedelta(days=3, minutes=-15),
    ))
    ground_truth[pe_case_c.id] = {"category": "customer_abandoned", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case C: Cart Saver / Checkout Drop-off (₹3,200, Day 3 follow-up, 10% discount link)")

    # ─────────────────────────────────────────────────────────────────
    # 3. Defensive Guardrails & Edge Cases (Cases D, E, F, G, H)
    # ─────────────────────────────────────────────────────────────────
    print("\n[3/4] Seeding Defensive Guardrails & Edge Cases (Cases D, E, F, G, H)...")

    # Case D: Fraud / Dispute Kill Switch
    pe_case_d = PaymentEvent(
        id="pe_demo_case_d_fraud_dispute",
        webhook_event_id=f"evt_case_d_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_d_{uuid.uuid4().hex[:8]}",
        order_id="order_case_d_45000",
        customer_id="cust_case_d_fraud",
        customer_email="sanjay.singhania@example.com",
        customer_contact="+919876543104",
        amount=4500000,  # ₹45,000
        currency="INR",
        status="failed",
        lifecycle_status=TransactionStatus.DISPUTED,
        disputed=True,
        fraud_suspected=True,
        ab_group="edge_cases",
        escalation_stage=1,
        created_at=datetime.utcnow() - timedelta(hours=6),
        raw_payload={
            "source": "seed_failures",
            "disputed": True,
            "fraud_suspected": True,
            "notes": {"chargeback_risk": "critical", "risk_score": 99},
        },
    )
    db.add(pe_case_d)
    diag_case_d = Diagnosis(
        payment_event_id=pe_case_d.id,
        root_cause_category="unrecoverable",
        confidence=1.0,
        llm_reasoning="HARD KILL SWITCH ACTIVATED: Transaction flagged with disputed=True and fraud_suspected=True. Bypassing LLM diagnosis and halting all automated communications immediately.",
        created_at=datetime.utcnow() - timedelta(hours=5, minutes=58),
    )
    db.add(diag_case_d)
    db.flush()
    action_case_d = RecoveryAction(
        diagnosis_id=diag_case_d.id,
        action_type="escalate_human",
        status="Escalated_to_Human",
        outcome="HALTED_FRAUD_DISPUTE: Automated recovery halted due to suspected fraud and active dispute flag. Escalated to fraud operations team.",
        created_at=datetime.utcnow() - timedelta(hours=5, minutes=55),
        executed_at=datetime.utcnow() - timedelta(hours=5, minutes=55),
    )
    db.add(action_case_d)
    db.add(AuditLog(
        actor="system",
        action="kill_switch_activated",
        reasoning="HARD KILL SWITCH ACTIVATED: Payment event pe_demo_case_d_fraud_dispute flagged as disputed and fraud_suspected. Bypassing all LLM calls and halting pipeline for human escalation.",
        related_entity_type="PaymentEvent",
        related_entity_id=pe_case_d.id,
        timestamp=datetime.utcnow() - timedelta(hours=5, minutes=58),
    ))
    db.add(AuditLog(
        actor="system",
        action="Escalated_to_Human",
        reasoning="HARD KILL SWITCH: Disputed transaction or fraud suspected. Halting pipeline immediately and escalating to human review [HALTED_FRAUD_DISPUTE].",
        related_entity_type="RecoveryAction",
        related_entity_id=action_case_d.id,
        timestamp=datetime.utcnow() - timedelta(hours=5, minutes=55),
    ))
    ground_truth[pe_case_d.id] = {"category": "unrecoverable", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case D: Fraud / Dispute Kill Switch (Escalated_to_Human / HALTED_FRAUD_DISPUTE)")

    # Case E: Frequency Capping Rate Limit (contact_count = 2 in last 12 hours)
    cust_e_id = "cust_case_e_rate_limit"
    db.add(CustomerProfile(
        customer_id=cust_e_id,
        customer_email="ritu.agarwal@example.com",
        customer_contact="+919876543105",
        opted_out=False,
    ))
    # Event 1: Contacted 8 hours ago
    pe_e1 = PaymentEvent(
        id=f"pe_case_e_hist1_{uuid.uuid4().hex[:6]}",
        customer_id=cust_e_id,
        customer_email="ritu.agarwal@example.com",
        customer_contact="+919876543105",
        amount=150000,
        status="failed",
        contact_count=1,
        last_contacted_at=datetime.utcnow() - timedelta(hours=8),
        created_at=datetime.utcnow() - timedelta(hours=8),
        ab_group="edge_cases",
    )
    db.add(pe_e1)
    diag_e1 = Diagnosis(payment_event_id=pe_e1.id, root_cause_category="soft_decline_retry", confidence=0.9)
    db.add(diag_e1)
    db.flush()
    db.add(RecoveryAction(
        diagnosis_id=diag_e1.id,
        action_type="send_email",
        status="executed",
        created_at=datetime.utcnow() - timedelta(hours=8),
        executed_at=datetime.utcnow() - timedelta(hours=8),
    ))

    # Event 2: Contacted 4 hours ago
    pe_e2 = PaymentEvent(
        id=f"pe_case_e_hist2_{uuid.uuid4().hex[:6]}",
        customer_id=cust_e_id,
        customer_email="ritu.agarwal@example.com",
        customer_contact="+919876543105",
        amount=220000,
        status="failed",
        contact_count=1,
        last_contacted_at=datetime.utcnow() - timedelta(hours=4),
        created_at=datetime.utcnow() - timedelta(hours=4),
        ab_group="edge_cases",
    )
    db.add(pe_e2)
    diag_e2 = Diagnosis(payment_event_id=pe_e2.id, root_cause_category="soft_decline_retry", confidence=0.9)
    db.add(diag_e2)
    db.flush()
    db.add(RecoveryAction(
        diagnosis_id=diag_e2.id,
        action_type="retry_payment_link",
        status="executed",
        created_at=datetime.utcnow() - timedelta(hours=4),
        executed_at=datetime.utcnow() - timedelta(hours=4),
    ))

    # Event 3: 3rd transaction triggering RATE_LIMIT_EXCEEDED
    pe_case_e = PaymentEvent(
        id="pe_demo_case_e_rate_limit_exceeded",
        webhook_event_id=f"evt_case_e_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_e_{uuid.uuid4().hex[:8]}",
        order_id="order_case_e_3500",
        customer_id=cust_e_id,
        customer_email="ritu.agarwal@example.com",
        customer_contact="+919876543105",
        amount=350000,
        currency="INR",
        status="failed",
        contact_count=2,
        last_contacted_at=datetime.utcnow() - timedelta(hours=4),
        ab_group="edge_cases",
        created_at=datetime.utcnow() - timedelta(minutes=30),
    )
    db.add(pe_case_e)
    diag_case_e = Diagnosis(
        payment_event_id=pe_case_e.id,
        root_cause_category="soft_decline_retry",
        confidence=0.88,
        llm_reasoning="Diagnosed soft decline. Frequency cap check detected 2 prior outreach touches in the last 12 hours.",
        created_at=datetime.utcnow() - timedelta(minutes=28),
    )
    db.add(diag_case_e)
    db.flush()
    action_case_e = RecoveryAction(
        diagnosis_id=diag_case_e.id,
        action_type="stop",
        status="failed",
        outcome="RATE_LIMIT_EXCEEDED: Customer contacted 2 times in the last 12 hours (cap: 2/24h). Automated recovery action blocked.",
        created_at=datetime.utcnow() - timedelta(minutes=25),
        executed_at=datetime.utcnow() - timedelta(minutes=25),
    )
    db.add(action_case_e)
    db.add(AuditLog(
        actor="agent",
        action="Rate_Limit_Exceeded",
        reasoning="Rate_Limit_Exceeded: User ritu.agarwal@example.com has been contacted 2 times in the last 12 hours (frequency cap: 2/24h). Automated recovery action blocked to prevent fatigue.",
        related_entity_type="RecoveryAction",
        related_entity_id=action_case_e.id,
        timestamp=datetime.utcnow() - timedelta(minutes=25),
    ))
    ground_truth[pe_case_e.id] = {"category": "soft_decline_retry", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case E: Frequency Capping Rate Limit (contact_count = 2 / 12h, RATE_LIMIT_EXCEEDED)")

    # Case F: Customer DND Opt-Out (reply 'STOP')
    cust_f_id = "cust_case_f_optout"
    db.add(CustomerProfile(
        customer_id=cust_f_id,
        customer_email="rajesh.khanna@example.com",
        customer_contact="+919876543106",
        opted_out=True,
        opted_out_at=datetime.utcnow() - timedelta(hours=2),
    ))
    pe_case_f = PaymentEvent(
        id="pe_demo_case_f_opt_out_stop",
        webhook_event_id=f"evt_case_f_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_f_{uuid.uuid4().hex[:8]}",
        order_id="order_case_f_6500",
        customer_id=cust_f_id,
        customer_email="rajesh.khanna@example.com",
        customer_contact="+919876543106",
        amount=650000,
        currency="INR",
        status="failed",
        opted_out=True,
        lifecycle_status=TransactionStatus.OPTED_OUT,
        ab_group="edge_cases",
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=30),
    )
    db.add(pe_case_f)
    diag_case_f = Diagnosis(
        payment_event_id=pe_case_f.id,
        root_cause_category="soft_decline_retry",
        confidence=0.90,
        llm_reasoning="Customer profile flagged as opted_out=True due to incoming STOP reply.",
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=25),
    )
    db.add(diag_case_f)
    db.flush()
    action_case_f = RecoveryAction(
        diagnosis_id=diag_case_f.id,
        action_type="stop",
        status="stopped",
        outcome="OPTED_OUT: Customer requested STOP. Outreach permanently terminated.",
        created_at=datetime.utcnow() - timedelta(hours=2, minutes=20),
        executed_at=datetime.utcnow() - timedelta(hours=2, minutes=20),
    )
    db.add(action_case_f)
    db.add(AuditLog(
        actor="customer",
        action="OPT_OUT_RECORDED",
        reasoning="OPT_OUT_RECORDED: Customer replied 'STOP'. Marked customer profile as opted_out=True and terminated all active recovery loops.",
        related_entity_type="CustomerProfile",
        related_entity_id=cust_f_id,
        timestamp=datetime.utcnow() - timedelta(hours=2),
    ))
    ground_truth[pe_case_f.id] = {"category": "soft_decline_retry", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case F: Customer DND Opt-Out (reply 'STOP', OPTED_OUT)")

    # Case G: Promise-to-Pay Scheduled vs. Breached
    # G1: Active Promise (future date)
    pe_case_g1 = PaymentEvent(
        id="pe_demo_case_g1_promise_scheduled",
        webhook_event_id=f"evt_case_g1_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_g1_{uuid.uuid4().hex[:8]}",
        order_id="order_case_g1_12000",
        customer_id="cust_case_g1_promise",
        customer_email="pooja.bhatt@example.com",
        customer_contact="+919876543107",
        amount=1200000,  # ₹12,000
        currency="INR",
        status="failed",
        lifecycle_status=TransactionStatus.PENDING,
        ab_group="edge_cases",
        created_at=datetime.utcnow() - timedelta(hours=12),
    )
    db.add(pe_case_g1)
    diag_case_g1 = Diagnosis(
        payment_event_id=pe_case_g1.id,
        root_cause_category="soft_decline_retry",
        confidence=0.92,
        llm_reasoning="Customer contacted via recovery nudge and committed to pay on upcoming salary date.",
        created_at=datetime.utcnow() - timedelta(hours=11),
    )
    db.add(diag_case_g1)
    db.flush()
    future_promise_date = datetime.utcnow() + timedelta(days=3)
    db.add(PromiseToPay(
        customer_id=pe_case_g1.customer_id,
        payment_event_id=pe_case_g1.id,
        promised_amount=1200000,
        promised_date=future_promise_date,
        status="pending",
        created_at=datetime.utcnow() - timedelta(hours=10),
    ))
    db.add(RecoveryAction(
        diagnosis_id=diag_case_g1.id,
        action_type="retry_payment_link",
        status="pending",
        scheduled_at=future_promise_date,
        outcome="PROMISE_SCHEDULED: Customer committed to settle ₹12,000 by scheduled date. State graph paused.",
        created_at=datetime.utcnow() - timedelta(hours=10),
    ))
    db.add(AuditLog(
        actor="system",
        action="promise_recorded",
        reasoning=f"Promise-to-Pay scheduled: Customer committed to pay ₹12,000.00 by {future_promise_date.strftime('%Y-%m-%d')}. State machine paused.",
        related_entity_type="PromiseToPay",
        timestamp=datetime.utcnow() - timedelta(hours=10),
    ))
    ground_truth[pe_case_g1.id] = {"category": "soft_decline_retry", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case G1: Promise-to-Pay Scheduled (future date +3d, graph paused)")

    # G2: Breached Promise (past date expired without payment)
    pe_case_g2 = PaymentEvent(
        id="pe_demo_case_g2_promise_breached",
        webhook_event_id=f"evt_case_g2_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_g2_{uuid.uuid4().hex[:8]}",
        order_id="order_case_g2_18500",
        customer_id="cust_case_g2_breached",
        customer_email="alok.nath@example.com",
        customer_contact="+919876543108",
        amount=1850000,  # ₹18,500
        currency="INR",
        status="failed",
        lifecycle_status=TransactionStatus.ESCALATED,
        escalation_stage=3,
        ab_group="edge_cases",
        created_at=datetime.utcnow() - timedelta(days=4),
    )
    db.add(pe_case_g2)
    diag_case_g2 = Diagnosis(
        payment_event_id=pe_case_g2.id,
        root_cause_category="soft_decline_retry",
        confidence=0.90,
        llm_reasoning="Customer promised payment date passed with no settlement. Breached commitment.",
        created_at=datetime.utcnow() - timedelta(days=4),
    )
    db.add(diag_case_g2)
    db.flush()
    past_promise_date = datetime.utcnow() - timedelta(days=1)
    db.add(PromiseToPay(
        customer_id=pe_case_g2.customer_id,
        payment_event_id=pe_case_g2.id,
        promised_amount=1850000,
        promised_date=past_promise_date,
        status="PROMISE_BREACHED",
        created_at=datetime.utcnow() - timedelta(days=3),
    ))
    db.add(RecoveryAction(
        diagnosis_id=diag_case_g2.id,
        action_type="ESCALATE_TO_ACCOUNT_MANAGER",
        status="pending",
        outcome="PROMISE_BREACHED: Commitment date expired without payment. Advanced to ESCALATE_TO_ACCOUNT_MANAGER.",
        created_at=datetime.utcnow() - timedelta(days=1),
    ))
    db.add(AuditLog(
        actor="system",
        action="PROMISE_BREACHED",
        reasoning=f"PROMISE_BREACHED: Promise by customer of ₹18,500.00 expired on {past_promise_date.strftime('%Y-%m-%d')} with no payment. Advanced to ESCALATE_TO_ACCOUNT_MANAGER.",
        related_entity_type="PromiseToPay",
        timestamp=datetime.utcnow() - timedelta(days=1),
    ))
    ground_truth[pe_case_g2.id] = {"category": "soft_decline_retry", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case G2: Promise-to-Pay Breached (expired yesterday, escalated to dunning)")

    # G3: Honored Promise (for complete recovery funnel visualization)
    pe_case_g3 = PaymentEvent(
        id="pe_demo_case_g3_promise_honored",
        webhook_event_id=f"evt_case_g3_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_g3_{uuid.uuid4().hex[:8]}",
        order_id="order_case_g3_9500",
        customer_id="cust_case_g3_honored",
        customer_email="kavita.rao@example.com",
        customer_contact="+919876543014",
        amount=950000,  # ₹9,500
        currency="INR",
        status="paid",
        lifecycle_status=TransactionStatus.PAID,
        ab_group="edge_cases",
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    db.add(pe_case_g3)
    diag_case_g3 = Diagnosis(
        payment_event_id=pe_case_g3.id,
        root_cause_category="soft_decline_retry",
        confidence=0.92,
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    db.add(diag_case_g3)
    db.flush()
    db.add(PromiseToPay(
        customer_id=pe_case_g3.customer_id,
        payment_event_id=pe_case_g3.id,
        promised_amount=950000,
        promised_date=datetime.utcnow() - timedelta(hours=6),
        status="honored",
        created_at=datetime.utcnow() - timedelta(days=1),
    ))
    db.add(RecoveryAction(
        diagnosis_id=diag_case_g3.id,
        action_type="retry_payment_link",
        status="executed",
        outcome="Promise honored: Payment captured successfully before commitment expiration.",
        created_at=datetime.utcnow() - timedelta(hours=6),
        executed_at=datetime.utcnow() - timedelta(hours=6),
    ))
    db.add(AuditLog(
        actor="system",
        action="promise_honored",
        reasoning="Promise honored: Customer payment of ₹9,500.00 received before commitment expiration.",
        related_entity_type="PromiseToPay",
        timestamp=datetime.utcnow() - timedelta(hours=6),
    ))
    ground_truth[pe_case_g3.id] = {"category": "soft_decline_retry", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case G3: Promise-to-Pay Honored (completes FunnelChart stages)")

    # Case H: Quiet Hours Delay (Triggered at 2:30 AM IST -> QUEUED_FOR_MORNING_WINDOW at 08:30 AM IST)
    kolkata_tz = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(kolkata_tz)
    quiet_time_ist = now_ist.replace(hour=2, minute=30, second=0, microsecond=0)
    quiet_time_utc = quiet_time_ist.astimezone(pytz.utc).replace(tzinfo=None)
    morning_time_ist = now_ist.replace(hour=8, minute=30, second=0, microsecond=0)
    morning_time_utc = morning_time_ist.astimezone(pytz.utc).replace(tzinfo=None)

    pe_case_h = PaymentEvent(
        id="pe_demo_case_h_quiet_hours",
        webhook_event_id=f"evt_case_h_{uuid.uuid4().hex[:8]}",
        razorpay_payment_id=f"pay_case_h_{uuid.uuid4().hex[:8]}",
        order_id="order_case_h_7500",
        customer_id="cust_case_h_quiet",
        customer_email="tanvi.kapoor@example.com",
        customer_contact="+919876543109",
        amount=750000,  # ₹7,500
        currency="INR",
        status="failed",
        lifecycle_status=TransactionStatus.PENDING,
        method="netbanking",
        error_code="GATEWAY_ERROR",
        error_reason="bank_declined",
        error_description="Bank declined transaction during late-night processing.",
        event_type="payment.failed",
        ab_group="edge_cases",
        escalation_stage=1,
        created_at=quiet_time_utc,
    )
    db.add(pe_case_h)
    diag_case_h = Diagnosis(
        payment_event_id=pe_case_h.id,
        root_cause_category="network_bank_issue",
        confidence=0.91,
        llm_reasoning="Payment failure triggered at 02:30 AM IST during mandatory quiet hours window (21:00 - 08:00 IST). Delayed outreach to morning window.",
        created_at=quiet_time_utc + timedelta(minutes=2),
    )
    db.add(diag_case_h)
    db.flush()
    action_case_h = RecoveryAction(
        diagnosis_id=diag_case_h.id,
        action_type="send_email",
        status="QUEUED_FOR_MORNING_WINDOW",
        scheduled_at=morning_time_utc,
        outcome="QUEUED_FOR_MORNING_WINDOW: Outreach scheduled for 08:30 AM IST to comply with TRAI / quiet hours policy (21:00 - 08:00 IST).",
        created_at=quiet_time_utc + timedelta(minutes=5),
    )
    db.add(action_case_h)
    db.add(AuditLog(
        actor="agent",
        action="decision_made",
        reasoning="Payment failure triggered at 02:30 AM IST. In accordance with quiet hours compliance (9 PM - 8 AM IST), recovery outreach is QUEUED_FOR_MORNING_WINDOW at 08:30 AM IST.",
        related_entity_type="RecoveryAction",
        related_entity_id=action_case_h.id,
        timestamp=quiet_time_utc + timedelta(minutes=5),
    ))
    ground_truth[pe_case_h.id] = {"category": "network_bank_issue", "ab_group": "edge_cases"}
    total_seeded += 1
    print("  [OK] Case H: Quiet Hours Delay (2:30 AM IST -> QUEUED_FOR_MORNING_WINDOW at 08:30 AM IST)")

    # ─────────────────────────────────────────────────────────────────
    # 4. Finalize Audit Log & Save Ground Truth
    # ─────────────────────────────────────────────────────────────────
    print("\n[4/4] Finalizing Database Commit and Writing Ground Truth...")

    db.add(AuditLog(
        actor="system",
        action="seed_completed",
        reasoning=f"Seeded {total_seeded} deterministic records: 50 Control (static rules, 28% recovery), 50 AI Treatment (dynamic recovery, 70% recovery -> 163% lift), Cases A-C root cause scenarios, and Cases D-H defensive guardrail edge cases.",
        related_entity_type="PaymentEvent",
        timestamp=datetime.utcnow(),
    ))

    db.commit()
    db.close()

    gt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ground_truth.json")
    with open(gt_path, "w") as f:
        json.dump(ground_truth, f, indent=2)

    print(f"\n[SUCCESS] Successfully seeded {total_seeded} PaymentEvent records into database.")
    print(f"[SUCCESS] Ground truth evaluation labels saved to {gt_path}")
    print("\nCategory Distribution Summary:")
    from collections import Counter
    categories = [v["category"] if isinstance(v, dict) else v for v in ground_truth.values()]
    counts = Counter(categories)
    for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"   - {cat:<28} : {cnt} transactions")
    print("\n" + "=" * 70 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed synthetic payment failure scenarios.")
    parser.add_argument("--reset", action="store_true", default=True, help="Wipe existing data before seeding (default: True)")
    parser.add_argument("--no-reset", dest="reset", action="store_false", help="Do not wipe existing data")
    args = parser.parse_args()

    seed(reset=args.reset)
