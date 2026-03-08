"""StockBot Pro — Panel de control web v2 (multi-página, premium design)"""

from flask import Flask, render_template_string, session, redirect, url_for, request, jsonify
import json, os, time, threading, math
from datetime import datetime, timedelta, date as _dt_date
from calendar import monthrange as _mrange, month_name as _mname
from collections import Counter as _Counter
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

# Caché de sparklines: ticker → ([puntos normalizados], timestamp)
_sparkline_cache = {}
_sparkline_lock  = threading.Lock()
_SPARKLINE_TTL   = 600  # 10 min

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


def _fetch_sparkline(ticker):
    """Devuelve lista de puntos normalizados 0-100 para sparkline. Caché 10 min."""
    now_ts = time.time()
    with _sparkline_lock:
        cached = _sparkline_cache.get(ticker)
        if cached and now_ts - cached[1] < _SPARKLINE_TTL:
            return cached[0]
    result = []
    if _HAS_YF:
        try:
            hist = yf.Ticker(ticker).history(period='5d', interval='1d')
            if not hist.empty and len(hist) >= 2:
                closes = hist['Close'].tolist()
                mn, mx = min(closes), max(closes)
                rng = mx - mn or 1
                result = [round((p - mn) / rng * 100) for p in closes]
        except Exception:
            pass
    with _sparkline_lock:
        _sparkline_cache[ticker] = (result, now_ts)
    return result


def _sparkline_svg(points):
    """Genera SVG inline de sparkline a partir de puntos normalizados."""
    if not points or len(points) < 2:
        return ''
    w, h = 72, 24
    n = len(points)
    xs = [round(i / (n - 1) * w, 1) for i in range(n)]
    ys = [round(h - points[i] / 100 * (h - 4) - 2, 1) for i in range(n)]
    path = f'M {xs[0]} {ys[0]} ' + ' '.join(f'L {xs[i]} {ys[i]}' for i in range(1, n))
    color = '#00e07a' if points[-1] >= points[0] else '#ff3b5c'
    return (f'<svg viewBox="0 0 {w} {h}" style="width:{w}px;height:{h}px;display:block">'
            f'<path d="{path}" stroke="{color}" stroke-width="1.5" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{xs[-1]}" cy="{ys[-1]}" r="2.5" fill="{color}"/>'
            f'</svg>')


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

        # Sparkline (5 días)
        spark_pts = _fetch_sparkline(p.get("ticker", ""))
        p["sparkline_svg"] = _sparkline_svg(spark_pts)

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

    # ── Patrones por día de semana ──────────────────────────────────────
    _DOW_NAMES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    by_dow = {i: {"w": 0, "l": 0, "name": _DOW_NAMES[i]} for i in range(7)}
    by_conf_range = {
        "85-87%": {"w": 0, "l": 0, "label": "Normal (85–87%)"},
        "88-93%": {"w": 0, "l": 0, "label": "Fuerte (88–93%)"},
        "94%+":   {"w": 0, "l": 0, "label": "Excepcional (94%+)"},
    }
    all_by_date = sorted(
        [p for p in preds if p.get("result") in ("win", "loss")],
        key=lambda x: x.get("date", "")
    )
    for p in all_by_date:
        if p.get("date"):
            try:
                dow = datetime.fromisoformat(p["date"][:10]).weekday()
                by_dow[dow]["w" if p["result"] == "win" else "l"] += 1
            except Exception:
                pass
        conf = p.get("confidence", 0) or 0
        key = "94%+" if conf >= 94 else "88-93%" if conf >= 88 else "85-87%"
        by_conf_range[key]["w" if p["result"] == "win" else "l"] += 1

    # Streaks
    streak_max_win = streak_max_loss = 0
    cur_streak = 0
    cur_type = None
    for p in all_by_date:
        r = p["result"]
        cur_streak = cur_streak + 1 if r == cur_type else 1
        cur_type = r
        if r == "win":  streak_max_win  = max(streak_max_win,  cur_streak)
        else:           streak_max_loss = max(streak_max_loss, cur_streak)
    cur_streak_type, cur_streak_ct = None, 0
    for p in reversed(all_by_date):
        r = p["result"]
        if cur_streak_type is None:
            cur_streak_type, cur_streak_ct = r, 1
        elif r == cur_streak_type:
            cur_streak_ct += 1
        else:
            break

    # ── Calendario mensual ──────────────────────────────────────────────
    _today    = _dt_date.today()
    _cal_y, _cal_m = _today.year, _today.month
    _, _days_in_month = _mrange(_cal_y, _cal_m)
    _first_dow = _dt_date(_cal_y, _cal_m, 1).weekday()
    cal_data = {}
    for p in preds:
        if p.get("result") in ("win", "loss") and p.get("date"):
            try:
                d = datetime.fromisoformat(p["date"][:10]).date()
                if d.year == _cal_y and d.month == _cal_m:
                    ep = p.get("entry", 0) or 0
                    xp = p.get("exit_price", 0) or 0
                    pl_v = 0.0
                    if ep and xp:
                        raw = (xp - ep) / ep * 100
                        pl_v = raw if p.get("signal") == "COMPRAR" else -raw
                    dy = d.day
                    if dy not in cal_data:
                        cal_data[dy] = {"w": 0, "l": 0, "pl": 0.0}
                    cal_data[dy]["w" if p["result"] == "win" else "l"] += 1
                    cal_data[dy]["pl"] = round(cal_data[dy]["pl"] + pl_v, 1)
            except Exception:
                pass
    # Build calendar HTML (Python)
    _cal_html = '<div class="cal-grid">'
    for h in ["L", "M", "X", "J", "V", "S", "D"]:
        _cal_html += f'<div class="cal-hdr">{h}</div>'
    for _ in range(_first_dow):
        _cal_html += '<div class="cal-day"></div>'
    for day in range(1, _days_in_month + 1):
        data = cal_data.get(day, {})
        today_cls = " today" if day == _today.day else ""
        if data:
            pl_v = data["pl"]
            clr = "#00e07a" if pl_v >= 0 else "#ff3b5c"
            bg  = "rgba(0,224,122,.09)" if pl_v >= 0 else "rgba(255,59,92,.09)"
            brd = "rgba(0,224,122,.3)"  if pl_v >= 0 else "rgba(255,59,92,.3)"
            pl_str = f"{'+' if pl_v >= 0 else ''}{round(pl_v,1)}%"
            _cal_html += (f'<div class="cal-day has-data{today_cls}" style="border-color:{brd};background:{bg}">'
                          f'<span class="cal-num" style="color:{clr}">{day}</span>'
                          f'<span class="cal-pl" style="color:{clr}">{pl_str}</span>'
                          f'<span class="cal-wl">{data["w"]}W·{data["l"]}L</span></div>')
        else:
            _cal_html += f'<div class="cal-day{today_cls}"><span class="cal-num">{day}</span></div>'
    _cal_html += '</div>'
    cal_month_name = f"{_mname[_cal_m]} {_cal_y}"

    # ── Ticker stats (modal) ────────────────────────────────────────────
    ticker_stats = {}
    for p in preds:
        t = p.get("ticker", "")
        if not t:
            continue
        if t not in ticker_stats:
            ticker_stats[t] = []
        ep = p.get("entry", 0) or 0
        xp = p.get("exit_price", 0) or 0
        pl_v = None
        if ep and xp and p.get("result") in ("win", "loss"):
            raw = (xp - ep) / ep * 100
            pl_v = round(raw if p.get("signal") == "COMPRAR" else -raw, 1)
        ticker_stats[t].append({
            "date":   (p.get("date") or "")[:10],
            "signal": p.get("signal", ""),
            "result": p.get("result", "pending"),
            "entry":  round(ep, 2),
            "exit":   round(xp, 2) if xp else None,
            "pl":     pl_v,
            "conf":   int(p.get("confidence", 0) or 0),
            "why":    _infer_exit_reason(p) if p.get("result") in ("win", "loss") else "active",
        })

    # ── Detección de duplicados ─────────────────────────────────────────
    _tk_counts = _Counter(p.get("ticker", "") for p in pending)
    for p in pending:
        p["is_duplicate"] = _tk_counts.get(p.get("ticker", ""), 0) > 1

    # ── Earnings próximos (señales activas) ─────────────────────────────
    earnings_upcoming = sorted(
        [{"ticker": p["ticker"], "date": p["earnings_date"],
          "signal": p.get("signal", ""), "during": p.get("earnings_during", False)}
         for p in pending if p.get("earnings_date")],
        key=lambda x: x["date"]
    )[:10]

    # ── Time Machine data ───────────────────────────────────────────────
    tm_trades = []
    for p in preds:
        if p.get("result") in ("win", "loss") and p.get("date"):
            ep = p.get("entry", 0) or 0
            xp = p.get("exit_price", 0) or 0
            pl_v = None
            if ep and xp:
                raw = (xp - ep) / ep * 100
                pl_v = round(raw if p.get("signal") == "COMPRAR" else -raw, 1)
            tm_trades.append({"date": p.get("date", "")[:10], "result": p.get("result", ""), "pl": pl_v, "ticker": p.get("ticker", "")})
    tm_trades.sort(key=lambda x: x["date"])
    tm_max_days = 0
    if tm_trades:
        try:
            oldest = datetime.fromisoformat(tm_trades[0]["date"]).date()
            tm_max_days = (_today - oldest).days
        except Exception:
            pass

    # ── Gauge circular F&G ─────────────────────────────────────────────
    fg_angle_rad  = math.radians(180 - fg / 100 * 180)
    fg_dot_x      = round(60 + 45 * math.cos(fg_angle_rad), 1)
    fg_dot_y      = round(60 - 45 * math.sin(fg_angle_rad), 1)
    fg_dash_offset = round(141.4 * (1 - fg / 100), 1)

    # ── Bot vs SPY ─────────────────────────────────────────────────────
    sorted_resolved = sorted(
        [p for p in preds if p.get("result") in ("win", "loss") and p.get("date") and p.get("entry")],
        key=lambda x: x["date"]
    )[-20:]
    bot_labels = ["Inicio"]
    bot_data   = [0]
    running    = 0.0
    for p in sorted_resolved:
        entry_p  = p.get("entry", 0) or 0
        exit_p   = p.get("exit_price", 0) or 0
        if entry_p and exit_p:
            raw = (exit_p - entry_p) / entry_p * 100
            pl  = raw if p.get("signal") == "COMPRAR" else -raw
        else:
            pl = 0.0
        running += pl
        bot_data.append(round(running, 1))
        bot_labels.append(p.get("ticker", "")[:5])
    n_trades = len(sorted_resolved)
    spy_mom  = regime.get("spy_mom3m", 0) or 0
    spy_step = spy_mom / n_trades if n_trades else 0
    spy_data = [round(i * spy_step, 1) for i in range(len(bot_data))]

    # ── Monthly performance (last 12 months) ────────────────────────────
    _monthly_perf = {}
    for p in preds:
        if p.get("result") in ("win", "loss") and p.get("date"):
            try:
                _d = datetime.fromisoformat(p["date"][:10]).date()
                _mk = f"{_d.year}-{_d.month:02d}"
                ep = p.get("entry", 0) or 0
                xp = p.get("exit_price", 0) or 0
                pl_v = 0.0
                if ep and xp:
                    raw = (xp - ep) / ep * 100
                    pl_v = round(raw if p.get("signal") == "COMPRAR" else -raw, 1)
                if _mk not in _monthly_perf:
                    _monthly_perf[_mk] = {"pl": 0.0, "w": 0, "l": 0, "label": _d.strftime("%b %y")}
                _monthly_perf[_mk]["w" if p["result"] == "win" else "l"] += 1
                _monthly_perf[_mk]["pl"] = round(_monthly_perf[_mk]["pl"] + pl_v, 1)
            except Exception:
                pass
    monthly_perf = dict(sorted(_monthly_perf.items())[-12:])

    # ── P/L per trade (last 30 resolved) ────────────────────────────────
    pl_per_trade = []
    for p in sorted(
        [p for p in preds if p.get("result") in ("win", "loss") and p.get("date")],
        key=lambda x: x.get("date", ""))[-30:]:
        ep = p.get("entry", 0) or 0
        xp = p.get("exit_price", 0) or 0
        pl_v = 0.0
        if ep and xp:
            raw = (xp - ep) / ep * 100
            pl_v = round(raw if p.get("signal") == "COMPRAR" else -raw, 1)
        pl_per_trade.append({"ticker": p.get("ticker", "")[:6], "pl": pl_v, "result": p.get("result", "")})

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
        "fg_dot_x":         fg_dot_x,
        "fg_dot_y":         fg_dot_y,
        "fg_dash_offset":   fg_dash_offset,
        "bot_labels":       bot_labels,
        "bot_data":         bot_data,
        "spy_data":         spy_data,
        "by_dow":           by_dow,
        "by_conf_range":    by_conf_range,
        "streak_max_win":   streak_max_win,
        "streak_max_loss":  streak_max_loss,
        "cur_streak_type":  cur_streak_type,
        "cur_streak_ct":    cur_streak_ct,
        "cal_html":         _cal_html,
        "cal_month_name":   cal_month_name,
        "cal_data":         cal_data,
        "ticker_stats":     ticker_stats,
        "tm_trades":        tm_trades,
        "tm_max_days":      tm_max_days,
        "earnings_upcoming": earnings_upcoming,
        "monthly_perf":     monthly_perf,
        "pl_per_trade":     pl_per_trade,
        "n_bot_trades":     n_trades,
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
<script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.2/dist/confetti.browser.min.js"></script>
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
.kpi-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:28px;position:relative;z-index:10}
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
  position:absolute;top:calc(100% + 10px);left:50%;bottom:auto;
  background:var(--s3);border:1px solid var(--b2);
  border-radius:10px;padding:12px 14px;
  font-size:12px;color:var(--t1);line-height:1.6;
  white-space:normal;z-index:9999;
  opacity:0;pointer-events:none;
  transition:opacity .15s,transform .15s;
  transform:translateX(-50%) translateY(-4px);
  box-shadow:0 8px 32px rgba(0,0,0,.7);
  min-width:200px;max-width:280px;
}
.has-tooltip:hover .tooltip{opacity:1;transform:translateX(-50%) translateY(0)}
.tooltip::after{
  content:'';position:absolute;bottom:100%;left:50%;transform:translateX(-50%);
  border:6px solid transparent;border-bottom-color:var(--s3);
}
/* Disable tooltips on touch/mobile — they get stuck and block content */
@media(hover:none),(max-width:700px){
  .has-tooltip .tooltip{display:none!important}
}
.tt-row{display:flex;justify-content:space-between;gap:20px;padding:2px 0}
.tt-range{color:var(--t2)}
.tt-label{color:var(--t1);font-weight:600}

