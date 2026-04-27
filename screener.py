"""
Nifty 500 Investment Screener  v2.0
────────────────────────────────────────────────────────────────────────────
Daily pipeline: yfinance → screener.in → Claude AI → WhatsApp

What's new in v2.0 vs v1.0:
  ✦ Simplified criteria  : 10 hard criteria + 2 soft flags (was 13 hard)
  ✦ Retry logic          : screener.in fetches retry up to 3× with backoff
  ✦ Alert dedup          : same stock never re-alerted within 30 days
  ✦ Watchlist            : live self-maintaining list, instant exit notices
  ✦ Near-miss log        : stocks failing ≤ 2 criteria tracked daily
  ✦ Daily summary        : heartbeat message every market day (even 0 alerts)
  ✦ Friday weekly report : near-misses digest + full portfolio performance
  ✦ SIP tracker          : ₹1L paper portfolio on 1st of each month
  ✦ Gold fallback        : GOLDBEES when no stocks qualify in a month
  ✦ Technical context    : 52W range, 200 DMA, RSI shown in alert (not a filter)
  ✦ Market share         : added to AI assessment JSON
────────────────────────────────────────────────────────────────────────────
"""

import csv
import io
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from anthropic import Anthropic
from bs4 import BeautifulSoup

import config as cfg

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("screener")

# ─── Constants ────────────────────────────────────────────────────────────────
NSE_NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
SCREENER_CONSOL  = "https://www.screener.in/company/{sym}/consolidated/"
SCREENER_ALONE   = "https://www.screener.in/company/{sym}/"

BFSI_KEYWORDS = {
    "bank", "insurance", "finance", "financial services", "nbfc",
    "asset management", "capital markets", "credit services",
    "mortgage", "microfinance", "housing finance", "brokerage",
    "wealth management", "money market",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer":         "https://www.screener.in/",
}

# Data file paths — all written to the repo root
ALERTS_LOG_PATH      = "alerts_log.json"       # {symbol: last_alert_date}
WATCHLIST_LOG_PATH   = "watchlist_log.json"    # {date_str: [symbols]}
NEAR_MISS_LOG_PATH   = "near_miss_log.csv"     # running CSV of near-misses
PERFORMANCE_LOG_PATH = "performance_log.json"  # SIP entries and prices

# Type alias
Criterion = Dict[str, Any]   # keys: value, pass, label


# ══════════════════════════════════════════════════════════════════════════════
# 1.  DATA PERSISTENCE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_json(path: str, default: Any) -> Any:
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load {path}: {e}")
    return default


def save_json(path: str, data: Any) -> None:
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Could not save {path}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 2.  UNIVERSE — Nifty 500 Symbols
# ══════════════════════════════════════════════════════════════════════════════

