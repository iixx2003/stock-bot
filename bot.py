import os, time, json, random, schedule, requests, feedparser, anthropic
from datetime import datetime, timedelta
from collections import defaultdict
import pytz

# ═══════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════
DISCORD_TOKEN     = os.environ.get("DISCORD_TOKEN")
DISCORD_ALERTS_ID = os.environ.get("DISCORD_CHANNEL_ID")
DISCORD_LOG_ID      = "1478113089093374034"
DISCORD_ACIERTOS_ID = "1478461406251847812"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.environ.get("NEWS_API_KEY")

SPAIN_TZ = pytz.timezone("Europe/Madrid")

# Umbrales de confianza
CONF_NORMAL      = 82
CONF_FUERTE      = 88
CONF_EXCEPCIONAL = 94
MAX_FUERTES_DIA  = 2
MAX_SELLS_DIA    = 1

# Estado global
alerts_sent      = {}   # {ticker: datetime}
predictions      = []
failed_patterns  = {}
market_context   = {"fear_greed":50,"sp500_change":0,"vix":15,"macro_news":[],"economic_events":[],"updated_at":None}
watch_signals    = {}   # {ticker: {"score": X, "last_analyzed": datetime, "developing": bool}}

PREDICTIONS_FILE  = "/app/predictions.json"
WATCHSTATE_FILE   = "/app/watchstate.json"

# ═══════════════════════════════════════════════════════
# UNIVERSO DE ACCIONES
# ═══════════════════════════════════════════════════════
SP500 = ["MMM","ABT","ABBV","ACN","ADBE","AMD","AFL","GOOGL","GOOG","MO","AMZN","AAL","AEP","AXP","AIG","AMT","AWK","AMGN","APH","ADI","AAPL","AMAT","ANET","T","ADSK","AZO","BKR","BAC","BK","BAX","BIIB","BLK","BX","BA","BMY","AVGO","CDNS","COF","KMX","CCL","CAT","CBRE","CNC","SCHW","CHTR","CVX","CMG","CB","CI","CTAS","CSCO","C","CLX","CME","KO","CL","CMCSA","COP","STZ","CPRT","GLW","COST","CCI","CSX","CMI","CVS","DHI","DHR","DE","DAL","DVN","DXCM","DLR","DFS","DG","DLTR","D","DOV","DOW","LLY","ETN","EBAY","ECL","EW","EA","ELV","EMR","ENPH","EOG","EFX","EQIX","EL","ETSY","EXC","EXPE","XOM","FDS","FAST","FRT","FDX","FIS","FITB","FSLR","FI","FLT","F","FTNT","FCX","GE","GD","GIS","GM","GILD","GS","HAL","HIG","HCA","HD","HON","HRL","HPQ","HUBB","HUM","IBM","IDXX","ITW","INTC","ICE","INTU","ISRG","JNJ","JPM","KDP","KEY","KMB","KMI","KLAC","KHC","KR","LHX","LH","LRCX","LIN","LMT","LOW","LULU","MTB","MPC","MAR","MMC","MAS","MA","MCD","MCK","MDT","MRK","META","MET","MGM","MCHP","MU","MSFT","MRNA","MDLZ","MNST","MCO","MS","MSI","NDAQ","NTAP","NFLX","NEE","NKE","NSC","NTRS","NOC","NCLH","NVDA","NXPI","ORLY","OXY","ODFL","ON","OKE","ORCL","PCAR","PLTR","PH","PAYX","PYPL","PEP","PFE","PM","PSX","PNC","PPG","PPL","PG","PGR","PLD","PRU","PEG","PSA","QCOM","RTX","O","REGN","RF","RMD","ROK","ROP","ROST","RCL","SPGI","CRM","SLB","SRE","NOW","SHW","SPG","SJM","SNA","SO","SWK","SBUX","STT","STLD","SYK","SMCI","SNPS","SYY","TMUS","TROW","TTWO","TGT","TEL","TSLA","TXN","TMO","TJX","TSCO","TT","TRV","TFC","TSN","USB","UBER","UNP","UAL","UPS","URI","UNH","VLO","VTR","VRSN","VRSK","VZ","VRTX","VMC","WAB","WBA","WMT","DIS","WM","WAT","WEC","WFC","WELL","WDC","WY","WHR","WMB","WYNN","XEL","YUM","ZBH","ZTS"]
NASDAQ100 = ["ADBE","AMD","ABNB","GOOGL","AMZN","AMGN","AAPL","ARM","ASML","ADSK","BKR","BIIB","BKNG","AVGO","CDNS","CHTR","CTAS","CSCO","CMCSA","CPRT","COST","CRWD","CSX","DDOG","DXCM","DLTR","EA","ENPH","EXC","FAST","FTNT","GILD","HON","IDXX","INTC","INTU","ISRG","KDP","KLAC","LRCX","LULU","MAR","MRVL","MELI","META","MCHP","MU","MSFT","MRNA","MDLZ","MDB","MNST","NFLX","NVDA","NXPI","ORLY","ODFL","ON","PCAR","PANW","PAYX","PYPL","QCOM","REGN","ROP","ROST","SBUX","SNPS","TTWO","TMUS","TSLA","TXN","VRSK","VRTX","WBA","WDAY","XEL","ZS","ZM"]
EXTRAS = ["SOFI","RIVN","COIN","MSTR","HOOD","RBLX","SNAP","LYFT","SHOP","SQ","ROKU","SPOT","NET","PANW","SMCI","GME","MARA","RIOT","CLSK","LCID","NKLA","AFRM","UPST","DKNG","CHWY","BYND","NIO","XPEV","LI","GRAB","SEA","BIDU","RKT","RELY","STNE","IREN","PINS","CCL","NCLH","RCL","DAL","AAL","UAL"]
UNIVERSE = list(set(SP500 + NASDAQ100 + EXTRAS))

SECTOR_ETFS = {"Technology":"XLK","Healthcare":"XLV","Financials":"XLF","Energy":"XLE","Consumer Cyclical":"XLY","Industrials":"XLI","Communication Services":"XLC","Consumer Defensive":"XLP","Utilities":"XLU","Real Estate":"XLRE","Basic Materials":"XLB"}

