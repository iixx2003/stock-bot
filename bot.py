"""
StockBot Pro v4
───────────────────────────────────────────────────────────────────────
Cambios respecto a v3:
  - Máximo 3 alertas totales al día (Excepcional no cuenta)
  - Confianza mínima subida de 82% a 85%
  - Prompt IA más estricto: exige convergencia real entre capas
  - La IA debe ser muy selectiva — mejor NO_SIGNAL que señal mediocre
  - Boost institucional aplicado ANTES del formateo (confianza coherente)
  - Score técnico mínimo subido de 5 a 6

Arquitectura de 6 capas:
  1. quick_scan()       — screeners Yahoo, sin IA, detecta movimiento real
  2. get_market_data()  — técnico completo: diario, semanal, mensual
  3. get_fundamentals() — P/E, short, earnings, insiders (1 petición)
  4. get_sentiment()    — noticias NewsAPI + RSS Yahoo + ETF sectorial
  5. get_inst_signal()  — actividad institucional desde volumen/OBV
  6. call_ai()          — Claude decide con convergencia real o NO_SIGNAL

Flujo automático:  quick_scan → analyze_ticker(force=False) → send_alert
Flujo manual:      !analizar TICKER → analyze_ticker(force=True) → send_solicitud
"""

import os, time, json, random, schedule, requests, feedparser, anthropic
from datetime import datetime, timedelta
import pytz

# ═══════════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════════

DISCORD_TOKEN            = os.environ.get("DISCORD_TOKEN")
DISCORD_ALERTS_ID        = os.environ.get("DISCORD_CHANNEL_ID")
DISCORD_LOG_ID           = "1478113089093374034"
DISCORD_ACIERTOS_ID      = "1478461406251847812"
DISCORD_SOLICITUD_ID     = "1478470481693900841"
DISCORD_INSTRUCCIONES_ID = "1478470715509440748"
DISCORD_STATUS_ID        = "1478471477568475248"
ANTHROPIC_API_KEY        = os.environ.get("ANTHROPIC_API_KEY")
NEWS_API_KEY             = os.environ.get("NEWS_API_KEY")

SPAIN_TZ = pytz.timezone("Europe/Madrid")

# Umbrales de confianza
CONF_NORMAL      = 85   # mínimo para enviar alerta automática (subido de 82)
CONF_FUERTE      = 88   # nivel fuerte
CONF_EXCEPCIONAL = 94   # excepcional — no cuenta para el límite diario total

# Límites diarios
MAX_ALERTAS_DIA  = 3    # máximo total (Excepcional no cuenta)
MAX_VENTAS_DIA   = 1    # máximo ventas al día
MAX_AI_POR_CICLO = 4    # máximo llamadas IA por ciclo de 5 min

# Score técnico mínimo para pasar al análisis profundo (subido de 5 a 6)
SCORE_MINIMO = 6

# Archivos de persistencia (Railway volume en /app/data)
PREDICTIONS_FILE = "/app/data/predictions.json"
WATCHSTATE_FILE  = "/app/data/watchstate.json"

# ═══════════════════════════════════════════════════════════════════════
# UNIVERSO DE ACCIONES
# ═══════════════════════════════════════════════════════════════════════

SP500 = [
    "MMM","ABT","ABBV","ACN","ADBE","AMD","AFL","GOOGL","GOOG","MO","AMZN","AAL","AEP","AXP",
    "AIG","AMT","AWK","AMGN","APH","ADI","AAPL","AMAT","ANET","T","ADSK","AZO","BKR","BAC",
    "BK","BAX","BIIB","BLK","BX","BA","BMY","AVGO","CDNS","COF","KMX","CCL","CAT","CBRE",
    "CNC","SCHW","CHTR","CVX","CMG","CB","CI","CTAS","CSCO","C","CLX","CME","KO","CL","CMCSA",
    "COP","STZ","CPRT","GLW","COST","CCI","CSX","CMI","CVS","DHI","DHR","DE","DAL","DVN",
    "DXCM","DLR","DFS","DG","DLTR","D","DOV","DOW","LLY","ETN","EBAY","ECL","EW","EA","ELV",
    "EMR","ENPH","EOG","EFX","EQIX","EL","ETSY","EXC","EXPE","XOM","FDS","FAST","FRT","FDX",
    "FIS","FITB","FSLR","FI","FLT","F","FTNT","FCX","GE","GD","GIS","GM","GILD","GS","HAL",
    "HIG","HCA","HD","HON","HRL","HPQ","HUBB","HUM","IBM","IDXX","ITW","INTC","ICE","INTU",
    "ISRG","JNJ","JPM","KDP","KEY","KMB","KMI","KLAC","KHC","KR","LHX","LH","LRCX","LIN",
    "LMT","LOW","LULU","MTB","MPC","MAR","MMC","MAS","MA","MCD","MCK","MDT","MRK","META",
    "MET","MGM","MCHP","MU","MSFT","MRNA","MDLZ","MNST","MCO","MS","MSI","NDAQ","NTAP",
    "NFLX","NEE","NKE","NSC","NTRS","NOC","NCLH","NVDA","NXPI","ORLY","OXY","ODFL","ON",
    "OKE","ORCL","PCAR","PLTR","PH","PAYX","PYPL","PEP","PFE","PM","PSX","PNC","PPG","PPL",
    "PG","PGR","PLD","PRU","PEG","PSA","QCOM","RTX","O","REGN","RF","RMD","ROK","ROP","ROST",
    "RCL","SPGI","CRM","SLB","SRE","NOW","SHW","SPG","SJM","SNA","SO","SWK","SBUX","STT",
    "STLD","SYK","SMCI","SNPS","SYY","TMUS","TROW","TTWO","TGT","TEL","TSLA","TXN","TMO",
    "TJX","TSCO","TT","TRV","TFC","TSN","USB","UBER","UNP","UAL","UPS","URI","UNH","VLO",
    "VTR","VRSN","VRSK","VZ","VRTX","VMC","WAB","WBA","WMT","DIS","WM","WAT","WEC","WFC",
    "WELL","WDC","WY","WHR","WMB","WYNN","XEL","YUM","ZBH","ZTS",
]
NASDAQ100 = [
    "ADBE","AMD","ABNB","GOOGL","AMZN","AMGN","AAPL","ARM","ASML","ADSK","BKR","BIIB","BKNG",
    "AVGO","CDNS","CHTR","CTAS","CSCO","CMCSA","CPRT","COST","CRWD","CSX","DDOG","DXCM","DLTR",
    "EA","ENPH","EXC","FAST","FTNT","GILD","HON","IDXX","INTC","INTU","ISRG","KDP","KLAC",
    "LRCX","LULU","MAR","MRVL","MELI","META","MCHP","MU","MSFT","MRNA","MDLZ","MDB","MNST",
    "NFLX","NVDA","NXPI","ORLY","ODFL","ON","PCAR","PANW","PAYX","PYPL","QCOM","REGN","ROP",
    "ROST","SBUX","SNPS","TTWO","TMUS","TSLA","TXN","VRSK","VRTX","WBA","WDAY","XEL","ZS","ZM",
]
EXTRAS = [
    "SOFI","RIVN","COIN","MSTR","HOOD","RBLX","SNAP","LYFT","SHOP","SQ","ROKU","SPOT","NET",
    "PANW","SMCI","GME","MARA","RIOT","CLSK","LCID","NKLA","AFRM","UPST","DKNG","CHWY","BYND",
    "NIO","XPEV","LI","GRAB","SEA","BIDU","RKT","RELY","STNE","IREN","PINS","CCL","NCLH",
    "RCL","DAL","AAL","UAL","ASTS","GTLB","PLTR","DKNG",
]
UNIVERSE = list(set(SP500 + NASDAQ100 + EXTRAS))

SECTOR_ETFS = {
    "Technology": "XLK", "Healthcare": "XLV", "Financials": "XLF",
    "Energy": "XLE", "Consumer Cyclical": "XLY", "Industrials": "XLI",
    "Communication Services": "XLC", "Consumer Defensive": "XLP",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
}

# ═══════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════════════

