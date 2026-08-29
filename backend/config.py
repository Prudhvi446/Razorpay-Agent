"""
Configuration module — loads all env vars and exposes constants.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Razorpay ──────────────────────────────────────────────
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET")

# ── Database ──────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")

# ── Gemini (Google AI) ────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"

# ── Resend (Email) ────────────────────────────────────────
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")

# ── Frontend CORS ─────────────────────────────────────────
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# ── Agent Guardrails ──────────────────────────────────────
MAX_CONTACTS_PER_INCIDENT = 3
MAX_CONTACTS_PER_24H = 2
QUIET_HOURS_START = 21        # 9 PM
QUIET_HOURS_END = 8           # 8 AM
TIMEZONE = "Asia/Kolkata"

# ── Scheduler ─────────────────────────────────────────────
BATCH_INTERVAL_MINUTES = 5