# ═══════════════════════════════════════════════════════
# DISCORD
# ═══════════════════════════════════════════════════════
def _post(cid, msg):
    if len(msg) > 1900: msg = msg[:1897] + "..."
    try:
        r = requests.post(
            f"https://discord.com/api/v10/channels/{cid}/messages",
            json={"content": msg},
            headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
            timeout=10)
        if r.status_code not in (200, 201):
            print(f"Discord {r.status_code}")
    except Exception as e:
        print(f"Discord err: {e}")

def send_alert(msg):   _post(DISCORD_ALERTS_ID, msg)
def send_log(msg):     _post(DISCORD_LOG_ID, msg)
def send_acierto(msg): _post(DISCORD_ACIERTOS_ID, msg)

# ═══════════════════════════════════════════════════════
# PERSISTENCIA
# ═══════════════════════════════════════════════════════
def load_state():
    global predictions, watch_signals
    try:
        if os.path.exists(PREDICTIONS_FILE):
            with open(PREDICTIONS_FILE) as f: predictions = json.load(f)
    except: predictions = []
    try:
        if os.path.exists(WATCHSTATE_FILE):
            with open(WATCHSTATE_FILE) as f: watch_signals = json.load(f)
    except: watch_signals = {}

def save_state():
    try:
        with open(PREDICTIONS_FILE, "w") as f: json.dump(predictions, f)
        with open(WATCHSTATE_FILE, "w") as f: json.dump(watch_signals, f)
    except: pass

def add_prediction(ticker, signal, entry, target_short, target_mid, target_long, stop, conf, days):
    predictions.append({
        "ticker": ticker, "signal": signal, "entry": entry,
        "target_short": target_short, "target_mid": target_mid, "target_long": target_long,
        "stop": stop, "confidence": conf, "days": days,
        "date": datetime.now().isoformat(), "result": "pending", "exit_price": None
    })
    save_state()

def already_alerted(ticker):
    if ticker in alerts_sent:
        return datetime.now() - alerts_sent[ticker] < timedelta(hours=24)
    return False

def fuertes_today():
    today = datetime.now(SPAIN_TZ).date()
    return sum(1 for p in predictions
               if p["confidence"] >= CONF_FUERTE
               and p["confidence"] < CONF_EXCEPCIONAL
               and datetime.fromisoformat(p["date"]).date() == today)

def sells_today():
    today = datetime.now(SPAIN_TZ).date()
    return sum(1 for p in predictions
               if p["signal"] == "VENDER"
               and datetime.fromisoformat(p["date"]).date() == today)

# ═══════════════════════════════════════════════════════
# CONTEXTO MACRO
# ═══════════════════════════════════════════════════════
def update_market_context():
    global market_context
    print("  Actualizando contexto macro...")
    fg = 50
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        if r.status_code == 200: fg = int(r.json()["data"][0]["value"])
    except: pass

    sp500, vix = 0, 15
    for ticker, key in [("SPY", "sp"), ("^VIX", "vix")]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code == 200:
                closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(closes) >= 2:
                    if key == "sp": sp500 = round(((closes[-1]-closes[-2])/closes[-2])*100, 2)
                    else: vix = round(closes[-1], 1)
            time.sleep(0.5)
        except: pass

    macro_news = []
    try:
        r = requests.get(
            f"https://newsapi.org/v2/top-headlines?category=business&language=en&pageSize=8&apiKey={NEWS_API_KEY}",
            timeout=10)
        if r.status_code == 200:
            macro_news = [a.get("title", "") for a in r.json().get("articles", [])[:8]]
    except: pass

    econ = []
    try:
        r = requests.get(
            f"https://newsapi.org/v2/everything?q=Federal+Reserve+OR+CPI+OR+inflation&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}",
            timeout=10)
        if r.status_code == 200:
            econ = [a.get("title", "") for a in r.json().get("articles", [])[:5]]
    except: pass

    market_context = {
        "fear_greed": fg, "sp500_change": sp500, "vix": vix,
        "macro_news": macro_news, "economic_events": econ,
        "updated_at": datetime.now(SPAIN_TZ).strftime("%H:%M")
    }
    fg_label = ("PANICO EXTREMO" if fg < 20 else "Miedo" if fg < 40
                else "Neutral" if fg < 60 else "Codicia" if fg < 80 else "EUFORIA")
    print(f"  Fear&Greed: {fg} ({fg_label}) | S&P500: {sp500}% | VIX: {vix}")
    send_log(f"📊 Macro — Fear&Greed: {fg} ({fg_label}) | S&P500: {'+' if sp500>=0 else ''}{sp500}% | VIX: {vix}")

# ═══════════════════════════════════════════════════════
# CAPA 1 — VIGILANCIA RÁPIDA (sin IA, sin coste)
# ═══════════════════════════════════════════════════════
def quick_scan():
    """Escaneo rápido cada 5 min. Solo precio y volumen. Sin IA."""
    now = datetime.now(SPAIN_TZ)
    if now.hour < 9 or now.hour >= 23: return

    urgent = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json", "Referer": "https://finance.yahoo.com"
    }

    # Screeners de Yahoo para acciones con movimiento real hoy
    for url in [
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=50",
    ]:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
                for q in quotes:
                    sym = q.get("symbol", "")
                    price = q.get("regularMarketPrice", 0)
                    vol = q.get("regularMarketVolume", 0)
                    avg_vol = max(q.get("averageDailyVolume3Month", 1), 1)
                    change = abs(q.get("regularMarketChangePercent", 0))
                    vol_ratio = vol / avg_vol

                    if not sym or "." in sym or len(sym) > 5: continue
                    if price < 5 or vol < 500_000: continue
                    if already_alerted(sym): continue

                    # Marcar como urgente si hay movimiento significativo
                    urgency_score = 0
                    if change > 8: urgency_score += 3
                    elif change > 5: urgency_score += 2
                    elif change > 3: urgency_score += 1
                    if vol_ratio > 3: urgency_score += 3
                    elif vol_ratio > 2: urgency_score += 2
                    elif vol_ratio > 1.5: urgency_score += 1

                    if urgency_score >= 2:
                        urgent.append({
                            "ticker": sym, "price": price,
                            "change": q.get("regularMarketChangePercent", 0),
                            "vol_ratio": round(vol_ratio, 2),
                            "urgency": urgency_score,
                            "name": q.get("longName", sym),
                            "sector": q.get("sector", "Unknown"),
                            "market_cap": q.get("marketCap", 0),
                        })
            time.sleep(0.5)
        except Exception as e:
            print(f"  Quick scan err: {e}")

    # Ordenar por urgencia
    urgent.sort(key=lambda x: x["urgency"], reverse=True)

    # Añadir acciones con señal en desarrollo de ciclos anteriores
    developing = [t for t, s in watch_signals.items()
                  if s.get("developing") and not already_alerted(t)]

    print(f"  Quick scan: {len(urgent)} urgentes + {len(developing)} en desarrollo")
    return urgent[:20], developing[:10]

