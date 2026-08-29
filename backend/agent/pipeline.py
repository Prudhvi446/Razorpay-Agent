"""
pipeline.py — Stateful recovery agent pipeline built with LangGraph.

Orchestrates recovery lifecycle as a state graph:
  [START] -> [Diagnose] -> [Draft_Message] -> [Execute] -> [Wait] -> [END]

Features:
  - Strict stopping rules & safeguards (dispute/fraud kill switch, frequency capping)
  - Multi-day escalation matrix: Day 1 soft reminder -> Wait 2 days -> Day 3 discount offer
  - State persistence & Promise Tracker integration: pauses graph if customer promises a payment date
  - Batch pipeline API (run_batch() -> dict) for scheduled and manual dashboard execution
"""

import asyncio
from datetime import datetime, timedelta
from typing import TypedDict, Optional, Dict, Any, List

from langgraph.graph import StateGraph, START, END
from sqlalchemy.orm import Session

from database import SessionLocal
from models import PaymentEvent, Diagnosis, RecoveryAction, AuditLog
from agent.diagnose import diagnose
from agent.decide import decide, is_hard_kill_switch_triggered, get_user_contact_count_24h
from agent.execute import execute, generate_contextual_message
from agent.promise_tracker import check_promises, check_expired_promises, get_active_promise_for_event
from config import MAX_CONTACTS_PER_24H


# ── State Definition ──────────────────────────────────────

class RecoveryGraphState(TypedDict):
    payment_event_id: str
    stage: str                  # "diagnose", "draft_message", "execute", "wait", "escalated", "paused", "completed", "stopped"
    day_step: int               # 1 = Day 1 (soft reminder), 2 = Day 3 (discount offer)
    root_cause_category: Optional[str]
    confidence: Optional[float]
    is_kill_switch: bool
    drafted_message: Optional[Dict[str, Any]]
    action_type: Optional[str]
    recovery_action_id: Optional[str]
    action_status: Optional[str]
    paused_for_promise: bool
    promised_date: Optional[str]
    discount_applied: bool
    error: Optional[str]
    logs: List[str]


# ── Node 1: Diagnose ──────────────────────────────────────

async def node_diagnose(state: RecoveryGraphState) -> RecoveryGraphState:
    """Diagnose root cause or activate Hard Kill Switch if dispute/fraud suspected."""
    pe_id = state["payment_event_id"]
    db = SessionLocal()
    try:
        pe = db.query(PaymentEvent).filter(PaymentEvent.id == pe_id).first()
        if not pe:
            state["error"] = f"PaymentEvent {pe_id} not found"
            state["stage"] = "failed"
            return state

        # Race condition check: If already paid or settled
        if (
            str(pe.status).upper() in ("PAID", "SETTLED", "CAPTURED")
            or getattr(pe, "lifecycle_status", "").upper() in ("PAID", "SETTLED")
        ):
            state["stage"] = "completed"
            state["action_status"] = "ALREADY_RESOLVED"
            state["logs"].append(f"Payment {pe_id[:8]} is already PAID/SETTLED. Aborting recovery as ALREADY_RESOLVED.")
            return state

        # Customer Opt-Out Check
        if getattr(pe, "opted_out", False):
            state["stage"] = "stopped"
            state["action_status"] = "OPTED_OUT"
            state["logs"].append(f"Customer {pe.customer_id or pe_id[:8]} opted out. Recovery halted.")
            return state

        # Hard Kill Switch Check
        if is_hard_kill_switch_triggered(pe):
            diag = pe.diagnosis
            if not diag:
                diag = Diagnosis(
                    payment_event_id=pe.id,
                    root_cause_category="unrecoverable",
                    confidence=1.0,
                    llm_reasoning="Hard Kill Switch: Disputed transaction or fraud suspected. Halting recovery pipeline.",
                )
                db.add(diag)
                db.commit()
            state["is_kill_switch"] = True
            state["stage"] = "escalated"
            state["root_cause_category"] = "unrecoverable"
            state["confidence"] = 1.0
            state["action_type"] = "escalate_human"
            state["logs"].append(f"HARD KILL SWITCH triggered for {pe_id[:8]}. Halting pipeline immediately.")
            return state

        # Check existing diagnosis or run async diagnose
        diag = pe.diagnosis
        if not diag:
            diag = await diagnose(pe, db)
            db.commit()

        state["root_cause_category"] = diag.root_cause_category
        state["confidence"] = diag.confidence
        state["stage"] = "draft_message"
        state["logs"].append(f"Diagnosed {pe_id[:8]} as {diag.root_cause_category} ({diag.confidence:.2f})")
    except Exception as e:
        db.rollback()
        state["error"] = f"Diagnosis error: {str(e)}"
        state["stage"] = "failed"
    finally:
        db.close()

    return state


