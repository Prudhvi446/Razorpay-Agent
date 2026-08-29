# AI Revenue Recovery Agent for Razorpay

An enterprise-grade, autonomous revenue recovery agent designed for Razorpay transactions. The agent combines hybrid deterministic rules with Google Gemini LLM reasoning, orchestrated by a LangGraph state machine, to recover failed payments, abandoned carts, and mandate failures while enforcing strict compliance, idempotency, and human guardrails.

---

## Key Features & Production Hardening

### 1. Hybrid Diagnosis Engine
- **Deterministic Pre-Classification**: Instant zero-latency mapping for unambiguous Razorpay error codes (`GATEWAY_ERROR`, `BAD_REQUEST_ERROR`, `insufficient_funds`, etc.).
- **Gemini LLM Enrichment**: Deep root-cause contextual diagnosis for ambiguous errors, customer payment history, and retry attempts.
- **Hard Kill Switch**: Immediate human escalation bypassing all LLM and outbound calls for disputed transactions or suspected fraud.

### 2. LangGraph Multi-Day Recovery State Machine
- **State Flow**: `Diagnose` $\to$ `Draft_Message` $\to$ `Execute` $\to$ `Wait` $\to$ `END`.
- **Multi-Day Dunning Lifecycle**:
  - **Day 1**: Soft reminder with one-click payment link.
  - **Day 3**: Escalation with incentive (automated 5% cart saver discount for high-value abandoned orders).
  - **Day 5+**: Automatic escalation to Account Manager or human review.
- **Promise-to-Pay State Pausing**: Halts dunning when a customer promises payment, automatically resuming or escalating if breached.

### 3. Production Edge-Case Safeguards
- **Webhook Idempotency**: Strict validation of `X-Razorpay-Event-Id` and `webhook_event_id` unique indexing. Duplicate webhooks return `HTTP 200 {"status": "duplicate_event_ignored"}` immediately.
- **Pre-execution Race Condition Guard**: If a transaction is already `PAID`, `SETTLED`, or `CAPTURED`, ongoing and incoming recovery pipelines immediately abort and record status `ALREADY_RESOLVED`.
- **Defensive Ingestion**: Strict validation catches malformed or negative payloads (`amount <= 0`, missing `payment_id`), logging an audit record (`MALFORMED_PAYLOAD`) and returning `HTTP 422` without server crashes.
- **Promise Breach Lifecycle**: Expired commitment dates (`promised_date < NOW`) automatically transition to `PROMISE_BREACHED`, escalating the incident to `ESCALATE_TO_ACCOUNT_MANAGER`. Invalid dates trigger immediate human escalation.
- **User Opt-Out / DND Kill Switch**: Replies containing `STOP`, `UNSUBSCRIBE`, or `DND` immediately terminate all recovery loops for that customer, set `opted_out = True`, and persist the preference to `CustomerProfile`.
- **Indian Standard Time (IST) Quiet Hours**: Communications between 21:00 and 08:00 IST are intercepted and transitioned to `QUEUED_FOR_MORNING_WINDOW`, scheduled for dispatch at 08:30 AM IST.
- **Timezone Alignment**: Audit logs and reasoning entries are fully aligned to Indian Standard Time (IST, UTC+5:30) with explicit timezone offsets.

---

## Architecture Overview

```
razorpay-agent/
├── backend/
│   ├── main.py                  # FastAPI application entrypoint & scheduler
│   ├── config.py                # Environment variables & constants
│   ├── database.py              # SQLAlchemy engine & dynamic schema migrations
│   ├── models.py                # Database models (8 tables)
│   ├── agent/
│   │   ├── diagnose.py          # Deterministic rules + Gemini classification
│   │   ├── decide.py            # Guardrails, quiet hours, & cooldown decisions
│   │   ├── execute.py           # Razorpay Payment Links + Resend email dispatch
│   │   ├── pipeline.py          # LangGraph state machine & batch orchestrator
│   │   └── promise_tracker.py   # Promise validation, tracking, & breach detection
│   ├── routes/
│   │   ├── api.py               # REST endpoints for dashboard & metrics
│   │   └── webhooks.py          # Defensive Razorpay webhook ingestion
│   ├── scripts/
│   │   ├── seed_failures.py     # Generates synthetic failure & edge-case scenarios
│   │   └── ground_truth.json    # Evaluation benchmark labels
│   └── tests/
│       ├── test_ab_eval.py          # A/B group tagging & eval tests
│       ├── test_dynamic_prompting.py# Dynamic template personalization tests
│       ├── test_edge_cases.py       # 6 production edge-case test suites
│       ├── test_safeguards.py       # Kill switch & frequency limit tests
│       └── test_state_machine.py    # LangGraph lifecycle & promise pause tests
└── frontend/
    └── src/
        ├── App.jsx              # Main dashboard layout
        ├── api.js               # REST client
        └── components/
            ├── AuditFeed.jsx        # Real-time IST audit log stream
            ├── EvalTable.jsx        # A/B testing & accuracy evaluation table
            ├── FunnelChart.jsx      # Recovery conversion funnel
            ├── RootCauseChart.jsx   # Root cause distribution breakdown
            ├── RunBatchButton.jsx   # Interactive pipeline execution button
            └── StatCards.jsx        # Real-time financial & recovery KPIs
```