# ═══════════════════════════════════════════════════════
# CAPA 2 — DATOS TÉCNICOS COMPLETOS
# ═══════════════════════════════════════════════════════
def get_technical_data(ticker):
    """Obtiene datos técnicos completos en 3 timeframes."""
    try:
        ua = random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        ])
        hdrs = {"User-Agent": ua, "Accept": "application/json",
                "Referer": f"https://finance.yahoo.com/quote/{ticker}/"}

        # Timeframe diario (1 año)
        s = requests.Session()
        s.get(f"https://finance.yahoo.com/quote/{ticker}/", headers=hdrs, timeout=8)
        host = random.choice(["query1", "query2"])
        r = s.get(
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y",
            headers=hdrs, timeout=15)
        if r.status_code != 200: return None

        result = r.json().get("chart", {}).get("result", [])
        if not result: return None
        result = result[0]
        meta = result.get("meta", {})
        q = result["indicators"]["quote"][0]

        closes  = [c for c in q.get("close",  []) if c is not None]
        volumes = [v for v in q.get("volume", []) if v is not None]
        highs   = [h for h in q.get("high",   []) if h is not None]
        lows    = [l for l in q.get("low",    []) if l is not None]

        if len(closes) < 50: return None

        price = closes[-1]
        change_pct = ((price - closes[-2]) / closes[-2]) * 100

        # === MEDIAS MÓVILES ===
        sma20  = sum(closes[-20:])  / 20
        sma50  = sum(closes[-50:])  / 50
        sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        ema12  = closes[-1]
        ema26  = closes[-1]
        for i in range(min(26, len(closes))):
            k12 = 2/(12+1); k26 = 2/(26+1)
            ema12 = closes[-(i+1)] * k12 + ema12 * (1-k12)
            ema26 = closes[-(i+1)] * k26 + ema26 * (1-k26)

        # === MACD ===
        macd = ema12 - ema26
        macd_signal = macd * 0.9
        macd_hist = macd - macd_signal
        macd_bullish = macd > macd_signal

        # === RSI 14 ===
        gains, losses = [], []
        for i in range(1, 15):
            d = closes[-i] - closes[-i-1]
            (gains if d >= 0 else losses).append(abs(d))
        avg_gain = sum(gains)/14 if gains else 0
        avg_loss = sum(losses)/14 if losses else 0.001
        rsi = 100 - (100 / (1 + avg_gain/avg_loss))

        # === ESTOCÁSTICO ===
        low14  = min(lows[-14:])  if len(lows)  >= 14 else min(lows)
        high14 = max(highs[-14:]) if len(highs) >= 14 else max(highs)
        stoch_k = ((price - low14) / (high14 - low14) * 100) if high14 != low14 else 50
        stoch_d = stoch_k * 0.85

        # === ROC (Rate of Change) ===
        roc5  = ((price - closes[-5])  / closes[-5]  * 100) if len(closes) >= 5  else 0
        roc10 = ((price - closes[-10]) / closes[-10] * 100) if len(closes) >= 10 else 0

        # === VOLUMEN ===
        avg_vol20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
        vol_ratio = volumes[-1] / avg_vol20 if avg_vol20 > 0 else 1

        # OBV (On Balance Volume)
        obv = 0
        for i in range(1, min(20, len(closes))):
            if closes[-i] > closes[-i-1]: obv += volumes[-i]
            elif closes[-i] < closes[-i-1]: obv -= volumes[-i]
        obv_trend = "ACUMULACION" if obv > 0 else "DISTRIBUCION"

        # VWAP aproximado (últimos 5 días)
        vwap = sum(closes[-5:]) / 5

        # === ATR ===
        atr_vals = []
        for i in range(1, min(15, len(closes))):
            hl = highs[-i] - lows[-i] if highs and lows else 0
            atr_vals.append(max(hl,
                abs(highs[-i]-closes[-i-1]) if highs else 0,
                abs(lows[-i]-closes[-i-1])  if lows  else 0))
        atr = sum(atr_vals)/len(atr_vals) if atr_vals else price*0.02

        # === FIBONACCI ===
        h52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        l52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
        rng = h52 - l52
        fib236 = round(h52 - rng*0.236, 2)
        fib382 = round(h52 - rng*0.382, 2)
        fib500 = round(h52 - rng*0.500, 2)
        fib618 = round(h52 - rng*0.618, 2)

        # === ESTRUCTURA DE PRECIO ===
        # Máximos y mínimos crecientes (últimos 10 días)
        recent_highs = highs[-10:] if len(highs) >= 10 else highs
        recent_lows  = lows[-10:]  if len(lows)  >= 10 else lows
        higher_highs = all(recent_highs[i] >= recent_highs[i-1] for i in range(1, len(recent_highs)))
        higher_lows  = all(recent_lows[i]  >= recent_lows[i-1]  for i in range(1, len(recent_lows)))
        lower_highs  = all(recent_highs[i] <= recent_highs[i-1] for i in range(1, len(recent_highs)))
        lower_lows   = all(recent_lows[i]  <= recent_lows[i-1]  for i in range(1, len(recent_lows)))

        if higher_highs and higher_lows: structure = "TENDENCIA ALCISTA CLARA"
        elif lower_highs and lower_lows: structure = "TENDENCIA BAJISTA CLARA"
        else: structure = "LATERAL / CONSOLIDACION"

        # Soporte y resistencia probados
        rh = max(highs[-20:]) if len(highs) >= 20 else price
        rl = min(lows[-20:])  if len(lows)  >= 20 else price

        # Cuántas veces rebotó en soporte actual
        support_touches = sum(1 for l in lows[-60:] if abs(l - rl) / rl < 0.02) if len(lows) >= 60 else 0

        # === MOMENTUM ===
        mom1m = ((price - closes[-22]) / closes[-22] * 100) if len(closes) >= 22 else 0
        mom3m = ((price - closes[-66]) / closes[-66] * 100) if len(closes) >= 66 else 0
        mom6m = ((price - closes[-126])/ closes[-126]* 100) if len(closes) >= 126 else 0
        vol20 = ((sum((c-sma20)**2 for c in closes[-20:])/20)**0.5)/price*100

        # === TIMEFRAME SEMANAL ===
        weekly_trend = "N/D"
        try:
            rw = s.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1wk&range=1y",
                headers=hdrs, timeout=10)
            if rw.status_code == 200:
                wc = [c for c in rw.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(wc) >= 10:
                    w_sma10 = sum(wc[-10:]) / 10
                    weekly_trend = "ALCISTA" if wc[-1] > w_sma10 else "BAJISTA"
        except: pass

        # === TIMEFRAME MENSUAL ===
        monthly_trend = "N/D"
        try:
            rm = s.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1mo&range=3y",
                headers=hdrs, timeout=10)
            if rm.status_code == 200:
                mc = [c for c in rm.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(mc) >= 6:
                    m_sma6 = sum(mc[-6:]) / 6
                    monthly_trend = "ALCISTA" if mc[-1] > m_sma6 else "BAJISTA"
        except: pass

        # Confluencia de timeframes
        daily_trend = "ALCISTA" if price > sma50 else "BAJISTA"
        tf_bullish = sum(1 for t in [daily_trend, weekly_trend, monthly_trend] if t == "ALCISTA")
        tf_bearish = sum(1 for t in [daily_trend, weekly_trend, monthly_trend] if t == "BAJISTA")
        if tf_bullish == 3: tf_confluence = "CONFLUENCIA ALCISTA TOTAL"
        elif tf_bullish == 2: tf_confluence = "MAYORIA ALCISTA"
        elif tf_bearish == 3: tf_confluence = "CONFLUENCIA BAJISTA TOTAL"
        elif tf_bearish == 2: tf_confluence = "MAYORIA BAJISTA"
        else: tf_confluence = "SIN CONFLUENCIA CLARA"

        # === SCORE TÉCNICO ===
        score = 0
        rsi_zone = "neutral"
        if rsi < 25:   score += 4; rsi_zone = "oversold_extreme"
        elif rsi < 32: score += 2; rsi_zone = "oversold"
        elif rsi > 75: score += 4; rsi_zone = "overbought_extreme"
        elif rsi > 68: score += 2; rsi_zone = "overbought"
        if vol_ratio > 3:   score += 3
        elif vol_ratio > 2: score += 2
        elif vol_ratio > 1.5: score += 1
        if abs(change_pct) > 8:  score += 3
        elif abs(change_pct) > 5: score += 2
        elif abs(change_pct) > 3: score += 1
        if macd_bullish and change_pct > 0: score += 2
        if stoch_k < 20 or stoch_k > 80: score += 1
        if tf_bullish >= 2: score += 2
        if tf_bearish >= 2: score += 2
        if obv_trend == "ACUMULACION" and change_pct > 0: score += 2
        if abs(mom1m) > 15: score += 2
        elif abs(mom1m) > 8: score += 1
        if support_touches >= 3: score += 2
        sector = meta.get("sector", "Unknown")
        pk = f"{'BUY' if change_pct > 0 else 'SELL'}_{rsi_zone}_{sector}"
        if failed_patterns.get(pk, 0) >= 3: score -= 3

        return {
            "ticker": ticker,
            "name": meta.get("longName", ticker),
            "sector": sector,
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            # Medias
            "sma20": round(sma20, 2), "sma50": round(sma50, 2),
            "sma200": round(sma200, 2) if sma200 else None,
            "vwap": round(vwap, 2),
            # Momentum
            "rsi": round(rsi, 1), "rsi_zone": rsi_zone,
            "macd": round(macd, 3), "macd_signal": round(macd_signal, 3),
            "macd_hist": round(macd_hist, 3), "macd_bullish": macd_bullish,
            "stoch_k": round(stoch_k, 1), "stoch_d": round(stoch_d, 1),
            "roc5": round(roc5, 2), "roc10": round(roc10, 2),
            # Volumen
            "vol_ratio": round(vol_ratio, 2),
            "obv_trend": obv_trend,
            # ATR y estructura
            "atr": round(atr, 2),
            "structure": structure,
            "support_touches": support_touches,
            # Fibonacci
            "h52": round(h52, 2), "l52": round(l52, 2),
            "fib236": fib236, "fib382": fib382,
            "fib500": fib500, "fib618": fib618,
            "rh": round(rh, 2), "rl": round(rl, 2),
            "dist_h": round(((price-h52)/h52)*100, 1),
            "dist_l": round(((price-l52)/l52)*100, 1),
            # Momentum temporal
            "mom1m": round(mom1m, 1), "mom3m": round(mom3m, 1), "mom6m": round(mom6m, 1),
            "vol20": round(vol20, 1),
            # Timeframes
            "daily_trend": daily_trend,
            "weekly_trend": weekly_trend,
            "monthly_trend": monthly_trend,
            "tf_confluence": tf_confluence,
            # Score
            "score": max(score, 0),
            "rsi_zone": rsi_zone,
        }
    except Exception as e:
        return None

# ═══════════════════════════════════════════════════════
# CAPA 3 — DATOS FUNDAMENTALES + SENTIMIENTO
# ═══════════════════════════════════════════════════════
def get_fundamental_data(ticker):
    result = {}
    headers = {"User-Agent": "Mozilla/5.0"}

    # P/E, deuda, crecimiento
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=defaultKeyStatistics,financialData,earningsTrend",
            headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json().get("quoteSummary", {}).get("result", [{}])[0]
            stats = data.get("defaultKeyStatistics", {})
            fin   = data.get("financialData", {})
            result["pe_ratio"]       = stats.get("forwardPE", {}).get("raw", 0)
            result["short_interest"] = round(stats.get("shortPercentOfFloat", {}).get("raw", 0) * 100, 1)
            result["revenue_growth"] = round(fin.get("revenueGrowth", {}).get("raw", 0) * 100, 1)
            result["profit_margins"] = round(fin.get("profitMargins", {}).get("raw", 0) * 100, 1)
            result["debt_equity"]    = round(fin.get("debtToEquity", {}).get("raw", 0), 2)
            result["rec_mean"]       = fin.get("recommendationMean", {}).get("raw", 3)
            result["rec_key"]        = fin.get("recommendationKey", "hold")
    except: pass

    # Earnings próximos
    result["earnings_days"] = None
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=calendarEvents",
            headers=headers, timeout=10)
        if r.status_code == 200:
            dates = r.json().get("quoteSummary", {}).get("result", [{}])[0].get("calendarEvents", {}).get("earnings", {}).get("earningsDate", [])
            if dates:
                days = (datetime.fromtimestamp(dates[0]["raw"]) - datetime.now()).days
                if 0 <= days <= 21: result["earnings_days"] = days
    except: pass

    # Historial de earnings beats
    result["earnings_beats"] = 0
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=earningsHistory",
            headers=headers, timeout=10)
        if r.status_code == 200:
            history = r.json().get("quoteSummary", {}).get("result", [{}])[0].get("earningsHistory", {}).get("history", [])
            beats = sum(1 for h in history[-4:]
                       if h.get("surprisePercent", {}).get("raw", 0) > 0)
            result["earnings_beats"] = beats
    except: pass

    # Insider activity
    result["insider_buys"] = 0
    result["insider_sells"] = 0
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=insiderTransactions",
            headers=headers, timeout=10)
        if r.status_code == 200:
            txns = r.json().get("quoteSummary", {}).get("result", [{}])[0].get("insiderTransactions", {}).get("transactions", [])
            for t in txns[:10]:
                days_ago = (datetime.now() - datetime.fromtimestamp(t.get("startDate", {}).get("raw", 0))).days
                if days_ago <= 30:
                    if "Purchase" in t.get("transactionText", ""): result["insider_buys"] += 1
                    elif "Sale" in t.get("transactionText", ""):   result["insider_sells"] += 1
    except: pass

    # Precio objetivo de analistas
    result["analyst_target"] = None
    result["analyst_upside"] = None
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=financialData",
            headers=headers, timeout=10)
        if r.status_code == 200:
            fin = r.json().get("quoteSummary", {}).get("result", [{}])[0].get("financialData", {})
            target = fin.get("targetMeanPrice", {}).get("raw", 0)
            current = fin.get("currentPrice", {}).get("raw", 0)
            if target and current:
                result["analyst_target"] = round(target, 2)
                result["analyst_upside"] = round(((target - current) / current) * 100, 1)
    except: pass

    return result

