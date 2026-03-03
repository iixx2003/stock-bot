import os
import time
import json
import schedule
import requests
import feedparser
import anthropic
from datetime import datetime, timedelta
import pytz

# --- CONFIG ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY")

SPAIN_TZ = pytz.timezone("Europe/Madrid")
NY_TZ = pytz.timezone("America/New_York")

MIN_PRICE = 5.0
MIN_VOLUME = 500_000
MIN_CONFIDENCE = 75

# Memoria del bot
alerts_sent = {}        # {ticker: datetime}
predictions = []        # Lista de predicciones para seguimiento
failed_patterns = {}    # {pattern_key: fail_count}

# Contexto macro del dia (se actualiza cada mañana)
market_context = {
    "fear_greed": 50,
    "sp500_change": 0,
    "vix": 15,
    "macro_news": [],
    "economic_events": [],
    "updated_at": None
}

# Mapa de ETFs sectoriales
SECTOR_ETFS = {
    "Technology": "XLK", "Healthcare": "XLV", "Financials": "XLF",
    "Energy": "XLE", "Consumer Cyclical": "XLY", "Industrials": "XLI",
    "Communication Services": "XLC", "Consumer Defensive": "XLP",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
    "Semiconductor": "SMH", "Biotech": "XBI", "Banks": "KBE"
}

PREDICTIONS_FILE = "/app/predictions.json"


# ═══════════════════════════════════════
# DISCORD
# ═══════════════════════════════════════
def send_discord(message):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    if len(message) > 1900:
        message = message[:1900] + "..."
    try:
        resp = requests.post(url, json={"content": message}, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error Discord: {resp.status_code}")
    except Exception as e:
        print(f"Error Discord: {e}")


# ═══════════════════════════════════════
# PERSISTENCIA DE PREDICCIONES
# ═══════════════════════════════════════
def load_predictions():
    global predictions
    try:
        if os.path.exists(PREDICTIONS_FILE):
            with open(PREDICTIONS_FILE, "r") as f:
                predictions = json.load(f)
    except:
        predictions = []

def save_predictions():
    try:
        with open(PREDICTIONS_FILE, "w") as f:
            json.dump(predictions, f)
    except:
        pass

def add_prediction(ticker, signal, entry, target, stop, confidence, days):
    pred = {
        "ticker": ticker,
        "signal": signal,
        "entry": entry,
        "target": target,
        "stop": stop,
        "confidence": confidence,
        "days": days,
        "date": datetime.now().isoformat(),
        "result": "pending",
        "exit_price": None
    }
    predictions.append(pred)
    save_predictions()

def check_predictions():
    """Comprueba el resultado de predicciones pasadas"""
    updated = False
    for pred in predictions:
        if pred["result"] != "pending":
            continue
        pred_date = datetime.fromisoformat(pred["date"])
        if datetime.now() - pred_date < timedelta(days=pred.get("days", 7)):
            continue
        # Obtener precio actual
        data = get_stock_price(pred["ticker"])
        if not data:
            continue
        price = data["price"]
        pred["exit_price"] = price
        if pred["signal"] == "COMPRAR":
            if price >= pred["target"]:
                pred["result"] = "win"
            elif price <= pred["stop"]:
                pred["result"] = "loss"
                register_failed_pattern(pred)
            else:
                pred["result"] = "partial"
        else:
            if price <= pred["target"]:
                pred["result"] = "win"
            elif price >= pred["stop"]:
                pred["result"] = "loss"
                register_failed_pattern(pred)
            else:
                pred["result"] = "partial"
        updated = True
    if updated:
        save_predictions()

def register_failed_pattern(pred):
    """Registra patrones que fallan para aprender"""
    key = f"{pred['signal']}_{pred.get('rsi_zone', 'unknown')}_{pred.get('sector', 'unknown')}"
    failed_patterns[key] = failed_patterns.get(key, 0) + 1
    print(f"  Patrón fallido registrado: {key} (total: {failed_patterns[key]})")

def get_stock_price(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        result = resp.json().get("chart", {}).get("result", [])
        if not result:
            return None
        closes = [c for c in result[0]["indicators"]["quote"][0].get("close", []) if c]
        return {"price": closes[-1]} if closes else None
    except:
        return None


# ═══════════════════════════════════════
# CONTEXTO MACRO
# ═══════════════════════════════════════
def get_fear_greed():
    try:
        resp = requests.get("https://fear-and-greed-index.p.rapidapi.com/v1/fgi",
            headers={"X-RapidAPI-Key": "free", "X-RapidAPI-Host": "fear-and-greed-index.p.rapidapi.com"},
            timeout=10)
        if resp.status_code == 200:
            return resp.json().get("fgi", {}).get("now", {}).get("value", 50)
    except:
        pass
    # Fallback: CNN Fear & Greed via scraping alternativo
    try:
        resp = requests.get("https://api.alternative.me/fng/", timeout=10)
        if resp.status_code == 200:
            return int(resp.json()["data"][0]["value"])
    except:
        pass
    return 50

def get_market_data():
    """Obtiene SPY, VIX y datos macro básicos"""
    result = {"sp500_change": 0, "vix": 15}
    try:
        for ticker, key in [("SPY", "sp500_change"), ("^VIX", "vix")]:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
            headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                r = resp.json().get("chart", {}).get("result", [])
                if r:
                    closes = [c for c in r[0]["indicators"]["quote"][0].get("close", []) if c]
                    if len(closes) >= 2:
                        if key == "sp500_change":
                            result[key] = round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2)
                        else:
                            result[key] = round(closes[-1], 1)
            time.sleep(0.5)
    except Exception as e:
        print(f"  Error market data: {e}")
    return result

def get_macro_news():
    """Noticias macro via NewsAPI + RSS"""
    news = []
    # NewsAPI
    try:
        url = f"https://newsapi.org/v2/top-headlines?category=business&language=en&pageSize=10&apiKey={NEWS_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            articles = resp.json().get("articles", [])
            for a in articles[:8]:
                news.append(a.get("title", ""))
    except Exception as e:
        print(f"  Error NewsAPI: {e}")
    # RSS fallback
    rss_feeds = [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.marketwatch.com/marketwatch/realtimeheadlines/",
    ]
    for feed_url in rss_feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:4]:
                news.append(entry.title)
        except:
            pass
    return news[:15]

