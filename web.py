"""StockBot Pro — Panel de control web"""

from flask import Flask, render_template_string, session, redirect, url_for, request, jsonify
import json, os
from datetime import datetime
import pytz

app = Flask(__name__)
app.secret_key = "stk_web_2026_xK9mP_secreto"
PASSWORD = "stockbot2026"
SPAIN_TZ = pytz.timezone("Europe/Madrid")
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")


def _rjson(name, default):
    try:
        p = os.path.join(DATA_DIR, name)
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return default


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
    wins    = sum(1 for p in preds if p.get("result") == "win")
    losses  = sum(1 for p in preds if p.get("result") == "loss")
    resolved = wins + losses
    accuracy = round(wins / resolved * 100, 1) if resolved > 0 else 0

    recent = sorted(
        [p for p in preds if p.get("result") in ("win", "loss")],
        key=lambda x: x.get("date", ""), reverse=True
    )[:15]

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
        "#ff4757" if fg < 25 else
        "#ff6b35" if fg < 45 else
        "#ffd32a" if fg < 55 else
        "#7bed9f" if fg < 75 else
        "#00d084"
    )

    return {
        "pending":      pending,
        "recent":       recent,
        "wins":         wins,
        "losses":       losses,
        "accuracy":     accuracy,
        "total":        len(preds),
        "pending_ct":   len(pending),
        "regime":       regime.get("regime", "?"),
        "regime_str":   regime.get("strength", 0),
        "regime_desc":  regime.get("description", ""),
        "spy_mom3m":    regime.get("spy_mom3m", 0),
        "fear_greed":   fg,
        "fg_label":     fg_label,
        "fg_color":     fg_color,
        "vix":          mctx.get("vix", 0),
        "sp500":        mctx.get("sp500_change", 0),
        "rules":        len(learnings.get("rules", [])),
        "high_impact":  econ.get("is_high_impact", False),
        "eco_events":   econ.get("high_impact_today", []),
        "by_type":      by_type,
        "by_sector":    by_sector,
        "macro_news":   mctx.get("macro_news", []),
        "updated":      datetime.now(SPAIN_TZ).strftime("%H:%M:%S · %d/%m/%Y"),
    }