predictions   = []    # predicciones guardadas en disco
watch_signals = {}    # {ticker: {"last_analyzed": ISO, "developing": bool}}
market_context = {    # actualizado al arrancar y cada día a las 09:00
    "fear_greed": 50, "sp500_change": 0.0, "vix": 15.0,
    "macro_news": [], "economic_events": [], "updated_at": None,
}
status_msg_id     = None   # ID del mensaje único en #status
last_cmd_msg_id   = None   # último ID visto en #solicitud-en-concreto
processed_cmd_ids = set()  # IDs ya procesados esta sesión

# ═══════════════════════════════════════════════════════════════════════
# PERSISTENCIA EN DISCO
# ═══════════════════════════════════════════════════════════════════════

def load_state():
    """Carga predictions y watch_signals desde disco al arrancar."""
    global predictions, watch_signals
    os.makedirs("/app/data", exist_ok=True)

    try:
        if os.path.exists(PREDICTIONS_FILE):
            with open(PREDICTIONS_FILE) as f:
                predictions = json.load(f)
            print(f"  Estado cargado: {len(predictions)} predicciones")
        else:
            predictions = []
            print("  Sin predictions.json previo — empezando desde cero")
    except Exception as e:
        predictions = []
        print(f"  ERROR cargando predictions.json: {e}")

    try:
        if os.path.exists(WATCHSTATE_FILE):
            with open(WATCHSTATE_FILE) as f:
                watch_signals = json.load(f)
        else:
            watch_signals = {}
    except Exception as e:
        watch_signals = {}
        print(f"  ERROR cargando watchstate.json: {e}")


def save_state():
    """Guarda predictions y watch_signals en disco."""
    try:
        with open(PREDICTIONS_FILE, "w") as f:
            json.dump(predictions, f, indent=2)
        with open(WATCHSTATE_FILE, "w") as f:
            json.dump(watch_signals, f, indent=2)
    except Exception as e:
        print(f"  ERROR guardando estado: {e}")


def save_prediction(ticker, signal, entry, stop, conf):
    """Registra una nueva predicción y guarda en disco."""
    predictions.append({
        "ticker":     ticker,
        "signal":     signal,
        "entry":      round(entry, 2),
        "target":     round(entry * (1.15 if signal == "COMPRAR" else 0.85), 2),
        "stop":       round(stop, 2),
        "confidence": conf,
        "date":       datetime.now(SPAIN_TZ).isoformat(),
        "result":     "pending",
        "exit_price": None,
    })
    save_state()

# ═══════════════════════════════════════════════════════════════════════
# LÍMITES DIARIOS
# Toda la lógica se basa en predictions guardadas en disco.
# Así los límites sobreviven reinicios sin necesidad de contadores en memoria.
# ═══════════════════════════════════════════════════════════════════════

def _preds_today():
    """Predicciones registradas hoy (zona horaria España)."""
    today = datetime.now(SPAIN_TZ).date()
    result = []
    for p in predictions:
        try:
            d = datetime.fromisoformat(p["date"])
            if d.tzinfo is not None:
                d = d.astimezone(SPAIN_TZ)
            if d.date() == today:
                result.append(p)
        except Exception:
            pass
    return result


def already_alerted_today(ticker):
    """True si ya se envió alerta de este ticker hoy."""
    return any(p["ticker"] == ticker for p in _preds_today())


def alertas_hoy():
    """
    Alertas normales+fuertes enviadas hoy.
    Las Excepcionales (94%+) no cuentan para este límite.
    """
    return sum(1 for p in _preds_today() if p["confidence"] < CONF_EXCEPCIONAL)


def ventas_hoy():
    """Alertas de venta enviadas hoy."""
    return sum(1 for p in _preds_today() if p["signal"] == "VENDER")


def fuertes_hoy():
    """Alertas Fuerte (88-93%) enviadas hoy — para logging."""
    return sum(1 for p in _preds_today() if CONF_FUERTE <= p["confidence"] < CONF_EXCEPCIONAL)


def puede_enviar_alerta(signal, conf):
    """
    Comprueba si se puede enviar una alerta dadas las restricciones diarias.
    Devuelve (True, None) si puede, o (False, "motivo") si no puede.
    """
    # Excepcional siempre pasa
    if conf >= CONF_EXCEPCIONAL:
        return True, None

    # Límite total diario
    if alertas_hoy() >= MAX_ALERTAS_DIA:
        return False, f"límite diario ({MAX_ALERTAS_DIA}) alcanzado"

    # Límite de ventas
    if signal == "VENDER" and ventas_hoy() >= MAX_VENTAS_DIA:
        return False, "límite de ventas diario alcanzado"

    return True, None

# ═══════════════════════════════════════════════════════════════════════
# DISCORD — ENVÍO Y GESTIÓN DE MENSAJES
# ═══════════════════════════════════════════════════════════════════════

def _discord_post(channel_id, text):
    """Envía un mensaje a un canal Discord. Trunca si supera 1900 chars."""
    if len(text) > 1900:
        text = text[:1897] + "..."
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            json={"content": text},
            headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            print(f"  Discord POST {r.status_code}: {r.text[:120]}")
    except Exception as e:
        print(f"  Discord POST excepción: {e}")


def send_alert(text):      _discord_post(DISCORD_ALERTS_ID, text)
def send_log(text):        _discord_post(DISCORD_LOG_ID, text)
def send_acierto(text):    _discord_post(DISCORD_ACIERTOS_ID, text)
def send_solicitud(text):  _discord_post(DISCORD_SOLICITUD_ID, text)


def update_status(text):
    """
    Edita el mensaje único de #status.
    Si no existe en memoria, busca el último del bot en el canal.
    Si no hay ninguno, crea uno nuevo.
    Nunca crea duplicados.
    """
    global status_msg_id
    auth = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    try:
        if not status_msg_id:
            r = requests.get(
                f"https://discord.com/api/v10/channels/{DISCORD_STATUS_ID}/messages?limit=20",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=10,
            )
            if r.status_code == 200:
                for m in r.json():
                    if m.get("author", {}).get("bot"):
                        status_msg_id = m["id"]
                        print(f"  Status: reutilizando mensaje {status_msg_id}")
                        break

        if status_msg_id:
            r = requests.patch(
                f"https://discord.com/api/v10/channels/{DISCORD_STATUS_ID}/messages/{status_msg_id}",
                json={"content": text}, headers=auth, timeout=10,
            )
            if r.status_code in (200, 201):
                return
            print(f"  Status PATCH falló ({r.status_code}) — creando nuevo")
            status_msg_id = None

        r = requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_STATUS_ID}/messages",
            json={"content": text}, headers=auth, timeout=10,
        )
        if r.status_code in (200, 201):
            status_msg_id = r.json().get("id")
            print(f"  Status: nuevo mensaje creado {status_msg_id}")
    except Exception as e:
        print(f"  Status error: {e}")


def post_instrucciones():
    """
    Borra mensajes anteriores del bot en #instrucciones y publica las nuevas.
    Solo se llama al arrancar.
    """
    try:
        r = requests.get(
            f"https://discord.com/api/v10/channels/{DISCORD_INSTRUCCIONES_ID}/messages?limit=20",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=10,
        )
        if r.status_code == 200:
            for m in r.json():
                if m.get("author", {}).get("bot"):
                    requests.delete(
                        f"https://discord.com/api/v10/channels/{DISCORD_INSTRUCCIONES_ID}/messages/{m['id']}",
                        headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=5,
                    )
                    time.sleep(0.5)
    except Exception as e:
        print(f"  Error borrando instrucciones anteriores: {e}")

    _discord_post(DISCORD_INSTRUCCIONES_ID, """━━━━━━━━━━━━━━━━━━━━━━━━━━━
📖  **CÓMO FUNCIONA STOCKBOT PRO**
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍  **Análisis automático**
Vigila +1.200 acciones cada 5 min.
Solo envía cuando hay convergencia real entre capas técnica, fundamental y macro.
Máximo 3 alertas al día.

⚡  **Niveles de confianza**
🟢  Normal 85-87% — señal sólida
🔥  Fuerte 88-93% — alta convicción
⚡  Excepcional 94%+ — sin límite diario

🎯  **Análisis bajo demanda**
Escribe en **#solicitud-en-concreto**:
`!analizar NVDA`
Respuesta en menos de 30 segundos.""")

    time.sleep(1)

    _discord_post(DISCORD_INSTRUCCIONES_ID, """━━━━━━━━━━━━━━━━━━━━━━━━━━━
📡  **CANALES**
**#stock-alerts** — alertas automáticas (máx. 3/día)
**#aciertos-bot** — resumen cada domingo 10:00
**#solicitud-en-concreto** — análisis bajo demanda
**#log-bot** — actividad interna
**#status** — estado del bot en tiempo real
━━━━━━━━━━━━━━━━━━━━━━━━━━━""")