/* ── POSITION SIZING ── */
.ps-card{background:linear-gradient(135deg,rgba(0,224,122,.04) 0%,rgba(100,149,255,.04) 100%);border:1px solid rgba(0,224,122,.2)!important}
.ps-input-wrap{display:flex;align-items:center;gap:0;border:1px solid var(--b2);border-radius:8px;overflow:hidden;background:var(--s2)}
.ps-currency{background:var(--s3);border:none;border-right:1px solid var(--b2);color:var(--t1);font-size:14px;font-weight:700;padding:8px 10px;cursor:pointer;outline:none;font-family:inherit}
.ps-capital{background:transparent;border:none;color:var(--t1);font-size:16px;font-weight:700;padding:8px 12px;width:130px;outline:none;font-family:inherit}
.ps-capital::placeholder{color:var(--t3);font-weight:400}
.ps-risk-wrap{display:flex;align-items:center;gap:10px}
.ps-risk-range{-webkit-appearance:none;width:100px;height:4px;border-radius:4px;background:var(--b2);outline:none;cursor:pointer}
.ps-risk-range::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--green);cursor:pointer;box-shadow:0 0 6px rgba(0,224,122,.5)}
.ps-risk-val{font-size:15px;font-weight:800;color:var(--green);min-width:32px}
.ps-summary{display:flex;gap:16px;flex-wrap:wrap;align-items:center}
.ps-stat{display:flex;flex-direction:column;gap:2px}
.ps-stat-val{font-size:15px;font-weight:800;color:var(--t1)}
.ps-stat-lbl{font-size:10px;color:var(--t3);text-transform:uppercase;letter-spacing:.4px}
.ps-factors{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--b1)}
.ps-factor{display:flex;align-items:center;gap:5px;background:var(--s2);border:1px solid var(--b1);border-radius:6px;padding:4px 9px;font-size:11px}
.ps-factor-name{color:var(--t2)}
.ps-factor-val{font-weight:700}
/* Position sizing column in table */
.ps-cell{display:flex;flex-direction:column;gap:2px;min-width:110px}
.ps-pct{font-size:13px;font-weight:800;color:var(--green)}
.ps-eur{font-size:12px;color:var(--t1);font-weight:600}
.ps-acc{font-size:10px;color:var(--t3)}
.ps-empty{font-size:11px;color:var(--t3)}

/* ── TOAST ── */
#toast-container{position:fixed;bottom:24px;right:24px;z-index:99999;display:flex;flex-direction:column;gap:10px;pointer-events:none}
.toast{display:flex;align-items:center;gap:10px;background:var(--s2);border:1px solid var(--b2);border-radius:10px;padding:12px 16px;font-size:13px;color:var(--t1);box-shadow:0 8px 32px rgba(0,0,0,.6);animation:toast-in .3s ease;pointer-events:auto;max-width:320px}
.toast.t-success{border-color:rgba(0,224,122,.35);background:rgba(0,224,122,.07)}
.toast.t-info{border-color:rgba(61,142,248,.35);background:rgba(61,142,248,.07)}
.toast.t-warn{border-color:rgba(245,166,35,.35);background:rgba(245,166,35,.07)}
@keyframes toast-in{from{transform:translateX(110%);opacity:0}to{transform:translateX(0);opacity:1}}
.toast-out{animation:toast-fade .3s ease forwards!important}
@keyframes toast-fade{to{transform:translateX(110%);opacity:0}}

