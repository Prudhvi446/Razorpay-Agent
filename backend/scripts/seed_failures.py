"""
seed_failures.py — Generate realistic failed payment/subscription scenarios
in Razorpay TEST MODE and insert corresponding PaymentEvent rows.

Usage:
    cd backend
    python -m scripts.seed_failures

Creates ~40 synthetic PaymentEvents with real Razorpay order IDs.
Writes ground-truth labels to scripts/ground_truth.json for evaluation.
"""

import json
import os
import sys
import random
import uuid
from datetime import datetime, timedelta

# Add parent dir to path so we can import backend modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET
from database import SessionLocal, engine, Base
from models import PaymentEvent, AuditLog

import razorpay

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ── Failure Scenarios ─────────────────────────────────────────────

FAILURE_SCENARIOS = [
    # (error_code, error_reason, error_description, ground_truth_category, weight)
    ("BAD_REQUEST_ERROR", "insufficient_funds", "Payment failed because the card has insufficient funds.", "soft_decline_retry", 8),
    ("BAD_REQUEST_ERROR", "card_declined", "The card was declined by the issuing bank.", "hard_decline_new_method", 6),
    ("BAD_REQUEST_ERROR", "expired_card", "The card has expired. Please use a valid card.", "hard_decline_new_method", 5),
    ("GATEWAY_ERROR", "gateway_timeout", "Payment could not be completed due to a bank gateway timeout. Please retry.", "network_bank_issue", 4),
    ("GATEWAY_ERROR", "gateway_error", "The bank's payment gateway returned an error.", "network_bank_issue", 3),
    ("BAD_REQUEST_ERROR", "authentication_failed", "3D Secure authentication was not completed by the cardholder.", "auth_failure_3ds", 4),
    ("BAD_REQUEST_ERROR", "mandate_not_approved", "The eMandate/NACH mandate was not approved by the customer's bank.", "mandate_issue", 4),
    (None, None, None, "customer_abandoned", 5),  # No payment attempt — just order created
    ("GATEWAY_ERROR", "bank_declined", "The transaction was declined by the issuing bank.", "network_bank_issue", 2),
    ("BAD_REQUEST_ERROR", "payment_cancelled", "Customer cancelled the payment on the checkout page.", "customer_abandoned", 3),
]

CUSTOMER_POOL = [
    {"name": "Aarav Sharma", "email": "aarav.sharma@example.com", "contact": "+919876543210"},
    {"name": "Priya Patel", "email": "priya.patel@example.com", "contact": "+919876543211"},
    {"name": "Rahul Mehta", "email": "rahul.mehta@example.com", "contact": "+919876543212"},
    {"name": "Sneha Gupta", "email": "sneha.gupta@example.com", "contact": "+919876543213"},
    {"name": "Vikram Singh", "email": "vikram.singh@example.com", "contact": "+919876543214"},
    {"name": "Ananya Reddy", "email": "ananya.reddy@example.com", "contact": "+919876543215"},
    {"name": "Karthik Nair", "email": "karthik.nair@example.com", "contact": "+919876543216"},
    {"name": "Deepa Iyer", "email": "deepa.iyer@example.com", "contact": "+919876543217"},
    {"name": "Rohit Joshi", "email": "rohit.joshi@example.com", "contact": "+919876543218"},
    {"name": "Meera Krishnan", "email": "meera.krishnan@example.com", "contact": "+919876543219"},
]

AMOUNTS = [
    20000, 35000, 50000, 75000, 100000, 150000, 199900, 249900, 
    500000, 750000, 1000000, 1500000, 2500000,
]

METHODS = ["card", "card", "card", "upi", "netbanking", "emandate"]


def build_weighted_scenarios(target_count=40):
    """Build a list of scenarios weighted by the configured distribution."""
    pool = []
    for scenario in FAILURE_SCENARIOS:
        pool.extend([scenario] * scenario[4])
    random.shuffle(pool)
    return pool[:target_count]


# Reconfigure stdout to utf-8 on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_razorpay_warning_printed = False