---

## Database Models

| Table | Model | Description |
|---|---|---|
| `payment_events` | `PaymentEvent` | Ingested Razorpay failures and carts, tracked with `lifecycle_status`, `webhook_event_id`, and `opted_out`. |
| `customer_profiles` | `CustomerProfile` | Customer preferences, global opt-out / DND status, and contact history. |
| `diagnoses` | `Diagnosis` | Root-cause classification, LLM reasoning, and confidence score. |
| `recovery_actions` | `RecoveryAction` | Scheduled and executed recovery actions (links, emails, human escalations). |
| `promises_to_pay` | `PromiseToPay` | Customer payment commitments with validation and breach detection. |
| `audit_log` | `AuditLog` | Append-only audit trail with Indian Standard Time (IST) timestamps. |
| `processed_webhooks` | `ProcessedWebhook` | Webhook idempotency ledger preventing duplicate execution. |
| `dead_letter_queue` | `DeadLetterQueue` | Unprocessable or poison messages isolated for debugging. |

---

## Getting Started

### 1. Prerequisites
- Python 3.10+
- Node.js 18+ & npm
- PostgreSQL database (e.g. Supabase) or local SQLite

### 2. Backend Setup

```bash
cd backend

# Create virtual environment (optional)
python -m venv venv
venv\Scripts\activate  # Windows: venv\Scripts\activate | Unix: source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env   # Add Razorpay, Gemini, Resend, and Database credentials
```

#### Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (Supabase) or SQLite |
| `RAZORPAY_KEY_ID` | Razorpay API Key ID |
| `RAZORPAY_KEY_SECRET` | Razorpay API Secret |
| `RAZORPAY_WEBHOOK_SECRET` | Webhook HMAC verification secret |
| `GEMINI_API_KEY` | Google Gemini API Key |
| `RESEND_API_KEY` | Resend transactional email API key |
| `DASHBOARD_API_KEY` | (Optional) API key for authenticated endpoints |
| `BATCH_INTERVAL_MINUTES`| Background scheduler interval (default: 5 min) |

### 3. Seed Realistic Test Data & Edge Cases

```bash
cd backend
python -m scripts.seed_failures
```
This generates ~40 realistic failure scenarios with simulated Razorpay orders, A/B testing splits, disputes, fraud flags, opt-outs, and promise-to-pay commitments.

### 4. Run the Backend API

```bash
cd backend
uvicorn main:app --reload --port 8000
```
Swagger API docs will be accessible at: `http://localhost:8000/docs`

### 5. Frontend Setup

```bash
cd frontend
npm install
npm run dev
```
Dashboard will be live at: `http://localhost:5173`

---

## Running the Automated Test Suite

The test suite covers safeguards, state transitions, prompt generation, and all production edge cases:

```bash
cd backend
python -m pytest -v
```

### Test Coverage Summary (21 Tests)
1. **Edge Cases (`test_edge_cases.py`)**:
   - `test_duplicate_webhook_idempotency` — Confirms duplicate `event_id` is ignored with HTTP 200.
   - `test_race_condition_already_paid` — Verifies recovery aborts when payment status is already `PAID`.
   - `test_malformed_webhook_payload` — Validates rejection of negative amount or missing IDs with HTTP 422.
   - `test_promise_date_validation_and_breach` — Tests past date rejection and automated `PROMISE_BREACHED` transition.
   - `test_opt_out_kill_switch` — Verifies `STOP` payload halts recovery and flags customer profile.
   - `test_quiet_hours_queuing` — Checks late-night queueing for 08:30 AM IST.
2. **Safeguards (`test_safeguards.py`)**: Kill switch bypasses LLM, 24-hour rate limiting, and frequency capping.
3. **State Machine (`test_state_machine.py`)**: LangGraph flow, promise pauses, and multi-day escalation.
4. **Dynamic Prompting (`test_dynamic_prompting.py`)**: Contextual tone and personalization per error category.
5. **A/B Evaluation (`test_ab_eval.py`)**: Randomized A/B cohort evaluation and accuracy tracking.

---

## API Reference

### Webhooks
- `POST /webhooks/razorpay`: Ingests Razorpay webhook events with HMAC signature validation, idempotency guards, and opt-out parsing.

### Dashboard REST APIs
- `GET /api/stats`: Recovery metrics, at-risk revenue, and comparative A/B conversion rates.
- `GET /api/funnel`: Recovery conversion funnel (Failed $\to$ Diagnosed $\to$ Contacted $\to$ Recovered).
- `GET /api/root-causes`: Distribution of failure causes (soft decline, bank network issue, 3DS, abandoned cart).
- `GET /api/audit-log`: Live audit feed with Indian Standard Time (`timestamp_ist`, `timestamp_display`).
- `POST /api/run-batch`: Manually triggers the LangGraph recovery state machine for live demonstration.
- `GET /api/eval`: Classification accuracy benchmarking against ground-truth labels.
- `POST /api/promise`: Records customer promise-to-pay commitments with automated validation.
- `GET /api/export-csv`: Exports payment events and recovery actions to CSV for reporting.