# ═══════════════════════════════════════════════════════
# CAPA 4 — NOTICIAS Y SENTIMIENTO
# ═══════════════════════════════════════════════════════
def get_sentiment_data(ticker, sector):
    news_items = []
    sentiment_score = 0

    # Noticias específicas de la acción
    try:
        r = requests.get(
            f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=8&apiKey={NEWS_API_KEY}",
            timeout=10)
        if r.status_code == 200:
            for a in r.json().get("articles", [])[:6]:
                title = a.get("title", "")
                news_items.append(title)
                # Sentimiento básico por palabras clave
                positive = ["beat", "surge", "jump", "upgrade", "buy", "strong", "growth", "record", "partnership", "contract"]
                negative = ["miss", "fall", "drop", "downgrade", "sell", "weak", "loss", "cut", "investigation", "lawsuit"]
                title_lower = title.lower()
                for w in positive:
                    if w in title_lower: sentiment_score += 1
                for w in negative:
                    if w in title_lower: sentiment_score -= 1
    except: pass

    # RSS Yahoo Finance
    try:
        feed = feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US")
        for e in feed.entries[:4]:
            news_items.append(e.title)
    except: pass

    # Rendimiento del ETF sectorial
    sector_perf = None
    etf = SECTOR_ETFS.get(sector)
    if etf:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code == 200:
                closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(closes) >= 2:
                    sector_perf = round(((closes[-1]-closes[-2])/closes[-2])*100, 2)
        except: pass

    sentiment_label = "POSITIVO" if sentiment_score > 2 else "NEGATIVO" if sentiment_score < -2 else "NEUTRAL"

    return {
        "news": news_items[:8],
        "sentiment_score": sentiment_score,
        "sentiment_label": sentiment_label,
        "sector_perf": sector_perf,
    }