def get_economic_calendar():
    """Eventos económicos importantes de la semana"""
    events = []
    try:
        url = f"https://newsapi.org/v2/everything?q=Federal+Reserve+OR+inflation+OR+CPI+OR+jobs+report&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            for a in resp.json().get("articles", [])[:5]:
                events.append(a.get("title", ""))
    except:
        pass
    return events

def update_market_context():
    """Actualiza el contexto macro completo cada mañana"""
    global market_context
    print("  Actualizando contexto macro del día...")
    fg = get_fear_greed()
    mkt = get_market_data()
    macro_news = get_macro_news()
    econ_events = get_economic_calendar()
    market_context = {
        "fear_greed": fg,
        "sp500_change": mkt["sp500_change"],
        "vix": mkt["vix"],
        "macro_news": macro_news,
        "economic_events": econ_events,
        "updated_at": datetime.now(SPAIN_TZ).strftime("%H:%M")
    }
    fg_label = "PÁNICO EXTREMO 🔴" if fg < 20 else "Miedo 🟡" if fg < 40 else "Neutral ⚪" if fg < 60 else "Codicia 🟡" if fg < 80 else "EUFORIA EXTREMA 🔴"
    print(f"  Fear&Greed: {fg} ({fg_label}) | S&P500: {mkt['sp500_change']}% | VIX: {mkt['vix']}")


# ═══════════════════════════════════════
# DATOS DE ACCIONES
# ═══════════════════════════════════════
def get_trending_tickers():
    tickers = {}
    screeners = [
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=50",
    ]
    headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0", "Accept": "application/json"}
    for url in screeners:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                quotes = resp.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
                for q in quotes:
                    symbol = q.get("symbol", "")
                    price = q.get("regularMarketPrice", 0)
                    volume = q.get("regularMarketVolume", 0)
                    if symbol and "." not in symbol and len(symbol) <= 5 and price >= MIN_PRICE and volume >= MIN_VOLUME:
                        tickers[symbol] = {
                            "price": price,
                            "change_pct": q.get("regularMarketChangePercent", 0),
                            "volume": volume,
                            "avg_volume": max(q.get("averageDailyVolume3Month", 1), 1),
                            "name": q.get("longName", symbol),
                            "sector": q.get("sector", "Unknown"),
                            "market_cap": q.get("marketCap", 0),
                        }
        except Exception as e:
            print(f"  Error screener: {e}")
        time.sleep(1)
    return tickers

