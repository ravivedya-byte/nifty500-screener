"""
NSE Investment Screener  v2.6
Changes from v2.5:
  Message order fixed: ALPHA WATCH -> WATCHLIST EXIT -> COMPANY SNAPSHOT
  Full watchlist shown, no truncation
  Day counts removed from watchlist display
  Clean terminal-style alignment
  Snapshot trimmed: only WHY IT QUALIFIED, BUSINESS SNAPSHOT,
    CAPITAL ALLOCATION, CASH FLOW QUALITY, WATCHLIST STATUS,
    Tomorrow, CTA, Disclaimer
  REMOVED TODAY section removed from ALPHA WATCH (exit is standalone)
  BUSINESS SNAPSHOT compressed to 2 sentences
  WATCHLIST STATUS simplified
  Claude prompt updated accordingly
"""

import csv, io, json, logging, os, re, sys, time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import yfinance as yf
from anthropic import Anthropic
from bs4 import BeautifulSoup

import config as cfg

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("screener")

NSE_EQUITY_URL   = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_NIFTY500_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
SCREENER_CONSOL  = "https://www.screener.in/company/{sym}/consolidated/"
SCREENER_ALONE   = "https://www.screener.in/company/{sym}/"
SCREENER_LINK    = "https://www.screener.in/company/{sym}/"

BFSI_KEYWORDS = {"bank","insurance","finance","financial services","nbfc",
    "asset management","capital markets","credit services","mortgage",
    "microfinance","housing finance","brokerage","wealth management","money market"}

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.screener.in/",
}

ALERTS_LOG_PATH      = "alerts_log.json"
WATCHLIST_LOG_PATH   = "watchlist_log.json"
NEAR_MISS_LOG_PATH   = "near_miss_log.csv"
PERFORMANCE_LOG_PATH = "performance_log.json"
AI_QUEUE_PATH        = "ai_queue.json"
AI_ASSESSMENTS_PATH  = "ai_assessments_log.json"

CRITERIA_META = {
    "Market Cap":       {"threshold": cfg.MIN_MARKET_CAP_CR,    "direction": "above", "label": f">=Rs{cfg.MIN_MARKET_CAP_CR:,}Cr"},
    "Debt / Equity":    {"threshold": cfg.MAX_DE_RATIO,          "direction": "below", "label": f"<={cfg.MAX_DE_RATIO}"},
    "ROE":              {"threshold": cfg.MIN_ROE,                "direction": "above", "label": f">={cfg.MIN_ROE}%"},
    "Revenue CAGR 5Y":  {"threshold": cfg.MIN_REVENUE_GROWTH_5Y, "direction": "above", "label": f">={cfg.MIN_REVENUE_GROWTH_5Y}%"},
    "Avg ROCE (3Y)":    {"threshold": cfg.MIN_ROCE_3Y_AVG,       "direction": "above", "label": f">={cfg.MIN_ROCE_3Y_AVG}%"},
    "FCF / PAT":        {"threshold": cfg.MIN_FCF_TO_PAT,        "direction": "above", "label": f">={cfg.MIN_FCF_TO_PAT}"},
    "Promoter Holding": {"threshold": cfg.MIN_PROMOTER_HOLDING,  "direction": "above", "label": f">={cfg.MIN_PROMOTER_HOLDING}%"},
    "Promoter Pledge":  {"threshold": cfg.MAX_PROMOTER_PLEDGE,   "direction": "below", "label": f"<={cfg.MAX_PROMOTER_PLEDGE}%"},
    "PEG Ratio":        {"threshold": cfg.MAX_PEG_RATIO,         "direction": "below", "label": f"<={cfg.MAX_PEG_RATIO}"},
}

Criterion = Dict[str, Any]


# ── Persistence ───────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f: return json.load(f)
        except Exception as e: log.warning(f"Load {path}: {e}")
    return default

def save_json(path, data):
    try:
        with open(path,"w") as f: json.dump(data,f,indent=2,default=str)
    except Exception as e: log.error(f"Save {path}: {e}")


# ── Universe ──────────────────────────────────────────────────────────────────

def fetch_nse_all():
    session = requests.Session()
    for url, label in [(NSE_EQUITY_URL,"NSE full"),(NSE_NIFTY500_URL,"Nifty500")]:
        try:
            session.get("https://www.nseindia.com",headers={**HTTP_HEADERS,"Accept":"*/*"},timeout=10)
            time.sleep(1)
            resp = session.get(url,headers={**HTTP_HEADERS,"Referer":"https://www.nseindia.com"},timeout=15)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text))
            sym_col = next((c for c in df.columns if "symbol" in c.lower()),None)
            if sym_col:
                syms = df[sym_col].dropna().str.strip().tolist()
                log.info(f"Loaded {len(syms)} ({label})"); return syms
        except Exception as e: log.warning(f"{label}: {e}")
    try:
        from nsepython import nse_eq_symbols
        syms = nse_eq_symbols(); log.info(f"nsepython: {len(syms)}"); return syms
    except Exception as e:
        log.error(f"All universe fetches failed: {e}"); return []


# ── Pass 1 ────────────────────────────────────────────────────────────────────

def _is_bfsi(sector, industry):
    return any(k in f"{sector} {industry}".lower() for k in BFSI_KEYWORDS)

def pass1_yfinance(symbols):
    survivors = []
    for i, sym in enumerate(symbols, 1):
        try:
            info = yf.Ticker(f"{sym}.NS").info
            if not info or not info.get("regularMarketPrice"): continue
            sector = info.get("sector","") or ""; industry = info.get("industry","") or ""
            if _is_bfsi(sector, industry): continue
            mc_cr = (info.get("marketCap") or 0) / 1e7
            if mc_cr < cfg.MIN_MARKET_CAP_CR: continue
            pe = info.get("trailingPE") or info.get("forwardPE")
            if pe and (pe <= 0 or pe > 200): pe = None
            survivors.append({"symbol":sym,"name":info.get("longName",sym),"sector":sector,
                               "industry":industry,"market_cap_cr":round(mc_cr,1),
                               "pe_yf":round(pe,2) if pe else None})
            if i % 100 == 0: log.info(f"  Pass1: {i}/{len(symbols)} | {len(survivors)} alive")
            time.sleep(0.4)
        except Exception as e: log.debug(f"yf {sym}: {e}")
    log.info(f"Pass1: {len(survivors)}/{len(symbols)}")
    return survivors