# ─── Login ────────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockBot Pro — Login</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{
    background:#07090f;
    min-height:100vh;
    display:flex;
    align-items:center;
    justify-content:center;
    font-family:'Segoe UI',system-ui,sans-serif;
    overflow:hidden;
  }
  /* animated grid bg */
  body::before{
    content:'';
    position:fixed;inset:0;
    background-image:
      linear-gradient(rgba(0,208,132,.04) 1px,transparent 1px),
      linear-gradient(90deg,rgba(0,208,132,.04) 1px,transparent 1px);
    background-size:40px 40px;
    animation:gridMove 20s linear infinite;
  }
  @keyframes gridMove{to{background-position:40px 40px}}
  .card{
    position:relative;
    background:rgba(15,22,35,.95);
    border:1px solid rgba(0,208,132,.2);
    border-radius:20px;
    padding:48px 40px;
    width:100%;max-width:420px;
    box-shadow:0 0 60px rgba(0,208,132,.08),0 20px 60px rgba(0,0,0,.6);
  }
  .logo{
    text-align:center;margin-bottom:32px;
  }
  .logo-icon{
    font-size:48px;display:block;margin-bottom:12px;
    filter:drop-shadow(0 0 16px rgba(0,208,132,.5));
  }
  .logo h1{
    color:#e2e8f0;font-size:22px;font-weight:700;letter-spacing:.5px;
  }
  .logo p{color:#6b7fa0;font-size:13px;margin-top:4px}
  .field{margin-bottom:20px}
  .field label{
    display:block;color:#8899b0;font-size:12px;
    font-weight:600;letter-spacing:.8px;text-transform:uppercase;
    margin-bottom:8px;
  }
  .field-wrap{position:relative}
  .field input{
    width:100%;
    background:rgba(255,255,255,.04);
    border:1px solid rgba(255,255,255,.1);
    border-radius:10px;
    color:#e2e8f0;
    font-size:15px;
    padding:12px 44px 12px 16px;
    outline:none;
    transition:border-color .2s,box-shadow .2s;
  }
  .field input:focus{
    border-color:rgba(0,208,132,.5);
    box-shadow:0 0 0 3px rgba(0,208,132,.1);
  }
  .eye-btn{
    position:absolute;right:14px;top:50%;transform:translateY(-50%);
    background:none;border:none;cursor:pointer;
    color:#6b7fa0;font-size:16px;
    transition:color .2s;
  }
  .eye-btn:hover{color:#00d084}
  .btn{
    width:100%;
    background:linear-gradient(135deg,#00d084,#00b870);
    border:none;border-radius:10px;
    color:#07090f;font-size:15px;font-weight:700;
    padding:14px;cursor:pointer;
    transition:transform .15s,box-shadow .15s;
    letter-spacing:.3px;
  }
  .btn:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(0,208,132,.3)}
  .btn:active{transform:translateY(0)}
  .error{
    background:rgba(255,71,87,.12);
    border:1px solid rgba(255,71,87,.3);
    border-radius:8px;
    color:#ff6b7a;
    font-size:13px;
    padding:10px 14px;
    margin-bottom:16px;
    text-align:center;
  }
  .footer{text-align:center;margin-top:24px;color:#3a4a60;font-size:12px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <span class="logo-icon">📈</span>
    <h1>StockBot Pro</h1>
    <p>Panel de control — v5.2</p>
  </div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <div class="field">
      <label>Contraseña</label>
      <div class="field-wrap">
        <input type="password" name="password" id="pwd" placeholder="••••••••••••" autofocus>
        <button type="button" class="eye-btn" onclick="togglePwd()">👁</button>
      </div>
    </div>
    <button type="submit" class="btn">Entrar al Dashboard</button>
  </form>
  <div class="footer">StockBot Pro © 2026</div>
</div>
<script>
function togglePwd(){
  const i=document.getElementById('pwd');
  i.type=i.type==='password'?'text':'password';
}
</script>
</body>
</html>"""


# ─── Dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>StockBot Pro — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#07090f;
    --card:#0f1623;
    --card2:#131b2a;
    --border:#1a2440;
    --green:#00d084;
    --red:#ff4757;
    --yellow:#ffd32a;
    --blue:#0fbcf9;
    --purple:#a55eea;
    --orange:#f7b731;
    --text:#e2e8f0;
    --muted:#6b7fa0;
    --muted2:#3a4a60;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}

  /* NAVBAR */
  .navbar{
    position:sticky;top:0;z-index:100;
    background:rgba(7,9,15,.92);
    backdrop-filter:blur(12px);
    border-bottom:1px solid var(--border);
    display:flex;align-items:center;justify-content:space-between;
    padding:0 24px;height:60px;
  }
  .nav-brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:16px}
  .nav-brand span{font-size:22px}
  .nav-right{display:flex;align-items:center;gap:16px;font-size:13px;color:var(--muted)}
  .live-dot{
    width:8px;height:8px;border-radius:50%;
    background:var(--green);
    box-shadow:0 0 8px var(--green);
    animation:pulse 2s infinite;
    display:inline-block;margin-right:6px;
  }
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .logout{
    background:rgba(255,71,87,.12);
    border:1px solid rgba(255,71,87,.25);
    color:#ff6b7a;border-radius:8px;
    padding:6px 14px;font-size:12px;font-weight:600;
    text-decoration:none;transition:background .2s;cursor:pointer;
  }
  .logout:hover{background:rgba(255,71,87,.22)}
  .refresh-badge{
    background:rgba(0,208,132,.08);
    border:1px solid rgba(0,208,132,.2);
    color:var(--green);border-radius:6px;padding:4px 10px;font-size:11px;
  }

  /* LAYOUT */
  .page{max-width:1400px;margin:0 auto;padding:24px}

  /* METRIC CARDS */
  .metrics{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
    gap:16px;
    margin-bottom:24px;
  }
  .metric{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:14px;
    padding:20px;
    position:relative;overflow:hidden;
    transition:border-color .2s,transform .15s;
  }
  .metric:hover{border-color:rgba(0,208,132,.3);transform:translateY(-2px)}
  .metric::before{
    content:'';position:absolute;top:0;left:0;right:0;height:2px;
  }
  .metric.green::before{background:linear-gradient(90deg,var(--green),transparent)}
  .metric.red::before{background:linear-gradient(90deg,var(--red),transparent)}
  .metric.yellow::before{background:linear-gradient(90deg,var(--yellow),transparent)}
  .metric.blue::before{background:linear-gradient(90deg,var(--blue),transparent)}
  .metric.purple::before{background:linear-gradient(90deg,var(--purple),transparent)}
  .metric-label{color:var(--muted);font-size:11px;font-weight:600;letter-spacing:.8px;text-transform:uppercase;margin-bottom:10px}
  .metric-value{font-size:28px;font-weight:800;line-height:1;margin-bottom:6px}
  .metric-sub{color:var(--muted);font-size:12px}
  .regime-bear{color:var(--red)}
  .regime-bull{color:var(--green)}
  .regime-lateral{color:var(--yellow)}

  /* FG BAR */
  .fg-bar{height:4px;border-radius:2px;background:var(--border);margin:8px 0 4px;overflow:hidden}
  .fg-bar-fill{height:100%;border-radius:2px;transition:width .5s}

  /* SECTION */
  .section{margin-bottom:24px}
  .section-title{
    font-size:13px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;
    color:var(--muted);margin-bottom:14px;
    display:flex;align-items:center;gap:8px;
  }
  .section-title::after{content:'';flex:1;height:1px;background:var(--border)}

  /* GRID 2COL */
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;margin-bottom:24px}
  @media(max-width:900px){.grid2,.grid3{grid-template-columns:1fr}}

  /* CARD */
  .card{
    background:var(--card);
    border:1px solid var(--border);
    border-radius:14px;
    padding:20px;
  }
  .card-title{
    font-size:12px;font-weight:700;letter-spacing:.6px;text-transform:uppercase;
    color:var(--muted);margin-bottom:16px;
  }

  /* TABLE */
  .table-wrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{
    color:var(--muted);font-size:11px;font-weight:600;
    letter-spacing:.6px;text-transform:uppercase;
    padding:8px 12px;text-align:left;
    border-bottom:1px solid var(--border);
  }
  td{padding:10px 12px;border-bottom:1px solid rgba(26,36,64,.6);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,.02)}

  /* BADGES */
  .badge{
    display:inline-flex;align-items:center;
    font-size:10px;font-weight:700;letter-spacing:.5px;
    padding:3px 8px;border-radius:5px;text-transform:uppercase;white-space:nowrap;
  }
  .badge-buy{background:rgba(0,208,132,.15);color:var(--green);border:1px solid rgba(0,208,132,.3)}
  .badge-sell{background:rgba(255,71,87,.15);color:var(--red);border:1px solid rgba(255,71,87,.3)}
  .badge-normal{background:rgba(107,127,160,.15);color:var(--muted);border:1px solid rgba(107,127,160,.3)}
  .badge-earnings{background:rgba(165,94,234,.15);color:var(--purple);border:1px solid rgba(165,94,234,.3)}
  .badge-squeeze{background:rgba(247,183,49,.15);color:var(--orange);border:1px solid rgba(247,183,49,.3)}
  .badge-insider{background:rgba(15,188,249,.15);color:var(--blue);border:1px solid rgba(15,188,249,.3)}
  .badge-win{background:rgba(0,208,132,.12);color:var(--green);border:1px solid rgba(0,208,132,.25)}
  .badge-loss{background:rgba(255,71,87,.12);color:var(--red);border:1px solid rgba(255,71,87,.25)}
  .badge-bear{background:rgba(255,71,87,.15);color:var(--red);border:1px solid rgba(255,71,87,.3)}
  .badge-bull{background:rgba(0,208,132,.15);color:var(--green);border:1px solid rgba(0,208,132,.3)}
  .badge-lateral{background:rgba(255,211,42,.15);color:var(--yellow);border:1px solid rgba(255,211,42,.3)}

  /* PROGRESS BAR */
  .pbar-wrap{display:flex;align-items:center;gap:8px;min-width:120px}
  .pbar{flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
  .pbar-fill{height:100%;border-radius:2px;background:var(--green);transition:width .4s}
  .pbar-fill.neg{background:var(--red)}
  .pbar-pct{font-size:11px;white-space:nowrap;min-width:38px;text-align:right}
  .pct-pos{color:var(--green)}
  .pct-neg{color:var(--red)}

  /* CONFIDENCE BAR */
  .conf-wrap{display:flex;align-items:center;gap:6px}
  .conf-bar{width:60px;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
  .conf-fill{height:100%;border-radius:2px}

  /* TICKER */
  .ticker{font-weight:700;font-size:14px;color:var(--text);letter-spacing:.3px}

  /* PRICE */
  .price{font-family:'Courier New',monospace;font-size:12px;color:#a0b0c8}

  /* SIGNAL TYPE helper */
  {% macro stype_badge(t) %}
    {% if t=='PRE_EARNINGS' %}<span class="badge badge-earnings">Pre-Earnings</span>
    {% elif t=='SHORT_SQUEEZE' %}<span class="badge badge-squeeze">Squeeze</span>
    {% elif t=='INSIDER_MASSIVE' %}<span class="badge badge-insider">Insider</span>
    {% else %}<span class="badge badge-normal">Normal</span>
    {% endif %}
  {% endmacro %}

  /* EMPTY STATE */
  .empty{
    text-align:center;padding:40px;color:var(--muted2);
    font-size:13px;
  }
  .empty span{display:block;font-size:32px;margin-bottom:8px}

  /* ALERT BANNER */
  .alert-banner{
    display:flex;align-items:center;gap:10px;
    background:rgba(255,211,42,.06);
    border:1px solid rgba(255,211,42,.25);
    border-radius:10px;padding:12px 16px;
    font-size:13px;color:var(--yellow);
    margin-bottom:20px;
  }

  /* STAT ROW */
  .stat-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(26,36,64,.5)}
  .stat-row:last-child{border-bottom:none}
  .stat-label{font-size:12px;color:var(--muted)}
  .stat-val{font-size:13px;font-weight:700}

  /* TYPE STAT */
  .type-block{margin-bottom:12px}
  .type-name{font-size:12px;font-weight:600;color:var(--text);margin-bottom:4px}
  .type-bar-wrap{display:flex;align-items:center;gap:8px}
  .type-bar{flex:1;height:6px;border-radius:3px;overflow:hidden;display:flex}
  .type-bar-w{background:var(--green)}
  .type-bar-l{background:var(--red)}
  .type-pct{font-size:11px;color:var(--muted)}

  /* DONUT */
  .chart-wrap{position:relative;height:200px;display:flex;align-items:center;justify-content:center}

  /* ECO EVENTS */
  .eco-item{
    background:rgba(255,71,87,.06);
    border:1px solid rgba(255,71,87,.2);
    border-radius:8px;padding:10px 14px;
    font-size:13px;color:#ffaab0;
    margin-bottom:8px;
  }
  .eco-item:last-child{margin-bottom:0}

  /* NEWS */
  .news-item{
    border-bottom:1px solid rgba(26,36,64,.5);
    padding:8px 0;font-size:12px;color:var(--muted);
    line-height:1.5;
  }
  .news-item:last-child{border-bottom:none}

  /* FOOTER */
  .footer{text-align:center;color:var(--muted2);font-size:11px;padding:24px;margin-top:8px}

  /* REFRESH countdown */
  #countdown{
    background:rgba(0,208,132,.06);
    border:1px solid rgba(0,208,132,.15);
    color:#4dab8a;
    border-radius:6px;padding:4px 10px;font-size:11px;
  }
</style>
</head>
<body>

<!-- NAVBAR -->
<nav class="navbar">
  <div class="nav-brand">
    <span>📈</span> StockBot Pro <span style="color:var(--muted);font-weight:400;font-size:13px">v5.2</span>
  </div>
  <div class="nav-right">
    <span><span class="live-dot"></span> En vivo</span>
    <span id="countdown" class="refresh-badge">Actualiza en 30s</span>
    <a href="/logout" class="logout">Salir</a>
  </div>
</nav>

<div class="page">

  <!-- ALTO IMPACTO BANNER -->
  {% if high_impact %}
  <div class="alert-banner">
    ⚠️ <strong>ALTO IMPACTO HOY:</strong>&nbsp;{{ eco_events|join(', ') }}
  </div>
  {% endif %}

  <!-- MÉTRICAS -->
  <div class="metrics">

    <!-- Régimen -->
    <div class="metric {% if regime=='BEAR' %}red{% elif regime=='BULL' %}green{% else %}yellow{% endif %}">
      <div class="metric-label">Régimen de Mercado</div>
      <div class="metric-value {% if regime=='BEAR' %}regime-bear{% elif regime=='BULL' %}regime-bull{% else %}regime-lateral{% endif %}">
        {{ regime }}
        {% if regime=='BEAR' %}<span class="badge badge-bear" style="font-size:12px;vertical-align:middle;margin-left:6px">{{ regime_str }}%</span>
        {% elif regime=='BULL' %}<span class="badge badge-bull" style="font-size:12px;vertical-align:middle;margin-left:6px">{{ regime_str }}%</span>
        {% else %}<span class="badge badge-lateral" style="font-size:12px;vertical-align:middle;margin-left:6px">{{ regime_str }}%</span>{% endif %}
      </div>
      <div class="metric-sub">SPY {{ "%+.1f"|format(spy_mom3m) }}% en 3m</div>
    </div>

    <!-- Fear & Greed -->
    <div class="metric {% if fear_greed < 45 %}red{% elif fear_greed > 55 %}green{% else %}yellow{% endif %}">
      <div class="metric-label">Fear &amp; Greed</div>
      <div class="metric-value" style="color:{{ fg_color }}">{{ fear_greed }}<span style="font-size:16px;font-weight:400">/100</span></div>
      <div class="fg-bar"><div class="fg-bar-fill" style="width:{{ fear_greed }}%;background:{{ fg_color }}"></div></div>
      <div class="metric-sub">{{ fg_label }}</div>
    </div>

    <!-- VIX -->
    <div class="metric {% if vix > 25 %}red{% elif vix < 18 %}green{% else %}yellow{% endif %}">
      <div class="metric-label">VIX</div>
      <div class="metric-value {% if vix > 25 %}regime-bear{% elif vix < 18 %}regime-bull{% else %}regime-lateral{% endif %}">{{ vix }}</div>
      <div class="metric-sub">{% if vix > 25 %}Alta volatilidad{% elif vix < 18 %}Volatilidad baja{% else %}Volatilidad moderada{% endif %}</div>
    </div>

    <!-- S&P 500 -->
    <div class="metric {% if sp500 < 0 %}red{% else %}green{% endif %}">
      <div class="metric-label">S&amp;P 500 (hoy)</div>
      <div class="metric-value {% if sp500 < 0 %}regime-bear{% else %}regime-bull{% endif %}">
        {{ "%+.2f"|format(sp500) }}%
      </div>
      <div class="metric-sub">SPY diario</div>
    </div>

    <!-- Precisión -->
    <div class="metric blue">
      <div class="metric-label">Precisión Global</div>
      <div class="metric-value" style="color:var(--blue)">{{ accuracy }}%</div>
      <div class="metric-sub">{{ wins }}✅ {{ losses }}❌ · {{ pending_ct }} pendientes</div>
    </div>

  </div>
  <!-- /métricas -->

  <!-- PREDICCIONES ACTIVAS -->
  <div class="section">
    <div class="section-title">⏳ Predicciones activas ({{ pending_ct }})</div>
    <div class="card">
      {% if pending %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Señal</th>
              <th>Tipo</th>
              <th>Entrada</th>
              <th>Objetivo</th>
              <th>Stop</th>
              <th>Confianza</th>
              <th>Progreso</th>
              <th>Sector</th>
              <th>Días</th>
            </tr>
          </thead>
          <tbody>
            {% for p in pending %}
            {% set days = ((now_ts - p.date|string|replace('T',' ')|replace('Z','')) if p.date else 0) %}
            <tr>
              <td><span class="ticker">{{ p.ticker }}</span></td>
              <td>
                {% if p.signal == 'COMPRAR' %}
                  <span class="badge badge-buy">📈 Comprar</span>
                {% else %}
                  <span class="badge badge-sell">📉 Vender</span>
                {% endif %}
              </td>
              <td>
                {% set st = p.get('signal_type','NORMAL') if p is mapping else p['signal_type'] if 'signal_type' in p else 'NORMAL' %}
                {% set st = p['signal_type'] if 'signal_type' in p else 'NORMAL' %}
                {% if st == 'PRE_EARNINGS' %}<span class="badge badge-earnings">Pre-Earn</span>
                {% elif st == 'SHORT_SQUEEZE' %}<span class="badge badge-squeeze">Squeeze</span>
                {% elif st == 'INSIDER_MASSIVE' %}<span class="badge badge-insider">Insider</span>
                {% else %}<span class="badge badge-normal">Normal</span>
                {% endif %}
              </td>
              <td class="price">${{ "%.2f"|format(p.entry|float) }}</td>
              <td class="price" style="color:var(--green)">${{ "%.2f"|format(p.target|float) }}</td>
              <td class="price" style="color:var(--red)">${{ "%.2f"|format(p.stop|float) }}</td>
              <td>
                <div class="conf-wrap">
                  <div class="conf-bar">
                    <div class="conf-fill" style="width:{{ p.confidence|int }}%;background:{% if p.confidence|int >= 94 %}var(--green){% elif p.confidence|int >= 88 %}var(--blue){% else %}var(--muted){% endif %}"></div>
                  </div>
                  <span style="font-size:12px;font-weight:700">{{ p.confidence|int }}%</span>
                </div>
              </td>
              <td>
                {% set entry = p.entry|float %}
                {% set target = p.target|float %}
                {% if entry > 0 and target > 0 %}
                  {% set total_move = ((target - entry) / entry * 100)|round(1) %}
                  <div class="pbar-wrap">
                    <div class="pbar"><div class="pbar-fill" style="width:40%"></div></div>
                    <span class="pbar-pct pct-pos">+{{ total_move }}% obj</span>
                  </div>
                {% else %}—{% endif %}
              </td>
              <td style="color:var(--muted);font-size:12px">{{ p.sector or '—' }}</td>
              <td>
                {% if p.date %}
                  <span data-date="{{ p.date }}" class="days-counter" style="font-size:12px;color:var(--muted)">—</span>
                {% else %}—{% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty"><span>🔍</span>No hay predicciones activas ahora mismo</div>
      {% endif %}
    </div>
  </div>

  <!-- GRÁFICA + RESULTADOS RECIENTES -->
  <div class="grid2">

    <!-- Donut chart -->
    <div class="card">
      <div class="card-title">Distribución Win / Loss</div>
      {% if wins + losses > 0 %}
      <div class="chart-wrap">
        <canvas id="donutChart" width="200" height="200"></canvas>
      </div>
      <div style="display:flex;gap:20px;justify-content:center;margin-top:12px;font-size:13px">
        <span style="color:var(--green)">✅ {{ wins }} wins</span>
        <span style="color:var(--red)">❌ {{ losses }} losses</span>
        <span style="color:var(--blue)">📊 {{ accuracy }}% precisión</span>
      </div>
      {% else %}
      <div class="empty"><span>📊</span>Sin datos resueltos aún</div>
      {% endif %}

      <!-- Stats adicionales -->
      <div style="margin-top:20px;border-top:1px solid var(--border);padding-top:16px">
        <div class="stat-row">
          <span class="stat-label">Total predicciones</span>
          <span class="stat-val">{{ total }}</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Pendientes</span>
          <span class="stat-val" style="color:var(--yellow)">{{ pending_ct }}</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Reglas aprendidas</span>
          <span class="stat-val" style="color:var(--purple)">{{ rules }}</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Última actualización</span>
          <span class="stat-val" style="color:var(--muted);font-size:11px">{{ updated }}</span>
        </div>
      </div>
    </div>

    <!-- Resultados recientes -->
    <div class="card">
      <div class="card-title">Resultados recientes</div>
      {% if recent %}
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Ticker</th>
              <th>Señal</th>
              <th>Resultado</th>
              <th>Entrada → Salida</th>
              <th>P/L</th>
            </tr>
          </thead>
          <tbody>
            {% for p in recent %}
            <tr>
              <td><span class="ticker">{{ p.ticker }}</span></td>
              <td>
                {% if p.signal == 'COMPRAR' %}
                <span class="badge badge-buy">📈</span>
                {% else %}
                <span class="badge badge-sell">📉</span>
                {% endif %}
              </td>
              <td>
                {% if p.result == 'win' %}
                <span class="badge badge-win">✅ WIN</span>
                {% else %}
                <span class="badge badge-loss">❌ LOSS</span>
                {% endif %}
              </td>
              <td class="price">
                ${{ "%.2f"|format(p.entry|float) }}
                {% if p.exit_price %}→ ${{ "%.2f"|format(p.exit_price|float) }}{% endif %}
              </td>
              <td>
                {% if p.exit_price and p.entry and p.entry|float > 0 %}
                  {% set pl = ((p.exit_price|float - p.entry|float) / p.entry|float * 100)|round(1) %}
                  {% if p.signal == 'VENDER' %}{% set pl = pl * -1 %}{% endif %}
                  <span class="{% if pl >= 0 %}pct-pos{% else %}pct-neg{% endif %}" style="font-weight:700;font-size:12px">
                    {{ "%+.1f"|format(pl) }}%
                  </span>
                {% else %}—{% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="empty"><span>📋</span>Sin resultados resueltos</div>
      {% endif %}
    </div>

  </div>
  <!-- /grid2 -->

  <!-- POR TIPO + POR SECTOR + EVENTOS ECO -->
  <div class="grid3">

    <!-- Por tipo de señal -->
    <div class="card">
      <div class="card-title">Precisión por tipo de señal</div>
      {% if by_type %}
        {% for t, s in by_type.items() %}
        <div class="type-block">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            {% if t == 'PRE_EARNINGS' %}<span class="badge badge-earnings">Pre-Earnings</span>
            {% elif t == 'SHORT_SQUEEZE' %}<span class="badge badge-squeeze">Squeeze</span>
            {% elif t == 'INSIDER_MASSIVE' %}<span class="badge badge-insider">Insider</span>
            {% else %}<span class="badge badge-normal">Normal</span>
            {% endif %}
            <span style="font-size:11px;color:var(--muted)">{{ s.w + s.l }} ops</span>
          </div>
          <div class="type-bar-wrap">
            <div class="type-bar" style="background:var(--border)">
              {% set total = s.w + s.l %}
              {% if total > 0 %}
              <div class="type-bar-w" style="width:{{ (s.w / total * 100)|int }}%"></div>
              <div class="type-bar-l" style="width:{{ (s.l / total * 100)|int }}%"></div>
              {% endif %}
            </div>
            <span class="type-pct" style="color:{% if total > 0 and (s.w/total*100) >= 60 %}var(--green){% elif total > 0 and (s.w/total*100) < 45 %}var(--red){% else %}var(--yellow){% endif %}">
              {% if s.w + s.l > 0 %}{{ (s.w / (s.w + s.l) * 100)|int }}%{% else %}—{% endif %}
            </span>
          </div>
        </div>
        {% endfor %}
      {% else %}
      <div class="empty" style="padding:20px"><span>📊</span>Sin datos</div>
      {% endif %}
    </div>

    <!-- Por sector -->
    <div class="card">
      <div class="card-title">Top sectores</div>
      {% if by_sector %}
        {% for s, v in by_sector|dictsort(by='value', reverse=True) %}
        {% if loop.index <= 8 %}
        <div class="type-block">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:12px;color:var(--text)">{{ s }}</span>
            <span style="font-size:11px;color:var(--muted)">{{ v.w + v.l }} ops</span>
          </div>
          <div class="type-bar-wrap">
            <div class="type-bar" style="background:var(--border)">
              {% set total = v.w + v.l %}
              {% if total > 0 %}
              <div class="type-bar-w" style="width:{{ (v.w / total * 100)|int }}%"></div>
              <div class="type-bar-l" style="width:{{ (v.l / total * 100)|int }}%"></div>
              {% endif %}
            </div>
            <span class="type-pct" style="color:{% if total > 0 and (v.w/total*100) >= 60 %}var(--green){% elif total > 0 and (v.w/total*100) < 45 %}var(--red){% else %}var(--yellow){% endif %}">
              {% if total > 0 %}{{ (v.w / total * 100)|int }}%{% else %}—{% endif %}
            </span>
          </div>
        </div>
        {% endif %}
        {% endfor %}
      {% else %}
      <div class="empty" style="padding:20px"><span>🏭</span>Sin datos</div>
      {% endif %}
    </div>

    <!-- Eventos macro / noticias -->
    <div class="card">
      <div class="card-title">📡 Contexto macro</div>
      {% if high_impact and eco_events %}
      <div style="margin-bottom:12px">
        <div style="font-size:11px;color:var(--red);font-weight:700;margin-bottom:8px;letter-spacing:.5px">⚠️ EVENTOS ALTO IMPACTO HOY</div>
        {% for ev in eco_events %}
        <div class="eco-item">{{ ev }}</div>
        {% endfor %}
      </div>
      {% endif %}
      {% if macro_news %}
      <div style="font-size:11px;color:var(--muted);font-weight:700;margin-bottom:8px;letter-spacing:.5px">NOTICIAS MACRO</div>
      {% for n in macro_news[:5] %}
      <div class="news-item">{{ n }}</div>
      {% endfor %}
      {% else %}
      <div class="empty" style="padding:20px"><span>📰</span>Sin noticias disponibles</div>
      {% endif %}
    </div>

  </div>
  <!-- /grid3 -->

  <div class="footer">
    StockBot Pro v5.2 · Dashboard © 2026 · Actualización automática cada 30s
  </div>

</div>
<!-- /page -->

<script>
// ─── Días activos ───────────────────────────────────────────────────
document.querySelectorAll('.days-counter').forEach(el => {
  const d = el.dataset.date;
  if (!d) return;
  try {
    const then = new Date(d);
    const diff = Math.floor((Date.now() - then) / 86400000);
    el.textContent = diff === 0 ? 'Hoy' : diff + 'd';
  } catch(e){}
});

// ─── Donut chart ───────────────────────────────────────────────────
{% if wins + losses > 0 %}
const ctx = document.getElementById('donutChart').getContext('2d');
new Chart(ctx, {
  type: 'doughnut',
  data: {
    labels: ['Wins', 'Losses'],
    datasets: [{
      data: [{{ wins }}, {{ losses }}],
      backgroundColor: ['rgba(0,208,132,.8)', 'rgba(255,71,87,.8)'],
      borderColor: ['#00d084', '#ff4757'],
      borderWidth: 2,
      hoverOffset: 6,
    }]
  },
  options: {
    cutout: '72%',
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          label: ctx => ` ${ctx.label}: ${ctx.parsed} (${Math.round(ctx.parsed/({{ wins }}+{{ losses }})*100)}%)`
        }
      }
    },
    animation: { animateScale: true }
  }
});
{% endif %}

// ─── Countdown + auto-refresh ──────────────────────────────────────
let secs = 30;
const cd = document.getElementById('countdown');
function tick() {
  secs--;
  if (secs <= 0) {
    cd.textContent = 'Actualizando...';
    window.location.reload();
    return;
  }
  cd.textContent = 'Actualiza en ' + secs + 's';
  setTimeout(tick, 1000);
}
setTimeout(tick, 1000);
</script>
</body>
</html>"""


# ─── Rutas ────────────────────────────────────────────────────────────────────

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


# ─── Start (hilo) ─────────────────────────────────────────────────────────────

def start_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    start_web()
