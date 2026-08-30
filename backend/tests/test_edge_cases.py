import sys
import os
import pytest
from datetime import datetime, timedelta
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

# Add backend directory to sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import database
from database import Base, get_db
from models import (
    PaymentEvent, Diagnosis, RecoveryAction, AuditLog,
    PromiseToPay, ProcessedWebhook, CustomerProfile, TransactionStatus
)
from agent.decide import decide, check_quiet_hours, get_morning_window
from agent.execute import execute, record_customer_opt_out, is_customer_opted_out
from agent.promise_tracker import record_promise, check_expired_promises
from main import app


from sqlalchemy.pool import StaticPool

@pytest.fixture
def edge_db(monkeypatch):
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False
    )
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine)

    # Monkeypatch SessionLocal across all modules
    monkeypatch.setattr(database, "SessionLocal", TestSession)
    from routes import webhooks
    monkeypatch.setattr(webhooks, "SessionLocal", TestSession)
    from agent import pipeline
    monkeypatch.setattr(pipeline, "SessionLocal", TestSession)

    def override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    db = TestSession()
    yield db
    db.close()
    app.dependency_overrides.clear()


@pytest.fixture
def client(edge_db):
    return TestClient(app)


# ── Test 1: Webhook Idempotency ───────────────────────────

def test_duplicate_webhook_idempotency(client, edge_db):
    """Verifies identical event_id does not execute duplicate outreach or save duplicate events."""
    event_id = "evt_idempotent_test_999"
    payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_idemp_001",
                    "order_id": "order_idemp_001",
                    "amount": 100000,
                    "currency": "INR",
                    "status": "failed",
                    "customer_id": "cust_idemp_001",
                    "email": "idemp@example.com",
                    "contact": "+919999999999",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_reason": "insufficient_funds",
                }
            }
        }
    }

    headers = {"X-Razorpay-Event-Id": event_id}

    # First request: should be processed normally
    resp1 = client.post("/webhooks/razorpay", json=payload, headers=headers)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["status"] == "ok"
    assert "payment_event_id" in data1

    # Second request with IDENTICAL event_id: must return duplicate_event_ignored
    resp2 = client.post("/webhooks/razorpay", json=payload, headers=headers)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "duplicate_event_ignored"

    # Confirm only one PaymentEvent was saved in database
    events = edge_db.query(PaymentEvent).filter(PaymentEvent.webhook_event_id == event_id).all()
    assert len(events) == 1
    assert events[0].razorpay_payment_id == "pay_idemp_001"


# ── Test 2: Race Condition (Already Paid) ──────────────────

def test_race_condition_already_paid(edge_db, client):
    """Verifies recovery halts when payment status is already PAID."""
    pe = PaymentEvent(
        id="pe_race_paid_1",
        razorpay_payment_id="pay_race_1",
        customer_id="cust_race_1",
        customer_email="race@example.com",
        amount=50000,
        status="paid",  # Race condition: capture webhook arrived before recovery execution
        lifecycle_status="PAID",
    )
    edge_db.add(pe)
    diag = Diagnosis(
        id="diag_race_1",
        payment_event_id=pe.id,
        root_cause_category="soft_decline_retry",
        confidence=0.9,
    )
    edge_db.add(diag)
    edge_db.commit()

    # Pre-execution check in decide()
    action = decide(diag, edge_db)
    assert action.status == "ALREADY_RESOLVED"
    assert action.action_type == "stop"
    assert "ALREADY_RESOLVED" in action.outcome

    # Verify audit log recorded
    audit = edge_db.query(AuditLog).filter(
        AuditLog.action == "ALREADY_RESOLVED",
        AuditLog.related_entity_id == pe.id
    ).first()
    assert audit is not None

    # Pre-execution check in execute()
    execute(action, edge_db)
    assert action.status == "ALREADY_RESOLVED"

    # Also test incoming failure webhook for already PAID transaction
    failure_payload = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_race_1",
                    "amount": 50000,
                    "customer_id": "cust_race_1",
                    "status": "failed",
                }
            }
        }
    }
    resp = client.post("/webhooks/razorpay", json=failure_payload, headers={"X-Razorpay-Event-Id": "evt_race_fail"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ALREADY_RESOLVED"


# ── Test 3: Defensive Ingestion for Malformed Payloads ─────

def test_malformed_webhook_payload(client, edge_db):
    """Verifies missing payment_id or negative amount returns 422/400 without crashing."""
    # Case A: Missing payment_id
    payload_missing_id = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "amount": 50000,
                    "customer_id": "cust_malformed_1",
                    "email": "cust@example.com",
                }
            }
        }
    }
    resp_missing_id = client.post("/webhooks/razorpay", json=payload_missing_id)
    assert resp_missing_id.status_code in (400, 422)

    # Case B: Negative amount
    payload_negative_amt = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_neg_1",
                    "amount": -5000,
                    "customer_id": "cust_malformed_2",
                }
            }
        }
    }
    resp_neg_amt = client.post("/webhooks/razorpay", json=payload_negative_amt)
    assert resp_neg_amt.status_code in (400, 422)

    # Case C: Zero amount
    payload_zero_amt = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_zero_1",
                    "amount": 0,
                    "customer_id": "cust_malformed_3",
                }
            }
        }
    }
    resp_zero_amt = client.post("/webhooks/razorpay", json=payload_zero_amt)
    assert resp_zero_amt.status_code in (400, 422)

    # Case D: Missing customer identifier
    payload_no_customer = {
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_no_cust_1",
                    "amount": 50000,
                }
            }
        }
    }
    resp_no_cust = client.post("/webhooks/razorpay", json=payload_no_customer)
    assert resp_no_cust.status_code in (400, 422)

    # Verify audit log was recorded for MALFORMED_PAYLOAD without 500 server crashes
    malformed_logs = edge_db.query(AuditLog).filter(AuditLog.action == "MALFORMED_PAYLOAD").all()
    assert len(malformed_logs) >= 3