# ── screener.in parsing ───────────────────────────────────────────────────────

def _soup_with_retry(sym, retries=3, backoff=4.0):
    for attempt in range(retries):
        for url in [SCREENER_CONSOL.format(sym=sym), SCREENER_ALONE.format(sym=sym)]:
            try:
                r = requests.get(url, headers=HTTP_HEADERS, timeout=15)
                if r.status_code == 200 and "company" in r.url:
                    return BeautifulSoup(r.text, "html.parser")
            except Exception as e: log.debug(f"Attempt {attempt+1} {sym}: {e}")
        if attempt < retries - 1: time.sleep(backoff * (2 ** attempt))
    log.warning(f"Cannot fetch {sym}"); return None

def _num(text):
    if not text: return None
    t = re.sub(r"[₹Rs,%\s]","",str(text)).replace(",","")
    t = re.sub(r"Cr\.?","",t).strip()
    if "/" in t: t = t.split("/")[0].strip()
    try: return float(t)
    except: return None

def _fmt(val, suffix="", decimals=1, fallback="N/A"):
    if val is None: return fallback
    try:
        import math
        f = float(val)
        if math.isnan(f) or math.isinf(f): return fallback
        return f"{f:.{decimals}f}{suffix}"
    except: return fallback

def _parse_ratios(soup):
    out = {}
    box = soup.find(id="top-ratios")
    if not box: return out
    for li in box.find_all("li"):
        n = li.find(class_="name"); v = li.find(class_="number")
        if n and v:
            key = n.text.strip().rstrip("+").strip().lower()
            out[key] = v.text.strip()
    for alias in ["return on equity","roe","return on equity %","return on equity(%)"]:
        if alias in out: out["return on equity"] = out[alias]; break
    for alias in ["stock p/e","p/e","pe ratio","price to earnings"]:
        if alias in out: out["stock p/e"] = out[alias]; break
    for alias in ["debt to equity","d/e","debt/equity"]:
        if alias in out: out["debt to equity"] = out[alias]; break
    return out

def _parse_table(soup, section_id):
    data, years = {}, []
    sec = soup.find(id=section_id)
    if not sec: return data, years
    tbl = sec.find("table")
    if not tbl: return data, years
    thead = tbl.find("thead")
    if thead: years = [th.text.strip() for th in thead.find_all("th")][1:]
    tbody = tbl.find("tbody")
    if tbody:
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if not cells: continue
            row_name = cells[0].text.strip().rstrip("+").strip()
            if row_name:
                data[row_name] = {years[i]:cells[i+1].text.strip().replace(",","")
                                  for i in range(min(len(years),len(cells)-1))}
    return data, years

def _parse_shareholding(soup):
    result = {"promoter_holding":None,"promoter_pledge":None,"promoter_trend":None}
    sec = soup.find(id="shareholding")
    if not sec: return result
    for tbl in sec.find_all("table"):
        tbody = tbl.find("tbody")
        if not tbody: continue
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if not cells: continue
            label = cells[0].text.strip().lower()
            vals = [_num(c.text.strip()) for c in cells[1:] if c.text.strip()]
            vals = [v for v in vals if v is not None]
            if not vals: continue
            if label == "promoters" or (label.startswith("promoter") and "pledge" not in label):
                result["promoter_holding"] = vals[-1]
                if len(vals) >= 2:
                    d = vals[-1] - vals[-2]
                    result["promoter_trend"] = "increasing" if d>0.5 else "decreasing" if d<-0.5 else "stable"
            elif "pledge" in label or "pledged" in label:
                result["promoter_pledge"] = vals[-1]
    return result

def _series(table, years, *row_names):
    row = {}
    for name in row_names:
        if name in table: row = table[name]; break
    return [_num(row.get(y,"")) for y in years]

def _cagr(series, n=5):
    valid = [(i,v) for i,v in enumerate(series) if v is not None and v>0]
    if len(valid) < 2: return None
    n = min(n, len(valid)-1)
    start, end = valid[-(n+1)][1], valid[-1][1]
    if start <= 0: return None
    return ((end/start)**(1/n)-1)*100

