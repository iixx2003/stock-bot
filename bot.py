import os, time, json, random, schedule, requests, feedparser, anthropic
from datetime import datetime, timedelta
import pytz

DISCORD_TOKEN     = os.environ.get("DISCORD_TOKEN")
DISCORD_ALERTS_ID = os.environ.get("DISCORD_CHANNEL_ID")
DISCORD_LOG_ID    = "1478113089093374034"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NEWS_API_KEY      = os.environ.get("NEWS_API_KEY")

SPAIN_TZ          = pytz.timezone("Europe/Madrid")
MIN_PRICE         = 5.0
MIN_VOLUME        = 500_000
MIN_CONFIDENCE    = 82
MAX_PER_DAY       = 3
MAX_SELLS_DAY     = 1

alerts_sent     = {}
predictions     = []
failed_patterns = {}
market_context  = {"fear_greed":50,"sp500_change":0,"vix":15,"macro_news":[],"economic_events":[],"updated_at":None}
PREDICTIONS_FILE = "/app/predictions.json"

SP500 = ["MMM","ABT","ABBV","ACN","ADBE","AMD","AFL","GOOGL","GOOG","MO","AMZN","AAL","AEP","AXP","AIG","AMT","AWK","AMGN","APH","ADI","AAPL","AMAT","ANET","T","ADSK","AZO","BKR","BAC","BK","BAX","BIIB","BLK","BX","BA","BMY","AVGO","CDNS","COF","KMX","CCL","CAT","CBRE","CNC","SCHW","CHTR","CVX","CMG","CB","CI","CTAS","CSCO","C","CLX","CME","KO","CL","CMCSA","COP","STZ","CPRT","GLW","COST","CCI","CSX","CMI","CVS","DHI","DHR","DE","DAL","DVN","DXCM","DLR","DFS","DG","DLTR","D","DOV","DOW","LLY","ETN","EBAY","ECL","EW","EA","ELV","EMR","ENPH","EOG","EFX","EQIX","EL","ETSY","EXC","EXPE","XOM","FDS","FAST","FRT","FDX","FIS","FITB","FSLR","FI","FLT","F","FTNT","FCX","GE","GD","GIS","GM","GILD","GS","HAL","HIG","HCA","HD","HON","HRL","HPQ","HUBB","HUM","IBM","IDXX","ITW","INTC","ICE","INTU","ISRG","JNJ","JPM","KDP","KEY","KMB","KMI","KLAC","KHC","KR","LHX","LH","LRCX","LIN","LMT","LOW","LULU","MTB","MPC","MAR","MMC","MAS","MA","MCD","MCK","MDT","MRK","META","MET","MGM","MCHP","MU","MSFT","MRNA","MDLZ","MNST","MCO","MS","MSI","NDAQ","NTAP","NFLX","NEE","NKE","NSC","NTRS","NOC","NCLH","NVDA","NXPI","ORLY","OXY","ODFL","ON","OKE","ORCL","PCAR","PLTR","PH","PAYX","PYPL","PEP","PFE","PM","PSX","PNC","PPG","PPL","PG","PGR","PLD","PRU","PEG","PSA","QCOM","RTX","O","REGN","RF","RMD","ROK","ROP","ROST","RCL","SPGI","CRM","SLB","SRE","NOW","SHW","SPG","SJM","SNA","SO","SWK","SBUX","STT","STLD","SYK","SMCI","SNPS","SYY","TMUS","TROW","TTWO","TGT","TEL","TSLA","TXN","TMO","TJX","TSCO","TT","TRV","TFC","TSN","USB","UBER","UNP","UAL","UPS","URI","UNH","VLO","VTR","VRSN","VRSK","VZ","VRTX","VMC","WAB","WBA","WMT","DIS","WM","WAT","WEC","WFC","WELL","WDC","WY","WHR","WMB","WYNN","XEL","YUM","ZBH","ZTS"]
NASDAQ100 = ["ADBE","AMD","ABNB","GOOGL","AMZN","AMGN","AAPL","ARM","ASML","ADSK","BKR","BIIB","BKNG","AVGO","CDNS","CHTR","CTAS","CSCO","CMCSA","CPRT","COST","CRWD","CSX","DDOG","DXCM","DLTR","EA","ENPH","EXC","FAST","FTNT","GILD","HON","IDXX","INTC","INTU","ISRG","KDP","KLAC","LRCX","LULU","MAR","MRVL","MELI","META","MCHP","MU","MSFT","MRNA","MDLZ","MDB","MNST","NFLX","NVDA","NXPI","ORLY","ODFL","ON","PCAR","PANW","PAYX","PYPL","QCOM","REGN","ROP","ROST","SBUX","SNPS","TTWO","TMUS","TSLA","TXN","VRSK","VRTX","WBA","WDAY","XEL","ZS","ZM"]
EXTRAS = ["SOFI","RIVN","COIN","MSTR","HOOD","RBLX","SNAP","LYFT","ABNB","SHOP","SQ","ROKU","SPOT","NET","PANW","SMCI","GME","MARA","RIOT","CLSK","PLTR","LCID","NKLA","AFRM","UPST","DKNG","CHWY","BYND","NIO","XPEV","LI","GRAB","SEA","BIDU"]
SECTOR_ETFS = {"Technology":"XLK","Healthcare":"XLV","Financials":"XLF","Energy":"XLE","Consumer Cyclical":"XLY","Industrials":"XLI","Communication Services":"XLC","Consumer Defensive":"XLP","Utilities":"XLU","Real Estate":"XLRE","Basic Materials":"XLB"}