# ── Test 4: Promise Date Validation & Breach ───────────────

def test_promise_date_validation_and_breach(edge_db):
    """Verifies past/invalid dates and simulated expired promises transition to PROMISE_BREACHED."""
    pe = PaymentEvent(
        id="pe_promise_test_1",
        customer_id="cust_prom_1",
        amount=150000,
        status="failed",
    )
    edge_db.add(pe)
    diag = Diagnosis(id="diag_prom_1", payment_event_id=pe.id, root_cause_category="soft_decline_retry")
    edge_db.add(diag)
    edge_db.commit()

    # Case 1: Past date fallback -> INVALID_PROMISE_DATE
    past_date = datetime.utcnow() - timedelta(days=2)
    p_past = record_promise(
        customer_id=pe.customer_id,
        payment_event_id=pe.id,
        promised_amount=pe.amount,
        promised_date=past_date,
        db=edge_db,
    )
    assert p_past.status == "INVALID_PROMISE_DATE"

    # Human review triggered
    human_action = edge_db.query(RecoveryAction).filter(
        RecoveryAction.diagnosis_id == diag.id,
        RecoveryAction.status == "Escalated_to_Human"
    ).first()
    assert human_action is not None
    assert "INVALID_PROMISE_DATE" in human_action.outcome

    # Case 2: Date > 30 days in future -> INVALID_PROMISE_DATE
    far_future_date = datetime.utcnow() + timedelta(days=40)
    p_far = record_promise(
        customer_id=pe.customer_id,
        payment_event_id=pe.id,
        promised_amount=pe.amount,
        promised_date=far_future_date,
        db=edge_db,
    )
    assert p_far.status == "INVALID_PROMISE_DATE"

    # Case 3: Valid promise expired -> PROMISE_BREACHED
    p_expired = PromiseToPay(
        id="prom_exp_1",
        customer_id=pe.customer_id,
        payment_event_id=pe.id,
        promised_amount=pe.amount,
        promised_date=datetime.utcnow() - timedelta(hours=2),
        status="pending",
    )
    edge_db.add(p_expired)
    edge_db.commit()

    # Run automated expired check
    transitioned = check_expired_promises(edge_db)
    assert transitioned >= 1
    assert p_expired.status == "PROMISE_BREACHED"

    # Verify escalation to ESCALATE_TO_ACCOUNT_MANAGER
    escalated_action = edge_db.query(RecoveryAction).filter(
        RecoveryAction.diagnosis_id == diag.id,
        RecoveryAction.action_type == "ESCALATE_TO_ACCOUNT_MANAGER"
    ).first()
    assert escalated_action is not None
    assert "PROMISE_BREACHED" in escalated_action.outcome

    # Audit log check
    audit = edge_db.query(AuditLog).filter(AuditLog.action == "PROMISE_BREACHED").first()
    assert audit is not None


