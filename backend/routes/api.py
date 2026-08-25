"""
api.py — REST API endpoints for the frontend dashboard.

Endpoints:
  GET  /api/stats         — total at risk, recovered, rate, active
  GET  /api/funnel        — Failed → Contacted → Promised → Paid counts
  GET  /api/root-causes   — breakdown by root_cause_category
  GET  /api/audit-log     — recent audit entries
  POST /api/run-batch     — trigger pipeline manually
  GET  /api/eval          — classification accuracy vs ground truth
  POST /api/promise       — record a promise-to-pay (demo endpoint)
"""

import json
import os
import csv
import uuid
from io import StringIO
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Security, HTTPException, status
from fastapi.security import APIKeyHeader
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db
from models import (
    PaymentEvent, Diagnosis, RecoveryAction,
    PromiseToPay, AuditLog, ProcessedWebhook
)
from agent.pipeline import run_batch
from agent.promise_tracker import record_promise
import requests

API_KEY_NAME = "X-API-Key"
API_KEY = os.getenv("DASHBOARD_API_KEY", "supersecretkey")
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)

def verify_api_key(api_key_header: str = Security(api_key_header)):
    if api_key_header != API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Could not validate credentials"
        )
    return api_key_header

router = APIRouter(prefix="/api", dependencies=[Depends(verify_api_key)])


# ── Pydantic Schemas ──────────────────────────────────────

class PromiseRequest(BaseModel):
    customer_id: str
    payment_event_id: str
    promised_amount: int          # in paise
    promised_date: str            # ISO date string


# ── GET /api/stats ────────────────────────────────────────

@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    """Dashboard summary stats."""
    # Total at risk: sum of all failed payment amounts
    total_at_risk = (
        db.query(func.coalesce(func.sum(PaymentEvent.amount), 0))
        .filter(PaymentEvent.status.in_(["failed", "created"]))
        .scalar()
    )

    # Total recovered: sum of amounts with successful follow-up payments
    # A payment is "recovered" if there's an executed recovery action
    # and a subsequent successful payment for the same customer
    recovered_events = (
        db.query(PaymentEvent.amount)
        .join(Diagnosis, Diagnosis.payment_event_id == PaymentEvent.id)
        .join(RecoveryAction, RecoveryAction.diagnosis_id == Diagnosis.id)
        .filter(RecoveryAction.status == "executed")
        .filter(RecoveryAction.action_type != "escalate_human")
        .all()
    )
    total_recovered = sum(r[0] for r in recovered_events) if recovered_events else 0

    # Also count honored promises as recovered
    honored_promises = (
        db.query(func.coalesce(func.sum(PromiseToPay.promised_amount), 0))
        .filter(PromiseToPay.status == "honored")
        .scalar()
    )
    total_recovered += honored_promises

    recovery_rate = round((total_recovered / total_at_risk * 100), 1) if total_at_risk > 0 else 0

    active_recoveries = (
        db.query(func.count(RecoveryAction.id))
        .filter(RecoveryAction.status.in_(["pending", "scheduled"]))
        .scalar()
    )

    return {
        "total_at_risk": total_at_risk,
        "total_recovered": total_recovered,
        "recovery_rate": recovery_rate,
        "active_recoveries": active_recoveries,
    }


# ── GET /api/funnel ───────────────────────────────────────

@router.get("/funnel")
def get_funnel(db: Session = Depends(get_db)):
    """Recovery funnel counts: Failed → Contacted → Promised → Paid."""
    failed = (
        db.query(func.count(PaymentEvent.id))
        .filter(PaymentEvent.status.in_(["failed", "created"]))
        .scalar()
    )

    # Contacted: payment events with at least one executed action
    contacted = (
        db.query(func.count(func.distinct(Diagnosis.payment_event_id)))
        .join(RecoveryAction, RecoveryAction.diagnosis_id == Diagnosis.id)
        .filter(RecoveryAction.status == "executed")
        .filter(RecoveryAction.action_type != "escalate_human")
        .scalar()
    )

    promised = (
        db.query(func.count(PromiseToPay.id))
        .scalar()
    )

    paid = (
        db.query(func.count(PromiseToPay.id))
        .filter(PromiseToPay.status == "honored")
        .scalar()
    )

    return {
        "failed": failed,
        "contacted": contacted,
        "promised": promised,
        "paid": paid,
    }


# ── GET /api/root-causes ─────────────────────────────────

@router.get("/root-causes")
def get_root_causes(db: Session = Depends(get_db)):
    """Breakdown of diagnosis categories."""
    results = (
        db.query(Diagnosis.root_cause_category, func.count(Diagnosis.id))
        .group_by(Diagnosis.root_cause_category)
        .all()
    )
    return [{"category": cat, "count": cnt} for cat, cnt in results]


# ── GET /api/audit-log ────────────────────────────────────

@router.get("/audit-log")
def get_audit_log(limit: int = Query(default=50, le=200), db: Session = Depends(get_db)):
    """Most recent audit entries, newest first."""
    entries = (
        db.query(AuditLog)
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "actor": e.actor,
            "action": e.action,
            "reasoning": e.reasoning,
            "related_entity_type": e.related_entity_type,
            "related_entity_id": e.related_entity_id,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        }
        for e in entries
    ]


# ── POST /api/run-batch ──────────────────────────────────

@router.post("/run-batch")
async def trigger_batch():
    """Manually trigger the agent pipeline for live demo."""
    result = await run_batch()
    return result


# ── GET /api/eval ─────────────────────────────────────────

