"""
NSE Investment Screener  v2.2
Philosophy: value investing — cash flow, moat, capital allocation, patience.
Weekdays 5:30 PM IST: full NSE scan. Saturday 10 AM IST: weekly report only.
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
AI_ASSESSMENTS_PATH  = "ai_assessments_log.json"

CRITERIA_META = {
    "Market Cap":       {"threshold": cfg.MIN_MARKET_CAP_CR,     "direction": "above", "label": f">=Rs{cfg.MIN_MARKET_CAP_CR:,}Cr"},
    "Debt / Equity":    {"threshold": cfg.MAX_DE_RATIO,           "direction": "below", "label": f"<={cfg.MAX_DE_RATIO}"},
    "ROE":              {"threshold": cfg.MIN_ROE,                 "direction": "above", "label": f">={cfg.MIN_ROE}%"},
    "Revenue CAGR 5Y":  {"threshold": cfg.MIN_REVENUE_GROWTH_5Y,  "direction": "above", "label": f">={cfg.MIN_REVENUE_GROWTH_5Y}%"},
    "Avg ROCE (3Y)":    {"threshold": cfg.MIN_ROCE_3Y_AVG,        "direction": "above", "label": f">={cfg.MIN_ROCE_3Y_AVG}%"},
    "FCF / PAT":        {"threshold": cfg.MIN_FCF_TO_PAT,         "direction": "above", "label": f">={cfg.MIN_FCF_TO_PAT}"},
    "Promoter Holding": {"threshold": cfg.MIN_PROMOTER_HOLDING,   "direction": "above", "label": f">={cfg.MIN_PROMOTER_HOLDING}%"},
    "Promoter Pledge":  {"threshold": cfg.MAX_PROMOTER_PLEDGE,    "direction": "below", "label": f"<={cfg.MAX_PROMOTER_PLEDGE}%"},
    "PEG Ratio":        {"threshold": cfg.MAX_PEG_RATIO,          "direction": "below", "label": f"<={cfg.MAX_PEG_RATIO}"},
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

def _is_bfsi(sector,industry):
    return any(k in f"{sector} {industry}".lower() for k in BFSI_KEYWORDS)

def pass1_yfinance(symbols):
    survivors = []
    for i,sym in enumerate(symbols,1):
        try:
            info = yf.Ticker(f"{sym}.NS").info
            if not info or not info.get("regularMarketPrice"): continue
            sector = info.get("sector","") or ""; industry = info.get("industry","") or ""
            if _is_bfsi(sector,industry): continue
            mc_cr = (info.get("marketCap") or 0)/1e7
            if mc_cr < cfg.MIN_MARKET_CAP_CR: continue
            pe = info.get("trailingPE") or info.get("forwardPE")
            if pe and (pe<=0 or pe>200): pe=None
            survivors.append({"symbol":sym,"name":info.get("longName",sym),"sector":sector,
                               "industry":industry,"market_cap_cr":round(mc_cr,1),"pe_yf":round(pe,2) if pe else None})
            if i%100==0: log.info(f"  Pass1: {i}/{len(symbols)} | {len(survivors)} alive")
            time.sleep(0.4)
        except Exception as e: log.debug(f"yf {sym}: {e}")
    log.info(f"Pass1: {len(survivors)}/{len(symbols)}")
    return survivors

# ── screener.in parsing ───────────────────────────────────────────────────────

def _soup_with_retry(sym,retries=3,backoff=4.0):
    for attempt in range(retries):
        for url in [SCREENER_CONSOL.format(sym=sym),SCREENER_ALONE.format(sym=sym)]:
            try:
                r = requests.get(url,headers=HTTP_HEADERS,timeout=15)
                if r.status_code==200 and "company" in r.url:
                    return BeautifulSoup(r.text,"html.parser")
            except Exception as e: log.debug(f"Attempt {attempt+1} {sym}: {e}")
        if attempt<retries-1: time.sleep(backoff*(2**attempt))
    log.warning(f"Cannot fetch {sym}"); return None

def _num(text):
    if not text: return None
    t = re.sub(r"[₹Rs,%\s]","",str(text)).replace(",","")
    t = re.sub(r"Cr\.?","",t).strip()
    if "/" in t: t = t.split("/")[0].strip()
    try: return float(t)
    except: return None

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

def _parse_table(soup,section_id):
    data,years = {},[]
    sec = soup.find(id=section_id)
    if not sec: return data,years
    tbl = sec.find("table")
    if not tbl: return data,years
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
    return data,years

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
            if label=="promoters" or (label.startswith("promoter") and "pledge" not in label):
                result["promoter_holding"] = vals[-1]
                if len(vals)>=2:
                    d = vals[-1]-vals[-2]
                    result["promoter_trend"] = "increasing" if d>0.5 else "decreasing" if d<-0.5 else "stable"
            elif "pledge" in label or "pledged" in label:
                result["promoter_pledge"] = vals[-1]
    return result

def _series(table,years,*row_names):
    row = {}
    for name in row_names:
        if name in table: row=table[name]; break
    return [_num(row.get(y,"")) for y in years]

def _cagr(series,n=5):
    valid = [(i,v) for i,v in enumerate(series) if v is not None and v>0]
    if len(valid)<2: return None
    n = min(n,len(valid)-1)
    start,end = valid[-(n+1)][1],valid[-1][1]
    if start<=0: return None
    return ((end/start)**(1/n)-1)*100

def get_fundamentals(sym):
    page = _soup_with_retry(sym)
    if not page: return None
    ratios = _parse_ratios(page)
    pl,pl_yrs = _parse_table(page,"profit-loss")
    bs,bs_yrs = _parse_table(page,"balance-sheet")
    cf,cf_yrs = _parse_table(page,"cash-flow")
    holding   = _parse_shareholding(page)
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
        "de_ratio":          de,"pe_ratio":pe,"peg_ratio":peg,
        "eps_growth_5y":     round(eps_g5,1) if eps_g5 else None,
        "revenue_growth_5y": round(rev_g5,1) if rev_g5 else None,
        "roce_3y_avg":       roce_3y_avg,"roce_history":roce_hist[-6:],
        "fcf_to_pat":        fcf_to_pat,
        "promoter_holding":  holding["promoter_holding"],
        "promoter_pledge":   holding["promoter_pledge"],
        "promoter_trend":    holding["promoter_trend"],
        "inventory_ratio":   inv_ratio,"receivables_ratio":rec_ratio,
        "margin_contracting":margin_contracting,
        "_sales_cr":         [round(v,0) if v else None for v in sales[-5:]],
        "_eps":eps[-5:],"_roce":roce_hist[-5:],"_opm":opm_hist[-3:],
    }

# ── Quant Filter ──────────────────────────────────────────────────────────────

def _check(value,condition,label): return {"value":value,"pass":condition,"label":label}

def miss_pct(actual, criterion_key):
    meta = CRITERIA_META.get(criterion_key)
    if not meta or actual is None: return None
    threshold = meta["threshold"]; direction = meta["direction"]
    if threshold==0: return None
    if direction=="above": return round(((threshold-actual)/abs(threshold))*100,1)
    else: return round(((actual-threshold)/abs(threshold))*100,1)

def quant_filter(data,basic):
    r = {}
    mc = basic["market_cap_cr"]
    r["Market Cap"] = _check(mc,mc>=cfg.MIN_MARKET_CAP_CR,f"Rs{mc:,.0f}Cr")
    de = data["de_ratio"]
    r["Debt / Equity"] = _check(de,de is not None and 0<=de<=cfg.MAX_DE_RATIO,
                                 f"{de:.2f}" if de is not None else "N/A")
    roe = data["roe"]
    r["ROE"] = _check(roe,roe is not None and roe>=cfg.MIN_ROE,
                      f"{roe:.1f}%" if roe is not None else "N/A")
    rg = data["revenue_growth_5y"]
    r["Revenue CAGR 5Y"] = _check(rg,rg is not None and rg>=cfg.MIN_REVENUE_GROWTH_5Y,
                                   f"{rg:.1f}%" if rg is not None else "N/A")
    ra = data["roce_3y_avg"]
    r["Avg ROCE (3Y)"] = _check(ra,ra is not None and ra>=cfg.MIN_ROCE_3Y_AVG,
                                 f"{ra:.1f}%  hist:{data['_roce']}" if ra is not None else "N/A")
    fp = data["fcf_to_pat"]
    r["FCF / PAT"] = _check(fp,fp is not None and fp>=cfg.MIN_FCF_TO_PAT,
                             f"{fp:.2f}" if fp is not None else "N/A")
    ph = data["promoter_holding"]
    r["Promoter Holding"] = _check(ph,ph is not None and ph>=cfg.MIN_PROMOTER_HOLDING,
                                   f"{ph:.1f}%" if ph is not None else "N/A")
    pp = data["promoter_pledge"]
    r["Promoter Pledge"] = _check(pp,pp is None or pp<=cfg.MAX_PROMOTER_PLEDGE,
                                  f"{pp:.1f}%" if pp is not None else "0%(nil)")
    peg = data["peg_ratio"]
    r["PEG Ratio"] = _check(peg,peg is not None and peg<=cfg.MAX_PEG_RATIO,
                             f"{peg:.2f}" if peg is not None else "N/A")
    soft_flags = []
    ir = data["inventory_ratio"]
    if ir is not None and ir>cfg.MAX_INVENTORY_RATIO:
        soft_flags.append(f"Inventory growing {ir:.2f}x revenue")
    rr = data["receivables_ratio"]
    if rr is not None and rr>cfg.MAX_RECEIVABLES_RATIO:
        soft_flags.append(f"Receivables growing {rr:.2f}x revenue")
    if data.get("promoter_trend")=="decreasing":
        soft_flags.append(f"Promoter holding declining QoQ ({data['promoter_holding']:.1f}%)" if data["promoter_holding"] else "Promoter declining QoQ")
    if data.get("margin_contracting"):
        soft_flags.append(f"Operating margin contracting {data.get('_opm')}")
    return all(v["pass"] for v in r.values()),r,soft_flags

def classify_fails(criteria):
    real,data_f = [],[]
    for k,v in criteria.items():
        if not v["pass"]:
            if v["label"]=="N/A": data_f.append((k,v["label"],None))
            else:
                gap = miss_pct(v["value"],k)
                real.append((k,v["label"],gap))
    return real,data_f

# ── Claude AI ─────────────────────────────────────────────────────────────────

def ai_assess(sym,data,basic):
    prompt = f"""You are evaluating a stock for a long-term value investing service.