# ── Test 5: User Opt-Out Kill Switch ───────────────────────

def test_opt_out_kill_switch(edge_db):
    """Verifies 'STOP' payload halts recovery and sets opted_out = True."""
    pe = PaymentEvent(
        id="pe_opt_test",
        customer_id="cust_opt_999",
        customer_email="optout@example.com",
        amount=75000,
        status="failed",
        opted_out=False,
    )
    edge_db.add(pe)
    diag = Diagnosis(id="diag_opt_test", payment_event_id=pe.id, root_cause_category="soft_decline_retry")
    edge_db.add(diag)
    action = RecoveryAction(
        id="act_opt_test",
        diagnosis_id=diag.id,
        action_type="send_email",
        status="pending",
    )
    edge_db.add(action)
    edge_db.commit()

    # Customer replies with STOP
    opted = record_customer_opt_out("cust_opt_999", "STOP", edge_db)
    assert opted is True

    # Verify customer profile updated
    profile = edge_db.query(CustomerProfile).filter(CustomerProfile.customer_id == "cust_opt_999").first()
    assert profile is not None
    assert profile.opted_out is True

    # Verify PaymentEvent has opted_out = True and lifecycle_status = OPTED_OUT
    edge_db.refresh(pe)
    assert pe.opted_out is True
    assert pe.lifecycle_status == TransactionStatus.OPTED_OUT

    # Verify active recovery action terminated
    edge_db.refresh(action)
    assert action.status == "stopped"
    assert "OPT_OUT_RECORDED" in action.outcome

    # Verify decide() immediately stops any new recovery for this customer
    new_action = decide(diag, edge_db)
    assert new_action.action_type == "stop"
    assert new_action.status == "stopped"
    assert "OPTED_OUT" in new_action.outcome


# ── Test 6: Timezone Quiet Hours Queuing ──────────────────

def test_quiet_hours_queuing(edge_db):
    """Verifies late-night executions are delayed to the morning window."""
    kolkata_tz = pytz.timezone("Asia/Kolkata")

    # 1. Test 23:00 IST (11:00 PM) is within quiet hours
    late_night_ist = kolkata_tz.localize(datetime(2026, 8, 29, 23, 0, 0))
    assert check_quiet_hours(late_night_ist, timezone="Asia/Kolkata") is True

    # 2. Test 14:00 IST (2:00 PM) is NOT in quiet hours
    afternoon_ist = kolkata_tz.localize(datetime(2026, 8, 29, 14, 0, 0))
    assert check_quiet_hours(afternoon_ist, timezone="Asia/Kolkata") is False

    # 3. Test morning window calculation: next morning at 08:30 AM IST
    morning_window_utc = get_morning_window(late_night_ist, timezone="Asia/Kolkata")
    morning_window_ist = pytz.utc.localize(morning_window_utc).astimezone(kolkata_tz)
    assert morning_window_ist.hour == 8
    assert morning_window_ist.minute == 30
    assert morning_window_ist.day == 30  # Next day

    # 4. Test decide() sets action state to QUEUED_FOR_MORNING_WINDOW when dispatch time is late night
    pe = PaymentEvent(
        id="pe_quiet_test",
        customer_id="cust_quiet_1",
        amount=80000,
        status="failed",
    )
    edge_db.add(pe)
    diag = Diagnosis(
        id="diag_quiet_test",
        payment_event_id=pe.id,
        root_cause_category="hard_decline_new_method",
        confidence=0.85,
    )
    edge_db.add(diag)
    edge_db.commit()

    from unittest.mock import patch

    # Force quiet hours scheduled time by running decide() with check_quiet_hours mocked to True
    with patch("agent.decide.check_quiet_hours", return_value=True):
        action = decide(diag, edge_db)
        assert action.status == "QUEUED_FOR_MORNING_WINDOW"
        assert action.scheduled_at > datetime.utcnow()

    # Verify execute() does NOT send real-time outreach while queued for morning window
    execute(action, edge_db)
    assert action.status == "QUEUED_FOR_MORNING_WINDOW"