def create_razorpay_order(amount):
    """Create a real Razorpay order in test mode if credentials are valid."""
    global _razorpay_warning_printed
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return None

    try:
        order = client.order.create({
            "amount": amount,
            "currency": "INR",
            "receipt": f"seed_{uuid.uuid4().hex[:12]}",
            "notes": {"source": "seed_failures", "env": "test"},
        })
        return order
    except Exception as e:
        if not _razorpay_warning_printed:
            print(f"  [Notice] Razorpay test API order creation skipped ({e}). Using realistic synthetic order IDs.")
            _razorpay_warning_printed = True
        return None


def seed():
    """Main seeding function."""
    print("=" * 60)
    print("Revenue Recovery Agent - Seed Failures Script")
    print("=" * 60)

    # Ensure tables exist
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    scenarios = build_weighted_scenarios(40)
    ground_truth = {}
    created = 0

    print(f"\nGenerating {len(scenarios)} failure scenarios...\n")

    for i, (error_code, error_reason, error_desc, gt_category, _weight) in enumerate(scenarios):
        customer = random.choice(CUSTOMER_POOL)
        amount = random.choice(AMOUNTS)
        method = random.choice(METHODS)

        # 50/50 A/B Testing split
        ab_group = "control_group" if (i % 2 == 0) else "ai_group"

        # Plant a few dispute and fraud test cases to validate the Hard Kill Switch
        is_disputed = (i == 3)
        is_fraud = (i == 7)

        # Create a real Razorpay order if keys exist, else realistic test order ID
        order = create_razorpay_order(amount)
        order_id = order["id"] if order else f"order_seed_{uuid.uuid4().hex[:14]}"

        # For abandoned carts, status is 'created' (no payment attempt)
        is_abandoned = (gt_category == "customer_abandoned" and error_code is None)
        status = "created" if is_abandoned else "failed"
        event_type = "order.created" if is_abandoned else "payment.failed"

        # Synthetic payment ID (Razorpay won't have one for abandoned orders)
        rzp_pay_id = None if is_abandoned else f"pay_seed_{uuid.uuid4().hex[:14]}"

        pe = PaymentEvent(
            id=str(uuid.uuid4()),
            razorpay_payment_id=rzp_pay_id,
            order_id=order_id,
            customer_id=f"cust_{customer['contact'][-6:]}",
            customer_email=customer["email"],
            customer_contact=customer["contact"],
            amount=amount,
            currency="INR",
            status=status,
            method=method if not is_abandoned else None,
            error_code=error_code,
            error_description=error_desc,
            error_reason=error_reason,
            event_type=event_type,
            disputed=is_disputed,
            fraud_suspected=is_fraud,
            ab_group=ab_group,
            escalation_stage=1,
            raw_payload={
                "source": "seed_failures",
                "customer": customer,
                "order_id": order_id,
                "method": method,
                "ab_group": ab_group,
                "disputed": is_disputed,
                "fraud_suspected": is_fraud,
            },
            created_at=datetime.utcnow() - timedelta(minutes=random.randint(5, 120)),
        )
        db.add(pe)
        ground_truth[pe.id] = {
            "category": gt_category,
            "ab_group": ab_group,
        }
        created += 1

        flag = "[DISPUTE]" if is_disputed else "[FRAUD]  " if is_fraud else ("[ABANDON]" if is_abandoned else "[FAILED] ")
        grp_tag = "[CTRL]" if ab_group == "control_group" else "[AI]  "
        print(f"  {flag} {grp_tag} [{i+1:02d}] {gt_category:<25} Rs.{amount/100:>10,.2f}  {customer['name']:<16} {order_id}")

    # Write audit log entry
    db.add(AuditLog(
        actor="system",
        action="seed_completed",
        reasoning=f"Seeded {created} synthetic failure scenarios for testing.",
        related_entity_type="PaymentEvent",
        related_entity_id=None,
    ))

    db.commit()
    db.close()

    # Write ground truth
    gt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ground_truth.json")
    with open(gt_path, "w") as f:
        json.dump(ground_truth, f, indent=2)

    print(f"\n[OK] Created {created} PaymentEvent records in database")
    print(f"[OK] Ground truth saved to {gt_path}")
    print(f"\nCategory Breakdown:")
    from collections import Counter
    categories = [v["category"] if isinstance(v, dict) else v for v in ground_truth.values()]
    counts = Counter(categories)
    for cat, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"   {cat:<28} {cnt}")
    print()


if __name__ == "__main__":
    seed()