/* ── HAMBURGER / MOBILE NAV ── */
.hamburger{display:none;flex-direction:column;gap:5px;background:none;border:none;cursor:pointer;padding:6px;border-radius:8px}
.hamburger span{width:20px;height:2px;background:var(--t2);border-radius:2px;transition:all .2s}
@media(max-width:700px){
  .hamburger{display:flex}
  .nav-center{display:none;position:fixed;top:56px;left:0;right:0;z-index:198;background:rgba(8,12,20,.97);backdrop-filter:blur(16px);border-bottom:1px solid var(--b1);padding:8px 12px;flex-direction:column;gap:2px}
  .nav-center.open{display:flex}
  .nav-tab{width:100%;justify-content:flex-start;padding:10px 14px;font-size:14px}
  .live-pill{display:none}
  .kpi-grid{grid-template-columns:repeat(2,1fr)!important}
  .page{padding:10px 10px 40px}
  .section{margin-bottom:16px}
  .card{padding:14px}
  .sh{font-size:10px;margin-bottom:10px}
  .sum-row{grid-template-columns:repeat(2,1fr)!important;gap:10px}
  .sum-val{font-size:22px}
  .g2,.g3,.g13{grid-template-columns:1fr!important}
  .bvs-box{height:180px}
  .tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{font-size:12px}
  td,th{padding:8px 10px}
  .modal-box{width:calc(100vw - 24px);max-height:85vh;overflow-y:auto}
  .tm-stats{grid-template-columns:repeat(2,1fr)}
  .dow-grid{grid-template-columns:repeat(4,1fr)}
  #toast-container{bottom:12px;right:12px;left:12px}
  .toast{max-width:100%}
}

/* ── SPARKLINE ── */
.spark-cell{padding:8px 14px!important}

/* ── BOT vs SPY ── */
.bvs-box{position:relative;height:220px}

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

/* ── MODAL TICKER ── */
.modal-overlay{position:fixed;inset:0;z-index:9000;background:rgba(0,0,0,.72);backdrop-filter:blur(5px);display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .2s}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal-box{background:var(--s1);border:1px solid var(--b2);border-radius:16px;width:min(680px,94vw);max-height:82vh;overflow-y:auto;padding:24px;position:relative;transform:translateY(14px);transition:transform .22s;box-shadow:0 24px 64px rgba(0,0,0,.7)}
.modal-overlay.open .modal-box{transform:translateY(0)}
.modal-close{position:absolute;top:16px;right:16px;background:rgba(255,255,255,.05);border:1px solid var(--b1);border-radius:8px;color:var(--t2);font-size:15px;width:32px;height:32px;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;line-height:1}
.modal-close:hover{background:var(--rd);color:var(--red);border-color:rgba(255,59,92,.3)}

/* ── CALENDARIO P/L ── */
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:5px}
.cal-hdr{font-size:10px;font-weight:700;text-transform:uppercase;color:var(--t3);text-align:center;padding:4px 0;letter-spacing:.5px}
.cal-day{min-height:54px;border-radius:8px;border:1px solid transparent;padding:5px 6px;display:flex;flex-direction:column;gap:2px;background:rgba(255,255,255,.02)}
.cal-day.today{border-color:var(--blue)!important;background:rgba(61,142,248,.06)!important}
.cal-day.has-data{border-width:1px}
.cal-num{font-size:11px;font-weight:600;color:var(--t3)}
.cal-day.today .cal-num{color:var(--blue)}
.cal-pl{font-size:11px;font-weight:700;line-height:1.2}
.cal-wl{font-size:9px;color:var(--t3)}

/* ── TIME MACHINE ── */
.tm-wrap{padding:10px 0 4px}
.tm-slider{-webkit-appearance:none;appearance:none;width:100%;height:5px;border-radius:3px;background:var(--b1);outline:none;cursor:pointer;transition:background .2s}
.tm-slider::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;border-radius:50%;background:var(--green);border:2px solid var(--bg);box-shadow:0 0 10px rgba(0,224,122,.45);cursor:pointer;transition:box-shadow .2s}
.tm-slider::-moz-range-thumb{width:20px;height:20px;border-radius:50%;background:var(--green);border:2px solid var(--bg);cursor:pointer}
.tm-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}
.tm-stat{background:var(--s2);border:1px solid var(--b1);border-radius:10px;padding:12px;text-align:center}
.tm-val{font-size:22px;font-weight:800;letter-spacing:-.5px}
.tm-lbl{font-size:10px;font-weight:600;letter-spacing:.5px;text-transform:uppercase;color:var(--t3);margin-top:3px}

/* ── HEATMAP DOW ── */
.dow-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:8px}
.dow-cell{border-radius:10px;padding:14px 6px;text-align:center;border:1px solid var(--b1)}
.dow-name{font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;color:var(--t3);margin-bottom:6px}
.dow-pct{font-size:20px;font-weight:800;line-height:1}
.dow-ops{font-size:10px;color:var(--t3);margin-top:4px}

/* ── TK LINK ── */
.tk-link{cursor:pointer;transition:color .15s;border-bottom:1px dashed transparent}
.tk-link:hover{color:var(--green)!important;border-bottom-color:var(--green)}

/* ── SEARCH BAR ── */
.search-wrap{position:relative;flex:1}
.search-wrap input{width:100%;background:var(--s2);border:1px solid var(--b1);border-radius:8px;color:var(--t1);font-size:13px;padding:9px 12px 9px 36px;outline:none;font-family:inherit;transition:border-color .2s}
.search-wrap input:focus{border-color:rgba(61,142,248,.45)}
.search-wrap .si{position:absolute;left:11px;top:50%;transform:translateY(-50%);font-size:14px;pointer-events:none}

/* ── SORTABLE HEADERS ── */
th.sortable{cursor:pointer;user-select:none;transition:color .15s}
th.sortable:hover{color:var(--t1)}
th.sort-asc::after{content:' ▲';font-size:8px;color:var(--green)}
th.sort-desc::after{content:' ▼';font-size:8px;color:var(--green)}

/* ── PIN / NOTE BUTTONS ── */
.icon-btn{background:none;border:none;cursor:pointer;font-size:13px;padding:2px 4px;border-radius:4px;transition:all .15s;line-height:1;color:var(--t3)}
.icon-btn:hover{background:rgba(255,255,255,.07);color:var(--t1)}
.icon-btn.pinned{color:var(--green)}
.icon-btn.noted{color:var(--yellow)}
.pin-row{background:rgba(0,224,122,.03)!important}
.dup-badge{display:inline-flex;align-items:center;gap:3px;font-size:9px;font-weight:700;padding:1px 5px;border-radius:4px;background:rgba(245,166,35,.12);color:var(--yellow);border:1px solid rgba(245,166,35,.25);vertical-align:middle;margin-left:4px}

/* ── NOTE MODAL ── */
#note-modal .modal-box{max-width:420px}
#note-textarea{width:100%;height:110px;background:var(--s2);border:1px solid var(--b1);border-radius:8px;color:var(--t1);font-size:13px;padding:12px;font-family:inherit;resize:vertical;outline:none;line-height:1.6;transition:border-color .2s}
#note-textarea:focus{border-color:rgba(61,142,248,.4)}

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

<div id="toast-container"></div>

