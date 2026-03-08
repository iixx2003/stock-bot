"""StockBot Pro — Panel de control web v2 (multi-página, premium design)"""

from flask import Flask, render_template_string, session, redirect, url_for, request, jsonify
import json, os, time, threading
from datetime import datetime, timedelta, date as _dt_date
import pytz

try:
    import yfinance as yf
    _HAS_YF = True
except ImportError:
    _HAS_YF = False

# Caché de precios: ticker → (price, change_pct, timestamp)
_price_cache = {}
_price_lock  = threading.Lock()
_PRICE_TTL   = 180   # 3 min

# Caché de earnings: ticker → (date_str_or_None, timestamp)
_earnings_cache = {}
_earnings_lock  = threading.Lock()
_EARNINGS_TTL   = 3600  # 1 h

app = Flask(__name__)
app.secret_key = "stk_web_2026_xK9mP_secreto"
PASSWORD = "stockbot2026"
SPAIN_TZ = pytz.timezone("Europe/Madrid")
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")

# ─── helpers ──────────────────────────────────────────────────────────────────

def _rjson(name, default):
    try:
        p = os.path.join(DATA_DIR, name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def is_market_open():
    now = datetime.now(SPAIN_TZ)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 930 <= t <= 1320   # 15:30–22:00 ES


def is_premarket_now():
    now = datetime.now(SPAIN_TZ)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 540 <= t < 930     # 09:00–15:30 ES


def _fetch_price(ticker):
    """Devuelve (price, change_pct) con caché de 3 min. Nunca lanza excepción."""
    now_ts = time.time()
    with _price_lock:
        cached = _price_cache.get(ticker)
        if cached and now_ts - cached[2] < _PRICE_TTL:
            return cached[0], cached[1]
    if not _HAS_YF:
        return None, None
    try:
        fi = yf.Ticker(ticker).fast_info
        price  = round(float(fi.last_price), 2)   if fi.last_price  else None
        prev   = float(fi.previous_close)          if fi.previous_close else None
        chg    = round((price - prev) / prev * 100, 2) if price and prev else None
        with _price_lock:
            _price_cache[ticker] = (price, chg, now_ts)
        return price, chg
    except Exception:
        return None, None


def _fetch_earnings_date(ticker):
    """Próxima fecha de earnings (YYYY-MM-DD) o None. Caché 1 h."""
    now_ts = time.time()
    with _earnings_lock:
        cached = _earnings_cache.get(ticker)
        if cached is not None and now_ts - cached[1] < _EARNINGS_TTL:
            return cached[0]
    result = None
    if _HAS_YF:
        try:
            today = _dt_date.today()
            t = yf.Ticker(ticker)
            # earnings_dates es un DataFrame con índice DatetimeTZ
            ef = t.earnings_dates
            if ef is not None and not ef.empty:
                future = [idx for idx in ef.index if idx.date() >= today]
                if future:
                    result = str(sorted(future)[0])[:10]
            if result is None:
                cal = t.calendar
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        result = str(ed[0] if isinstance(ed, (list, tuple)) else ed)[:10]
        except Exception:
            pass
    with _earnings_lock:
        _earnings_cache[ticker] = (result, now_ts)
    return result


def _days_elapsed(date_str):
    if not date_str:
        return 0
    try:
        d = datetime.fromisoformat(date_str)
        if d.tzinfo is None:
            d = SPAIN_TZ.localize(d)
        return max(0, (datetime.now(SPAIN_TZ) - d).days)
    except Exception:
        return 0


def _infer_exit_reason(p):
    """Infiere motivo de salida si no está guardado."""
    r = p.get("exit_reason")
    if r:
        return r
    if p.get("result") == "win":
        return "TARGET"
    days = p.get("days_to_result") or _days_elapsed(p.get("date"))
    return "EXPIRADA" if days >= 29 else "STOP"


# ─── payload ──────────────────────────────────────────────────────────────────

def build_payload():
    preds     = _rjson("predictions.json", [])
    regime    = _rjson("regime.json", {})
    learnings = _rjson("learnings.json", {"rules": []})
    econ      = _rjson("econ_calendar.json", {})
    mctx      = _rjson("market_context.json", {})

    pending = sorted(
        [p for p in preds if p.get("result") == "pending"],
        key=lambda x: x.get("date", ""), reverse=True
    )
    wins     = sum(1 for p in preds if p.get("result") == "win")
    losses   = sum(1 for p in preds if p.get("result") == "loss")
    resolved = wins + losses
    accuracy = round(wins / resolved * 100, 1) if resolved > 0 else 0

    # Enrich pending (incluye precio actual en tiempo real)
    for p in pending:
        entry  = p.get("entry", 0) or 0
        target = p.get("target", 0) or 0
        stop   = p.get("stop",   0) or 0
        days   = _days_elapsed(p.get("date"))
        p["days_elapsed"]   = days
        p["days_remaining"] = max(0, 30 - days)
        p["target_pct"]     = round((target - entry) / entry * 100, 1) if entry > 0 and target > 0 else None

        # Earnings durante el período de la señal
        ed_str = _fetch_earnings_date(p.get("ticker", ""))
        p["earnings_date"] = None
        p["earnings_during"] = False
        if ed_str:
            try:
                edate = _dt_date.fromisoformat(ed_str)
                today = _dt_date.today()
                end_of_signal = today + timedelta(days=p["days_remaining"])
                p["earnings_date"] = ed_str
                p["earnings_during"] = today <= edate <= end_of_signal
            except Exception:
                pass

        # Precio actual + % cambio desde entrada
        cur_price, cur_chg = _fetch_price(p.get("ticker", ""))
        p["current_price"] = cur_price
        p["current_chg"]   = cur_chg    # % diario
        if cur_price and entry > 0:
            raw_vs_entry = (cur_price - entry) / entry * 100
            p["vs_entry_pct"] = round(raw_vs_entry if p.get("signal") == "COMPRAR" else -raw_vs_entry, 2)
            # Progreso real hacia target
            if target > entry and p.get("signal") == "COMPRAR":
                p["real_progress"] = max(0, min(100, round((cur_price - entry) / (target - entry) * 100)))
            elif target < entry and p.get("signal") == "VENDER":
                p["real_progress"] = max(0, min(100, round((entry - cur_price) / (entry - target) * 100)))
            else:
                p["real_progress"] = None
        else:
            p["vs_entry_pct"]  = None
            p["real_progress"] = None

    recent = sorted(
        [p for p in preds if p.get("result") in ("win", "loss")],
        key=lambda x: x.get("date", ""), reverse=True
    )[:25]

    # Enrich recent
    for p in recent:
        entry      = p.get("entry", 0) or 0
        exit_price = p.get("exit_price", 0) or 0
        if entry > 0 and exit_price > 0:
            raw = (exit_price - entry) / entry * 100
            p["pl_pct"] = round(raw if p.get("signal") == "COMPRAR" else -raw, 1)
        else:
            p["pl_pct"] = None
        p["exit_reason_label"] = _infer_exit_reason(p)

    by_type = {}
    for p in preds:
        if p.get("result") in ("win", "loss"):
            t = p.get("signal_type", "NORMAL")
            if t not in by_type:
                by_type[t] = {"w": 0, "l": 0}
            by_type[t]["w" if p["result"] == "win" else "l"] += 1

    by_sector = {}
    for p in preds:
        if p.get("result") in ("win", "loss"):
            s = p.get("sector") or "Unknown"
            if s not in by_sector:
                by_sector[s] = {"w": 0, "l": 0}
            by_sector[s]["w" if p["result"] == "win" else "l"] += 1

    fg = mctx.get("fear_greed", 50)
    fg_label = (
        "Miedo Extremo" if fg < 25 else
        "Miedo"         if fg < 45 else
        "Neutral"       if fg < 55 else
        "Codicia"       if fg < 75 else
        "Codicia Extrema"
    )
    fg_color = (
        "#ff3b5c" if fg < 25 else
        "#ff6b35" if fg < 45 else
        "#f5a623" if fg < 55 else
        "#7bed9f" if fg < 75 else
        "#00e07a"
    )

    vix   = mctx.get("vix", 0) or 0
    sp500 = mctx.get("sp500_change", 0) or 0
    now   = datetime.now(SPAIN_TZ)
    wd    = now.weekday()

    if wd >= 5:
        market_status   = "weekend"
        sp500_display   = "Cerrado"
        sp500_sub       = "Fin de semana"
    elif is_market_open():
        market_status   = "open"
        sp500_display   = f"{sp500:+.2f}%"
        sp500_sub       = "Mercado abierto"
    elif is_premarket_now():
        market_status   = "premarket"
        sp500_display   = "Pre-mkt"
        sp500_sub       = "Pre-apertura"
    else:
        market_status   = "closed"
        sp500_display   = "Cerrado"
        sp500_sub       = "Mercado cerrado"

    # Best/worst performing P/L stats
    avg_win  = 0
    avg_loss = 0
    if wins   > 0:
        w_pls = [((p.get("exit_price",0) - p["entry"]) / p["entry"] * 100)
                 for p in preds if p.get("result") == "win" and p.get("exit_price") and p.get("entry")]
        avg_win = round(sum(w_pls) / len(w_pls), 1) if w_pls else 0
    if losses > 0:
        l_pls = [((p.get("exit_price",0) - p["entry"]) / p["entry"] * 100)
                 for p in preds if p.get("result") == "loss" and p.get("exit_price") and p.get("entry")]
        avg_loss = round(sum(l_pls) / len(l_pls), 1) if l_pls else 0

    rules = learnings.get("rules", [])

    return {
        "pending":        pending,
        "recent":         recent,
        "wins":           wins,
        "losses":         losses,
        "accuracy":       accuracy,
        "total":          len(preds),
        "pending_ct":     len(pending),
        "regime":         regime.get("regime", "?"),
        "regime_str":     regime.get("strength", 0),
        "spy_mom3m":      regime.get("spy_mom3m", 0),
        "fear_greed":     fg,
        "fg_label":       fg_label,
        "fg_color":       fg_color,
        "vix":            vix,
        "sp500":          sp500,
        "sp500_display":  sp500_display,
        "sp500_sub":      sp500_sub,
        "market_status":  market_status,
        "rules_ct":       len(rules),
        "rules":          rules[:10],
        "high_impact":    econ.get("is_high_impact", False),
        "eco_events":     econ.get("high_impact_today", []),
        "by_type":        by_type,
        "by_sector":      by_sector,
        "hour_memory":    learnings.get("hour_memory", {}),
        "regime_memory":  learnings.get("regime_memory", {}),
        "sector_memory":  learnings.get("sector_memory", {}),
        "macro_news":       mctx.get("macro_news", []),
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "geo_context":      mctx.get("geopolitical_context", []),
        "sector_bias":      mctx.get("sector_bias", {}),
        "updated":          now.strftime("%H:%M:%S · %d/%m/%Y"),
        "next_open":        "Lunes 15:30h" if now.weekday() >= 5 else ("15:30h" if now.weekday() < 5 and (now.hour * 60 + now.minute) < 930 else None),
    }


# ─── Login HTML ────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockBot Pro — Acceso</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{
  background:#080c14;min-height:100vh;display:flex;align-items:center;
  justify-content:center;font-family:'Inter',system-ui,sans-serif;overflow:hidden;
}
body::before{
  content:'';position:fixed;inset:0;
  background-image:radial-gradient(circle at 20% 50%,rgba(0,224,122,.06) 0,transparent 50%),
    radial-gradient(circle at 80% 20%,rgba(61,142,248,.06) 0,transparent 40%);
}
body::after{
  content:'';position:fixed;inset:0;
  background-image:linear-gradient(rgba(30,45,68,.3) 1px,transparent 1px),
    linear-gradient(90deg,rgba(30,45,68,.3) 1px,transparent 1px);
  background-size:48px 48px;
}
.wrap{position:relative;z-index:1;width:100%;max-width:400px;padding:24px}
.card{
  background:rgba(13,20,32,.97);
  border:1px solid rgba(30,45,68,.8);
  border-radius:20px;padding:44px 36px;
  box-shadow:0 0 0 1px rgba(0,224,122,.05),0 32px 64px rgba(0,0,0,.6);
}
.logo{text-align:center;margin-bottom:36px}
.logo-ring{
  width:72px;height:72px;border-radius:50%;
  background:linear-gradient(135deg,rgba(0,224,122,.15),rgba(61,142,248,.15));
  border:1.5px solid rgba(0,224,122,.3);
  display:flex;align-items:center;justify-content:center;
  margin:0 auto 16px;font-size:32px;
  box-shadow:0 0 24px rgba(0,224,122,.15);
}
.logo h1{color:#e8edf5;font-size:20px;font-weight:700;letter-spacing:-.3px}
.logo p{color:#576880;font-size:13px;margin-top:4px}
.field{margin-bottom:18px}
.field label{display:block;color:#8596b0;font-size:11px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;margin-bottom:8px}
.fi{position:relative}
.fi input{
  width:100%;background:rgba(255,255,255,.03);
  border:1.5px solid rgba(30,45,68,.9);border-radius:10px;
  color:#e8edf5;font-size:14px;font-family:inherit;
  padding:11px 42px 11px 14px;outline:none;
  transition:border-color .2s,box-shadow .2s;
}
.fi input:focus{border-color:rgba(0,224,122,.4);box-shadow:0 0 0 3px rgba(0,224,122,.08)}
.eye{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;color:#576880;font-size:15px;transition:color .15s}
.eye:hover{color:#00e07a}
.btn{
  width:100%;background:linear-gradient(135deg,#00e07a,#00c46a);
  border:none;border-radius:10px;color:#080c14;
  font-size:14px;font-weight:700;font-family:inherit;
  padding:13px;cursor:pointer;letter-spacing:.2px;
  transition:transform .15s,box-shadow .15s,opacity .15s;
  margin-top:4px;
}
.btn:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(0,224,122,.3)}
.btn:active{transform:translateY(0);opacity:.9}
.err{
  background:rgba(255,59,92,.08);border:1px solid rgba(255,59,92,.25);
  border-radius:8px;color:#ff7a8a;font-size:13px;padding:10px 14px;
  margin-bottom:16px;text-align:center;
}
.foot{text-align:center;margin-top:24px;color:#2e3f57;font-size:11px}
</style>
</head>
<body>
<div class="wrap">
<div class="card">
  <div class="logo">
    <div class="logo-ring">📈</div>
    <h1>StockBot Pro</h1>
    <p>Panel de control · v5.2</p>
  </div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST">
    <div class="field">
      <label>Contraseña</label>
      <div class="fi">
        <input type="password" name="password" id="pwd" placeholder="••••••••" autofocus autocomplete="current-password">
        <button type="button" class="eye" onclick="t()">👁</button>
      </div>
    </div>
    <button type="submit" class="btn">Entrar al Dashboard →</button>
  </form>
  <div class="foot">StockBot Pro © 2026 · Datos en tiempo real</div>
</div>
</div>
<script>function t(){const i=document.getElementById('pwd');i.type=i.type==='password'?'text':'password'}</script>
</body>
</html>"""


# ─── Dashboard SPA ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockBot Pro</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{
  --bg:#080c14; --s1:#0d1420; --s2:#111a2b; --s3:#1a2438;
  --b1:rgba(30,45,68,.7); --b2:rgba(37,51,73,.6);
  --green:#00e07a; --gd:rgba(0,224,122,.1); --g2:rgba(0,224,122,.06);
  --red:#ff3b5c;   --rd:rgba(255,59,92,.1);
  --blue:#3d8ef8;  --bd:rgba(61,142,248,.1);
  --yellow:#f5a623;--yd:rgba(245,166,35,.1);
  --purple:#9b6dff;--pd:rgba(155,109,255,.1);
  --orange:#ff7a35;
  --t1:#e8edf5; --t2:#8596b0; --t3:#4a5a72;
  --r:12px;
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--t1);font-family:'Inter',system-ui,sans-serif;min-height:100vh;font-size:14px}

/* ── NAVBAR ── */
.nav{
  position:sticky;top:0;z-index:200;height:56px;
  background:rgba(8,12,20,.85);backdrop-filter:blur(16px);
  border-bottom:1px solid var(--b1);
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;
}
.nav-brand{display:flex;align-items:center;gap:8px;font-weight:700;font-size:15px;letter-spacing:-.2px}
.nav-brand .dot{
  width:28px;height:28px;border-radius:8px;
  background:linear-gradient(135deg,rgba(0,224,122,.2),rgba(61,142,248,.2));
  border:1px solid rgba(0,224,122,.3);
  display:flex;align-items:center;justify-content:center;font-size:14px;
}
.nav-center{display:flex;align-items:center;gap:2px}
.nav-tab{
  display:flex;align-items:center;gap:6px;
  padding:6px 14px;border-radius:8px;
  font-size:13px;font-weight:500;color:var(--t2);
  cursor:pointer;border:none;background:none;
  transition:all .15s;white-space:nowrap;
}
.nav-tab:hover{color:var(--t1);background:rgba(255,255,255,.05)}
.nav-tab.active{color:var(--green);background:var(--g2);font-weight:600}
.nav-tab .icon{font-size:14px}
.nav-right{display:flex;align-items:center;gap:12px}
.live-pill{
  display:flex;align-items:center;gap:6px;
  font-size:12px;color:var(--t2);
  background:rgba(255,255,255,.04);
  border:1px solid var(--b1);border-radius:20px;
  padding:4px 10px;
}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.btn-logout{
  background:none;border:1px solid var(--b1);border-radius:8px;
  color:var(--t2);font-size:12px;font-weight:500;padding:5px 12px;
  cursor:pointer;transition:all .15s;font-family:inherit;
}
.btn-logout:hover{border-color:var(--red);color:var(--red)}

/* ── LAYOUT ── */
.page{max-width:1440px;margin:0 auto;padding:20px 20px 40px}
.tab-panel{display:none}
.tab-panel.active{display:block}

/* ── SECTION ── */
.section{margin-bottom:28px}
.sh{
  display:flex;align-items:center;gap:10px;
  font-size:11px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
  color:var(--t3);margin-bottom:16px;
}
.sh::after{content:'';flex:1;height:1px;background:var(--b1)}

/* ── CARDS ── */
.card{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);padding:20px}
.card-sm{background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);padding:16px}
.card-title{font-size:11px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:var(--t3);margin-bottom:16px}

/* ── KPI GRID ── */
.kpi-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:28px}
@media(max-width:1100px){.kpi-grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:700px){.kpi-grid{grid-template-columns:repeat(2,1fr)}}
.kpi{
  background:var(--s1);border:1px solid var(--b1);border-radius:var(--r);
  padding:18px 18px 16px;position:relative;
  transition:border-color .2s,transform .15s;cursor:default;
}
.kpi:hover{transform:translateY(-1px)}
.kpi-accent{position:absolute;top:0;left:0;right:0;height:2px;border-radius:var(--r) var(--r) 0 0}
.kpi.green .kpi-accent{background:linear-gradient(90deg,var(--green),transparent)}
.kpi.red   .kpi-accent{background:linear-gradient(90deg,var(--red),transparent)}
.kpi.blue  .kpi-accent{background:linear-gradient(90deg,var(--blue),transparent)}
.kpi.yellow.kpi-accent{background:linear-gradient(90deg,var(--yellow),transparent)}
.kpi.purple .kpi-accent{background:linear-gradient(90deg,var(--purple),transparent)}
.kpi-label{font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:var(--t3);margin-bottom:10px}
.kpi-val{font-size:26px;font-weight:800;line-height:1;margin-bottom:6px;letter-spacing:-.5px}
.kpi-sub{font-size:12px;color:var(--t2)}
.kpi-sub b{color:var(--t1);font-weight:600}
.col-green{color:var(--green)}
.col-red{color:var(--red)}
.col-yellow{color:var(--yellow)}
.col-blue{color:var(--blue)}
.col-purple{color:var(--purple)}

/* ── TOOLTIP ── */
.has-tooltip{position:relative}
.has-tooltip .tooltip{
  position:absolute;top:calc(100% + 10px);left:50%;
  background:var(--s3);border:1px solid var(--b2);
  border-radius:10px;padding:12px 14px;
  font-size:12px;color:var(--t1);line-height:1.6;
  white-space:nowrap;z-index:500;
  opacity:0;pointer-events:none;
  transition:opacity .15s,transform .15s;
  transform:translateX(-50%) translateY(-4px);
  box-shadow:0 8px 24px rgba(0,0,0,.5);
  min-width:200px;
}
.has-tooltip:hover .tooltip{opacity:1;transform:translateX(-50%) translateY(0)}
.tooltip::after{
  content:'';position:absolute;bottom:100%;left:50%;transform:translateX(-50%);
  border:6px solid transparent;border-bottom-color:var(--s3);
}
.tt-row{display:flex;justify-content:space-between;gap:20px;padding:2px 0}
.tt-range{color:var(--t2)}
.tt-label{color:var(--t1);font-weight:600}

/* ── FG BAR ── */
.fg-bar{height:5px;border-radius:3px;background:var(--b1);margin:8px 0 4px;overflow:hidden;position:relative}
.fg-fill{height:100%;border-radius:3px;transition:width .6s cubic-bezier(.4,0,.2,1)}

/* ── GRID LAYOUTS ── */
.g2{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:24px}
.g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;margin-bottom:24px}
.g13{display:grid;grid-template-columns:1fr 3fr;gap:18px;margin-bottom:24px}
@media(max-width:900px){.g2,.g3,.g13{grid-template-columns:1fr}}

/* ── TABLE ── */
.tw{overflow-x:auto;border-radius:var(--r)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{
  color:var(--t3);font-size:10px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
  padding:10px 14px;text-align:left;border-bottom:1px solid var(--b1);white-space:nowrap;
  background:var(--s1);overflow:visible;position:relative;
}
td{padding:11px 14px;border-bottom:1px solid rgba(26,36,64,.4);vertical-align:middle}
tr:last-child td{border-bottom:none}
tbody tr{transition:background .1s}
tbody tr:hover td{background:rgba(255,255,255,.02)}
.tk{font-weight:700;font-size:14px;letter-spacing:.3px;color:var(--t1)}
.mono{font-family:'Courier New',monospace;font-size:12px;color:#a0b4cc}
.mono.g{color:var(--green)}
.mono.r{color:var(--red)}

/* ── BADGES ── */
.badge{
  display:inline-flex;align-items:center;gap:4px;
  font-size:10px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;
  padding:3px 8px;border-radius:5px;white-space:nowrap;
}
.b-buy   {background:var(--gd);color:var(--green);border:1px solid rgba(0,224,122,.25)}
.b-sell  {background:var(--rd);color:var(--red);border:1px solid rgba(255,59,92,.25)}
.b-win   {background:var(--gd);color:var(--green);border:1px solid rgba(0,224,122,.25)}
.b-loss  {background:var(--rd);color:var(--red);border:1px solid rgba(255,59,92,.25)}
.b-norm  {background:rgba(255,255,255,.05);color:var(--t2);border:1px solid var(--b1)}
.b-earn  {background:var(--pd);color:var(--purple);border:1px solid rgba(155,109,255,.25)}
.b-sq    {background:var(--yd);color:var(--yellow);border:1px solid rgba(245,166,35,.25)}
.b-ins   {background:var(--bd);color:var(--blue);border:1px solid rgba(61,142,248,.25)}
.b-bear  {background:var(--rd);color:var(--red);border:1px solid rgba(255,59,92,.25)}
.b-bull  {background:var(--gd);color:var(--green);border:1px solid rgba(0,224,122,.25)}
.b-lat   {background:var(--yd);color:var(--yellow);border:1px solid rgba(245,166,35,.25)}
.b-target{background:rgba(0,224,122,.08);color:var(--green);border:1px solid rgba(0,224,122,.2)}
.b-stop  {background:rgba(255,59,92,.08);color:var(--red);border:1px solid rgba(255,59,92,.2)}
.b-exp   {background:rgba(245,166,35,.08);color:var(--yellow);border:1px solid rgba(245,166,35,.2)}

/* ── PROGRESS ── */
.prog-wrap{min-width:130px}
.prog-bar{height:5px;border-radius:3px;background:var(--b1);overflow:hidden;margin-bottom:4px;position:relative}
.prog-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--blue),var(--green));transition:width .4s}
.prog-fill.danger{background:linear-gradient(90deg,var(--red),var(--orange))}
.prog-meta{display:flex;justify-content:space-between;font-size:10px;color:var(--t3)}

/* ── CONF BAR ── */
.conf-wrap{display:flex;align-items:center;gap:7px}
.conf-bar{width:56px;height:4px;background:var(--b1);border-radius:2px;overflow:hidden}
.conf-fill{height:100%;border-radius:2px}
.conf-num{font-size:12px;font-weight:700}

/* ── STAT BARS (sector/type) ── */
.stat-block{padding:10px 0;border-bottom:1px solid rgba(26,36,64,.4)}
.stat-block:last-child{border-bottom:none}
.stat-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px}
.stat-name{font-size:12px;font-weight:600;color:var(--t1)}
.stat-ops{font-size:11px;color:var(--t3)}
.stat-bar-wrap{display:flex;align-items:center;gap:8px}
.stat-bar{flex:1;height:5px;border-radius:3px;overflow:hidden;display:flex;background:var(--b1)}
.stat-bar-w{background:var(--green);transition:width .4s}
.stat-bar-l{background:var(--red);transition:width .4s}
.stat-pct{font-size:11px;font-weight:600;min-width:32px;text-align:right}

/* ── STAT ROW ── */
.sr{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid rgba(26,36,64,.4)}
.sr:last-child{border-bottom:none}
.sr-label{font-size:12px;color:var(--t2)}
.sr-val{font-size:13px;font-weight:700}

/* ── EMPTY ── */
.empty{text-align:center;padding:48px 20px;color:var(--t3)}
.empty .ei{font-size:36px;display:block;margin-bottom:10px;opacity:.5}
.empty p{font-size:13px}

/* ── ALERT BANNER ── */
.banner{
  display:flex;align-items:center;gap:10px;
  background:rgba(245,166,35,.06);border:1px solid rgba(245,166,35,.2);
  border-radius:10px;padding:12px 16px;font-size:13px;color:var(--yellow);
  margin-bottom:20px;
}

/* ── CHART WRAP ── */
.chart-box{position:relative;height:200px;display:flex;align-items:center;justify-content:center}

/* ── NEWS ── */
.news-item{
  padding:10px 0;border-bottom:1px solid rgba(26,36,64,.4);
  font-size:12px;color:var(--t2);line-height:1.55;
}
.news-item:last-child{border-bottom:none}
.news-item:hover{color:var(--t1)}

/* ── RULE ── */
.rule-item{
  display:flex;align-items:flex-start;gap:10px;
  padding:9px 12px;border-radius:8px;
  background:rgba(255,255,255,.02);border:1px solid var(--b1);
  margin-bottom:8px;font-size:12px;
}
.rule-item:last-child{margin-bottom:0}
.rule-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:2px}
.rule-text{color:var(--t2);line-height:1.5;flex:1}
.rule-wr{font-weight:700;margin-left:4px}

/* ── DAYS pill ── */
.dpill{
  display:inline-flex;align-items:center;gap:4px;
  font-size:11px;font-weight:600;
  padding:2px 8px;border-radius:20px;
  background:rgba(255,255,255,.05);color:var(--t2);
}
.dpill.urgent{background:var(--rd);color:var(--red)}
.dpill.ok{background:var(--gd);color:var(--green)}

/* ── MARKET STATUS ── */
.mkt-badge{
  display:inline-flex;align-items:center;gap:5px;
  font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;
}
.mkt-open   {background:var(--gd);color:var(--green)}
.mkt-pre    {background:var(--yd);color:var(--yellow)}
.mkt-closed {background:rgba(255,255,255,.05);color:var(--t3)}
.mkt-weekend{background:rgba(255,255,255,.04);color:var(--t3)}

/* ── HISTORIAL EXPANDIBLE ── */
.hist-row{cursor:pointer}
.hist-row:hover td{background:rgba(255,255,255,.025)!important}
.hist-row.open td{background:rgba(61,142,248,.04)!important}
.hist-row .exp-icon{font-size:10px;color:var(--t3);display:inline-block;transition:transform .2s;margin-left:4px}
.hist-row.open .exp-icon{transform:rotate(90deg)}
.hist-detail-row td{padding:0!important;border-bottom:1px solid var(--b1)!important}
.hist-detail-inner{
  display:none;padding:14px 20px;
  background:rgba(10,16,28,.6);
  font-size:12px;color:var(--t2);line-height:1.75;
  border-top:1px solid rgba(61,142,248,.15);
}
.hist-detail-inner.open{display:block}
.hist-detail-inner b{color:var(--t1)}
.b-earn-sm{display:inline-flex;align-items:center;gap:3px;font-size:10px;font-weight:700;
  padding:2px 6px;border-radius:4px;background:var(--pd);color:var(--purple);
  border:1px solid rgba(155,109,255,.25);vertical-align:middle;white-space:nowrap}

/* ── FOOTER ── */
.footer{
  text-align:center;color:var(--t3);font-size:11px;
  padding:20px;border-top:1px solid var(--b1);margin-top:8px;
}

/* ── SUMMARY STATS ROW ── */
.sum-row{
  display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px;
}
@media(max-width:700px){.sum-row{grid-template-columns:repeat(2,1fr)}}
.sum-card{
  background:var(--s2);border:1px solid var(--b1);border-radius:10px;
  padding:14px 16px;text-align:center;
}
.sum-val{font-size:22px;font-weight:800;letter-spacing:-.5px}
.sum-label{font-size:10px;font-weight:600;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-top:4px}
</style>
</head>
<body>

<!-- NAVBAR -->
<nav class="nav">
  <div class="nav-brand">
    <div class="dot">📈</div>
    StockBot Pro
    <span style="color:var(--t3);font-weight:400;font-size:12px">v5.2</span>
  </div>

  <div class="nav-center">
    <button class="nav-tab active" data-tab="dashboard" onclick="goTab('dashboard')">
      <span class="icon">⬛</span> Dashboard
    </button>
    <button class="nav-tab" data-tab="signals" onclick="goTab('signals')">
      <span class="icon">📡</span> Señales
      <span id="nav-pending-ct" style="background:var(--blue);color:#fff;font-size:10px;font-weight:700;border-radius:10px;padding:1px 6px;margin-left:2px">{{ pending_ct }}</span>
    </button>
    <button class="nav-tab" data-tab="history" onclick="goTab('history')">
      <span class="icon">📋</span> Historial
    </button>
    <button class="nav-tab" data-tab="analysis" onclick="goTab('analysis')">
      <span class="icon">🧠</span> Análisis
    </button>
    <button class="nav-tab" data-tab="macro" onclick="goTab('macro')">
      <span class="icon">🌍</span> Macro
    </button>
  </div>

  <div class="nav-right">
    <div class="live-pill">
      <span class="live-dot"></span>
      <span id="updated-ts">{{ updated }}</span>
    </div>
    <a href="/logout" class="btn-logout">Salir</a>
  </div>
</nav>

<div class="page">

{% if high_impact %}
<div class="banner">⚠️ <strong>EVENTOS ALTO IMPACTO HOY:</strong>&nbsp;{{ eco_events|join(' · ') }}</div>
{% endif %}

<!-- ════════════════════════════════════════════════════ TAB: DASHBOARD -->
<div class="tab-panel active" id="tab-dashboard">

  <!-- KPI CARDS -->
  <div class="kpi-grid">

    <!-- Régimen -->
    <div class="kpi {% if regime=='BEAR' %}red{% elif regime=='BULL' %}green{% else %}yellow{% endif %} has-tooltip" id="kpi-regime-card">
      <div class="kpi-accent"></div>
      <div class="kpi-label">Régimen de mercado</div>
      <div id="kpi-regime-val" class="kpi-val col-{% if regime=='BEAR' %}red{% elif regime=='BULL' %}green{% else %}yellow{% endif %}">
        {{ regime }}
        <span class="badge b-{% if regime=='BEAR' %}bear{% elif regime=='BULL' %}bull{% else %}lat{% endif %}" style="font-size:11px;vertical-align:middle;margin-left:4px">{{ regime_str }}%</span>
      </div>
      <div id="kpi-regime-sub" class="kpi-sub">SPY <b>{{ "%+.1f"|format(spy_mom3m) }}%</b> en 3 meses</div>
      <div class="tooltip" style="min-width:240px">
        <div style="font-weight:700;margin-bottom:8px;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--t3)">Regímenes de mercado</div>
        <div class="tt-row" style="margin-bottom:6px"><span class="tt-label col-green">🐂 BULL</span></div>
        <div style="font-size:11px;color:var(--t2);margin-bottom:10px;line-height:1.5">SPY por encima de SMA200, momentum positivo. El bot opera con umbrales normales y busca largos.</div>
        <div class="tt-row" style="margin-bottom:6px"><span class="tt-label col-red">🐻 BEAR</span></div>
        <div style="font-size:11px;color:var(--t2);margin-bottom:10px;line-height:1.5">SPY bajo SMA200, tendencia bajista. El bot sube umbrales de confianza y reduce objetivos a +8%.</div>
        <div class="tt-row" style="margin-bottom:6px"><span class="tt-label col-yellow">↔ LATERAL</span></div>
        <div style="font-size:11px;color:var(--t2);margin-bottom:10px;line-height:1.5">Sin tendencia clara. Rango entre soportes y resistencias. Objetivos +12%, más cautela.</div>
        <div style="padding-top:8px;border-top:1px solid var(--b1);font-size:11px;color:var(--t3)">
          El % indica la <b style="color:var(--t1)">fuerza del régimen</b> (0–100).<br>
          Actualmente: <b style="color:{% if regime=='BEAR' %}var(--red){% elif regime=='BULL' %}var(--green){% else %}var(--yellow){% endif %}">{{ regime }} {{ regime_str }}%</b> · SPY {{ "%+.1f"|format(spy_mom3m) }}% en 3m
        </div>
      </div>
    </div>

    <!-- Fear & Greed -->
    <div class="kpi {% if fear_greed < 45 %}red{% elif fear_greed > 55 %}green{% else %}yellow{% endif %} has-tooltip">
      <div class="kpi-accent"></div>
      <div class="kpi-label">Fear &amp; Greed</div>
      <div id="kpi-fg-val" class="kpi-val" style="color:{{ fg_color }}">{{ fear_greed }}<span style="font-size:14px;font-weight:400;opacity:.7">/100</span></div>
      <div class="fg-bar"><div id="kpi-fg-fill" class="fg-fill" style="width:{{ fear_greed }}%;background:{{ fg_color }}"></div></div>
      <div id="kpi-fg-sub" class="kpi-sub">{{ fg_label }}</div>
      <div class="tooltip">
        <div style="font-weight:700;margin-bottom:8px;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--t3)">Escala Fear &amp; Greed</div>
        <div class="tt-row"><span class="tt-range col-red">0 – 24</span><span class="tt-label">😱 Miedo Extremo</span></div>
        <div class="tt-row"><span class="tt-range col-red" style="opacity:.7">25 – 44</span><span class="tt-label">😰 Miedo</span></div>
        <div class="tt-row"><span class="tt-range col-yellow">45 – 54</span><span class="tt-label">😐 Neutral</span></div>
        <div class="tt-row"><span class="tt-range col-green" style="opacity:.7">55 – 74</span><span class="tt-label">😏 Codicia</span></div>
        <div class="tt-row"><span class="tt-range col-green">75 – 100</span><span class="tt-label">🤑 Codicia Extrema</span></div>
        <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--b1);font-size:11px;color:var(--t3)">
          El índice mide el sentimiento del mercado.<br>Extremos = oportunidad de entrada contrarian.
        </div>
      </div>
    </div>

    <!-- VIX -->
    <div class="kpi {% if vix > 30 %}red{% elif vix > 20 %}yellow{% else %}green{% endif %} has-tooltip">
      <div class="kpi-accent"></div>
      <div class="kpi-label">VIX — Volatilidad</div>
      <div id="kpi-vix-val" class="kpi-val col-{% if vix > 30 %}red{% elif vix > 20 %}yellow{% else %}green{% endif %}">{{ vix }}</div>
      <div id="kpi-vix-sub" class="kpi-sub">
        {% if vix > 35 %}🔴 Pánico de mercado
        {% elif vix > 25 %}🟠 Alta volatilidad
        {% elif vix > 18 %}🟡 Volatilidad elevada
        {% else %}🟢 Calma — baja vol
        {% endif %}
      </div>
      <div class="tooltip">
        <div style="font-weight:700;margin-bottom:8px;font-size:11px;letter-spacing:.5px;text-transform:uppercase;color:var(--t3)">Índice de Volatilidad (VIX)</div>
        <div class="tt-row"><span class="tt-range col-green">VIX &lt; 15</span><span class="tt-label">Mercado muy calmado</span></div>
        <div class="tt-row"><span class="tt-range col-green" style="opacity:.7">15 – 20</span><span class="tt-label">Volatilidad normal</span></div>
        <div class="tt-row"><span class="tt-range col-yellow">20 – 30</span><span class="tt-label">Estrés moderado</span></div>
        <div class="tt-row"><span class="tt-range col-red" style="opacity:.7">30 – 40</span><span class="tt-label">Alta incertidumbre</span></div>
        <div class="tt-row"><span class="tt-range col-red">&gt; 40</span><span class="tt-label">Pánico — 2008/2020</span></div>
        <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--b1);font-size:11px;color:var(--t3)">
          VIX alto = mayor riesgo percibido.<br>Bot sube umbrales cuando VIX &gt; 25.
        </div>
      </div>
    </div>

    <!-- S&P 500 -->
    <div class="kpi {% if market_status in ('weekend','closed','premarket') %}{% elif sp500 < 0 %}red{% else %}green{% endif %}">
      <div class="kpi-accent"></div>
      <div class="kpi-label">S&amp;P 500</div>
      <div id="kpi-sp500-val" class="kpi-val
        {% if market_status in ('weekend','closed') %}col-{% else %}{% if sp500 < 0 %}col-red{% else %}col-green{% endif %}{% endif %}"
        style="{% if market_status in ('weekend','closed','premarket') %}color:var(--t3);font-size:20px{% endif %}">
        {{ sp500_display }}
      </div>
      <div id="kpi-sp500-sub" class="kpi-sub">
        {% if market_status == 'open' %}
          <span class="mkt-badge mkt-open">● Abierto</span>
        {% elif market_status == 'premarket' %}
          <span class="mkt-badge mkt-pre">◑ Pre-apertura</span>
        {% elif market_status == 'weekend' %}
          <span class="mkt-badge mkt-weekend">Fin de semana</span>
        {% else %}
          <span class="mkt-badge mkt-closed">Mercado cerrado</span>
        {% endif %}
      </div>
    </div>

    <!-- Precisión -->
    <div class="kpi blue">
      <div class="kpi-accent" style="background:linear-gradient(90deg,var(--blue),transparent)"></div>
      <div class="kpi-label">Precisión global</div>
      <div id="kpi-acc-val" class="kpi-val col-blue">{{ accuracy }}%</div>
      <div id="kpi-acc-sub" class="kpi-sub">
        <span style="color:var(--green)">{{ wins }}✓</span> ·
        <span style="color:var(--red)">{{ losses }}✗</span> ·
        <span style="color:var(--yellow)">{{ pending_ct }} pend.</span>
      </div>
    </div>

  </div>

  <!-- SUMMARY ROW -->
  <div class="sum-row">
    <div class="sum-card">
      <div class="sum-val col-blue">{{ total }}</div>
      <div class="sum-label">Total señales</div>
    </div>
    <div class="sum-card">
      <div class="sum-val col-green">{% if avg_win > 0 %}+{{ avg_win }}%{% else %}—{% endif %}</div>
      <div class="sum-label">Ganancia media (wins)</div>
    </div>
    <div class="sum-card">
      <div class="sum-val col-red">{% if avg_loss != 0 %}{{ avg_loss }}%{% else %}—{% endif %}</div>
      <div class="sum-label">Pérdida media (losses)</div>
    </div>
    <div class="sum-card">
      <div class="sum-val col-purple">{{ rules_ct }}</div>
      <div class="sum-label">Reglas aprendidas</div>
    </div>
  </div>

  <!-- CHART + STATS -->
  <div class="g2">
    <div class="card">
      <div class="card-title">Distribución Win / Loss</div>
      {% if wins + losses > 0 %}
      <div class="chart-box">
        <canvas id="donutChart" width="180" height="180"></canvas>
      </div>
      <div style="display:flex;gap:16px;justify-content:center;margin-top:14px;font-size:13px">
        <span style="color:var(--green);font-weight:600">✓ {{ wins }} wins</span>
        <span style="color:var(--red);font-weight:600">✗ {{ losses }} losses</span>
        <span style="color:var(--blue);font-weight:600">{{ accuracy }}% precisión</span>
      </div>
      {% else %}
      <div class="empty"><span class="ei">📊</span><p>Sin datos resueltos aún</p></div>
      {% endif %}
    </div>

    <div class="card">
      <div class="card-title">Resumen de estadísticas</div>
      <div class="sr"><span class="sr-label">Total predicciones</span><span class="sr-val">{{ total }}</span></div>
      <div class="sr"><span class="sr-label">Activas pendientes</span><span class="sr-val col-yellow">{{ pending_ct }}</span></div>
      <div class="sr"><span class="sr-label">Wins acumulados</span><span class="sr-val col-green">{{ wins }}</span></div>
      <div class="sr"><span class="sr-label">Losses acumulados</span><span class="sr-val col-red">{{ losses }}</span></div>
      <div class="sr"><span class="sr-label">Ganancia media win</span><span class="sr-val col-green">{% if avg_win %}+{{ avg_win }}%{% else %}—{% endif %}</span></div>
      <div class="sr"><span class="sr-label">Pérdida media loss</span><span class="sr-val col-red">{% if avg_loss %}{{ avg_loss }}%{% else %}—{% endif %}</span></div>
      <div class="sr"><span class="sr-label">Reglas aprendidas</span><span class="sr-val col-purple">{{ rules_ct }}</span></div>
      <div class="sr"><span class="sr-label">Actualizado</span><span class="sr-val" style="font-size:11px;color:var(--t3)" id="dash-updated">{{ updated }}</span></div>
    </div>
  </div>

  <!-- SEÑALES ACTIVAS (preview en dashboard) -->
  <div class="section">
    <div class="sh">⏳ Señales activas ({{ pending_ct }})</div>
    {% if pending %}
    <div class="card">
      <div class="tw">
        <table>
          <thead>
            <tr>
              <th>Ticker</th><th>Señal</th><th>Tipo</th><th>Entrada</th>
              <th>Objetivo</th><th>Stop</th><th>Confianza</th><th>Progreso</th><th>Sector</th><th>Días</th>
            </tr>
          </thead>
          <tbody>
            {% for p in pending[:5] %}
            {% set st = p.get('signal_type','NORMAL') %}
            <tr>
              <td><span class="tk">{{ p.ticker }}</span></td>
              <td>
                {% if p.signal == 'COMPRAR' %}
                  <span class="badge b-buy">📈 Comprar</span>
                {% else %}
                  <span class="badge b-sell">📉 Vender</span>
                {% endif %}
              </td>
              <td>
                {% if st == 'PRE_EARNINGS' %}<span class="badge b-earn">Pre-Earn</span>
                {% elif st == 'SHORT_SQUEEZE' %}<span class="badge b-sq">Squeeze</span>
                {% elif st == 'INSIDER_MASSIVE' %}<span class="badge b-ins">Insider</span>
                {% else %}<span style="font-size:12px;color:var(--t3)">Normal</span>{% endif %}
              </td>
              <td class="mono">${{ "%.2f"|format(p.entry|float) }}</td>
              <td class="mono g">${{ "%.2f"|format(p.target|float) }}</td>
              <td class="mono r">${{ "%.2f"|format(p.stop|float) }}</td>
              <td>
                <div class="conf-wrap">
                  <div class="conf-bar">
                    <div class="conf-fill" style="width:{{ p.confidence|int }}%;background:{% if p.confidence|int >= 94 %}var(--green){% elif p.confidence|int >= 88 %}var(--blue){% else %}var(--t3){% endif %}"></div>
                  </div>
                  <span class="conf-num" style="color:{% if p.confidence|int >= 94 %}var(--green){% elif p.confidence|int >= 88 %}var(--blue){% else %}var(--t2){% endif %}">{{ p.confidence|int }}%</span>
                </div>
              </td>
              <td>
                {% if p.target_pct %}
                <div class="prog-wrap">
                  <div class="prog-bar">
                    <div class="prog-fill" style="width:{{ [[(p.days_elapsed / 30 * 100)|int, 0]|max, 100]|min }}%"></div>
                  </div>
                  <div class="prog-meta">
                    <span>+{{ p.target_pct }}% obj</span>
                    <span>{{ p.days_elapsed }}d / 30d</span>
                  </div>
                </div>
                {% else %}—{% endif %}
              </td>
              <td style="font-size:12px;color:var(--t2)">{{ p.sector or '—' }}</td>
              <td>
                <span class="dpill {% if p.days_remaining <= 5 %}urgent{% elif p.days_remaining >= 20 %}ok{% endif %}">
                  {{ p.days_remaining }}d rest.
                </span>
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% if pending|length > 5 %}
      <div style="text-align:center;padding-top:14px">
        <button onclick="goTab('signals')" style="background:none;border:1px solid var(--b2);color:var(--t2);font-size:12px;border-radius:8px;padding:6px 16px;cursor:pointer;font-family:inherit;transition:all .15s" onmouseover="this.style.borderColor='var(--green)';this.style.color='var(--green)'" onmouseout="this.style.borderColor='var(--b2)';this.style.color='var(--t2)'">
          Ver todas las señales ({{ pending|length }}) →
        </button>
      </div>
      {% endif %}
    </div>
    {% else %}
    <div class="card"><div class="empty"><span class="ei">🔍</span><p>No hay predicciones activas ahora mismo</p></div></div>
    {% endif %}
  </div>

</div>
<!-- /dashboard -->


<!-- ════════════════════════════════════════════════════ TAB: SEÑALES -->
<div class="tab-panel" id="tab-signals">

  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
    <div class="sh" style="margin-bottom:0;flex:1">📡 Señales activas — {{ pending_ct }} predicciones en curso</div>
    <span style="font-size:11px;color:var(--t3);background:var(--s2);border:1px solid var(--b1);border-radius:6px;padding:4px 10px">
      💡 Precio actual se actualiza cada 3 min
    </span>
  </div>

  {% if pending %}
  <div class="card" style="margin-bottom:20px;padding:0">
    <div class="tw">
      <table>
        <thead>
          <tr>
            <th>Ticker</th>
            <th>Señal</th>
            <th>Tipo</th>
            <th>Entrada</th>
            <th>Precio actual</th>
            <th>vs Entrada</th>
            <th>Objetivo</th>
            <th>Stop</th>
            <th>Confianza</th>
            <th>Progreso al objetivo</th>
            <th>Earnings</th>
            <th>Sector</th>
            <th>Días rest.</th>
          </tr>
        </thead>
        <tbody>
          {% for p in pending %}
          {% set st = p.get('signal_type','NORMAL') %}
          {% set has_cur = p.current_price is not none %}
          <tr>
            <td>
              <span class="tk">{{ p.ticker }}</span>
              {% if p.session %}<br><span style="font-size:9px;color:var(--t3)">{{ p.session }}</span>{% endif %}
            </td>
            <td>
              {% if p.signal == 'COMPRAR' %}<span class="badge b-buy">📈 Comprar</span>
              {% else %}<span class="badge b-sell">📉 Vender</span>{% endif %}
            </td>
            <td>
              {% if st == 'PRE_EARNINGS' %}<span class="badge b-earn">Pre-Earn</span>
              {% elif st == 'SHORT_SQUEEZE' %}<span class="badge b-sq">Squeeze</span>
              {% elif st == 'INSIDER_MASSIVE' %}<span class="badge b-ins">Insider</span>
              {% else %}<span style="font-size:11px;color:var(--t3)">Normal</span>{% endif %}
            </td>
            <td>
              <span class="mono">${{ "%.2f"|format(p.entry|float) }}</span>
            </td>
            <td>
              {% if has_cur %}
                <span class="mono" style="font-weight:700;color:var(--t1)">${{ "%.2f"|format(p.current_price) }}</span>
                {% if p.current_chg is not none %}
                <br><span style="font-size:10px;color:{% if p.current_chg >= 0 %}var(--green){% else %}var(--red){% endif %}">
                  {{ "%+.2f"|format(p.current_chg) }}% hoy
                </span>
                {% endif %}
              {% else %}
                <span style="color:var(--t3);font-size:12px">— fuera mkt</span>
              {% endif %}
            </td>
            <td>
              {% if p.vs_entry_pct is not none %}
                <span style="font-weight:700;font-size:13px;color:{% if p.vs_entry_pct >= 0 %}var(--green){% else %}var(--red){% endif %}">
                  {{ "%+.2f"|format(p.vs_entry_pct) }}%
                </span>
              {% else %}<span style="color:var(--t3)">—</span>{% endif %}
            </td>
            <td>
              <span class="mono g">${{ "%.2f"|format(p.target|float) }}</span>
              {% if p.target_pct %}<br><span style="font-size:10px;color:var(--green);opacity:.8">+{{ p.target_pct }}%</span>{% endif %}
            </td>
            <td>
              <span class="mono r">${{ "%.2f"|format(p.stop|float) }}</span>
              {% if p.entry and p.stop %}<br><span style="font-size:10px;color:var(--red);opacity:.7">{{ "%.1f"|format((p.stop|float - p.entry|float) / p.entry|float * 100) }}%</span>{% endif %}
            </td>
            <td>
              <div class="conf-wrap">
                <div class="conf-bar">
                  <div class="conf-fill" style="width:{{ p.confidence|int }}%;background:{% if p.confidence|int >= 94 %}var(--green){% elif p.confidence|int >= 88 %}var(--blue){% else %}var(--t3){% endif %}"></div>
                </div>
                <span class="conf-num" style="color:{% if p.confidence|int >= 94 %}var(--green){% elif p.confidence|int >= 88 %}var(--blue){% else %}var(--t2){% endif %}">{{ p.confidence|int }}%</span>
              </div>
            </td>
            <td>
              <div class="prog-wrap" style="min-width:150px">
                {% if p.real_progress is not none %}
                  <!-- Barra basada en precio real -->
                  <div class="prog-bar">
                    <div class="prog-fill" style="width:{{ p.real_progress }}%;background:{% if p.real_progress >= 75 %}var(--green){% elif p.real_progress >= 40 %}var(--blue){% else %}var(--t3){% endif %}"></div>
                  </div>
                  <div class="prog-meta">
                    <span style="color:var(--t2)">{{ p.real_progress }}% al objetivo</span>
                    <span style="color:var(--t3)">{{ p.days_remaining }}d rest.</span>
                  </div>
                {% elif p.target_pct %}
                  <!-- Fallback: barra temporal -->
                  <div class="prog-bar">
                    <div class="prog-fill {% if p.days_remaining <= 7 %}danger{% endif %}" style="width:{{ [[(p.days_elapsed / 30 * 100)|int, 0]|max, 100]|min }}%"></div>
                  </div>
                  <div class="prog-meta">
                    <span style="color:var(--t3)">+{{ p.target_pct }}% obj · {{ p.days_elapsed }}d</span>
                    <span style="color:{% if p.days_remaining <= 7 %}var(--red){% else %}var(--t3){% endif %}">{{ p.days_remaining }}d rest.</span>
                  </div>
                {% else %}—{% endif %}
              </div>
            </td>
            <td>
              {% if p.earnings_during %}
                <span class="b-earn-sm">📅 {{ p.earnings_date }}</span>
              {% elif p.earnings_date %}
                <span style="font-size:10px;color:var(--t3)">{{ p.earnings_date }}</span>
              {% else %}
                <span style="color:var(--t3)">—</span>
              {% endif %}
            </td>
            <td style="font-size:12px">
              {% if p.sector and p.sector != 'Unknown' %}<span style="color:var(--t1)">{{ p.sector }}</span>
              {% else %}<span style="color:var(--t3)">—</span>{% endif %}
            </td>
            <td>
              <span class="dpill {% if p.days_remaining <= 5 %}urgent{% elif p.days_remaining >= 20 %}ok{% endif %}">
                {{ p.days_remaining }}d
              </span>
              <br><span style="font-size:10px;color:var(--t3)">{{ p.date[:10] if p.date else '' }}</span>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% else %}
  <div class="card"><div class="empty"><span class="ei">🔍</span><p>No hay señales activas en este momento</p></div></div>
  {% endif %}

</div>
<!-- /signals -->


<!-- ════════════════════════════════════════════════════ TAB: HISTORIAL -->
<div class="tab-panel" id="tab-history">

  <div class="sh">📋 Historial de operaciones — {{ wins + losses }} resueltas</div>

  <!-- Summary -->
  <div class="sum-row" style="margin-bottom:20px">
    <div class="sum-card"><div class="sum-val col-green">{{ wins }}</div><div class="sum-label">Wins</div></div>
    <div class="sum-card"><div class="sum-val col-red">{{ losses }}</div><div class="sum-label">Losses</div></div>
    <div class="sum-card"><div class="sum-val col-blue">{{ accuracy }}%</div><div class="sum-label">Tasa de acierto</div></div>
    <div class="sum-card"><div class="sum-val col-green">{% if avg_win %}+{{ avg_win }}%{% else %}—{% endif %}</div><div class="sum-label">P/L medio win</div></div>
  </div>

  {% if recent %}
  <div style="font-size:11px;color:var(--t3);margin-bottom:10px">Haz clic en cualquier fila para ver el análisis completo de la operación.</div>
  <div class="card">
    <div class="tw">
      <table>
        <thead>
          <tr>
            <th></th>
            <th>Ticker</th><th>Señal</th><th>Resultado</th><th>Motivo</th>
            <th class="has-tooltip" style="cursor:help">
              Entrada → Salida
              <span style="color:var(--t3);font-size:9px;margin-left:3px">ℹ</span>
              <div class="tooltip" style="min-width:280px;text-align:left">
                <div style="font-weight:700;margin-bottom:6px;font-size:11px;letter-spacing:.4px;text-transform:uppercase;color:var(--t3)">¿Qué significan estos precios?</div>
                <div class="tt-row" style="margin-bottom:4px"><span class="badge b-buy" style="font-size:10px">📈 COMPRAR</span></div>
                <div style="font-size:11px;color:var(--t2);margin-bottom:10px;line-height:1.5">Entrada = precio al que compras. Salida = precio al que vendiste (objetivo o stop). Si el precio sube, ganas.</div>
                <div class="tt-row" style="margin-bottom:4px"><span class="badge b-sell" style="font-size:10px">📉 VENDER</span></div>
                <div style="font-size:11px;color:var(--t2);line-height:1.5">Entrada = precio al que vendes en corto (short). Salida = precio al que recompras. Si el precio baja, ganas. Si sube, pierdes.</div>
              </div>
            </th>
            <th>P/L</th><th>Tipo</th><th>Sector</th><th>Días</th><th>Fecha</th>
          </tr>
        </thead>
        <tbody>
          {% for p in recent %}
          {% set er = p.exit_reason_label %}
          {% set st = p.get('signal_type','NORMAL') %}
          <tr class="hist-row" onclick="toggleHist(this)">
            <td style="width:20px;padding-right:6px"><span class="exp-icon">▶</span></td>
            <td><span class="tk">{{ p.ticker }}</span></td>
            <td>
              {% if p.signal == 'COMPRAR' %}<span class="badge b-buy">📈 Comprar</span>
              {% else %}<span class="badge b-sell">📉 Vender</span>{% endif %}
            </td>
            <td>
              {% if p.result == 'win' %}<span class="badge b-win">✓ WIN</span>
              {% else %}<span class="badge b-loss">✗ LOSS</span>{% endif %}
            </td>
            <td>
              {% if er == 'TARGET' %}<span class="badge b-target">🎯 Target</span>
              {% elif er == 'STOP' %}<span class="badge b-stop">🛑 Stop</span>
              {% else %}<span class="badge b-exp">⏱ Expirada</span>{% endif %}
            </td>
            <td class="mono">
              ${{ "%.2f"|format(p.entry|float) }}
              {% if p.exit_price %}
                <span style="color:var(--t3)">→</span>
                <span style="color:{% if p.result=='win' %}var(--green){% else %}var(--red){% endif %}">
                  ${{ "%.2f"|format(p.exit_price|float) }}
                </span>
              {% endif %}
            </td>
            <td>
              {% if p.pl_pct is not none %}
                <span style="font-weight:700;font-size:13px;color:{% if p.pl_pct >= 0 %}var(--green){% else %}var(--red){% endif %}">
                  {{ "%+.1f"|format(p.pl_pct) }}%
                </span>
              {% else %}—{% endif %}
            </td>
            <td>
              {% if st == 'PRE_EARNINGS' %}<span class="badge b-earn">Pre-Earn</span>
              {% elif st == 'SHORT_SQUEEZE' %}<span class="badge b-sq">Squeeze</span>
              {% elif st == 'INSIDER_MASSIVE' %}<span class="badge b-ins">Insider</span>
              {% else %}<span style="font-size:11px;color:var(--t3)">Normal</span>{% endif %}
            </td>
            <td style="font-size:12px;color:var(--t2)">{{ p.sector or '—' }}</td>
            <td style="font-size:12px;color:var(--t3)">{{ p.days_to_result or '—' }}d</td>
            <td style="font-size:11px;color:var(--t3)">{% if p.date %}{{ p.date[:10] }}{% endif %}</td>
          </tr>
          <tr class="hist-detail-row">
            <td colspan="11">
              <div class="hist-detail-inner">
                {% if p.signal == 'COMPRAR' %}
                  <b>Señal COMPRAR (posición larga).</b>
                  El bot predijo que <b>{{ p.ticker }}</b> subiría desde <b>${{ "%.2f"|format(p.entry|float) }}</b>.
                  {% if p.target %}Objetivo: <b style="color:var(--green)">${{ "%.2f"|format(p.target|float) }}</b>{% if p.target_pct %} (+{{ p.target_pct }}%){% endif %}.{% endif %}
                  {% if p.stop %}Stop loss: <b style="color:var(--red)">${{ "%.2f"|format(p.stop|float) }}</b> (protección si baja).{% endif %}
                  <br>
                  {% if er == 'TARGET' %}
                    ✅ <b style="color:var(--green)">El precio alcanzó el objetivo.</b> Se cerró la posición con beneficio en ${{ "%.2f"|format(p.exit_price|float) if p.exit_price else '—' }}.
                  {% elif er == 'STOP' %}
                    🛑 <b style="color:var(--red)">El precio bajó hasta el stop loss (${{ "%.2f"|format(p.exit_price|float) if p.exit_price else '—' }}) en lugar de subir.</b>
                    La posición se cerró automáticamente para limitar pérdidas. P/L: <b style="color:var(--red)">{{ "%+.1f"|format(p.pl_pct) if p.pl_pct else '—' }}%</b>.
                  {% else %}
                    ⏱ <b style="color:var(--yellow)">La señal expiró</b> a los {{ p.days_to_result or 30 }} días sin alcanzar objetivo ni stop. Se cerró al precio de mercado.
                  {% endif %}
                {% else %}
                  <b>Señal VENDER (posición corta / short).</b>
                  El bot predijo que <b>{{ p.ticker }}</b> bajaría desde <b>${{ "%.2f"|format(p.entry|float) }}</b>.
                  En un short, vendes primero y recompras después: <b>ganas si el precio baja, pierdes si sube.</b>
                  {% if p.stop %}Stop loss: <b style="color:var(--red)">${{ "%.2f"|format(p.stop|float) }}</b> (se activa si el precio sube hasta aquí).{% endif %}
                  <br>
                  {% if er == 'TARGET' %}
                    ✅ <b style="color:var(--green)">El precio bajó al objetivo.</b> Se recompró con beneficio en ${{ "%.2f"|format(p.exit_price|float) if p.exit_price else '—' }}.
                  {% elif er == 'STOP' %}
                    🛑 <b style="color:var(--red)">El precio subió hasta ${{ "%.2f"|format(p.exit_price|float) if p.exit_price else '—' }} (stop loss) en lugar de bajar.</b>
                    Al subir el precio se recompró más caro de lo que se vendió → pérdida de <b style="color:var(--red)">{{ "%+.1f"|format(p.pl_pct) if p.pl_pct else '—' }}%</b>.
                  {% else %}
                    ⏱ <b style="color:var(--yellow)">La señal expiró</b> a los {{ p.days_to_result or 30 }} días sin alcanzar objetivo ni stop.
                  {% endif %}
                {% endif %}
                <span style="color:var(--t3);margin-left:6px">
                  · Confianza inicial: <b>{{ p.confidence|int if p.confidence else '—' }}%</b>
                  · Emitida: <b>{{ p.date[:10] if p.date else '—' }}</b>
                  · Régimen: <b>{{ p.get('regime','?') }}</b>
                </span>
              </div>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% else %}
  <div class="card"><div class="empty"><span class="ei">📋</span><p>Sin operaciones resueltas aún</p></div></div>
  {% endif %}

</div>
<!-- /history -->


<!-- ════════════════════════════════════════════════════ TAB: ANÁLISIS -->
<div class="tab-panel" id="tab-analysis">

  <!-- ── KPIs de rendimiento ── -->
  <div class="sum-row" style="margin-bottom:24px">
    <div class="sum-card" style="border-color:rgba(0,224,122,.2)">
      <div class="sum-val col-green">{{ accuracy }}%</div>
      <div class="sum-label">Tasa de acierto</div>
    </div>
    <div class="sum-card" style="border-color:rgba(61,142,248,.2)">
      <div class="sum-val col-blue">{{ wins + losses }}</div>
      <div class="sum-label">Operaciones resueltas</div>
    </div>
    <div class="sum-card" style="border-color:rgba(0,224,122,.2)">
      <div class="sum-val col-green">{% if avg_win %}+{{ avg_win }}%{% else %}—{% endif %}</div>
      <div class="sum-label">P/L medio en wins</div>
    </div>
    <div class="sum-card" style="border-color:rgba(255,59,92,.2)">
      <div class="sum-val col-red">{% if avg_loss %}{{ avg_loss }}%{% else %}—{% endif %}</div>
      <div class="sum-label">P/L medio en losses</div>
    </div>
  </div>

  <!-- ── Fila principal: tipo + sector + régimen ── -->
  <div class="g3" style="margin-bottom:24px">

    <!-- Tipo de señal — con chart de barras -->
    <div class="card">
      <div class="card-title">⚡ Precisión por tipo de señal</div>
      {% if by_type %}
        {% for t, s in by_type.items() %}
        {% set total_t = s.w + s.l %}
        {% set pct_t = (s.w / total_t * 100)|int if total_t > 0 else 0 %}
        <div style="margin-bottom:14px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            {% if t == 'PRE_EARNINGS' %}<span class="badge b-earn">📅 Pre-Earnings</span>
            {% elif t == 'SHORT_SQUEEZE' %}<span class="badge b-sq">🔥 Squeeze</span>
            {% elif t == 'INSIDER_MASSIVE' %}<span class="badge b-ins">🕵️ Insider</span>
            {% else %}<span class="badge b-norm">📊 Normal</span>{% endif %}
            <div style="text-align:right">
              <span style="font-size:18px;font-weight:800;color:{% if pct_t >= 60 %}var(--green){% elif pct_t < 45 %}var(--red){% else %}var(--yellow){% endif %}">{{ pct_t }}%</span>
              <span style="font-size:10px;color:var(--t3);display:block">{{ s.w }}W / {{ s.l }}L</span>
            </div>
          </div>
          <div style="height:8px;border-radius:4px;background:var(--b1);overflow:hidden;display:flex">
            <div style="width:{{ (s.w / total_t * 100)|int if total_t > 0 else 0 }}%;background:var(--green);transition:width .5s"></div>
            <div style="width:{{ (s.l / total_t * 100)|int if total_t > 0 else 0 }}%;background:var(--red)"></div>
          </div>
        </div>
        {% endfor %}
      {% else %}
        <div class="empty" style="padding:32px"><span class="ei">📊</span><p>Sin datos resueltos aún</p></div>
      {% endif %}
    </div>

    <!-- Sector — top 8 con heat visual -->
    <div class="card">
      <div class="card-title">🏭 Rendimiento por sector</div>
      {% if by_sector %}
        {% for s, v in by_sector.items()|sort(attribute='1.w', reverse=True) %}
        {% if loop.index <= 8 and s != 'Unknown' %}
        {% set total_s = v.w + v.l %}
        {% set pct_s = (v.w / total_s * 100)|int if total_s > 0 else 0 %}
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
          <div style="flex:1;min-width:0">
            <div style="display:flex;justify-content:space-between;margin-bottom:3px">
              <span style="font-size:12px;color:var(--t1);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ s }}</span>
              <span style="font-size:11px;color:var(--t3);flex-shrink:0;margin-left:8px">{{ total_s }} ops</span>
            </div>
            <div style="height:6px;border-radius:3px;background:var(--b1);overflow:hidden;display:flex">
              <div style="width:{{ pct_s }}%;background:{% if pct_s >= 65 %}var(--green){% elif pct_s >= 50 %}#3d8ef8{% elif pct_s >= 40 %}var(--yellow){% else %}var(--red){% endif %};transition:width .5s"></div>
            </div>
          </div>
          <span style="font-size:13px;font-weight:700;min-width:36px;text-align:right;color:{% if pct_s >= 65 %}var(--green){% elif pct_s >= 50 %}var(--blue){% elif pct_s >= 40 %}var(--yellow){% else %}var(--red){% endif %}">{{ pct_s }}%</span>
        </div>
        {% endif %}
        {% endfor %}
      {% else %}
        <div class="empty" style="padding:32px"><span class="ei">🏭</span><p>Sin datos de sector</p></div>
      {% endif %}
    </div>

    <!-- Régimen de mercado — rendimiento histórico -->
    <div class="card">
      <div class="card-title">🌡️ Rendimiento por régimen</div>
      {% if regime_memory %}
        {% for reg, data in regime_memory.items() %}
        {% set wr = data.win_rate|float %}
        <div style="margin-bottom:20px;padding:16px;border-radius:10px;background:var(--s2);border:1px solid var(--b1)">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
            <span class="badge b-{% if reg=='BEAR' %}bear{% elif reg=='BULL' %}bull{% else %}lat{% endif %}" style="font-size:12px">
              {% if reg=='BULL' %}🐂{% elif reg=='BEAR' %}🐻{% else %}↔{% endif %} {{ reg }}
            </span>
            <span style="font-size:11px;color:var(--t3)">{{ data.total }} ops</span>
          </div>
          <div style="font-size:36px;font-weight:800;color:{% if wr >= 60 %}var(--green){% elif wr < 45 %}var(--red){% else %}var(--yellow){% endif %}">{{ wr }}%</div>
          <div style="font-size:11px;color:var(--t3);margin:4px 0 8px">tasa de acierto histórica</div>
          <div style="height:5px;border-radius:3px;background:var(--b1);overflow:hidden">
            <div style="width:{{ wr|int }}%;height:100%;border-radius:3px;background:{% if wr >= 60 %}var(--green){% elif wr < 45 %}var(--red){% else %}var(--yellow){% endif %}"></div>
          </div>
        </div>
        {% endfor %}
      {% else %}
        <div class="empty" style="padding:24px"><span class="ei">📈</span><p>Necesita más operaciones</p></div>
      {% endif %}
    </div>

  </div>

  <!-- ── Reglas aprendidas ── -->
  <div class="sh">🧠 Motor de aprendizaje autónomo — {{ rules_ct }} reglas activas</div>
  {% if rules %}
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:12px;margin-bottom:24px">
    {% for r in rules %}
    {% set wr = r.win_rate|float %}
    {% set is_positive = wr >= 55 %}
    <div style="display:flex;gap:12px;padding:14px 16px;border-radius:10px;background:var(--s1);border:1px solid {% if wr >= 70 %}rgba(0,224,122,.2){% elif wr >= 55 %}rgba(61,142,248,.2){% elif wr <= 35 %}rgba(255,59,92,.2){% else %}var(--b1){% endif %}">
      <div style="flex-shrink:0;margin-top:2px">
        <div style="width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;background:{% if wr >= 70 %}rgba(0,224,122,.1){% elif wr >= 55 %}rgba(61,142,248,.1){% elif wr <= 35 %}rgba(255,59,92,.1){% else %}rgba(255,255,255,.05){% endif %}">
          {% if wr >= 70 %}✅{% elif wr >= 55 %}📈{% elif wr <= 35 %}⚠️{% else %}📊{% endif %}
        </div>
      </div>
      <div style="flex:1;min-width:0">
        <div style="font-size:12px;color:var(--t2);line-height:1.5;margin-bottom:6px">{{ r.description }}</div>
        <div style="display:flex;align-items:center;gap:8px">
          <span style="font-size:16px;font-weight:800;color:{% if wr >= 70 %}var(--green){% elif wr >= 55 %}var(--blue){% elif wr <= 35 %}var(--red){% else %}var(--yellow){% endif %}">{{ wr }}%</span>
          <div style="flex:1;height:4px;border-radius:2px;background:var(--b1);overflow:hidden">
            <div style="width:{{ wr|int }}%;height:100%;border-radius:2px;background:{% if wr >= 70 %}var(--green){% elif wr >= 55 %}var(--blue){% elif wr <= 35 %}var(--red){% else %}var(--yellow){% endif %}"></div>
          </div>
          <span style="font-size:10px;color:var(--t3)">{{ r.sample_size }} casos</span>
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="card" style="margin-bottom:24px">
    <div class="empty"><span class="ei">🧠</span><p>El bot necesita más operaciones resueltas para generar reglas de aprendizaje</p></div>
  </div>
  {% endif %}

  <!-- ── Gráfica donut + historial resumido ── -->
  <div class="g2">
    <div class="card">
      <div class="card-title">📊 Distribución de resultados</div>
      {% if wins + losses > 0 %}
      <div class="chart-box" style="height:180px"><canvas id="donutAnalysis"></canvas></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px">
        <div style="background:rgba(0,224,122,.06);border:1px solid rgba(0,224,122,.15);border-radius:8px;padding:12px;text-align:center">
          <div style="font-size:24px;font-weight:800;color:var(--green)">{{ wins }}</div>
          <div style="font-size:11px;color:var(--t3);margin-top:2px">WINS ✓</div>
        </div>
        <div style="background:rgba(255,59,92,.06);border:1px solid rgba(255,59,92,.15);border-radius:8px;padding:12px;text-align:center">
          <div style="font-size:24px;font-weight:800;color:var(--red)">{{ losses }}</div>
          <div style="font-size:11px;color:var(--t3);margin-top:2px">LOSSES ✗</div>
        </div>
      </div>
      {% else %}
      <div class="empty"><span class="ei">📊</span><p>Sin datos resueltos</p></div>
      {% endif %}
    </div>
    <div class="card">
      <div class="card-title">📅 Últimas 5 operaciones resueltas</div>
      {% if recent %}
      {% for p in recent[:5] %}
      <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid rgba(26,36,64,.4)">
        <span style="font-weight:700;font-size:14px;min-width:52px">{{ p.ticker }}</span>
        {% if p.result == 'win' %}<span class="badge b-win">✓ WIN</span>{% else %}<span class="badge b-loss">✗ LOSS</span>{% endif %}
        <div style="flex:1">
          <div style="font-size:11px;color:var(--t3)">${{ "%.2f"|format(p.entry|float) }} → {% if p.exit_price %}${{ "%.2f"|format(p.exit_price|float) }}{% else %}—{% endif %}</div>
          <div style="font-size:10px;color:var(--t3)">{{ p.exit_reason_label }} · {{ p.date[:10] if p.date else '' }}</div>
        </div>
        {% if p.pl_pct is not none %}
        <span style="font-weight:700;color:{% if p.pl_pct >= 0 %}var(--green){% else %}var(--red){% endif %}">{{ "%+.1f"|format(p.pl_pct) }}%</span>
        {% endif %}
      </div>
      {% endfor %}
      {% else %}
      <div class="empty"><span class="ei">📋</span><p>Sin historial aún</p></div>
      {% endif %}
    </div>
  </div>

</div>
<!-- /analysis -->


<!-- ════════════════════════════════════════════════════ TAB: MACRO -->
<div class="tab-panel" id="tab-macro">

  <div class="sh">🌍 Contexto macro — mercados, eventos y noticias</div>

  <!-- ── Fila indicadores ── -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px">

    <!-- Régimen -->
    <div style="background:var(--s1);border:1px solid var(--b1);border-radius:12px;padding:18px;text-align:center;border-top:2px solid {% if regime=='BULL' %}var(--green){% elif regime=='BEAR' %}var(--red){% else %}var(--yellow){% endif %}">
      <div style="font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:8px">Régimen</div>
      <div style="font-size:32px;font-weight:800;color:{% if regime=='BULL' %}var(--green){% elif regime=='BEAR' %}var(--red){% else %}var(--yellow){% endif %}">{{ regime }}</div>
      <div style="font-size:12px;color:var(--t3);margin-top:4px">Fuerza {{ regime_str }}%</div>
      <div style="margin-top:10px">
        <span class="mkt-badge {% if market_status=='open' %}mkt-open{% elif market_status=='premarket' %}mkt-pre{% else %}mkt-closed{% endif %}">
          {% if market_status=='open' %}● Abierto{% elif market_status=='premarket' %}◑ Pre-mkt{% elif market_status=='weekend' %}Fin semana{% else %}Cerrado{% endif %}
        </span>
      </div>
      {% if next_open %}<div style="font-size:11px;color:var(--t3);margin-top:6px">Abre: {{ next_open }}</div>{% endif %}
    </div>

    <!-- F&G gauge visual -->
    <div style="background:var(--s1);border:1px solid var(--b1);border-radius:12px;padding:18px;text-align:center;border-top:2px solid {{ fg_color }}">
      <div style="font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:8px">Fear &amp; Greed</div>
      <div style="font-size:36px;font-weight:800;color:{{ fg_color }}">{{ fear_greed }}</div>
      <div style="font-size:12px;color:var(--t2);margin:4px 0 8px">{{ fg_label }}</div>
      <div style="height:6px;border-radius:3px;background:linear-gradient(90deg,#ff3b5c,#ff6b35,#f5a623,#7bed9f,#00e07a);margin-bottom:4px;position:relative">
        <div style="position:absolute;top:-4px;left:{{ fear_greed }}%;transform:translateX(-50%);width:14px;height:14px;border-radius:50%;background:white;border:2px solid {{ fg_color }};box-shadow:0 0 8px {{ fg_color }}"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--t3)"><span>Miedo ext.</span><span>Neutral</span><span>Codicia ext.</span></div>
    </div>

    <!-- VIX -->
    <div style="background:var(--s1);border:1px solid var(--b1);border-radius:12px;padding:18px;text-align:center;border-top:2px solid {% if vix > 30 %}var(--red){% elif vix > 20 %}var(--yellow){% else %}var(--green){% endif %}">
      <div style="font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:8px">VIX — Volatilidad</div>
      <div style="font-size:36px;font-weight:800;color:{% if vix > 30 %}var(--red){% elif vix > 20 %}var(--yellow){% else %}var(--green){% endif %}">{{ vix }}</div>
      <div style="font-size:12px;color:var(--t2);margin-top:4px">
        {% if vix > 35 %}🔴 Pánico extremo{% elif vix > 25 %}🟠 Alta volatilidad{% elif vix > 18 %}🟡 Estrés moderado{% else %}🟢 Mercado calmado{% endif %}
      </div>
      <div style="margin-top:10px;padding:6px 10px;border-radius:6px;background:{% if vix > 30 %}rgba(255,59,92,.08){% elif vix > 20 %}rgba(245,166,35,.08){% else %}rgba(0,224,122,.08){% endif %};font-size:11px;color:var(--t3)">
        {% if vix > 30 %}Bot en modo ultra-conservador{% elif vix > 20 %}Umbrales elevados{% else %}Operación normal{% endif %}
      </div>
    </div>

    <!-- S&P 500 -->
    <div style="background:var(--s1);border:1px solid var(--b1);border-radius:12px;padding:18px;text-align:center;border-top:2px solid {% if market_status in ('weekend','closed','premarket') %}var(--b1){% elif sp500 < 0 %}var(--red){% else %}var(--green){% endif %}">
      <div style="font-size:11px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:8px">S&amp;P 500 (SPY)</div>
      <div style="font-size:32px;font-weight:800;color:{% if market_status in ('weekend','closed','premarket') %}var(--t3){% elif sp500 < 0 %}var(--red){% else %}var(--green){% endif %}">{{ sp500_display }}</div>
      <div style="font-size:12px;color:var(--t2);margin-top:4px">{{ sp500_sub }}</div>
      <div style="margin-top:10px;font-size:12px;color:var(--t3)">
        SPY momentum 3m: <b style="color:{% if spy_mom3m >= 0 %}var(--green){% else %}var(--red){% endif %}">{{ "%+.1f"|format(spy_mom3m) }}%</b>
      </div>
    </div>

  </div>

  <!-- ── Fila eventos + bias sectorial + noticias ── -->
  <div class="g3">

    <!-- Eventos económicos (calendario visual) -->
    <div class="card">
      <div class="card-title">📅 Calendario económico</div>

      {% if high_impact and eco_events %}
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;padding:10px 12px;border-radius:8px;background:rgba(255,59,92,.06);border:1px solid rgba(255,59,92,.2)">
          <span style="font-size:16px">⚠️</span>
          <div>
            <div style="font-size:11px;font-weight:700;color:var(--red);letter-spacing:.4px">ALTO IMPACTO HOY</div>
            <div style="font-size:11px;color:var(--t3);margin-top:2px">El bot opera con umbrales más altos</div>
          </div>
        </div>
        {% for ev in eco_events %}
        <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;background:var(--s2);border:1px solid var(--b1);margin-bottom:8px">
          <span style="font-size:18px">📌</span>
          <span style="font-size:13px;color:var(--t1)">{{ ev }}</span>
        </div>
        {% endfor %}
      {% else %}
        <div style="padding:16px 12px;border-radius:8px;background:rgba(0,224,122,.04);border:1px solid rgba(0,224,122,.1);display:flex;align-items:center;gap:10px;margin-bottom:16px">
          <span style="font-size:18px">✅</span>
          <div style="font-size:12px;color:var(--t2)">Sin eventos de alto impacto hoy.<br>El bot opera en modo normal.</div>
        </div>
      {% endif %}

      <!-- Efectos en el bot -->
      <div style="border-top:1px solid var(--b1);padding-top:14px;margin-top:8px">
        <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:10px">Cómo afecta al bot</div>
        <div class="sr"><span class="sr-label">Umbral confianza normal</span><span class="sr-val" style="font-size:12px">85–87%</span></div>
        <div class="sr"><span class="sr-label">Umbral fuerte</span><span class="sr-val" style="font-size:12px">88–93%</span></div>
        <div class="sr"><span class="sr-label">Umbral excepcional</span><span class="sr-val" style="font-size:12px">94%+</span></div>
        <div class="sr"><span class="sr-label">Pre-apertura (15:00–15:30)</span><span class="sr-val" style="font-size:12px;color:var(--yellow)">Mín. FUERTE</span></div>
        {% if high_impact %}<div class="sr"><span class="sr-label">Alto impacto hoy</span><span class="sr-val" style="font-size:12px;color:var(--red)">+5% umbral extra</span></div>{% endif %}
      </div>
    </div>

    <!-- Bias sectorial geopolítico -->
    <div class="card">
      <div class="card-title">🗺️ Contexto geopolítico y sectorial</div>

      {% if geo_context %}
        <div style="margin-bottom:14px">
          <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:8px">Factores detectados</div>
          {% for g in geo_context %}
          <span style="display:inline-block;background:rgba(61,142,248,.1);border:1px solid rgba(61,142,248,.2);color:var(--blue);border-radius:6px;padding:3px 10px;font-size:12px;font-weight:600;margin:0 4px 6px 0">{{ g }}</span>
          {% endfor %}
        </div>
      {% endif %}

      {% if sector_bias %}
        <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:10px">Bias sectorial detectado</div>
        {% for etf, direction in sector_bias.items() %}
        <div style="display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-radius:8px;background:var(--s2);border:1px solid var(--b1);margin-bottom:6px">
          <span style="font-size:13px;font-weight:600">{{ etf }}</span>
          <span style="font-size:12px;font-weight:700;color:{% if direction=='up' %}var(--green){% else %}var(--red){% endif %}">
            {% if direction=='up' %}▲ Favorecido{% else %}▼ Presión{% endif %}
          </span>
        </div>
        {% endfor %}
      {% else %}
        <div style="padding:14px 0;font-size:12px;color:var(--t3);text-align:center">Sin bias sectorial detectado</div>
      {% endif %}

      <!-- VIX context -->
      <div style="border-top:1px solid var(--b1);padding-top:14px;margin-top:10px">
        <div style="font-size:10px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;color:var(--t3);margin-bottom:10px">Niveles de referencia VIX</div>
        {% for label, low, high, color in [('Calma', 0, 15, '#00e07a'), ('Normal', 15, 20, '#7bed9f'), ('Estrés', 20, 30, '#f5a623'), ('Miedo', 30, 40, '#ff6b35'), ('Pánico', 40, 100, '#ff3b5c')] %}
        <div style="display:flex;align-items:center;gap:8px;padding:4px 0">
          <div style="width:8px;height:8px;border-radius:50%;background:{{ color }};flex-shrink:0"></div>
          <span style="font-size:12px;color:var(--t2);flex:1">{{ label }}</span>
          <span style="font-size:11px;color:var(--t3)">{{ low }}–{{ high if high < 100 else '∞' }}</span>
          {% if vix >= low and vix < high %}<span style="font-size:10px;font-weight:700;color:{{ color }};background:rgba(255,255,255,.05);border-radius:4px;padding:1px 6px">← actual</span>{% endif %}
        </div>
        {% endfor %}
      </div>
    </div>

    <!-- Noticias macro feed -->
    <div class="card">
      <div class="card-title">📰 Feed de noticias macro</div>
      {% if macro_news %}
        {% for n in macro_news[:10] %}
        <div style="padding:10px 0;border-bottom:1px solid rgba(26,36,64,.4);display:flex;align-items:flex-start;gap:8px">
          <span style="color:var(--t3);font-size:14px;flex-shrink:0;margin-top:1px">•</span>
          <span style="font-size:12px;color:var(--t2);line-height:1.55;flex:1">{{ n }}</span>
        </div>
        {% endfor %}
        <div style="padding-top:12px;font-size:11px;color:var(--t3);text-align:center">
          Fuente: RSS macro · Actualizado con el contexto del bot
        </div>
      {% else %}
        <div class="empty" style="padding:40px"><span class="ei">📰</span><p>Sin noticias macro disponibles</p></div>
      {% endif %}
    </div>

  </div>

</div>
<!-- /macro -->


<div class="footer">
  StockBot Pro v5.2 · Datos actualizados automáticamente cada 60s · © 2026
</div>

</div><!-- /page -->

<script>
// ── Tab navigation ──────────────────────────────────────────────────
function goTab(name) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  const panel = document.getElementById('tab-' + name);
  const btn   = document.querySelector('[data-tab="' + name + '"]');
  if (panel) panel.classList.add('active');
  if (btn)   btn.classList.add('active');
  window.location.hash = name;
}

// restore hash on load
(function() {
  const h = window.location.hash.replace('#', '');
  if (h && document.getElementById('tab-' + h)) goTab(h);
})();

// ── Donut chart ─────────────────────────────────────────────────────
{% if wins + losses > 0 %}
const ctx = document.getElementById('donutChart').getContext('2d');
new Chart(ctx, {
  type: 'doughnut',
  data: {
    labels: ['Wins', 'Losses'],
    datasets: [{
      data: [{{ wins }}, {{ losses }}],
      backgroundColor: ['rgba(0,224,122,.75)', 'rgba(255,59,92,.75)'],
      borderColor: ['#00e07a', '#ff3b5c'],
      borderWidth: 2,
      hoverOffset: 8,
    }]
  },
  options: {
    cutout: '74%',
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: c => ` ${c.label}: ${c.parsed} (${Math.round(c.parsed/({{ wins }}+{{ losses }})*100)}%)`
        }
      }
    },
    animation: { animateScale: true }
  }
});
{% endif %}

// ── Donut en Análisis ────────────────────────────────────────────────
{% if wins + losses > 0 %}
const ctx2 = document.getElementById('donutAnalysis');
if (ctx2) {
  new Chart(ctx2.getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: ['Wins', 'Losses'],
      datasets: [{
        data: [{{ wins }}, {{ losses }}],
        backgroundColor: ['rgba(0,224,122,.75)', 'rgba(255,59,92,.75)'],
        borderColor: ['#00e07a', '#ff3b5c'],
        borderWidth: 2,
        hoverOffset: 8,
      }]
    },
    options: {
      cutout: '74%',
      plugins: { legend: { display: false } },
      animation: { animateScale: true }
    }
  });
}
{% endif %}

// ── Silent AJAX refresh ─────────────────────────────────────────────
function _set(id, txt) { const e = document.getElementById(id); if (e) e.textContent = txt; }
function _setH(id, html) { const e = document.getElementById(id); if (e) e.innerHTML = html; }

function updateData() {
  fetch('/api/data')
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d) return;

      // Timestamps & counts
      _set('nav-pending-ct', d.pending_ct);
      _set('updated-ts', d.updated);
      _set('dash-updated', d.updated);

      // ── Régimen ──
      const regC = d.regime === 'BEAR' ? 'var(--red)' : d.regime === 'BULL' ? 'var(--green)' : 'var(--yellow)';
      const regB = d.regime === 'BEAR' ? 'bear' : d.regime === 'BULL' ? 'bull' : 'lat';
      _setH('kpi-regime-val',
        `${d.regime} <span class="badge b-${regB}" style="font-size:11px;vertical-align:middle;margin-left:4px">${d.regime_str}%</span>`);
      const rv = document.getElementById('kpi-regime-val');
      if (rv) rv.style.color = regC;
      _setH('kpi-regime-sub', `SPY <b>${d.spy_mom3m >= 0 ? '+' : ''}${d.spy_mom3m.toFixed(1)}%</b> en 3 meses`);

      // ── Fear & Greed ──
      const fv = document.getElementById('kpi-fg-val');
      if (fv) { fv.innerHTML = `${d.fear_greed}<span style="font-size:14px;font-weight:400;opacity:.7">/100</span>`; fv.style.color = d.fg_color; }
      const ff = document.getElementById('kpi-fg-fill');
      if (ff) { ff.style.width = d.fear_greed + '%'; ff.style.background = d.fg_color; }
      _set('kpi-fg-sub', d.fg_label);

      // ── VIX ──
      const vixC = d.vix > 30 ? 'var(--red)' : d.vix > 20 ? 'var(--yellow)' : 'var(--green)';
      const vixV = document.getElementById('kpi-vix-val');
      if (vixV) { vixV.textContent = d.vix; vixV.style.color = vixC; }
      _set('kpi-vix-sub', d.vix > 35 ? '🔴 Pánico de mercado' : d.vix > 25 ? '🟠 Alta volatilidad' : d.vix > 18 ? '🟡 Volatilidad elevada' : '🟢 Calma — baja vol');

      // ── SP500 ──
      _set('kpi-sp500-val', d.sp500_display);
      const spSub = document.getElementById('kpi-sp500-sub');
      if (spSub) {
        const badges = { open:'<span class="mkt-badge mkt-open">● Abierto</span>', premarket:'<span class="mkt-badge mkt-pre">◑ Pre-apertura</span>', weekend:'<span class="mkt-badge mkt-weekend">Fin de semana</span>', closed:'<span class="mkt-badge mkt-closed">Mercado cerrado</span>' };
        spSub.innerHTML = badges[d.market_status] || badges.closed;
      }

      // ── Accuracy ──
      _set('kpi-acc-val', d.accuracy + '%');
      _setH('kpi-acc-sub',
        `<span style="color:var(--green)">${d.wins}✓</span> · <span style="color:var(--red)">${d.losses}✗</span> · <span style="color:var(--yellow)">${d.pending_ct} pend.</span>`);
    })
    .catch(() => {});
}

// Refresh every 60s silently
setInterval(updateData, 60000);

// ── Historial expandible ─────────────────────────────────────────────
function toggleHist(rowEl) {
  const detailRow = rowEl.nextElementSibling;
  const inner = detailRow ? detailRow.querySelector('.hist-detail-inner') : null;
  if (!inner) return;
  const isOpen = rowEl.classList.contains('open');
  // Cerrar todos los abiertos
  document.querySelectorAll('.hist-row.open').forEach(r => {
    r.classList.remove('open');
    const dr = r.nextElementSibling;
    if (dr) { const inn = dr.querySelector('.hist-detail-inner'); if (inn) inn.classList.remove('open'); }
  });
  if (!isOpen) {
    rowEl.classList.add('open');
    inner.classList.add('open');
    inner.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}
</script>
</body>
</html>"""


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET", "POST"])
def login():
    if session.get("auth"):
        return redirect(url_for("dashboard"))
    error = ""
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["auth"] = True
            return redirect(url_for("dashboard"))
        error = "Contraseña incorrecta. Inténtalo de nuevo."
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/dashboard")
def dashboard():
    if not session.get("auth"):
        return redirect(url_for("login"))
    data = build_payload()
    return render_template_string(DASHBOARD_HTML, **data)


@app.route("/api/data")
def api_data():
    if not session.get("auth"):
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(build_payload())


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ─── Start ────────────────────────────────────────────────────────────────────

def start_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_web()
