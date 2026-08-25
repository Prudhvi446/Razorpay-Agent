"""
SQLAlchemy ORM models for the Revenue Recovery Agent.

Tables:
  - PaymentEvent   — raw payment/subscription failure data
  - Diagnosis      — root cause classification (deterministic + LLM)
  - RecoveryAction — what the agent decided to do and the outcome
  - PromiseToPay   — customer promises tracked to resolution
  - AuditLog       — full audit trail of every agent/system action
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, JSON,
    ForeignKey, Enum as SAEnum,
)
from sqlalchemy.orm import relationship
from database import Base


def _uuid():
    return str(uuid.uuid4())


# ── PaymentEvent ──────────────────────────────────────────

class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id                   = Column(String, primary_key=True, default=_uuid)
    razorpay_payment_id  = Column(String, index=True, nullable=True)
    order_id             = Column(String, index=True, nullable=True)
    customer_id          = Column(String, index=True, nullable=True)
    customer_email       = Column(String, nullable=True)
    customer_contact     = Column(String, nullable=True)
    amount               = Column(Integer, nullable=False)            # in paise
    currency             = Column(String, default="INR")
    status               = Column(String, nullable=False)             # failed, authorized, captured, created
    method               = Column(String, nullable=True)              # card, upi, netbanking, etc.
    error_code           = Column(String, nullable=True)
    error_description    = Column(String, nullable=True)
    error_reason         = Column(String, nullable=True)
    event_type           = Column(String, nullable=True)              # payment.failed, order.paid, etc.
    raw_payload          = Column(JSON, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)

    # Relationships
    diagnosis            = relationship("Diagnosis", back_populates="payment_event", uselist=False)
    promises             = relationship("PromiseToPay", back_populates="payment_event")

    def __repr__(self):
        return f"<PaymentEvent {self.id[:8]} status={self.status} amount={self.amount}>"


# ── Diagnosis ─────────────────────────────────────────────

ROOT_CAUSE_CATEGORIES = [
    "soft_decline_retry",
    "hard_decline_new_method",
    "network_bank_issue",
    "auth_failure_3ds",
    "mandate_issue",
    "customer_abandoned",
    "unrecoverable",
]

class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id                   = Column(String, primary_key=True, default=_uuid)
    payment_event_id     = Column(String, ForeignKey("payment_events.id"), nullable=False, unique=True)
    root_cause_category  = Column(String, nullable=False)
    confidence           = Column(Float, default=0.0)
    llm_reasoning        = Column(Text, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)

    # Relationships
    payment_event        = relationship("PaymentEvent", back_populates="diagnosis")
    recovery_actions     = relationship("RecoveryAction", back_populates="diagnosis")

    def __repr__(self):
        return f"<Diagnosis {self.id[:8]} category={self.root_cause_category}>"


# ── RecoveryAction ────────────────────────────────────────

ACTION_TYPES = [
    "retry_payment_link",
    "retry_subscription",
    "send_email",
    "escalate_human",
    "stop",
]

ACTION_STATUSES = ["pending", "scheduled", "executed", "failed"]

class RecoveryAction(Base):
    __tablename__ = "recovery_actions"

    id                   = Column(String, primary_key=True, default=_uuid)
    diagnosis_id         = Column(String, ForeignKey("diagnoses.id"), nullable=False)
    action_type          = Column(String, nullable=False)
    status               = Column(String, default="pending")
    payment_link_url     = Column(String, nullable=True)
    scheduled_at         = Column(DateTime, nullable=True)
    executed_at          = Column(DateTime, nullable=True)
    outcome              = Column(Text, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)

    # Relationships
    diagnosis            = relationship("Diagnosis", back_populates="recovery_actions")

    def __repr__(self):
        return f"<RecoveryAction {self.id[:8]} type={self.action_type} status={self.status}>"


# ── PromiseToPay ──────────────────────────────────────────

PROMISE_STATUSES = ["pending", "honored", "broken"]

class PromiseToPay(Base):
    __tablename__ = "promises_to_pay"

    id                   = Column(String, primary_key=True, default=_uuid)
    customer_id          = Column(String, nullable=False, index=True)
    payment_event_id     = Column(String, ForeignKey("payment_events.id"), nullable=False)
    promised_amount      = Column(Integer, nullable=False)            # in paise
    promised_date        = Column(DateTime, nullable=False)
    status               = Column(String, default="pending")
    created_at           = Column(DateTime, default=datetime.utcnow)

    # Relationships
    payment_event        = relationship("PaymentEvent", back_populates="promises")

    def __repr__(self):
        return f"<PromiseToPay {self.id[:8]} status={self.status}>"


# ── AuditLog ──────────────────────────────────────────────

class AuditLog(Base):
    __tablename__ = "audit_log"

    id                   = Column(String, primary_key=True, default=_uuid)
    actor                = Column(String, nullable=False)              # agent, system, human
    action               = Column(String, nullable=False)
    reasoning            = Column(Text, nullable=True)
    related_entity_type  = Column(String, nullable=True)              # PaymentEvent, Diagnosis, etc.
    related_entity_id    = Column(String, nullable=True)
    timestamp            = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AuditLog {self.id[:8]} actor={self.actor} action={self.action}>"