# ── Node 2: Draft Message ─────────────────────────────────

async def node_draft_message(state: RecoveryGraphState) -> RecoveryGraphState:
    """Contextually draft tailored message based on root cause and multi-day escalation stage."""
    if state.get("is_kill_switch") or state.get("root_cause_category") == "unrecoverable":
        state["action_type"] = "escalate_human"
        state["stage"] = "execute"
        return state

    pe_id = state["payment_event_id"]
    db = SessionLocal()
    try:
        pe = db.query(PaymentEvent).filter(PaymentEvent.id == pe_id).first()
        diag = pe.diagnosis if pe else None

        day_step = state.get("day_step", 1)
        discount_applied = state.get("discount_applied", False)
        if day_step >= 2 or (pe and pe.amount >= 200000 and diag and diag.root_cause_category == "customer_abandoned"):
            discount_applied = True
            state["discount_applied"] = True

        msg = generate_contextual_message(
            pe=pe,
            diagnosis=diag,
            day_step=day_step,
            discount_applied=discount_applied,
            payment_link="{payment_link}",
        )
        state["drafted_message"] = msg
        state["action_type"] = msg.get("recovery_action_type", "send_email")
        state["stage"] = "execute"
        state["logs"].append(
            f"Drafted contextual message (Day {day_step}) for {diag.root_cause_category if diag else 'general'}: "
            f"'{msg.get('subject')}'"
        )
    except Exception as e:
        state["error"] = f"Drafting error: {str(e)}"
        state["stage"] = "execute"
    finally:
        db.close()

    return state


# ── Node 3: Execute ───────────────────────────────────────