def get_fundamentals(sym):
    page = _soup_with_retry(sym)
    if not page: return None
    ratios = _parse_ratios(page)
    pl, pl_yrs = _parse_table(page,"profit-loss")
    bs, bs_yrs = _parse_table(page,"balance-sheet")
    cf, cf_yrs = _parse_table(page,"cash-flow")
    holding = _parse_shareholding(page)

    sales   = _series(pl,pl_yrs,"Sales")
    net_p   = _series(pl,pl_yrs,"Net Profit")
    eps     = _series(pl,pl_yrs,"EPS in Rs")
    op_prof = _series(pl,pl_yrs,"Operating Profit")
    equity  = _series(bs,bs_yrs,"Equity Capital")
    reserv  = _series(bs,bs_yrs,"Reserves")
    borrow  = _series(bs,bs_yrs,"Borrowings")
    inv     = _series(bs,bs_yrs,"Inventories","Inventory")
    debtor  = _series(bs,bs_yrs,"Debtors","Trade Receivables","Accounts Receivable")
    cfo     = _series(cf,cf_yrs,"Cash from Operating Activity","Operating Activity")
    cfi     = _series(cf,cf_yrs,"Cash from Investing Activity","Investing Activity")

    roce_hist = []
    for o,e,r,b in zip(op_prof,equity,reserv,borrow):
        if o is not None and e is not None:
            cap = (e or 0)+(r or 0)+(b or 0)
            roce_hist.append(round((o/cap)*100,1) if cap>0 else None)
        else: roce_hist.append(None)
    roce_valid = [v for v in roce_hist[-3:] if v is not None]
    roce_3y_avg = round(sum(roce_valid)/len(roce_valid),1) if roce_valid else None

    opm_hist = []
    for o,s in zip(op_prof,sales):
        if o is not None and s and s>0: opm_hist.append(round((o/s)*100,1))
        else: opm_hist.append(None)
    opm_recent = [v for v in opm_hist[-3:] if v is not None]
    margin_contracting = (len(opm_recent)>=3 and
        sum(1 for i in range(1,len(opm_recent)) if opm_recent[i]<opm_recent[i-1])>=2)

    de = _num(ratios.get("debt to equity",""))
    if de is None:
        b_lat = next((v for v in reversed(borrow) if v is not None),None)
        e_lat = next((v for v in reversed(equity) if v is not None),None)
        r_lat = next((v for v in reversed(reserv) if v is not None),None)
        if b_lat is not None and e_lat is not None:
            eq_total = (e_lat or 0)+(r_lat or 0)
            de = round(b_lat/eq_total,2) if eq_total>0 else None

    eps_g5=_cagr(eps,5); rev_g5=_cagr(sales,5); rev_g3=_cagr(sales,3)
    inv_g3=_cagr(inv,3); rec_g3=_cagr(debtor,3)
    inv_ratio = round(inv_g3/rev_g3,2) if inv_g3 is not None and rev_g3 and rev_g3>0 else None
    rec_ratio = round(rec_g3/rev_g3,2) if rec_g3 is not None and rev_g3 and rev_g3>0 else None
    fcf_list = [(o+fi)/p for o,fi,p in zip(cfo,cfi,net_p)
                if o is not None and fi is not None and p and p>0]
    fcf_to_pat = round(sum(fcf_list)/len(fcf_list),2) if fcf_list else None
    pe  = _num(ratios.get("stock p/e",""))
    peg = round(pe/eps_g5,2) if pe and eps_g5 and eps_g5>0 else None

    name_el = page.find("h1"); about_el = page.find(id="about")
    return {
        "company_name":      name_el.text.strip() if name_el else sym,
        "about":             about_el.get_text(" ",strip=True)[:5000] if about_el else "",
        "roe":               _num(ratios.get("return on equity","")),
        "de_ratio":          de, "pe_ratio":pe, "peg_ratio":peg,
        "eps_growth_5y":     round(eps_g5,1) if eps_g5 else None,
        "revenue_growth_5y": round(rev_g5,1) if rev_g5 else None,
        "roce_3y_avg":       roce_3y_avg, "roce_history":roce_hist[-6:],
        "fcf_to_pat":        fcf_to_pat,
        "promoter_holding":  holding["promoter_holding"],
        "promoter_pledge":   holding["promoter_pledge"],
        "promoter_trend":    holding["promoter_trend"],
        "inventory_ratio":   inv_ratio, "receivables_ratio":rec_ratio,
        "margin_contracting":margin_contracting,
        "_sales_cr":         [round(v,0) if v else None for v in sales[-5:]],
        "_eps":eps[-5:],"_roce":roce_hist[-5:],"_opm":opm_hist[-3:],
    }


# ── Quant Filter ──────────────────────────────────────────────────────────────

def _check(value, condition, label):
    return {"value":value,"pass":condition,"label":label}

def miss_pct(actual, criterion_key):
    meta = CRITERIA_META.get(criterion_key)
    if not meta or actual is None: return None
    threshold = meta["threshold"]; direction = meta["direction"]
    if threshold == 0: return None
    if direction == "above": return round(((threshold-actual)/abs(threshold))*100,1)
    else: return round(((actual-threshold)/abs(threshold))*100,1)

def quant_filter(data, basic):
    r = {}
    mc = basic["market_cap_cr"]
    r["Market Cap"] = _check(mc,mc>=cfg.MIN_MARKET_CAP_CR,f"Rs{mc:,.0f}Cr")
    de = data["de_ratio"]
    r["Debt / Equity"] = _check(de,de is not None and 0<=de<=cfg.MAX_DE_RATIO,
                                 _fmt(de,decimals=2) if de is not None else "N/A")
    roe = data["roe"]
    r["ROE"] = _check(roe,roe is not None and roe>=cfg.MIN_ROE,
                      _fmt(roe,"%") if roe is not None else "N/A")
    rg = data["revenue_growth_5y"]
    r["Revenue CAGR 5Y"] = _check(rg,rg is not None and rg>=cfg.MIN_REVENUE_GROWTH_5Y,
                                   _fmt(rg,"%") if rg is not None else "N/A")
    ra = data["roce_3y_avg"]
    r["Avg ROCE (3Y)"] = _check(ra,ra is not None and ra>=cfg.MIN_ROCE_3Y_AVG,
                                 f"{_fmt(ra,'%')}  hist:{data['_roce']}" if ra is not None else "N/A")
    fp = data["fcf_to_pat"]
    r["FCF / PAT"] = _check(fp,fp is not None and fp>=cfg.MIN_FCF_TO_PAT,
                             _fmt(fp,decimals=2) if fp is not None else "N/A")
    ph = data["promoter_holding"]
    r["Promoter Holding"] = _check(ph,ph is not None and ph>=cfg.MIN_PROMOTER_HOLDING,
                                   _fmt(ph,"%") if ph is not None else "N/A")
    pp = data["promoter_pledge"]
    r["Promoter Pledge"] = _check(pp,pp is None or pp<=cfg.MAX_PROMOTER_PLEDGE,
                                  _fmt(pp,"%") if pp is not None else "0% (nil)")
    peg = data["peg_ratio"]
    r["PEG Ratio"] = _check(peg,peg is not None and peg<=cfg.MAX_PEG_RATIO,
                             _fmt(peg,decimals=2) if peg is not None else "N/A")
    soft_flags = []
    ir = data["inventory_ratio"]
    if ir is not None and ir>cfg.MAX_INVENTORY_RATIO:
        soft_flags.append(f"Inventory growing {_fmt(ir,'x',2)} revenue")
    rr = data["receivables_ratio"]
    if rr is not None and rr>cfg.MAX_RECEIVABLES_RATIO:
        soft_flags.append(f"Receivables growing {_fmt(rr,'x',2)} revenue")
    if data.get("promoter_trend") == "decreasing":
        soft_flags.append(f"Promoter holding declining QoQ ({_fmt(data['promoter_holding'],'%')})")
    if data.get("margin_contracting"):
        soft_flags.append(f"Operating margin contracting over last 3 years")
    return all(v["pass"] for v in r.values()), r, soft_flags