# ═══════════════════════════════════════════════════════
# CAPA 5 — MOMENTUM INSTITUCIONAL
# ═══════════════════════════════════════════════════════
def get_institutional_data(tech_data):
    """Infiere actividad institucional desde volumen y precio."""
    institutional_signal = "NEUTRAL"
    confidence_boost = 0

    vol_ratio = tech_data.get("vol_ratio", 1)
    obv_trend = tech_data.get("obv_trend", "NEUTRAL")
    change_pct = tech_data.get("change_pct", 0)
    price = tech_data.get("price", 0)
    vwap = tech_data.get("vwap", price)

    # Volumen masivo con precio subiendo = institucional comprando
    if vol_ratio > 2.5 and change_pct > 3:
        institutional_signal = "COMPRA INSTITUCIONAL PROBABLE"
        confidence_boost += 5
    # Volumen masivo con precio bajando = institucional vendiendo
    elif vol_ratio > 2.5 and change_pct < -3:
        institutional_signal = "VENTA INSTITUCIONAL PROBABLE"
        confidence_boost += 5
    # Precio por encima de VWAP con volumen alto = presión compradora
    elif price > vwap and vol_ratio > 1.5 and change_pct > 0:
        institutional_signal = "PRESION COMPRADORA"
        confidence_boost += 3
    elif price < vwap and vol_ratio > 1.5 and change_pct < 0:
        institutional_signal = "PRESION VENDEDORA"
        confidence_boost += 3

    # OBV confirma dirección
    if obv_trend == "ACUMULACION" and change_pct > 0:
        confidence_boost += 2
    elif obv_trend == "DISTRIBUCION" and change_pct < 0:
        confidence_boost += 2

    return {
        "institutional_signal": institutional_signal,
        "confidence_boost": confidence_boost,
    }