<!-- NAVBAR -->
<nav class="nav">
  <div class="nav-brand">
    <div class="dot">📈</div>
    StockBot Pro
    <span style="color:var(--t3);font-weight:400;font-size:12px">v5.2</span>
  </div>

  <div class="nav-center" id="nav-center">
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
    <button class="nav-tab" data-tab="charts" onclick="goTab('charts')">
      <span class="icon">📊</span> Gráficos
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
    <button class="hamburger" id="hamburger" onclick="toggleMobileNav()" aria-label="Menu">
      <span></span><span></span><span></span>
    </button>
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
      <div style="display:flex;align-items:center;gap:12px">
        <svg id="fg-gauge-svg" viewBox="0 0 120 70" style="width:100px;height:60px;overflow:visible;flex-shrink:0">
          <defs>
            <linearGradient id="fgGrad" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" style="stop-color:#ff3b5c"/>
              <stop offset="30%" style="stop-color:#f5a623"/>
              <stop offset="60%" style="stop-color:#7bed9f"/>
              <stop offset="100%" style="stop-color:#00e07a"/>
            </linearGradient>
          </defs>
          <path d="M 15 60 A 45 45 0 0 1 105 60" stroke="#1a2438" stroke-width="9" fill="none" stroke-linecap="round"/>
          <path d="M 15 60 A 45 45 0 0 1 105 60" stroke="url(#fgGrad)" stroke-width="9" fill="none" stroke-linecap="round" opacity="0.25"/>
          <path id="fg-gauge-fill" d="M 15 60 A 45 45 0 0 1 105 60" stroke="{{ fg_color }}" stroke-width="9" fill="none" stroke-linecap="round" stroke-dasharray="141.4" stroke-dashoffset="{{ fg_dash_offset }}"/>
          <circle id="fg-gauge-dot" cx="{{ fg_dot_x }}" cy="{{ fg_dot_y }}" r="5" fill="{{ fg_color }}" style="filter:drop-shadow(0 0 4px {{ fg_color }})"/>
          <text x="60" y="56" text-anchor="middle" font-size="16" font-weight="800" fill="{{ fg_color }}" font-family="Inter,sans-serif" id="fg-gauge-txt">{{ fear_greed }}</text>
        </svg>
        <div>
          <div id="kpi-fg-val" class="kpi-val" style="color:{{ fg_color }};font-size:22px">{{ fear_greed }}<span style="font-size:12px;font-weight:400;opacity:.7">/100</span></div>
          <div id="kpi-fg-sub" class="kpi-sub">{{ fg_label }}</div>
        </div>
      </div>
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
              <td><span class="tk tk-link" onclick="openTickerModal('{{ p.ticker }}')">{{ p.ticker }}</span></td>
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

  <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
    <div class="sh" style="margin-bottom:0;flex:1;min-width:200px">📡 Señales activas — {{ pending_ct }} predicciones en curso</div>
    <span style="font-size:11px;color:var(--t3);background:var(--s2);border:1px solid var(--b1);border-radius:6px;padding:4px 10px;white-space:nowrap">
      💡 Precio actualiza cada 3 min
    </span>
  </div>

  <!-- Buscador -->
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
    <div class="search-wrap">
      <span class="si">🔍</span>
      <input type="text" id="signals-search" placeholder="Buscar por ticker, sector, tipo..." oninput="filterSignals(this.value)">
    </div>
    <span id="signals-count" style="font-size:12px;color:var(--t3);white-space:nowrap;min-width:64px">{{ pending_ct }} señales</span>
  </div>

  <!-- Earnings próximos -->
  {% if earnings_upcoming %}
  <div class="card" style="margin-bottom:14px;padding:14px 16px">
    <div class="card-title" style="margin-bottom:10px">📅 Próximos earnings en señales activas</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      {% for e in earnings_upcoming %}
      <div style="display:flex;align-items:center;gap:8px;background:var(--s2);border:1px solid {% if e.during %}rgba(155,109,255,.35){% else %}var(--b1){% endif %};border-radius:8px;padding:7px 12px">
        <span class="tk-link" onclick="openTickerModal('{{ e.ticker }}')" style="font-weight:700;font-size:13px">{{ e.ticker }}</span>
        <span style="font-size:11px;color:var(--t3)">{{ e.date }}</span>
        {% if e.during %}<span class="b-earn-sm">⚠️ en ventana</span>{% endif %}
      </div>
      {% endfor %}
    </div>
  </div>
  {% endif %}

  <!-- ── POSITION SIZING CARD ── -->
  <div class="card ps-card" style="margin-bottom:16px;padding:16px 18px">
    <div style="display:flex;align-items:flex-start;gap:20px;flex-wrap:wrap">

      <!-- Capital input -->
      <div style="flex:0 0 auto">
        <div style="font-size:10px;font-weight:700;color:var(--t3);letter-spacing:.5px;text-transform:uppercase;margin-bottom:7px">💰 Tu capital disponible</div>
        <div class="ps-input-wrap">
          <select id="ps-currency" class="ps-currency" onchange="recalcPositions()">
            <option value="USD">$</option>
            <option value="EUR">€</option>
          </select>
          <input type="number" id="ps-capital" class="ps-capital" placeholder="10 000" min="100" step="500"
            oninput="recalcPositions()" onchange="recalcPositions()">
        </div>
        <div style="font-size:10px;color:var(--t3);margin-top:5px">Se guarda automáticamente</div>
      </div>

      <!-- Riesgo base slider -->
      <div style="flex:0 0 auto">
        <div style="font-size:10px;font-weight:700;color:var(--t3);letter-spacing:.5px;text-transform:uppercase;margin-bottom:7px">⚙️ Riesgo por operación</div>
        <div class="ps-risk-wrap">
          <input type="range" id="ps-risk" class="ps-risk-range" min="0.5" max="5" step="0.5" value="2" oninput="recalcPositions()">
          <span id="ps-risk-val" class="ps-risk-val">2%</span>
        </div>
        <div style="font-size:10px;color:var(--t3);margin-top:5px">Del capital arriesgado (≠ invertido)</div>
      </div>

      <!-- Resumen vivo -->
      <div style="flex:1;min-width:220px">
        <div style="font-size:10px;font-weight:700;color:var(--t3);letter-spacing:.5px;text-transform:uppercase;margin-bottom:7px">📊 Resumen de exposición</div>
        <div class="ps-summary" id="ps-summary">
          <span style="color:var(--t3);font-size:12px">Introduce tu capital para ver el sizing →</span>
        </div>
      </div>

    </div>
    <!-- Factores activos -->
    <div class="ps-factors" id="ps-factors" style="display:none"></div>
  </div>

  {% if pending %}
  <div class="card" style="margin-bottom:20px;padding:0">
    <div class="tw">
      <table>
        <thead>
          <tr>
            <th style="width:20px;padding:10px 6px"></th>
            <th class="sortable" onclick="sortTable(this,1)">Ticker</th>
            <th class="sortable" onclick="sortTable(this,2)">Señal</th>
            <th class="sortable" onclick="sortTable(this,3)">Tipo</th>
            <th>5D</th>
            <th class="sortable" onclick="sortTable(this,5)">Entrada</th>
            <th class="sortable" onclick="sortTable(this,6)">Precio actual</th>
            <th class="sortable" onclick="sortTable(this,7)">vs Entrada</th>
            <th class="sortable" onclick="sortTable(this,8)">Objetivo</th>
            <th class="sortable" onclick="sortTable(this,9)">Stop</th>
            <th class="sortable" onclick="sortTable(this,10)">Confianza</th>
            <th>Progreso</th>
            <th class="sortable" onclick="sortTable(this,12)">Earnings</th>
            <th class="sortable" onclick="sortTable(this,13)">Sector</th>
            <th class="sortable" onclick="sortTable(this,14)">Días</th>
            <th title="Position sizing basado en tu capital">💰 Posición</th>
            <th>📝</th>
          </tr>
        </thead>
        <tbody id="signals-tbody">
          {% for p in pending %}
          {% set st = p.get('signal_type','NORMAL') %}
          {% set has_cur = p.current_price is not none %}
          {% set pkey = 'sbp_pin_' ~ p.ticker ~ '_' ~ (p.date[:10] if p.date else '') %}
          {% set nkey = 'sbp_note_' ~ p.ticker ~ '_' ~ (p.date[:10] if p.date else '') %}
          <tr data-pin-key="{{ pkey }}" data-note-key="{{ nkey }}"
            data-entry="{{ p.entry|float }}"
            data-stop="{{ p.stop|float }}"
            data-confidence="{{ p.confidence|int }}"
            data-stype="{{ p.get('signal_type','NORMAL') }}"
            data-price="{{ p.current_price if p.current_price else p.entry|float }}">
            <td style="padding:8px 4px;text-align:center">
              <button class="icon-btn" data-pin-btn onclick="togglePin('{{ pkey }}',this)" title="Fijar señal">⭐</button>
            </td>
            <td>
              <span class="tk tk-link" onclick="openTickerModal('{{ p.ticker }}')">{{ p.ticker }}</span>
              {% if p.is_duplicate %}<span class="dup-badge">⚠ DUP</span>{% endif %}
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
            <td class="spark-cell">
              {% if p.sparkline_svg %}{{ p.sparkline_svg|safe }}{% else %}<span style="color:var(--t3);font-size:11px">—</span>{% endif %}
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
            <td class="ps-td" style="padding:8px 10px">
              <div class="ps-cell ps-empty">—</div>
            </td>
            <td style="padding:8px 6px;text-align:center">
              <button class="icon-btn" data-note-btn data-note-key="{{ nkey }}"
                onclick="openNoteModal('{{ p.ticker }}','{{ p.date[:10] if p.date else '' }}')"
                title="Añadir nota">📝</button>
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
            <td><span class="tk tk-link" onclick="openTickerModal('{{ p.ticker }}')">{{ p.ticker }}</span></td>
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
        <span class="tk-link" style="font-weight:700;font-size:14px;min-width:52px" onclick="openTickerModal('{{ p.ticker }}')">{{ p.ticker }}</span>
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