def classify_fails(criteria):
    real, data_f = [], []
    for k,v in criteria.items():
        if not v["pass"]:
            if v["label"] == "N/A": data_f.append((k,v["label"],None))
            else:
                gap = miss_pct(v["value"],k)
                real.append((k,v["label"],gap))
    return real, data_f


# ── AI Queue ──────────────────────────────────────────────────────────────────

def queue_add(sym, name, data, basic, criteria, soft_flags, ai_queue):
    pending_syms = [e["sym"] for e in ai_queue.get("pending",[])]
    if sym in ai_queue.get("assessed",{}) or sym in pending_syms:
        return False
    ai_queue.setdefault("pending",[]).append({
        "sym":        sym,
        "name":       name,
        "date_added": date.today().strftime("%Y-%m-%d"),
        "data":       data,
        "basic":      basic,
        "criteria":   {k:{"value":v["value"],"pass":v["pass"],"label":v["label"]}
                       for k,v in criteria.items()},
        "soft_flags": soft_flags,
    })
    return True

def queue_pop_next(ai_queue):
    pending = ai_queue.get("pending",[])
    if not pending: return None
    entry = pending.pop(0)
    ai_queue["pending"] = pending
    return entry

def queue_peek_next(ai_queue):
    pending = ai_queue.get("pending",[])
    return pending[0]["name"] if pending else None

def queue_mark_assessed(sym, ai_result, ai_queue):
    ai_queue.setdefault("assessed",{})[sym] = {
        "date_assessed": date.today().strftime("%Y-%m-%d"),
        **ai_result,
    }


# ── Claude AI ─────────────────────────────────────────────────────────────────

def ai_assess(sym, data, basic):
    prompt = f"""You are writing a concise institutional research snapshot for a value investing publication.

COMPANY: {data['company_name']} (NSE: {sym})
SECTOR:  {basic['sector']} | INDUSTRY: {basic['industry']}
MARKET CAP: Rs{basic['market_cap_cr']:,.0f} Cr
ABOUT:   {data['about'] or 'Not available'}

FINANCIALS (all 9 hard criteria passed):
  ROE {data['roe']}% | D/E {data['de_ratio']} | P/E {data['pe_ratio']} (reference)
  Revenue CAGR 5Y {data['revenue_growth_5y']}% | ROCE 3Y avg {data['roce_3y_avg']}%
  FCF/PAT {data['fcf_to_pat']} | Promoter {data['promoter_holding']}% | PEG {data['peg_ratio']}

TONE: Calm, restrained, analytical. No hype. Never use multibagger, hidden gem,
massive upside, guaranteed. Do not use " - " (dash between words) anywhere.
Write like a thoughtful analyst, not a promoter or finfluencer.

WRITE THESE FOUR THINGS:

1. BUSINESS SNAPSHOT: Exactly 2 sentences. First sentence: what the company does
   and where. Second sentence: market cap and basic positioning.
   Example: "Accelya Solutions provides revenue accounting and settlement software
   to airlines and travel companies globally. The business operates in a niche
   enterprise software segment with a market cap of approximately Rs1,718 crore."

2. CAPITAL ALLOCATION:
   Rating: EXCELLENT / GOOD / AVERAGE / POOR
   Note: One sentence on what management does with earned cash.

3. CASH FLOW QUALITY:
   Rating: CLEAN / MINOR CONCERN / SIGNIFICANT CONCERN
   Note: One sentence on FCF reliability.

4. FORENSIC NOTE: One sentence. Specific observation only. If nothing notable,
   write "No material concerns identified from available data."

Return ONLY this JSON:
{{"score": <1-10>,
"verdict": "STRONG PASS|PASS|BORDERLINE|FAIL",
"business_snapshot": "<exactly 2 sentences>",
"capital_allocation_rating": "EXCELLENT|GOOD|AVERAGE|POOR",
"capital_allocation_note": "<one sentence>",
"cash_flow_quality": "CLEAN|MINOR CONCERN|SIGNIFICANT CONCERN",
"cash_flow_note": "<one sentence>",
"forensic_note": "<one sentence>"}}"""

    text = ""
    try:
        resp = Anthropic(api_key=cfg.ANTHROPIC_API_KEY).messages.create(
            model="claude-sonnet-4-6", max_tokens=600,
            messages=[{"role":"user","content":prompt}])
        text = resp.content[0].text.strip()
        text = re.sub(r"```json?\s*","",text).replace("```","").strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"JSON error {sym}: {e}\n{text[:200]}")
        return {"score":0,"verdict":"FAIL","business_snapshot":f"Assessment unavailable."}
    except Exception as e:
        log.error(f"AI failed {sym}: {e}")
        return {"score":0,"verdict":"FAIL","business_snapshot":str(e)}


# ── Technical Data ─────────────────────────────────────────────────────────────

def get_technical_data(sym):
    import math
    try:
        t = yf.Ticker(f"{sym}.NS"); h1 = t.history(period="1y")
        if h1.empty: return {}
        cur = h1["Close"].iloc[-1]
        if math.isnan(cur): return {}
        h14 = t.history(period="14mo")
        dma = None
        if len(h14) >= 200:
            dma_val = h14["Close"].rolling(200).mean().iloc[-1]
            dma = round(dma_val,2) if not math.isnan(dma_val) else None
        cl = h1["Close"].tail(15); d = cl.diff().dropna()
        g = d.clip(lower=0).mean(); l = (-d.clip(upper=0)).mean()
        rsi = round(100-(100/(1+g/l)),1) if l>0 else 100.0
        high = h1["Close"].max(); low = h1["Close"].min()
        pct = ((cur-low)/low)*100 if low>0 else 0
        return {
            "current":       round(cur,2),
            "high_52w":      round(high,2),
            "low_52w":       round(low,2),
            "pct_above_low": round(pct,1),
            "dma_200":       dma,
            "above_200dma":  bool(cur>dma) if dma else None,
            "rsi":           rsi,
        }
    except Exception as e: log.debug(f"Tech {sym}: {e}"); return {}


