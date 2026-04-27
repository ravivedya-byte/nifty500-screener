# Nifty 500 Investment Screener

Screens the full Nifty 500 every weekday after market close and sends you a WhatsApp alert when a stock matches your investment philosophy.

**Pipeline:**
```
Nifty 500 (500 stocks)
    │
    ▼  Pass 1 — yfinance
    │  • Market cap ≥ ₹500 Cr
    │  • Exclude all BFSI stocks
    │
    ▼  Pass 2 — screener.in (13 quantitative criteria)
    │  • Debt/Equity 0–1           • ROE > 15%
    │  • EPS CAGR 15–35%           • Revenue CAGR > 10%
    │  • Institutional hold < 30%  • PEG ratio < 1
    │  • Inventory/Rev ratio < 1.2x• Receivables/Rev < 1.3x
    │  • ROCE consistent > 20%     • FCF/PAT > 0.75
    │  • Promoter holding > 45%    • Promoter pledge < 5%
    │
    ▼  Pass 3 — Claude AI (business model durability)
    │  • Revenue source clarity
    │  • Competitive moat type
    │  • 15-year demand outlook
    │  • Disruption risk assessment
    │  • Confidence score 1–10
    │
    ▼  WhatsApp alert (via CallMeBot)
       Score ≥ 6 and verdict PASS or STRONG PASS
```

---

## Setup (One-Time, ~15 minutes)

### Step 1 — Get the Code

```bash
git clone https://github.com/YOUR_USERNAME/nifty500-screener.git
cd nifty500-screener
pip install -r requirements.txt
```

---

### Step 2 — Set Up CallMeBot WhatsApp (Free, 2 minutes)

1. Save this number in your phone contacts: **+34 644 59 79 96**
2. Send this exact WhatsApp message to that number:
   ```
   I allow callmebot to send me messages
   ```
3. You will receive an API key back. It looks like `1234567`.
4. Note down: your WhatsApp number (with `+91`) and your API key.

---

### Step 3 — Get Your Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an account and go to **API Keys**
3. Click **Create Key** and copy it
4. Expected cost: **~₹5–25 per day** (Claude Sonnet, ~10–30 AI assessments max)

---

### Step 4 — Configure Credentials

**For local testing**, edit `config.py` directly:
```python
WHATSAPP_PHONE    = "+919876543210"   # your number
CALLMEBOT_API_KEY = "1234567"         # from Step 2
ANTHROPIC_API_KEY = "sk-ant-..."      # from Step 3
```

**For GitHub Actions** (production), add them as Secrets:
1. Go to your GitHub repo → **Settings → Secrets and variables → Actions**
2. Click **New repository secret** and add three secrets:
   - `WHATSAPP_PHONE`    → your number with country code
   - `CALLMEBOT_API_KEY` → your CallMeBot key
   - `ANTHROPIC_API_KEY` → your Anthropic key

---

### Step 5 — Test It Locally

```bash
python screener.py
```

Expect ~1–2 hours for a full Nifty 500 run. Watch the logs — you'll see
each stock being checked with pass/fail details.

To test on just a few stocks quickly, temporarily reduce the universe by
editing `fetch_nifty500()` and slicing the list:
```python
# In screener.py, temporarily add this line at the end of fetch_nifty500():
return symbols[:20]   # test on first 20 only
```

---

### Step 6 — Enable GitHub Actions (Automated Daily Run)

1. Push the code to a GitHub repository
2. Go to **Actions** tab on GitHub
3. The workflow will auto-schedule at **4:00 PM IST, Mon–Fri**
4. To run it immediately: click **Run workflow** under the workflow name

---

## Understanding the WhatsApp Alert

```
📈 STOCK ALERT
*Astral Ltd* (ASTRAL)
🏭 Industrials  |  ₹21,400 Cr
────────────────────────────────

📊 QUANTITATIVE SCORECARD
  ✅ Market Cap: ₹21,400 Cr
  ✅ Debt / Equity: 0.08
  ✅ ROE: 23.4%
  ✅ EPS Growth 5Y: 21.2%
  ✅ Revenue Growth 5Y: 16.8%
  ✅ Institutional Holding: 27.1%
  ✅ PEG Ratio: 0.93
  ✅ Inventory / Revenue: 1.11x
  ✅ ROCE Consistency: 5/5 yrs >20%, min 21.3%
  ✅ FCF / PAT: 0.82
  ✅ Promoter Holding: 55.1%
  ✅ Promoter Pledge: 0% (nil)

────────────────────────────────
🤖 AI BUSINESS MODEL ASSESSMENT
✅ Verdict: PASS
📊 Score: 7.5/10  [████████░░]

💼 Revenue: CPVC piping systems (~75%) and adhesives (~25%)
⭐ Cash Cows: CPVC pipes, Fevicol-competing adhesives
🔒 Moat: niche dominance
📅 15Y Demand Outlook: GROWING
⚡ Disruption Risk: LOW
🚨 Watch: Raw material (PVC resin) price volatility, competition from Finolex

_Strong piping brand in a structurally growing market driven by Indian 
urbanisation and infrastructure build-out. Low disruption risk — pipes 
are a physical necessity. Conservative balance sheet confirms management 
quality. Borderline concern: adhesives segment faces more competition._

────────────────────────────────
⚠️ Not a buy recommendation. Do your own due diligence.
🕐 22 Apr 2026, 04:15 PM IST
```

---

## Adjusting Your Criteria

All thresholds live in `config.py`. Common adjustments:

| What you want | What to change |
|---|---|
| Wider net (more alerts) | Lower `MIN_ROCE`, `MIN_PROMOTER_HOLDING`, raise `MAX_PEG_RATIO` |
| Stricter filter (fewer alerts) | Raise `MIN_ROE`, lower `MAX_DE_RATIO` |
| Accept lower P/E stocks | Lower `MAX_PEG_RATIO` to 0.7 |
| Include BFSI stocks | Edit `BFSI_KEYWORDS` in `screener.py` |
| Smaller-cap focus | Lower `MIN_MARKET_CAP_CR` to 200 |

---

## Cost Estimate

| Service | Cost |
|---|---|
| yfinance | Free |
| screener.in scraping | Free |
| CallMeBot WhatsApp | Free |
| Claude Sonnet (AI) | ~₹2–5 per qualifying stock assessed |
| GitHub Actions | Free (2,000 mins/month on free tier) |

**Total expected daily cost: ₹0–20** depending on how many stocks pass the quant filter.

---

## Troubleshooting

**"No symbols loaded"** → NSE website may be blocking the request. Run during IST market hours or try again.

**"No screener data"** → The stock ticker may differ on screener.in. Check manually at `screener.in/company/SYMBOL/`.

**"WhatsApp not received"** → Verify your CallMeBot setup. Send the join message again if needed.

**"AI assessment failed"** → Check your Anthropic API key and account balance at `console.anthropic.com`.