async def node_execute(state: RecoveryGraphState) -> RecoveryGraphState:
    """Enforce frequency capping guardrails and dispatch recovery action."""
    pe_id = state["payment_event_id"]
    db = SessionLocal()
    try:
        pe = db.query(PaymentEvent).filter(PaymentEvent.id == pe_id).first()
        diag = pe.diagnosis if pe else None

        # Hard Kill Switch execution path
        if state.get("is_kill_switch"):
            if not diag and pe:
                diag = Diagnosis(
                    payment_event_id=pe.id,
                    root_cause_category="unrecoverable",
                    confidence=1.0,
                    llm_reasoning="Hard Kill Switch: Disputed transaction or fraud suspected.",
                )
                db.add(diag)
                db.commit()

            action = RecoveryAction(
                diagnosis_id=diag.id,
                action_type="escalate_human",
                status="Escalated_to_Human",
                outcome="Escalated_to_Human: Disputed transaction or fraud suspected",
            )
            db.add(action)
            db.commit()
            state["recovery_action_id"] = action.id
            state["action_status"] = "Escalated_to_Human"
            state["stage"] = "escalated"
            state["logs"].append("Escalated to human due to kill switch.")
            return state

        # Frequency Capping check
        if pe:
            contact_count_24h = get_user_contact_count_24h(pe, db)
            if contact_count_24h >= MAX_CONTACTS_PER_24H:
                reason = (
                    f"Rate_Limit_Exceeded: User {pe.customer_id or pe.customer_email or pe.id[:8]} "
                    f"contacted {contact_count_24h} times in 24h. Aborting."
                )
                db.add(AuditLog(
                    actor="agent",
                    action="Rate_Limit_Exceeded",
                    reasoning=reason,
                    related_entity_type="RecoveryAction",
                    related_entity_id=None,
                ))
                if not diag:
                    diag = Diagnosis(
                        payment_event_id=pe.id,
                        root_cause_category="unrecoverable",
                        confidence=0.5,
                        llm_reasoning="Rate limited before diagnosis.",
                    )
                    db.add(diag)
                    db.commit()

                action = RecoveryAction(
                    diagnosis_id=diag.id,
                    action_type="stop",
                    status="failed",
                    outcome="Rate_Limit_Exceeded",
                )
                db.add(action)
                db.commit()
                state["recovery_action_id"] = action.id
                state["action_status"] = "failed"
                state["stage"] = "stopped"
                state["logs"].append("Rate_Limit_Exceeded: Frequency limit reached. Aborted.")
                return state

        # Create or update recovery action via decide
        if diag:
            action = decide(diag, db)
            if state.get("discount_applied"):
                action.discount_applied = True

            # If action is already resolved, stopped, or opted out, conclude pipeline
            if action.status in ("ALREADY_RESOLVED", "OPTED_OUT", "stopped"):
                state["action_status"] = action.status
                state["stage"] = "stopped" if action.status in ("OPTED_OUT", "stopped") else "completed"
            elif action.status == "pending" and action.scheduled_at and action.scheduled_at <= datetime.utcnow():
                execute(action, db)
                state["action_status"] = action.status
                state["stage"] = "wait"
            else:
                state["action_status"] = action.status
                state["stage"] = "wait"

            state["recovery_action_id"] = action.id
            db.commit()
            state["logs"].append(f"Action {action.action_type} recorded: {action.status}")
    except Exception as e:
        db.rollback()
        state["error"] = f"Execution error: {str(e)}"
        state["stage"] = "failed"
    finally:
        db.close()

    return state


# ── Node 4: Wait (Escalation Lifecycle & Promise Tracker) ──

async def node_wait(state: RecoveryGraphState) -> RecoveryGraphState:
    """
    State Persistence & Promise Integration:
    - If customer responded with a promised date, pause graph execution.
    - Otherwise, manage multi-day recovery lifecycle (Day 1 -> Day 3).
    """
    pe_id = state["payment_event_id"]
    db = SessionLocal()
    try:
        # Check and update any expired promises across the system
        check_expired_promises(db)

        # Check if user promised to pay
        active_promise = get_active_promise_for_event(pe_id, db)
        if active_promise:
            state["paused_for_promise"] = True
            state["promised_date"] = active_promise.promised_date.isoformat()
            state["stage"] = "paused"
            state["logs"].append(
                f"PAUSED: Customer promised to pay by {active_promise.promised_date.strftime('%Y-%m-%d')}. "
                f"Graph paused."
            )
            db.add(AuditLog(
                actor="agent",
                action="recovery_paused",
                reasoning=(
                    f"Recovery state graph paused for {pe_id[:8]}. "
                    f"Customer promised payment by {active_promise.promised_date.strftime('%Y-%m-%d')}."
                ),
                related_entity_type="PromiseToPay",
                related_entity_id=active_promise.id,
            ))
            db.commit()
            return state

        # Multi-day recovery progression: Day 1 -> Wait -> Day 3
        current_step = state.get("day_step", 1)
        pe = db.query(PaymentEvent).filter(PaymentEvent.id == pe_id).first()
        if current_step == 1:
            # Advance to Day 3 Discount offer stage
            state["day_step"] = 2
            state["discount_applied"] = True
            if pe:
                pe.escalation_stage = 2
                db.commit()
            state["stage"] = "waiting_for_escalation"
            state["logs"].append("Escalation stage updated to Day 3 (Discount offer scheduled).")
        else:
            state["stage"] = "completed"
            state["logs"].append("Multi-day recovery escalation lifecycle completed.")
    except Exception as e:
        db.rollback()
        state["error"] = f"Wait node error: {str(e)}"
    finally:
        db.close()

    return state