# ═══════════════════════════════════════════════════════════════════════
# CONTEXTO MACRO
# ═══════════════════════════════════════════════════════════════════════

def _fg_label(fg):
    if fg < 20: return "PÁNICO EXTREMO"
    if fg < 40: return "Miedo"
    if fg < 60: return "Neutral"
    if fg < 80: return "Codicia"
    return "EUFORIA"


def update_market_context():
    """
    Actualiza Fear&Greed, S&P500, VIX y noticias macro.
    Se llama al arrancar y por schedule cada día a las 09:00.
    """
    global market_context
    print("  Actualizando contexto macro...")

    fg = 50
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        if r.status_code == 200:
            fg = int(r.json()["data"][0]["value"])
    except Exception as e:
        print(f"    Fear&Greed error: {e}")

    sp500_change, vix = 0.0, 15.0
    for symbol, key in [("SPY", "sp500"), ("^VIX", "vix")]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if r.status_code == 200:
                closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(closes) >= 2:
                    if key == "sp500":
                        sp500_change = round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2)
                    else:
                        vix = round(closes[-1], 1)
            time.sleep(0.5)
        except Exception as e:
            print(f"    {symbol} error: {e}")

    macro_news, econ_events = [], []
    if NEWS_API_KEY:
        try:
            r = requests.get(
                f"https://newsapi.org/v2/top-headlines?category=business&language=en&pageSize=6&apiKey={NEWS_API_KEY}",
                timeout=10,
            )
            if r.status_code == 200:
                macro_news = [a.get("title", "") for a in r.json().get("articles", [])[:6]]
        except: pass
        try:
            r = requests.get(
                f"https://newsapi.org/v2/everything?q=Federal+Reserve+OR+CPI+OR+inflation&language=en&sortBy=publishedAt&pageSize=4&apiKey={NEWS_API_KEY}",
                timeout=10,
            )
            if r.status_code == 200:
                econ_events = [a.get("title", "") for a in r.json().get("articles", [])[:4]]
        except: pass

    market_context = {
        "fear_greed":      fg,
        "sp500_change":    sp500_change,
        "vix":             vix,
        "macro_news":      macro_news,
        "economic_events": econ_events,
        "updated_at":      datetime.now(SPAIN_TZ).strftime("%H:%M"),
    }

    fg_str = _fg_label(fg)
    print(f"  Fear&Greed: {fg} ({fg_str}) | S&P500: {sp500_change:+.2f}% | VIX: {vix}")
    send_log(f"📊 Macro — Fear&Greed: {fg} ({fg_str}) | S&P500: {sp500_change:+.2f}% | VIX: {vix}")

# ═══════════════════════════════════════════════════════════════════════
# CAPA 1 — QUICK SCAN (sin IA, sin coste)
# ═══════════════════════════════════════════════════════════════════════