PHILOSOPHY
  - Cash flow cannot be fudged. Prioritise operating cash flow over reported earnings.
  - What you pay determines returns more than business quality.
  - Three moat types: LOW_COST_PRODUCER, DIFFERENTIATED_PRODUCT, PROPRIETARY_ADVANTAGE.
  - Capital allocation matters as much as capital efficiency.
  - Avoid rapidly changing businesses. Seek stable, understandable ones.
  - Compounding and patience are the only edge available to retail investors.

COMPANY: {data['company_name']} (NSE: {sym})
SECTOR:  {basic['sector']} | INDUSTRY: {basic['industry']}
ABOUT:   {data['about'] or 'Not available'}

FINANCIALS (all 9 hard criteria passed):
  ROE {data['roe']}% | D/E {data['de_ratio']} | Rev CAGR 5Y {data['revenue_growth_5y']}%
  ROCE 3Y avg {data['roce_3y_avg']}% | ROCE hist {data['_roce']}
  FCF/PAT {data['fcf_to_pat']} | Promoter {data['promoter_holding']}% | PEG {data['peg_ratio']}

EVALUATE THESE SEVEN THINGS:
1. MOAT — LOW_COST_PRODUCER, DIFFERENTIATED_PRODUCT, PROPRIETARY_ADVANTAGE, or NONE.
2. CAPITAL ALLOCATION — rate EXCELLENT/GOOD/AVERAGE/POOR + one sentence.
3. CASH FLOW QUALITY — CLEAN/MINOR_CONCERN/SIGNIFICANT_CONCERN + one sentence.
4. FORENSIC FLAGS — CLEAN/MINOR_FLAGS/SIGNIFICANT_FLAGS + specific observation.
5. BUSINESS STABILITY — VERY_STABLE/STABLE/MODERATELY_CHANGING/RAPIDLY_CHANGING.
6. BEAR CASE — single most specific dangerous risk. Not generic.
7. PEER COMPARISON — 2-3 named Indian competitors. Best in sector?

