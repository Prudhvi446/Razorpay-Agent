"""
promise_tracker.py — Track customer promises-to-pay and resolve them.

Functions:
  - record_promise(customer_id, payment_event_id, promised_amount, promised_date, db)
  - check_promises(db) -> int  (number of promises updated)
"""

from datetime import datetime

from sqlalchemy.orm import Session

from models import PromiseToPay, PaymentEvent, AuditLog


def record_promise(
    customer_id: str,
    payment_event_id: str,
    promised_amount: int,
    promised_date: datetime,
    db: Session,
) -> PromiseToPay:
    """
    Record a new promise-to-pay from a customer.
    
    Args:
        customer_id: Customer identifier
        payment_event_id: FK to the related PaymentEvent
        promised_amount: Amount in paise
        promised_date: Date by which customer promises to pay
        db: Database session
    
    Returns:
        Created PromiseToPay object
    """
    promise = PromiseToPay(
        customer_id=customer_id,
        payment_event_id=payment_event_id,
        promised_amount=promised_amount,
        promised_date=promised_date,
        status="pending",
    )
    db.add(promise)

    db.add(AuditLog(
        actor="system",
        action="promise_recorded",
        reasoning=(
            f"Customer {customer_id} promised ₹{promised_amount / 100:,.2f} "
            f"by {promised_date.strftime('%Y-%m-%d')} for payment event {payment_event_id[:8]}."
        ),
        related_entity_type="PromiseToPay",
        related_entity_id=promise.id,
    ))

    db.flush()
    return promise


def check_promises(db: Session) -> int:
    """
    Check all pending promises and update their status.
    
    - honored: A matching successful payment exists (status = authorized/captured/paid)
                with amount >= promised_amount, created before promised_date.
    - broken:  promised_date has passed with no matching payment.
    
    Returns:
        Number of promises whose status was updated.
    """
    pending = db.query(PromiseToPay).filter(PromiseToPay.status == "pending").all()
    updated = 0

    for promise in pending:
        # Check for a matching successful payment
        matching_payment = (
            db.query(PaymentEvent)
            .filter(
                PaymentEvent.customer_id == promise.customer_id,
                PaymentEvent.amount >= promise.promised_amount,
                PaymentEvent.status.in_(["authorized", "captured", "paid"]),
                PaymentEvent.created_at <= promise.promised_date,
            )
            .first()
        )

        if matching_payment:
            promise.status = "honored"
            db.add(AuditLog(
                actor="system",
                action="promise_honored",
                reasoning=(
                    f"Promise by {promise.customer_id} honored. "
                    f"Payment {matching_payment.razorpay_payment_id or matching_payment.id[:8]} "
                    f"of ₹{matching_payment.amount / 100:,.2f} received."
                ),
                related_entity_type="PromiseToPay",
                related_entity_id=promise.id,
            ))
            updated += 1

        elif datetime.utcnow() > promise.promised_date:
            promise.status = "broken"
            db.add(AuditLog(
                actor="system",
                action="promise_broken",
                reasoning=(
                    f"Promise by {promise.customer_id} broken. "
                    f"Promised ₹{promise.promised_amount / 100:,.2f} by "
                    f"{promise.promised_date.strftime('%Y-%m-%d')}, no payment received."
                ),
                related_entity_type="PromiseToPay",
                related_entity_id=promise.id,
            ))
            updated += 1

    if updated > 0:
        db.flush()

    return updated