# ── Links and Helpers ─────────────────────────────────────────────────────────

def screener_url(sym): return SCREENER_LINK.format(sym=sym)
def tg_link(display, url): return f"[{display}]({url})"
def sym_link(sym): return tg_link(sym, screener_url(sym))
def _score_bar(score, width=10):
    f = max(0,min(width,round(score))); return "█"*f+"░"*(width-f)


# ── Watchlist Helpers ─────────────────────────────────────────────────────────

def get_previous_watchlist(wl_log, today_str):
    d = datetime.strptime(today_str,"%Y-%m-%d").date()
    for i in range(1,8):
        prev = (d-timedelta(days=i)).strftime("%Y-%m-%d")
        if prev in wl_log: return wl_log[prev]
    return []

def get_days_on_watchlist(wl_log, sym, today_str):
    d = datetime.strptime(today_str,"%Y-%m-%d").date(); count = 0
    for i in range(365):
        dd = (d-timedelta(days=i)).strftime("%Y-%m-%d")
        if sym in wl_log.get(dd,[]): count += 1
        elif count > 0: break
    return count

def get_entry_date(wl_log, sym, today_str):
    d = datetime.strptime(today_str,"%Y-%m-%d").date(); entry = None
    for i in range(365):
        dd = (d-timedelta(days=i)).strftime("%Y-%m-%d")
        if sym in wl_log.get(dd,[]): entry = dd
        elif entry: break
    return entry

def get_30day_eligible(wl_log, reference_date):
    counts = {}
    for i in range(30):
        d = (reference_date-timedelta(days=i+1)).strftime("%Y-%m-%d")
        for sym in wl_log.get(d,[]):
            counts[sym] = counts.get(sym,0)+1
    return counts


# ── Near-Miss Logging ─────────────────────────────────────────────────────────