<!-- ════════════════════════════════════════════════════ TAB: CHARTS -->
<div class="tab-panel" id="tab-charts">

  <!-- ── Bot vs SPY ── -->
  {% set bvs_n = n_bot_trades %}
  {% if bvs_n >= 3 %}
  <div class="section">
    <div class="sh">📈 Bot vs SPY — P/L acumulado (últimas {{ bvs_n }} {{ 'operación' if bvs_n == 1 else 'operaciones' }})</div>
    <div class="card">
      <div style="font-size:12px;color:var(--t3);margin-bottom:12px">Comparativa del P/L acumulado del bot frente a SPY en el mismo período. La línea verde es el rendimiento real del bot; la azul punteada es el benchmark de SPY.</div>
      <div class="bvs-box"><canvas id="botVsSpy"></canvas></div>
    </div>
  </div>
  {% endif %}

  <!-- ── P/L mensual ── -->
  {% if monthly_perf %}
  <div class="section">
    <div class="sh">📆 P/L acumulado por mes</div>
    <div class="card">
      <div style="font-size:12px;color:var(--t3);margin-bottom:12px">Suma de P/L % de todas las operaciones cerradas por mes.</div>
      <div class="bvs-box" style="height:240px"><canvas id="monthlyPerfChart"></canvas></div>
    </div>
  </div>
  {% endif %}

  <!-- ── P/L por operación ── -->
  {% if pl_per_trade %}
  <div class="section">
    <div class="sh">🎯 Curva de equity — P/L acumulado por operación (últimas {{ pl_per_trade|length }} {{ 'operación' if pl_per_trade|length == 1 else 'operaciones' }})</div>
    <div class="card">
      <div style="font-size:12px;color:var(--t3);margin-bottom:12px">P/L % de cada operación resuelta. Verde = WIN, rojo = LOSS.</div>
      <div class="bvs-box" style="height:260px"><canvas id="plPerTradeChart"></canvas></div>
    </div>
  </div>
  {% endif %}

  <!-- ── Time Machine ── -->
  <div class="section">
    <div class="sh">⏪ Time Machine</div>
    <div class="card">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
        <span style="font-size:22px">⏪</span>
        <div>
          <div style="font-size:13px;font-weight:700;color:var(--t1)">Viaja al pasado</div>
          <div style="font-size:11px;color:var(--t3)">Arrastra para ver el rendimiento del bot en cualquier fecha pasada</div>
        </div>
        <span id="tm-date-lbl" style="margin-left:auto;font-size:12px;font-weight:600;color:var(--green);background:var(--g2);border:1px solid rgba(0,224,122,.2);border-radius:6px;padding:4px 12px;white-space:nowrap">Hoy</span>
      </div>
      <div class="tm-wrap">
        <input type="range" class="tm-slider" id="tm-slider" min="0" max="{{ tm_max_days }}" value="0" oninput="updateTimeMachine(this.value)">
      </div>
      <div class="tm-stats">
        <div class="tm-stat"><div class="tm-val col-blue" id="tm-total">{{ wins + losses }}</div><div class="tm-lbl">Operaciones</div></div>
        <div class="tm-stat"><div class="tm-val col-green" id="tm-wins">{{ wins }}</div><div class="tm-lbl">Wins</div></div>
        <div class="tm-stat"><div class="tm-val col-red" id="tm-losses">{{ losses }}</div><div class="tm-lbl">Losses</div></div>
        <div class="tm-stat"><div class="tm-val col-blue" id="tm-acc">{{ accuracy }}%</div><div class="tm-lbl">Precisión</div></div>
      </div>
    </div>
  </div>

  <!-- ── Detección de patrones ── -->
  <div class="sh">🔍 Detección de patrones automática</div>
  <div class="g2" style="margin-bottom:24px">

    <!-- Heatmap día de semana -->
    <div class="card">
      <div class="card-title">📅 Rendimiento por día de semana</div>
      <div class="dow-grid">
        {% for i in range(7) %}
        {% set dow = by_dow[i] %}
        {% set tot_d = dow.w + dow.l %}
        {% set pct_d = (dow.w / tot_d * 100)|int if tot_d > 0 else 0 %}
        <div class="dow-cell" style="background:{% if tot_d==0 %}rgba(255,255,255,.02){% elif pct_d>=65 %}rgba(0,224,122,.1){% elif pct_d>=50 %}rgba(61,142,248,.08){% elif pct_d>=40 %}rgba(245,166,35,.08){% else %}rgba(255,59,92,.08){% endif %};border-color:{% if tot_d==0 %}var(--b1){% elif pct_d>=65 %}rgba(0,224,122,.25){% elif pct_d>=50 %}rgba(61,142,248,.2){% elif pct_d>=40 %}rgba(245,166,35,.2){% else %}rgba(255,59,92,.25){% endif %}">
          <div class="dow-name">{{ dow.name }}</div>
          <div class="dow-pct" style="color:{% if tot_d==0 %}var(--t3){% elif pct_d>=65 %}var(--green){% elif pct_d>=50 %}var(--blue){% elif pct_d>=40 %}var(--yellow){% else %}var(--red){% endif %}">{% if tot_d>0 %}{{ pct_d }}%{% else %}—{% endif %}</div>
          <div class="dow-ops">{% if tot_d>0 %}{{ dow.w }}W/{{ dow.l }}L{% else %}sin datos{% endif %}</div>
        </div>
        {% endfor %}
      </div>
    </div>

    <!-- Confianza + Rachas -->
    <div class="card">
      <div class="card-title">🎯 Precisión por nivel de confianza</div>
      {% for key, data in by_conf_range.items() %}
      {% set tot_c = data.w + data.l %}
      {% set pct_c = (data.w / tot_c * 100)|int if tot_c > 0 else 0 %}
      <div style="margin-bottom:18px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:7px">
          <span style="font-size:13px;font-weight:600;color:var(--t1)">{{ data.label }}</span>
          <span style="font-size:18px;font-weight:800;color:{% if pct_c>=65 %}var(--green){% elif pct_c<45 and tot_c>0 %}var(--red){% else %}var(--yellow){% endif %}">{% if tot_c>0 %}{{ pct_c }}%{% else %}—{% endif %}</span>
        </div>
        {% if tot_c>0 %}
        <div style="height:7px;border-radius:4px;background:var(--b1);overflow:hidden;display:flex">
          <div style="width:{{ (data.w/tot_c*100)|int }}%;background:var(--green)"></div>
          <div style="width:{{ (data.l/tot_c*100)|int }}%;background:var(--red)"></div>
        </div>
        <div style="font-size:11px;color:var(--t3);margin-top:4px">{{ data.w }}W / {{ data.l }}L · {{ tot_c }} ops</div>
        {% else %}<div style="font-size:11px;color:var(--t3)">Sin datos aún</div>{% endif %}
      </div>
      {% endfor %}
      <div style="border-top:1px solid var(--b1);padding-top:14px;margin-top:4px">
        <div class="card-title" style="margin-bottom:10px">🔥 Rachas</div>
        <div class="sr">
          <span class="sr-label">Racha actual</span>
          <span class="sr-val" style="color:{% if cur_streak_type=='win' %}var(--green){% elif cur_streak_type=='loss' %}var(--red){% else %}var(--t3){% endif %}">
            {% if cur_streak_type %}{{ cur_streak_ct }} {{ '✓' if cur_streak_type=='win' else '✗' }} consecutivas{% else %}—{% endif %}
          </span>
        </div>
        <div class="sr"><span class="sr-label">Mejor racha wins</span><span class="sr-val col-green">{{ streak_max_win }} ✓</span></div>
        <div class="sr"><span class="sr-label">Peor racha losses</span><span class="sr-val col-red">{{ streak_max_loss }} ✗</span></div>
      </div>
    </div>

  </div>

  <!-- ── Calendario P/L mensual ── -->
  <div class="sh">📅 Calendario P/L — {{ cal_month_name }}</div>
  <div class="card" style="margin-bottom:24px">
    {{ cal_html|safe }}
    {% if not cal_data %}
    <div class="empty" style="padding:32px"><span class="ei">📅</span><p>Sin operaciones resueltas este mes</p></div>
    {% endif %}
  </div>

  <!-- ── Calendario de earnings ── -->
  <div class="sh">📅 Calendario de earnings — señales activas</div>
  <div class="card" style="margin-bottom:24px">
    {% if earnings_upcoming %}
    <div style="font-size:12px;color:var(--t3);margin-bottom:14px">Próximas fechas de presentación de resultados de tus señales activas. ⚠️ indica que el earnings cae dentro de la ventana activa de la señal.</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px">
      {% for e in earnings_upcoming %}
      <div style="display:flex;flex-direction:column;gap:4px;background:var(--s2);border:1px solid {% if e.during %}rgba(155,109,255,.35){% else %}var(--b1){% endif %};border-radius:10px;padding:12px 14px">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <span class="tk-link" onclick="openTickerModal('{{ e.ticker }}')" style="font-weight:700;font-size:15px">{{ e.ticker }}</span>
          {% if e.during %}<span style="font-size:10px;background:rgba(155,109,255,.15);color:#9b6dff;border:1px solid rgba(155,109,255,.3);border-radius:5px;padding:2px 7px">⚠️ Activo</span>{% endif %}
        </div>
        <div style="font-size:12px;color:var(--t2)">{{ e.date }}</div>
        <div style="font-size:11px;color:var(--t3)">
          {% if e.signal == 'COMPRAR' %}📈 Comprar{% else %}📉 Vender{% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="empty" style="padding:32px"><span class="ei">📅</span><p>Sin earnings próximos en señales activas</p></div>
    {% endif %}
  </div>

</div>
<!-- /charts -->


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

<!-- ════ MODAL TICKER ════ -->
<div class="modal-overlay" id="ticker-modal" onclick="if(event.target===this)closeTickerModal()">
  <div class="modal-box">
    <button class="modal-close" onclick="closeTickerModal()">✕</button>
    <div id="modal-content"></div>
  </div>
</div>

<!-- ════ MODAL NOTA ════ -->
<div class="modal-overlay" id="note-modal" onclick="if(event.target===this)closeNoteModal()">
  <div class="modal-box">
    <button class="modal-close" onclick="closeNoteModal()">✕</button>
    <div style="font-size:14px;font-weight:700;margin-bottom:4px">📝 Nota personal</div>
    <div style="font-size:12px;color:var(--t3);margin-bottom:14px" id="note-modal-sub">Ticker</div>
    <textarea id="note-textarea" placeholder="Escribe tu análisis, motivo de entrada, alertas personales..."></textarea>
    <div style="display:flex;gap:10px;margin-top:12px;justify-content:flex-end">
      <button onclick="deleteNote()" style="background:none;border:1px solid rgba(255,59,92,.3);border-radius:8px;color:var(--red);font-size:12px;padding:7px 14px;cursor:pointer;font-family:inherit">Borrar</button>
      <button onclick="closeNoteModal()" style="background:none;border:1px solid var(--b1);border-radius:8px;color:var(--t2);font-size:12px;padding:7px 14px;cursor:pointer;font-family:inherit">Cancelar</button>
      <button onclick="saveNote()" style="background:var(--blue);border:none;border-radius:8px;color:#fff;font-size:13px;font-weight:600;padding:7px 18px;cursor:pointer;font-family:inherit">Guardar ✓</button>
    </div>
  </div>
</div>

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
  // close mobile nav
  const nc = document.getElementById('nav-center');
  if (nc) nc.classList.remove('open');
}

// restore hash on load
(function() {
  const h = window.location.hash.replace('#', '');
  if (h && document.getElementById('tab-' + h)) goTab(h);
})();

// ── Mobile hamburger ────────────────────────────────────────────────
function toggleMobileNav() {
  const nc = document.getElementById('nav-center');
  if (nc) nc.classList.toggle('open');
}