def quick_scan():
    """
    Escanea screeners de Yahoo Finance buscando acciones con movimiento
    real de precio y volumen anómalo. Sin IA, sin coste.
    Devuelve lista de candidatos ordenados por urgencia, sin duplicados.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://finance.yahoo.com",
    }
    seen       = set()
    candidates = []

    for url in [
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=50",
    ]:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                continue
            quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
            for q in quotes:
                sym       = q.get("symbol", "")
                price     = q.get("regularMarketPrice", 0)
                vol       = q.get("regularMarketVolume", 0)
                avg_vol   = max(q.get("averageDailyVolume3Month", 1), 1)
                change    = abs(q.get("regularMarketChangePercent", 0))
                vol_ratio = vol / avg_vol

                if not sym or "." in sym or len(sym) > 5:  continue
                if price < 5 or vol < 500_000:              continue
                if sym in seen:                             continue
                if already_alerted_today(sym):              continue

                score = 0
                if change > 8:        score += 3
                elif change > 5:      score += 2
                elif change > 3:      score += 1
                if vol_ratio > 3:     score += 3
                elif vol_ratio > 2:   score += 2
                elif vol_ratio > 1.5: score += 1

                if score >= 2:
                    seen.add(sym)
                    candidates.append({
                        "ticker":    sym,
                        "name":      q.get("longName", sym),
                        "sector":    q.get("sector", "Unknown"),
                        "price":     price,
                        "change":    q.get("regularMarketChangePercent", 0),
                        "vol_ratio": round(vol_ratio, 2),
                        "score":     score,
                    })
            time.sleep(0.5)
        except Exception as e:
            print(f"  Quick scan error: {e}")

    developing = [
        {"ticker": t, "name": t, "sector": "Unknown", "price": 0, "change": 0, "vol_ratio": 0, "score": 1}
        for t, s in watch_signals.items()
        if s.get("developing") and not already_alerted_today(t) and t not in seen
    ]

    all_candidates = sorted(candidates, key=lambda x: x["score"], reverse=True) + developing
    print(f"  Quick scan: {len(candidates)} urgentes + {len(developing)} en desarrollo")
    return all_candidates[:20]

# ═══════════════════════════════════════════════════════════════════════
# CAPA 2 — DATOS DE MERCADO (Yahoo Finance, 3 timeframes)
# ═══════════════════════════════════════════════════════════════════════

def get_market_data(ticker):
    """
    Datos técnicos completos para un ticker.
    3 peticiones a Yahoo: diario (1y), semanal (1y), mensual (3y).
    Devuelve dict con todos los indicadores, o None si falla.
    """
    try:
        ua = random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
        ])
        hdrs = {
            "User-Agent": ua,
            "Accept":     "application/json",
            "Referer":    f"https://finance.yahoo.com/quote/{ticker}/",
        }
        host = random.choice(["query1", "query2"])
        s    = requests.Session()
        s.get(f"https://finance.yahoo.com/quote/{ticker}/", headers=hdrs, timeout=8)
        time.sleep(0.8)

        # ── Diario (1 año) ───────────────────────────────────────────────
        r = s.get(
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y",
            headers=hdrs, timeout=15,
        )
        if r.status_code != 200:
            print(f"    {ticker}: Yahoo HTTP {r.status_code}")
            return None

        chart = r.json().get("chart", {}).get("result", [])
        if not chart:
            print(f"    {ticker}: sin datos en Yahoo")
            return None

        res     = chart[0]
        meta    = res.get("meta", {})
        q       = res["indicators"]["quote"][0]
        closes  = [c for c in q.get("close",  []) if c is not None]
        volumes = [v for v in q.get("volume", []) if v is not None]
        highs   = [h for h in q.get("high",   []) if h is not None]
        lows    = [l for l in q.get("low",    []) if l is not None]

        if len(closes) < 50:
            return None

        price      = closes[-1]
        change_pct = ((price - closes[-2]) / closes[-2]) * 100

        # Medias móviles
        sma20  = sum(closes[-20:]) / 20
        sma50  = sum(closes[-50:]) / 50
        sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None

        # EMA 12 y 26 para MACD
        ema12 = ema26 = closes[-1]
        for i in range(min(26, len(closes))):
            ema12 = closes[-(i+1)] * (2/13) + ema12 * (11/13)
            ema26 = closes[-(i+1)] * (2/27) + ema26 * (25/27)
        macd         = ema12 - ema26
        macd_signal  = macd * 0.9
        macd_bullish = macd > macd_signal

        # RSI 14
        gains, losses_list = [], []
        for i in range(1, 15):
            d = closes[-i] - closes[-i-1]
            (gains if d >= 0 else losses_list).append(abs(d))
        avg_gain = sum(gains) / 14       if gains       else 0
        avg_loss = sum(losses_list) / 14 if losses_list else 0.001
        rsi      = 100 - (100 / (1 + avg_gain / avg_loss))

        rsi_zone = "neutral"
        if rsi < 25:   rsi_zone = "oversold_extreme"
        elif rsi < 32: rsi_zone = "oversold"
        elif rsi > 75: rsi_zone = "overbought_extreme"
        elif rsi > 68: rsi_zone = "overbought"

        # Estocástico
        low14   = min(lows[-14:])  if len(lows)  >= 14 else min(lows)
        high14  = max(highs[-14:]) if len(highs) >= 14 else max(highs)
        stoch_k = ((price - low14) / (high14 - low14) * 100) if high14 != low14 else 50

        # Volumen y OBV
        avg_vol20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
        vol_ratio = volumes[-1] / avg_vol20  if avg_vol20 > 0    else 1
        obv = sum(
            volumes[-i] if closes[-i] > closes[-i-1] else -volumes[-i]
            for i in range(1, min(20, len(closes)))
        )
        obv_trend = "ACUMULACION" if obv > 0 else "DISTRIBUCION"

        # VWAP (últimos 5 días)
        vwap = sum(closes[-5:]) / 5

        # ATR (14 días)
        atr_vals = [
            max(highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i-1]),
                abs(lows[-i]  - closes[-i-1]))
            for i in range(1, min(15, len(closes)))
            if highs and lows
        ]
        atr = sum(atr_vals) / len(atr_vals) if atr_vals else price * 0.02

        # Fibonacci 52 semanas
        h52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        l52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
        rng = h52 - l52
        fib236 = round(h52 - rng * 0.236, 2)
        fib382 = round(h52 - rng * 0.382, 2)
        fib500 = round(h52 - rng * 0.500, 2)
        fib618 = round(h52 - rng * 0.618, 2)

        # Soporte y resistencia (últimos 20 días)
        rh = max(highs[-20:]) if len(highs) >= 20 else price
        rl = min(lows[-20:])  if len(lows)  >= 20 else price
        support_touches = sum(1 for l in lows[-60:] if abs(l - rl) / rl < 0.02) if len(lows) >= 60 else 0

        # Momentum
        mom1m = ((price - closes[-22]) / closes[-22] * 100) if len(closes) >= 22 else 0
        mom3m = ((price - closes[-66]) / closes[-66] * 100) if len(closes) >= 66 else 0

        # Estructura de precio (últimos 10 días)
        rh10 = highs[-10:] if len(highs) >= 10 else highs
        rl10 = lows[-10:]  if len(lows)  >= 10 else lows
        hh = all(rh10[i] >= rh10[i-1] for i in range(1, len(rh10)))
        hl = all(rl10[i] >= rl10[i-1] for i in range(1, len(rl10)))
        lh = all(rh10[i] <= rh10[i-1] for i in range(1, len(rh10)))
        ll = all(rl10[i] <= rl10[i-1] for i in range(1, len(rl10)))
        if hh and hl:   structure = "TENDENCIA ALCISTA CLARA"
        elif lh and ll: structure = "TENDENCIA BAJISTA CLARA"
        else:           structure = "LATERAL / CONSOLIDACION"

        # Score técnico — basado en combinaciones con sentido
        tech_score = 0
        if rsi_zone == "oversold_extreme":    tech_score += 4
        elif rsi_zone == "oversold":          tech_score += 2
        elif rsi_zone == "overbought_extreme":tech_score += 4
        elif rsi_zone == "overbought":        tech_score += 2
        if vol_ratio > 3:      tech_score += 3
        elif vol_ratio > 2:    tech_score += 2
        elif vol_ratio > 1.5:  tech_score += 1
        if abs(change_pct) > 8:   tech_score += 3
        elif abs(change_pct) > 5: tech_score += 2
        elif abs(change_pct) > 3: tech_score += 1
        if macd_bullish and change_pct > 0:              tech_score += 2
        if stoch_k < 20 or stoch_k > 80:                 tech_score += 1
        if obv_trend == "ACUMULACION" and change_pct > 0: tech_score += 2
        if abs(mom1m) > 15:  tech_score += 2
        elif abs(mom1m) > 8: tech_score += 1
        if support_touches >= 3: tech_score += 2

        # ── Semanal ──────────────────────────────────────────────────────
        weekly_trend = "N/D"
        try:
            rw = s.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1wk&range=1y",
                headers=hdrs, timeout=10,
            )
            if rw.status_code == 200:
                wc = [c for c in rw.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(wc) >= 10:
                    weekly_trend = "ALCISTA" if wc[-1] > sum(wc[-10:]) / 10 else "BAJISTA"
        except: pass

        # ── Mensual ──────────────────────────────────────────────────────
        monthly_trend = "N/D"
        try:
            rm = s.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1mo&range=3y",
                headers=hdrs, timeout=10,
            )
            if rm.status_code == 200:
                mc = [c for c in rm.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(mc) >= 6:
                    monthly_trend = "ALCISTA" if mc[-1] > sum(mc[-6:]) / 6 else "BAJISTA"
        except: pass

        daily_trend = "ALCISTA" if price > sma50 else "BAJISTA"
        tf_bullish  = [daily_trend, weekly_trend, monthly_trend].count("ALCISTA")
        tf_bearish  = [daily_trend, weekly_trend, monthly_trend].count("BAJISTA")
        if tf_bullish == 3:   tf_conf = "CONFLUENCIA ALCISTA TOTAL"
        elif tf_bullish == 2: tf_conf = "MAYORIA ALCISTA"
        elif tf_bearish == 3: tf_conf = "CONFLUENCIA BAJISTA TOTAL"
        elif tf_bearish == 2: tf_conf = "MAYORIA BAJISTA"
        else:                 tf_conf = "SIN CONFLUENCIA CLARA"

        if tf_bullish >= 2: tech_score += 2
        if tf_bearish >= 2: tech_score += 2

        return {
            "ticker":          ticker,
            "name":            meta.get("longName", ticker),
            "sector":          meta.get("sector", "Unknown"),
            "price":           round(price, 2),
            "change_pct":      round(change_pct, 2),
            "sma20":           round(sma20, 2),
            "sma50":           round(sma50, 2),
            "sma200":          round(sma200, 2) if sma200 else None,
            "vwap":            round(vwap, 2),
            "rsi":             round(rsi, 1),
            "rsi_zone":        rsi_zone,
            "macd":            round(macd, 3),
            "macd_signal":     round(macd_signal, 3),
            "macd_bullish":    macd_bullish,
            "stoch_k":         round(stoch_k, 1),
            "vol_ratio":       round(vol_ratio, 2),
            "obv_trend":       obv_trend,
            "atr":             round(atr, 2),
            "h52":             round(h52, 2),
            "l52":             round(l52, 2),
            "dist_h":          round(((price - h52) / h52) * 100, 1),
            "dist_l":          round(((price - l52) / l52) * 100, 1),
            "fib236":          fib236,
            "fib382":          fib382,
            "fib500":          fib500,
            "fib618":          fib618,
            "rh":              round(rh, 2),
            "rl":              round(rl, 2),
            "support_touches": support_touches,
            "mom1m":           round(mom1m, 1),
            "mom3m":           round(mom3m, 1),
            "structure":       structure,
            "daily_trend":     daily_trend,
            "weekly_trend":    weekly_trend,
            "monthly_trend":   monthly_trend,
            "tf_confluence":   tf_conf,
            "tech_score":      max(tech_score, 0),
        }

    except Exception as e:
        print(f"    {ticker}: error en datos de mercado — {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════
# CAPA 3 — DATOS FUNDAMENTALES (1 petición a Yahoo quoteSummary)
# ═══════════════════════════════════════════════════════════════════════

def get_fundamentals(ticker):
    """
    P/E, short interest, earnings próximos, histórico de beats e insiders.
    Una sola petición a Yahoo Finance quoteSummary.
    """
    result = {
        "pe_ratio": None, "short_interest": None,
        "revenue_growth": None, "profit_margins": None,
        "rec_key": "hold", "analyst_target": None, "analyst_upside": None,
        "earnings_days": None, "earnings_beats": 0,
        "insider_buys": 0, "insider_sells": 0,
    }
    modules = "defaultKeyStatistics,financialData,calendarEvents,earningsHistory,insiderTransactions"
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=12,
        )
        if r.status_code != 200:
            return result
        data  = r.json().get("quoteSummary", {}).get("result", [{}])[0]
        stats = data.get("defaultKeyStatistics", {})
        fin   = data.get("financialData", {})

        def _raw(d, key, default=None):
            return (d.get(key) or {}).get("raw", default)

        result["pe_ratio"]       = _raw(stats, "forwardPE")
        result["short_interest"] = round((_raw(stats, "shortPercentOfFloat", 0) or 0) * 100, 1)
        result["revenue_growth"] = round((_raw(fin, "revenueGrowth", 0) or 0) * 100, 1)
        result["profit_margins"] = round((_raw(fin, "profitMargins", 0) or 0) * 100, 1)
        result["rec_key"]        = fin.get("recommendationKey", "hold")

        target  = _raw(fin, "targetMeanPrice", 0) or 0
        current = _raw(fin, "currentPrice",    0) or 0
        if target and current:
            result["analyst_target"] = round(target, 2)
            result["analyst_upside"] = round(((target - current) / current) * 100, 1)

        dates = data.get("calendarEvents", {}).get("earnings", {}).get("earningsDate", [])
        if dates:
            days = (datetime.fromtimestamp(dates[0]["raw"]) - datetime.now()).days
            if 0 <= days <= 21:
                result["earnings_days"] = days

        history = data.get("earningsHistory", {}).get("history", [])
        result["earnings_beats"] = sum(
            1 for h in history[-4:]
            if (_raw(h, "surprisePercent", 0) or 0) > 0
        )

        for t in data.get("insiderTransactions", {}).get("transactions", [])[:10]:
            days_ago = (datetime.now() - datetime.fromtimestamp(_raw(t, "startDate", 0) or 0)).days
            if days_ago <= 30:
                txt = t.get("transactionText", "")
                if "Purchase" in txt: result["insider_buys"]  += 1
                elif "Sale"  in txt:  result["insider_sells"] += 1

    except Exception as e:
        print(f"    {ticker}: fundamentales error — {e}")

    return result

# ═══════════════════════════════════════════════════════════════════════
# CAPA 4 — SENTIMIENTO Y NOTICIAS
# ═══════════════════════════════════════════════════════════════════════

def get_sentiment(ticker, sector):
    """
    Noticias NewsAPI + RSS Yahoo Finance + rendimiento ETF sectorial.
    Calcula sentimiento básico por palabras clave.
    """
    news_items      = []
    sentiment_score = 0
    positive_words  = ["beat","surge","jump","upgrade","buy","strong","growth","record","partnership","contract"]
    negative_words  = ["miss","fall","drop","downgrade","sell","weak","loss","cut","investigation","lawsuit"]

    if NEWS_API_KEY:
        try:
            r = requests.get(
                f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=6&apiKey={NEWS_API_KEY}",
                timeout=10,
            )
            if r.status_code == 200:
                for a in r.json().get("articles", [])[:6]:
                    title = a.get("title", "")
                    news_items.append(title)
                    tl = title.lower()
                    sentiment_score += sum(1 for w in positive_words if w in tl)
                    sentiment_score -= sum(1 for w in negative_words if w in tl)
        except: pass

    try:
        feed = feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US")
        for e in feed.entries[:4]:
            news_items.append(e.title)
    except: pass

    sector_perf = None
    etf = SECTOR_ETFS.get(sector)
    if etf:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if r.status_code == 200:
                closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(closes) >= 2:
                    sector_perf = round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2)
        except: pass

    return {
        "news":            news_items[:8],
        "sentiment_score": sentiment_score,
        "sentiment_label": "POSITIVO" if sentiment_score > 2 else "NEGATIVO" if sentiment_score < -2 else "NEUTRAL",
        "sector_perf":     sector_perf,
    }

# ═══════════════════════════════════════════════════════════════════════
# CAPA 5 — MOMENTUM INSTITUCIONAL (sin IA, sin coste)
# ═══════════════════════════════════════════════════════════════════════

def get_inst_signal(tech):
    """
    Infiere actividad institucional desde volumen, precio y OBV.
    Devuelve (señal textual, boost de confianza).
    El boost se aplica ANTES del formateo para que la confianza sea coherente.
    """
    vol_ratio  = tech.get("vol_ratio", 1)
    obv_trend  = tech.get("obv_trend", "NEUTRAL")
    change_pct = tech.get("change_pct", 0)
    price      = tech.get("price", 0)
    vwap       = tech.get("vwap", price)
    boost      = 0
    signal     = "NEUTRAL"

    if   vol_ratio > 2.5 and change_pct > 3:   signal = "COMPRA INSTITUCIONAL PROBABLE";  boost = 5
    elif vol_ratio > 2.5 and change_pct < -3:   signal = "VENTA INSTITUCIONAL PROBABLE";   boost = 5
    elif price > vwap and vol_ratio > 1.5 and change_pct > 0: signal = "PRESION COMPRADORA"; boost = 3
    elif price < vwap and vol_ratio > 1.5 and change_pct < 0: signal = "PRESION VENDEDORA";  boost = 3

    if obv_trend == "ACUMULACION" and change_pct > 0:  boost += 2
    elif obv_trend == "DISTRIBUCION" and change_pct < 0: boost += 2

    return signal, boost

# ═══════════════════════════════════════════════════════════════════════
# CAPA 6 — IA (Claude Sonnet)
# ═══════════════════════════════════════════════════════════════════════

def call_ai(prompt, max_tokens=700):
    """Llama a la API de Claude. Devuelve texto o None si falla."""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg    = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"    IA error: {e}")
        return None


def _build_auto_prompt(tech, fund, sent, inst_signal, conf_boost):
    """
    Prompt para análisis automático.
    Exige convergencia real entre capas. La IA debe ser muy selectiva.
    El boost institucional ya está calculado — se informa a la IA para que
    lo tenga en cuenta pero no lo infle artificialmente.
    """
    fg      = market_context["fear_greed"]
    sp500   = market_context["sp500_change"]
    vix     = market_context["vix"]
    fg_str  = _fg_label(fg)

    news_txt  = "\n".join(f"- {h}" for h in sent["news"][:5]) or "- Sin noticias"
    macro_txt = "\n".join(f"- {h}" for h in market_context.get("macro_news", [])[:4]) or "- Sin noticias macro"
    econ_txt  = "\n".join(f"- {h}" for h in market_context.get("economic_events", [])[:3]) or "- Sin eventos"

    rec_map  = {"strongBuy":"COMPRA FUERTE","buy":"COMPRAR","hold":"MANTENER","sell":"VENDER","strongSell":"VENTA FUERTE"}
    rec_txt  = rec_map.get(fund.get("rec_key", "hold"), "MANTENER")
    tgt_txt  = (f"Precio objetivo analistas: ${fund['analyst_target']} ({fund['analyst_upside']:+.1f}% upside)"
                if fund.get("analyst_target") else "Sin precio objetivo disponible")
    earn_txt = (f"EARNINGS EN {fund['earnings_days']} DÍAS — {fund.get('earnings_beats',0)}/4 últimos beats"
                if fund.get("earnings_days") is not None else "Sin earnings próximos")

    return f"""Eres el mejor analista cuantitativo del mundo. Tu misión es encontrar las pocas oportunidades REALES del mercado.