def _post(cid, msg):
    if len(msg) > 1900: msg = msg[:1897]+"..."
    try:
        r = requests.post(f"https://discord.com/api/v10/channels/{cid}/messages",
            json={"content":msg},
            headers={"Authorization":f"Bot {DISCORD_TOKEN}","Content-Type":"application/json"},
            timeout=10)
        if r.status_code not in (200,201): print(f"Discord {r.status_code}")
    except Exception as e: print(f"Discord err: {e}")

def send_alert(msg): _post(DISCORD_ALERTS_ID, msg)
def send_log(msg):   _post(DISCORD_LOG_ID, msg)

def load_predictions():
    global predictions
    try:
        if os.path.exists(PREDICTIONS_FILE):
            with open(PREDICTIONS_FILE) as f: predictions = json.load(f)
    except: predictions = []

def save_predictions():
    try:
        with open(PREDICTIONS_FILE,"w") as f: json.dump(predictions,f)
    except: pass

def add_prediction(ticker,signal,entry,target,stop,conf,days):
    predictions.append({"ticker":ticker,"signal":signal,"entry":entry,"target":target,"stop":stop,
        "confidence":conf,"days":days,"date":datetime.now().isoformat(),"result":"pending","exit_price":None})
    save_predictions()

def already_alerted(ticker):
    if ticker in alerts_sent:
        return datetime.now()-alerts_sent[ticker] < timedelta(hours=24)
    return False

def sells_today():
    today = datetime.now(SPAIN_TZ).date()
    return sum(1 for p in predictions if p["signal"]=="VENDER" and datetime.fromisoformat(p["date"]).date()==today)

