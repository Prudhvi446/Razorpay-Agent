# Revenue Recovery Agent

AI-powered autonomous revenue recovery agent for Razorpay — built for the AI Revenue Recovery hackathon track.

## Architecture

```
razorpay-agent/
├── backend/           # Python FastAPI backend
│   ├── main.py        # FastAPI app entry point
│   ├── config.py      # Environment config
│   ├── database.py    # SQLAlchemy connection
│   ├── models.py      # ORM models (5 tables)
│   ├── agent/         # Core agent modules
│   │   ├── diagnose.py       # Hybrid rules + Gemini diagnosis
│   │   ├── decide.py         # Deterministic action decisions
│   │   ├── execute.py        # Action execution (Razorpay + Resend)
│   │   ├── pipeline.py       # Batch orchestrator
│   │   └── promise_tracker.py # Promise-to-pay tracking
│   ├── routes/        # API routes
│   │   ├── api.py            # Dashboard REST endpoints
│   │   └── webhooks.py       # Razorpay webhook receiver
│   └── scripts/       # Utilities
│       └── seed_failures.py  # Generate test failure scenarios
└── frontend/          # React + Vite dashboard
    └── src/
        ├── api.js             # API client
        ├── App.jsx            # Main layout
        └── components/        # Dashboard components
```

## Quick Start

### 1. Backend Setup

```bash
cd backend
cp .env.example .env           # Fill in your API keys
pip install -r requirements.txt
python -m scripts.seed_failures  # Seed test data
uvicorn main:app --reload --port 8000
```

### 2. Frontend Setup

```bash
cd frontend
npm install
npm run dev                    # Runs on http://localhost:5173
```

### 3. Run the Agent

Either:
- **Automatic**: Agent runs every 5 minutes via APScheduler
- **Manual**: Click "Run Agent Pipeline" in the dashboard, or:
  ```bash
  curl -X POST http://localhost:8000/api/run-batch
  ```

## Environment Variables

| Variable | Description |
|---|---|
| `RAZORPAY_KEY_ID` | Razorpay test mode key ID |
| `RAZORPAY_KEY_SECRET` | Razorpay test mode key secret |
| `RAZORPAY_WEBHOOK_SECRET` | Webhook signature verification secret |
| `DATABASE_URL` | PostgreSQL connection string (Supabase) |
| `GEMINI_API_KEY` | Google Gemini API key |
| `RESEND_API_KEY` | Resend email API key |

## Pipeline Stages

1. **Ingest** — Webhooks + seed script create PaymentEvent records
2. **Diagnose** — Hybrid deterministic + Gemini LLM classification
3. **Decide** — Rule-based action mapping with hard-coded guardrails
4. **Execute** — Razorpay Payment Links + Resend transactional email
5. **Track** — Promise-to-pay lifecycle + audit trail

## Guardrails

- Max 3 contacts per incident (enforced in code)
- No outbound actions 9PM–8AM IST
- Abandoned carts → email only
- Every decision audited BEFORE execution