REGLA CRÍTICA: Solo emites señal cuando hay CONVERGENCIA entre al menos 4 de estas 5 capas:
  1. Técnico (RSI, MACD, volumen, estructura)
  2. Timeframes (diario + semanal + mensual alineados)
  3. Fundamental (valoración, analistas, insiders)
  4. Sentimiento (noticias, contexto macro)
  5. Institucional (volumen anómalo, OBV)
Si no hay convergencia real en 4 capas → NO_SIGNAL obligatorio.
Prefiere NO_SIGNAL a una señal mediocre. La calidad importa más que la cantidad.

MACRO
Fear&Greed: {fg}/100 — {fg_str}
S&P500: {sp500:+.2f}% | VIX: {vix} {'— ALTA VOLATILIDAD' if vix > 25 else ''}
Noticias macro: {macro_txt}
Eventos económicos: {econ_txt}

TÉCNICO — {tech['ticker']} ({tech['name']})
Precio: ${tech['price']} ({tech['change_pct']:+.2f}% hoy) | Sector: {tech['sector']} | ETF sectorial: {sent.get('sector_perf','N/D')}%
Tendencias: Diario {tech['daily_trend']} | Semanal {tech['weekly_trend']} | Mensual {tech['monthly_trend']}
Confluencia: {tech['tf_confluence']} | Estructura: {tech['structure']}
SMA20: ${tech['sma20']} | SMA50: ${tech['sma50']} | SMA200: ${tech.get('sma200','N/D')} | VWAP: ${tech['vwap']} ({'SOBRE' if tech['price'] > tech['vwap'] else 'BAJO'} VWAP)
RSI(14): {tech['rsi']} {'— SOBREVENTA EXTREMA' if tech['rsi']<25 else '— sobreventa' if tech['rsi']<32 else '— SOBRECOMPRA EXTREMA' if tech['rsi']>75 else '— sobrecompra' if tech['rsi']>68 else ''}
MACD: {'ALCISTA' if tech['macd_bullish'] else 'BAJISTA'} | Estocástico K: {tech['stoch_k']} {'— SOBREVENTA' if tech['stoch_k']<20 else '— SOBRECOMPRA' if tech['stoch_k']>80 else ''}
Volumen: {tech['vol_ratio']}x media | OBV: {tech['obv_trend']} | ATR: ${tech['atr']}
Momentum: 1m {tech['mom1m']:+.1f}% | 3m {tech['mom3m']:+.1f}%
Fibonacci: 23.6%=${tech['fib236']} | 38.2%=${tech['fib382']} | 50%=${tech['fib500']} | 61.8%=${tech['fib618']}
Soporte: ${tech['rl']} ({tech['support_touches']} toques) | Resistencia: ${tech['rh']}
Mín 52s: ${tech['l52']} ({tech['dist_l']:+.1f}%) | Máx 52s: ${tech['h52']} ({tech['dist_h']:+.1f}%)

