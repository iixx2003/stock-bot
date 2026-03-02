import os
import time
import schedule
import requests
import feedparser
import anthropic
from datetime import datetime

# --- CONFIG ---
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

TICKERS = [
    "AAPL", "TSLA", "NVDA", "MSFT", "AMZN", "META",
    "GOOGL", "AMD", "NFLX", "PLTR", "SOFI", "RIVN"
]


def send_discord(message):
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json"
    }
    # Discord tiene limite de 2000 caracteres
    if len(message) > 1900:
        message = message[:1900] + "..."
    payload = {"content": message}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"Error Discord: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Error enviando Discord: {e}")


def get_technical_data(ticker):
    try:
        # Usamos la API de Yahoo Finance v8 directamente con headers de navegador
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=3mo"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"    {ticker}: HTTP {resp.status_code}")
            return None

        data = resp.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        result = result[0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        volumes = [v for v in result["indicators"]["quote"][0].get("volume", []) if v is not None]

        if len(closes) < 20:
            return None

        price = closes[-1]
        prev_price = closes[-2]
        change_pct = ((price - prev_price) / prev_price) * 100

        sma20 = sum(closes[-20:]) / 20
        sma50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

        gains, losses = [], []
        for i in range(1, 15):
            diff = closes[-i] - closes[-i - 1]
            if diff >= 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0.001
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
        vol_ratio = volumes[-1] / avg_vol if avg_vol > 0 else 1

        high_52w = max(closes)
        low_52w = min(closes)

        return {
            "ticker": ticker,
            "price": round(price, 2),
            "change_pct": round(change_pct, 2),
            "sma20": round(sma20, 2),
            "sma50": round(sma50, 2) if sma50 else None,
            "rsi": round(rsi, 1),
            "vol_ratio": round(vol_ratio, 2),
            "high_52w": round(high_52w, 2),
            "low_52w": round(low_52w, 2),
            "dist_from_low": round(((price - low_52w) / low_52w) * 100, 1),
        }
    except Exception as e:
        print(f"    Error obteniendo datos de {ticker}: {e}")
        return None


def get_news(ticker):
    headlines = []
    feed_url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US"
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:
            headlines.append(entry.title)
    except:
        pass
    return headlines


def analyze_with_ai(data, news):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    news_text = "\n".join(f"- {h}" for h in news) if news else "- Sin noticias recientes"

    prompt = f"""Eres un analista financiero experto. Analiza esta acción y decide si hay una oportunidad clara de inversión.

TICKER: {data['ticker']}
Precio actual: ${data['price']} ({'+' if data['change_pct'] >= 0 else ''}{data['change_pct']}% hoy)
RSI (14): {data['rsi']}
SMA 20: ${data['sma20']} | Precio {'SOBRE' if data['price'] > data['sma20'] else 'BAJO'} la media
SMA 50: ${data['sma50']} | Precio {'SOBRE' if data['sma50'] and data['price'] > data['sma50'] else 'BAJO'} la media
Volumen hoy vs media: {data['vol_ratio']}x
Máximo 3 meses: ${data['high_52w']}
Mínimo 3 meses: ${data['low_52w']} ({data['dist_from_low']}% sobre el mínimo)

NOTICIAS RECIENTES:
{news_text}

Responde SOLO si hay una oportunidad clara.
Si hay oportunidad responde exactamente así:
SEÑAL: COMPRAR o VENDER
CONFIANZA: Alta / Media
RAZÓN: 2-3 frases explicando por qué
OBJETIVO: precio objetivo a corto plazo
RIESGO: qué podría salir mal en 1 frase

Si NO hay oportunidad clara responde únicamente: NO_SIGNAL"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"    Error con AI: {e}")
        return "NO_SIGNAL"


def format_message(data, analysis):
    is_buy = "COMPRAR" in analysis.split("\n")[0]
    emoji = "🟢" if is_buy else "🔴"
    sign = "+" if data['change_pct'] >= 0 else ""
    return f"""{emoji} **ALERTA: {data['ticker']}**
💰 Precio: **${data['price']}** ({sign}{data['change_pct']}% hoy)
📊 RSI: {data['rsi']} | Vol: {data['vol_ratio']}x normal

{analysis}

⚠️ Solo orientativo. No es asesoramiento financiero.
🕐 {datetime.now().strftime('%H:%M %d/%m/%Y')}"""


def scan_market():
    print(f"[{datetime.now().strftime('%H:%M')}] Escaneando mercado...")
    found = 0

    for ticker in TICKERS:
        print(f"  Analizando {ticker}...")
        data = get_technical_data(ticker)
        if not data:
            continue

        interesting = (
            data['rsi'] < 35 or
            data['rsi'] > 70 or
            data['vol_ratio'] > 2.0 or
            abs(data['change_pct']) > 3
        )

        if not interesting:
            print(f"    {ticker}: sin señal relevante")
            continue

        news = get_news(ticker)
        analysis = analyze_with_ai(data, news)

        if "NO_SIGNAL" not in analysis:
            send_discord(format_message(data, analysis))
            found += 1
            print(f"    {ticker}: OPORTUNIDAD enviada a Discord")
        else:
            print(f"    {ticker}: sin oportunidad clara")

        time.sleep(2)

    print(f"  Ciclo terminado. {found} oportunidades enviadas.")


def main():
    print("StockBot iniciado")
    send_discord("🤖 **StockBot activado**\nEscaneando mercado cada 30 minutos...")
    scan_market()
    schedule.every(30).minutes.do(scan_market)
    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
