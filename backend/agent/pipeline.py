"""
pipeline.py — Batch processing pipeline for the recovery agent.

Function: run_batch() -> dict

Orchestrates:
  1. Find undiagnosed PaymentEvents → diagnose()
  2. Find due RecoveryActions (scheduled_at <= now, status=pending) → execute()
  3. Check pending promises → check_promises()

This is called by APScheduler every 5 minutes and by POST /api/run-batch.
"""

import asyncio
from datetime import datetime
from sqlalchemy.orm import Session
from database import SessionLocal
from models import PaymentEvent, Diagnosis, RecoveryAction, AuditLog
from agent.diagnose import diagnose
from agent.decide import decide
from agent.execute import execute
from agent.promise_tracker import check_promises


async def process_event(pe_id: str, summary: dict, sem: asyncio.Semaphore):
    async with sem:
        db = SessionLocal()
        try:
            pe = db.query(PaymentEvent).filter(PaymentEvent.id == pe_id).first()
            if not pe:
                return
                
            diag = await diagnose(pe, db)
            summary["diagnosed"] += 1

            # Immediately decide an action for the new diagnosis
            action = decide(diag, db)
            summary["decisions_made"] += 1
            db.commit()
        except Exception as e:
            db.rollback()
            summary["errors"].append(f"Diagnosis error for {pe_id[:8]}: {str(e)}")
            db.add(AuditLog(
                actor="system",
                action="pipeline_error",
                reasoning=f"Error diagnosing payment event {pe_id[:8]}: {str(e)}",
                related_entity_type="PaymentEvent",
                related_entity_id=pe_id,
            ))
            db.commit()
        finally:
            db.close()


async def run_batch() -> dict:
    """
    Run the full agent pipeline asynchronously:
      1. Diagnose unprocessed payment events concurrently (bounded by semaphore)
      2. Execute due recovery actions
      3. Check promise statuses
    """
    db = SessionLocal()
    summary = {
        "diagnosed": 0,
        "decisions_made": 0,
        "actions_executed": 0,
        "promises_checked": 0,
        "errors": [],
    }

    try:
        # ── Step 1: Diagnose unprocessed payment events ───
        undiagnosed = (
            db.query(PaymentEvent)
            .outerjoin(Diagnosis, Diagnosis.payment_event_id == PaymentEvent.id)
            .filter(Diagnosis.id.is_(None))
            .all()
        )

        # Batch LLM diagnoses concurrently with semaphore
        sem = asyncio.Semaphore(5)
        tasks = [process_event(pe.id, summary, sem) for pe in undiagnosed]
        await asyncio.gather(*tasks)

        # ── Step 2: Execute due recovery actions ──────────
        due_actions = (
            db.query(RecoveryAction)
            .filter(
                RecoveryAction.status == "pending",
                RecoveryAction.scheduled_at <= datetime.utcnow(),
            )
            .all()
        )

        for action in due_actions:
            try:
                execute(action, db)
                summary["actions_executed"] += 1
            except Exception as e:
                summary["errors"].append(f"Execution error for action {action.id[:8]}: {str(e)}")
                action.status = "failed"
                action.outcome = f"Pipeline execution error: {str(e)}"
                db.add(AuditLog(
                    actor="system",
                    action="pipeline_error",
                    reasoning=f"Error executing action {action.id[:8]}: {str(e)}",
                    related_entity_type="RecoveryAction",
                    related_entity_id=action.id,
                ))

        db.commit()

        # ── Step 3: Check promises ────────────────────────
        try:
            promises_updated = check_promises(db)
            summary["promises_checked"] = promises_updated
            db.commit()
        except Exception as e:
            summary["errors"].append(f"Promise check error: {str(e)}")

        # ── Audit log for batch completion ────────────────
        db.add(AuditLog(
            actor="system",
            action="batch_run_completed",
            reasoning=(
                f"Batch run completed. "
                f"Diagnosed: {summary['diagnosed']}, "
                f"Decisions: {summary['decisions_made']}, "
                f"Executed: {summary['actions_executed']}, "
                f"Promises checked: {summary['promises_checked']}. "
                f"Errors: {len(summary['errors'])}."
            ),
        ))
        db.commit()

    except Exception as e:
        db.rollback()
        summary["errors"].append(f"Pipeline error: {str(e)}")
    finally:
        db.close()

    return summary
