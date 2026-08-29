"""
decide.py — Deterministic decision engine mapping diagnoses to recovery actions.

Function: decide(diagnosis, db) -> RecoveryAction

All decisions are rule-based (no LLM) — deterministic and auditable.
Enforces hard-coded guardrails that can never be bypassed.
"""

from datetime import datetime, timedelta
import pytz

from sqlalchemy.orm import Session
from sqlalchemy import or_

from models import Diagnosis, RecoveryAction, PaymentEvent, AuditLog
from config import (
    MAX_CONTACTS_PER_INCIDENT,
    MAX_CONTACTS_PER_24H,
    QUIET_HOURS_START,
    QUIET_HOURS_END,
    TIMEZONE,
)

# ── Action Mapping ────────────────────────────────────────

ACTION_CONFIG = {
    "soft_decline_retry": {
        "action_type": "retry_payment_link",
        "cooldown_hours": 4,
        "max_attempts": 3,
    },
    "network_bank_issue": {
        "action_type": "retry_payment_link",
        "cooldown_hours": 4,
        "max_attempts": 3,
    },
    "hard_decline_new_method": {
        "action_type": "send_email",
        "cooldown_hours": 48,
        "max_attempts": 2,
    },
    "auth_failure_3ds": {
        "action_type": "retry_payment_link",
        "cooldown_hours": 0,
        "max_attempts": 1,
    },
    "mandate_issue": {
        "action_type": "send_email",
        "cooldown_hours": 48,
        "max_attempts": 2,
    },
    "customer_abandoned": {
        "action_type": "send_email",   # GUARDRAIL: abandoned = email only
        "cooldown_hours": 24,
        "max_attempts": 2,
    },
    "unrecoverable": {
        "action_type": "escalate_human",
        "cooldown_hours": 0,
        "max_attempts": 0,
    },
}


def count_prior_actions(diagnosis: Diagnosis, db: Session) -> int:
    """Count all recovery actions for the same payment event chain."""
    # Get all diagnoses for the same payment event
    pe_id = diagnosis.payment_event_id
    diag_ids = [d.id for d in db.query(Diagnosis).filter(Diagnosis.payment_event_id == pe_id).all()]
    if not diag_ids:
        return 0
    return db.query(RecoveryAction).filter(RecoveryAction.diagnosis_id.in_(diag_ids)).count()


def get_last_action_time(diagnosis: Diagnosis, db: Session) -> datetime | None:
    """Get the timestamp of the most recent recovery action for this incident."""
    pe_id = diagnosis.payment_event_id
    diag_ids = [d.id for d in db.query(Diagnosis).filter(Diagnosis.payment_event_id == pe_id).all()]
    if not diag_ids:
        return None
    last = (
        db.query(RecoveryAction)
        .filter(RecoveryAction.diagnosis_id.in_(diag_ids))
        .order_by(RecoveryAction.created_at.desc())
        .first()
    )
    return last.created_at if last else None


def is_quiet_hours() -> bool:
    """Check if current time is within quiet hours (9PM–8AM in configured timezone)."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    hour = now.hour
    if QUIET_HOURS_START > QUIET_HOURS_END:
        # Wraps midnight: e.g. 21–8 means 21,22,23,0,1,...,7
        return hour >= QUIET_HOURS_START or hour < QUIET_HOURS_END
    else:
        return QUIET_HOURS_START <= hour < QUIET_HOURS_END


def is_hard_kill_switch_triggered(pe: PaymentEvent | None) -> bool:
    """Check if payment event payload or fields indicate dispute or suspected fraud."""
    if not pe:
        return False
    if getattr(pe, "disputed", False) or getattr(pe, "fraud_suspected", False):
        return True

    payload = pe.raw_payload or {}
    if not isinstance(payload, dict):
        return False

    if payload.get("disputed") is True or payload.get("fraud_suspected") is True:
        return True

    # Check nested entity or notes
    inner_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    entity = inner_payload.get("payment", {}).get("entity", {}) if isinstance(inner_payload.get("payment"), dict) else {}
    if entity.get("disputed") is True or entity.get("fraud_suspected") is True:
        return True

    notes = entity.get("notes", {}) if isinstance(entity, dict) else {}
    if notes.get("disputed") in (True, "true", "True", 1, "1") or notes.get("fraud_suspected") in (True, "true", "True", 1, "1"):
        return True

    top_notes = payload.get("notes", {}) if isinstance(payload.get("notes"), dict) else {}
    if top_notes.get("disputed") in (True, "true", "True", 1, "1") or top_notes.get("fraud_suspected") in (True, "true", "True", 1, "1"):
        return True

    return False


def get_user_contact_count_24h(pe: PaymentEvent, db: Session) -> int:
    """
    Count how many times a user has been contacted in the last 24 hours
    across all payment events.
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    
    filters = []
    if pe.customer_id:
        filters.append(PaymentEvent.customer_id == pe.customer_id)
    if pe.customer_email:
        filters.append(PaymentEvent.customer_email == pe.customer_email)
    if pe.customer_contact:
        filters.append(PaymentEvent.customer_contact == pe.customer_contact)
    
    if not filters:
        filters.append(PaymentEvent.id == pe.id)

    user_pe_ids = [
        r[0] for r in db.query(PaymentEvent.id).filter(or_(*filters)).all()
    ]
    if not user_pe_ids:
        return 0

    diag_ids = [
        r[0] for r in db.query(Diagnosis.id).filter(Diagnosis.payment_event_id.in_(user_pe_ids)).all()
    ]
    if not diag_ids:
        return 0

    action_count = (
        db.query(RecoveryAction)
        .filter(
            RecoveryAction.diagnosis_id.in_(diag_ids),
            RecoveryAction.created_at >= cutoff,
            RecoveryAction.action_type.in_(["send_email", "retry_payment_link", "retry_subscription"]),
            RecoveryAction.status.in_(["executed", "pending", "scheduled"]),
        )
        .count()
    )
    return action_count