FUNDAMENTAL
P/E Forward: {fund.get('pe_ratio','N/D')} | Margen: {fund.get('profit_margins','N/D')}% | Short: {fund.get('short_interest',0)}% {'— POSIBLE SHORT SQUEEZE' if (fund.get('short_interest') or 0) > 20 else ''}
{earn_txt}
Insiders: {fund.get('insider_buys',0)} compras / {fund.get('insider_sells',0)} ventas (30d)

SENTIMIENTO
Noticias: {sent['sentiment_label']} (score {sent['sentiment_score']}) | Analistas: {rec_txt}
{tgt_txt}
{news_txt}

INSTITUCIONAL
{inst_signal} | Boost calculado: +{conf_boost}%

INSTRUCCIONES
Confianza mínima aceptable: {CONF_NORMAL}%. Por debajo → NO_SIGNAL.
No expliques el proceso. Ve directo al resultado.
Responde EXACTAMENTE en este formato:

SEÑAL: COMPRAR o VENDER
CONFIANZA: [X]%
🎯 ENTRADA ÓPTIMA: $[precio exacto]
📈 OBJETIVO: [+/-X%] → $[precio] en [X días/semanas] — [razón en 5 palabras]
🛑 STOP LOSS: $[precio en soporte/Fibonacci] — prob. stop: [X]%
⚖️ RATIO R/B: [X]:1
💬 POR QUÉ: [2-3 frases concretas — qué converge, por qué ahora]
⚡ CATALIZADOR: [factor más importante]
❌ INVALIDACIÓN: [precio o evento exacto]

Si no hay convergencia real en 4 capas: NO_SIGNAL"""


def _build_manual_prompt(tech, fund, sent):
    """
    Prompt para !analizar — siempre da respuesta completa aunque sea NEUTRAL.
    Más directo y conciso que el automático.
    """
    fg     = market_context["fear_greed"]
    fg_str = _fg_label(fg)
    return f"""Eres el mejor analista del mundo. El usuario solicita análisis de {tech['ticker']} ({tech['name']}).
Da SIEMPRE análisis completo. Si no hay señal clara: NEUTRAL con toda la info igualmente.
No expliques el proceso. Ve directo al resultado.

Precio: ${tech['price']} ({tech['change_pct']:+.2f}% hoy)
RSI: {tech['rsi']} | MACD: {'ALCISTA' if tech['macd_bullish'] else 'BAJISTA'} | Volumen: {tech['vol_ratio']}x
SMA20: ${tech['sma20']} | SMA50: ${tech['sma50']} | VWAP: ${tech['vwap']}
Tendencia: {tech['daily_trend']} diario | {tech['weekly_trend']} semanal | {tech['tf_confluence']}
Soporte: ${tech['rl']} ({tech['support_touches']} toques) | Resistencia: ${tech['rh']}
Fib 38.2%: ${tech['fib382']} | 61.8%: ${tech['fib618']}
Momentum: 1m {tech['mom1m']:+.1f}% | 3m {tech['mom3m']:+.1f}%
Fear&Greed: {fg}/100 ({fg_str}) | VIX: {market_context['vix']}
P/E: {fund.get('pe_ratio','N/D')} | Short: {fund.get('short_interest','N/D')}% | Analistas: {fund.get('rec_key','N/D')}
Sentimiento noticias: {sent['sentiment_label']}