def get_stock_data(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y"
        headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0", "Accept": "application/json"}
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            return None
        result = resp.json().get("chart", {}).get("result", [])
        if not result:
            return None
        result = result[0]
        meta = result.get("meta", {})
        q = result["indicators"]["quote"][0]
        closes = [c for c in q.get("close", []) if c is not None]
        volumes = [v for v in q.get("volume", []) if v is not None]
        highs = [h for h in q.get("high", []) if h is not None]
        lows = [l for l in q.get("low", []) if l is not None]

        if len(closes) < 20:
            return None

        price = closes[-1]
        change_pct = ((price - closes[-2]) / closes[-2]) * 100

        # Medias moviles
        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        sma200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None

        # RSI 14
        gains, losses = [], []
        for i in range(1, 15):
            diff = closes[-i] - closes[-i - 1]
            (gains if diff >= 0 else losses).append(abs(diff))
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0.001
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        # Volumen
        avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

        # ATR 14 dias
        atr_values = []
        for i in range(1, min(15, len(closes))):
            hl = highs[-i] - lows[-i] if highs and lows else 0
            hc = abs(highs[-i] - closes[-i - 1]) if highs else 0
            lc = abs(lows[-i] - closes[-i - 1]) if lows else 0
            atr_values.append(max(hl, hc, lc))
        atr = sum(atr_values) / len(atr_values) if atr_values else price * 0.02

        # Fibonacci sobre rango 52 semanas
        high_52w = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        low_52w = min(closes[-252:]) if len(closes) >= 252 else min(closes)
        fib_range = high_52w - low_52w
        fib_236 = high_52w - fib_range * 0.236
        fib_382 = high_52w - fib_range * 0.382
        fib_500 = high_52w - fib_range * 0.500
        fib_618 = high_52w - fib_range * 0.618

        # Volatilidad
        mean = sma20
        volatility = ((sum((c - mean) ** 2 for c in closes[-20:]) / 20) ** 0.5) / price * 100

        # Momentum
        momentum_1m = ((price - closes[-22]) / closes[-22] * 100) if len(closes) >= 22 else 0
        momentum_3m = ((price - closes[-66]) / closes[-66] * 100) if len(closes) >= 66 else 0

        # Soporte y resistencia recientes
        recent_high = max(highs[-20:]) if len(highs) >= 20 else price
        recent_low = min(lows[-20:]) if len(lows) >= 20 else price

        # Conteo de señales para pre-filtro
        signals = 0
        rsi_zone = "neutral"
        if rsi < 30:
            signals += 2
            rsi_zone = "oversold"
        elif rsi < 40:
            signals += 1
            rsi_zone = "near_oversold"
        elif rsi > 70:
            signals += 2
            rsi_zone = "overbought"
        elif rsi > 60:
            signals += 1
            rsi_zone = "near_overbought"
        if vol_ratio > 2.5:
            signals += 2
        elif vol_ratio > 1.8:
            signals += 1
        if abs(change_pct) > 5:
            signals += 2
        elif abs(change_pct) > 3:
            signals += 1
        if sma50 and ((price > sma50 and price > sma20) or (price < sma50 and price < sma20)):
            signals += 1
        if abs(momentum_1m) > 15:
            signals += 1

        # Penalizar patrones que han fallado
        sector = meta.get("sector", "Unknown")
        pattern_key = f"{'COMPRAR' if change_pct > 0 else 'VENDER'}_{rsi_zone}_{sector}"
        if failed_patterns.get(pattern_key, 0) >= 3:
            signals -= 2
            print(f"    {ticker}: penalizado por patrón fallido ({pattern_key})")

        return {
            "ticker": ticker,
            "name": meta.get("longName", ticker),
            "sector": sector,
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            "sma20": round(sma20, 2),
            "sma50": round(sma50, 2) if sma50 else None,
            "sma200": round(sma200, 2) if sma200 else None,
            "rsi": round(rsi, 1),
            "rsi_zone": rsi_zone,
            "vol_ratio": round(vol_ratio, 2),
            "atr": round(atr, 2),
            "high_52w": round(high_52w, 2),
            "low_52w": round(low_52w, 2),
            "fib_236": round(fib_236, 2),
            "fib_382": round(fib_382, 2),
            "fib_500": round(fib_500, 2),
            "fib_618": round(fib_618, 2),
            "recent_high": round(recent_high, 2),
            "recent_low": round(recent_low, 2),
            "dist_from_high": round(((price - high_52w) / high_52w) * 100, 1),
            "dist_from_low": round(((price - low_52w) / low_52w) * 100, 1),
            "volatility": round(volatility, 1),
            "momentum_1m": round(momentum_1m, 1),
            "momentum_3m": round(momentum_3m, 1),
            "signals": max(signals, 0),
        }
    except Exception as e:
        return None

def get_earnings_date(ticker):
    """Comprueba si hay earnings próximos (1-21 dias)"""
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=calendarEvents"
        headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            earnings = resp.json().get("quoteSummary", {}).get("result", [{}])[0].get("calendarEvents", {}).get("earnings", {})
            dates = earnings.get("earningsDate", [])
            if dates:
                ts = dates[0].get("raw", 0)
                earning_dt = datetime.fromtimestamp(ts)
                days_until = (earning_dt - datetime.now()).days
                if 0 <= days_until <= 21:
                    return days_until
    except:
        pass
    return None

def get_short_interest(ticker):
    """Obtiene short interest via Yahoo Finance"""
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=defaultKeyStatistics"
        headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            stats = resp.json().get("quoteSummary", {}).get("result", [{}])[0].get("defaultKeyStatistics", {})
            short_pct = stats.get("shortPercentOfFloat", {}).get("raw", 0)
            return round(short_pct * 100, 1) if short_pct else 0
    except:
        pass
    return 0

def get_insider_activity(ticker):
    """Detecta compras de insiders recientes"""
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=insiderTransactions"
        headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            transactions = resp.json().get("quoteSummary", {}).get("result", [{}])[0].get("insiderTransactions", {}).get("transactions", [])
            recent_buys = 0
            for t in transactions[:10]:
                ts = t.get("startDate", {}).get("raw", 0)
                t_date = datetime.fromtimestamp(ts)
                if (datetime.now() - t_date).days <= 30:
                    if "Purchase" in t.get("transactionText", ""):
                        recent_buys += 1
            return recent_buys
    except:
        pass
    return 0

def get_split_announcement(ticker):
    """Detecta anuncios de splits"""
    try:
        url = f"https://newsapi.org/v2/everything?q={ticker}+stock+split&language=en&sortBy=publishedAt&pageSize=3&apiKey={NEWS_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            articles = resp.json().get("articles", [])
            for a in articles:
                pub_date = datetime.strptime(a.get("publishedAt", "")[:10], "%Y-%m-%d")
                if (datetime.now() - pub_date).days <= 14:
                    return True
    except:
        pass
    return False

def get_options_flow(ticker):
    """Detecta flujo inusual de opciones via volumen de calls"""
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=summaryDetail"
        headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            detail = resp.json().get("quoteSummary", {}).get("result", [{}])[0].get("summaryDetail", {})
            avg_vol = detail.get("averageVolume", {}).get("raw", 1)
            vol = detail.get("volume", {}).get("raw", 0)
            if avg_vol > 0 and vol / avg_vol > 3:
                return True
    except:
        pass
    return False

def get_sector_etf_performance(sector):
    """Obtiene rendimiento del ETF sectorial"""
    etf = SECTOR_ETFS.get(sector)
    if not etf:
        return None
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?interval=1d&range=5d"
        headers = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            result = resp.json().get("chart", {}).get("result", [])
            if result:
                closes = [c for c in result[0]["indicators"]["quote"][0].get("close", []) if c]
                if len(closes) >= 2:
                    return round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2)
    except:
        pass
    return None

