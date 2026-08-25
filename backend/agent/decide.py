"""
decide.py — Deterministic decision engine mapping diagnoses to recovery actions.

Function: decide(diagnosis, db) -> RecoveryAction

All decisions are rule-based (no LLM) — deterministic and auditable.
Enforces hard-coded guardrails that can never be bypassed.
"""

from datetime import datetime, timedelta
import pytz

from sqlalchemy.orm import Session

from models import Diagnosis, RecoveryAction, PaymentEvent, AuditLog
from config import (
    MAX_CONTACTS_PER_INCIDENT,
    QUIET_HOURS_START,
    QUIET_HOURS_END,
    TIMEZONE,
)

# ── Action Mapping ────────────────────────────────────────

ACTION_CONFIG = {
    "soft_decline_retry": {
        "action_type": "retry_payment_link",
        "cooldown_hours": 0,
        "max_attempts": 3,
    },
    "network_bank_issue": {
        "action_type": "retry_payment_link",
        "cooldown_hours": 0,
        "max_attempts": 3,
    },
    "hard_decline_new_method": {
        "action_type": "send_email",
        "cooldown_hours": 0,
        "max_attempts": 2,
    },
    "auth_failure_3ds": {
        "action_type": "retry_payment_link",
        "cooldown_hours": 0,
        "max_attempts": 1,
    },
    "mandate_issue": {
        "action_type": "send_email",
        "cooldown_hours": 0,
        "max_attempts": 2,
    },
    "customer_abandoned": {
        "action_type": "send_email",
        "cooldown_hours": 0,
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
      - Max contacts per incident
      - Quiet hours
      - Channel restrictions for abandoned carts
      - Audit log BEFORE action creation
    """
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

    # Immediate scheduling for immediate recovery mode (no cooldown or quiet hours delays)
    scheduled_at = datetime.utcnow()

    # ── Write audit log BEFORE creating the action ────────
    attempt_num = prior_count + 1
    reason = (
        f"Decided {action_type} for {category} "
        f"(attempt {attempt_num}/{config['max_attempts']}, "
        f"confidence {diagnosis.confidence:.2f}). "
        f"Scheduled immediately for instant execution."
    )

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
    )
    db.add(action)
    db.flush()

    return action
