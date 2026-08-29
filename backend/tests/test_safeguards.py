import sys
import os
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add backend directory to sys.path
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from database import Base
from models import PaymentEvent, Diagnosis, RecoveryAction, AuditLog
from agent.decide import decide, is_hard_kill_switch_triggered, get_user_contact_count_24h
from agent.diagnose import diagnose
from agent.execute import execute


@pytest.fixture
def test_db():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()


def test_hard_kill_switch_disputed_flag(test_db):
    """Test that disputed=True immediately triggers the hard kill switch in decide."""
    pe = PaymentEvent(
        id="pe_dispute_1",
        customer_id="cust_test_1",
        customer_email="user1@example.com",
        amount=50000,
        status="failed",
        error_code="BAD_REQUEST_ERROR",
        error_reason="card_declined",
        disputed=True,
        raw_payload={"disputed": True},
    )
    test_db.add(pe)
    
    diag = Diagnosis(
        id="diag_1",
        payment_event_id=pe.id,
        root_cause_category="hard_decline_new_method",
        confidence=0.9,
    )
    test_db.add(diag)
    test_db.commit()

    action = decide(diag, test_db)

    assert action.action_type == "escalate_human"
    assert action.status == "Escalated_to_Human"
    assert "Disputed" in action.outcome or "disputed" in action.outcome.lower()

    # Verify audit log
    audit = test_db.query(AuditLog).filter(AuditLog.action == "Escalated_to_Human").first()
    assert audit is not None
    assert "HARD KILL SWITCH" in audit.reasoning


def test_hard_kill_switch_fraud_suspected_payload(test_db):
    """Test that fraud_suspected in raw_payload triggers the kill switch."""
    pe = PaymentEvent(
        id="pe_fraud_1",
        customer_id="cust_test_2",
        customer_email="user2@example.com",
        amount=100000,
        status="failed",
        error_code="GATEWAY_ERROR",
        raw_payload={"payload": {"payment": {"entity": {"notes": {"fraud_suspected": True}}}}},
    )
    test_db.add(pe)

    diag = Diagnosis(
        id="diag_2",
        payment_event_id=pe.id,
        root_cause_category="network_bank_issue",
        confidence=0.8,
    )
    test_db.add(diag)
    test_db.commit()

    action = decide(diag, test_db)

    assert action.action_type == "escalate_human"
    assert action.status == "Escalated_to_Human"
    assert "Disputed transaction or fraud suspected" in action.outcome


@pytest.mark.asyncio
async def test_kill_switch_bypasses_llm_in_diagnose(test_db):
    """Test that diagnose() completely bypasses LLM calls when disputed/fraud is present."""
    pe = PaymentEvent(
        id="pe_kill_switch_diag",
        customer_id="cust_test_3",
        amount=75000,
        status="failed",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        disputed=True,
    )
    test_db.add(pe)
    test_db.commit()

    diag = await diagnose(pe, test_db)

    assert diag.root_cause_category == "unrecoverable"
    assert diag.confidence == 1.0
    assert "HARD KILL SWITCH" in diag.llm_reasoning


def test_frequency_capping_rate_limit_exceeded(test_db):
    """Test that more than 2 contacts in 24 hours aborts recovery with Rate_Limit_Exceeded."""
    pe1 = PaymentEvent(
        id="pe_hist_1",
        customer_id="cust_freq_test",
        customer_email="freq@example.com",
        amount=20000,
        status="failed",
        contact_count=1,
        created_at=datetime.utcnow() - timedelta(hours=5),
    )
    pe2 = PaymentEvent(
        id="pe_hist_2",
        customer_id="cust_freq_test",
        customer_email="freq@example.com",
        amount=30000,
        status="failed",
        contact_count=1,
        created_at=datetime.utcnow() - timedelta(hours=3),
    )
    test_db.add_all([pe1, pe2])

    diag1 = Diagnosis(id="d1", payment_event_id=pe1.id, root_cause_category="soft_decline_retry")
    diag2 = Diagnosis(id="d2", payment_event_id=pe2.id, root_cause_category="soft_decline_retry")
    test_db.add_all([diag1, diag2])

    act1 = RecoveryAction(
        id="act1",
        diagnosis_id=diag1.id,
        action_type="send_email",
        status="executed",
        created_at=datetime.utcnow() - timedelta(hours=5),
    )
    act2 = RecoveryAction(
        id="act2",
        diagnosis_id=diag2.id,
        action_type="retry_payment_link",
        status="executed",
        created_at=datetime.utcnow() - timedelta(hours=3),
    )
    test_db.add_all([act1, act2])
    test_db.commit()

    # Now a third event arrives for the same user within 24h
    pe3 = PaymentEvent(
        id="pe_new_3",
        customer_id="cust_freq_test",
        customer_email="freq@example.com",
        amount=40000,
        status="failed",
        created_at=datetime.utcnow(),
    )
    test_db.add(pe3)
    diag3 = Diagnosis(id="d3", payment_event_id=pe3.id, root_cause_category="soft_decline_retry")
    test_db.add(diag3)
    test_db.commit()

    action = decide(diag3, test_db)

    assert action.action_type == "stop"
    assert action.status == "failed"
    assert action.outcome == "Rate_Limit_Exceeded"

    # Verify audit log
    audit = test_db.query(AuditLog).filter(AuditLog.action == "Rate_Limit_Exceeded").first()
    assert audit is not None
    assert "Rate_Limit_Exceeded" in audit.reasoning


def test_contact_count_incremented_on_execution(test_db):
    """Test that executing a contact recovery action increments contact_count and updates last_contacted_at."""
    pe = PaymentEvent(
        id="pe_exec_test",
        customer_id="cust_exec",
        customer_email="exec@example.com",
        amount=50000,
        status="failed",
        contact_count=0,
        last_contacted_at=None,
    )
    test_db.add(pe)
    diag = Diagnosis(id="d_exec", payment_event_id=pe.id, root_cause_category="soft_decline_retry")
    test_db.add(diag)

    action = RecoveryAction(
        id="act_exec",
        diagnosis_id=diag.id,
        action_type="retry_payment_link",
        status="pending",
    )
    test_db.add(action)
    test_db.commit()

    execute(action, test_db)

    assert action.status == "executed"
    assert pe.contact_count == 1
    assert pe.last_contacted_at is not None