def get_stock_news(ticker):
    news = []
    # NewsAPI para la accion especifica
    try:
        url = f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            for a in resp.json().get("articles", [])[:4]:
                news.append(a.get("title", ""))
    except:
        pass
    # RSS Yahoo Finance
    try:
        feed = feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US")
        for entry in feed.entries[:4]:
            news.append(entry.title)
    except:
        pass
    return news[:8]

def is_pump_dump(data, vol_ratio):
    """Detecta posible pump & dump"""
    return (vol_ratio > 5 and
            data.get("market_cap", float('inf')) < 500_000_000 and
            abs(data.get("change_pct", 0)) > 15)


# ═══════════════════════════════════════
# ANÁLISIS CON IA
# ═══════════════════════════════════════
def analyze_with_ai(data, news, extras):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    news_text = "\n".join(f"- {h}" for h in news) if news else "- Sin noticias recientes"
    macro_news_text = "\n".join(f"- {h}" for h in market_context.get("macro_news", [])[:6])
    econ_events_text = "\n".join(f"- {h}" for h in market_context.get("economic_events", [])[:3])

    fg = market_context.get("fear_greed", 50)
    fg_label = "PÁNICO EXTREMO" if fg < 20 else "Miedo" if fg < 40 else "Neutral" if fg < 60 else "Codicia" if fg < 80 else "EUFORIA EXTREMA"
    sp500 = market_context.get("sp500_change", 0)
    vix = market_context.get("vix", 15)

    trend_short = "ALCISTA" if data['price'] > data['sma20'] else "BAJISTA"
    trend_medium = ("ALCISTA" if data['sma50'] and data['price'] > data['sma50'] else "BAJISTA") if data['sma50'] else "N/D"
    trend_long = ("ALCISTA" if data['sma200'] and data['price'] > data['sma200'] else "BAJISTA") if data['sma200'] else "N/D"

    earnings_text = f"⚠️ EARNINGS EN {extras.get('earnings_days')} DÍAS" if extras.get('earnings_days') is not None else "Sin earnings próximos"
    short_text = f"{extras.get('short_interest', 0)}% en corto {'— POTENCIAL SHORT SQUEEZE' if extras.get('short_interest', 0) > 20 else ''}"
    insider_text = f"{extras.get('insider_buys', 0)} compras de insiders en 30 días {'— SEÑAL ALCISTA FUERTE' if extras.get('insider_buys', 0) >= 2 else ''}"
    sector_text = f"ETF sectorial ({data.get('sector', 'N/D')}): {extras.get('sector_perf', 'N/D')}% hoy"
    options_text = "⚡ FLUJO INUSUAL DE OPCIONES DETECTADO" if extras.get('unusual_options') else "Normal"
    split_text = "📢 SPLIT ANUNCIADO RECIENTEMENTE" if extras.get('split_announced') else "Sin anuncio"

    prompt = f"""Eres un analista cuantitativo de élite de un hedge fund top. Perfil del inversor: agresivo-moderado, busca oportunidades de +10% a +40%, acepta riesgo controlado, opera en corto/medio/largo plazo.

Tu análisis debe ser exhaustivo, preciso y accionable. Solo recomiendas cuando hay convergencia sólida de señales. El umbral mínimo de confianza es 75%.

━━━ CONTEXTO MACRO DEL DÍA ━━━
Fear & Greed Index: {fg}/100 — {fg_label}
S&P500 hoy: {'+' if sp500 >= 0 else ''}{sp500}%
VIX: {vix} {'— VOLATILIDAD ALTA, ser selectivo' if vix > 25 else '— mercado calmado'}

Noticias macro:
{macro_news_text}

Eventos económicos:
{econ_events_text}

━━━ DATOS COMPLETOS: {data['ticker']} — {data['name']} ━━━

PRECIO: ${data['price']} ({'+' if data['change_pct'] >= 0 else ''}{data['change_pct']}% hoy)
Sector: {data.get('sector', 'N/D')} | {sector_text}

TENDENCIAS:
Corto plazo (SMA20 ${data['sma20']}): {trend_short}
Medio plazo (SMA50 ${data['sma50'] if data['sma50'] else 'N/D'}): {trend_medium}
Largo plazo (SMA200 ${data['sma200'] if data['sma200'] else 'N/D'}): {trend_long}

MOMENTUM:
RSI(14): {data['rsi']} — {'SOBRECOMPRA EXTREMA' if data['rsi'] > 75 else 'sobrecompra' if data['rsi'] > 65 else 'SOBREVENTA EXTREMA' if data['rsi'] < 25 else 'sobreventa' if data['rsi'] < 35 else 'neutral'}
Volumen vs media: {data['vol_ratio']}x {'— ENTRADA INSTITUCIONAL MASIVA' if data['vol_ratio'] > 3 else '— volumen elevado' if data['vol_ratio'] > 2 else ''}
Momentum 1 mes: {'+' if data['momentum_1m'] >= 0 else ''}{data['momentum_1m']}%
Momentum 3 meses: {'+' if data['momentum_3m'] >= 0 else ''}{data['momentum_3m']}%
Volatilidad 20d: {data['volatility']}%
ATR diario: ${data['atr']} (movimiento medio diario)

NIVELES FIBONACCI:
23.6%: ${data['fib_236']} | 38.2%: ${data['fib_382']}
50.0%: ${data['fib_500']} | 61.8%: ${data['fib_618']}
Soporte reciente: ${data['recent_low']} | Resistencia reciente: ${data['recent_high']}
Mínimo 52s: ${data['low_52w']} (+{data['dist_from_low']}%) | Máximo 52s: ${data['high_52w']} ({data['dist_from_high']}%)

FACTORES FUNDAMENTALES:
{earnings_text}
Short interest: {short_text}
Insider trading: {insider_text}
Opciones: {options_text}
Split: {split_text}

NOTICIAS DE LA ACCIÓN:
{news_text}

━━━ INSTRUCCIONES ━━━
Analiza todo con rigor. Busca convergencia de señales técnicas, fundamentales y macro.
Confianza mínima para recomendar: 75%. Si hay dudas, NO_SIGNAL.
Usa el ATR para calcular plazos realistas (precio objetivo / ATR diario = días estimados).
Usa Fibonacci para anclar precio objetivo y stop loss a niveles reales.

Si hay oportunidad responde EXACTAMENTE en este formato:

SEÑAL: COMPRAR o VENDER
CONFIANZA: [X]%

🎯 ENTRADA ÓPTIMA: $[precio exacto]
📈 PREDICCIÓN: [+/-X%] → $[precio objetivo exacto basado en Fibonacci o resistencia/soporte]
⏱ PLAZO: [X días/semanas/meses — calculado con ATR]
🛑 STOP LOSS: $[precio exacto anclado en soporte/Fibonacci] — probabilidad de llegar: [X]%
⚖️ RATIO R/B: [X]:1

💬 POR QUÉ:
[3 frases en lenguaje simple y directo. Sin jerga técnica. Explica qué está pasando, por qué va a moverse y por qué ahora. Como si se lo explicaras a un amigo.]

⚡ CATALIZADOR PRINCIPAL: [el factor concreto más importante]
❌ INVALIDACIÓN: [precio exacto o evento que invalida la tesis]

Si no hay oportunidad: NO_SIGNAL"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"    Error AI: {e}")
        return "NO_SIGNAL"

def analyze_earnings_prediction(ticker, data, news, days_until):
    """Análisis específico pre-earnings"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    news_text = "\n".join(f"- {h}" for h in news) if news else "- Sin noticias"

    prompt = f"""Eres un analista experto en earnings de Wall Street. Analiza si esta acción subirá o bajará tras sus resultados trimestrales.

ACCIÓN: {ticker} — {data['name']}
Precio actual: ${data['price']}
Earnings en: {days_until} días
Momentum 1 mes: {'+' if data['momentum_1m'] >= 0 else ''}{data['momentum_1m']}%
Momentum 3 meses: {'+' if data['momentum_3m'] >= 0 else ''}{data['momentum_3m']}%
RSI: {data['rsi']}
Volumen: {data['vol_ratio']}x la media
Short interest: alto si hay muchos en corto

Noticias recientes:
{news_text}

Responde EXACTAMENTE así:

📅 EARNINGS EN {days_until} DÍAS — {ticker}

PREDICCIÓN POST-EARNINGS: SUBIRÁ / BAJARÁ
MOVIMIENTO ESPERADO: [+/-X%] → $[precio estimado]
CONFIANZA: [X]%

POR QUÉ:
[2-3 frases explicando qué se espera de los resultados, basado en el momentum, noticias del sector y contexto actual. Lenguaje simple.]

ESTRATEGIA: [qué hacer — entrar antes, esperar confirmación, evitar]"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text.strip()
    except:
        return None


# ═══════════════════════════════════════
# FORMATO MENSAJES DISCORD
# ═══════════════════════════════════════
def format_alert(ticker, data, analysis, session_label, extras):
    now_spain = datetime.now(SPAIN_TZ)
    is_buy = "COMPRAR" in analysis.split("\n")[0]
    sign = "+" if data['change_pct'] >= 0 else ""

    # Detectar nivel de confianza para emoji especial
    confidence_emoji = "🟢" if is_buy else "🔴"
    try:
        for line in analysis.split("\n"):
            if "CONFIANZA:" in line:
                conf_val = int(''.join(filter(str.isdigit, line)))
                if conf_val >= 85:
                    confidence_emoji = "🔥" if is_buy else "💀"
                break
    except:
        pass

    earnings_line = f"📅 Earnings en {extras.get('earnings_days')} días\n" if extras.get('earnings_days') is not None else ""
    session_tag = f"  [{session_label}]" if session_label != "MERCADO" else ""

    return f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
{confidence_emoji}  **{'COMPRAR' if is_buy else 'VENDER'}  —  {ticker}**{session_tag}
{data['name']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰  Precio actual:  **${data['price']}**  ({sign}{data['change_pct']}% hoy)
{earnings_line}━━━━━━━━━━━━━━━━━━━━━━━━━━━
{analysis}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐  {now_spain.strftime('%H:%M  %d/%m/%Y')} hora España"""