# ═══════════════════════════════════════════════════════
# CAPA 6 — IA SINTETIZA CON CONVICCIÓN
# ═══════════════════════════════════════════════════════
def analyze_with_ai(tech, fund, sent, inst):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    fg = market_context["fear_greed"]
    fg_label = ("PANICO EXTREMO" if fg < 20 else "Miedo" if fg < 40
                else "Neutral" if fg < 60 else "Codicia" if fg < 80 else "EUFORIA")

    news_text  = "\n".join(f"- {h}" for h in sent.get("news", [])[:6]) if sent.get("news") else "- Sin noticias"
    macro_text = "\n".join(f"- {h}" for h in market_context.get("macro_news", [])[:5])
    econ_text  = "\n".join(f"- {h}" for h in market_context.get("economic_events", [])[:3])

    rec_map = {"strongBuy": "COMPRA FUERTE", "buy": "COMPRAR", "hold": "MANTENER",
               "sell": "VENDER", "strongSell": "VENTA FUERTE"}
    rec_text = rec_map.get(fund.get("rec_key", "hold"), "MANTENER")

    analyst_text = (f"Precio objetivo analistas: ${fund.get('analyst_target')} "
                    f"({'+'if (fund.get('analyst_upside') or 0)>=0 else ''}{fund.get('analyst_upside')}% upside)"
                    if fund.get("analyst_target") else "Sin precio objetivo disponible")

    earnings_text = (f"EARNINGS EN {fund['earnings_days']} DIAS — "
                     f"Ha batido estimaciones {fund.get('earnings_beats',0)}/4 ultimos trimestres"
                     if fund.get("earnings_days") is not None else "Sin earnings proximos")

    insider_text = (f"{fund.get('insider_buys',0)} compras / {fund.get('insider_sells',0)} ventas insiders (30d)")

    prompt = f"""Eres el mejor analista cuantitativo del mundo. Tu objetivo es predecir con maxima precision la direccion, precio objetivo y timing de esta accion.

PRIORIDAD 1: Acertar la direccion (sube o baja)
PRIORIDAD 2: Acertar el precio objetivo
PRIORIDAD 3: Acertar el timing
PRIORIDAD 4: Stop loss preciso

Confianza minima para recomendar: {CONF_NORMAL}%. Si no hay conviction real: NO_SIGNAL.

━━━ CAPA 1: CONTEXTO MACRO ━━━
Fear & Greed: {fg}/100 — {fg_label}
S&P500 hoy: {'+' if market_context['sp500_change']>=0 else ''}{market_context['sp500_change']}%
VIX: {market_context['vix']} {'— ALTA VOLATILIDAD' if market_context['vix'] > 25 else '— mercado calmado'}
Macro: {macro_text}
Eventos economicos: {econ_text}

━━━ CAPA 2: TECNICO COMPLETO — {tech['ticker']} ({tech['name']}) ━━━
Precio: ${tech['price']} ({'+' if tech['change_pct']>=0 else ''}{tech['change_pct']}% hoy)
Sector: {tech['sector']} | ETF sectorial: {sent.get('sector_perf','N/D')}% hoy

TENDENCIAS:
Diario ({tech['daily_trend']}) | Semanal ({tech['weekly_trend']}) | Mensual ({tech['monthly_trend']})
Confluencia: {tech['tf_confluence']}
Estructura de precio: {tech['structure']}

MEDIAS MOVILES:
SMA20: ${tech['sma20']} | SMA50: ${tech['sma50']} | SMA200: ${tech.get('sma200','N/D')}
VWAP: ${tech['vwap']} | Precio {'SOBRE' if tech['price']>tech['vwap'] else 'BAJO'} VWAP

MOMENTUM:
RSI(14): {tech['rsi']} {'— SOBREVENTA EXTREMA' if tech['rsi']<25 else '— sobreventa' if tech['rsi']<32 else '— SOBRECOMPRA EXTREMA' if tech['rsi']>75 else '— sobrecompra' if tech['rsi']>68 else ''}
MACD: {tech['macd']} vs Signal {tech['macd_signal']} — {'ALCISTA' if tech['macd_bullish'] else 'BAJISTA'}
Estocástico K:{tech['stoch_k']} D:{tech['stoch_d']} {'— SOBREVENTA' if tech['stoch_k']<20 else '— SOBRECOMPRA' if tech['stoch_k']>80 else ''}
ROC 5d: {'+' if tech['roc5']>=0 else ''}{tech['roc5']}% | ROC 10d: {'+' if tech['roc10']>=0 else ''}{tech['roc10']}%
Momentum 1m: {'+' if tech['mom1m']>=0 else ''}{tech['mom1m']}% | 3m: {'+' if tech['mom3m']>=0 else ''}{tech['mom3m']}% | 6m: {'+' if tech['mom6m']>=0 else ''}{tech['mom6m']}%

VOLUMEN:
Ratio vs media: {tech['vol_ratio']}x {'— INSTITUCIONAL MASIVO' if tech['vol_ratio']>3 else '— elevado' if tech['vol_ratio']>1.5 else ''}
OBV: {tech['obv_trend']}
ATR diario: ${tech['atr']} | Volatilidad: {tech['vol20']}%

NIVELES CLAVE:
Fibonacci: 23.6%=${tech['fib236']} | 38.2%=${tech['fib382']} | 50%=${tech['fib500']} | 61.8%=${tech['fib618']}
Soporte probado: ${tech['rl']} ({tech['support_touches']} toques) | Resistencia: ${tech['rh']}
Min 52s: ${tech['l52']} ({'+' if tech['dist_l']>=0 else ''}{tech['dist_l']}%) | Max 52s: ${tech['h52']} ({tech['dist_h']}%)

━━━ CAPA 3: FUNDAMENTAL ━━━
P/E Forward: {fund.get('pe_ratio',0)} | Deuda/Equity: {fund.get('debt_equity',0)}
Crecimiento ingresos: {'+' if (fund.get('revenue_growth') or 0)>=0 else ''}{fund.get('revenue_growth',0)}%
Margen beneficio: {fund.get('profit_margins',0)}%
Short interest: {fund.get('short_interest',0)}% {'— POSIBLE SHORT SQUEEZE' if (fund.get('short_interest') or 0)>20 else ''}
{earnings_text}
{insider_text}

━━━ CAPA 4: SENTIMIENTO ━━━
Sentimiento noticias: {sent.get('sentiment_label','NEUTRAL')} (score: {sent.get('sentiment_score',0)})
Recomendacion analistas: {rec_text}
{analyst_text}
Noticias recientes:
{news_text}

━━━ CAPA 5: MOMENTUM INSTITUCIONAL ━━━
{inst['institutional_signal']}

━━━ INSTRUCCIONES ━━━
Analiza las 5 capas buscando CONVERGENCIA. Cuantas mas capas apunten en la misma direccion, mayor la confianza.
Usa ATR para calcular plazos realistas. Ancla objetivos en niveles de Fibonacci o soportes/resistencias probados.
Da 3 plazos concretos en dias o semanas (no etiquetas como "corto" o "largo").

Si hay oportunidad responde EXACTAMENTE asi (sin explicar las capas, solo el resultado):

SEÑAL: COMPRAR o VENDER
CONFIANZA: [X]%
🎯 ENTRADA ÓPTIMA: $[precio exacto]
📈 OBJETIVO: [+/-X%] → $[precio] en [X dias o X semanas] — [razon en 5 palabras]
🛑 STOP LOSS: $[precio en soporte/Fibonacci] — prob. stop: [X]%
⚖️ RATIO R/B: [X]:1
💬 POR QUÉ: [2-3 frases simples. Qué pasa, por qué va a moverse, por qué ahora]
⚡ CATALIZADOR: [factor concreto mas importante]
❌ INVALIDACIÓN: [precio o evento exacto]

Si no hay oportunidad clara: NO_SIGNAL"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"    Error IA: {e}")
        return "NO_SIGNAL"

# ═══════════════════════════════════════════════════════
# FORMATO ALERTAS DISCORD
# ═══════════════════════════════════════════════════════
def format_alert(tech, analysis, session, alert_num=None):
    now = datetime.now(SPAIN_TZ)
    signal = "COMPRAR"
    for line in analysis.split("\n"):
        if line.startswith("SEÑAL:"):
            signal = "VENDER" if "VENDER" in line else "COMPRAR"
            break

    conf_val = 0
    for line in analysis.split("\n"):
        if "CONFIANZA:" in line:
            try: conf_val = int(''.join(filter(str.isdigit, line)))
            except: pass
            break

    is_buy = signal == "COMPRAR"
    if conf_val >= CONF_EXCEPCIONAL: emoji = "⚡" if is_buy else "💀"
    elif conf_val >= CONF_FUERTE:    emoji = "🔥" if is_buy else "🔴"
    else:                             emoji = "🟢" if is_buy else "🔴"

    sign = "+" if tech["change_pct"] >= 0 else ""
    session_tag = f"  [{session}]" if session != "MERCADO" else ""
    num_tag = f"  │  {alert_num}" if alert_num else ""

    # Limpiar respuesta: quitar SEÑAL y CONFIANZA del cuerpo (ya están en el header)
    skip = {"SEÑAL:", "CONFIANZA:"}
    clean = "\n".join(l for l in analysis.split("\n")
                       if not any(l.startswith(s) for s in skip)).strip()

    return f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
{emoji}  **{signal}  —  {tech['ticker']}**{session_tag}{num_tag}
{tech['name']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰  **${tech['price']}**  ({sign}{tech['change_pct']}% hoy)  ·  {conf_val}% confianza
━━━━━━━━━━━━━━━━━━━━━━━━━━━
{clean}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐  {now.strftime('%H:%M  %d/%m/%Y')} hora España"""