// ── Toast notifications ─────────────────────────────────────────────
function showToast(msg, type='info', duration=4000) {
  const ct = document.getElementById('toast-container');
  if (!ct) return;
  const el = document.createElement('div');
  el.className = 'toast t-' + type;
  el.innerHTML = msg;
  ct.appendChild(el);
  setTimeout(() => {
    el.classList.add('toast-out');
    setTimeout(() => el.remove(), 350);
  }, duration);
}

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

// ── Bot vs SPY chart (Gráficos tab) ─────────────────────────────────
{% if n_bot_trades >= 3 and bot_data|length > 1 %}
const ctxBvS = document.getElementById('botVsSpy');
if (ctxBvS) {
  new Chart(ctxBvS.getContext('2d'), {
    type: 'line',
    data: {
      labels: {{ bot_labels|tojson }},
      datasets: [
        {
          label: 'Bot acumulado %',
          data: {{ bot_data|tojson }},
          borderColor: '#00e07a',
          backgroundColor: 'rgba(0,224,122,.07)',
          fill: true,
          tension: 0.4,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointBackgroundColor: '#00e07a',
          borderWidth: 2,
        },
        {
          label: 'SPY ~equiv %',
          data: {{ spy_data|tojson }},
          borderColor: '#3d8ef8',
          backgroundColor: 'transparent',
          fill: false,
          tension: 0.4,
          borderDash: [5, 4],
          pointRadius: 0,
          borderWidth: 1.5,
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true, labels: { color: '#8596b0', font: { size: 11 }, boxWidth: 16 } },
        tooltip: { callbacks: { label: c => ` ${c.dataset.label}: ${c.parsed.y > 0 ? '+' : ''}${c.parsed.y}%` } }
      },
      scales: {
        x: { ticks: { color: '#4a5a72', font: { size: 10 } }, grid: { color: 'rgba(30,45,68,.4)' } },
        y: { ticks: { color: '#4a5a72', font: { size: 10 }, callback: v => (v >= 0 ? '+' : '') + v + '%' }, grid: { color: 'rgba(30,45,68,.4)' }, zero: true }
      }
    }
  });
}
{% endif %}

// ── Monthly P/L line chart ───────────────────────────────────────────
{% if monthly_perf %}
const ctxMP = document.getElementById('monthlyPerfChart');
if (ctxMP) {
  const mpLabels = {{ monthly_perf.values()|map(attribute='label')|list|tojson }};
  const mpData   = {{ monthly_perf.values()|map(attribute='pl')|list|tojson }};
  const mpCtx = ctxMP.getContext('2d');
  const mpGrad = mpCtx.createLinearGradient(0, 0, 0, 200);
  mpGrad.addColorStop(0,   'rgba(0,224,122,.25)');
  mpGrad.addColorStop(0.6, 'rgba(0,224,122,.05)');
  mpGrad.addColorStop(1,   'rgba(0,224,122,0)');
  new Chart(mpCtx, {
    type: 'line',
    data: {
      labels: mpLabels,
      datasets: [{
        label: 'P/L mensual %',
        data: mpData,
        borderColor: '#00e07a',
        backgroundColor: mpGrad,
        fill: true,
        tension: 0.4,
        pointRadius: 5,
        pointHoverRadius: 7,
        pointBackgroundColor: mpData.map(v => v >= 0 ? '#00e07a' : '#ff3b5c'),
        pointBorderColor: '#080c14',
        pointBorderWidth: 2,
        borderWidth: 2.5,
        segment: { borderColor: ctx => ctx.p0.parsed.y >= 0 ? '#00e07a' : '#ff3b5c' }
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(13,20,32,.95)',
          borderColor: 'rgba(37,51,73,.6)',
          borderWidth: 1,
          callbacks: {
            label: c => ` P/L: ${c.parsed.y >= 0 ? '+' : ''}${c.parsed.y}%`
          }
        }
      },
      scales: {
        x: { ticks: { color: '#4a5a72', font: { size: 11 } }, grid: { color: 'rgba(30,45,68,.4)' }, border: { color: 'rgba(30,45,68,.4)' } },
        y: {
          ticks: { color: '#4a5a72', font: { size: 11 }, callback: v => (v >= 0 ? '+' : '') + v + '%' },
          grid: { color: 'rgba(30,45,68,.4)' },
          border: { color: 'rgba(30,45,68,.4)' }
        }
      }
    }
  });
}
{% endif %}

// ── P/L per trade line chart ─────────────────────────────────────────
{% if pl_per_trade %}
const ctxPT = document.getElementById('plPerTradeChart');
if (ctxPT) {
  const ptLabels = {{ pl_per_trade|map(attribute='ticker')|list|tojson }};
  const ptData   = {{ pl_per_trade|map(attribute='pl')|list|tojson }};
  // Cumulative P/L for a nice equity-curve look
  const ptCumul = [];
  let ptRun = 0;
  ptData.forEach(v => { ptRun += v; ptCumul.push(Math.round(ptRun * 10) / 10); });
  const ptCtx = ctxPT.getContext('2d');
  const ptFinalPositive = ptCumul.length ? ptCumul[ptCumul.length-1] >= 0 : true;
  const ptColor = ptFinalPositive ? '#00e07a' : '#ff3b5c';
  const ptGrad = ptCtx.createLinearGradient(0, 0, 0, 200);
  ptGrad.addColorStop(0,   ptFinalPositive ? 'rgba(0,224,122,.22)' : 'rgba(255,59,92,.22)');
  ptGrad.addColorStop(1,   'rgba(0,0,0,0)');
  new Chart(ptCtx, {
    type: 'line',
    data: {
      labels: ptLabels,
      datasets: [
        {
          label: 'P/L acumulado %',
          data: ptCumul,
          borderColor: ptColor,
          backgroundColor: ptGrad,
          fill: true,
          tension: 0.35,
          pointRadius: 4,
          pointHoverRadius: 7,
          pointBackgroundColor: ptData.map(v => v >= 0 ? '#00e07a' : '#ff3b5c'),
          pointBorderColor: '#080c14',
          pointBorderWidth: 2,
          borderWidth: 2.5,
        },
        {
          label: 'P/L individual %',
          data: ptData,
          borderColor: 'rgba(61,142,248,.5)',
          backgroundColor: 'transparent',
          fill: false,
          tension: 0,
          pointRadius: 3,
          pointBackgroundColor: ptData.map(v => v >= 0 ? 'rgba(0,224,122,.7)' : 'rgba(255,59,92,.7)'),
          borderWidth: 1,
          borderDash: [3,3],
        }
      ]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true, labels: { color: '#8596b0', font: { size: 11 }, boxWidth: 14, padding: 16 } },
        tooltip: {
          backgroundColor: 'rgba(13,20,32,.95)',
          borderColor: 'rgba(37,51,73,.6)',
          borderWidth: 1,
          callbacks: {
            label: c => ` ${c.dataset.label}: ${c.parsed.y >= 0 ? '+' : ''}${c.parsed.y}%`
          }
        }
      },
      scales: {
        x: { ticks: { color: '#4a5a72', font: { size: 10 }, maxRotation: 40 }, grid: { color: 'rgba(30,45,68,.4)' }, border: { color: 'rgba(30,45,68,.4)' } },
        y: {
          ticks: { color: '#4a5a72', font: { size: 11 }, callback: v => (v >= 0 ? '+' : '') + v + '%' },
          grid: { color: 'rgba(30,45,68,.4)' },
          border: { color: 'rgba(30,45,68,.4)' }
        }
      }
    }
  });
}
{% endif %}

// ── Silent AJAX refresh ─────────────────────────────────────────────
function _set(id, txt) { const e = document.getElementById(id); if (e) e.textContent = txt; }
function _setH(id, html) { const e = document.getElementById(id); if (e) e.innerHTML = html; }

let _prevPendingCt = {{ pending_ct }};
let _prevWins      = {{ wins }};
// Position sizing globals (updated by live refresh)
let _liveRegime = '{{ regime }}';
let _liveVix    = {{ vix }};

