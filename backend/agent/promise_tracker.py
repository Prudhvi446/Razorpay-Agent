"""
promise_tracker.py — Track customer promises-to-pay and resolve them.

Functions:
  - record_promise(customer_id, payment_event_id, promised_amount, promised_date, db)
  - check_promises(db) -> int  (number of promises updated)
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from models import PromiseToPay, PaymentEvent, AuditLog, Diagnosis, RecoveryAction, TransactionStatus


def validate_promise_date(promised_date: datetime | str) -> tuple[bool, str, datetime | None]:
    """
    Validate customer commitment dates:
    - Must be a valid datetime or ISO date string
    - Must not be in the past
    - Must not exceed 30 days in the future
    - Must not be nonsensical
    """
    if isinstance(promised_date, str):
        try:
            parsed_date = datetime.fromisoformat(promised_date)
        except Exception:
            return False, "nonsensical_unparseable", None
    elif isinstance(promised_date, datetime):
        parsed_date = promised_date
    else:
        return False, "nonsensical_type", None

    if parsed_date.year < 2020 or parsed_date.year > 2050:
        return False, "nonsensical_year", parsed_date

    now = datetime.utcnow()
    if parsed_date <= now:
        return False, "date_in_past", parsed_date

    if parsed_date > now + timedelta(days=30):
        return False, "date_exceeds_30_days", parsed_date

    return True, "valid", parsed_date


def record_promise(
    customer_id: str,
    payment_event_id: str,
    promised_amount: int,
    promised_date: datetime | str,
    db: Session,
) -> PromiseToPay:
    """
    Record a new promise-to-pay from a customer with defensive date validation.
    
    If date is in the past, nonsensical, or > 30 days out, falls back to INVALID_PROMISE_DATE
    and triggers human agent review.
    """
    is_valid, reason, parsed_date = validate_promise_date(promised_date)
    date_to_store = parsed_date or datetime.utcnow()

    if not is_valid:
        promise = PromiseToPay(
            customer_id=customer_id,
            payment_event_id=payment_event_id,
            promised_amount=promised_amount,
            promised_date=date_to_store,
            status="INVALID_PROMISE_DATE",
        )
        db.add(promise)

        db.add(AuditLog(
            actor="system",
            action="INVALID_PROMISE_DATE",
            reasoning=(
                f"Customer {customer_id} provided invalid promise date '{promised_date}' "
                f"({reason}). Status set to INVALID_PROMISE_DATE. Triggering human agent review."
            ),
            related_entity_type="PromiseToPay",
            related_entity_id=promise.id,
        ))

        # Trigger human agent review
        pe = db.query(PaymentEvent).filter(PaymentEvent.id == payment_event_id).first()
        if pe:
            pe.lifecycle_status = TransactionStatus.ESCALATED
            diag = db.query(Diagnosis).filter(Diagnosis.payment_event_id == pe.id).first()
            if diag:
                review_action = RecoveryAction(
                    diagnosis_id=diag.id,
                    action_type="escalate_human",
                    status="Escalated_to_Human",
                    outcome=f"INVALID_PROMISE_DATE: Customer provided invalid promise date ({reason}). Escalated for human review.",
                )
                db.add(review_action)

        db.flush()
        return promise

    # Valid promise
    promise = PromiseToPay(
        customer_id=customer_id,
        payment_event_id=payment_event_id,
        promised_amount=promised_amount,
        promised_date=date_to_store,
        status="pending",
    )
    db.add(promise)

    db.add(AuditLog(
        actor="system",
        action="promise_recorded",
        reasoning=(
            f"Customer {customer_id} promised ₹{promised_amount / 100:,.2f} "
            f"by {date_to_store.strftime('%Y-%m-%d')} for payment event {payment_event_id[:8]}."
        ),
        related_entity_type="PromiseToPay",
        related_entity_id=promise.id,
    ))

    db.flush()
    return promise


def get_active_promise_for_event(payment_event_id: str, db: Session) -> PromiseToPay | None:
    """
    Check if there is an active pending promise-to-pay for this payment event or customer.
    Used by the LangGraph state machine to pause recovery workflow execution.
    """
    from sqlalchemy import or_
    pe = db.query(PaymentEvent).filter(PaymentEvent.id == payment_event_id).first()
    if not pe:
        return None

    filters = [PromiseToPay.payment_event_id == payment_event_id]
    if pe.customer_id:
        filters.append(PromiseToPay.customer_id == pe.customer_id)

    return (
        db.query(PromiseToPay)
        .filter(
            or_(*filters),
            PromiseToPay.status == "pending",
            PromiseToPay.promised_date >= datetime.utcnow(),
        )
        .order_by(PromiseToPay.promised_date.desc())
        .first()
    )


def check_expired_promises(db: Session) -> int:
    """
    Automated state check function:
    - Queries all records where promise_date < CURRENT_TIMESTAMP and payment status is not PAID.
    - Automatically transitions these expired promises to PROMISE_BREACHED.
    - Advances the state graph to the next escalation node (ESCALATE_TO_ACCOUNT_MANAGER or FINAL_DUNNING_NOTICE).
    
    Returns:
        Number of promises transitioned.
    """
    now = datetime.utcnow()
    expired_pending = (
        db.query(PromiseToPay)
        .filter(
            PromiseToPay.status == "pending",
            PromiseToPay.promised_date < now,
        )
        .all()
    )

    updated = 0
    for promise in expired_pending:
        pe = db.query(PaymentEvent).filter(PaymentEvent.id == promise.payment_event_id).first()

        # Check if already paid
        is_paid = False
        if pe and (
            str(pe.status).upper() in ("PAID", "SETTLED", "CAPTURED")
            or getattr(pe, "lifecycle_status", "").upper() in ("PAID", "SETTLED")
        ):
            is_paid = True
        else:
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
                is_paid = True

        if is_paid:
            promise.status = "honored"
            db.add(AuditLog(
                actor="system",
                action="promise_honored",
                reasoning=(
                    f"Promise by {promise.customer_id} honored. "
                    f"Payment received before promise expiration."
                ),
                related_entity_type="PromiseToPay",
                related_entity_id=promise.id,
            ))
            updated += 1
        else:
            promise.status = "PROMISE_BREACHED"
            db.add(AuditLog(
                actor="system",
                action="PROMISE_BREACHED",
                reasoning=(
                    f"Promise by {promise.customer_id} of ₹{promise.promised_amount / 100:,.2f} "
                    f"expired on {promise.promised_date.strftime('%Y-%m-%d')} with no payment. "
                    f"Status transitioned to PROMISE_BREACHED."
                ),
                related_entity_type="PromiseToPay",
                related_entity_id=promise.id,
            ))

            # Advance state graph to escalation node: ESCALATE_TO_ACCOUNT_MANAGER
            if pe:
                pe.escalation_stage = 3
                pe.lifecycle_status = TransactionStatus.ESCALATED
                diag = db.query(Diagnosis).filter(Diagnosis.payment_event_id == pe.id).first()
                if diag:
                    escalate_action = RecoveryAction(
                        diagnosis_id=diag.id,
                        action_type="ESCALATE_TO_ACCOUNT_MANAGER",
                        status="pending",
                        outcome=(
                            f"PROMISE_BREACHED: Promise expired on {promise.promised_date.strftime('%Y-%m-%d')}. "
                            f"Advanced to ESCALATE_TO_ACCOUNT_MANAGER."
                        ),
                    )
                    db.add(escalate_action)
            updated += 1

    if updated > 0:
        db.flush()

    return updated


def check_promises(db: Session) -> int:
    """
    Check all pending promises and update their status (honored vs breached).
    Returns number of promises whose status was updated.
    """
    return check_expired_promises(db)

