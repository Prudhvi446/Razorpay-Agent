"""
main.py — FastAPI application entry point.

Wires all routes, CORS, and APScheduler.

Run:
    cd backend
    uvicorn main:app --reload --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

from config import FRONTEND_URL, BATCH_INTERVAL_MINUTES
from database import engine, Base
from routes.webhooks import router as webhook_router
from routes.api import router as api_router
from agent.pipeline import run_batch

# ── Scheduler ─────────────────────────────────────────────

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    # Startup: create tables + start scheduler
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created/verified")

    scheduler.add_job(
        run_batch,
        "interval",
        minutes=BATCH_INTERVAL_MINUTES,
        id="recovery_agent_batch",
        replace_existing=True,
    )
    scheduler.start()
    print(f"✅ APScheduler started (every {BATCH_INTERVAL_MINUTES} min)")

    yield

    # Shutdown
    scheduler.shutdown(wait=False)
    print("🛑 APScheduler stopped")


# ── App ───────────────────────────────────────────────────

app = FastAPI(
    title="Revenue Recovery Agent API",
    description="AI-powered revenue recovery agent for Razorpay — hackathon project",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────

app.include_router(webhook_router)
app.include_router(api_router)


@app.get("/")
def root():
    return {
        "service": "Revenue Recovery Agent",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "healthy"}