function updateData() {
  fetch('/api/data')
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (!d) return;

      // ── Toast & confetti logic ──
      if (d.pending_ct > _prevPendingCt) {
        showToast('📡 <b>Nueva señal activa</b>', 'info');
      } else if (d.pending_ct < _prevPendingCt) {
        if (d.wins > _prevWins) {
          showToast('🎯 <b>Señal cerrada en WIN!</b>', 'success', 5000);
          if (typeof confetti !== 'undefined') {
            confetti({ particleCount: 120, spread: 70, origin: { y: 0.6 }, colors: ['#00e07a', '#3d8ef8', '#f5a623'] });
          }
        } else {
          showToast('📊 Señal cerrada', 'info');
        }
      }
      _prevPendingCt = d.pending_ct;
      _prevWins      = d.wins;

      // Timestamps & counts
      _set('nav-pending-ct', d.pending_ct);
      _set('updated-ts', d.updated);
      _set('dash-updated', d.updated);

      // Update position sizing globals
      _liveRegime = d.regime;
      _liveVix    = d.vix;
      recalcPositions();

      // ── Régimen ──
      const regC = d.regime === 'BEAR' ? 'var(--red)' : d.regime === 'BULL' ? 'var(--green)' : 'var(--yellow)';
      const regB = d.regime === 'BEAR' ? 'bear' : d.regime === 'BULL' ? 'bull' : 'lat';
      _setH('kpi-regime-val',
        `${d.regime} <span class="badge b-${regB}" style="font-size:11px;vertical-align:middle;margin-left:4px">${d.regime_str}%</span>`);
      const rv = document.getElementById('kpi-regime-val');
      if (rv) rv.style.color = regC;
      _setH('kpi-regime-sub', `SPY <b>${d.spy_mom3m >= 0 ? '+' : ''}${d.spy_mom3m.toFixed(1)}%</b> en 3 meses`);

      // ── Fear & Greed ──
      if (d.fear_greed !== undefined) {
        const fv = document.getElementById('kpi-fg-val');
        if (fv) { fv.innerHTML = `${d.fear_greed}<span style="font-size:12px;font-weight:400;opacity:.7">/100</span>`; fv.style.color = d.fg_color; }
        _set('kpi-fg-sub', d.fg_label);
        // Update circular gauge
        const fill = document.getElementById('fg-gauge-fill');
        const dot  = document.getElementById('fg-gauge-dot');
        const txt  = document.getElementById('fg-gauge-txt');
        if (fill) { fill.setAttribute('stroke-dashoffset', (141.4*(1-d.fear_greed/100)).toFixed(1)); fill.setAttribute('stroke', d.fg_color); }
        if (dot)  { const angle = (180 - d.fear_greed/100*180)*Math.PI/180; dot.setAttribute('cx', (60+45*Math.cos(angle)).toFixed(1)); dot.setAttribute('cy', (60-45*Math.sin(angle)).toFixed(1)); dot.setAttribute('fill', d.fg_color); }
        if (txt)  { txt.textContent = d.fear_greed; txt.setAttribute('fill', d.fg_color); }
      }

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

// ── Confetti en wins recientes ───────────────────────────────────────
{% if recent and recent[0].result == 'win' %}
setTimeout(() => {
  if (typeof confetti !== 'undefined') {
    confetti({ particleCount: 80, spread: 60, origin: { y: 0.7 }, colors: ['#00e07a', '#3d8ef8', '#f5a623'] });
  }
}, 1800);
{% endif %}

// ── Time Machine ─────────────────────────────────────────────────────
const TM_TRADES = {{ tm_trades|tojson }};
function updateTimeMachine(daysBack) {
  daysBack = parseInt(daysBack);
  const lbl = document.getElementById('tm-date-lbl');
  if (daysBack === 0) {
    if (lbl) lbl.textContent = 'Hoy';
    const wins = TM_TRADES.filter(t => t.result === 'win').length;
    const losses = TM_TRADES.filter(t => t.result === 'loss').length;
    _applyTM(wins, losses);
    return;
  }
  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - daysBack);
  const cutStr = cutoff.toISOString().split('T')[0];
  if (lbl) lbl.textContent = cutStr;
  const filtered = TM_TRADES.filter(t => t.date <= cutStr);
  const wins = filtered.filter(t => t.result === 'win').length;
  const losses = filtered.filter(t => t.result === 'loss').length;
  _applyTM(wins, losses);
}
function _applyTM(wins, losses) {
  const total = wins + losses;
  const acc = total > 0 ? (Math.round(wins / total * 1000) / 10) : 0;
  const _s = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  _s('tm-total', total);
  _s('tm-wins', wins);
  _s('tm-losses', losses);
  _s('tm-acc', acc + '%');
}

// ── Ticker Modal ─────────────────────────────────────────────────────
const TICKER_STATS = {{ ticker_stats|tojson }};
function openTickerModal(ticker) {
  const trades = (TICKER_STATS[ticker] || []).slice().reverse();
  const resolved = trades.filter(t => t.result === 'win' || t.result === 'loss');
  const wins = resolved.filter(t => t.result === 'win').length;
  const losses = resolved.filter(t => t.result === 'loss').length;
  const total = wins + losses;
  const acc = total > 0 ? Math.round(wins / total * 100) : 0;
  const accClr = acc >= 60 ? 'var(--green)' : acc >= 45 ? 'var(--yellow)' : 'var(--red)';

  let html = `<div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:20px;flex-wrap:wrap">
    <div style="font-size:32px;font-weight:800;letter-spacing:-.5px;color:var(--t1)">${ticker}</div>
    <div style="flex:1;min-width:160px">
      <div style="display:flex;gap:14px;flex-wrap:wrap;margin-bottom:6px">
        <span style="font-size:13px;color:var(--green);font-weight:600">✓ ${wins} wins</span>
        <span style="font-size:13px;color:var(--red);font-weight:600">✗ ${losses} losses</span>
        <span style="font-size:13px;font-weight:700;color:${accClr}">${total > 0 ? acc + '% precisión' : 'Sin historial'}</span>
      </div>
      <div style="height:5px;border-radius:3px;background:var(--b1);overflow:hidden;display:flex;max-width:200px">
        ${total > 0 ? `<div style="width:${Math.round(wins/total*100)}%;background:var(--green)"></div><div style="width:${Math.round(losses/total*100)}%;background:var(--red)"></div>` : ''}
      </div>
    </div>
  </div>`;

  if (trades.length === 0) {
    html += `<div class="empty"><span class="ei">📋</span><p>Sin historial para ${ticker}</p></div>`;
  } else {
    html += `<div class="tw"><table>
      <thead><tr><th>Fecha</th><th>Señal</th><th>Entrada</th><th>Salida</th><th>P/L</th><th>Conf.</th><th>Resultado</th></tr></thead>
      <tbody>`;
    for (const t of trades) {
      const plStr   = t.pl !== null && t.pl !== undefined ? `${t.pl >= 0 ? '+' : ''}${t.pl}%` : '—';
      const plColor = t.pl === null || t.pl === undefined ? 'var(--t3)' : t.pl >= 0 ? 'var(--green)' : 'var(--red)';
      const resBadge = t.result === 'pending'
        ? '<span class="badge b-sq" style="font-size:10px">Activa</span>'
        : t.result === 'win'
          ? '<span class="badge b-win" style="font-size:10px">✓ WIN</span>'
          : '<span class="badge b-loss" style="font-size:10px">✗ LOSS</span>';
      const sigBadge = t.signal === 'COMPRAR'
        ? '<span class="badge b-buy" style="font-size:10px">📈 Buy</span>'
        : '<span class="badge b-sell" style="font-size:10px">📉 Sell</span>';
      const exitStr = t.exit ? `$${parseFloat(t.exit).toFixed(2)}` : '—';
      html += `<tr>
        <td style="font-size:11px;color:var(--t3)">${t.date}</td>
        <td>${sigBadge}</td>
        <td class="mono">$${parseFloat(t.entry).toFixed(2)}</td>
        <td class="mono">${exitStr}</td>
        <td style="font-weight:700;color:${plColor}">${plStr}</td>
        <td style="font-size:12px;color:var(--t2)">${t.conf}%</td>
        <td>${resBadge}</td>
      </tr>`;
    }
    html += '</tbody></table></div>';
  }

  document.getElementById('modal-content').innerHTML = html;
  document.getElementById('ticker-modal').classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeTickerModal() {
  document.getElementById('ticker-modal').classList.remove('open');
  document.body.style.overflow = '';
}
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeTickerModal(); closeNoteModal(); }
});

// ── Buscador señales ─────────────────────────────────────────────────
function filterSignals(q) {
  q = q.toLowerCase().trim();
  const tbody = document.getElementById('signals-tbody');
  if (!tbody) return;
  let vis = 0;
  tbody.querySelectorAll('tr').forEach(row => {
    const match = !q || row.textContent.toLowerCase().includes(q);
    row.style.display = match ? '' : 'none';
    if (match) vis++;
  });
  const ct = document.getElementById('signals-count');
  if (ct) ct.textContent = vis + ' señal' + (vis !== 1 ? 'es' : '');
}

// ── Ordenar columnas ─────────────────────────────────────────────────
const _sortState = {};
function sortTable(th, col) {
  const tbody = document.getElementById('signals-tbody');
  if (!tbody) return;
  const dir = _sortState[col] === 'asc' ? 'desc' : 'asc';
  _sortState[col] = dir;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    const av = a.cells[col] ? a.cells[col].textContent.trim() : '';
    const bv = b.cells[col] ? b.cells[col].textContent.trim() : '';
    const an = parseFloat(av.replace(/[^0-9.\-]/g, ''));
    const bn = parseFloat(bv.replace(/[^0-9.\-]/g, ''));
    if (!isNaN(an) && !isNaN(bn)) return dir === 'asc' ? an - bn : bn - an;
    return dir === 'asc' ? av.localeCompare(bv, 'es') : bv.localeCompare(av, 'es');
  });
  rows.forEach(r => tbody.appendChild(r));
  th.closest('thead').querySelectorAll('th').forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
  th.classList.add('sort-' + dir);
}

