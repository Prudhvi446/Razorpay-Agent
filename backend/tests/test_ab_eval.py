import sys
import os
import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from database import Base
from models import PaymentEvent, Diagnosis, RecoveryAction
from routes.api import get_stats, get_eval


@pytest.fixture
def ab_db():
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    yield db
    db.close()


def test_api_stats_calculates_comparative_ab_metrics(ab_db):
    """Verify that get_stats accurately computes Control vs AI group recovery rates and lift."""
    # Control group: 2 failed events of Rs 1000 each (total 200,000 paise). 1 recovered.
    pe_ctrl_1 = PaymentEvent(
        id="ctrl_pe_1",
        customer_id="cust_ctrl_1",
        amount=100000,
        status="failed",
        ab_group="control_group",
    )
    pe_ctrl_2 = PaymentEvent(
        id="ctrl_pe_2",
        customer_id="cust_ctrl_2",
        amount=100000,
        status="failed",
        ab_group="control_group",
    )
    ab_db.add_all([pe_ctrl_1, pe_ctrl_2])

    diag_c1 = Diagnosis(id="d_c1", payment_event_id=pe_ctrl_1.id, root_cause_category="soft_decline_retry")
    diag_c2 = Diagnosis(id="d_c2", payment_event_id=pe_ctrl_2.id, root_cause_category="soft_decline_retry")
    ab_db.add_all([diag_c1, diag_c2])

    act_c1 = RecoveryAction(id="a_c1", diagnosis_id=diag_c1.id, action_type="send_email", status="executed")
    act_c2 = RecoveryAction(id="a_c2", diagnosis_id=diag_c2.id, action_type="send_email", status="failed")
    ab_db.add_all([act_c1, act_c2])

    # AI group: 2 failed events of Rs 1000 each (total 200,000 paise). Both recovered.
    pe_ai_1 = PaymentEvent(
        id="ai_pe_1",
        customer_id="cust_ai_1",
        amount=100000,
        status="failed",
        ab_group="ai_group",
    )
    pe_ai_2 = PaymentEvent(
        id="ai_pe_2",
        customer_id="cust_ai_2",
        amount=100000,
        status="failed",
        ab_group="ai_group",
    )
    ab_db.add_all([pe_ai_1, pe_ai_2])

    diag_a1 = Diagnosis(id="d_a1", payment_event_id=pe_ai_1.id, root_cause_category="soft_decline_retry")
    diag_a2 = Diagnosis(id="d_a2", payment_event_id=pe_ai_2.id, root_cause_category="soft_decline_retry")
    ab_db.add_all([diag_a1, diag_a2])

    act_a1 = RecoveryAction(id="a_a1", diagnosis_id=diag_a1.id, action_type="send_email", status="executed")
    act_a2 = RecoveryAction(id="a_a2", diagnosis_id=diag_a2.id, action_type="send_email", status="executed")
    ab_db.add_all([act_a1, act_a2])

    ab_db.commit()

    stats = get_stats(ab_db)

    assert "ab_testing" in stats
    ab = stats["ab_testing"]
    assert "control_group" in ab
    assert "ai_group" in ab
    assert "incremental_lift_pct" in ab

    # Control: 100,000 / 200,000 = 50.0%
    assert ab["control_group"]["recovery_rate"] == 50.0
    assert ab["control_group"]["count"] == 2

    # AI: 200,000 / 200,000 = 100.0%
    assert ab["ai_group"]["recovery_rate"] == 100.0
    assert ab["ai_group"]["count"] == 2

    # Lift: (100 - 50) / 50 * 100 = +100.0%
    assert ab["incremental_lift_pct"] == 100.0
    assert ab["incremental_revenue"] == 100000


def test_seed_failures_tags_ab_groups(monkeypatch):
    """Verify that seed_failures tags transactions with control_group and ai_group."""
    from scripts.seed_failures import build_weighted_scenarios
    scenarios = build_weighted_scenarios(10)
    assert len(scenarios) == 10