# ═══════════════════════════════════════════════════════
# ANÁLISIS PROFUNDO
# ═══════════════════════════════════════════════════════
def deep_analyze(ticker, name, sector, urgency=0):
    """Análisis completo con las 6 capas."""
    print(f"  Análisis profundo: {ticker}...")

    # Obtener todos los datos
    tech = get_technical_data(ticker)
    if not tech or tech["score"] < 2:
        print(f"    {ticker}: score técnico insuficiente")
        return None

    fund = get_fundamental_data(ticker)
    sent = get_sentiment_data(ticker, sector)
    inst = get_institutional_data(tech)

    # Boost de confianza por momentum institucional
    confidence_boost = inst.get("confidence_boost", 0)

    analysis = analyze_with_ai(tech, fund, sent, inst)

    if "NO_SIGNAL" in analysis:
        # Marcar como analizada pero sin señal
        watch_signals[ticker] = {
            "score": tech["score"],
            "last_analyzed": datetime.now().isoformat(),
            "developing": False
        }
        save_state()
        return None

    # Extraer confianza y ajustar con boost
    conf = CONF_NORMAL
    for line in analysis.split("\n"):
        if "CONFIANZA:" in line:
            try: conf = int(''.join(filter(str.isdigit, line)))
            except: pass
            break
    conf = min(conf + confidence_boost, 99)

    if conf < CONF_NORMAL:
        print(f"    {ticker}: confianza {conf}% insuficiente")
        return None

    # Control de niveles
    signal = "COMPRAR"
    for line in analysis.split("\n"):
        if line.startswith("SEÑAL:"):
            signal = "VENDER" if "VENDER" in line else "COMPRAR"
            break

    if signal == "VENDER" and sells_today() >= MAX_SELLS_DIA:
        print(f"    {ticker}: venta descartada (limite diario)")
        return None

    if conf >= CONF_FUERTE and conf < CONF_EXCEPCIONAL and fuertes_today() >= MAX_FUERTES_DIA:
        print(f"    {ticker}: señal fuerte descartada (limite diario de fuertes)")
        return None

    # Actualizar estado de vigilancia
    watch_signals[ticker] = {
        "score": tech["score"],
        "last_analyzed": datetime.now().isoformat(),
        "developing": conf >= CONF_FUERTE
    }
    save_state()

    return {"tech": tech, "analysis": analysis, "conf": conf, "signal": signal}

# ═══════════════════════════════════════════════════════
# CICLO DE VIGILANCIA RÁPIDA (cada 5 min)
# ═══════════════════════════════════════════════════════
def get_session(now):
    m = now.hour * 60 + now.minute
    if 900 <= m < 930:   return "PREMARKET"
    if 930 <= m < 1380:  return "MERCADO"
    if 1380 <= m < 1440: return "AFTERHOURS"
    return "MERCADO"