def log_near_miss(sym, name, real_fails, date_str):
    exists = os.path.exists(NEAR_MISS_LOG_PATH)
    with open(NEAR_MISS_LOG_PATH,"a",newline="",encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists: w.writerow(["date","symbol","name","failed_count","criterion","detail","miss_pct"])
        for criterion,detail,gap in real_fails:
            w.writerow([date_str,sym,name,len(real_fails),criterion,detail,gap or ""])

def get_weekly_near_misses(today):
    if not os.path.exists(NEAR_MISS_LOG_PATH): return []
    cutoff = today-timedelta(days=7); results = {}
    try:
        with open(NEAR_MISS_LOG_PATH,"r",encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    rd = datetime.strptime(row["date"],"%Y-%m-%d").date()
                    if rd < cutoff: continue
                    sym = row["symbol"]
                    if sym not in results or rd > datetime.strptime(results[sym]["date"],"%Y-%m-%d").date():
                        results[sym] = {"date":row["date"],"symbol":sym,"name":row["name"],
                                        "failed_count":int(row["failed_count"]),"criteria":[]}
                    gap = row.get("miss_pct","")
                    results[sym]["criteria"].append((row["criterion"],row["detail"],float(gap) if gap else None))
                except: pass
    except Exception as e: log.warning(f"NM log: {e}")
    def sort_key(nm):
        gaps = [g for _,_,g in nm["criteria"] if g is not None]
        return min(gaps) if gaps else 999
    return sorted(results.values(), key=sort_key)


# ── Dedup ─────────────────────────────────────────────────────────────────────

def was_recently_alerted(sym, alerts_log):
    if sym not in alerts_log: return False
    try:
        last = datetime.strptime(alerts_log[sym],"%Y-%m-%d").date()
        return (date.today()-last).days < cfg.ALERT_COOLDOWN_DAYS
    except: return False

def mark_alerted(sym, alerts_log, today_str): alerts_log[sym] = today_str


# ── SIP Tracker ───────────────────────────────────────────────────────────────

def _fetch_price(sym, target_date):
    try:
        ts = sym if sym.startswith("^") else f"{sym}.NS"
        h  = yf.Ticker(ts).history(start=target_date.strftime("%Y-%m-%d"),
                                    end=(target_date+timedelta(days=7)).strftime("%Y-%m-%d"))
        return round(float(h["Close"].iloc[0]),2) if not h.empty else None
    except: return None

def _fetch_current_price(sym):
    try:
        ts = sym if sym.startswith("^") else f"{sym}.NS"
        h  = yf.Ticker(ts).history(period="5d")
        return round(float(h["Close"].iloc[-1]),2) if not h.empty else None
    except: return None

def calculate_monthly_sip(wl_log, perf_log, today):
    month_key = today.strftime("%Y-%m")
    if month_key in [e["month"] for e in perf_log.get("sip_entries",[])]:
        log.info(f"SIP done {month_key}"); return
    eligible = get_30day_eligible(wl_log, today)
    bp = _fetch_price(cfg.BENCHMARK_TICKER, today)
    if not eligible:
        gp = _fetch_price(cfg.GOLD_ETF_SYMBOL, today)
        entry = {"month":month_key,"date":today.strftime("%Y-%m-%d"),"type":"gold_fallback",
                 "allocations":[{"symbol":cfg.GOLD_ETF_SYMBOL,"name":"GOLDBEES","price_entry":gp,
                                  "shares":round(cfg.SIP_AMOUNT_INR/gp,4) if gp else 0}],
                 "amount_per_stock":cfg.SIP_AMOUNT_INR,"total_deployed":cfg.SIP_AMOUNT_INR,
                 "benchmark_price_entry":bp}
    else:
        ae = cfg.SIP_AMOUNT_INR/len(eligible)
        allocs = [{"symbol":s,"price_entry":p,"shares":round(ae/p,4),"days_on_list":eligible[s]}
                  for s in eligible for p in [_fetch_price(s,today)] if p and p>0]
        entry = {"month":month_key,"date":today.strftime("%Y-%m-%d"),"type":"stocks",
                 "allocations":allocs,"amount_per_stock":round(ae,2),
                 "total_deployed":cfg.SIP_AMOUNT_INR,"benchmark_price_entry":bp}
    perf_log.setdefault("sip_entries",[]).append(entry)
    save_json(PERFORMANCE_LOG_PATH, perf_log); log.info(f"SIP recorded {month_key}")

def get_portfolio_performance(perf_log):
    results = []; bc = _fetch_current_price(cfg.BENCHMARK_TICKER)
    for entry in perf_log.get("sip_entries",[]):
        res = {"month":entry["month"],"date":entry["date"],"type":entry["type"],
               "total_deployed":entry["total_deployed"],"allocations":[],"total_current":0.0,
               "return_pct":None,"bench_pct":None,"alpha":None}
        be = entry.get("benchmark_price_entry")
        if be and bc: res["bench_pct"] = round(((bc-be)/be)*100,2)
        tc = 0.0
        for a in entry.get("allocations",[]):
            cp = _fetch_current_price(a["symbol"]); ep = a.get("price_entry"); sh = a.get("shares",0)
            if cp and ep and sh:
                cv = sh*cp
                res["allocations"].append({**a,"price_current":cp,"current_value":round(cv,2),
                                           "pct_change":round(((cp-ep)/ep)*100,2)})
                tc += cv
            else: res["allocations"].append({**a,"price_current":None})
        if tc > 0:
            res["total_current"] = round(tc,2)
            res["return_pct"] = round(((tc-entry["total_deployed"])/entry["total_deployed"])*100,2)
            if res["bench_pct"] is not None: res["alpha"] = round(res["return_pct"]-res["bench_pct"],2)
        results.append(res)
    return results


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_whatsapp(text):
    url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = []
    while len(text) > 4000:
        split_at = text.rfind("\n",0,4000)
        if split_at == -1: split_at = 4000
        chunks.append(text[:split_at]); text = text[split_at:].lstrip("\n")
    chunks.append(text)
    all_sent = True
    for chunk in chunks:
        try:
            r = requests.post(url, json={
                "chat_id":                  cfg.TELEGRAM_CHAT_ID,
                "text":                     chunk,
                "parse_mode":               "Markdown",
                "disable_web_page_preview": True,
            }, timeout=15)
            if r.status_code == 200: log.info("Telegram sent ✅")
            else: log.error(f"Telegram {r.status_code}: {r.text[:200]}"); all_sent = False
            time.sleep(1)
        except Exception as e: log.error(f"Telegram failed: {e}"); all_sent = False
    return all_sent


# ── Message Formatting ────────────────────────────────────────────────────────

def format_daily_summary(run_stats, today_wl, wl_log, today_str,
                          ai_queue, new_entries):
    """
    ALPHA WATCH — sent first.
    Full watchlist, no truncation.
    No day counts in display.
    No REMOVED TODAY section (exits sent as standalone messages).
    Longest Active only if a stock has >7 days.
    """
    ts = datetime.now().strftime("%a, %d %b %Y")

    assessed_syms  = set(ai_queue.get("assessed",{}).keys())
    pending_syms   = [e["sym"] for e in ai_queue.get("pending",[])]
    pending_count  = len(pending_syms)
    assessed_count = len(assessed_syms)

    # Header
    header = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"ALPHA WATCH  |  {ts}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Scanned:     {run_stats.get('total',0):,}\n"
        f"Qualified:   {len(today_wl)}\n"
        f"New today:   {len(new_entries)}\n\n"
        f"AI Queue:    {pending_count} pending  |  {assessed_count} assessed"
    )

    # Watchlist — all stocks, no day counts, clean alignment
    if today_wl:
        wl_lines = []
        for sym in today_wl:
            if sym in assessed_syms:
                status = "✅ Assessed"
            elif sym in pending_syms:
                pos = pending_syms.index(sym) + 1
                status = f"⏳ Queue #{pos}"
            else:
                status = "⏳ Queue"
            # Fixed-width: symbol padded to 14 chars
            wl_lines.append(f"  {sym:<14} {status}")

        wl_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 WATCHLIST  ({len(today_wl)} stocks)\n\n"
            + "\n".join(wl_lines)
        )
    else:
        wl_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📋 WATCHLIST\n  No stocks currently qualify"
        )

    # Longest Active — only if at least one stock has >7 days
    la_block = ""
    days_map = {sym: get_days_on_watchlist(wl_log, sym, today_str) for sym in today_wl}
    longest = sorted(today_wl, key=lambda s: days_map.get(s,0), reverse=True)
    if longest and days_map.get(longest[0], 0) > 7:
        top5 = longest[:5]
        la_lines = "\n".join(
            f"  {sym:<14} {days_map.get(s,1)} days"
            for sym, s in zip(top5, top5)
        )
        la_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 LONGEST ACTIVE\n\n{la_lines}"
        )

    # New entries
    new_block = ""
    if new_entries:
        new_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆕 NEW ENTRIES\n\n  "
            + ", ".join(new_entries)
        )

    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"The system prioritises:\n"
        f"Earnings quality  •  Free cash flow\n"
        f"Capital efficiency  •  Reasonable valuation\n"
        f"Long-term growth durability\n\n"
        f"Not investment advice  |  SEBI RA: {cfg.SEBI_RA_NUMBER}"
    )

    return f"{header}{wl_block}{la_block}{new_block}{footer}"


def format_exit_notice(sym, name, real_fails, days, entry_date):
    """Standalone WATCHLIST EXIT — sent after ALPHA WATCH."""
    reasons = "; ".join(f"{k} ({v})" for k,v,_ in real_fails) if real_fails else "criteria no longer met"
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"❌ WATCHLIST EXIT\n\n"
        f"{tg_link(f'*{name}*', screener_url(sym))} ({sym})\n\n"
        f"Reason: {reasons}\n"
        f"Days on list: {days}"
        + (f"  |  Entry: {entry_date}" if entry_date else "") +
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