def format_pump_dump_alert(ticker, data, session_label):
    now_spain = datetime.now(SPAIN_TZ)
    sign = "+" if data['change_pct'] >= 0 else ""
    return f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀  **MOVIMIENTO ESPECULATIVO  —  {ticker}**  [{session_label}]
{data['name']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰  Precio: **${data['price']}**  ({sign}{data['change_pct']}% hoy)
📊  Volumen: {data['vol_ratio']}x la media — ANORMAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ **POSIBLE PUMP & DUMP DETECTADO**
Este movimiento parece especulativo sin fundamento sólido.

🎯 Entrada rápida posible: ${data['price']}
📈 Objetivo de salida rápida: ${round(data['price'] * 1.08, 2)} (+8%)
🛑 Stop loss inmediato: ${round(data['price'] * 0.95, 2)} (-5%)
⚖️ Sal rápido si el volumen cae bruscamente

⚠️ RIESGO ALTO — puede caer tan rápido como sube
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐  {now_spain.strftime('%H:%M  %d/%m/%Y')} hora España"""


# ═══════════════════════════════════════
# RESUMEN SEMANAL
# ═══════════════════════════════════════
def send_weekly_summary():
    check_predictions()
    now_spain = datetime.now(SPAIN_TZ)

    total = len([p for p in predictions if p["result"] != "pending"])
    wins = len([p for p in predictions if p["result"] == "win"])
    losses = len([p for p in predictions if p["result"] == "loss"])
    partials = len([p for p in predictions if p["result"] == "partial"])
    pending = len([p for p in predictions if p["result"] == "pending"])

    win_rate = round((wins / total * 100) if total > 0 else 0, 1)

    win_pcts = []
    loss_pcts = []
    for p in predictions:
        if p["exit_price"] and p["entry"]:
            pct = ((p["exit_price"] - p["entry"]) / p["entry"]) * 100
            if p["signal"] == "VENDER":
                pct = -pct
            if p["result"] == "win":
                win_pcts.append(pct)
            elif p["result"] == "loss":
                loss_pcts.append(pct)

    avg_win = round(sum(win_pcts) / len(win_pcts), 1) if win_pcts else 0
    avg_loss = round(sum(loss_pcts) / len(loss_pcts), 1) if loss_pcts else 0

    best = max(predictions, key=lambda p: ((p["exit_price"] or p["entry"]) - p["entry"]) / p["entry"] if p["entry"] else 0, default=None)
    worst = min(predictions, key=lambda p: ((p["exit_price"] or p["entry"]) - p["entry"]) / p["entry"] if p["entry"] else 0, default=None)

    patterns_text = ""
    if failed_patterns:
        top_failures = sorted(failed_patterns.items(), key=lambda x: x[1], reverse=True)[:3]
        patterns_text = "\n🧠 **Patrones que estoy aprendiendo a evitar:**\n"
        for pattern, count in top_failures:
            patterns_text += f"• {pattern}: {count} fallos\n"

    msg = f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊  **RESUMEN SEMANAL — StockBot**
{now_spain.strftime('%d/%m/%Y')} hora España
━━━━━━━━━━━━━━━━━━━━━━━━━━━
📈 Alertas totales enviadas: {len(predictions)}
✅ Acertadas (objetivo alcanzado): {wins}
❌ Falladas (stop loss tocado): {losses}
🔄 Parciales (en curso): {partials}
⏳ Pendientes (plazo no cumplido): {pending}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 **Tasa de acierto: {win_rate}%**
📊 Ganancia media en acertadas: +{avg_win}%
📉 Pérdida media en falladas: {avg_loss}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━
{'🏆 Mejor alerta: ' + best['ticker'] + ' +' + str(round(((best['exit_price'] or best['entry']) - best['entry']) / best['entry'] * 100, 1)) + '%' if best else ''}
{'💔 Peor alerta: ' + worst['ticker'] + ' ' + str(round(((worst['exit_price'] or worst['entry']) - worst['entry']) / worst['entry'] * 100, 1)) + '%' if worst else ''}
{patterns_text}━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

    send_discord(msg)


# ═══════════════════════════════════════
# ESCANEO PRINCIPAL
# ═══════════════════════════════════════
def get_session_label(now_spain):
    hour = now_spain.hour
    minute = now_spain.minute
    total_minutes = hour * 60 + minute
    if 900 <= total_minutes < 930:
        return "PREMARKET"
    elif 930 <= total_minutes < 1380:
        return "MERCADO"
    elif 1380 <= total_minutes < 1440:
        return "AFTERHOURS"
    return "MERCADO"

def already_alerted(ticker):
    if ticker in alerts_sent:
        if datetime.now() - alerts_sent[ticker] < timedelta(hours=24):
            return True
    return False

def scan_market():
    now_spain = datetime.now(SPAIN_TZ)
    hour = now_spain.hour

    if hour < 9 or hour >= 23:
        return

    session = get_session_label(now_spain)
    print(f"\n[{now_spain.strftime('%H:%M')} ES | {session}] Escaneando...")

    # Actualizar contexto macro una vez al dia a las 9am
    if hour == 9 and now_spain.minute < 30:
        update_market_context()

    trending = get_trending_tickers()
    print(f"  {len(trending)} acciones encontradas")

    candidates = []
    for ticker, basic in trending.items():
        if already_alerted(ticker):
            continue

        # Detectar pump & dump
        vol_ratio = basic['volume'] / basic['avg_volume'] if basic['avg_volume'] > 0 else 1
        if is_pump_dump(basic, vol_ratio):
            pump_data = {**basic, "vol_ratio": round(vol_ratio, 2), "change_pct": basic['change_pct']}
            msg = format_pump_dump_alert(ticker, pump_data, session)
            send_discord(msg)
            alerts_sent[ticker] = datetime.now()
            continue

        data = get_stock_data(ticker)
        if data and data['signals'] >= 2:
            candidates.append(data)
        time.sleep(0.3)

    candidates.sort(key=lambda x: (x['signals'], x['vol_ratio']), reverse=True)
    print(f"  {len(candidates)} candidatas con señales múltiples")

    found = 0
    for data in candidates[:12]:
        ticker = data['ticker']
        print(f"  Analizando {ticker} ({data['signals']} señales)...")

        # Obtener todos los datos extra
        earnings_days = get_earnings_date(ticker)
        short_interest = get_short_interest(ticker)
        insider_buys = get_insider_activity(ticker)
        unusual_options = get_options_flow(ticker)
        split_announced = get_split_announcement(ticker)
        sector_perf = get_sector_etf_performance(data.get('sector'))
        news = get_stock_news(ticker)

        extras = {
            "earnings_days": earnings_days,
            "short_interest": short_interest,
            "insider_buys": insider_buys,
            "unusual_options": unusual_options,
            "split_announced": split_announced,
            "sector_perf": sector_perf,
        }

        # Alerta especial de earnings
        if earnings_days is not None and earnings_days <= 7:
            earnings_analysis = analyze_earnings_prediction(ticker, data, news, earnings_days)
            if earnings_analysis:
                send_discord(earnings_analysis)

        # Análisis principal
        analysis = analyze_with_ai(data, news, extras)

        if "NO_SIGNAL" not in analysis:
            # Extraer datos para seguimiento
            try:
                lines = analysis.split("\n")
                conf = next((int(''.join(filter(str.isdigit, l))) for l in lines if "CONFIANZA:" in l), 0)
                if conf >= MIN_CONFIDENCE:
                    entry = data['price']
                    target = data['price'] * 1.15
                    stop = data['price'] * 0.93
                    for l in lines:
                        if "ENTRADA" in l and "$" in l:
                            try:
                                entry = float(l.split("$")[1].split()[0].replace(",", ""))
                            except:
                                pass
                        if "PREDICCIÓN" in l and "$" in l:
                            try:
                                target = float(l.split("→ $")[1].split()[0].replace(",", ""))
                            except:
                                pass
                        if "STOP" in l and "$" in l:
                            try:
                                stop = float(l.split("$")[1].split()[0].replace(",", ""))
                            except:
                                pass

                    signal = "COMPRAR" if "COMPRAR" in analysis.split("\n")[0] else "VENDER"
                    add_prediction(ticker, signal, entry, target, stop, conf, 14)

                    msg = format_alert(ticker, data, analysis, session, extras)
                    send_discord(msg)
                    alerts_sent[ticker] = datetime.now()
                    found += 1
                    print(f"    {ticker}: ALERTA enviada (confianza {conf}%)")
                else:
                    print(f"    {ticker}: confianza {conf}% < mínimo {MIN_CONFIDENCE}%")
            except Exception as e:
                msg = format_alert(ticker, data, analysis, session, extras)
                send_discord(msg)
                alerts_sent[ticker] = datetime.now()
                found += 1
        else:
            print(f"    {ticker}: sin señal sólida")

        time.sleep(2)

    if found == 0:
        print(f"  Sin oportunidades sólidas en este ciclo")
    else:
        print(f"  {found} alertas enviadas")


# ═══════════════════════════════════════
# MAIN
# ═══════════════════════════════════════
def main():
    load_predictions()
    print("StockBot Pro iniciado")
    now_spain = datetime.now(SPAIN_TZ)

    send_discord(
        f"🤖 **StockBot Pro activado**\n"
        f"📡 Escaneo cada 30 min | 9:00-23:00h hora España\n"
        f"🎯 Confianza mínima: {MIN_CONFIDENCE}% | 🔥 Señal especial: 85%+\n"
        f"📊 Análisis: técnico + macro + fundamentales + earnings + insiders\n"
        f"🕐 {now_spain.strftime('%H:%M %d/%m/%Y')} hora España"
    )

    update_market_context()
    scan_market()

    schedule.every(30).minutes.do(scan_market)
    schedule.every().monday.at("09:00").do(send_weekly_summary)
    schedule.every().thursday.at("09:00").do(send_weekly_summary)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