# ── Conditional Routing ───────────────────────────────────

def route_after_diagnose(state: RecoveryGraphState) -> str:
    if state.get("stage") in ("failed", "completed", "stopped") or state.get("action_status") in ("ALREADY_RESOLVED", "OPTED_OUT"):
        return END
    if state.get("is_kill_switch") or state.get("root_cause_category") == "unrecoverable":
        return "Execute"
    return "Draft_Message"


def route_after_execute(state: RecoveryGraphState) -> str:
    if (
        state.get("stage") in ("escalated", "stopped", "failed", "completed")
        or state.get("action_status") in ("failed", "Escalated_to_Human", "stop", "ALREADY_RESOLVED", "OPTED_OUT")
    ):
        return END
    return "Wait"


# ── State Graph Builder ───────────────────────────────────

def build_recovery_graph():
    """Build and compile the LangGraph recovery state machine."""
    workflow = StateGraph(RecoveryGraphState)

    workflow.add_node("Diagnose", node_diagnose)
    workflow.add_node("Draft_Message", node_draft_message)
    workflow.add_node("Execute", node_execute)
    workflow.add_node("Wait", node_wait)

    workflow.add_edge(START, "Diagnose")
    workflow.add_conditional_edges(
        "Diagnose",
        route_after_diagnose,
        {
            "Execute": "Execute",
            "Draft_Message": "Draft_Message",
            END: END,
        }
    )
    workflow.add_edge("Draft_Message", "Execute")
    workflow.add_conditional_edges(
        "Execute",
        route_after_execute,
        {
            "Wait": "Wait",
            END: END,
        }
    )
    workflow.add_edge("Wait", END)

    return workflow.compile()


recovery_graph = build_recovery_graph()


# ── Batch Pipeline Runner ─────────────────────────────────

async def process_event_graph(pe_id: str, day_step: int, summary: dict, sem: asyncio.Semaphore):
    """Run a single payment event through the LangGraph State Machine."""
    async with sem:
        try:
            initial_state: RecoveryGraphState = {
                "payment_event_id": pe_id,
                "stage": "diagnose",
                "day_step": day_step,
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

            if final_state.get("root_cause_category"):
                summary["diagnosed"] += 1
            if final_state.get("action_type"):
                summary["decisions_made"] += 1
            if final_state.get("action_status") == "executed":
                summary["actions_executed"] += 1
            if final_state.get("error"):
                summary["errors"].append(final_state["error"])
        except Exception as e:
            summary["errors"].append(f"State graph error for {pe_id[:8]}: {str(e)}")


async def run_batch() -> dict:
    """
    Run the agent state machine pipeline asynchronously:
      1. Run undiagnosed / active recovery events through the LangGraph State Machine
      2. Execute due scheduled recovery actions
      3. Check promise statuses and update resolutions
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
        # ── Step 1: Process events through LangGraph State Machine ───
        undiagnosed = (
            db.query(PaymentEvent)
            .outerjoin(Diagnosis, Diagnosis.payment_event_id == PaymentEvent.id)
            .filter(Diagnosis.id.is_(None))
            .limit(15)
            .all()
        )

        sem = asyncio.Semaphore(5)
        tasks = [process_event_graph(pe.id, getattr(pe, "escalation_stage", 1), summary, sem) for pe in undiagnosed]
        if tasks:
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
                f"Batch run completed via LangGraph State Machine. "
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