def format_company_snapshot(entry, ai, queue_pos, queue_total, next_name):
    """
    COMPANY SNAPSHOT — sent last, after ALPHA WATCH and WATCHLIST EXIT.
    Contains only: Header, WHY IT QUALIFIED, BUSINESS SNAPSHOT,
    CAPITAL ALLOCATION, CASH FLOW QUALITY, WATCHLIST STATUS,
    Tomorrow's Snapshot, Substack CTA, Disclaimer.
    """
    sym      = entry["sym"]
    name     = entry["name"]
    data     = entry["data"]
    basic    = entry["basic"]
    criteria = entry["criteria"]
    soft_flags = entry["soft_flags"]

    # WHY IT QUALIFIED — key numbers, clean bullets, no Market Cap row
    skip = {"Market Cap", "Avg ROCE (3Y)"}
    qual_lines = []
    for k, v in criteria.items():
        if k in skip: continue
        qual_lines.append(f"• {k:<20} {v['label']}")
    ra = data.get("roce_3y_avg")
    if ra:
        qual_lines.append(f"• {'ROCE (3Y avg)':<20} {_fmt(ra,'%')}")
    pe = data.get("pe_ratio")
    if pe:
        qual_lines.append(f"• {'P/E':<20} {_fmt(pe,decimals=1)}  _(reference)_")
    for sf in soft_flags:
        qual_lines.append(f"⚠️  {sf}")

    # Watchlist status — simplified
    wl_status = "In assessment queue" if queue_pos <= queue_total else "Assessment complete"

    # Tomorrow preview
    tomorrow_block = ""
    if next_name:
        tomorrow_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Tomorrow's Snapshot:\n{next_name}"
        )

    # Substack CTA
    substack_url = cfg.SUBSTACK_BASE_URL.rstrip("/")
    cta = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Full research note:\n{substack_url}"
    )

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"COMPANY SNAPSHOT  |  {name.upper()}\n"
        f"NSE: {sym}  |  {basic.get('sector','')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"WHY IT QUALIFIED\n\n"
        + "\n".join(qual_lines) +
        f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"BUSINESS SNAPSHOT\n\n"
        f"{ai.get('business_snapshot','Not available')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"CAPITAL ALLOCATION\n\n"
        f"Rating: {ai.get('capital_allocation_rating','N/A')}\n"
        f"{ai.get('capital_allocation_note','N/A')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"CASH FLOW QUALITY\n\n"
        f"Rating: {ai.get('cash_flow_quality','N/A')}\n"
        f"{ai.get('cash_flow_note','N/A')}\n\n"
        f"Forensic note: {ai.get('forensic_note','N/A')}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"WATCHLIST STATUS\n\n"
        f"{wl_status}"
        f"{tomorrow_block}"
        f"{cta}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Not a buy recommendation.  SEBI RA: {cfg.SEBI_RA_NUMBER}"
    )


def format_weekly_report(today, wl_log, perf_results, weekly_nms):
    ts = today.strftime("%d %b %Y")
    if weekly_nms:
        ones = [nm for nm in weekly_nms if nm["failed_count"]==1]
        twos = [nm for nm in weekly_nms if nm["failed_count"]==2]
        def nm_line(nm):
            parts = []
            for criterion,detail,gap in nm["criteria"]:
                gap_str = f" (missed by {_fmt(gap,'%',0)})" if gap is not None else ""
                parts.append(f"{criterion}: {detail}{gap_str}")
            return f"  {sym_link(nm['symbol']):<35} {',  '.join(parts)}"
        secs = []
        if ones: secs.append("Missed by 1 criterion:\n"+"\n".join(nm_line(nm) for nm in ones))
        if twos: secs.append("Missed by 2 criteria:\n"+"\n".join(nm_line(nm) for nm in twos))
        nb = "NEAR-MISSES THIS WEEK\n_(sorted closest to qualifying first)_\n\n"+"\n\n".join(secs)
    else: nb = "NEAR-MISSES THIS WEEK\n  None this week."

    if not perf_results: pb = "PAPER PORTFOLIO\n  No SIP entries yet."
    else:
        batches=[]; td=tc=0.0
        for res in sorted(perf_results,key=lambda x:x["month"],reverse=True):
            al_lines = []
            for a in res.get("allocations",[]):
                chg = a.get("pct_change"); chg_s = f"{chg:+.1f}%" if chg is not None else "pending"
                al_lines.append(
                    f"    {sym_link(a['symbol']):<30} "
                    f"Rs{a.get('price_entry','?')} to Rs{a.get('price_current','?')}  {chg_s}")
            r=res.get("return_pct"); b=res.get("bench_pct"); al2=res.get("alpha")
            rs=f"{r:+.1f}%" if r is not None else "pending"
            vs=(f"vs Nifty50: {b:+.1f}%  |  Alpha: {al2:+.1f}%"
                if b is not None and al2 is not None else "pending")
            beat="✅" if al2 is not None and al2>=0 else "❌"
            btype="Gold" if res["type"]=="gold_fallback" else "Stocks"
            batches.append(f"─ {res['month']}  ({btype})\n"+"\n".join(al_lines)
                           +f"\n  Return: *{rs}*  {beat}\n  {vs}")
            td+=res.get("total_deployed",0)
            tc+=res.get("total_current") or res.get("total_deployed",0)
        ar=(tc-td)/td*100 if td>0 else 0; m=len(perf_results)
        gm=sum(1 for r in perf_results if r["type"]=="gold_fallback")
        at=(f"─ ALL-TIME ({m} month{'s' if m!=1 else ''})\n"
            f"  Deployed: Rs{td:,.0f}  |  Value: Rs{tc:,.0f}\n"
            f"  Return: {ar:+.1f}%  |  Gold months: {gm}/{m}")
        pb=(f"PAPER PORTFOLIO\n"
            f"Rs{cfg.SIP_AMOUNT_INR/100_000:.0f}L SIP on 1st  |  Benchmark: Nifty 50\n\n"
            +"\n\n".join(batches)+"\n\n"+at)

    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"WEEKLY REPORT  |  Sat, {ts}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{nb}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{pb}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Paper portfolio only. Not investment advice.\n"
        f"SEBI RA: {cfg.SEBI_RA_NUMBER}"
    )


# ── Saturday Mode ─────────────────────────────────────────────────────────────

