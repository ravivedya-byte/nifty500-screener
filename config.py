"""
config.py — Fill in YOUR credentials below, then run screener.py
─────────────────────────────────────────────────────────────────
For local runs  : edit the default values directly in this file.
For GitHub Actions: leave defaults as-is; set the values as
                    GitHub Secrets (see README.md).
"""

import os

# ─── Credentials ─────────────────────────────────────────────────────────────

# Telegram Bot (get from @BotFather)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8268343253:AAFpX-pYWOJca1UjQ5wUUjKwwH0bHEvZH6k")

# Your Telegram Chat ID (see README for how to find this)
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "1276631405")

# Your Anthropic API key — https://console.anthropic.com
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "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")

# ─── Hard Elimination Criteria (Pass 2) ──────────────────────────────────────
# A stock must pass ALL of these to proceed to AI assessment.

MIN_MARKET_CAP_CR     = 500    # Minimum market capitalisation (₹ Crores)
MAX_DE_RATIO          = 1.0    # Maximum Debt / Equity ratio
MIN_ROE               = 15.0   # Minimum Return on Equity (%)
MIN_REVENUE_GROWTH_5Y = 10.0   # Minimum 5-year Revenue CAGR (%)
MIN_ROCE_3Y_AVG       = 18.0   # Minimum average ROCE over last 3 years (%)
MIN_FCF_TO_PAT        = 0.75   # Average (Free Cash Flow / Net Profit) over 5Y
MIN_PROMOTER_HOLDING  = 45.0   # Minimum promoter stake (%)
MAX_PROMOTER_PLEDGE   = 5.0    # Maximum promoter pledge (%)
MAX_PEG_RATIO         = 1.0    # Maximum PEG ratio  (P/E ÷ 5Y EPS CAGR)


# ─── Soft Flag Thresholds (shown in alert, NOT eliminating) ──────────────────
# These appear as warnings in the WhatsApp alert for you to judge manually.

MAX_INVENTORY_RATIO   = 1.2    # Inventory 3Y CAGR ÷ Revenue 3Y CAGR
MAX_RECEIVABLES_RATIO = 1.3    # Receivables 3Y CAGR ÷ Revenue 3Y CAGR


# ─── Near-Miss Settings ──────────────────────────────────────────────────────

NEAR_MISS_MAX_FAILS   = 2      # Stocks failing ≤ this many criteria → near-miss log


# ─── Alert / Watchlist Settings ──────────────────────────────────────────────

ALERT_COOLDOWN_DAYS   = 30     # Don't re-alert same stock within this many days


# ─── Paper Portfolio (SIP Tracker) ───────────────────────────────────────────

SIP_AMOUNT_INR        = 100_000   # ₹1,00,000 deployed on 1st of each month
GOLD_ETF_SYMBOL       = "GOLDBEES" # Fallback when no stocks qualify
BENCHMARK_TICKER      = "^NSEI"    # Nifty 50 as performance benchmark