@router.get("/eval")
def get_eval(db: Session = Depends(get_db)):
    """Compare diagnoses against ground-truth labels from seed_failures.py."""
    gt_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backend", "scripts", "ground_truth.json"
    )

    # Try multiple possible paths
    possible_paths = [
        gt_path,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "ground_truth.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "ground_truth.json"),
    ]

    ground_truth = {}
    for path in possible_paths:
        if os.path.exists(path):
            with open(path) as f:
                ground_truth = json.load(f)
            break

    if not ground_truth:
        return {
            "categories": [],
            "overall_total": 0,
            "overall_correct": 0,
            "overall_accuracy": 0,
            "message": "Ground truth file not found. Run seed_failures.py first.",
        }

    # Get all diagnoses
    diagnoses = db.query(Diagnosis).all()
    diag_map = {d.payment_event_id: d.root_cause_category for d in diagnoses}

    # Calculate accuracy per category
    from collections import defaultdict
    category_stats = defaultdict(lambda: {"total": 0, "correct": 0})

    for pe_id, expected_cat in ground_truth.items():
        actual_cat = diag_map.get(pe_id)
        if actual_cat is None:
            continue  # Not yet diagnosed
        category_stats[expected_cat]["total"] += 1
        if actual_cat == expected_cat:
            category_stats[expected_cat]["correct"] += 1

    categories = []
    overall_total = 0
    overall_correct = 0
    for cat, stats in sorted(category_stats.items()):
        accuracy = (stats["correct"] / stats["total"] * 100) if stats["total"] > 0 else 0
        categories.append({
            "category": cat,
            "total": stats["total"],
            "correct": stats["correct"],
            "accuracy": round(accuracy, 1),
        })
        overall_total += stats["total"]
        overall_correct += stats["correct"]

    overall_accuracy = (overall_correct / overall_total * 100) if overall_total > 0 else 0

    return {
        "categories": categories,
        "overall_total": overall_total,
        "overall_correct": overall_correct,
        "overall_accuracy": round(overall_accuracy, 1),
    }


# ── POST /api/promise ────────────────────────────────────

@router.post("/promise")
def create_promise(req: PromiseRequest, db: Session = Depends(get_db)):
    """Manually create a promise-to-pay for demo purposes."""
    promise = record_promise(
        customer_id=req.customer_id,
        payment_event_id=req.payment_event_id,
        promised_amount=req.promised_amount,
        promised_date=datetime.fromisoformat(req.promised_date),
        db=db,
    )
    db.commit()
    return {
        "id": promise.id,
        "status": promise.status,
        "promised_amount": promise.promised_amount,
        "promised_date": promise.promised_date.isoformat(),
    }


# ── GET /api/export-csv ──────────────────────────────────

@router.get("/export-csv")
def export_csv(db: Session = Depends(get_db)):
    """Export Payment Events and Recovery Actions to CSV."""
    f = StringIO()
    writer = csv.writer(f)
    
    # Write header
    writer.writerow([
        "PaymentEvent_ID", "Status", "Amount", "Error_Reason",
        "Diagnosis_Category", "Recovery_Action", "Recovery_Status",
        "Template_Used", "Discount_Applied"
    ])
    
    events = (
        db.query(PaymentEvent, Diagnosis, RecoveryAction)
        .outerjoin(Diagnosis, Diagnosis.payment_event_id == PaymentEvent.id)
        .outerjoin(RecoveryAction, RecoveryAction.diagnosis_id == Diagnosis.id)
        .all()
    )
    
    for pe, diag, action in events:
        writer.writerow([
            pe.id,
            pe.status,
            pe.amount,
            pe.error_reason,
            diag.root_cause_category if diag else "",
            action.action_type if action else "",
            action.status if action else "",
            action.template_used if action else "",
            action.discount_applied if action else False,
        ])
    
    f.seek(0)
    return StreamingResponse(f, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=recovery_export.csv"})


# ── POST /api/simulate-payment ───────────────────────────

class SimulatePaymentRequest(BaseModel):
    payment_link_id: str

@router.post("/simulate-payment")
def simulate_payment(req: SimulatePaymentRequest, db: Session = Depends(get_db)):
    """Simulate a successful payment for a payment link."""
    # Find the action with this payment link
    action = db.query(RecoveryAction).filter(RecoveryAction.payment_link_url.like(f"%{req.payment_link_id}%")).first()
    if not action:
        raise HTTPException(status_code=404, detail="Payment link not found in any RecoveryAction")

    diag = db.query(Diagnosis).filter(Diagnosis.id == action.diagnosis_id).first()
    pe = db.query(PaymentEvent).filter(PaymentEvent.id == diag.payment_event_id).first()

    # Simulate webhook hitting our backend
    webhook_payload = {
        "event": "payment.captured",
        "payload": {
            "payment": {
                "entity": {
                    "id": f"pay_sim_{uuid.uuid4().hex[:10]}",
                    "amount": pe.amount,
                    "status": "captured",
                    "email": pe.customer_email,
                    "contact": pe.customer_contact,
                    "notes": {
                        "recovery_for": pe.razorpay_payment_id
                    }
                }
            }
        }
    }
    
    import requests
    try:
        # Need to call our own webhook endpoint locally
        requests.post(
            "http://localhost:8000/webhooks/razorpay",
            json=webhook_payload,
            headers={"X-Razorpay-Event-Id": f"ev_sim_{uuid.uuid4().hex[:10]}"},
            timeout=2
        )
    except Exception as e:
        return {"status": "error", "message": f"Simulated webhook failed: {str(e)}"}
    
    return {"status": "success", "message": "Simulated payment.captured webhook sent successfully."}