Responde EXACTAMENTE así:
SEÑAL: COMPRAR / VENDER / NEUTRAL
CONFIANZA: [X]%
📊 SITUACIÓN: [1 frase — dónde está la acción ahora y por qué importa]
🎯 ENTRADA ÓPTIMA: $[precio]
📈 OBJETIVO: [+/-X%] → $[precio] en [plazo] — [razón]
🛑 STOP LOSS: $[precio] — prob. stop: [X]%
⚖️ RATIO R/B: [X]:1
💬 POR QUÉ: [2-3 frases concretas]
⚡ CATALIZADOR: [factor principal]
❌ INVALIDACIÓN: [precio o evento]"""

# ═══════════════════════════════════════════════════════════════════════
# FORMATEO DE ALERTAS DISCORD
# ═══════════════════════════════════════════════════════════════════════

def format_alert(tech, ai_response, conf_final, session_tag=""):
    """
    Formatea la respuesta de la IA en mensaje Discord limpio.
    Recibe conf_final ya con boost aplicado para que el header sea coherente.
    Devuelve (texto_formateado, señal, confianza).
    """
    # Extraer señal
    signal = "COMPRAR"
    for line in ai_response.splitlines():
        if line.startswith("SEÑAL:"):
            signal = "VENDER" if "VENDER" in line else "COMPRAR"
            break

    # Emoji por nivel y dirección
    is_buy = signal == "COMPRAR"
    if conf_final >= CONF_EXCEPCIONAL:
        emoji = "⚡" if is_buy else "💀"
    elif conf_final >= CONF_FUERTE:
        emoji = "🔥" if is_buy else "🔴"
    else:
        emoji = "🟢" if is_buy else "🔴"

    # Cuerpo: quitar SEÑAL y CONFIANZA (ya están en el header)
    body = "\n".join(
        line for line in ai_response.splitlines()
        if not line.startswith("SEÑAL:") and not line.startswith("CONFIANZA:")
    ).strip()

    sign = "+" if tech["change_pct"] >= 0 else ""
    sess = f"  [{session_tag}]" if session_tag and session_tag != "MERCADO" else ""
    now  = datetime.now(SPAIN_TZ).strftime("%H:%M  %d/%m/%Y")

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji}  **{signal}  —  {tech['ticker']}**{sess}\n"
        f"{tech['name']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰  **${tech['price']}**  ({sign}{tech['change_pct']}% hoy)  ·  {conf_final}% confianza\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{body}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐  {now} hora España"
    )
    return text, signal

# ═══════════════════════════════════════════════════════════════════════
# ANÁLISIS COMPLETO — automático y manual en una sola función
# ═══════════════════════════════════════════════════════════════════════

def analyze_ticker(ticker, name="", sector="Unknown", force=False):
    """
    Análisis completo con las 6 capas.

    force=False (automático):
        - Respeta score técnico mínimo (SCORE_MINIMO)
        - Aplica boost institucional antes del formateo
        - Respeta límites diarios
        - Rechaza NO_SIGNAL y confianza insuficiente

    force=True (manual, !analizar):
        - Sin filtros de score
        - Sin límites diarios
        - Siempre devuelve resultado (aunque sea NEUTRAL)

    Retorna (texto, señal, confianza, tech) o None si no hay señal válida.
    """
    print(f"  Analizando {ticker}...")

    # Capa 2 — datos técnicos
    tech = get_market_data(ticker)
    if not tech:
        print(f"    {ticker}: sin datos de mercado")
        return None

    # Filtro de score solo en automático
    if not force and tech["tech_score"] < SCORE_MINIMO:
        print(f"    {ticker}: score {tech['tech_score']} insuficiente (mín {SCORE_MINIMO})")
        return None

    # Capas 3, 4, 5
    fund              = get_fundamentals(ticker)
    sent              = get_sentiment(ticker, sector or tech["sector"])
    inst_signal, boost = get_inst_signal(tech)

    # Capa 6 — IA
    if force:
        prompt = _build_manual_prompt(tech, fund, sent)
    else:
        prompt = _build_auto_prompt(tech, fund, sent, inst_signal, boost)

    ai_response = call_ai(prompt, max_tokens=650 if force else 800)
    if not ai_response:
        return None

    # Automático: rechazar NO_SIGNAL
    if not force and "NO_SIGNAL" in ai_response:
        watch_signals[ticker] = {"last_analyzed": datetime.now().isoformat(), "developing": False}
        save_state()
        return None

    # Extraer confianza de la respuesta de la IA
    conf_ia = 0
    for line in ai_response.splitlines():
        if "CONFIANZA:" in line:
            digits = "".join(c for c in line if c.isdigit())
            if digits:
                conf_ia = int(digits[:3])
            break

    # Aplicar boost institucional ANTES del formateo (confianza coherente en Discord)
    conf_final = min(conf_ia + boost, 99) if not force else conf_ia

    # Controles de límites solo en automático
    if not force:
        if conf_final < CONF_NORMAL:
            print(f"    {ticker}: confianza {conf_final}% insuficiente (mín {CONF_NORMAL}%)")
            return None

        # Extraer señal para verificar límites
        signal_check = "COMPRAR"
        for line in ai_response.splitlines():
            if line.startswith("SEÑAL:"):
                signal_check = "VENDER" if "VENDER" in line else "COMPRAR"
                break

        puede, motivo = puede_enviar_alerta(signal_check, conf_final)
        if not puede:
            print(f"    {ticker}: {motivo}")
            return None

    # Formatear alerta con confianza final ya correcta
    session_tag = _session_label(datetime.now(SPAIN_TZ))
    text, signal = format_alert(tech, ai_response, conf_final, session_tag)

    # Actualizar watch_signals
    watch_signals[ticker] = {
        "last_analyzed": datetime.now().isoformat(),
        "developing":    conf_final >= CONF_FUERTE,
    }
    save_state()

    nivel = "EXCEPCIONAL ⚡" if conf_final >= CONF_EXCEPCIONAL else "FUERTE 🔥" if conf_final >= CONF_FUERTE else "NORMAL 🟢"
    print(f"    {ticker}: {nivel} {signal} {conf_final}%")

    return text, signal, conf_final, tech

# ═══════════════════════════════════════════════════════════════════════
# CICLO AUTOMÁTICO DE VIGILANCIA — cada 5 minutos
# ═══════════════════════════════════════════════════════════════════════

def watch_cycle():
    """
    1. quick_scan() — candidatos sin IA
    2. analyze_ticker() para los mejores
    3. Envía alertas respetando límites diarios
    No corre fuera del horario 09:00-23:00 hora España.
    """
    now = datetime.now(SPAIN_TZ)
    if now.hour < 9 or now.hour >= 23:
        return

    # Si ya alcanzamos el límite diario no tiene sentido analizar más
    # (las Excepcionales sí pueden seguir)
    if alertas_hoy() >= MAX_ALERTAS_DIA:
        print(f"  Límite diario de {MAX_ALERTAS_DIA} alertas alcanzado — ciclo omitido")
        return

    candidates = quick_scan()
    if not candidates:
        return

    # Rotación: muestra aleatoria del universo para no depender solo del screener
    not_analyzed = [
        t for t in UNIVERSE
        if not already_alerted_today(t)
        and (t not in watch_signals
             or (datetime.now() - datetime.fromisoformat(watch_signals[t].get("last_analyzed", "2000-01-01"))).total_seconds() > 86400)
    ]
    rotation = random.sample(not_analyzed, min(6, len(not_analyzed)))
    seen_in_candidates = {c["ticker"] for c in candidates}
    rotation_items = [
        {"ticker": t, "name": t, "sector": "Unknown", "score": 0}
        for t in rotation if t not in seen_in_candidates
    ]

    to_analyze = candidates + rotation_items
    print(f"\n[{now.strftime('%H:%M')} ES] {len(to_analyze)} candidatos | alertas hoy: {alertas_hoy()}/{MAX_ALERTAS_DIA}")

    alerts_this_cycle = 0

    for item in to_analyze:
        if alerts_this_cycle >= MAX_AI_POR_CICLO:
            break

        ticker = item["ticker"]

        if already_alerted_today(ticker):
            continue

        # No re-analizar si fue analizado hace menos de 1 hora
        last = watch_signals.get(ticker, {}).get("last_analyzed")
        if last:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < 3600:
                continue

        result = analyze_ticker(ticker, item.get("name", ticker), item.get("sector", "Unknown"))
        if not result:
            time.sleep(2)
            continue

        text, signal, conf, tech = result
        send_alert(text)
        save_prediction(ticker, signal, tech["price"], tech["rl"], conf)
        alerts_this_cycle += 1

        nivel = "EXCEPCIONAL ⚡" if conf >= CONF_EXCEPCIONAL else "FUERTE 🔥" if conf >= CONF_FUERTE else "NORMAL 🟢"
        print(f"    → Alerta enviada: {ticker} {nivel} ({signal}, {conf}%)")
        time.sleep(3)

    if alerts_this_cycle > 0:
        print(f"  {alerts_this_cycle} alerta(s) enviada(s) este ciclo")

# ═══════════════════════════════════════════════════════════════════════
# COMANDOS MANUALES — escucha !analizar cada 30 segundos
# ═══════════════════════════════════════════════════════════════════════

def listen_commands(init=False):
    """
    Revisa #solicitud-en-concreto buscando !analizar TICKER.

    init=True: marca mensajes existentes como vistos sin procesarlos
               (evita reprocesar comandos antiguos tras reinicio)
    init=False: procesa solo mensajes nuevos
    """
    global last_cmd_msg_id, processed_cmd_ids

    try:
        params = {"limit": 10}
        if last_cmd_msg_id:
            params["after"] = last_cmd_msg_id

        r = requests.get(
            f"https://discord.com/api/v10/channels/{DISCORD_SOLICITUD_ID}/messages",
            params=params,
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  listen_commands error {r.status_code}")
            return

        messages = r.json()
        if not messages:
            return

        last_cmd_msg_id = messages[0]["id"]

        if init:
            for m in messages:
                processed_cmd_ids.add(m["id"])
            print(f"  Comandos: {len(messages)} mensajes previos ignorados")
            return

        for msg in reversed(messages):
            msg_id = msg.get("id")
            if msg_id in processed_cmd_ids:
                continue
            processed_cmd_ids.add(msg_id)

            if msg.get("author", {}).get("bot"):
                continue

            text = msg.get("content", "").strip()
            print(f"  Mensaje en solicitudes: {text[:60]}")

            tickers = []
            for line in text.splitlines():
                line = line.strip().lower()
                if not line.startswith("!analizar"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    t = parts[1].upper().strip()
                    if t not in tickers:
                        tickers.append(t)

            if not tickers:
                continue

            for ticker in tickers:
                print(f"  !analizar {ticker}")
                send_solicitud(f"🔍  Analizando **{ticker}**... dame unos segundos.")
                update_status(f"🔍  Analizando **{ticker}** bajo demanda...")

                result = analyze_ticker(ticker, ticker, "Unknown", force=True)
                now_es = datetime.now(SPAIN_TZ)

                if not result:
                    send_solicitud(
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"❓  **{ticker}** no encontrado\n"
                        f"Verifica que el ticker sea correcto (ej: NVDA, AAPL)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                else:
                    text_alert, signal, conf, tech = result
                    send_solicitud(text_alert)

                update_status(
                    f"🟢  **Activo** — vigilando mercado\n"
                    f"📡 Fear&Greed: {market_context['fear_greed']} ({_fg_label(market_context['fear_greed'])}) | VIX: {market_context['vix']}\n"
                    f"🕐  Última actualización: {now_es.strftime('%H:%M')} — si esto no cambia en 10 min el bot está caído"
                )
                time.sleep(2)

    except Exception as e:
        import traceback
        print(f"  listen_commands excepción: {e}")
        print(traceback.format_exc())

# ═══════════════════════════════════════════════════════════════════════
# RESUMEN DOMINICAL — #aciertos-bot (cada domingo 10:00)
# ═══════════════════════════════════════════════════════════════════════

def weekly_report():
    """Revisa predicciones, compara con precios actuales, publica resumen."""
    now = datetime.now(SPAIN_TZ)
    wins, losses, pending = [], [], []

    for p in predictions:
        ticker      = p["ticker"]
        entry       = p.get("entry", 0)
        target      = p.get("target", entry * 1.15)
        stop        = p.get("stop",   entry * 0.93)
        signal      = p.get("signal", "COMPRAR")
        days_passed = (datetime.now() - datetime.fromisoformat(p["date"])).days

        current = entry
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
            )
            if r.status_code == 200:
                closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if closes:
                    current = closes[-1]
        except: pass

        change     = ((current - entry) / entry) * 100 if entry else 0
        hit_target = (signal == "COMPRAR" and current >= target) or (signal == "VENDER" and current <= target)
        hit_stop   = (signal == "COMPRAR" and current <= stop)   or (signal == "VENDER" and current >= stop)

        if p["result"] == "win" or hit_target:
            if p["result"] != "win":
                p["result"] = "win"; p["exit_price"] = current; save_state()
            wins.append({"ticker": ticker, "change": change, "days": days_passed})
        elif p["result"] == "loss" or hit_stop:
            if p["result"] != "loss":
                p["result"] = "loss"; p["exit_price"] = current; save_state()
            losses.append({"ticker": ticker, "change": change, "days": days_passed})
        elif p["result"] == "pending" and days_passed >= 1:
            pending.append({"ticker": ticker, "change": change, "days": days_passed})

        time.sleep(0.3)

    total    = len(wins) + len(losses)
    win_rate = round(len(wins) / total * 100) if total > 0 else 0
    avg_win  = round(sum(w["change"] for w in wins)   / len(wins),   1) if wins   else 0
    avg_loss = round(sum(l["change"] for l in losses) / len(losses), 1) if losses else 0

    week_start = (now - timedelta(days=7)).strftime("%d/%m")
    week_end   = now.strftime("%d/%m/%Y")

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊  **RESUMEN SEMANAL**",
        f"Semana del {week_start} al {week_end}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for w in sorted(wins,   key=lambda x: x["change"], reverse=True):
        lines.append(f"✅  **{w['ticker']}**  {w['change']:+.1f}%  en {w['days']} días")
    for l in sorted(losses, key=lambda x: x["change"]):
        lines.append(f"❌  **{l['ticker']}**  {l['change']:+.1f}%  stop en {l['days']} días")
    for p in pending:
        lines.append(f"⏳  **{p['ticker']}**  {p['change']:+.1f}% — pendiente")
    if not wins and not losses and not pending:
        lines.append("Sin predicciones esta semana")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯  Aciertos: {len(wins)}/{total}  —  {win_rate}%",
    ]
    if wins:   lines.append(f"💰  Ganancia media: +{avg_win}%")
    if losses: lines.append(f"📉  Pérdida media: {avg_loss}%")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    send_acierto("\n".join(lines))
    send_log(f"📊 Resumen dominical: {len(wins)} aciertos / {len(losses)} stops / {len(pending)} pendientes")


def weekly_summary():
    """Resumen de actividad en #log-bot (lunes y jueves 09:00)."""
    now     = datetime.now(SPAIN_TZ)
    total   = len([p for p in predictions if p["result"] != "pending"])
    wins    = len([p for p in predictions if p["result"] == "win"])
    losses  = len([p for p in predictions if p["result"] == "loss"])
    pending = len([p for p in predictions if p["result"] == "pending"])
    rate    = round(wins / total * 100, 1) if total > 0 else 0
    send_log(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 RESUMEN — {now.strftime('%d/%m/%Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Acertadas: {wins}  ❌ Falladas: {losses}  ⏳ Pendientes: {pending}\n"
        f"🎯 Tasa de acierto: {rate}%\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _session_label(now):
    m = now.hour * 60 + now.minute
    if  900 <= m <  930: return "PREMARKET"
    if  930 <= m < 1380: return "MERCADO"
    if 1380 <= m < 1440: return "AFTERHOURS"
    return "FUERA DE MERCADO"

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(SPAIN_TZ)
    print(f"StockBot Pro v4 — {now.strftime('%H:%M %d/%m/%Y')}")

    load_state()
    update_status(f"⚙️  **Arrancando...**\n🕐  {now.strftime('%H:%M  %d/%m/%Y')}")
    update_status("⚙️  **Cargando contexto macro...**")
    update_market_context()

    fg      = market_context["fear_greed"]
    fg_str  = _fg_label(fg)
    sp500   = market_context["sp500_change"]
    vix     = market_context["vix"]
    total_h = alertas_hoy()

    send_log(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 **StockBot v4 arrancado** — {now.strftime('%H:%M %d/%m/%Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Fear&Greed: {fg}/100 ({fg_str})\n"
        f"📈 S&P500: {sp500:+.2f}%  |  VIX: {vix}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Alertas hoy: {total_h}/{MAX_ALERTAS_DIA}\n"
        f"🟢 Vigilancia activa cada 5 min"
    )

    print("  Publicando instrucciones...")
    post_instrucciones()
    print("  Instrucciones publicadas")

    update_status(
        f"🟢  **Activo** — vigilando mercado\n"
        f"📡 Fear&Greed: {fg} ({fg_str}) | VIX: {vix}\n"
        f"🕐  {now.strftime('%H:%M  %d/%m/%Y')}"
    )

    watch_cycle()
    listen_commands(init=True)

    schedule.every(5).minutes.do(watch_cycle)
    schedule.every().day.at("09:00").do(update_market_context)
    schedule.every().day.at("00:01").do(lambda: send_log("🔄 Nuevo día — límites reseteados automáticamente"))
    schedule.every().sunday.at("10:00").do(weekly_report)
    schedule.every().monday.at("09:00").do(weekly_summary)
    schedule.every().thursday.at("09:00").do(weekly_summary)

    last_cmd_check    = 0.0
    last_status_check = 0.0

    while True:
        schedule.run_pending()
        ts = time.time()

        if ts - last_cmd_check >= 30:
            listen_commands()
            last_cmd_check = ts

        if ts - last_status_check >= 300:
            now_loop = datetime.now(SPAIN_TZ)
            fg_loop  = market_context["fear_greed"]
            update_status(
                f"🟢  **Activo** — vigilando mercado\n"
                f"📡 Fear&Greed: {fg_loop} ({_fg_label(fg_loop)}) | VIX: {market_context['vix']}\n"
                f"🕐  Última actualización: {now_loop.strftime('%H:%M')} — si esto no cambia en 10 min el bot está caído"
            )
            last_status_check = ts

        time.sleep(5)


if __name__ == "__main__":
    main()