// ── Pins (localStorage) ──────────────────────────────────────────────
function togglePin(pkey, btn) {
  const isPinned = !!localStorage.getItem(pkey);
  if (isPinned) localStorage.removeItem(pkey);
  else          localStorage.setItem(pkey, '1');
  loadPins();
}
function loadPins() {
  const tbody = document.getElementById('signals-tbody');
  if (!tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const pinned = [], unpinned = [];
  rows.forEach(row => {
    const pkey = row.getAttribute('data-pin-key');
    const btn  = row.querySelector('[data-pin-btn]');
    const on   = pkey && !!localStorage.getItem(pkey);
    if (on) {
      pinned.push(row);
      row.classList.add('pin-row');
      if (btn) { btn.textContent = '📌'; btn.classList.add('pinned'); }
    } else {
      unpinned.push(row);
      row.classList.remove('pin-row');
      if (btn) { btn.textContent = '⭐'; btn.classList.remove('pinned'); }
    }
  });
  [...pinned, ...unpinned].forEach(r => tbody.appendChild(r));
}

// ── Notas (localStorage) ─────────────────────────────────────────────
let _curNoteKey = '';
function openNoteModal(ticker, date) {
  _curNoteKey = 'sbp_note_' + ticker + '_' + date;
  document.getElementById('note-modal-sub').textContent = ticker + (date ? ' · ' + date : '');
  document.getElementById('note-textarea').value = localStorage.getItem(_curNoteKey) || '';
  document.getElementById('note-modal').classList.add('open');
  document.getElementById('note-textarea').focus();
  document.body.style.overflow = 'hidden';
}
function closeNoteModal() {
  document.getElementById('note-modal').classList.remove('open');
  document.body.style.overflow = '';
}
function saveNote() {
  const val = document.getElementById('note-textarea').value.trim();
  if (val) localStorage.setItem(_curNoteKey, val);
  else     localStorage.removeItem(_curNoteKey);
  closeNoteModal();
  loadNoteBadges();
  showToast('📝 Nota guardada', 'success', 2500);
}
function deleteNote() {
  localStorage.removeItem(_curNoteKey);
  closeNoteModal();
  loadNoteBadges();
}
function loadNoteBadges() {
  document.querySelectorAll('[data-note-btn]').forEach(btn => {
    const nkey = btn.getAttribute('data-note-key');
    const note = nkey ? localStorage.getItem(nkey) : null;
    if (note) { btn.textContent = '🗒️'; btn.classList.add('noted'); btn.title = note.substring(0, 80); }
    else       { btn.textContent = '📝'; btn.classList.remove('noted'); btn.title = 'Añadir nota'; }
  });
}

// Init Ronda 3 on load
document.addEventListener('DOMContentLoaded', () => {
  loadPins();
  loadNoteBadges();
});

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

// ── POSITION SIZING ─────────────────────────────────────────────────────
(function initPositionSizing() {
  const capEl  = document.getElementById('ps-capital');
  const curEl  = document.getElementById('ps-currency');
  const riskEl = document.getElementById('ps-risk');
  if (!capEl) return;
  // Restore from localStorage
  const saved = localStorage.getItem('ps_capital');
  const savedCur = localStorage.getItem('ps_currency') || 'USD';
  const savedRisk = localStorage.getItem('ps_risk') || '2';
  if (saved) capEl.value = saved;
  if (curEl) curEl.value = savedCur;
  if (riskEl) { riskEl.value = savedRisk; document.getElementById('ps-risk-val').textContent = savedRisk + '%'; }
  if (saved) recalcPositions();
})();

function recalcPositions() {
  const capEl   = document.getElementById('ps-capital');
  const curEl   = document.getElementById('ps-currency');
  const riskEl  = document.getElementById('ps-risk');
  const riskLbl = document.getElementById('ps-risk-val');
  if (!capEl) return;

  const capital  = parseFloat(capEl.value) || 0;
  const currency = curEl ? curEl.value : 'USD';
  const sym      = currency === 'EUR' ? '€' : '$';
  const baseRisk = parseFloat(riskEl ? riskEl.value : 2);
  if (riskLbl) riskLbl.textContent = baseRisk + '%';

  // Save to localStorage
  if (capital > 0) localStorage.setItem('ps_capital', capital);
  localStorage.setItem('ps_currency', currency);
  localStorage.setItem('ps_risk', baseRisk);

  // ── Factores de ajuste ──
  const regimeFactor = _liveRegime === 'BEAR' ? 0.6 : _liveRegime === 'LATERAL' ? 0.8 : 1.0;
  const vixFactor    = _liveVix > 30 ? 0.7 : _liveVix > 20 ? 0.85 : 1.0;

  // ── Factores card visual ──
  const factorsEl = document.getElementById('ps-factors');
  if (factorsEl && capital > 0) {
    factorsEl.style.display = 'flex';
    const regColor = _liveRegime === 'BEAR' ? 'var(--red)' : _liveRegime === 'LATERAL' ? 'var(--yellow)' : 'var(--green)';
    const vixColor = _liveVix > 30 ? 'var(--red)' : _liveVix > 20 ? 'var(--yellow)' : 'var(--green)';
    factorsEl.innerHTML =
      `<span style="font-size:10px;color:var(--t3);align-self:center;margin-right:4px">Factores activos:</span>` +
      `<span class="ps-factor"><span class="ps-factor-name">Régimen</span><span class="ps-factor-val" style="color:${regColor}">${_liveRegime} ×${regimeFactor.toFixed(1)}</span></span>` +
      `<span class="ps-factor"><span class="ps-factor-name">VIX ${_liveVix}</span><span class="ps-factor-val" style="color:${vixColor}">×${vixFactor.toFixed(2)}</span></span>` +
      `<span class="ps-factor"><span class="ps-factor-name">Riesgo base</span><span class="ps-factor-val" style="color:var(--green)">${baseRisk}%</span></span>` +
      `<span style="font-size:10px;color:var(--t3);align-self:center;margin-left:4px">· Ajuste final = riesgo base × régimen × VIX × confianza</span>`;
  } else if (factorsEl) {
    factorsEl.style.display = 'none';
  }

  // ── Calcular por cada fila ──
  const rows = document.querySelectorAll('#signals-tbody tr');
  let totalExposed = 0, totalRisk = 0, signalCount = 0;

  rows.forEach(row => {
    const entry      = parseFloat(row.dataset.entry)      || 0;
    const stop       = parseFloat(row.dataset.stop)       || 0;
    const confidence = parseFloat(row.dataset.confidence) || 0;
    const stype      = row.dataset.stype || 'NORMAL';
    const price      = parseFloat(row.dataset.price)      || entry;
    const psCell     = row.querySelector('.ps-td .ps-cell');
    if (!psCell) return;

    if (capital <= 0 || entry <= 0 || stop <= 0) {
      psCell.innerHTML = '<span class="ps-empty">—</span>';
      return;
    }

    // Factores por señal
    const confFactor = confidence >= 97 ? 1.2 : confidence >= 94 ? 1.0 : confidence >= 88 ? 0.8 : 0.6;
    const typeFactor = stype === 'INSIDER_MASSIVE' ? 1.1 : stype === 'PRE_EARNINGS' ? 0.8 : stype === 'SHORT_SQUEEZE' ? 0.9 : 1.0;

    // Riesgo ajustado %
    const adjRiskPct = baseRisk * confFactor * regimeFactor * vixFactor * typeFactor;

    // Dinero que arriesgas
    const riskAmount = capital * adjRiskPct / 100;

    // Stop distance por acción
    const stopDist = Math.abs(entry - stop);
    if (stopDist < 0.01) { psCell.innerHTML = '<span class="ps-empty">Stop inválido</span>'; return; }

    // Nº de acciones = riesgo / distancia al stop
    const shares = Math.floor(riskAmount / stopDist);
    if (shares < 1) { psCell.innerHTML = `<span class="ps-empty">&lt;1 acc · sube capital</span>`; return; }

    // Valor total de la posición
    const posValue = shares * price;
    const posPct   = (posValue / capital * 100).toFixed(1);

    totalExposed += posValue;
    totalRisk    += riskAmount;
    signalCount++;

    // Color según tamaño de posición
    const pctNum = parseFloat(posPct);
    const pctColor = pctNum >= 15 ? 'var(--green)' : pctNum >= 8 ? 'var(--blue)' : 'var(--t2)';

    psCell.innerHTML =
      `<span class="ps-pct" style="color:${pctColor}">${posPct}%</span>` +
      `<span class="ps-eur">= ${sym}${posValue < 10000 ? posValue.toFixed(0) : (posValue/1000).toFixed(1)+'k'}</span>` +
      `<span class="ps-acc">~${shares} acc · riesgo ${sym}${riskAmount.toFixed(0)}</span>`;
  });

  // ── Resumen global ──
  const summaryEl = document.getElementById('ps-summary');
  if (!summaryEl) return;
  if (capital <= 0) {
    summaryEl.innerHTML = '<span style="color:var(--t3);font-size:12px">Introduce tu capital para ver el sizing →</span>';
    return;
  }
  const expPct  = (totalExposed / capital * 100).toFixed(1);
  const riskPct = (totalRisk    / capital * 100).toFixed(1);
  const expColor  = parseFloat(expPct)  > 80 ? 'var(--red)' : parseFloat(expPct)  > 50 ? 'var(--yellow)' : 'var(--green)';
  const riskColor = parseFloat(riskPct) > 10 ? 'var(--red)' : parseFloat(riskPct) > 5  ? 'var(--yellow)' : 'var(--green)';

  summaryEl.innerHTML =
    `<div class="ps-stat"><span class="ps-stat-val">${sym}${capital.toLocaleString()}</span><span class="ps-stat-lbl">Capital total</span></div>` +
    `<div class="ps-stat"><span class="ps-stat-val" style="color:${expColor}">${sym}${totalExposed < 10000 ? totalExposed.toFixed(0) : (totalExposed/1000).toFixed(1)+'k'} <span style="font-size:11px;opacity:.7">(${expPct}%)</span></span><span class="ps-stat-lbl">Capital expuesto</span></div>` +
    `<div class="ps-stat"><span class="ps-stat-val" style="color:${riskColor}">${sym}${totalRisk.toFixed(0)} <span style="font-size:11px;opacity:.7">(${riskPct}%)</span></span><span class="ps-stat-lbl">Riesgo máx. total</span></div>` +
    `<div class="ps-stat"><span class="ps-stat-val">${signalCount}</span><span class="ps-stat-lbl">Señales calculadas</span></div>`;
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
