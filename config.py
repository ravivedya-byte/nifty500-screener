"""
config.py — Fill in YOUR credentials below, then run screener.py
"""
import os

# ─── Credentials ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "YOUR_CHAT_ID_HERE")
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "sk-ant-XXXXXXXXXXXXXXXX")

# ─── Your Details ────────────────────────────────────────────────────────────
SEBI_RA_NUMBER    = os.getenv("SEBI_RA_NUMBER",   "INH000XXXXXX")
SUBSTACK_BASE_URL = os.getenv("SUBSTACK_BASE_URL", "https://yourname.substack.com")

# ─── Hard Elimination Criteria (Pass 2) ──────────────────────────────────────
MIN_MARKET_CAP_CR     = 500
MAX_DE_RATIO          = 1.0
MIN_ROE               = 15.0
MIN_REVENUE_GROWTH_5Y = 10.0
MIN_ROCE_3Y_AVG       = 18.0
MIN_FCF_TO_PAT        = 0.75
MIN_PROMOTER_HOLDING  = 45.0
MAX_PROMOTER_PLEDGE   = 5.0
MAX_PEG_RATIO         = 1.0

# ─── Soft Flag Thresholds ────────────────────────────────────────────────────
MAX_INVENTORY_RATIO   = 1.2
MAX_RECEIVABLES_RATIO = 1.3

# ─── Near-Miss & Alert Settings ──────────────────────────────────────────────
NEAR_MISS_MAX_FAILS   = 2
ALERT_COOLDOWN_DAYS   = 30

# ─── Paper Portfolio ─────────────────────────────────────────────────────────
SIP_AMOUNT_INR        = 100_000
GOLD_ETF_SYMBOL       = "GOLDBEES"
BENCHMARK_TICKER      = "^NSEI"