def watch_cycle():
    """Ciclo rápido cada 5 minutos. Solo vigilancia, sin IA."""
    now = datetime.now(SPAIN_TZ)
    if now.hour < 9 or now.hour >= 23: return

    result = quick_scan()
    if not result: return
    urgent, developing = result

    session = get_session(now)
    to_analyze = []

    # Añadir urgentes que no hayan sido analizados hoy
    for item in urgent:
        ticker = item["ticker"]
        last = watch_signals.get(ticker, {}).get("last_analyzed")
        if last:
            last_dt = datetime.fromisoformat(last)
            if (datetime.now() - last_dt).seconds < 3600: continue  # No re-analizar en menos de 1h
        to_analyze.append((ticker, item.get("name", ticker), item.get("sector", "Unknown"), item.get("urgency", 0)))

    # Añadir acciones con señal en desarrollo
    for ticker in developing:
        if ticker not in [t[0] for t in to_analyze]:
            to_analyze.append((ticker, ticker, "Unknown", 0))

    # Añadir acciones del universo que llevan más de 24h sin analizar (rotación)
    not_analyzed = [t for t in UNIVERSE
                    if t not in watch_signals or
                    (datetime.now() - datetime.fromisoformat(watch_signals[t].get("last_analyzed", "2000-01-01"))).total_seconds() > 86400]
    sample = random.sample(not_analyzed, min(10, len(not_analyzed)))
    for ticker in sample:
        if ticker not in [t[0] for t in to_analyze]:
            to_analyze.append((ticker, ticker, "Unknown", 0))

    if not to_analyze:
        return

    print(f"\n[{now.strftime('%H:%M')} ES] Analizando {len(to_analyze)} acciones en profundidad...")

    alert_count = 0
    for ticker, name, sector, urgency in to_analyze[:8]:  # Max 8 análisis profundos por ciclo
        if already_alerted(ticker): continue

        result = deep_analyze(ticker, name, sector, urgency)
        if not result:
            time.sleep(1)
            continue

        tech     = result["tech"]
        analysis = result["analysis"]
        conf     = result["conf"]
        signal   = result["signal"]

        alert_num = alert_count + 1
        msg = format_alert(tech, analysis, session, alert_num=alert_num)
        send_alert(msg)
        alerts_sent[ticker] = datetime.now()
        alert_count += 1

        nivel = "EXCEPCIONAL ⚡" if conf >= CONF_EXCEPCIONAL else "FUERTE 🔥" if conf >= CONF_FUERTE else "NORMAL 🟢"
        print(f"    {ticker}: ALERTA {nivel} ({signal}, {conf}%)")
        send_log(f"✅ Alerta {nivel}: {ticker} — {signal} — Confianza {conf}%")
        time.sleep(3)

    if alert_count > 0:
        print(f"  {alert_count} alertas enviadas")


# ═══════════════════════════════════════════════════════
# CANAL ACIERTOS
# ═══════════════════════════════════════════════════════
def check_aciertos():
    """Revisa predicciones pendientes y publica en #aciertos-bot las que se cumplieron."""
    headers = {"User-Agent": "Mozilla/5.0"}
    for p in predictions:
        if p["result"] != "pending": continue
        date = datetime.fromisoformat(p["date"])
        days_passed = (datetime.now() - date).days
        if days_passed < 1: continue  # Demasiado pronto para evaluar
        ticker = p["ticker"]
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
                headers=headers, timeout=10)
            if r.status_code != 200: continue
            closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close",[]) if c]
            if not closes: continue
            current = closes[-1]
            entry   = p.get("entry", current)
            target  = p.get("target_short") or p.get("target", entry * 1.1)
            stop    = p.get("stop", entry * 0.93)
            signal  = p.get("signal", "COMPRAR")
            change  = ((current - entry) / entry) * 100
            target_change = ((target - entry) / entry) * 100

            hit_target = (signal == "COMPRAR" and current >= target) or (signal == "VENDER" and current <= target)
            hit_stop   = (signal == "COMPRAR" and current <= stop)   or (signal == "VENDER" and current >= stop)

            if hit_target:
                p["result"] = "win"
                p["exit_price"] = current
                save_state()
                sign = "+" if change >= 0 else ""
                send_acierto(
                    f"✅  **ACIERTO — {ticker}**\n"
                    f"Entrada: ${entry:.2f}  →  Actual: ${current:.2f}  ({sign}{change:.1f}%)\n"
                    f"Objetivo alcanzado: ${target:.2f}  en {days_passed} días\n"
                    f"Confianza original: {p.get('confidence',0)}%"
                )
            elif hit_stop:
                p["result"] = "loss"
                p["exit_price"] = current
                save_state()
                send_acierto(
                    f"❌  **STOP — {ticker}**\n"
                    f"Entrada: ${entry:.2f}  →  Stop: ${current:.2f}  ({change:.1f}%)\n"
                    f"Stop loss activado en {days_passed} días"
                )
            elif days_passed > p.get("days", 30) * 1.5:
                # Tiempo expirado sin resultado claro
                p["result"] = "expired"
                p["exit_price"] = current
                save_state()
        except: continue

# ═══════════════════════════════════════════════════════
# RESUMEN SEMANAL
# ═══════════════════════════════════════════════════════
def send_weekly_summary():
    now = datetime.now(SPAIN_TZ)
    total   = len([p for p in predictions if p["result"] != "pending"])
    wins    = len([p for p in predictions if p["result"] == "win"])
    losses  = len([p for p in predictions if p["result"] == "loss"])
    pending = len([p for p in predictions if p["result"] == "pending"])
    rate    = round(wins/total*100, 1) if total > 0 else 0
    send_log(f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 RESUMEN StockBot — {now.strftime('%d/%m/%Y')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ Acertadas: {wins}  ❌ Falladas: {losses}  ⏳ Pendientes: {pending}
🎯 Tasa de acierto: {rate}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━""")

# ═══════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════
def main():
    load_state()
    now = datetime.now(SPAIN_TZ)
    print(f"StockBot Pro v2 iniciado — {now.strftime('%H:%M %d/%m/%Y')}")
    send_log(
        f"🤖 **StockBot Pro v2 activado** — {now.strftime('%H:%M %d/%m/%Y')}\n"
        f"📡 Vigilancia cada 5 min | Análisis profundo solo cuando hay señal\n"
        f"⚡ 6 capas: Macro + Técnico + Fundamental + Sentimiento + Institucional + IA\n"
        f"🟢 Normal {CONF_NORMAL}%+ | 🔥 Fuerte {CONF_FUERTE}%+ | ⚡ Excepcional {CONF_EXCEPCIONAL}%+"
    )

    update_market_context()
    watch_cycle()

    schedule.every(5).minutes.do(watch_cycle)
    schedule.every(1).hours.do(check_aciertos)
    schedule.every().day.at("09:00").do(update_market_context)
    schedule.every().monday.at("09:00").do(send_weekly_summary)
    schedule.every().thursday.at("09:00").do(send_weekly_summary)

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