def update_market_context():
    global market_context
    print("  Actualizando contexto macro...")
    fg=50
    try:
        r=requests.get("https://api.alternative.me/fng/",timeout=8)
        if r.status_code==200: fg=int(r.json()["data"][0]["value"])
    except: pass
    sp500=0; vix=15
    for ticker,key in [("SPY","sp"),("^VIX","vix")]:
        try:
            r=requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
                headers={"User-Agent":"Mozilla/5.0"},timeout=10)
            if r.status_code==200:
                closes=[c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close",[]) if c]
                if len(closes)>=2:
                    if key=="sp": sp500=round(((closes[-1]-closes[-2])/closes[-2])*100,2)
                    else: vix=round(closes[-1],1)
            time.sleep(0.5)
        except: pass
    macro_news=[]
    try:
        r=requests.get(f"https://newsapi.org/v2/top-headlines?category=business&language=en&pageSize=8&apiKey={NEWS_API_KEY}",timeout=10)
        if r.status_code==200: macro_news=[a.get("title","") for a in r.json().get("articles",[])[:8]]
    except: pass
    econ=[]
    try:
        r=requests.get(f"https://newsapi.org/v2/everything?q=Federal+Reserve+OR+CPI+OR+inflation&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}",timeout=10)
        if r.status_code==200: econ=[a.get("title","") for a in r.json().get("articles",[])[:5]]
    except: pass
    market_context={"fear_greed":fg,"sp500_change":sp500,"vix":vix,"macro_news":macro_news,"economic_events":econ,"updated_at":datetime.now(SPAIN_TZ).strftime("%H:%M")}
    fg_label=("PANICO EXTREMO" if fg<20 else "Miedo" if fg<40 else "Neutral" if fg<60 else "Codicia" if fg<80 else "EUFORIA")
    print(f"  Fear&Greed: {fg} ({fg_label}) | S&P500: {sp500}% | VIX: {vix}")
    send_log(f"📊 Macro actualizado — Fear&Greed: {fg} ({fg_label}) | S&P500: {'+' if sp500>=0 else ''}{sp500}% | VIX: {vix}")

def get_trending_tickers():
    tickers={}
    headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Accept":"application/json","Referer":"https://finance.yahoo.com"}
    for url in ["https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=50","https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=50","https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=50","https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=50"]:
        try:
            r=requests.get(url,headers=headers,timeout=15)
            if r.status_code==200:
                quotes=r.json().get("finance",{}).get("result",[{}])[0].get("quotes",[])
                for q in quotes:
                    sym=q.get("symbol",""); price=q.get("regularMarketPrice",0); vol=q.get("regularMarketVolume",0)
                    if sym and "." not in sym and len(sym)<=5 and price>=MIN_PRICE and vol>=MIN_VOLUME:
                        tickers[sym]={"price":price,"change_pct":q.get("regularMarketChangePercent",0),"volume":vol,"avg_volume":max(q.get("averageDailyVolume3Month",1),1),"name":q.get("longName",sym),"sector":q.get("sector","Unknown"),"market_cap":q.get("marketCap",0)}
                if quotes: print(f"  Screener OK: {len(quotes)}")
        except Exception as e: print(f"  Screener err: {e}")
        time.sleep(0.8)
    for sym in set(SP500+NASDAQ100+EXTRAS):
        if sym not in tickers:
            tickers[sym]={"price":0,"change_pct":0,"volume":MIN_VOLUME+1,"avg_volume":MIN_VOLUME,"name":sym,"sector":"Unknown","market_cap":0}
    print(f"  Universo total: {len(tickers)}")
    return tickers

def get_stock_data(ticker):
    try:
        host=random.choice(["query1","query2"])
        url=f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1y&includePrePost=false"
        ua=random.choice(["Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"])
        hdrs={"User-Agent":ua,"Accept":"application/json","Referer":f"https://finance.yahoo.com/quote/{ticker}/"}
        s=requests.Session()
        s.get(f"https://finance.yahoo.com/quote/{ticker}/",headers=hdrs,timeout=8)
        r=s.get(url,headers=hdrs,timeout=15)
        if r.status_code!=200: return None
        result=r.json().get("chart",{}).get("result",[])
        if not result: return None
        result=result[0]; meta=result.get("meta",{}); q=result["indicators"]["quote"][0]
        closes=[c for c in q.get("close",[]) if c is not None]
        volumes=[v for v in q.get("volume",[]) if v is not None]
        highs=[h for h in q.get("high",[]) if h is not None]
        lows=[l for l in q.get("low",[]) if l is not None]
        if len(closes)<20: return None
        price=closes[-1]; change_pct=((price-closes[-2])/closes[-2])*100
        sma20=sum(closes[-20:])/20
        sma50=sum(closes[-50:])/50 if len(closes)>=50 else None
        sma200=sum(closes[-200:])/200 if len(closes)>=200 else None
        gains=[]; losses=[]
        for i in range(1,15):
            d=closes[-i]-closes[-i-1]
            (gains if d>=0 else losses).append(abs(d))
        rsi=100-(100/(1+(sum(gains)/14 or 0)/(sum(losses)/14 or 0.001)))
        avg_vol=sum(volumes[-20:])/20 if len(volumes)>=20 else 1
        vol_ratio=volumes[-1]/avg_vol if avg_vol>0 else 1
        atr_vals=[]
        for i in range(1,min(15,len(closes))):
            hl=highs[-i]-lows[-i] if highs and lows else 0
            atr_vals.append(max(hl,abs(highs[-i]-closes[-i-1]) if highs else 0,abs(lows[-i]-closes[-i-1]) if lows else 0))
        atr=sum(atr_vals)/len(atr_vals) if atr_vals else price*0.02
        h52=max(closes[-252:]) if len(closes)>=252 else max(closes)
        l52=min(closes[-252:]) if len(closes)>=252 else min(closes)
        rng=h52-l52
        mom1m=((price-closes[-22])/closes[-22]*100) if len(closes)>=22 else 0
        mom3m=((price-closes[-66])/closes[-66]*100) if len(closes)>=66 else 0
        vol20=((sum((c-sma20)**2 for c in closes[-20:])/20)**0.5)/price*100
        rh=max(highs[-20:]) if len(highs)>=20 else price
        rl=min(lows[-20:]) if len(lows)>=20 else price
        score=0; rsi_zone="neutral"
        if rsi<25: score+=3; rsi_zone="oversold_extreme"
        elif rsi<32: score+=2; rsi_zone="oversold"
        elif rsi>75: score+=3; rsi_zone="overbought_extreme"
        elif rsi>68: score+=2; rsi_zone="overbought"
        if vol_ratio>3: score+=3
        elif vol_ratio>2: score+=2
        elif vol_ratio>1.5: score+=1
        if abs(change_pct)>8: score+=3
        elif abs(change_pct)>5: score+=2
        elif abs(change_pct)>3: score+=1
        if sma50 and ((price>sma50 and price>sma20) or (price<sma50 and price<sma20)): score+=1
        if abs(mom1m)>15: score+=2
        elif abs(mom1m)>8: score+=1
        sector=meta.get("sector","Unknown")
        pk=f"{'BUY' if change_pct>0 else 'SELL'}_{rsi_zone}_{sector}"
        if failed_patterns.get(pk,0)>=3: score-=2
        return {"ticker":ticker,"name":meta.get("longName",ticker),"sector":sector,"price":round(price,2),"change_pct":round(change_pct,2),"sma20":round(sma20,2),"sma50":round(sma50,2) if sma50 else None,"sma200":round(sma200,2) if sma200 else None,"rsi":round(rsi,1),"rsi_zone":rsi_zone,"vol_ratio":round(vol_ratio,2),"atr":round(atr,2),"h52":round(h52,2),"l52":round(l52,2),"fib236":round(h52-rng*0.236,2),"fib382":round(h52-rng*0.382,2),"fib500":round(h52-rng*0.500,2),"fib618":round(h52-rng*0.618,2),"rh":round(rh,2),"rl":round(rl,2),"dist_h":round(((price-h52)/h52)*100,1),"dist_l":round(((price-l52)/l52)*100,1),"vol20":round(vol20,1),"mom1m":round(mom1m,1),"mom3m":round(mom3m,1),"score":max(score,0)}
    except Exception as e:
        return None

def get_earnings_days(ticker):
    try:
        r=requests.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=calendarEvents",headers={"User-Agent":"Mozilla/5.0"},timeout=10)
        if r.status_code==200:
            dates=r.json().get("quoteSummary",{}).get("result",[{}])[0].get("calendarEvents",{}).get("earnings",{}).get("earningsDate",[])
            if dates:
                days=(datetime.fromtimestamp(dates[0]["raw"])-datetime.now()).days
                if 0<=days<=21: return days
    except: pass
    return None

def get_short_interest(ticker):
    try:
        r=requests.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=defaultKeyStatistics",headers={"User-Agent":"Mozilla/5.0"},timeout=10)
        if r.status_code==200:
            pct=r.json().get("quoteSummary",{}).get("result",[{}])[0].get("defaultKeyStatistics",{}).get("shortPercentOfFloat",{}).get("raw",0)
            return round(pct*100,1)
    except: pass
    return 0

def get_insider_buys(ticker):
    try:
        r=requests.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=insiderTransactions",headers={"User-Agent":"Mozilla/5.0"},timeout=10)
        if r.status_code==200:
            txns=r.json().get("quoteSummary",{}).get("result",[{}])[0].get("insiderTransactions",{}).get("transactions",[])
            return sum(1 for t in txns[:10] if (datetime.now()-datetime.fromtimestamp(t.get("startDate",{}).get("raw",0))).days<=30 and "Purchase" in t.get("transactionText",""))
    except: pass
    return 0

def get_sector_perf(sector):
    etf=SECTOR_ETFS.get(sector)
    if not etf: return None
    try:
        r=requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?interval=1d&range=5d",headers={"User-Agent":"Mozilla/5.0"},timeout=10)
        if r.status_code==200:
            closes=[c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close",[]) if c]
            if len(closes)>=2: return round(((closes[-1]-closes[-2])/closes[-2])*100,2)
    except: pass
    return None

def get_news(ticker):
    news=[]
    try:
        r=requests.get(f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=5&apiKey={NEWS_API_KEY}",timeout=10)
        if r.status_code==200: news+=[a.get("title","") for a in r.json().get("articles",[])[:4]]
    except: pass
    try:
        feed=feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US")
        news+=[e.title for e in feed.entries[:4]]
    except: pass
    return news[:8]

def analyze(data, news, extras):
    client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    news_text="\n".join(f"- {h}" for h in news) if news else "- Sin noticias"
    macro_text="\n".join(f"- {h}" for h in market_context.get("macro_news",[])[:5])
    econ_text="\n".join(f"- {h}" for h in market_context.get("economic_events",[])[:3])
    fg=market_context["fear_greed"]
    fg_label=("PANICO EXTREMO" if fg<20 else "Miedo" if fg<40 else "Neutral" if fg<60 else "Codicia" if fg<80 else "EUFORIA")
    t=data
    trend_s="ALCISTA" if t["price"]>t["sma20"] else "BAJISTA"
    trend_m=("ALCISTA" if t["sma50"] and t["price"]>t["sma50"] else "BAJISTA") if t["sma50"] else "N/D"
    trend_l=("ALCISTA" if t["sma200"] and t["price"]>t["sma200"] else "BAJISTA") if t["sma200"] else "N/D"
    earn=f"EARNINGS EN {extras['earnings_days']} DIAS" if extras.get("earnings_days") is not None else "Sin earnings proximos"
    short=f"{extras.get('short_interest',0)}% en corto{'  POSIBLE SHORT SQUEEZE' if extras.get('short_interest',0)>20 else ''}"
    ins=f"{extras.get('insider_buys',0)} compras insiders (30d){'  SENAL ALCISTA' if extras.get('insider_buys',0)>=2 else ''}"
    sect=f"ETF sectorial hoy: {extras.get('sector_perf','N/D')}%"
    prompt=f"""Eres analista cuantitativo de elite. Perfil: agresivo-moderado, busca +10% a +40%.
Confianza minima: {MIN_CONFIDENCE}%. Si dudas: NO_SIGNAL.
Usa ATR para calcular plazos. Usa Fibonacci para anclar objetivos y stops.

MACRO: Fear&Greed {fg}/100 ({fg_label}) | S&P500 {'+' if market_context['sp500_change']>=0 else ''}{market_context['sp500_change']}% | VIX {market_context['vix']}
Noticias macro: {macro_text}
Eventos economicos: {econ_text}

DATOS: {t['ticker']} - {t['name']}
Precio: ${t['price']} ({'+' if t['change_pct']>=0 else ''}{t['change_pct']}% hoy) | Sector: {t['sector']} | {sect}
SMA20: ${t['sma20']} ({trend_s}) | SMA50: ${t['sma50'] or 'N/D'} ({trend_m}) | SMA200: ${t['sma200'] or 'N/D'} ({trend_l})
RSI: {t['rsi']} | Vol: {t['vol_ratio']}x | ATR: ${t['atr']}
Momentum 1m: {'+' if t['mom1m']>=0 else ''}{t['mom1m']}% | 3m: {'+' if t['mom3m']>=0 else ''}{t['mom3m']}%
Fibonacci: 23.6%=${t['fib236']} | 38.2%=${t['fib382']} | 50%=${t['fib500']} | 61.8%=${t['fib618']}
Soporte: ${t['rl']} | Resistencia: ${t['rh']} | Min52: ${t['l52']} | Max52: ${t['h52']}
{earn} | Short: {short} | Insiders: {ins}
Noticias: {news_text}

Si hay oportunidad responde EXACTAMENTE asi (sin añadir nada mas):

SEÑAL: COMPRAR o VENDER
CONFIANZA: [X]%
🎯 ENTRADA ÓPTIMA: $[precio exacto]
📈 PREDICCIÓN: [+/-X%] → $[objetivo en Fibonacci o resistencia]
⏱ PLAZO: [X dias/semanas calculado con ATR]
🛑 STOP LOSS: $[precio en soporte/Fibonacci] — prob. stop: [X]%
⚖️ RATIO R/B: [X]:1
💬 POR QUÉ: [2-3 frases simples sin jerga tecnica]
⚡ CATALIZADOR: [factor concreto mas importante]
❌ INVALIDACIÓN: [precio o evento exacto]

Si no hay oportunidad: NO_SIGNAL"""
    try:
        msg=client.messages.create(model="claude-sonnet-4-20250514",max_tokens=600,messages=[{"role":"user","content":prompt}])
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"    Error IA: {e}")
        return "NO_SIGNAL"

def analyze_earnings(ticker, data, news, days):
    client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    news_text="\n".join(f"- {h}" for h in news) if news else "- Sin noticias"
    prompt=f"""Analista experto en earnings. Predice si {ticker} subira o bajara tras resultados en {days} dias.
Precio: ${data['price']} | RSI: {data['rsi']} | Momentum 1m: {data['mom1m']}% | Vol: {data['vol_ratio']}x
Noticias: {news_text}

Responde EXACTAMENTE asi:
EARNINGS EN {days} DIAS - {ticker} ({data['name']})
PREDICCION: SUBIRA / BAJARA
MOVIMIENTO ESPERADO: [+/-X%] to $[precio]
CONFIANZA: [X]%
POR QUE: [2 frases simples]
ESTRATEGIA: [entrar antes / esperar confirmacion / evitar]"""
    try:
        msg=client.messages.create(model="claude-sonnet-4-20250514",max_tokens=300,messages=[{"role":"user","content":prompt}])
        return msg.content[0].text.strip()
    except: return None

def format_alert(data, analysis, session, extras):
    now=datetime.now(SPAIN_TZ)
    signal="COMPRAR"
    for line in analysis.split("\n"):
        if line.startswith("SEÑAL:"):
            signal="VENDER" if "VENDER" in line else "COMPRAR"
            break
    is_buy=signal=="COMPRAR"
    conf_val=0
    for line in analysis.split("\n"):
        if "CONFIANZA:" in line:
            try: conf_val=int(''.join(filter(str.isdigit,line)))
            except: pass
            break
    if conf_val>=85: emoji="🔥" if is_buy else "💀"
    else: emoji="🟢" if is_buy else "🔴"
    sign="+" if data["change_pct"]>=0 else ""
    session_tag=f"  [{session}]" if session!="MERCADO" else ""
    earnings_line=f"📅  Earnings en {extras['earnings_days']} días\n" if extras.get("earnings_days") is not None else ""
    clean="\n".join(l for l in analysis.split("\n") if not l.startswith("SEÑAL:")).strip()
    return f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
{emoji}  **{signal}  —  {data['ticker']}**{session_tag}
{data['name']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰  **${data['price']}**  ({sign}{data['change_pct']}% hoy)
{earnings_line}━━━━━━━━━━━━━━━━━━━━━━━━━━━
{clean}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐  {now.strftime('%H:%M  %d/%m/%Y')} hora España"""

def send_weekly_summary():
    now=datetime.now(SPAIN_TZ)
    total=len([p for p in predictions if p["result"]!="pending"])
    wins=len([p for p in predictions if p["result"]=="win"])
    losses=len([p for p in predictions if p["result"]=="loss"])
    pending=len([p for p in predictions if p["result"]=="pending"])
    rate=round(wins/total*100,1) if total>0 else 0
    send_log(f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESUMEN StockBot — {now.strftime('%d/%m/%Y')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Acertadas: {wins} | Falladas: {losses} | Pendientes: {pending}
Tasa de acierto: {rate}%
━━━━━━━━━━━━━━━━━━━━━━━━━━━""")

def get_session(now):
    m=now.hour*60+now.minute
    if 900<=m<930: return "PREMARKET"
    if 930<=m<1380: return "MERCADO"
    if 1380<=m<1440: return "AFTERHOURS"
    return "MERCADO"

def scan():
    now=datetime.now(SPAIN_TZ)
    if now.hour<9 or now.hour>=23: return
    session=get_session(now)
    print(f"\n[{now.strftime('%H:%M')} ES | {session}] Escaneando...")
    if now.hour==9 and now.minute<30: update_market_context()
    trending=get_trending_tickers()
    screener={k:v for k,v in trending.items() if v.get("change_pct",0)!=0}
    static={k:v for k,v in trending.items() if v.get("change_pct",0)==0}
    sample=dict(random.sample(list(static.items()),min(150,len(static))))
    to_check={**screener,**sample}
    print(f"  Analizando {len(to_check)} ({len(screener)} screener + {len(sample)} muestra)")
    candidates=[]
    for ticker,basic in to_check.items():
        if already_alerted(ticker): continue
        data=get_stock_data(ticker)
        if data and data["score"]>=2:
            candidates.append(data)
            print(f"  + {ticker}: score {data['score']} | RSI {data['rsi']} | vol {data['vol_ratio']}x | {data['change_pct']}%")
        time.sleep(0.2)
    candidates.sort(key=lambda x:x["score"],reverse=True)
    print(f"  {len(candidates)} candidatas para IA")
    send_log(f"[{now.strftime('%H:%M')}] {len(to_check)} analizadas → {len(candidates)} candidatas")
    # Contar alertas ya enviadas hoy
    today = datetime.now(SPAIN_TZ).date()
    alerts_today = sum(1 for dt in alerts_sent.values() if dt.date() == today)
    if alerts_today >= MAX_PER_DAY:
        print(f"  Limite diario alcanzado ({alerts_today} alertas hoy)")
        send_log(f"ℹ️ Limite diario de {MAX_PER_DAY} alertas alcanzado hoy")
        return
    found=0; sells_hoy=sells_today()
    for data in candidates[:15]:
        if alerts_today + found >= MAX_PER_DAY: break
        ticker=data["ticker"]
        print(f"  IA: {ticker} (score {data['score']})...")
        earnings_days=get_earnings_days(ticker)
        short=get_short_interest(ticker)
        insiders=get_insider_buys(ticker)
        sector_perf=get_sector_perf(data["sector"])
        news=get_news(ticker)
        extras={"earnings_days":earnings_days,"short_interest":short,"insider_buys":insiders,"sector_perf":sector_perf}
        if earnings_days is not None and earnings_days<=7:
            ea=analyze_earnings(ticker,data,news,earnings_days)
            if ea: send_alert(ea)
        analysis=analyze(data,news,extras)
        if "NO_SIGNAL" in analysis:
            print(f"    {ticker}: sin señal"); time.sleep(2); continue
        signal="COMPRAR"
        for line in analysis.split("\n"):
            if line.startswith("SEÑAL:"):
                signal="VENDER" if "VENDER" in line else "COMPRAR"; break
        if signal=="VENDER" and sells_hoy>=MAX_SELLS_DAY:
            print(f"    {ticker}: venta descartada (limite)"); time.sleep(2); continue
        conf=MIN_CONFIDENCE
        for line in analysis.split("\n"):
            if "CONFIANZA:" in line:
                try: conf=int(''.join(filter(str.isdigit,line)))
                except: pass
                break
        if conf<MIN_CONFIDENCE:
            print(f"    {ticker}: confianza {conf}% insuficiente"); time.sleep(2); continue
        entry=data["price"]; target=data["price"]*1.15; stop=data["price"]*0.93
        for line in analysis.split("\n"):
            if "ENTRADA" in line and "$" in line:
                try: entry=float(line.split("$")[1].split()[0].replace(",",""))
                except: pass
            if "PREDICCION" in line.upper() and "→ $" in line:
                try: target=float(line.split("→ $")[1].split()[0].replace(",",""))
                except: pass
            if "STOP LOSS" in line and "$" in line:
                try: stop=float(line.split("$")[1].split()[0].replace(",",""))
                except: pass
        add_prediction(ticker,signal,entry,target,stop,conf,14)
        msg=format_alert(data,analysis,session,extras)
        send_alert(msg)
        alerts_sent[ticker]=datetime.now()
        if signal=="VENDER": sells_hoy+=1
        found+=1
        print(f"    {ticker}: ALERTA enviada ({signal}, {conf}%)")
        time.sleep(3)
    if found==0: print("  Sin oportunidades de calidad")
    else: print(f"  {found} alertas enviadas")

def main():
    load_predictions()
    now=datetime.now(SPAIN_TZ)
    print(f"StockBot Pro iniciado — {now.strftime('%H:%M %d/%m/%Y')}")
    send_log(f"🤖 **StockBot Pro activado** — {now.strftime('%H:%M %d/%m/%Y')}\n📡 Escaneo 30min | 9:00-23:00h España\n✅ Confianza min: {MIN_CONFIDENCE}% | 🔥 Especial: 85%+\n🎯 Max {MAX_PER_CYCLE} alertas/ciclo | Max {MAX_SELLS_DAY} venta/dia")
    update_market_context()
    scan()
    schedule.every(30).minutes.do(scan)
    schedule.every().monday.at("09:00").do(send_weekly_summary)
    schedule.every().thursday.at("09:00").do(send_weekly_summary)
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__=="__main__":
    main()
