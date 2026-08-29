import sys
import os
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

import database
from database import Base
from models import PaymentEvent, Diagnosis, RecoveryAction, PromiseToPay, AuditLog
from agent.pipeline import recovery_graph, RecoveryGraphState
from agent.promise_tracker import record_promise


@pytest.fixture
def graph_db(monkeypatch):
    test_engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=test_engine)
    TestSession = sessionmaker(bind=test_engine)

    # Monkeypatch SessionLocal in pipeline and database
    monkeypatch.setattr(database, "SessionLocal", TestSession)
    from agent import pipeline
    monkeypatch.setattr(pipeline, "SessionLocal", TestSession)

    db = TestSession()
    yield db
    db.close()


@pytest.mark.asyncio
async def test_langgraph_nodes_flow(graph_db):
    """Test that an event executes through Diagnose -> Draft_Message -> Execute -> Wait in LangGraph."""
    pe = PaymentEvent(
        id="pe_graph_test_1",
        customer_id="cust_graph_1",
        customer_email="graph1@example.com",
        amount=250000,
        status="failed",
        error_code="GATEWAY_ERROR",
        error_reason="gateway_timeout",
        error_description="Gateway timed out",
    )
    graph_db.add(pe)
    graph_db.commit()

    initial_state: RecoveryGraphState = {
        "payment_event_id": pe.id,
        "stage": "diagnose",
        "day_step": 1,
        "root_cause_category": None,
        "confidence": None,
        "is_kill_switch": False,
        "drafted_message": None,
        "action_type": None,
        "recovery_action_id": None,
        "action_status": None,
        "paused_for_promise": False,
        "promised_date": None,
        "discount_applied": False,
        "error": None,
        "logs": [],
    }

    final_state = await recovery_graph.ainvoke(initial_state)

    assert final_state["root_cause_category"] == "network_bank_issue"
    assert final_state["drafted_message"] is not None
    assert final_state["action_type"] in ("send_email", "retry_payment_link")
    assert final_state["is_kill_switch"] is False
    assert len(final_state["logs"]) >= 3


@pytest.mark.asyncio
async def test_promise_tracker_pauses_graph_execution(graph_db):
    """Test that an active promise to pay causes the graph to pause at the Wait node."""
    pe = PaymentEvent(
        id="pe_promise_pause",
        customer_id="cust_pause_test",
        customer_email="pause@example.com",
        amount=50000,
        status="failed",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
    )
    graph_db.add(pe)
    graph_db.commit()

    # Record a promise for 3 days in future
    promised_date = datetime.utcnow() + timedelta(days=3)
    record_promise(
        customer_id=pe.customer_id,
        payment_event_id=pe.id,
        promised_amount=pe.amount,
        promised_date=promised_date,
        db=graph_db,
    )
    graph_db.commit()

    initial_state: RecoveryGraphState = {
        "payment_event_id": pe.id,
        "stage": "diagnose",
        "day_step": 1,
        "root_cause_category": None,
        "confidence": None,
        "is_kill_switch": False,
        "drafted_message": None,
        "action_type": None,
        "recovery_action_id": None,
        "action_status": None,
        "paused_for_promise": False,
        "promised_date": None,
        "discount_applied": False,
        "error": None,
        "logs": [],
    }

    final_state = await recovery_graph.ainvoke(initial_state)

    assert final_state["paused_for_promise"] is True
    assert final_state["stage"] == "paused"
    assert any("PAUSED" in log for log in final_state["logs"])

    # Verify audit log
    audit = graph_db.query(AuditLog).filter(AuditLog.action == "recovery_paused").first()
    assert audit is not None


@pytest.mark.asyncio
async def test_multi_day_escalation_advances_stage(graph_db):
    """Test that in the absence of a promise, Wait node transitions Day 1 -> Day 3 discount stage."""
    pe = PaymentEvent(
        id="pe_multi_day",
        customer_id="cust_multi",
        amount=100000,
        status="failed",
        error_code="BAD_REQUEST_ERROR",
        error_reason="insufficient_funds",
        escalation_stage=1,
    )
    graph_db.add(pe)
    graph_db.commit()

    initial_state: RecoveryGraphState = {
        "payment_event_id": pe.id,
        "stage": "diagnose",
        "day_step": 1,
        "root_cause_category": None,
        "confidence": None,
        "is_kill_switch": False,
        "drafted_message": None,
        "action_type": None,
        "recovery_action_id": None,
        "action_status": None,
        "paused_for_promise": False,
        "promised_date": None,
        "discount_applied": False,
        "error": None,
        "logs": [],
    }

    final_state = await recovery_graph.ainvoke(initial_state)

    assert final_state["day_step"] == 2
    assert final_state["discount_applied"] is True
    graph_db.refresh(pe)
    assert pe.escalation_stage == 2


@pytest.mark.asyncio
async def test_kill_switch_routes_to_escalate_human(graph_db):
    """Test that disputed=True routes directly to escalation and halts pipeline."""
    pe = PaymentEvent(
        id="pe_graph_kill",
        customer_id="cust_kill",
        amount=80000,
        status="failed",
        disputed=True,
    )
    graph_db.add(pe)
    graph_db.commit()

    initial_state: RecoveryGraphState = {
        "payment_event_id": pe.id,
        "stage": "diagnose",
        "day_step": 1,
        "root_cause_category": None,
        "confidence": None,
        "is_kill_switch": False,
        "drafted_message": None,
        "action_type": None,
        "recovery_action_id": None,
        "action_status": None,
        "paused_for_promise": False,
        "promised_date": None,
        "discount_applied": False,
        "error": None,
        "logs": [],
    }

    final_state = await recovery_graph.ainvoke(initial_state)

    assert final_state["is_kill_switch"] is True
    assert final_state["action_type"] == "escalate_human"
    assert final_state["action_status"] == "Escalated_to_Human"
    assert final_state["drafted_message"] is None  # Bypassed drafting