def next_allowed_time() -> datetime:
    """Calculate the next timestamp after quiet hours end."""
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    next_day = now + timedelta(days=1) if now.hour >= QUIET_HOURS_START else now
    return next_day.replace(hour=QUIET_HOURS_END, minute=0, second=0, microsecond=0)


def decide(diagnosis: Diagnosis, db: Session) -> RecoveryAction:
    """
    Map a diagnosis to a concrete recovery action.

    Enforces all guardrails:
      - Hard kill switch (disputed / fraud_suspected -> Escalated_to_Human)
      - Frequency capping (Rate_Limit_Exceeded -> max contacts in 24h)
      - Max contacts per incident
      - Quiet hours
      - Channel restrictions for abandoned carts
      - Audit log BEFORE action creation
    """
    pe = db.query(PaymentEvent).filter(PaymentEvent.id == diagnosis.payment_event_id).first()
    if not pe:
        pe = diagnosis.payment_event

    # ── GUARDRAIL 0A: Hard Kill Switch (Disputed / Fraud Suspected) ──
    if pe and is_hard_kill_switch_triggered(pe):
        reason = (
            f"HARD KILL SWITCH: Disputed transaction or fraud suspected for payment event "
            f"{diagnosis.payment_event_id[:8]}. Halting pipeline immediately and escalating to human."
        )
        db.add(AuditLog(
            actor="system",
            action="Escalated_to_Human",
            reasoning=reason,
            related_entity_type="RecoveryAction",
            related_entity_id=None,
        ))
        action = RecoveryAction(
            diagnosis_id=diagnosis.id,
            action_type="escalate_human",
            status="Escalated_to_Human",
            outcome="Escalated_to_Human: Disputed transaction or fraud suspected",
        )
        db.add(action)
        db.flush()
        return action

    # ── GUARDRAIL 0B: Frequency Capping (Rate Limit > 2 contacts / 24h) ──
    if pe:
        contacts_24h = get_user_contact_count_24h(pe, db)
        if contacts_24h >= MAX_CONTACTS_PER_24H:
            reason = (
                f"Rate_Limit_Exceeded: User {pe.customer_id or pe.customer_email or pe.id[:8]} "
                f"has been contacted {contacts_24h} times in the last 24 hours (limit: {MAX_CONTACTS_PER_24H}). "
                f"Aborting recovery action."
            )
            db.add(AuditLog(
                actor="agent",
                action="Rate_Limit_Exceeded",
                reasoning=reason,
                related_entity_type="RecoveryAction",
                related_entity_id=None,
            ))
            action = RecoveryAction(
                diagnosis_id=diagnosis.id,
                action_type="stop",
                status="failed",
                outcome="Rate_Limit_Exceeded",
            )
            db.add(action)
            db.flush()
            return action

    category = diagnosis.root_cause_category
    config = ACTION_CONFIG.get(category, ACTION_CONFIG["unrecoverable"])
    prior_count = count_prior_actions(diagnosis, db)

    # ── GUARDRAIL 1: Max contacts exhausted ───────────────
    if prior_count >= MAX_CONTACTS_PER_INCIDENT:
        reason = (
            f"GUARDRAIL: Max contacts reached ({prior_count}/{MAX_CONTACTS_PER_INCIDENT}) "
            f"for payment event {diagnosis.payment_event_id[:8]}. "
            f"Escalating to human — no further automated contact."
        )
        db.add(AuditLog(
            actor="agent",
            action="decision_made",
            reasoning=reason,
            related_entity_type="RecoveryAction",
            related_entity_id=None,
        ))

        action = RecoveryAction(
            diagnosis_id=diagnosis.id,
            action_type="escalate_human",
            status="pending",
            outcome=reason,
        )
        db.add(action)
        db.flush()
        return action

    # ── GUARDRAIL 2: Max attempts for this action type ────
    if config["max_attempts"] > 0 and prior_count >= config["max_attempts"]:
        reason = (
            f"Max attempts reached for {config['action_type']} "
            f"({prior_count}/{config['max_attempts']}). "
            f"Escalating to human."
        )
        db.add(AuditLog(
            actor="agent",
            action="decision_made",
            reasoning=reason,
            related_entity_type="RecoveryAction",
            related_entity_id=None,
        ))

        action = RecoveryAction(
            diagnosis_id=diagnosis.id,
            action_type="escalate_human",
            status="pending",
            outcome=reason,
        )
        db.add(action)
        db.flush()
        return action

    # ── GUARDRAIL 3: Unrecoverable = immediate escalation ─
    if category == "unrecoverable" or config["max_attempts"] == 0:
        reason = (
            f"Category '{category}' is unrecoverable. "
            f"No automated recovery possible. Escalating to human."
        )
        db.add(AuditLog(
            actor="agent",
            action="decision_made",
            reasoning=reason,
            related_entity_type="RecoveryAction",
            related_entity_id=None,
        ))

        action = RecoveryAction(
            diagnosis_id=diagnosis.id,
            action_type="escalate_human",
            status="pending",
            outcome=reason,
        )
        db.add(action)
        db.flush()
        return action

    # ── Determine scheduling ──────────────────────────────
    action_type = config["action_type"]
    cooldown_hours = config["cooldown_hours"]

    # GUARDRAIL 4: customer_abandoned → only email
    if category == "customer_abandoned" and action_type != "send_email":
        action_type = "send_email"

    # Calculate scheduled time with cooldown
    last_action_time = get_last_action_time(diagnosis, db)
    if last_action_time and cooldown_hours > 0:
        scheduled_at = last_action_time + timedelta(hours=cooldown_hours)
    else:
        scheduled_at = datetime.utcnow()

    # SMART RETRY: Push soft declines to 10 AM if currently past 2 PM (optimal window)
    if category == "soft_decline_retry":
        tz = pytz.timezone(TIMEZONE)
        now_tz = datetime.now(tz)
        if now_tz.hour >= 14:
            next_day = now_tz + timedelta(days=1)
            optimal_time = next_day.replace(hour=10, minute=0, second=0, microsecond=0).replace(tzinfo=None)
            if scheduled_at < optimal_time:
                scheduled_at = optimal_time

    # GUARDRAIL 5: Quiet hours enforcement
    if is_quiet_hours():
        next_ok = next_allowed_time()
        if scheduled_at < next_ok.replace(tzinfo=None):
            scheduled_at = next_ok.replace(tzinfo=None)

    # CART SAVER ENGINE: Apply 5% discount tag if abandoned cart > ₹2,000
    discount_applied = False
    pe = diagnosis.payment_event
    if category == "customer_abandoned" and pe and pe.amount >= 200000:
        discount_applied = True

    # ── Write audit log BEFORE creating the action ────────
    attempt_num = prior_count + 1
    reason = (
        f"Decided {action_type} for {category} "
        f"(attempt {attempt_num}/{config['max_attempts']}, "
        f"confidence {diagnosis.confidence:.2f}). "
        f"Scheduled for {scheduled_at.strftime('%Y-%m-%d %H:%M UTC')} "
        f"after {cooldown_hours}h cooldown."
    )
    if discount_applied:
        reason += " [CART SAVER: 5% Discount Applied]"

    db.add(AuditLog(
        actor="agent",
        action="decision_made",
        reasoning=reason,
        related_entity_type="RecoveryAction",
        related_entity_id=None,
    ))

    # ── Create the recovery action ────────────────────────
    action = RecoveryAction(
        diagnosis_id=diagnosis.id,
        action_type=action_type,
        status="pending",
        scheduled_at=scheduled_at,
        discount_applied=discount_applied,
    )
    db.add(action)
    db.flush()

    return action