def run_weekly_report_only():
    today    = date.today()
    perf_log = load_json(PERFORMANCE_LOG_PATH,{"sip_entries":[]})
    wl_log   = load_json(WATCHLIST_LOG_PATH,{})
    log.info("Saturday mode — weekly report…")
    send_whatsapp(format_weekly_report(
        today, wl_log,
        get_portfolio_performance(perf_log),
        get_weekly_near_misses(today)
    ))
    log.info("Weekly report sent ✅")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode = os.getenv("SCREENER_MODE","screener").lower()
    if mode == "weekly_report":
        run_weekly_report_only(); return

    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")
    is_first  = today.day == 1

    log.info("="*60)
    log.info(f"NSE Screener v2.6  |  {today.strftime('%A, %d %b %Y')}")
    log.info("="*60)

    alerts_log = load_json(ALERTS_LOG_PATH,{})
    wl_log     = load_json(WATCHLIST_LOG_PATH,{})
    perf_log   = load_json(PERFORMANCE_LOG_PATH,{"sip_entries":[]})
    ai_queue   = load_json(AI_QUEUE_PATH,{"pending":[],"assessed":{}})

    if is_first:
        log.info("\nSIP Day…"); calculate_monthly_sip(wl_log,perf_log,today)

    log.info("\nLoading NSE universe…")
    symbols = fetch_nse_all()
    if not symbols: log.error("No symbols."); sys.exit(1)
    log.info(f"  {len(symbols)} symbols")

    log.info(f"\nPass 1 ({len(symbols)} stocks)…")
    p1 = pass1_yfinance(symbols)
    if not p1: log.info("None survived."); return
    log.info(f"  {len(p1)} survived")

    prev_wl     = get_previous_watchlist(wl_log, today_str)
    prev_wl_set = set(prev_wl)
    p1_set      = {b["symbol"] for b in p1}

    # Collect exits — do NOT send yet, send after ALPHA WATCH
    exits_to_send = []
    for sym in prev_wl_set - p1_set:
        days  = get_days_on_watchlist(wl_log,sym,today_str)
        entry = get_entry_date(wl_log,sym,today_str)
        exits_to_send.append({
            "sym":sym,"name":sym,
            "real_fails":[("Pass 1","Market cap or BFSI",None)],
            "days":days,"entry":entry
        })

    log.info(f"\nPass 2 ({len(p1)} stocks)…")
    today_wl = []; new_entries = []

    for idx, basic in enumerate(p1, 1):
        sym = basic["symbol"]
        log.info(f"  [{idx:>3}/{len(p1)}] {sym}…")
        try:
            data = get_fundamentals(sym)
            if not data: time.sleep(2.5); continue
            if not data["pe_ratio"] and basic.get("pe_yf"):
                data["pe_ratio"] = basic["pe_yf"]
                if data.get("eps_growth_5y") and data["eps_growth_5y"]>0:
                    data["peg_ratio"] = round(data["pe_ratio"]/data["eps_growth_5y"],2)

            passed, criteria, soft_flags = quant_filter(data, basic)
            real_fails, data_fails = classify_fails(criteria)

            if passed:
                log.info(f"  ✅  {sym}")
                today_wl.append(sym)
                added = queue_add(sym, data["company_name"], data, basic,
                                  criteria, soft_flags, ai_queue)
                if added: new_entries.append(sym)
            else:
                if sym in prev_wl_set:
                    days  = get_days_on_watchlist(wl_log,sym,today_str)
                    entry = get_entry_date(wl_log,sym,today_str)
                    # Defer exit notice — send after ALPHA WATCH
                    exits_to_send.append({
                        "sym":sym,"name":data["company_name"],
                        "real_fails":real_fails,"days":days,"entry":entry
                    })
                    prev_wl_set.discard(sym)
                if real_fails and len(real_fails) <= cfg.NEAR_MISS_MAX_FAILS:
                    all_close = all(gap is not None and gap <= cfg.NEAR_MISS_MAX_GAP_PCT
                                    for _,_,gap in real_fails)
                    if all_close:
                        log_near_miss(sym,data["company_name"],real_fails,today_str)
        except Exception as e: log.error(f"  Error {sym}: {e}")
        time.sleep(2.5)

    log.info(f"\n  Pass 2: {len(today_wl)} qualified  |  {len(new_entries)} new")

    wl_log[today_str] = today_wl
    save_json(WATCHLIST_LOG_PATH, wl_log)

    # ── MESSAGE ORDER: 1. ALPHA WATCH ─────────────────────────────────────────
    run_stats = {"total":len(symbols),"pass1":len(p1),"pass2":len(today_wl)}
    send_whatsapp(format_daily_summary(
        run_stats, today_wl, wl_log, today_str,
        ai_queue, new_entries
    ))

    # ── MESSAGE ORDER: 2. WATCHLIST EXITS ─────────────────────────────────────
    for ex in exits_to_send:
        send_whatsapp(format_exit_notice(
            ex["sym"], ex["name"], ex["real_fails"], ex["days"], ex["entry"]
        ))
        time.sleep(2)

    # ── MESSAGE ORDER: 3. COMPANY SNAPSHOT ────────────────────────────────────
    pending_count  = len(ai_queue.get("pending",[]))
    assessed_count = len(ai_queue.get("assessed",{}))
    queue_total    = pending_count + assessed_count

    if pending_count > 0:
        entry = queue_pop_next(ai_queue)
        if entry:
            sym   = entry["sym"]
            data  = entry["data"]
            basic = entry["basic"]
            log.info(f"\nCompany Snapshot: {sym}…")
            ai      = ai_assess(sym, data, basic)
            queue_mark_assessed(sym, ai, ai_queue)
            assessed_so_far = len(ai_queue.get("assessed",{}))
            next_name = queue_peek_next(ai_queue)
            send_whatsapp(format_company_snapshot(
                entry, ai,
                queue_pos=assessed_so_far,
                queue_total=queue_total,
                next_name=next_name,
            ))
            log.info(f"  {sym}: {ai.get('verdict','?')} ({ai.get('score',0)}/10)")
    else:
        log.info("\nAI queue empty — no snapshot today")

    save_json(AI_QUEUE_PATH, ai_queue)
    save_json(ALERTS_LOG_PATH, alerts_log)

    log.info(f"\n{'='*60}\nDone. Watchlist: {len(today_wl)} stocks.\n{'='*60}\n")


if __name__ == "__main__":
    main()