def fetch_nifty500() -> List[str]:
    """
    Returns the list of NSE symbols for all Nifty 500 constituents.
    Tries NSE archives first, falls back to nsepython.
    """
    session = requests.Session()
    try:
        session.get(
            "https://www.nseindia.com",
            headers={**HTTP_HEADERS, "Accept": "*/*"},
            timeout=10,
        )
        time.sleep(1)
        resp = session.get(
            NSE_NIFTY500_URL,
            headers={**HTTP_HEADERS, "Referer": "https://www.nseindia.com"},
            timeout=15,
        )
        resp.raise_for_status()
        df     = pd.read_csv(io.StringIO(resp.text))
        sym_col = next((c for c in df.columns if "symbol" in c.lower()), None)
        if sym_col:
            symbols = df[sym_col].dropna().str.strip().tolist()
            log.info(f"Loaded {len(symbols)} symbols from NSE archives")
            return symbols
    except Exception as e:
        log.warning(f"NSE archives fetch failed ({e}). Trying nsepython fallback…")

    try:
        from nsepython import nse_eq_symbols  # type: ignore
        syms = nse_eq_symbols()
        log.info(f"Loaded {len(syms)} symbols via nsepython")
        return syms
    except Exception as e:
        log.error(f"nsepython fallback also failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 3.  PASS 1 — yfinance Quick Filter
# ══════════════════════════════════════════════════════════════════════════════

def _is_bfsi(sector: str, industry: str) -> bool:
    combined = f"{sector} {industry}".lower()
    return any(k in combined for k in BFSI_KEYWORDS)


def pass1_yfinance(symbols: List[str]) -> List[Dict]:
    """
    Fast first pass using yfinance.
    Keeps: market cap ≥ 500 Cr  AND  not BFSI.
    Returns list of basic info dicts for survivors.
    """
    survivors = []
    for i, sym in enumerate(symbols, 1):
        try:
            info = yf.Ticker(f"{sym}.NS").info
            if not info or not info.get("regularMarketPrice"):
                continue

            sector   = info.get("sector",   "") or ""
            industry = info.get("industry", "") or ""

            if _is_bfsi(sector, industry):
                continue

            mc_cr = (info.get("marketCap") or 0) / 1e7   # rupees → crores
            if mc_cr < cfg.MIN_MARKET_CAP_CR:
                continue

            pe = info.get("trailingPE") or info.get("forwardPE")
            if pe and (pe <= 0 or pe > 200):
                pe = None

            survivors.append({
                "symbol":        sym,
                "name":          info.get("longName", sym),
                "sector":        sector,
                "industry":      industry,
                "market_cap_cr": round(mc_cr, 1),
                "pe_yf":         round(pe, 2) if pe else None,
            })

            if i % 50 == 0:
                log.info(f"  Pass 1: {i}/{len(symbols)} checked | {len(survivors)} alive")

            time.sleep(0.4)

        except Exception as e:
            log.debug(f"yfinance {sym}: {e}")

    log.info(f"Pass 1 complete → {len(survivors)}/{len(symbols)} survived")
    return survivors


# ══════════════════════════════════════════════════════════════════════════════
# 4.  PASS 2 — screener.in Parsing
# ══════════════════════════════════════════════════════════════════════════════

def _soup_with_retry(sym: str, retries: int = 3, backoff: float = 4.0) -> Optional[BeautifulSoup]:
    """
    Fetch screener.in page with exponential backoff retry.
    Tries consolidated view first, then standalone.
    Gives up only after `retries` consecutive failures on both URLs.
    """
    for attempt in range(retries):
        for url in [SCREENER_CONSOL.format(sym=sym), SCREENER_ALONE.format(sym=sym)]:
            try:
                r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
                if r.status_code == 200 and "company" in r.url:
                    return BeautifulSoup(r.text, "html.parser")
            except Exception as e:
                log.debug(f"  Attempt {attempt + 1} failed for {sym} ({url}): {e}")

        if attempt < retries - 1:
            wait = backoff * (2 ** attempt)   # 4s, 8s, 16s
            log.debug(f"  Retrying {sym} in {wait:.0f}s…")
            time.sleep(wait)

    log.warning(f"  Could not fetch screener.in for {sym} after {retries} attempts")
    return None


def _num(text: str) -> Optional[float]:
    """Parse scraped strings like '₹1,234 Cr.' or '23.5%' into float."""
    if not text:
        return None
    t = re.sub(r"[₹,%\s]", "", str(text)).replace(",", "")
    t = re.sub(r"Cr\.?", "", t).strip()
    if "/" in t:
        t = t.split("/")[0].strip()
    try:
        return float(t)
    except (ValueError, TypeError):
        return None


def _parse_ratios(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract the key ratios box at the top of a screener.in page."""
    out: Dict[str, str] = {}
    box = soup.find(id="top-ratios")
    if not box:
        return out
    for li in box.find_all("li"):
        n = li.find(class_="name")
        v = li.find(class_="number")
        if n and v:
            out[n.text.strip().rstrip("+").strip()] = v.text.strip()
    return out


def _parse_table(
    soup: BeautifulSoup, section_id: str
) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """
    Parse a screener.in financial table (profit-loss, balance-sheet, cash-flow).
    Returns (row_dict, year_list) where row_dict[row_name][year] = raw string.
    """
    data:  Dict[str, Dict[str, str]] = {}
    years: List[str] = []

    sec = soup.find(id=section_id)
    if not sec:
        return data, years
    tbl = sec.find("table")
    if not tbl:
        return data, years

    thead = tbl.find("thead")
    if thead:
        years = [th.text.strip() for th in thead.find_all("th")][1:]

    tbody = tbl.find("tbody")
    if tbody:
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            row_name = cells[0].text.strip().rstrip("+").strip()
            if row_name:
                data[row_name] = {
                    years[i]: cells[i + 1].text.strip().replace(",", "")
                    for i in range(min(len(years), len(cells) - 1))
                }
    return data, years


def _parse_shareholding(soup: BeautifulSoup) -> Dict[str, Optional[float]]:
    """Extract promoter holding, pledge %, FII, DII from the shareholding section."""
    result: Dict[str, Optional[float]] = {
        "promoter_holding": None,
        "promoter_pledge":  None,
    }

    sec = soup.find(id="shareholding")
    if not sec:
        return result

    for tbl in sec.find_all("table"):
        tbody = tbl.find("tbody")
        if not tbody:
            continue
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            label  = cells[0].text.strip().lower()
            vals   = [c.text.strip() for c in cells[1:] if c.text.strip()]
            if not vals:
                continue
            latest = _num(vals[-1])   # most recent quarter = last column

            if label == "promoters" or (
                label.startswith("promoter") and "pledge" not in label
            ):
                result["promoter_holding"] = latest
            elif "pledge" in label or "pledged" in label:
                result["promoter_pledge"] = latest

    return result


def _series(
    table: Dict[str, Dict[str, str]], years: List[str], *row_names: str
) -> List[Optional[float]]:
    """Extract a numeric time series trying multiple row-name variants."""
    row: Dict[str, str] = {}
    for name in row_names:
        if name in table:
            row = table[name]
            break
    return [_num(row.get(y, "")) for y in years]


def _cagr(series: List[Optional[float]], n: int = 5) -> Optional[float]:
    """
    Calculate CAGR from a time series.
    Uses the last n+1 valid positive values.
    """
    valid = [(i, v) for i, v in enumerate(series) if v is not None and v > 0]
    if len(valid) < 2:
        return None
    n = min(n, len(valid) - 1)
    start, end = valid[-(n + 1)][1], valid[-1][1]
    if start <= 0:
        return None
    return ((end / start) ** (1 / n) - 1) * 100


def get_fundamentals(sym: str) -> Optional[Dict]:
    """
    Scrape screener.in for `sym` and return a dict of all metrics
    needed for the quantitative filter and AI assessment.
    Returns None if the page cannot be fetched.
    """
    page = _soup_with_retry(sym)
    if not page:
        return None

    # ── Key ratios box ─────────────────────────────────────────────────────
    ratios = _parse_ratios(page)

    # ── Financial tables ───────────────────────────────────────────────────
    pl, pl_yrs = _parse_table(page, "profit-loss")
    bs, bs_yrs = _parse_table(page, "balance-sheet")
    cf, cf_yrs = _parse_table(page, "cash-flow")

    # ── Shareholding ───────────────────────────────────────────────────────
    holding = _parse_shareholding(page)

    # ── Time series ────────────────────────────────────────────────────────
    sales   = _series(pl, pl_yrs, "Sales")
    net_p   = _series(pl, pl_yrs, "Net Profit")
    eps     = _series(pl, pl_yrs, "EPS in Rs")
    op_prof = _series(pl, pl_yrs, "Operating Profit")

    equity  = _series(bs, bs_yrs, "Equity Capital")
    reserv  = _series(bs, bs_yrs, "Reserves")
    borrow  = _series(bs, bs_yrs, "Borrowings")
    inv     = _series(bs, bs_yrs, "Inventories", "Inventory")
    debtor  = _series(bs, bs_yrs, "Debtors", "Trade Receivables", "Accounts Receivable")

    cfo     = _series(cf, cf_yrs, "Cash from Operating Activity", "Operating Activity")
    cfi     = _series(cf, cf_yrs, "Cash from Investing Activity", "Investing Activity")

    # ── Derived: ROCE per year ──────────────────────────────────────────────
    # ROCE = Operating Profit / Capital Employed
    # Capital Employed = Equity Capital + Reserves + Borrowings
    roce_hist: List[Optional[float]] = []
    for o, e, r, b in zip(op_prof, equity, reserv, borrow):
        if o is not None and e is not None:
            cap = (e or 0) + (r or 0) + (b or 0)
            roce_hist.append(round((o / cap) * 100, 1) if cap > 0 else None)
        else:
            roce_hist.append(None)

    # ── Derived: 3-year average ROCE ───────────────────────────────────────
    roce_valid = [v for v in roce_hist[-3:] if v is not None]
    roce_3y_avg = round(sum(roce_valid) / len(roce_valid), 1) if roce_valid else None

    # ── Derived: Debt / Equity ─────────────────────────────────────────────
    de = _num(ratios.get("Debt to equity", ""))
    if de is None:
        b_lat = next((v for v in reversed(borrow) if v is not None), None)
        e_lat = next((v for v in reversed(equity) if v is not None), None)
        r_lat = next((v for v in reversed(reserv) if v is not None), None)
        if b_lat is not None and e_lat is not None:
            eq_total = (e_lat or 0) + (r_lat or 0)
            de = round(b_lat / eq_total, 2) if eq_total > 0 else None

    # ── Derived: Growth CAGRs ──────────────────────────────────────────────
    eps_g5  = _cagr(eps, 5)
    rev_g5  = _cagr(sales, 5)
    rev_g3  = _cagr(sales, 3)
    inv_g3  = _cagr(inv, 3)
    rec_g3  = _cagr(debtor, 3)

    # Inventory growth relative to revenue growth (3Y)
    inv_ratio = (
        round(inv_g3 / rev_g3, 2)
        if inv_g3 is not None and rev_g3 and rev_g3 > 0
        else None
    )
    # Receivables growth relative to revenue growth (3Y)
    rec_ratio = (
        round(rec_g3 / rev_g3, 2)
        if rec_g3 is not None and rev_g3 and rev_g3 > 0
        else None
    )

    # ── Derived: FCF / PAT (5Y average) ───────────────────────────────────
    # FCF = CFO + CFI  (CFI is typically negative — capex outflow)
    fcf_pat_list: List[float] = []
    for o, fi, p in zip(cfo, cfi, net_p):
        if o is not None and fi is not None and p and p > 0:
            fcf_pat_list.append((o + fi) / p)
    fcf_to_pat = (
        round(sum(fcf_pat_list) / len(fcf_pat_list), 2) if fcf_pat_list else None
    )

    # ── P/E and PEG ────────────────────────────────────────────────────────
    pe  = _num(ratios.get("Stock P/E", ""))
    peg = (
        round(pe / eps_g5, 2)
        if pe and eps_g5 and eps_g5 > 0
        else None
    )

    # ── Company narrative (for AI prompt) ──────────────────────────────────
    name_el  = page.find("h1")
    about_el = page.find(id="about")

    return {
        "company_name":      name_el.text.strip() if name_el else sym,
        "about":             about_el.get_text(" ", strip=True)[:1500] if about_el else "",

        # Hard-filter metrics
        "roe":               _num(ratios.get("Return on equity", "")),
        "de_ratio":          de,
        "pe_ratio":          pe,
        "peg_ratio":         peg,
        "eps_growth_5y":     round(eps_g5, 1)  if eps_g5  else None,
        "revenue_growth_5y": round(rev_g5, 1)  if rev_g5  else None,
        "roce_3y_avg":       roce_3y_avg,
        "roce_history":      roce_hist[-6:],    # kept for display in alert
        "fcf_to_pat":        fcf_to_pat,
        "promoter_holding":  holding["promoter_holding"],
        "promoter_pledge":   holding["promoter_pledge"],

        # Soft-flag metrics
        "inventory_ratio":   inv_ratio,
        "receivables_ratio": rec_ratio,

        # Raw series for formatting
        "_sales_cr":         [round(v, 0) if v else None for v in sales[-5:]],
        "_eps":              eps[-5:],
        "_roce":             roce_hist[-5:],
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5.  PASS 2 — Quantitative Filter
# ══════════════════════════════════════════════════════════════════════════════

def _check(value: Any, condition: bool, label: str) -> Criterion:
    return {"value": value, "pass": condition, "label": label}


def quant_filter(
    data: Dict, basic: Dict
) -> Tuple[bool, Dict[str, Criterion], List[str]]:
    """
    Apply all 10 hard quantitative criteria.
    Also evaluates 2 soft flags (inventory, receivables).

    Returns:
        all_passed  : True if every hard criterion passes
        criteria    : per-criterion results (value, pass, label)
        soft_flags  : list of warning strings for soft flag violations
    """
    r: Dict[str, Criterion] = {}

    mc = basic["market_cap_cr"]
    r["Market Cap"] = _check(mc, mc >= cfg.MIN_MARKET_CAP_CR, f"₹{mc:,.0f} Cr")

    de = data["de_ratio"]
    r["Debt / Equity"] = _check(
        de, de is not None and 0 <= de <= cfg.MAX_DE_RATIO,
        f"{de:.2f}" if de is not None else "N/A",
    )

    roe = data["roe"]
    r["ROE"] = _check(
        roe, roe is not None and roe >= cfg.MIN_ROE,
        f"{roe:.1f}%" if roe is not None else "N/A",
    )

    rg = data["revenue_growth_5y"]
    r["Revenue CAGR 5Y"] = _check(
        rg, rg is not None and rg >= cfg.MIN_REVENUE_GROWTH_5Y,
        f"{rg:.1f}%" if rg is not None else "N/A",
    )

    roce_avg = data["roce_3y_avg"]
    r["Avg ROCE (3Y)"] = _check(
        roce_avg, roce_avg is not None and roce_avg >= cfg.MIN_ROCE_3Y_AVG,
        f"{roce_avg:.1f}%  (hist: {data['_roce']})" if roce_avg is not None else "N/A",
    )

    fp = data["fcf_to_pat"]
    r["FCF / PAT"] = _check(
        fp, fp is not None and fp >= cfg.MIN_FCF_TO_PAT,
        f"{fp:.2f}" if fp is not None else "N/A",
    )

    ph = data["promoter_holding"]
    r["Promoter Holding"] = _check(
        ph, ph is not None and ph >= cfg.MIN_PROMOTER_HOLDING,
        f"{ph:.1f}%" if ph is not None else "N/A",
    )

    pp = data["promoter_pledge"]
    r["Promoter Pledge"] = _check(
        pp, pp is None or pp <= cfg.MAX_PROMOTER_PLEDGE,
        f"{pp:.1f}%" if pp is not None else "0% (nil)",
    )

    peg = data["peg_ratio"]
    r["PEG Ratio"] = _check(
        peg, peg is not None and peg <= cfg.MAX_PEG_RATIO,
        f"{peg:.2f}" if peg is not None else "N/A",
    )

    # ── Soft flags ─────────────────────────────────────────────────────────
    soft_flags: List[str] = []

    ir = data["inventory_ratio"]
    if ir is not None and ir > cfg.MAX_INVENTORY_RATIO:
        soft_flags.append(
            f"Inventory growing {ir:.2f}x revenue — possible stock buildup ⚠️"
        )

    rr = data["receivables_ratio"]
    if rr is not None and rr > cfg.MAX_RECEIVABLES_RATIO:
        soft_flags.append(
            f"Receivables growing {rr:.2f}x revenue — watch debtor days ⚠️"
        )

    all_pass = all(v["pass"] for v in r.values())
    return all_pass, r, soft_flags


# ══════════════════════════════════════════════════════════════════════════════
# 6.  PASS 3 — AI Business Model Assessment (Claude)
# ══════════════════════════════════════════════════════════════════════════════

def ai_assess(sym: str, data: Dict, basic: Dict) -> Dict:
    """
    Call Claude to evaluate the business model against the investment philosophy.
    Returns a structured dict with score, verdict, market share, and reasoning.
    """
    client = Anthropic(api_key=cfg.ANTHROPIC_API_KEY)

    prompt = f"""You are evaluating whether a stock fits a specific long-term investment philosophy.

COMPANY : {data['company_name']}  (NSE: {sym})
SECTOR  : {basic['sector']}
INDUSTRY: {basic['industry']}
ABOUT   : {data['about'] or 'Not available on screener.in'}

QUANTITATIVE SNAPSHOT (all hard criteria already passed):
  • Market Cap      : ₹{basic['market_cap_cr']:,.0f} Cr
  • ROE             : {data['roe']}%
  • D/E Ratio       : {data['de_ratio']}
  • Revenue CAGR 5Y : {data['revenue_growth_5y']}%
  • Avg ROCE (3Y)   : {data['roce_3y_avg']}%
  • ROCE History    : {data['_roce']}
  • FCF/PAT         : {data['fcf_to_pat']}
  • Promoter Hold   : {data['promoter_holding']}%

INVESTMENT PHILOSOPHY — assess strictly against these principles:
1. The majority of revenue comes from identifiable, tangible products or services.
2. The business has a clear competitive moat (pricing power, switching costs,
   niche dominance, brand, cost advantage, or network effects).
3. The company holds a meaningful, defensible market share in its core segment —
   ideally a top-3 player with a share that is stable or growing.
4. The core products/services face LOW risk of technological or regulatory
   disruption over the next 15 years.
5. Demand for the company's main offerings is likely to GROW or remain STABLE
   through 2040, driven by structural tailwinds (demographics, urbanisation,
   infrastructure, essential consumption etc.).
6. The business model is simple and understandable — not dependent on regulatory
   arbitrage, financial leverage, or complex instruments.
7. Management runs the business conservatively, evidenced by the quant metrics above.

YOUR TASK:
Evaluate this company against the above philosophy and return ONLY a valid JSON object:
{{
  "score"                  : <float 1–10, where 10 = perfect fit>,
  "verdict"                : <"STRONG PASS" | "PASS" | "BORDERLINE" | "FAIL">,
  "primary_revenue_source" : "<one sentence — where does most money come from>",
  "cash_cow_products"      : "<the 1–2 key products or services driving profitability>",
  "moat_type"              : "<pricing power | switching costs | network effects | cost advantage | niche dominance | brand | none>",
  "market_share"           : "<estimated market position, e.g. '#1 in CPVC pipes ~30% share' or 'top-3 in industrial wires, ~18% share'>",
  "demand_outlook_15y"     : "<GROWING | STABLE | UNCERTAIN | DECLINING>",
  "disruption_risk"        : "<LOW | MEDIUM | HIGH>",
  "key_risks"              : "<2 specific risks to monitor, comma-separated>",
  "reasoning"              : "<3–4 sentences explaining the verdict>"
}}

Scoring guide:
  9–10 : Exceptional durable compounder, near-perfect fit
  7–8  : Strong business with minor caveats
  5–6  : Decent but meaningful concerns — borderline
  3–4  : Significant issues with durability or moat
  1–2  : Does not fit the philosophy at all

Return ONLY the JSON. No text before or after."""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        text = re.sub(r"```json?\s*", "", text).replace("```", "").strip()
        return json.loads(text)

    except json.JSONDecodeError as e:
        log.error(f"JSON parse error for {sym}: {e}\nRaw: {text[:300]}")
        return {"score": 0, "verdict": "FAIL", "reasoning": f"JSON parse error: {e}"}
    except Exception as e:
        log.error(f"AI assessment failed for {sym}: {e}")
        return {"score": 0, "verdict": "FAIL", "reasoning": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# 7.  TECHNICAL DATA  (informational only — not a filter)
# ══════════════════════════════════════════════════════════════════════════════

def get_technical_data(sym: str) -> Dict:
    """
    Fetch 52-week range, 200-day DMA, and 14-day RSI from yfinance.
    Returns an empty dict on failure — caller handles gracefully.
    """
    try:
        ticker = yf.Ticker(f"{sym}.NS")

        # 1-year history for 52W range and RSI
        hist_1y = ticker.history(period="1y")
        if hist_1y.empty:
            return {}

        current  = hist_1y["Close"].iloc[-1]
        high_52w = hist_1y["Close"].max()
        low_52w  = hist_1y["Close"].min()
        pct_above_low = ((current - low_52w) / low_52w) * 100

        # 14-month history to get a reliable 200-day DMA
        hist_14m = ticker.history(period="14mo")
        dma_200  = None
        above_200dma = None
        if len(hist_14m) >= 200:
            dma_200      = round(hist_14m["Close"].rolling(200).mean().iloc[-1], 2)
            above_200dma = bool(current > dma_200)

        # RSI (14-day)
        close  = hist_1y["Close"].tail(15)
        delta  = close.diff().dropna()
        gain   = delta.clip(lower=0).mean()
        loss   = (-delta.clip(upper=0)).mean()
        rsi    = round(100 - (100 / (1 + gain / loss)), 1) if loss > 0 else 100.0

        return {
            "current":       round(current, 2),
            "high_52w":      round(high_52w, 2),
            "low_52w":       round(low_52w, 2),
            "pct_above_low": round(pct_above_low, 1),
            "dma_200":       dma_200,
            "above_200dma":  above_200dma,
            "rsi":           rsi,
        }
    except Exception as e:
        log.debug(f"Technical data failed for {sym}: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# 8.  WATCHLIST MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def get_previous_watchlist(wl_log: Dict, today_str: str) -> List[str]:
    """Return the most recent watchlist snapshot before today (up to 7 days back)."""
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    for i in range(1, 8):
        prev = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if prev in wl_log:
            return wl_log[prev]
    return []


def get_days_on_watchlist(wl_log: Dict, sym: str, today_str: str) -> int:
    """Count consecutive days sym has been on the watchlist up to and including today."""
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    count    = 0
    for i in range(365):   # look back max 1 year
        d = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if sym in wl_log.get(d, []):
            count += 1
        elif count > 0:
            break   # consecutive streak broken
    return count


def get_entry_date(wl_log: Dict, sym: str, today_str: str) -> Optional[str]:
    """Find the first date sym appeared in the current consecutive streak."""
    today_dt = datetime.strptime(today_str, "%Y-%m-%d").date()
    entry    = None
    for i in range(365):
        d = (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        if sym in wl_log.get(d, []):
            entry = d
        elif entry is not None:
            break
    return entry


def get_30day_eligible(wl_log: Dict, reference_date: date) -> Dict[str, int]:
    """
    Returns {symbol: days_appeared} for every stock that appeared on the
    watchlist at least once in the 30 days prior to reference_date.
    Used by the SIP tracker to decide which stocks to invest in.
    """
    counts: Dict[str, int] = {}
    for i in range(30):
        d = (reference_date - timedelta(days=i + 1)).strftime("%Y-%m-%d")
        for sym in wl_log.get(d, []):
            counts[sym] = counts.get(sym, 0) + 1
    return counts


# ══════════════════════════════════════════════════════════════════════════════
# 9.  NEAR-MISS LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def log_near_miss(
    sym: str, name: str, failed: List[Tuple[str, str]], date_str: str
) -> None:
    """Append a near-miss entry to the running CSV log."""
    file_exists = os.path.exists(NEAR_MISS_LOG_PATH)
    with open(NEAR_MISS_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["date", "symbol", "name", "failed_count", "criterion", "detail"])
        for criterion, detail in failed:
            writer.writerow([date_str, sym, name, len(failed), criterion, detail])


def get_weekly_near_misses(today: date) -> List[Dict]:
    """
    Read all near-miss entries from the past 7 days.
    Deduplicates by symbol — shows the most recent entry per stock.
    """
    if not os.path.exists(NEAR_MISS_LOG_PATH):
        return []

    cutoff  = today - timedelta(days=7)
    results: Dict[str, Dict] = {}

    try:
        with open(NEAR_MISS_LOG_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    row_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
                    if row_date < cutoff:
                        continue
                    sym = row["symbol"]
                    if sym not in results or row_date > datetime.strptime(
                        results[sym]["date"], "%Y-%m-%d"
                    ).date():
                        results[sym] = {
                            "date":         row["date"],
                            "symbol":       sym,
                            "name":         row["name"],
                            "failed_count": int(row["failed_count"]),
                            "criteria":     [],
                        }
                    results[sym]["criteria"].append(
                        (row["criterion"], row["detail"])
                    )
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"Could not read near-miss log: {e}")

    return sorted(results.values(), key=lambda x: x["failed_count"])


# ══════════════════════════════════════════════════════════════════════════════
# 10. ALERT DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════════════

def was_recently_alerted(sym: str, alerts_log: Dict) -> bool:
    """Return True if sym was alerted within the cooldown window."""
    if sym not in alerts_log:
        return False
    try:
        last = datetime.strptime(alerts_log[sym], "%Y-%m-%d").date()
        return (date.today() - last).days < cfg.ALERT_COOLDOWN_DAYS
    except Exception:
        return False


def mark_alerted(sym: str, alerts_log: Dict, today_str: str) -> None:
    """Record today as the last alert date for sym."""
    alerts_log[sym] = today_str


# ══════════════════════════════════════════════════════════════════════════════
# 11. SIP PAPER PORTFOLIO TRACKER
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_price(sym: str, target_date: date) -> Optional[float]:
    """
    Fetch the closing price on or just after target_date.
    Handles weekends and NSE holidays by scanning up to 7 days forward.
    """
    try:
        ticker_sym = (
            sym if sym.startswith("^")
            else f"{sym}.NS"
        )
        hist = yf.Ticker(ticker_sym).history(
            start=target_date.strftime("%Y-%m-%d"),
            end=(target_date + timedelta(days=7)).strftime("%Y-%m-%d"),
        )
        if not hist.empty:
            return round(float(hist["Close"].iloc[0]), 2)
    except Exception as e:
        log.debug(f"Price fetch failed for {sym} on {target_date}: {e}")
    return None


def _fetch_current_price(sym: str) -> Optional[float]:
    """Fetch the latest closing price for a symbol."""
    try:
        ticker_sym = sym if sym.startswith("^") else f"{sym}.NS"
        hist = yf.Ticker(ticker_sym).history(period="5d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
    except Exception:
        pass
    return None


def calculate_monthly_sip(wl_log: Dict, perf_log: Dict, today: date) -> None:
    """
    Called on the 1st of each month.

    Logic:
      1. Look back 30 days at watchlist_log for eligible stocks.
      2. If stocks found → split ₹1L equally among all of them.
      3. If no stocks found → put ₹1L into GOLDBEES (gold fallback).
      4. Record benchmark (Nifty 50) price for later comparison.
      5. Append entry to performance_log.json.
    """
    month_key = today.strftime("%Y-%m")

    # Don't double-process the same month
    existing_months = [e["month"] for e in perf_log.get("sip_entries", [])]
    if month_key in existing_months:
        log.info(f"SIP already recorded for {month_key} — skipping")
        return

    eligible = get_30day_eligible(wl_log, today)   # {sym: days_appeared}
    benchmark_price = _fetch_price(cfg.BENCHMARK_TICKER, today)

    if not eligible:
        # Gold fallback
        gold_price = _fetch_price(cfg.GOLD_ETF_SYMBOL, today)
        shares     = cfg.SIP_AMOUNT_INR / gold_price if gold_price else 0
        entry = {
            "month":                month_key,
            "date":                 today.strftime("%Y-%m-%d"),
            "type":                 "gold_fallback",
            "reason":               "No stocks qualified in the prior 30 days",
            "allocations": [{
                "symbol":      cfg.GOLD_ETF_SYMBOL,
                "name":        "Nippon India Gold ETF (GOLDBEES)",
                "price_entry": gold_price,
                "shares":      round(shares, 4),
            }],
            "amount_per_stock":      cfg.SIP_AMOUNT_INR,
            "total_deployed":        cfg.SIP_AMOUNT_INR,
            "benchmark_price_entry": benchmark_price,
        }
        log.info(f"SIP {month_key}: gold fallback @ ₹{gold_price}")
    else:
        symbols      = list(eligible.keys())
        amount_each  = cfg.SIP_AMOUNT_INR / len(symbols)
        allocations  = []

        for sym in symbols:
            price = _fetch_price(sym, today)
            if price and price > 0:
                allocations.append({
                    "symbol":       sym,
                    "price_entry":  price,
                    "shares":       round(amount_each / price, 4),
                    "days_on_list": eligible[sym],
                })

        entry = {
            "month":                 month_key,
            "date":                  today.strftime("%Y-%m-%d"),
            "type":                  "stocks",
            "allocations":           allocations,
            "amount_per_stock":      round(amount_each, 2),
            "total_deployed":        cfg.SIP_AMOUNT_INR,
            "benchmark_price_entry": benchmark_price,
        }
        log.info(
            f"SIP {month_key}: ₹{amount_each:,.0f} each "
            f"into {[a['symbol'] for a in allocations]}"
        )

    perf_log.setdefault("sip_entries", []).append(entry)
    save_json(PERFORMANCE_LOG_PATH, perf_log)


def get_portfolio_performance(perf_log: Dict) -> List[Dict]:
    """
    Calculate current value and return for every SIP entry.
    Fetches live prices from yfinance.
    """
    results = []
    benchmark_current = _fetch_current_price(cfg.BENCHMARK_TICKER)

    for entry in perf_log.get("sip_entries", []):
        result = {
            "month":            entry["month"],
            "date":             entry["date"],
            "type":             entry["type"],
            "total_deployed":   entry["total_deployed"],
            "allocations":      [],
            "total_current":    0.0,
            "total_return_pct": None,
            "bench_return_pct": None,
            "alpha":            None,
        }

        # Benchmark return
        b_entry = entry.get("benchmark_price_entry")
        if b_entry and benchmark_current:
            result["bench_return_pct"] = round(
                ((benchmark_current - b_entry) / b_entry) * 100, 2
            )

        total_current = 0.0
        for alloc in entry.get("allocations", []):
            sym          = alloc["symbol"]
            curr_price   = _fetch_current_price(sym)
            entry_price  = alloc.get("price_entry")
            shares       = alloc.get("shares", 0)

            if curr_price and entry_price and shares:
                curr_value = shares * curr_price
                pct_change = ((curr_price - entry_price) / entry_price) * 100
                result["allocations"].append({
                    **alloc,
                    "price_current": curr_price,
                    "current_value": round(curr_value, 2),
                    "pct_change":    round(pct_change, 2),
                })
                total_current += curr_value
            else:
                result["allocations"].append({**alloc, "price_current": None})

        if total_current > 0:
            result["total_current"]    = round(total_current, 2)
            result["total_return_pct"] = round(
                ((total_current - entry["total_deployed"]) / entry["total_deployed"]) * 100, 2
            )
            if result["bench_return_pct"] is not None:
                result["alpha"] = round(
                    result["total_return_pct"] - result["bench_return_pct"], 2
                )

        results.append(result)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 12. MESSAGE FORMATTING
# ══════════════════════════════════════════════════════════════════════════════

def _score_bar(score: float, width: int = 10) -> str:
    filled = max(0, min(width, round(score)))
    return "█" * filled + "░" * (width - filled)


def format_stock_alert(
    basic:      Dict,
    data:       Dict,
    criteria:   Dict[str, Criterion],
    soft_flags: List[str],
    ai:         Dict,
    tech:       Dict,
) -> str:
    """Format the full WhatsApp stock alert for a new watchlist entry."""
    verdict      = ai.get("verdict", "N/A")
    score        = ai.get("score", 0)
    verdict_icon = {
        "STRONG PASS": "🟢",
        "PASS":        "✅",
        "BORDERLINE":  "🟡",
        "FAIL":        "🔴",
    }.get(verdict, "⚪")

    # Hard criteria lines (all passed — only passed ones shown)
    crit_lines = "\n".join(
        f"  ✅ {k}: {v['label']}"
        for k, v in criteria.items() if v["pass"]
    )

    # Soft flags block
    soft_block = ""
    if soft_flags:
        flag_lines  = "\n".join(f"  • {f}" for f in soft_flags)
        soft_block  = f"\n\n  *Soft Flags (for your judgement):*\n{flag_lines}"

    # Technical context block
    tech_block = ""
    if tech:
        dma_icon  = "✅" if tech.get("above_200dma") else (
                    "❌" if tech.get("above_200dma") is False else "—")
        rsi       = tech.get("rsi", "—")
        rsi_label = (
            "overbought 🔴" if isinstance(rsi, float) and rsi > 70
            else "oversold 🟢" if isinstance(rsi, float) and rsi < 30
            else "neutral"
        )
        dma_str = f"₹{tech['dma_200']:,.0f}  ({dma_icon})" if tech.get("dma_200") else "N/A"
        tech_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 *TECHNICAL CONTEXT* _(not a filter — for reference)_\n\n"
            f"  52W Range:   ₹{tech['low_52w']:,.0f} – ₹{tech['high_52w']:,.0f}\n"
            f"  Current:     ₹{tech['current']:,.0f}  "
            f"({tech['pct_above_low']:.0f}% above 52W low)\n"
            f"  200-day DMA: {dma_str}\n"
            f"  RSI (14d):   {rsi}  ({rsi_label})"
        )

    market_share_line = (
        f"📊 Market Share:  {ai.get('market_share', 'N/A')}\n"
        if ai.get("market_share") else ""
    )

    return (
        f"📈 *STOCK ALERT — New Watchlist Entry*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*{data['company_name']}* ({basic['symbol']})\n"
        f"🏭 {basic['sector']}  |  ₹{basic['market_cap_cr']:,.0f} Cr\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *QUANTITATIVE SCORECARD*\n\n"
        f"{crit_lines}"
        f"{soft_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 *AI BUSINESS MODEL ASSESSMENT*\n\n"
        f"{verdict_icon} Verdict: *{verdict}*\n"
        f"📊 Score: *{score}/10*  [{_score_bar(score)}]\n\n"
        f"💼 Revenue:        {ai.get('primary_revenue_source', 'N/A')}\n"
        f"⭐ Cash Cows:      {ai.get('cash_cow_products', 'N/A')}\n"
        f"🔒 Moat:           {ai.get('moat_type', 'N/A')}\n"
        f"{market_share_line}"
        f"📅 15Y Demand:     {ai.get('demand_outlook_15y', 'N/A')}\n"
        f"⚡ Disruption:     {ai.get('disruption_risk', 'N/A')}\n"
        f"🚨 Watch:          {ai.get('key_risks', 'N/A')}\n\n"
        f"_{ai.get('reasoning', '')}_"
        f"{tech_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Not a buy recommendation. Do your own due diligence._\n"
        f"🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
    )


def format_exit_notice(
    sym: str, name: str, failed: List[Tuple[str, str]],
    days_on_list: int, entry_date: Optional[str]
) -> str:
    """Format a watchlist exit notice."""
    fail_lines = "\n".join(f"  ❌ {k}: {v}" for k, v in failed)
    entry_str  = f"Entry date: {entry_date}" if entry_date else ""
    return (
        f"📤 *WATCHLIST EXIT*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"*{name}* ({sym}) has left the watchlist.\n\n"
        f"*Failed criteria:*\n{fail_lines}\n\n"
        f"Days on watchlist: {days_on_list}\n"
        f"{entry_str}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 13. WHATSAPP SENDER
# ══════════════════════════════════════════════════════════════════════════════

def send_whatsapp(text: str) -> bool:
    """
    Send a message via Telegram Bot API.
    Function kept as send_whatsapp so no other code needs to change.
    Supports Markdown formatting natively.
    """
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":    cfg.TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }, timeout=15)
        if r.status_code == 200:
            log.info("Telegram message sent ✅")
            return True
        log.error(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
    return False

# ══════════════════════════════════════════════════════════════════════════════
# 14. DAILY SUMMARY MESSAGE
# ══════════════════════════════════════════════════════════════════════════════

def send_daily_summary(
    run_stats:       Dict,
    today_watchlist: List[str],
    near_miss_today: List[Dict],
    wl_log:          Dict,
    today_str:       str,
) -> None:
    """
    Send a daily heartbeat summary.
    Includes run stats, active watchlist, and today's near-misses.
    Always sent — even on zero-alert days.
    """
    ts   = datetime.now().strftime("%a, %d %b %Y")
    day  = datetime.now().strftime("%A")

    # Watchlist section
    if today_watchlist:
        wl_lines = []
        for sym in today_watchlist:
            days = get_days_on_watchlist(wl_log, sym, today_str)
            wl_lines.append(f"  • {sym:<14} Day {days}")
        wl_block = (
            f"📌 *ACTIVE WATCHLIST*  ({len(today_watchlist)} stocks)\n\n"
            + "\n".join(wl_lines)
        )
    else:
        wl_block = "📌 *ACTIVE WATCHLIST*\n  (empty — no stocks currently qualify)"

    # Near-miss section (today only)
    nm_block = ""
    if near_miss_today:
        nm_lines = [
            f"  • {nm['symbol']} — failed {nm['failed_count']} "
            f"({', '.join(c for c, _ in nm['criteria'][:2])})"
            for nm in near_miss_today[:5]   # cap at 5 in daily summary
        ]
        nm_block = (
            f"\n\n⚠️ *TODAY'S NEAR-MISSES*  ({len(near_miss_today)} stocks)\n"
            + "\n".join(nm_lines)
            + ("\n  _(+ more — see Friday report)_" if len(near_miss_today) > 5 else "")
        )

    new_alerts = run_stats.get("alerts", 0)
    alert_note = (
        f"🔔 {new_alerts} new alert(s) sent above."
        if new_alerts > 0
        else "🔕 No new alerts today."
    )

    msg = (
        f"📊 *DAILY SCREENER — {ts}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔍 *Run Summary*\n"
        f"  Stocks in universe:    {run_stats.get('total', '—')}\n"
        f"  Passed size/BFSI:      {run_stats.get('pass1', '—')}\n"
        f"  Passed quant filter:   {run_stats.get('pass2', '—')}\n"
        f"  Passed AI assessment:  {run_stats.get('pass3_ok', '—')}\n"
        f"  {alert_note}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{wl_block}"
        f"{nm_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Not investment advice._  🕐 {datetime.now().strftime('%I:%M %p IST')}"
    )
    send_whatsapp(msg)


# ══════════════════════════════════════════════════════════════════════════════
# 15. WEEKLY FRIDAY REPORT
# ══════════════════════════════════════════════════════════════════════════════

def send_weekly_report(today: date, wl_log: Dict, perf_results: List[Dict]) -> None:
    """
    Friday-only report with three sections:
      A. Week in review (screening stats)
      B. Near-miss digest (all stocks that almost qualified this week)
      C. Paper portfolio performance (every SIP batch vs Nifty 50)
    """
    ts          = today.strftime("%A, %d %b %Y")
    near_misses = get_weekly_near_misses(today)

    # ── A. Near-miss digest ────────────────────────────────────────────────
    if near_misses:
        nm_sections = []
        for i, nm in enumerate(near_misses[:10], 1):   # cap at 10
            crit_lines = "\n".join(
                f"    ❌ {c}: {d}" for c, d in nm["criteria"]
            )
            nm_sections.append(
                f"{i}. *{nm['name']}* ({nm['symbol']}) "
                f"— failed {nm['failed_count']} criterion/a\n{crit_lines}"
            )
        nm_block = (
            f"⚠️ *NEAR-MISSES THIS WEEK*\n"
            f"_(Stocks that almost qualified — watch these)_\n\n"
            + "\n\n".join(nm_sections)
        )
    else:
        nm_block = "⚠️ *NEAR-MISSES THIS WEEK*\n  None this week."

    # ── B. Portfolio performance ───────────────────────────────────────────
    if not perf_results:
        port_block = (
            "💰 *PAPER PORTFOLIO*\n"
            "  No SIP entries yet. Will populate from the 1st of next month."
        )
    else:
        batch_sections = []
        total_deployed = 0.0
        total_current  = 0.0

        for res in sorted(perf_results, key=lambda x: x["month"], reverse=True):
            alloc_lines = []
            for a in res.get("allocations", []):
                sym   = a["symbol"]
                chg   = a.get("pct_change")
                entry = a.get("price_entry", "—")
                curr  = a.get("price_current", "—")
                chg_str = f"{chg:+.1f}%" if chg is not None else "pending"
                alloc_lines.append(f"    {sym:<12} ₹{entry} → ₹{curr}  {chg_str}")

            ret     = res.get("total_return_pct")
            bench   = res.get("bench_return_pct")
            alpha   = res.get("alpha")
            ret_str = f"{ret:+.1f}%" if ret is not None else "pending"

            if bench is not None and alpha is not None:
                vs_bench = f"vs Nifty50: {bench:+.1f}%  |  Alpha: {alpha:+.1f}%"
                vs_icon  = "✅" if alpha >= 0 else "❌"
            else:
                vs_bench = "benchmark pending"
                vs_icon  = "⏳"

            batch_type = (
                "🥇 Gold Fallback" if res["type"] == "gold_fallback" else "📈 Stocks"
            )

            batch_sections.append(
                f"─ *{res['month']} SIP* ({res['date']})  {batch_type}\n"
                + "\n".join(alloc_lines) + "\n"
                f"  Portfolio return: *{ret_str}*  {vs_icon}\n"
                f"  {vs_bench}"
            )

            total_deployed += res.get("total_deployed", 0)
            total_current  += res.get("total_current") or res.get("total_deployed", 0)

        # All-time summary
        abs_return = ((total_current - total_deployed) / total_deployed * 100
                      if total_deployed > 0 else 0)
        months     = len(perf_results)
        gold_months = sum(1 for r in perf_results if r["type"] == "gold_fallback")

        all_time = (
            f"─ *ALL-TIME SUMMARY* ({months} month{'s' if months != 1 else ''} tracked)\n"
            f"  Total deployed:  ₹{total_deployed:,.0f}\n"
            f"  Current value:   ₹{total_current:,.0f}\n"
            f"  Absolute return: {abs_return:+.1f}%\n"
            f"  Gold fallback months: {gold_months} / {months}\n"
            f"  _(Annualised CAGR unreliable until 12+ months of data)_"
        )

        port_block = (
            f"💰 *PAPER PORTFOLIO  (₹{cfg.SIP_AMOUNT_INR/100_000:.0f}L SIP on 1st)*\n"
            f"  Benchmark: Nifty 50 (^NSEI)\n\n"
            + "\n\n".join(batch_sections)
            + "\n\n" + all_time
        )

    msg = (
        f"📋 *WEEKLY REPORT — {ts}*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{nm_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{port_block}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ _Paper portfolio only. Not investment advice._\n"
        f"🕐 {datetime.now().strftime('%I:%M %p IST')}"
    )
    send_whatsapp(msg)


# ══════════════════════════════════════════════════════════════════════════════
# 16. MAIN ORCHESTRATOR
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")
    is_friday         = today.weekday() == 4
    is_first_of_month = today.day == 1

    log.info("=" * 60)
    log.info(f"Nifty 500 Screener v2.0  |  {today.strftime('%A, %d %b %Y')}")
    log.info("=" * 60)

    # ── Load persistent data ──────────────────────────────────────────────
    alerts_log = load_json(ALERTS_LOG_PATH,      {})
    wl_log     = load_json(WATCHLIST_LOG_PATH,   {})
    perf_log   = load_json(PERFORMANCE_LOG_PATH, {"sip_entries": []})

    # ── SIP on 1st of month (runs before screening so 30-day look-back is fresh)
    if is_first_of_month:
        log.info("\n▶ SIP Day — calculating monthly paper portfolio entry…")
        calculate_monthly_sip(wl_log, perf_log, today)

    # ── Step 1: Universe ──────────────────────────────────────────────────
    log.info("\n▶ Step 1  Loading Nifty 500 universe…")
    symbols = fetch_nifty500()
    if not symbols:
        log.error("Could not load Nifty 500 symbols. Aborting.")
        sys.exit(1)
    log.info(f"  {len(symbols)} symbols loaded")

    # ── Step 2: Pass 1 — yfinance ─────────────────────────────────────────
    log.info(f"\n▶ Step 2  Pass 1 — market cap + BFSI filter ({len(symbols)} stocks)…")
    p1 = pass1_yfinance(symbols)
    if not p1:
        log.info("No stocks survived Pass 1. Exiting.")
        return
    log.info(f"  {len(p1)} stocks qualified")

    # Track previous watchlist so we can detect exits
    prev_watchlist     = get_previous_watchlist(wl_log, today_str)
    prev_watchlist_set = set(prev_watchlist)
    p1_symbols_set     = {b["symbol"] for b in p1}

    # Stocks that were on yesterday's watchlist but didn't even survive Pass 1
    # (e.g. market cap dropped, sector reclassified) — immediate exit notice
    for sym in prev_watchlist_set - p1_symbols_set:
        days  = get_days_on_watchlist(wl_log, sym, today_str)
        entry = get_entry_date(wl_log, sym, today_str)
        msg   = format_exit_notice(sym, sym, [("Pass 1", "Market cap or BFSI filter")],
                                   days, entry)
        send_whatsapp(msg)
        time.sleep(3)

    # ── Step 3: Pass 2 — screener.in ─────────────────────────────────────
    log.info(f"\n▶ Step 3  Pass 2 — quantitative screen ({len(p1)} stocks)…")

    qualified: List[Tuple[Dict, Dict, Dict[str, Criterion], List[str]]] = []
    near_miss_today: List[Dict] = []

    for idx, basic in enumerate(p1, 1):
        sym = basic["symbol"]
        log.info(f"  [{idx:>3}/{len(p1)}] {sym}…")

        try:
            data = get_fundamentals(sym)
            if not data:
                log.debug(f"  No screener data for {sym}")
                time.sleep(2.5)
                continue

            # Back-fill P/E from yfinance if screener missed it
            if not data["pe_ratio"] and basic.get("pe_yf"):
                data["pe_ratio"] = basic["pe_yf"]
                if data.get("eps_growth_5y") and data["eps_growth_5y"] > 0:
                    data["peg_ratio"] = round(
                        data["pe_ratio"] / data["eps_growth_5y"], 2
                    )

            passed, criteria, soft_flags = quant_filter(data, basic)

            if passed:
                log.info(f"  ✅  {sym} — all criteria PASSED")
                qualified.append((basic, data, criteria, soft_flags))

            else:
                failed = [(k, v["label"]) for k, v in criteria.items() if not v["pass"]]
                log.debug(f"  ❌  {sym} failed: {[k for k, _ in failed]}")

                # Watchlist exit — was on list but fails today
                if sym in prev_watchlist_set:
                    days  = get_days_on_watchlist(wl_log, sym, today_str)
                    entry = get_entry_date(wl_log, sym, today_str)
                    exit_msg = format_exit_notice(
                        sym, data["company_name"], failed, days, entry
                    )
                    send_whatsapp(exit_msg)
                    prev_watchlist_set.discard(sym)
                    time.sleep(3)

                # Near-miss logging
                if len(failed) <= cfg.NEAR_MISS_MAX_FAILS:
                    nm = {
                        "symbol":       sym,
                        "name":         data["company_name"],
                        "failed_count": len(failed),
                        "criteria":     failed,
                    }
                    near_miss_today.append(nm)
                    log_near_miss(sym, data["company_name"], failed, today_str)

        except Exception as e:
            log.error(f"  Error on {sym}: {e}")

        time.sleep(2.5)   # polite scraping — do not reduce below 1.5s

    log.info(
        f"\n  Pass 2 result: {len(qualified)} stocks cleared all criteria  "
        f"|  {len(near_miss_today)} near-misses"
    )

    # ── Step 4: Pass 3 — AI assessment ───────────────────────────────────
    log.info(f"\n▶ Step 4  AI assessment ({len(qualified)} stocks)…")

    today_watchlist: List[str] = []
    alerts_sent     = 0
    pass3_ok        = 0

    for basic, data, criteria, soft_flags in qualified:
        sym = basic["symbol"]
        log.info(f"  Assessing {sym} with Claude…")

        ai      = ai_assess(sym, data, basic)
        score   = ai.get("score", 0)
        verdict = ai.get("verdict", "FAIL")
        log.info(f"  → {sym}: {verdict}  ({score}/10)")

        if verdict in ("STRONG PASS", "PASS") and score >= 6.0:
            pass3_ok += 1
            today_watchlist.append(sym)

            if not was_recently_alerted(sym, alerts_log):
                tech = get_technical_data(sym)
                msg  = format_stock_alert(basic, data, criteria, soft_flags, ai, tech)
                send_whatsapp(msg)
                mark_alerted(sym, alerts_log, today_str)
                alerts_sent += 1
                time.sleep(5)
            else:
                log.info(f"  ⏭️  {sym} — alert suppressed (sent within {cfg.ALERT_COOLDOWN_DAYS} days)")

        time.sleep(1.5)   # rate-limit Claude API calls

    # ── Update persistent state ───────────────────────────────────────────
    wl_log[today_str] = today_watchlist
    save_json(WATCHLIST_LOG_PATH, wl_log)
    save_json(ALERTS_LOG_PATH,    alerts_log)

    # ── Daily summary ─────────────────────────────────────────────────────
    run_stats = {
        "total":    len(symbols),
        "pass1":    len(p1),
        "pass2":    len(qualified),
        "pass3_ok": pass3_ok,
        "alerts":   alerts_sent,
    }
    send_daily_summary(run_stats, today_watchlist, near_miss_today, wl_log, today_str)

    # ── Friday weekly report ──────────────────────────────────────────────
    if is_friday:
        log.info("\n▶ Friday — generating weekly report…")
        perf_results = get_portfolio_performance(perf_log)
        send_weekly_report(today, wl_log, perf_results)

    log.info(f"\n{'=' * 60}")
    log.info(f"Done.  {alerts_sent} new alert(s) sent.  "
             f"Watchlist: {len(today_watchlist)} stock(s).")
    log.info(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