Return ONLY this JSON:
{{"score":<1-10>,"verdict":"STRONG PASS|PASS|BORDERLINE|FAIL",
"primary_revenue_source":"<one sentence>",
"moat_category":"LOW_COST_PRODUCER|DIFFERENTIATED_PRODUCT|PROPRIETARY_ADVANTAGE|NONE",
"moat_explanation":"<one sentence>","market_share":"<estimated position>",
"management_quality":"EXCELLENT|GOOD|AVERAGE|POOR","capital_allocation_note":"<one sentence>",
"cash_flow_quality":"CLEAN|MINOR_CONCERN|SIGNIFICANT_CONCERN","cash_flow_note":"<one sentence>",
"forensic_risk":"CLEAN|MINOR_FLAGS|SIGNIFICANT_FLAGS","forensic_note":"<specific observation>",
"business_stability":"VERY_STABLE|STABLE|MODERATELY_CHANGING|RAPIDLY_CHANGING",
"credit_resilience":"STRONG|ADEQUATE|WEAK","demand_outlook_15y":"GROWING|STABLE|UNCERTAIN|DECLINING",
"disruption_risk":"LOW|MEDIUM|HIGH","bear_case":"<most specific risk>",
"peer_comparison":"<vs 2-3 named competitors>","key_risks":"<2 risks comma-separated>",
"reasoning":"<4-5 sentences bull and bear balanced>"}}"""
    text=""
    try:
        resp = Anthropic(api_key=cfg.ANTHROPIC_API_KEY).messages.create(
            model="claude-sonnet-4-6",max_tokens=1200,
            messages=[{"role":"user","content":prompt}])
        text = resp.content[0].text.strip()
        text = re.sub(r"```json?\s*","",text).replace("```","").strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        log.error(f"JSON error {sym}: {e}\n{text[:200]}")
        return {"score":0,"verdict":"FAIL","reasoning":f"JSON error: {e}"}
    except Exception as e:
        log.error(f"AI failed {sym}: {e}")
        return {"score":0,"verdict":"FAIL","reasoning":str(e)}

# ── Technical Data ─────────────────────────────────────────────────────────────

def get_technical_data(sym):
    try:
        t=yf.Ticker(f"{sym}.NS"); h1=t.history(period="1y")
        if h1.empty: return {}
        cur=h1["Close"].iloc[-1]
        h14=t.history(period="14mo")
        dma=round(h14["Close"].rolling(200).mean().iloc[-1],2) if len(h14)>=200 else None
        cl=h1["Close"].tail(15); d=cl.diff().dropna()
        g=d.clip(lower=0).mean(); l=(-d.clip(upper=0)).mean()
        rsi=round(100-(100/(1+g/l)),1) if l>0 else 100.0
        return {"current":round(cur,2),"high_52w":round(h1["Close"].max(),2),
                "low_52w":round(h1["Close"].min(),2),
                "pct_above_low":round(((cur-h1["Close"].min())/h1["Close"].min())*100,1),
                "dma_200":dma,"above_200dma":bool(cur>dma) if dma else None,"rsi":rsi}
    except Exception as e: log.debug(f"Tech {sym}: {e}"); return {}

# ── Links ─────────────────────────────────────────────────────────────────────

def screener_url(sym): return SCREENER_LINK.format(sym=sym)
def tg_link(display,url): return f"[{display}]({url})"
def sym_link(sym): return tg_link(sym,screener_url(sym))
def _score_bar(score,width=10):
    f=max(0,min(width,round(score))); return "█"*f+"░"*(width-f)

# ── Watchlist Helpers ─────────────────────────────────────────────────────────

def get_previous_watchlist(wl_log,today_str):
    d=datetime.strptime(today_str,"%Y-%m-%d").date()
    for i in range(1,8):
        prev=(d-timedelta(days=i)).strftime("%Y-%m-%d")
        if prev in wl_log: return wl_log[prev]
    return []

def get_days_on_watchlist(wl_log,sym,today_str):
    d=datetime.strptime(today_str,"%Y-%m-%d").date(); count=0
    for i in range(365):
        dd=(d-timedelta(days=i)).strftime("%Y-%m-%d")
        if sym in wl_log.get(dd,[]): count+=1
        elif count>0: break
    return count

def get_entry_date(wl_log,sym,today_str):
    d=datetime.strptime(today_str,"%Y-%m-%d").date(); entry=None
    for i in range(365):
        dd=(d-timedelta(days=i)).strftime("%Y-%m-%d")
        if sym in wl_log.get(dd,[]): entry=dd
        elif entry: break
    return entry

def get_30day_eligible(wl_log,reference_date):
    counts={}
    for i in range(30):
        d=(reference_date-timedelta(days=i+1)).strftime("%Y-%m-%d")
        for sym in wl_log.get(d,[]):
            counts[sym]=counts.get(sym,0)+1
    return counts

# ── Near-Miss Logging ─────────────────────────────────────────────────────────

def log_near_miss(sym,name,real_fails,date_str):
    exists=os.path.exists(NEAR_MISS_LOG_PATH)
    with open(NEAR_MISS_LOG_PATH,"a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        if not exists: w.writerow(["date","symbol","name","failed_count","criterion","detail","miss_pct"])
        for criterion,detail,gap in real_fails:
            w.writerow([date_str,sym,name,len(real_fails),criterion,detail,gap or ""])

def get_weekly_near_misses(today):
    if not os.path.exists(NEAR_MISS_LOG_PATH): return []
    cutoff=today-timedelta(days=7); results={}
    try:
        with open(NEAR_MISS_LOG_PATH,"r",encoding="utf-8") as f:
            reader=csv.DictReader(f)
            for row in reader:
                try:
                    rd=datetime.strptime(row["date"],"%Y-%m-%d").date()
                    if rd<cutoff: continue
                    sym=row["symbol"]
                    if sym not in results or rd>datetime.strptime(results[sym]["date"],"%Y-%m-%d").date():
                        results[sym]={"date":row["date"],"symbol":sym,"name":row["name"],
                                      "failed_count":int(row["failed_count"]),"criteria":[]}
                    gap=row.get("miss_pct","")
                    results[sym]["criteria"].append((row["criterion"],row["detail"],float(gap) if gap else None))
                except: pass
    except Exception as e: log.warning(f"NM log: {e}")
    def sort_key(nm):
        gaps=[g for _,_,g in nm["criteria"] if g is not None]
        return min(gaps) if gaps else 999
    return sorted(results.values(),key=sort_key)

# ── Dedup ─────────────────────────────────────────────────────────────────────

def was_recently_alerted(sym,alerts_log):
    if sym not in alerts_log: return False
    try:
        last=datetime.strptime(alerts_log[sym],"%Y-%m-%d").date()
        return (date.today()-last).days<cfg.ALERT_COOLDOWN_DAYS
    except: return False

def mark_alerted(sym,alerts_log,today_str): alerts_log[sym]=today_str

# ── SIP Tracker ───────────────────────────────────────────────────────────────

def _fetch_price(sym,target_date):
    try:
        ts=sym if sym.startswith("^") else f"{sym}.NS"
        h=yf.Ticker(ts).history(start=target_date.strftime("%Y-%m-%d"),
                                  end=(target_date+timedelta(days=7)).strftime("%Y-%m-%d"))
        return round(float(h["Close"].iloc[0]),2) if not h.empty else None
    except: return None

def _fetch_current_price(sym):
    try:
        ts=sym if sym.startswith("^") else f"{sym}.NS"
        h=yf.Ticker(ts).history(period="5d")
        return round(float(h["Close"].iloc[-1]),2) if not h.empty else None
    except: return None

def calculate_monthly_sip(wl_log,perf_log,today):
    month_key=today.strftime("%Y-%m")
    if month_key in [e["month"] for e in perf_log.get("sip_entries",[])]:
        log.info(f"SIP done {month_key}"); return
    eligible=get_30day_eligible(wl_log,today); bp=_fetch_price(cfg.BENCHMARK_TICKER,today)
    if not eligible:
        gp=_fetch_price(cfg.GOLD_ETF_SYMBOL,today)
        entry={"month":month_key,"date":today.strftime("%Y-%m-%d"),"type":"gold_fallback",
               "allocations":[{"symbol":cfg.GOLD_ETF_SYMBOL,"name":"GOLDBEES","price_entry":gp,
                                "shares":round(cfg.SIP_AMOUNT_INR/gp,4) if gp else 0}],
               "amount_per_stock":cfg.SIP_AMOUNT_INR,"total_deployed":cfg.SIP_AMOUNT_INR,"benchmark_price_entry":bp}
    else:
        ae=cfg.SIP_AMOUNT_INR/len(eligible)
        allocs=[{"symbol":s,"price_entry":p,"shares":round(ae/p,4),"days_on_list":eligible[s]}
                for s in eligible for p in [_fetch_price(s,today)] if p and p>0]
        entry={"month":month_key,"date":today.strftime("%Y-%m-%d"),"type":"stocks","allocations":allocs,
               "amount_per_stock":round(ae,2),"total_deployed":cfg.SIP_AMOUNT_INR,"benchmark_price_entry":bp}
    perf_log.setdefault("sip_entries",[]).append(entry)
    save_json(PERFORMANCE_LOG_PATH,perf_log); log.info(f"SIP recorded {month_key}")

def get_portfolio_performance(perf_log):
    results=[]; bc=_fetch_current_price(cfg.BENCHMARK_TICKER)
    for entry in perf_log.get("sip_entries",[]):
        res={"month":entry["month"],"date":entry["date"],"type":entry["type"],
             "total_deployed":entry["total_deployed"],"allocations":[],"total_current":0.0,
             "return_pct":None,"bench_pct":None,"alpha":None}
        be=entry.get("benchmark_price_entry")
        if be and bc: res["bench_pct"]=round(((bc-be)/be)*100,2)
        tc=0.0
        for a in entry.get("allocations",[]):
            cp=_fetch_current_price(a["symbol"]); ep=a.get("price_entry"); sh=a.get("shares",0)
            if cp and ep and sh:
                cv=sh*cp
                res["allocations"].append({**a,"price_current":cp,"current_value":round(cv,2),
                                           "pct_change":round(((cp-ep)/ep)*100,2)})
                tc+=cv
            else: res["allocations"].append({**a,"price_current":None})
        if tc>0:
            res["total_current"]=round(tc,2)
            res["return_pct"]=round(((tc-entry["total_deployed"])/entry["total_deployed"])*100,2)
            if res["bench_pct"] is not None: res["alpha"]=round(res["return_pct"]-res["bench_pct"],2)
        results.append(res)
    return results

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_whatsapp(text):
    url=f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks=[]
    while len(text)>4000:
        split_at=text.rfind("\n",0,4000)
        if split_at==-1: split_at=4000
        chunks.append(text[:split_at]); text=text[split_at:].lstrip("\n")
    chunks.append(text)
    all_sent=True
    for chunk in chunks:
        try:
            r=requests.post(url,json={"chat_id":cfg.TELEGRAM_CHAT_ID,"text":chunk,"parse_mode":"Markdown"},timeout=15)
            if r.status_code==200: log.info("Telegram sent ✅")
            else: log.error(f"Telegram {r.status_code}: {r.text[:200]}"); all_sent=False
            time.sleep(1)
        except Exception as e: log.error(f"Telegram failed: {e}"); all_sent=False
    return all_sent

# ── Message Formatting ────────────────────────────────────────────────────────

def format_stock_alert(basic,data,criteria,soft_flags,ai,tech,today_wl,today_wl_scores):
    sym=basic["symbol"]; name=data["company_name"]
    verdict=ai.get("verdict","N/A"); score=ai.get("score",0)
    vi={"STRONG PASS":"🟢","PASS":"✅","BORDERLINE":"🟡","FAIL":"🔴"}.get(verdict,"⚪")
    mi={"EXCELLENT":"🟢","GOOD":"✅","AVERAGE":"🟡","POOR":"🔴"}.get(ai.get("management_quality",""),"⚪")
    ci={"CLEAN":"✅","MINOR_CONCERN":"⚠️","SIGNIFICANT_CONCERN":"🚨"}.get(ai.get("cash_flow_quality",""),"⚪")
    fi={"CLEAN":"✅","MINOR_FLAGS":"⚠️","SIGNIFICANT_FLAGS":"🚨"}.get(ai.get("forensic_risk",""),"⚪")
    si={"VERY_STABLE":"🟢","STABLE":"✅","MODERATELY_CHANGING":"🟡","RAPIDLY_CHANGING":"🔴"}.get(ai.get("business_stability",""),"⚪")
    ml={"LOW_COST_PRODUCER":"Low cost producer","DIFFERENTIATED_PRODUCT":"Differentiated product",
        "PROPRIETARY_ADVANTAGE":"Proprietary advantage","NONE":"No durable moat"}.get(ai.get("moat_category",""),"N/A")
    crit_lines="\n".join(f"  {k:<20} {CRITERIA_META.get(k,{}).get('label',''):<16} {v['label']}   ✅"
                         for k,v in criteria.items())
    soft_block=("\n\nSOFT FLAGS\n"+"\n".join(f"  ⚠️ {f}" for f in soft_flags)) if soft_flags else ""
    tech_block=""
    if tech:
        di="✅" if tech.get("above_200dma") is True else "❌" if tech.get("above_200dma") is False else "—"
        rsi=tech.get("rsi","—")
        rl="overbought 🔴" if isinstance(rsi,float) and rsi>70 else "oversold 🟢" if isinstance(rsi,float) and rsi<30 else "neutral"
        dm=f"Rs{tech['dma_200']:,.0f} ({di})" if tech.get("dma_200") else "N/A"
        tech_block=(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📈 TECHNICAL  _(reference only)_\n\n"
                    f"  Price Rs{tech['current']:,.0f}  |  RSI {rsi} {rl}\n"
                    f"  52W  Rs{tech['low_52w']:,.0f} – Rs{tech['high_52w']:,.0f}\n"
                    f"  200DMA {dm}  |  {tech['pct_above_low']:.0f}% above 52W low")
    existing=sorted([(s,today_wl_scores.get(s,0)) for s in today_wl if s!=sym],key=lambda x:x[1])
    wl_block=""
    if existing:
        wl_lines="\n".join(f"  {sym_link(s):<35} {sc}/10"+(" <- consider replacing" if i==0 else "")
                           for i,(s,sc) in enumerate(existing))
        wl_block=(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📌 CURRENT WATCHLIST\n"
                  f"_(If fully invested, consider replacing lowest score)_\n\n{wl_lines}")
    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📈 NEW ALERT\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{tg_link(f'*{name}*',screener_url(sym))}\n"
            f"NSE: {sym}  |  {basic['sector']}\nRs{basic['market_cap_cr']:,.0f}Cr  |  "
            f"{datetime.now().strftime('%d %b %Y, %I:%M %p IST')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nPASSED ALL {len(criteria)} CRITERIA\n\n"
            f"  {'Criterion':<20} {'Threshold':<16} Actual\n  {'─'*54}\n{crit_lines}{soft_block}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"AI ASSESSMENT   {score}/10  {vi} {verdict}\n[{_score_bar(score)}]\n\n"
            f"  Revenue    {ai.get('primary_revenue_source','N/A')}\n"
            f"  Moat       {ml}\n             _{ai.get('moat_explanation','')}_\n"
            f"  Mkt Share  {ai.get('market_share','N/A')}\n\n"
            f"  Management {mi} {ai.get('management_quality','N/A')}\n             _{ai.get('capital_allocation_note','')}_\n\n"
            f"  Cash Flow  {ci} {ai.get('cash_flow_quality','N/A')}\n             _{ai.get('cash_flow_note','')}_\n\n"
            f"  Forensics  {fi} {ai.get('forensic_risk','N/A')}\n             _{ai.get('forensic_note','')}_\n\n"
            f"  Stability  {si} {ai.get('business_stability','N/A')}\n"
            f"  Demand 15Y {ai.get('demand_outlook_15y','N/A')}\n"
            f"  Disruption {ai.get('disruption_risk','N/A')}\n\n"
            f"  🐻 Bear case\n  {ai.get('bear_case','N/A')}\n\n"
            f"  🏆 Vs peers\n  {ai.get('peer_comparison','N/A')}\n\n"
            f"  🚨 Watch: {ai.get('key_risks','N/A')}\n\n"
            f"  _{ai.get('reasoning','')}_"
            f"{tech_block}{wl_block}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{tg_link('📄 Full Report on Substack ->',screener_url(sym))}\n"
            f"_(Link updates when report is published)_\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Not a buy recommendation. Do your own due diligence.\nSEBI RA: {cfg.SEBI_RA_NUMBER}")

def format_exit_notice(sym,name,real_fails,days,entry):
    fl="\n".join(f"  ❌ {k}: {v}" for k,v,_ in real_fails)
    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📤 WATCHLIST EXIT\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{tg_link(f'*{name}*',screener_url(sym))} left the watchlist.\n\n"
            f"Failed:\n{fl}\nDays on list: {days}"
            +(f"\nEntry: {entry}" if entry else "")+
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

def format_daily_summary(run_stats,today_wl,today_wl_scores,ai_results,wl_log,today_str):
    ts=datetime.now().strftime("%a, %d %b %Y  |  %I:%M %p IST")
    na=run_stats.get("alerts",0)
    an=f"🔔 {na} new alert(s) sent above ↑" if na>0 else "🔕 No new alerts today"
    wl_block=(
        f"📌 WATCHLIST  ({len(today_wl)} stocks)\n\n"+
        "\n".join(f"  {sym_link(s):<35} Day {get_days_on_watchlist(wl_log,s,today_str):<4} {today_wl_scores.get(s,0)}/10"
                  for s in today_wl)
        if today_wl else "📌 WATCHLIST\n  Empty — no stocks currently qualify"
    )

    # AI Rankings — shown ABOVE near-misses
    ai_block=""
    if ai_results:
        def vi(v,s):
            if v in ("STRONG PASS","PASS") and s>=6: return "✅"
            if v=="BORDERLINE" or s>=5: return "🟡"
            return "🔴"
        passed=[r for r in ai_results if r["verdict"] in ("STRONG PASS","PASS") and r["score"]>=6]
        queue=sorted([r for r in ai_results if not (r["verdict"] in ("STRONG PASS","PASS") and r["score"]>=6)],
                     key=lambda x:x["score"],reverse=True)
        passed_lines="\n".join(f"  {sym_link(r['sym']):<35} {r['score']:.1f}/10  ✅ {r['verdict']}"
                               for r in passed) if passed else "  None today"
        queue_lines="\n".join(f"  {sym_link(r['sym']):<35} {r['score']:.1f}/10  {vi(r['verdict'],r['score'])}"
                              for r in queue) if queue else "  None"
        ai_block=(f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                  f"🤖 AI RESULTS  ({len(ai_results)} stocks assessed)\n\n"
                  f"✅ PASSED FILTER\n{passed_lines}\n\n"
                  f"📋 SUBSTACK QUEUE  _(highest to lowest — your judgment)_\n{queue_lines}")

    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📊 Daily screen\n{ts}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Checked {run_stats.get('total','—')}  |  {run_stats.get('pass1','—')} filter  |  "
            f"{run_stats.get('pass2','—')} quant  |  {run_stats.get('pass3_ok','—')} AI\n\n{an}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{wl_block}{ai_block}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nNot investment advice  |  SEBI RA: {cfg.SEBI_RA_NUMBER}")

def format_weekly_report(today,wl_log,perf_results,weekly_nms):
    ts=today.strftime("%d %b %Y")
    if weekly_nms:
        ones=[nm for nm in weekly_nms if nm["failed_count"]==1]
        twos=[nm for nm in weekly_nms if nm["failed_count"]==2]
        def nm_line(nm):
            parts=[]
            for criterion,detail,gap in nm["criteria"]:
                gap_str=f" (missed by {gap:.0f}%)" if gap is not None else ""
                parts.append(f"{criterion}: {detail}{gap_str}")
            return f"  {sym_link(nm['symbol']):<35} {',  '.join(parts)}"
        secs=[]
        if ones: secs.append("Missed by 1 criterion:\n"+"\n".join(nm_line(nm) for nm in ones))
        if twos: secs.append("Missed by 2 criteria:\n"+"\n".join(nm_line(nm) for nm in twos))
        nb="⚠️ NEAR-MISSES THIS WEEK\n_(sorted closest to qualifying first)_\n\n"+"\n\n".join(secs)
    else: nb="⚠️ NEAR-MISSES THIS WEEK\n  None this week."

    if not perf_results: pb="💰 PAPER PORTFOLIO\n  No SIP entries yet."
    else:
        batches=[]; td=tc=0.0
        for res in sorted(perf_results,key=lambda x:x["month"],reverse=True):
            al_lines=[]
            for a in res.get("allocations",[]):
                chg=a.get("pct_change"); chg_s=f"{chg:+.1f}%" if chg is not None else "pending"
                al_lines.append(f"    {sym_link(a['symbol']):<30} Rs{a.get('price_entry','—')} -> Rs{a.get('price_current','—')}  {chg_s}")
            r=res.get("return_pct"); b=res.get("bench_pct"); al2=res.get("alpha")
            rs=f"{r:+.1f}%" if r is not None else "pending"
            vs=(f"vs Nifty50: {b:+.1f}%  |  Alpha: {al2:+.1f}%" if b is not None and al2 is not None else "pending")
            beat="✅" if al2 is not None and al2>=0 else "❌"
            btype="🥇 Gold" if res["type"]=="gold_fallback" else "📈 Stocks"
            batches.append(f"─ {res['month']}  ({btype})\n"+"\n".join(al_lines)+f"\n  Return: *{rs}*  {beat}\n  {vs}")
            td+=res.get("total_deployed",0); tc+=res.get("total_current") or res.get("total_deployed",0)
        ar=(tc-td)/td*100 if td>0 else 0; m=len(perf_results)
        gm=sum(1 for r in perf_results if r["type"]=="gold_fallback")
        at=(f"─ ALL-TIME ({m} month{'s' if m!=1 else ''})\n"
            f"  Deployed: Rs{td:,.0f}  |  Value: Rs{tc:,.0f}\n"
            f"  Return: {ar:+.1f}%  |  Gold months: {gm}/{m}")
        pb=(f"💰 PAPER PORTFOLIO\nRs{cfg.SIP_AMOUNT_INR/100_000:.0f}L SIP on 1st  |  Benchmark: Nifty 50\n\n"
            +"\n\n".join(batches)+"\n\n"+at)

    return (f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n📋 WEEKLY REPORT — Sat, {ts}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n{nb}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{pb}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Paper portfolio only. Not investment advice.\nSEBI RA: {cfg.SEBI_RA_NUMBER}")

# ── Saturday Mode ─────────────────────────────────────────────────────────────

def run_weekly_report_only():
    today=date.today()
    perf_log=load_json(PERFORMANCE_LOG_PATH,{"sip_entries":[]})
    wl_log=load_json(WATCHLIST_LOG_PATH,{})
    log.info("Saturday mode — generating weekly report…")
    perf_results=get_portfolio_performance(perf_log)
    weekly_nms=get_weekly_near_misses(today)
    send_whatsapp(format_weekly_report(today,wl_log,perf_results,weekly_nms))
    log.info("Weekly report sent ✅")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    mode=os.getenv("SCREENER_MODE","screener").lower()
    if mode=="weekly_report":
        run_weekly_report_only(); return

    today=date.today(); today_str=today.strftime("%Y-%m-%d"); is_first=today.day==1
    log.info("="*60)
    log.info(f"NSE Screener v2.2  |  {today.strftime('%A, %d %b %Y')}")
    log.info("="*60)

    alerts_log=load_json(ALERTS_LOG_PATH,{})
    wl_log=load_json(WATCHLIST_LOG_PATH,{})
    perf_log=load_json(PERFORMANCE_LOG_PATH,{"sip_entries":[]})
    ai_log=load_json(AI_ASSESSMENTS_PATH,{})

    if is_first:
        log.info("\nSIP Day…"); calculate_monthly_sip(wl_log,perf_log,today)

    log.info("\nLoading NSE universe…")
    symbols=fetch_nse_all()
    if not symbols: log.error("No symbols."); sys.exit(1)
    log.info(f"  {len(symbols)} symbols")

    log.info(f"\nPass 1 ({len(symbols)} stocks)…")
    p1=pass1_yfinance(symbols)
    if not p1: log.info("None survived."); return
    log.info(f"  {len(p1)} survived")

    prev_wl=get_previous_watchlist(wl_log,today_str)
    prev_wl_set=set(prev_wl); p1_set={b["symbol"] for b in p1}
    for sym in prev_wl_set-p1_set:
        days=get_days_on_watchlist(wl_log,sym,today_str); entry=get_entry_date(wl_log,sym,today_str)
        send_whatsapp(format_exit_notice(sym,sym,[("Pass 1","Market cap or BFSI",None)],days,entry))
        time.sleep(3)

    log.info(f"\nPass 2 ({len(p1)} stocks)…")
    qualified=[]; near_miss_today=[]
    for idx,basic in enumerate(p1,1):
        sym=basic["symbol"]
        log.info(f"  [{idx:>3}/{len(p1)}] {sym}…")
        try:
            data=get_fundamentals(sym)
            if not data: time.sleep(2.5); continue
            if not data["pe_ratio"] and basic.get("pe_yf"):
                data["pe_ratio"]=basic["pe_yf"]
                if data.get("eps_growth_5y") and data["eps_growth_5y"]>0:
                    data["peg_ratio"]=round(data["pe_ratio"]/data["eps_growth_5y"],2)
            passed,criteria,soft_flags=quant_filter(data,basic)
            real_fails,data_fails=classify_fails(criteria)
            if passed:
                log.info(f"  ✅  {sym}"); qualified.append((basic,data,criteria,soft_flags))
            else:
                if sym in prev_wl_set:
                    days=get_days_on_watchlist(wl_log,sym,today_str); entry=get_entry_date(wl_log,sym,today_str)
                    send_whatsapp(format_exit_notice(sym,data["company_name"],real_fails,days,entry))
                    prev_wl_set.discard(sym); time.sleep(3)
                if real_fails and len(real_fails)<=cfg.NEAR_MISS_MAX_FAILS:
                    all_close = all(
                        gap is not None and gap <= 30
                        for _,_,gap in real_fails
                    )
                    if all_close:
                        log_near_miss(sym,data["company_name"],real_fails,today_str)
        except Exception as e: log.error(f"  Error {sym}: {e}")
        time.sleep(2.5)
    log.info(f"\n  Pass 2: {len(qualified)} qualified  |  {len(near_miss_today)} near-misses (1-crit)")

    log.info(f"\nPass 3 — AI ({len(qualified)} stocks)…")
    today_wl=[]; today_wl_scores={}; ai_results=[]; alerts_sent=pass3_ok=0
    for basic,data,criteria,soft_flags in qualified:
        sym=basic["symbol"]
        log.info(f"  Assessing {sym}…")
        ai=ai_assess(sym,data,basic); score=ai.get("score",0); verdict=ai.get("verdict","FAIL")
        log.info(f"  -> {sym}: {verdict} ({score}/10)")
        ai_results.append({"sym":sym,"name":data["company_name"],"score":score,"verdict":verdict})
        ai_log[today_str]=ai_log.get(today_str,{})
        ai_log[today_str][sym]={"score":score,"verdict":verdict,**ai}
        if verdict in ("STRONG PASS","PASS") and score>=6.0:
            pass3_ok+=1; today_wl.append(sym); today_wl_scores[sym]=score
            if not was_recently_alerted(sym,alerts_log):
                tech=get_technical_data(sym)
                send_whatsapp(format_stock_alert(basic,data,criteria,soft_flags,ai,tech,today_wl,today_wl_scores))
                mark_alerted(sym,alerts_log,today_str); alerts_sent+=1; time.sleep(5)
            else: log.info(f"  Suppressed {sym}")
        time.sleep(1.5)

    wl_log[today_str]=today_wl
    save_json(WATCHLIST_LOG_PATH,wl_log); save_json(ALERTS_LOG_PATH,alerts_log)
    save_json(AI_ASSESSMENTS_PATH,ai_log)

    run_stats={"total":len(symbols),"pass1":len(p1),"pass2":len(qualified),"pass3_ok":pass3_ok,"alerts":alerts_sent}
    send_whatsapp(format_daily_summary(run_stats,today_wl,today_wl_scores,ai_results,wl_log,today_str))

    log.info(f"\n{'='*60}\nDone. {alerts_sent} alert(s). Watchlist: {len(today_wl)}.\n{'='*60}\n")

if __name__=="__main__":
    main()
