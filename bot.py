"""
StockBot Pro v5.2
───────────────────────────────────────────────────────────────────────
Añadidos sobre v5.2:
  - Macro actualizada cada 30 min durante sesión USA (15:00-22:30)
  - MAX_AI dinámico: 3 en BULL/LATERAL, 1 en BEAR
  - Learning engine arranca con 5 predicciones (era 20)
  - Sentimiento con detección de negaciones y contexto (anti falsos positivos)
  - Cooldown de score reducido: 3 ciclos (era 5)
  - Franja suave pre-apertura USA 15:00-15:30: mínimo CONF_FUERTE
  - Reset diario completo: incluye _score_fail_count y _score_cooldown
  - Parsing de confianza blindado: regex robusto + validación 0-99
  - Régimen con fallback al valor anterior si falla SPY
  - Stop Loss real de la IA guardado en predicción (con fallback técnico)
  - Sector real obtenido de assetProfile (quoteSummary)
  - Notificación inmediata a Discord cuando predicción se resuelve (win/loss)
  - Target dinámico por régimen: BULL 18%, LATERAL 12%, BEAR 8%
  - Circuit breaker: 3 pérdidas seguidas → umbral +5% durante 24h
"""

import os, time, json, random, schedule, requests, feedparser, anthropic, re, threading
from datetime import datetime, timedelta
from collections import defaultdict
import pytz

_ws_lock = threading.Lock()  # protege escrituras concurrentes en watch_signals

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
_ai_client               = None   # singleton — se crea la primera vez que se necesita
NEWS_API_KEY             = os.environ.get("NEWS_API_KEY")
FINNHUB_TOKEN            = os.environ.get("FINNHUB_TOKEN")
TWELVE_DATA_KEY          = os.environ.get("TWELVE_DATA_KEY")

# Rate limiter global para Twelve Data (Basic 8: 8 req/min)
_td_last_call = 0.0
_TD_MIN_INTERVAL = 8.0  # segundos entre llamadas

# Símbolos inválidos en Twelve Data (se puebla en sesión, no persiste)
_td_invalid_symbols: set = set()

# Créditos agotados: timestamp UTC en que se puede volver a intentar (medianoche UTC)
_td_credits_reset_at: float = 0.0

# Cooldown de score bajo: {ticker: ciclos_restantes_a_saltar}
_score_cooldown: dict = {}
SCORE_COOLDOWN_CICLOS = 3  # saltar N ciclos si falla por score repetidamente (era 5)
SCORE_FAIL_THRESHOLD  = 3  # fallos consecutivos antes de activar cooldown
_score_fail_count: dict = {}  # {ticker: nº fallos consecutivos}

def _td_rate_limit():
    """Bloquea hasta que haya pasado el intervalo mínimo entre llamadas."""
    global _td_last_call
    elapsed = time.time() - _td_last_call
    if elapsed < _TD_MIN_INTERVAL:
        time.sleep(_TD_MIN_INTERVAL - elapsed)
    _td_last_call = time.time()

SPAIN_TZ = pytz.timezone("Europe/Madrid")

# Umbrales de confianza
CONF_NORMAL      = 85
CONF_FUERTE      = 88
CONF_EXCEPCIONAL = 94

# Límites diarios
MAX_ALERTAS_DIA       = 3
MAX_VENTAS_DIA        = 1
MAX_AI_POR_CICLO      = 3   # máx llamadas IA por ciclo (1 en BEAR, 3 en BULL/LATERAL)
MAX_PRE_EARNINGS_DIA  = 1   # máximo señales PRE-EARNINGS al día

# Score técnico mínimo
SCORE_MINIMO = 8   # subido de 6 a 8 para reducir llamadas IA innecesarias

# Cola de calidad: hora a partir de la cual se desbloquean normales/fuertes
HORA_DESBLOQUEO = 14   # 14:00 hora España

# Seguridad y monitorización
OWNER_DISCORD_ID      = os.environ.get("OWNER_DISCORD_ID", "")  # tu user ID de Discord
MAX_ANALIZAR_POR_HORA = 3      # máximo !analizar por usuario por hora
COSTE_MAX_DIA         = 0.50   # alerta si se supera este gasto estimado ($)
MAX_429_SEGUIDOS      = 5      # ciclos con mayoría de 429 antes de pausa larga
PAUSA_429_MINUTOS     = 20     # minutos de pausa si Yahoo está bloqueando fuerte

# Aprendizaje — umbrales bajados para activar antes (v5.2)
LEARN_MIN_PREDS = 5    # era 20 — nivel 1 y 2
LEARN_MIN_L3    = 15   # era 40
LEARN_MIN_L4    = 30   # era 60
LEARN_MIN_L5    = 50   # era 80

# Archivos de persistencia
PREDICTIONS_FILE      = "/app/data/predictions.json"
WATCHSTATE_FILE       = "/app/data/watchstate.json"
LEARNINGS_FILE        = "/app/data/learnings.json"
REGIME_FILE           = "/app/data/regime.json"
CATALYST_MEMORY_FILE  = "/app/data/catalyst_memory.json"
ECON_CALENDAR_FILE    = "/app/data/econ_calendar.json"
MARKET_CONTEXT_FILE   = "/app/data/market_context.json"  # web dashboard
EARNINGS_WATCH_FILE   = "/app/data/earnings_watch.json"  # scanner semanal de earnings

# ═══════════════════════════════════════════════════════════════════════
# UNIVERSO — igual que v5, incluido aquí por completitud
# ═══════════════════════════════════════════════════════════════════════

SP500 = [
    "A","AAL","AAPL","ABBV","ABNB","ABT","ACN","ADBE","ADI","ADP","ADSK","AEP","AES","AFL",
    "AIG","AJG","AKAM","ALB","ALGN","ALL","ALLE","AMAT","AMCR","AMD","AME","AMGN","AMP","AMT",
    "AMZN","ANET","ANSS","AON","AOS","APD","APH","APO","APTV","ARE","ATO","AVB","AVGO","AVY",
    "AWK","AXON","AXP","AZO","BA","BAC","BAX","BBY","BDX","BIIB","BK","BKNG","BKR","BLK",
    "BMY","BR","BSX","BX","BXP","C","CAG","CAH","CARR","CAT","CB","CBRE","CCI","CCL",
    "CDNS","CDW","CEG","CF","CHRW","CHTR","CI","CINF","CL","CLF","CLX","CMA","CMCSA","CME",
    "CMG","CMI","CMS","CNC","CNP","COF","COP","COST","CPB","CPRT","CPT","CRM","CSCO","CSGP",
    "CSX","CTAS","CTSH","CTVA","CVS","CVX","D","DAL","DE","DECK","DFS","DG","DHI","DHR",
    "DIS","DLR","DLTR","DOV","DOW","DPZ","DRI","DUK","DVA","DVN","DXCM","EA","EBAY","ECL",
    "ED","EFX","EG","EIX","EL","ELV","EMN","EMR","ENPH","EOG","EQIX","ETN","ETR","ETSY",
    "EVRG","EW","EXC","EXPE","EXR","F","FANG","FAST","FCX","FDS","FDX","FE","FFIV","FI",
    "FIS","FITB","FLT","FMC","FOX","FOXA","FRT","FSLR","FTNT","FTV","GD","GE","GEHC","GEN",
    "GILD","GIS","GL","GLW","GM","GNRC","GOOG","GOOGL","GPC","GPN","GS","GWW","HAL","HAS",
    "HBAN","HCA","HD","HES","HIG","HII","HOLX","HON","HPE","HPQ","HRL","HSIC","HST","HSY",
    "HUBB","HUM","HWM","IBM","ICE","IDXX","IEX","IFF","INTC","INTU","IP","IPG","IQV","IR",
    "IRM","ISRG","ITW","JBHT","JCI","JKHY","JNJ","JPM","K","KDP","KEY","KHC","KIM","KLAC",
    "KMB","KMI","KMX","KO","KR","KVUE","L","LEN","LH","LHX","LIN","LKQ","LLY","LMT",
    "LNC","LNT","LOW","LRCX","LULU","LUV","LVS","LW","LYB","LYV","MA","MAR","MAS","MAT",
    "MCD","MCHP","MCK","MCO","MDLZ","MDT","MET","META","MGM","MKTX","MLM","MMC","MMM","MNST",
    "MO","MOH","MOS","MPC","MRK","MRNA","MRO","MS","MSFT","MSI","MTB","MTCH","MTD","MU",
    "NCLH","NDAQ","NEE","NEM","NFLX","NI","NKE","NOC","NOV","NOW","NRG","NSC","NTAP","NTRS",
    "NUE","NVDA","NVR","NWS","NWSA","NXPI","O","ODFL","OGN","OKE","OMC","ON","ORCL","ORLY",
    "OTIS","OXY","PARA","PAYC","PAYX","PCAR","PEG","PEP","PFE","PFG","PG","PGR","PH","PKG",
    "PLD","PLTR","PM","PNC","POOL","PPG","PPL","PRU","PSA","PSX","PTC","PWR","PYPL","QCOM",
    "RCL","RE","REG","REGN","RF","RHI","RMD","ROK","ROL","ROP","ROST","RPM","RSG","RTX",
    "SBAC","SBUX","SCHW","SEE","SFM","SHW","SJM","SKX","SLB","SMCI","SNA","SNPS","SNX","SO",
    "SOLV","SPG","SPGI","SRE","STE","STLD","STT","STX","STZ","SW","SWK","SYF","SYK","SYY",
    "T","TDG","TDY","TEL","TER","TFC","TFX","TGT","TJX","TMO","TMUS","TRGP","TROW","TRV",
    "TSCO","TSLA","TSN","TT","TTWO","TXN","TXT","TYL","UAA","UAL","UBER","UDR","UHS","ULTA",
    "UNH","UNM","UNP","UPS","URI","USB","UTHR","V","VICI","VLO","VMC","VNO","VRSK","VRSN",
    "VRTX","VTR","VZ","WAB","WAT","WBA","WBD","WDAY","WDC","WEC","WELL","WFC","WHR","WM",
    "WMB","WMT","WRB","WTW","WY","WYNN","XEL","XOM","XYL","YUM","ZBH","ZBRA","ZION","ZTS",
]
NASDAQ100 = [
    "AAPL","ABNB","ADBE","ADP","ADSK","AMAT","AMD","AMGN","AMZN","ANSS","ARM","ASML","AVGO","AXON",
    "BIIB","BKNG","BKR","CDNS","CEG","CHTR","CMCSA","COST","CPRT","CRWD","CSCO","CSGP","CSX","CTAS",
    "CTSH","DDOG","DLTR","DXCM","EA","ENPH","EXC","FANG","FAST","FTNT","GEHC","GFS","GILD","GOOGL",
    "HON","IDXX","ILMN","INTC","INTU","ISRG","KDP","KLAC","LRCX","LULU","MAR","MCHP","MDB","MDLZ",
    "MELI","META","MNST","MRNA","MRVL","MSFT","MSTR","MU","NDAQ","NFLX","NVDA","NXPI","ODFL","ON",
    "ORLY","PANW","PAYX","PCAR","PDD","PYPL","PZZA","QCOM","REGN","ROP","ROST","SBUX","SIRI","SNPS",
    "TEAM","TMUS","TSLA","TTD","TTWO","TXN","VRSK","VRTX","WBA","WDAY","XEL","ZM","ZS",
]
EXTRAS = [
    "AAL","ACHR","AFRM","ASTS","BBAI","BIDU","BROS","BYND","CAVA","CCL","CELH","CHWY","CLSK","COIN",
    "DAL","DKNG","DUOL","ELF","GME","GRAB","GTLB","HOOD","IREN","JOBY","LCID","LI","LUNR","LYFT",
    "MARA","MSTR","NCLH","NET","NIO","NKLA","ONON","OPEN","PANW","PINS","PLTR","RBLX","RCL","RDDT",
    "RELY","RIOT","RIVN","RKLB","RKT","ROKU","SEA","SHOP","SMCI","SNAP","SOFI","SOUN","SPOT","SQ",
    "STNE","UAL","UPST","XPEV",
]
RUSSELL2000 = [
    "ACLS","ACMR","AEHR","AEIS","AEYE","AFCG","AGIO","AGYS","AHCO","AHPI","AIOT","AIXI",
    "AKAM","AKBA","AKRO","ALEC","ALGM","ALGT","ALHC","ALKS","ALLT","ALNY","ALRM","ALRS",
    "ALSA","ALTG","ALVO","ALXO","AMBC","AMBI","AMCX","AMKR","AMNB","AMPH","AMRK","AMSC",
    "AMTB","AMWD","ANAB","ANGI","ANGO","ANIK","ANIP","ANSS","ANTE","ANVS","AORT","APAM",
    "APEI","APLD","APLS","APOG","APPF","APRE","APTS","APTV","ARCO","ARCB","ARCT","ARDX",
    "ARHS","AROW","ARQT","ARTL","ARTNA","ARVN","ASIX","ASND","ASTE","ASUR","ATEC","ATEX",
    "ATGE","ATGL","ATHN","ATLO","ATNF","ATNI","ATNM","ATRC","ATRI","ATSG","ATXI","ATYR",
    "AUDC","AUPH","AURE","AURT","AUTL","AVAH","AVAV","AVDL","AVEO","AVNW","AVTE","AVXL",
    "AXGN","AXNX","AXSM","AXTA","AYRO","AZEK","AZTA","AZUL","BAND","BANF","BANR","BARK",
    "BBCP","BBIO","BBSI","BCAL","BCOV","BCPC","BCRX","BCYC","BDSI","BDSX","BEAM","BECN",
    "BEST","BFIN","BFLY","BGFV","BGSF","BHVN","BIGC","BILL","BIRD","BLBD","BLCO","BLDP",
    "BLFS","BLMN","BLNK","BLPH","BLRX","BLTE","BLUE","BMBL","BMRN","BNGO","BNTC","BOCH",
    "BODY","BOOT","BOWX","BPOP","BRBR","BRCC","BRDG","BRKL","BRLT","BROG","BRTX","BRZE",
    "BSIG","BSRR","BTBT","BTMD","BTRS","BURL","BUSE","BYFC","BYND","BYRN","CADL","CALT",
    "CALX","CAMP","CANO","CARE","CARG","CASH","CASI","CATC","CATO","CBAT","CBAN","CBFV",
    "CBIO","CBRL","CBSH","CBTX","CCBG","CCCC","CCEP","CCRN","CDMO","CDNA","CDRE","CDRO",
    "CELC","CELH","CENTA","CERT","CEVA","CFFI","CFFN","CFLT","CGEM","CGNX","CHCO","CHDN",
    "CHEF","CHGG","CHRS","CHUY","CIFR","CINF","CIVB","CKPT","CLBK","CLBT","CLDT","CLFD",
    "CLMT","CLNC","CLNE","CLNN","CLOV","CLPR","CLPT","CLRB","CLRO","CLSK","CLVT","CLWT",
    "CMBM","CMCO","CMLS","CMMB","CMPO","CMRX","CMTL","CNDT","CNMD","CNNB","CNOB","CNXC",
    "CNXN","CODA","CODX","COEP","COFS","COGT","COHU","COKE","COLB","CONN","CORR","CORS",
    "CORT","CPSI","CRAI","CRDO","CRIS","CRMT","CRNX","CROX","CRSP","CRTD","CRVL","CRVO",
    "CRWS","CSBR","CSGS","CSPI","CSSE","CSTE","CSTL","CTBI","CTGO","CTKB","CTLP","CTOS",
    "CUBI","CURL","CUTR","CVBF","CVCO","CVGW","CVLG","CVLT","CVLY","CWCO","CWEN","CWST",
    "DAKT","DBRG","DBTX","DCBO","DCFC","DCOM","DCPH","DENN","DFIN","DIOD","DLHC","DLTH",
    "DNOW","DNUT","DOOR","DORM","DOUG","DRCT","DRIO","DRNA","DRVN","DSGX","DTST","DUOL",
    "DXPE","DYAI","DYNE","EARN","EBIX","EBTC","ECBK","ECHO","ECPG","EDSA","EFSC","EGAN",
    "EGBN","EGHT","EGIO","EGLE","EGRX","EKSO","ELLO","ELME","EMBC","EMCF","EMKR","ENOV",
    "ENSG","ENTA","ENVB","ENVX","EOLS","EPAC","EPIQ","EPRT","EQBK","ERAS","ERII","ESAB",
    "ESEA","ESGR","ESNT","ESPR","ESSA","ESTE","EVER","EVEX","EVGO","EVLO","EVLV","EVOP",
    "EVRI","EWBC","EXAS","EXFY","EXLS","EXPI","EXPO","EXTR","EZPW","FARO","FBIZ","FBMS",
    "FBNC","FBRT","FCBC","FCBP","FCCO","FDMT","FDUS","FERG","FGBI","FGEN","FIBK","FIHL",
    "FIVE","FIVN","FIZZ","FLGC","FLIC","FLNC","FLNT","FLUX","FMBH","FMBI","FMCB","FMNB",
    "FNKO","FNLC","FNWB","FOLD","FONR","FORR","FOUR","FRAF","FRBA","FRGE","FRHC","FRME",
    "FRPH","FRST","FSEA","FSFG","FSTR","FTDR","FTEK","FTRE","FULT","FUNC","FUSB","FUTU",
    "GAIN","GATO","GBCI","GBIO","GCMG","GENC","GENI","GEOS","GERN","GEVO","GFED","GIII",
    "GLDD","GLNG","GLPG","GLRE","GLYC","GNLN","GNPX","GNTX","GOCO","GOLF","GOOD","GOSS",
    "GPMT","GPOR","GPRE","GRFS","GRND","GRPN","GRTS","GRTX","GSBC","GSIT","GTLS","HAFC",
    "HALO","HARP","HAYN","HBCP","HBIO","HBNC","HCAT","HCCI","HCKT","HCSG","HDSN","HEAR",
    "HEES","HELE","HFWA","HGTY","HIBB","HIFS","HIIQ","HIMS","HIVE","HLMN","HLNE","HLTH",
    "HMST","HNNA","HNST","HOFT","HOLO","HONE","HOTH","HRMY","HROW","HRTG","HRTX","HSII",
    "HSKA","HSON","HTBI","HTBK","HTGM","HTLD","HTLF","HURC","HURN","HVBC","HWBK","HWKN",
    "HYMC","HYRE","IART","IBCP","IBEX","IBIO","IBRX","ICAD","ICFI","ICHR","ICMB","ICPT",
    "IDCC","IDEX","IDYA","IESC","IFRX","IGMS","IHRT","IIIN","IIIV","IKNA","IMCR","IMGO",
    "IMKTA","IMMP","IMMR","IMNN","IMRX","IMTX","IMUX","IMVT","IMXI","INBK","INBS","INCY",
    "INDB","INDP","INDT","INFN","INGN","INMB","INMD","INNV","INPX","INSE","INSM","INSP",
    "INST","INTZ","INVA","INVE","INVH","IPIX","IPSC","IPWR","IRBT","IRDM","IRET","IRMD",
    "IRWD","ISEE","ISPC","ISTR","ITGR","ITRM","ITRN","ITRI","IVAC","IVVD","IZEA","JACK",
    "JAGX","JANX","JBLU","JBSS","JELD","JJSF","JKHY","JNCE","JOBY","JOUT","JRVR","JSPR",
    "JYNT","KALA","KALV","KALU","KARO","KBSF","KBTX","KFRC","KIDS","KION","KIRK","KINS",
    "KLIC","KLTR","KNBE","KNDI","KNSL","KOPN","KPTI","KRMD","KROS","KRTX","KRUS","KRYS",
    "KSCP","KTOS","KVHI","KYMR","KZIA","LBAI","LBPH","LBRT","LCII","LCNB","LCUT","LECO",
    "LEGH","LESL","LGND","LGVN","LIQT","LITE","LIVN","LLNW","LMAT","LMFA","LMNL","LMNR",
    "LNTH","LOCO","LOOP","LOVE","LPCN","LPLA","LPSN","LQDA","LRFC","LSCC","LSEA","LTHM",
    "LTRN","LTRX","LUNA","LUNG","LVOX","LWLG","LYEL","LYRA","LYTS","MACK","MAGS","MARPS",
    "MATW","MAXN","MBCN","MBII","MBIN","MBUU","MBWM","MCBC","MCBS","MCFT","MCRI","MCRB",
    "MDGL","MDNA","MDVX","MEIP","MERC","MESA","METC","MFAC","MFIN","MGEE","MGNX","MGPI",
    "MGRC","MGTA","MIND","MINM","MIRM","MIST","MITK","MKSI","MLAB","MLKN","MLNK","MMSI",
    "MNKD","MNMD","MNPR","MNRO","MNTK","MNTX","MODN","MOFG","MOMO","MORA","MORF","MPAA",
    "MPLN","MPWR","MRAM","MRBK","MRCY","MREO","MRIN","MRKR","MRNS","MRSN","MRTX","MRUS",
    "MSEX","MSFG","MSON","MTCH","MTEX","MTRN","MTRX","MTTR","MTUS","MVBF","MVIS","MXCT",
    "MYMD","MYND","MYPS","MYRG","NARI","NATH","NATR","NAUT","NAVB","NAVI","NBHC","NBIX",
    "NBTB","NCNA","NCSM","NDLS","NDRA","NEOG","NEON","NEPH","NESR","NEXT","NFBK","NGVC",
    "NHHS","NHTC","NICK","NINE","NKLA","NKTR","NLSP","NMIH","NMRA","NNBR","NOMD","NOTE",
    "NRBO","NRDS","NRIM","NRIX","NRXP","NSIT","NSSC","NSTG","NTBL","NTCT","NTGR","NTIC",
    "NTLA","NTNX","NTST","NUVA","NVAX","NVEI","NVST","NWBI","NWFL","NWLI","NXGN","NXRT",
    "NXST","NYMX","OABI","OBNK","OBSV","OCFC","OCGN","OCSL","OCUL","OCUP","OFIX","OFLX",
    "OMCL","OMER","OMGA","ONTF","ONVO","OPBK","OPCH","OPEN","OPGN","OPOF","OPRX","OPTN",
    "ORBC","ORGO","ORGS","ORIC","ORMP","ORRF","OSBC","OSCR","OSIS","OSST","OSTK","OTLK",
    "OTMO","OTRK","OVBC","OVID","OVLY","OWLT","OXLC","OXSQ","OYST","PACK","PACS","PAHC",
    "PATK","PBFS","PBHC","PBIP","PBPB","PCBC","PCCO","PCFG","PCOM","PCOR","PCSA","PCTI",
    "PCVX","PDCO","PDFS","PDSB","PECO","PEGA","PENN","PFBC","PFIS","PFMT","PFNX","PFSI",
    "PGNY","PHAT","PHGE","PHIO","PHVS","PKBK","PKOH","PLBC","PLBY","PLCE","PLRX","PLSE",
    "PLUR","PMCB","PMTS","PNFP","PNTG","POAI","POCI","PODD","POND","POOL","POWI","PPBI",
    "PPBT","PPTA","PRAA","PRAX","PRCH","PRDO","PRFT","PRGS","PRLD","PRME","PRNB","PRPL",
    "PRQR","PRST","PRTA","PRTK","PRTS","PRVA","PSFE","PSMT","PSNL","PSTV","PSTX","PTCT",
    "PTGX","PTHM","PTLO","PTPI","PTVE","PUBM","PVBC","PWOD","PXLW","PYCR","PYXS","QCRH",
    "QDEL","QFIN","QNST","QRTEA","QRTEB","QRVO","QTWO","QUAD","QUBT","QUIK","QURE","RADI",
    "RAPT","RARE","RAVN","RCKT","RCKY","RCON","RCUS","RDCM","RDNT","RDUS","RDVT","RDWR",
    "REAL","REAX","REFI","REGI","REKR","RELI","RELY","RENB","RENN","REPX","REXR","REYN",
    "RFIL","RGCO","RGEN","RGLD","RGLS","RGNX","RGRX","RGTI","RIGL","RIOT","RIVN","RLGT",
    "RLAY","RLMD","RLYB","RMBI","RMBS","RMCF","RMNI","RNAC","RNAZ","RNDB","RNET","RNLX",
    "RNXT","ROCC","ROCR","RONI","ROTH","RPAY","RPTX","RRBI","RRGB","RRST","RRTS","RSSS",
    "RTLR","RUBY","RVSB","RVNC","RVPH","RXDX","RXRX","RYAM","RZLT","SAFE","SAGE","SAIA",
    "SANA","SAND","SATS","SBCF","SBET","SBFG","SBGI","SBIG","SBSI","SBTX","SCHL","SCKT",
    "SCPH","SCSC","SCVL","SDCL","SDGR","SELB","SENS","SERV","SFBC","SFNC","SFST","SGBX",
    "SGMO","SGRY","SGTX","SHBI","SHLS","SHOO","SHPW","SHYF","SIBN","SIEB","SIGA","SIGI",
    "SILK","SILV","SIMO","SINT","SIRE","SITM","SKIN","SKWD","SLCA","SLDB","SLDP","SLGL",
    "SLGN","SLNO","SLNX","SLQT","SMBC","SMBK","SMFL","SMID","SMIT","SMPL","SMSI","SMTC",
    "SNBR","SNCY","SNCR","SNEX","SNOA","SNPO","SNSE","SNSR","SNVX","SOFI","SOHO","SOLO",
    "SOLY","SONX","SOPA","SOPH","SOTK","SPFI","SPKE","SPNE","SPNS","SPOK","SPPI","SPRO",
    "SPRY","SPRX","SPSC","SPTN","SPWH","SPWR","SQFT","SQNS","SQSP","SRCE","SRCL","SRFM",
    "SRGA","SRRK","SRTS","SSBI","SSBK","SSII","SSRM","SSSS","SSYS","STAA","STAG","STBA",
    "STBZ","STCN","STEP","STGW","STIM","STKS","STNE","STOK","STRA","STRS","STRT","STRW",
    "STSS","STVN","STXS","SUMO","SUPN","SURF","SVRA","SWAG","SWAV","SWIM","SWKH","SXTP",
    "SYBT","SYBX","SYRS","TACT","TALO","TALS","TANH","TASK","TAST","TATT","TBCP","TBIO",
    "TBNK","TBPH","TBRG","TCBK","TCBX","TCFC","TCMD","TCON","TCPC","TDUP","TELL","TENB",
    "TENX","TERN","TESS","TFFP","TFII","TFSL","TGLS","TGTX","THFF","THRM","THTX","TILE",
    "TLGA","TLRY","TMBR","TMDI","TMDX","TMHC","TNXP","TORC","TPVG","TRAK","TRAN","TRDA",
    "TREE","TRGP","TRGT","TRIN","TRMK","TRMT","TRNO","TRON","TROO","TRST","TRTN","TRTX",
    "TRUP","TRVG","TRVN","TSEM","TSHA","TSIO","TSLX","TTEC","TTEK","TTGT","TTMI","TTNP",
    "TTSH","TUSK","TUYA","TVIA","TVTX","TWKS","TWIN","TWNI","TWNK","TWST","TXNM","TXRH",
    "UAVS","UBCP","UBFO","UBOH","UBSI","UCBI","UCTT","UFCS","UFPI","UFPT","UGRO","ULCC",
    "ULTA","UMBF","UMPQ","UNAM","UNFI","UNIT","UNTY","UPLD","UPWK","URBN","URGN","USAC",
    "USAK","USAP","USAT","USEI","USFD","USIO","USLM","USNA","USPH","UTHR","UTMD","UVSP",
    "VBFC","VBIV","VBTX","VCNX","VCTR","VCEL","VCYT","VECO","VERA","VERB","VERX","VGFC",
    "VIAV","VICR","VIEW","VIGL","VINC","VIOT","VIPS","VITL","VIVO","VKTX","VLCN","VNDA",
    "VNRX","VOXX","VRAY","VRCA","VRDN","VREX","VRNA","VRNS","VRNT","VRPX","VSCO","VSEC",
    "VSTA","VTGN","VTOL","VTVT","VUZI","VVOS","VXRT","VYNT","WABC","WAFD","WASH","WATT",
    "WAVE","WBHC","WBND","WDFC","WERN","WETF","WEYS","WFRD","WHLM","WHLR","WINA","WING",
    "WINT","WIRE","WKHS","WKME","WLDN","WLFC","WLMS","WNEB","WOLF","WOOF","WORX","WPRT",
    "WRBY","WSBC","WSFS","WTBA","WTFC","WTRG","WULF","XAIR","XBIO","XCUR","XELA","XELB",
    "XENE","XFOR","XNCR","XOMA","XPEL","XPER","XPOF","XRAY","XTNT","XXII","XYLO","YCBD",
    "YEXT","YMAB","YORW","YTEN","YUMC","ZETA","ZEUS","ZFOX","ZGNX","ZIXI","ZLAB","ZNTE",
    "ZNTL","ZSAN","ZTLK","ZVRA","ZYME","ZYXI",
]

UNIVERSE = list(set(SP500 + NASDAQ100 + EXTRAS + RUSSELL2000))

SECTOR_ETFS = {
    "Technology": "XLK", "Healthcare": "XLV", "Financials": "XLF",
    "Energy": "XLE", "Consumer Cyclical": "XLY", "Industrials": "XLI",
    "Communication Services": "XLC", "Consumer Defensive": "XLP",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
}

CORRELATIONS = {
    "NVDA": ["AMD","SMCI","AVGO","AMAT","LRCX","KLAC","MU"],
    "TSLA": ["RIVN","LCID","NIO","XPEV","LI"],
    "COIN": ["MSTR","MARA","RIOT","CLSK","IREN","BTBT","HUT"],
    "MSTR": ["COIN","MARA","RIOT","CLSK"],
    "AAPL": ["MSFT","GOOGL","META","AMZN"],
    "META": ["SNAP","PINS","GOOGL","RBLX"],
    "AMZN": ["SHOP","EBAY","ETSY"],
    "PLTR": ["BBAI","SOUN"],
    "AMD":  ["NVDA","INTC","QCOM","AVGO"],
    "SMCI": ["NVDA","AMD","HPQ"],
}

# Sectores afectados por eventos geopolíticos
GEO_SECTOR_MAP = {
    "middle_east":  {"up": ["XLE","XLB"],   "down": ["XLY","XLC"]},
    "china_us":     {"up": ["XLB","XLI"],   "down": ["XLK","XLY"]},
    "ukraine":      {"up": ["XLE","XLB"],   "down": ["XLI","XLY"]},
    "fed_hawkish":  {"up": ["XLF","XLU"],   "down": ["XLK","XLRE"]},
    "fed_dovish":   {"up": ["XLK","XLRE"],  "down": ["XLF"]},
    "recession":    {"up": ["XLP","XLU"],   "down": ["XLY","XLK"]},
}

# ═══════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════════════

predictions    = []
watch_signals  = {}
earnings_watch = {}   # {ticker: {date, days_ahead, name, time}} — rellenado cada mañana
learnings      = {
    "rules": [], "sector_memory": {}, "hour_memory": {},
    "error_memory": [], "regime_memory": {}, "last_updated": None,
}
catalyst_memory = {}   # {sector: {catalyst_keyword: {wins, total}}}
econ_calendar   = {    # eventos económicos del día
    "high_impact_today": [],   # ["Fed Rate Decision", "CPI", ...]
    "is_high_impact":    False,
    "updated_at":        None,
}
market_context = {
    "fear_greed": 50, "sp500_change": 0.0, "vix": 15.0,
    "macro_news": [], "economic_events": [], "updated_at": None,
    "geopolitical_context": [],   # eventos geo detectados
    "sector_bias": {},            # {sector_etf: "up"/"down"/"neutral"}
}
market_regime  = {
    "regime": "UNKNOWN", "strength": 0, "description": "", "updated_at": None,
}
status_msg_id     = None
last_cmd_msg_id   = None
processed_cmd_ids = set()

# Rate limiting !analizar
cmd_rate_limit    = {}   # {user_id: [timestamps]}
_hist_cache       = {}   # {ticker: DataFrame} — batch prefetch

# Watchdog
ciclos_429_seguidos = 0
pausa_429_hasta     = None
coste_estimado_hoy  = 0.0
ai_calls_hoy        = 0

# Circuit breaker — 3 pérdidas consecutivas → umbral +5% durante 24h
_cb_consecutive_losses = 0
_cb_active_until       = None   # datetime o None
CB_MAX_LOSSES          = 3
CB_CONF_BOOST          = 5      # puntos extra al umbral mínimo
CB_DURATION_HOURS      = 24

# ═══════════════════════════════════════════════════════════════════════
# PERSISTENCIA
# ═══════════════════════════════════════════════════════════════════════

def load_state():
    global predictions, watch_signals, learnings, market_regime, catalyst_memory, econ_calendar, earnings_watch
    os.makedirs("/app/data", exist_ok=True)

    for var_name, filepath, default in [
        ("predictions",     PREDICTIONS_FILE,     []),
        ("watch_signals",   WATCHSTATE_FILE,       {}),
        ("learnings",       LEARNINGS_FILE,        learnings),
        ("market_regime",   REGIME_FILE,           market_regime),
        ("catalyst_memory", CATALYST_MEMORY_FILE,  {}),
        ("econ_calendar",   ECON_CALENDAR_FILE,    econ_calendar),
        ("earnings_watch",  EARNINGS_WATCH_FILE,   {}),
    ]:
        try:
            if os.path.exists(filepath):
                with open(filepath) as f:
                    data = json.load(f)
                if   var_name == "predictions":     predictions     = data
                elif var_name == "watch_signals":   watch_signals   = data
                elif var_name == "learnings":       learnings       = data
                elif var_name == "market_regime":   market_regime   = data
                elif var_name == "catalyst_memory": catalyst_memory = data
                elif var_name == "econ_calendar":   econ_calendar   = data
                elif var_name == "earnings_watch":  earnings_watch  = data
        except Exception as e:
            print(f"  ERROR cargando {filepath}: {e}")

    print(f"  Estado cargado: {len(predictions)} predicciones | universo: {len(UNIVERSE)} acciones")
    resolved = len([p for p in predictions if p.get("result") != "pending"])
    print(f"  Predicciones resueltas: {resolved} | reglas aprendidas: {len(learnings.get('rules', []))}")


def save_state():
    for data, filepath in [
        (predictions,     PREDICTIONS_FILE),
        (watch_signals,   WATCHSTATE_FILE),
        (learnings,       LEARNINGS_FILE),
        (market_regime,   REGIME_FILE),
        (catalyst_memory, CATALYST_MEMORY_FILE),
        (econ_calendar,   ECON_CALENDAR_FILE),
        (earnings_watch,  EARNINGS_WATCH_FILE),
    ]:
        try:
            tmp_path = filepath + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, filepath)  # atómico en POSIX
        except Exception as e:
            print(f"  ERROR guardando {filepath}: {e}")


def _target_multiplier(signal):
    """Target dinámico según régimen: BULL=18%, LATERAL=12%, BEAR=8%."""
    regime = market_regime.get("regime", "LATERAL")
    mults = {"BULL": 0.18, "LATERAL": 0.12, "BEAR": 0.08}
    pct = mults.get(regime, 0.12)
    return (1 + pct) if signal == "COMPRAR" else (1 - pct)


def save_prediction(ticker, signal, tech, conf, signal_type="NORMAL", stop_ia=None):
    """Guarda predicción enriquecida con tipo de señal."""
    price = tech.get("price", 0)
    # Stop loss: usar el de la IA si es válido, sino fallback técnico
    if stop_ia and price > 0:
        stop_val = round(stop_ia, 2)
    else:
        stop_val = round(tech.get("rl", price * 0.93), 2)
    predictions.append({
        "ticker": ticker, "signal": signal, "confidence": conf,
        "signal_type": signal_type,   # NORMAL / PRE_EARNINGS / SHORT_SQUEEZE / INSIDER_MASSIVE
        "date": datetime.now(SPAIN_TZ).isoformat(),
        "result": "pending", "exit_price": None, "days_to_result": None,
        "entry":  round(price, 2),
        "target": round(price * _target_multiplier(signal), 2),
        "stop":   stop_val,
        "stop_source": "ia" if stop_ia else "tecnico",
        "rsi": tech.get("rsi"), "rsi_zone": tech.get("rsi_zone"),
        "macd_bullish": tech.get("macd_bullish"), "stoch_k": tech.get("stoch_k"),
        "vol_ratio": tech.get("vol_ratio"), "obv_trend": tech.get("obv_trend"),
        "mom1m": tech.get("mom1m"), "mom3m": tech.get("mom3m"),
        "tf_confluence": tech.get("tf_confluence"), "structure": tech.get("structure"),
        "tech_score": tech.get("tech_score"), "support_touches": tech.get("support_touches"),
        "dist_h52": tech.get("dist_h"), "dist_l52": tech.get("dist_l"),
        "fear_greed": market_context.get("fear_greed"),
        "vix": market_context.get("vix"),
        "sp500_change": market_context.get("sp500_change"),
        "regime": market_regime.get("regime"),
        "sector": tech.get("sector"),
        "hour": datetime.now(SPAIN_TZ).hour,
        "session": _session_label(datetime.now(SPAIN_TZ)),
        "day_of_week": datetime.now(SPAIN_TZ).weekday(),
        "is_high_impact_day": econ_calendar.get("is_high_impact", False),
    })
    save_state()

# ═══════════════════════════════════════════════════════════════════════
# LÍMITES DIARIOS
# ═══════════════════════════════════════════════════════════════════════

def _preds_today():
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
    return any(p["ticker"] == ticker for p in _preds_today())


def alertas_hoy():
    return len(_preds_today())


def ventas_hoy():
    return sum(1 for p in _preds_today() if p["signal"] == "VENDER")


def pre_earnings_hoy():
    return sum(1 for p in _preds_today() if p.get("signal_type") == "PRE_EARNINGS")


def puede_enviar_alerta(signal, conf, signal_type="NORMAL"):
    now = datetime.now(SPAIN_TZ)
    now_hour   = now.hour
    now_minute = now.minute

    # Cola de calidad: antes de HORA_DESBLOQUEO solo Excepcionales
    if conf < CONF_EXCEPCIONAL and now_hour < HORA_DESBLOQUEO:
        return False, f"cola de calidad activa hasta las {HORA_DESBLOQUEO}:00"

    # Franja suave pre-apertura USA (15:00–15:30): umbral FUERTE mínimo
    if conf < CONF_EXCEPCIONAL and now_hour == 15 and now_minute < 30 and conf < CONF_FUERTE:
        return False, f"pre-apertura USA — confianza mínima {CONF_FUERTE}% hasta las 15:30"

    # Circuit breaker: racha de pérdidas → umbral mínimo elevado
    if circuit_breaker_active() and conf < (CONF_NORMAL + CB_CONF_BOOST):
        until_str = _cb_active_until.strftime("%d/%m %H:%M") if _cb_active_until else "?"
        return False, f"circuit breaker activo — mínimo {CONF_NORMAL + CB_CONF_BOOST}% hasta {until_str}"

    # Límite total (aplica también a señales excepcionales)
    if alertas_hoy() >= MAX_ALERTAS_DIA:
        return False, f"límite diario ({MAX_ALERTAS_DIA}) alcanzado"

    # Límite ventas
    if signal == "VENDER" and ventas_hoy() >= MAX_VENTAS_DIA:
        return False, "límite de ventas diario alcanzado"

    # Límite PRE-EARNINGS
    if signal_type == "PRE_EARNINGS" and pre_earnings_hoy() >= MAX_PRE_EARNINGS_DIA:
        return False, "límite de señales PRE-EARNINGS alcanzado"

    # Días de alto impacto macro: subir umbral
    if econ_calendar.get("is_high_impact") and conf < CONF_FUERTE:
        events = ", ".join(econ_calendar.get("high_impact_today", []))
        return False, f"día de alto impacto macro ({events}) — confianza mínima {CONF_FUERTE}%"

    return True, None


def _session_label(now):
    m = now.hour * 60 + now.minute
    if  540 <= m <  570: return "PREMARKET_EARLY"
    if  570 <= m <  930: return "PREMARKET"
    if  930 <= m < 1320: return "MERCADO"
    if 1320 <= m < 1440: return "AFTERHOURS"
    return "FUERA DE MERCADO"


def _to_aware(dt_str, default="2000-01-01T00:00:00+00:00"):
    """Parsea un string ISO y lo convierte a timezone-aware (SPAIN_TZ)."""
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = SPAIN_TZ.localize(dt)
        return dt.astimezone(SPAIN_TZ)
    except Exception:
        return datetime.fromisoformat(default).astimezone(SPAIN_TZ)


def is_premarket():
    now = datetime.now(SPAIN_TZ)
    return (9 <= now.hour < 15) or (now.hour == 15 and now.minute < 30)


# ═══════════════════════════════════════════════════════════════════════
# FUENTES EXTERNAS — Stocktwits, Investing.com, calendario económico
# Put/call ratio, contexto geopolítico
# ═══════════════════════════════════════════════════════════════════════

def get_stocktwits_sentiment(ticker):
    """
    Sentimiento retail de Stocktwits.
    Devuelve (bullish_pct, bearish_pct, message_count, trending).
    """
    try:
        r = requests.get(
            f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
        )
        if r.status_code != 200:
            return None

        data     = r.json()
        messages = data.get("messages", [])
        if not messages:
            return None

        bullish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        bearish = sum(1 for m in messages if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish")
        total   = bullish + bearish

        if total == 0:
            return None

        bullish_pct = round(bullish / total * 100, 1)
        bearish_pct = round(bearish / total * 100, 1)
        trending    = len(messages) >= 15   # activo si hay 15+ mensajes recientes

        return {
            "bullish_pct":   bullish_pct,
            "bearish_pct":   bearish_pct,
            "message_count": len(messages),
            "trending":      trending,
            "label":         "MUY ALCISTA" if bullish_pct > 75 else
                             "ALCISTA"     if bullish_pct > 55 else
                             "MUY BAJISTA" if bearish_pct > 75 else
                             "BAJISTA"     if bearish_pct > 55 else "MIXTO",
        }
    except Exception as e:
        print(f"    Stocktwits {ticker}: {e}")
        return None


def get_investing_news(ticker):
    """
    Noticias de Investing.com vía RSS.
    Devuelve lista de titulares recientes.
    """
    news = []
    try:
        feed = feedparser.parse(
            f"https://www.investing.com/rss/news_25.rss",
        )
        keyword = ticker.lower()
        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            if keyword in title.lower() or keyword in entry.get("summary", "").lower():
                news.append(title)
            if len(news) >= 4:
                break
    except Exception as e:
        print(f"    Investing.com news: {e}")
    return news


def get_put_call_ratio(ticker):
    """
    Intenta obtener put/call ratio de Yahoo Finance options.
    Devuelve (ratio, interpretación) o (None, None).
    Un ratio < 0.7 es alcista (más calls), > 1.2 es bajista (más puts).
    """
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v7/finance/options/{ticker}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        if r.status_code != 200:
            return None, None

        data    = r.json().get("optionChain", {}).get("result", [])
        if not data:
            return None, None

        options = data[0].get("options", [{}])[0]
        puts    = len(options.get("puts",  []))
        calls   = len(options.get("calls", []))

        if calls == 0:
            return None, None

        ratio = round(puts / calls, 2)
        if ratio < 0.5:    interp = "MUY ALCISTA (calls dominan)"
        elif ratio < 0.7:  interp = "ALCISTA"
        elif ratio < 1.0:  interp = "NEUTRAL-ALCISTA"
        elif ratio < 1.2:  interp = "NEUTRAL-BAJISTA"
        elif ratio < 1.5:  interp = "BAJISTA"
        else:              interp = "MUY BAJISTA (puts dominan)"

        return ratio, interp
    except Exception as e:
        return None, None


def update_econ_calendar():
    """
    Actualiza el calendario económico del día usando Investing.com RSS
    y detección de palabras clave en noticias macro.
    """
    global econ_calendar
    high_impact_keywords = {
        "Federal Reserve": "Fed Decision",
        "Fed Rate":        "Fed Decision",
        "Interest Rate":   "Fed Decision",
        "CPI":             "CPI Inflation",
        "Consumer Price":  "CPI Inflation",
        "NFP":             "Non-Farm Payrolls",
        "Non-Farm":        "Non-Farm Payrolls",
        "Jobs Report":     "Non-Farm Payrolls",
        "GDP":             "GDP Report",
        "Unemployment":    "Unemployment Data",
        "FOMC":            "FOMC Meeting",
        "Powell":          "Fed Speech",
    }

    found_events = []
    try:
        feed = feedparser.parse("https://www.investing.com/rss/news_301.rss")
        for entry in feed.entries[:30]:
            title = entry.get("title", "") + " " + entry.get("summary", "")
            for keyword, event_name in high_impact_keywords.items():
                if keyword.lower() in title.lower() and event_name not in found_events:
                    found_events.append(event_name)
    except Exception as e:
        print(f"[ERROR] econ_calendar RSS: {e}")

    # También revisar macro_news del contexto
    for news in market_context.get("macro_news", []):
        for keyword, event_name in high_impact_keywords.items():
            if keyword.lower() in news.lower() and event_name not in found_events:
                found_events.append(event_name)

    econ_calendar = {
        "high_impact_today": found_events,
        "is_high_impact":    len(found_events) > 0,
        "updated_at":        datetime.now(SPAIN_TZ).isoformat(),
    }
    save_state()

    if found_events:
        print(f"  Calendario económico: ⚠️ ALTO IMPACTO — {', '.join(found_events)}")
        send_log(f"⚠️ Eventos macro hoy: {', '.join(found_events)} — umbral de confianza elevado")
    else:
        print(f"  Calendario económico: sin eventos de alto impacto")


def detect_geopolitical_context():
    """
    Detecta contexto geopolítico relevante en noticias macro.
    Actualiza market_context['sector_bias'] para que el prompt
    sepa qué sectores priorizar/evitar.
    """
    geo_keywords = {
        "middle east": "middle_east",
        "israel":      "middle_east",
        "iran":        "middle_east",
        "china tariff":"china_us",
        "trade war":   "china_us",
        "ukraine":     "ukraine",
        "russia":      "ukraine",
        "hawkish":     "fed_hawkish",
        "rate hike":   "fed_hawkish",
        "dovish":      "fed_dovish",
        "rate cut":    "fed_dovish",
        "recession":   "recession",
    }

    all_news = (
        market_context.get("macro_news", []) +
        market_context.get("economic_events", [])
    )
    all_text   = " ".join(all_news).lower()
    detected   = []
    sector_bias = {}

    for keyword, geo_type in geo_keywords.items():
        if keyword in all_text and geo_type not in detected:
            detected.append(geo_type)
            for etf in GEO_SECTOR_MAP.get(geo_type, {}).get("up", []):
                sector_bias[etf] = "up"
            for etf in GEO_SECTOR_MAP.get(geo_type, {}).get("down", []):
                if etf not in sector_bias:
                    sector_bias[etf] = "down"

    market_context["geopolitical_context"] = detected
    market_context["sector_bias"]          = sector_bias

    if detected:
        print(f"  Contexto geopolítico: {detected} → bias sectorial: {sector_bias}")

# ═══════════════════════════════════════════════════════════════════════
# DISCORD — igual que v5
# ═══════════════════════════════════════════════════════════════════════

def _discord_post(channel_id, text):
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
    global status_msg_id
    auth = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    try:
        if not status_msg_id:
            r = requests.get(
                f"https://discord.com/api/v10/channels/{DISCORD_STATUS_ID}/messages?limit=20",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=10,
            )
            if r.status_code == 200:
                try:
                    for m in r.json():
                        if m.get("author", {}).get("bot"):
                            status_msg_id = m["id"]
                            break
                except (ValueError, KeyError) as e:
                    print(f"[ERROR] update_status JSON GET: {e}")

        if status_msg_id:
            r = requests.patch(
                f"https://discord.com/api/v10/channels/{DISCORD_STATUS_ID}/messages/{status_msg_id}",
                json={"content": text}, headers=auth, timeout=10,
            )
            if r.status_code in (200, 201):
                return
            status_msg_id = None

        r = requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_STATUS_ID}/messages",
            json={"content": text}, headers=auth, timeout=10,
        )
        if r.status_code in (200, 201):
            try:
                status_msg_id = r.json().get("id")
            except (ValueError, KeyError) as e:
                print(f"[ERROR] update_status JSON POST: {e}")
    except Exception as e:
        print(f"  Status error: {e}")


def post_instrucciones():
    try:
        r = requests.get(
            f"https://discord.com/api/v10/channels/{DISCORD_INSTRUCCIONES_ID}/messages?limit=20",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=10,
        )
        if r.status_code == 200:
            try:
                _msgs = r.json()
            except ValueError as e:
                print(f"[ERROR] post_instrucciones JSON: {e}")
                _msgs = []
            for m in _msgs:
                if m.get("author", {}).get("bot"):
                    requests.delete(
                        f"https://discord.com/api/v10/channels/{DISCORD_INSTRUCCIONES_ID}/messages/{m['id']}",
                        headers={"Authorization": f"Bot {DISCORD_TOKEN}"}, timeout=5,
                    )
                    time.sleep(0.5)
    except Exception as e:
        print(f"[ERROR] post_instrucciones limpieza: {e}")

    resolved  = len([p for p in predictions if p.get("result") != "pending"])
    rules_cnt = len(learnings.get("rules", []))
    regime    = market_regime.get("regime", "UNKNOWN")
    eco_warn  = "⚠️ ALTO IMPACTO HOY" if econ_calendar.get("is_high_impact") else ""

    _discord_post(DISCORD_INSTRUCCIONES_ID, f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
📖  **CÓMO FUNCIONA STOCKBOT PRO v5.2**
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍  **Análisis automático**
Vigila ~{len(UNIVERSE)} acciones cada 5 min.
Máximo 3 alertas al día. Solo Excepcionales antes de las 14:00h. {eco_warn}

🧠  **Aprendizaje autónomo**
Predicciones resueltas: {resolved} | Reglas aprendidas: {rules_cnt}
Régimen de mercado: {regime}

⚡  **Niveles de confianza**
🟢  Normal 85-87% | 🔥  Fuerte 88-93% | ⚡  Excepcional 94%+
📅  PRE-EARNINGS — señal especial antes de resultados
🔀  SHORT SQUEEZE — posición corta atrapada
👥  INSIDERS — compras masivas internas

🎯  **Bajo demanda:** `!analizar NVDA` en #solicitud-en-concreto""")

    time.sleep(1)
    _discord_post(DISCORD_INSTRUCCIONES_ID, """━━━━━━━━━━━━━━━━━━━━━━━━━━━
📡  **CANALES**
**#stock-alerts** — alertas automáticas
**#aciertos-bot** — resumen dominical
**#solicitud-en-concreto** — análisis bajo demanda
**#log-bot** — actividad interna
**#status** — estado en tiempo real
━━━━━━━━━━━━━━━━━━━━━━━━━━━""")

# ═══════════════════════════════════════════════════════════════════════
# CONTEXTO MACRO + RÉGIMEN
# ═══════════════════════════════════════════════════════════════════════

def _fg_label(fg):
    if fg < 20: return "PÁNICO EXTREMO"
    if fg < 40: return "Miedo"
    if fg < 60: return "Neutral"
    if fg < 80: return "Codicia"
    return "EUFORIA"


def fetch_earnings_calendar():
    """
    Obtiene earnings de los próximos 7 días laborables desde la API pública de Nasdaq.
    Coste: ~5 peticiones HTTP ligeras por semana (una por día laboral), 0 tokens IA.
    Devuelve dict {ticker: {date, days_ahead, name, time}} filtrado al universo propio.
    """
    from datetime import timedelta as _td
    universe_set = set(UNIVERSE)
    found = {}
    today = datetime.now(SPAIN_TZ).date()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    for i in range(1, 8):
        target_date = today + _td(days=i)
        if target_date.weekday() >= 5:   # saltar fin de semana
            continue
        try:
            url = f"https://api.nasdaq.com/api/calendar/earnings?date={target_date}"
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code != 200:
                continue
            rows = (r.json().get("data") or {}).get("rows") or []
            for row in rows:
                sym = (row.get("symbol") or "").upper().strip()
                if sym in universe_set and sym not in found:
                    found[sym] = {
                        "date":       str(target_date),
                        "days_ahead": i,
                        "name":       row.get("name", sym),
                        "time":       row.get("time", "?"),   # "time-after-hours" / "time-pre-market"
                    }
            time.sleep(1)   # respetar rate-limit Nasdaq
        except Exception as e:
            print(f"  earnings_calendar día+{i}: {e}")
    return found


def update_earnings_watch():
    """
    Actualiza el watch semanal de earnings. Se ejecuta a las 09:00 cada día laboral.
    Solo hace peticiones HTTP, nunca llama a la IA.
    """
    global earnings_watch
    now = datetime.now(SPAIN_TZ)
    if now.weekday() >= 5:
        return
    print("  📅 Escaneando earnings próximos 7 días...")
    try:
        new_watch = fetch_earnings_calendar()
        earnings_watch = new_watch

        if earnings_watch:
            items_str = ", ".join(
                f"{t}({v['days_ahead']}d)" for t, v in
                sorted(earnings_watch.items(), key=lambda x: x[1]["days_ahead"])[:12]
            )
            extra = f" (+{len(earnings_watch)-12} más)" if len(earnings_watch) > 12 else ""
            print(f"  📅 {len(earnings_watch)} acciones del universo con earnings próximos: {items_str}{extra}")

            # Notificar en Discord solo una vez al día (persiste en econ_calendar para sobrevivir reinicios)
            today_str = now.date().isoformat()
            if econ_calendar.get("earnings_notif_date") != today_str:
                lines = [f"📅 **EARNINGS SCANNER** — {len(earnings_watch)} acciones en tu universo esta semana"]
                for tk, info in sorted(earnings_watch.items(), key=lambda x: x[1]["days_ahead"]):
                    t_tag = "🌅 pre-mkt" if "pre" in info.get("time","").lower() else "🌆 post-cierre" if "after" in info.get("time","").lower() else "🕐 horario"
                    lines.append(f"  • **{tk}** — {info['date']} ({info['days_ahead']}d) {t_tag}")
                send_solicitud("\n".join(lines))
                econ_calendar["earnings_notif_date"] = today_str
            else:
                print("  📅 Notificación earnings ya enviada hoy — saltando Discord")
        else:
            print("  📅 Sin earnings en el universo los próximos 7 días")
        save_state()
    except Exception as e:
        print(f"  ERROR update_earnings_watch: {e}")


def update_market_context():
    global market_context
    print("  Actualizando contexto macro...")

    # Fear & Greed del mercado de acciones (CNN) — fallback a crypto (alternative.me)
    fg = 50
    try:
        import fear_and_greed
        info = fear_and_greed.get()
        fg = int(info.value)
    except Exception:
        try:
            r = requests.get("https://api.alternative.me/fng/", timeout=8)
            if r.status_code == 200:
                _fng_data = r.json().get("data") or []
                if _fng_data:
                    fg = int(_fng_data[0]["value"])
        except Exception as e:
            print(f"[ERROR] fear_greed fallback: {e}")

    sp500_change, vix = 0.0, 15.0
    for symbol, key in [("SPY", "sp500"), ("^VIX", "vix")]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if r.status_code == 200:
                _chart_result = r.json().get("chart", {}).get("result") or []
                if _chart_result:
                    closes = [c for c in _chart_result[0]["indicators"]["quote"][0].get("close", []) if c]
                    if len(closes) >= 2:
                        if key == "sp500":
                            sp500_change = round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2)
                        else:
                            vix = round(closes[-1], 1)
            time.sleep(0.5)
        except Exception as e:
            print(f"[ERROR] update_market_context {symbol}: {e}")

    macro_news, econ_events = [], []
    if NEWS_API_KEY:
        try:
            r = requests.get(
                f"https://newsapi.org/v2/top-headlines?category=business&language=en&pageSize=6&apiKey={NEWS_API_KEY}",
                timeout=10,
            )
            if r.status_code == 200:
                macro_news = [a.get("title", "") for a in r.json().get("articles", [])[:6]]
        except Exception as e:
            print(f"[ERROR] macro_news NewsAPI: {e}")
        try:
            r = requests.get(
                f"https://newsapi.org/v2/everything?q=Federal+Reserve+OR+CPI+OR+inflation&language=en&sortBy=publishedAt&pageSize=4&apiKey={NEWS_API_KEY}",
                timeout=10,
            )
            if r.status_code == 200:
                econ_events = [a.get("title", "") for a in r.json().get("articles", [])[:4]]
        except Exception as e:
            print(f"[ERROR] econ_events NewsAPI: {e}")

    market_context.update({
        "fear_greed": fg, "sp500_change": sp500_change, "vix": vix,
        "macro_news": macro_news, "economic_events": econ_events,
        "updated_at": datetime.now(SPAIN_TZ).strftime("%H:%M"),
    })

    fg_str = _fg_label(fg)
    print(f"  Fear&Greed: {fg} ({fg_str}) | S&P500: {sp500_change:+.2f}% | VIX: {vix}")
    send_log(f"📊 Macro — Fear&Greed: {fg} ({fg_str}) | S&P500: {sp500_change:+.2f}% | VIX: {vix}")

    # Persistir market_context para el dashboard web
    try:
        import json as _json
        with open(MARKET_CONTEXT_FILE, "w") as _f:
            _json.dump(market_context, _f)
    except Exception:
        pass

    detect_market_regime()
    detect_geopolitical_context()
    update_econ_calendar()


def detect_market_regime():
    global market_regime
    try:
        spy_closes = []
        r = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&range=3mo",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        if r.status_code == 200:
            _spy_result = r.json().get("chart", {}).get("result") or []
            spy_closes = [c for c in _spy_result[0]["indicators"]["quote"][0].get("close", []) if c] if _spy_result else []

        if not spy_closes or len(spy_closes) < 20:
            print("  detect_market_regime: datos SPY insuficientes — manteniendo régimen anterior")
            return

        price  = spy_closes[-1]
        sma20  = sum(spy_closes[-20:]) / 20
        sma50  = sum(spy_closes[-50:]) / 50 if len(spy_closes) >= 50 else sma20
        mom1m  = ((price - spy_closes[-22]) / spy_closes[-22] * 100) if len(spy_closes) >= 22 else 0
        mom3m  = ((price - spy_closes[-66]) / spy_closes[-66] * 100) if len(spy_closes) >= 66 else 0
        vix    = market_context.get("vix", 15)
        fg     = market_context.get("fear_greed", 50)

        bull_score = bear_score = 0
        if price > sma20: bull_score += 2
        else:             bear_score += 2
        if price > sma50: bull_score += 2
        else:             bear_score += 2
        if mom1m > 2:     bull_score += 2
        elif mom1m < -2:  bear_score += 2
        if mom3m > 5:     bull_score += 3
        elif mom3m < -5:  bear_score += 3
        if vix < 18:      bull_score += 2
        elif vix > 25:    bear_score += 2
        if fg > 60:       bull_score += 1
        elif fg < 30:     bear_score += 1

        total = bull_score + bear_score
        if total == 0:
            regime, strength, desc = "LATERAL", 50, "Sin dirección clara"
        elif bull_score > bear_score * 1.5:
            strength = min(int(bull_score / total * 100), 99)
            regime, desc = "BULL", f"Tendencia alcista | SPY {mom3m:+.1f}% en 3m | VIX {vix}"
        elif bear_score > bull_score * 1.5:
            strength = min(int(bear_score / total * 100), 99)
            regime, desc = "BEAR", f"Tendencia bajista | SPY {mom3m:+.1f}% en 3m | VIX {vix}"
        else:
            strength = 50
            regime, desc = "LATERAL", f"Sin dirección clara | SPY {mom3m:+.1f}% en 3m | VIX {vix}"

        prev = market_regime.get("regime", "UNKNOWN")
        market_regime = {
            "regime": regime, "strength": strength, "description": desc,
            "spy_mom1m": round(mom1m, 1), "spy_mom3m": round(mom3m, 1),
            "vix": vix, "updated_at": datetime.now(SPAIN_TZ).isoformat(),
        }
        save_state()
        print(f"  Régimen: {regime} ({strength}%) — {desc}")
        if prev != regime and prev != "UNKNOWN":
            send_log(f"🔄 Cambio régimen: {prev} → {regime}")
    except Exception as e:
        print(f"  detect_market_regime error: {e} — manteniendo régimen anterior ({market_regime.get('regime','?')})")


def get_regime_conf_adjustment():
    regime = market_regime.get("regime", "UNKNOWN")
    return {
        "BULL":    {"COMPRAR": 0, "VENDER": 3},
        "BEAR":    {"COMPRAR": 3, "VENDER": 0},
        "LATERAL": {"COMPRAR": 2, "VENDER": 2},
    }.get(regime, {"COMPRAR": 0, "VENDER": 0})


# ═══════════════════════════════════════════════════════════════════════
# CONFLUENCIA DINÁMICA DE TIMEFRAMES SEGÚN RÉGIMEN
# En BEAR: mensual manda más. En BULL: diario manda más.
# ═══════════════════════════════════════════════════════════════════════

def get_tf_weights():
    """
    Devuelve pesos (diario, semanal, mensual) según régimen.
    Suma siempre 1.0.
    """
    regime = market_regime.get("regime", "UNKNOWN")
    return {
        "BULL":    (0.50, 0.30, 0.20),
        "BEAR":    (0.20, 0.30, 0.50),
        "LATERAL": (0.35, 0.35, 0.30),
    }.get(regime, (0.35, 0.35, 0.30))


def calc_weighted_tf_confluence(tech):
    """
    Calcula confluencia ponderada según régimen.
    Devuelve (score_0_a_3, descripción).
    """
    w_d, w_w, w_m = get_tf_weights()
    regime = market_regime.get("regime", "UNKNOWN")

    d_bull = 1.0 if tech.get("daily_trend")   == "ALCISTA" else 0.0
    w_bull = 1.0 if tech.get("weekly_trend")  == "ALCISTA" else 0.0
    m_bull = 1.0 if tech.get("monthly_trend") == "ALCISTA" else 0.0

    score   = d_bull * w_d + w_bull * w_w + m_bull * w_m
    raw_pct = round(score * 100, 0)

    if score >= 0.8:   label = "CONFLUENCIA ALCISTA FUERTE"
    elif score >= 0.5: label = "MAYORIA ALCISTA"
    elif score > 0.2:  label = "MAYORIA BAJISTA"
    else:              label = "CONFLUENCIA BAJISTA FUERTE"

    return round(score, 2), f"{label} ({raw_pct}% ponderado — régimen {regime}: D{int(w_d*100)}%/S{int(w_w*100)}%/M{int(w_m*100)}%)"


# ═══════════════════════════════════════════════════════════════════════
# DETECCIÓN DE DIVERGENCIAS RSI/PRECIO (2 timeframes mínimo)
# Solo señal si aparece en diario Y semanal simultáneamente
# ═══════════════════════════════════════════════════════════════════════

def detect_divergence(closes_d, rsi_d, closes_w, rsi_w):
    """
    Divergencia alcista: precio baja pero RSI sube (señal de reversión al alza)
    Divergencia bajista: precio sube pero RSI baja (señal de reversión a la baja)
    Solo cuenta si aparece en AMBOS timeframes (diario y semanal).
    Devuelve (tipo, descripción) o (None, None).
    """
    if not closes_d or not closes_w or rsi_d is None or rsi_w is None:
        return None, None

    # Diario: comparar últimos 5 días
    if len(closes_d) >= 5:
        price_change_d = closes_d[-1] - closes_d[-5]
        # Aproximación RSI trend: usamos cambio de precio reciente
        rsi_proxy_d    = sum(closes_d[-3:]) / 3 - sum(closes_d[-6:-3]) / 3 if len(closes_d) >= 6 else 0
        div_bull_d = price_change_d < 0 and rsi_proxy_d > 0 and rsi_d < 40
        div_bear_d = price_change_d > 0 and rsi_proxy_d < 0 and rsi_d > 60
    else:
        div_bull_d = div_bear_d = False

    # Semanal: comparar últimas 3 semanas
    if len(closes_w) >= 3:
        price_change_w = closes_w[-1] - closes_w[-3]
        rsi_proxy_w    = sum(closes_w[-2:]) / 2 - sum(closes_w[-4:-2]) / 2 if len(closes_w) >= 4 else 0
        div_bull_w = price_change_w < 0 and rsi_proxy_w > 0
        div_bear_w = price_change_w > 0 and rsi_proxy_w < 0
    else:
        div_bull_w = div_bear_w = False

    # Solo señal si aparece en AMBOS timeframes
    if div_bull_d and div_bull_w:
        return "ALCISTA", f"Divergencia alcista confirmada en diario+semanal (RSI {rsi_d:.0f} con precio bajando)"
    if div_bear_d and div_bear_w:
        return "BAJISTA", f"Divergencia bajista confirmada en diario+semanal (RSI {rsi_d:.0f} con precio subiendo)"

    return None, None


# ═══════════════════════════════════════════════════════════════════════
# DETECCIÓN DE GAP DE APERTURA
# ═══════════════════════════════════════════════════════════════════════

def get_premarket_data(ticker):
    """
    Obtiene precio premarket y calcula gap respecto al cierre anterior.
    Devuelve dict con gap_pct, premarket_price, premarket_volume o None.
    """
    try:
        _pm_wait = 5
        for _pm_attempt in range(3):
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d&prePost=true",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if r.status_code == 429:
                print(f"[WARN] get_premarket_data 429 {ticker}, reintento en {_pm_wait}s")
                time.sleep(_pm_wait)
                _pm_wait *= 2
                continue
            break
        else:
            return None
        if r.status_code != 200:
            return None

        result = r.json().get("chart", {}).get("result", [])
        if not result:
            return None

        meta             = result[0].get("meta", {})
        prev_close       = meta.get("chartPreviousClose") or meta.get("previousClose")
        pre_price        = meta.get("preMarketPrice")
        pre_volume       = meta.get("preMarketVolume", 0)
        regular_volume   = meta.get("regularMarketVolume", 0)
        avg_volume       = meta.get("averageDailyVolume3Month", 1) or 1

        if not prev_close or not pre_price:
            return None

        gap_pct       = round(((pre_price - prev_close) / prev_close) * 100, 2)
        pre_vol_ratio = round(pre_volume / (avg_volume / 6.5), 2) if avg_volume > 0 else 0  # 6.5h sesión normal

        return {
            "pre_price":     round(pre_price, 2),
            "prev_close":    round(prev_close, 2),
            "gap_pct":       gap_pct,
            "pre_volume":    pre_volume,
            "pre_vol_ratio": pre_vol_ratio,
            "gap_up":        gap_pct >= 3,
            "gap_down":      gap_pct <= -3,
            "significant":   abs(gap_pct) >= 3,
        }
    except Exception as e:
        return None


# ═══════════════════════════════════════════════════════════════════════
# SHORT SQUEEZE DETECTOR
# ═══════════════════════════════════════════════════════════════════════

def detect_short_squeeze(fund, tech):
    """
    Condiciones para short squeeze:
    - Short interest > 15% del float
    - Precio subiendo con volumen alto (>2x)
    - OBV en acumulación
    Devuelve (True, descripción) o (False, None).
    """
    short_int  = fund.get("short_interest", 0) or 0
    vol_ratio  = tech.get("vol_ratio", 1)
    change_pct = tech.get("change_pct", 0)
    obv_trend  = tech.get("obv_trend", "")

    if short_int >= 15 and change_pct > 3 and vol_ratio > 2:
        confidence = "ALTO" if short_int > 25 and vol_ratio > 3 else "MODERADO"
        return True, f"SHORT SQUEEZE {confidence}: {short_int}% short float + {change_pct:+.1f}% + vol {vol_ratio}x"

    if short_int >= 20 and change_pct > 1 and obv_trend == "ACUMULACION":
        return True, f"SQUEEZE INICIÁNDOSE: {short_int}% short float con acumulación OBV"

    return False, None


# ═══════════════════════════════════════════════════════════════════════
# INSIDERS MASIVOS DETECTOR
# ═══════════════════════════════════════════════════════════════════════

def detect_massive_insider(fund):
    """
    3+ compras de insiders en 30 días = señal fuerte.
    Devuelve (True, descripción) o (False, None).
    """
    buys  = fund.get("insider_buys", 0)
    sells = fund.get("insider_sells", 0)

    if buys >= 3 and buys > sells * 2:
        return True, f"INSIDERS MASIVOS: {buys} compras vs {sells} ventas (30 días)"
    if buys >= 5:
        return True, f"INSIDERS ACUMULANDO: {buys} compras internas (30 días)"

    return False, None


# ═══════════════════════════════════════════════════════════════════════
# PRE-EARNINGS SIGNAL
# Solo COMPRAR, mínimo 3/4 beats, máximo 1 al día
# ═══════════════════════════════════════════════════════════════════════

def check_pre_earnings_signal(fund, tech):
    """
    Condiciones para señal PRE-EARNINGS:
    - Earnings en 1-7 días (ventana óptima)
    - 3/4 o 4/4 últimos beats
    - Técnico alcista (estructura o confluencia alcista)
    - No bajista en ningún timeframe importante
    Devuelve (True, días_para_earnings, descripción) o (False, None, None).
    """
    days    = fund.get("earnings_days")
    beats   = fund.get("earnings_beats", 0)
    upside  = fund.get("analyst_upside", 0) or 0

    if days is None or days < 1 or days > 10:   # ampliado de 7 a 10 días
        return False, None, None

    if beats < 2:   # bajado de 3 a 2 (más permisivo, la IA valida la calidad)
        return False, None, None

    # Técnico debe ser alcista
    structure   = tech.get("structure", "")
    tf_conf     = tech.get("tf_confluence", "")
    change_pct  = tech.get("change_pct", 0)

    is_bullish = (
        "ALCISTA" in structure or
        "ALCISTA" in tf_conf or
        (tech.get("rsi", 50) > 50 and change_pct > 0)
    )

    if not is_bullish:
        return False, None, None

    beat_pct = beats * 25  # 4/4 = 100%, 3/4 = 75%
    desc = (
        f"PRE-EARNINGS en {days}d: {beats}/4 beats ({beat_pct}%) | "
        f"Analistas: {upside:+.1f}% upside | Técnico alcista"
    )
    return True, days, desc


# ═══════════════════════════════════════════════════════════════════════
# MEMORIA DE CATALIZADORES POR SECTOR
# Aprende qué tipo de catalizador funciona mejor en cada sector
# ═══════════════════════════════════════════════════════════════════════

def extract_catalyst_keyword(ai_response):
    """Extrae la palabra clave del catalizador de la respuesta de la IA."""
    for line in ai_response.splitlines():
        if "CATALIZADOR:" in line or "⚡" in line:
            text = line.split(":", 1)[-1].strip().lower()
            # Extraer primera palabra significativa
            words = [w for w in text.split() if len(w) > 4]
            return words[0] if words else None
    return None


def update_catalyst_memory(ticker, sector, ai_response, result):
    """
    Actualiza la memoria de catalizadores cuando se resuelve una predicción.
    result: "win" o "loss"
    """
    keyword = extract_catalyst_keyword(ai_response)
    if not keyword or not sector:
        return

    if sector not in catalyst_memory:
        catalyst_memory[sector] = {}

    if keyword not in catalyst_memory[sector]:
        catalyst_memory[sector][keyword] = {"wins": 0, "total": 0}

    catalyst_memory[sector][keyword]["total"] += 1
    if result == "win":
        catalyst_memory[sector][keyword]["wins"] += 1

    save_state()


def get_catalyst_context(sector):
    """
    Devuelve contexto de catalizadores para el prompt.
    Muestra qué catalizadores han funcionado/fallado en este sector.
    """
    if not sector or sector not in catalyst_memory:
        return ""

    lines = []
    for keyword, data in catalyst_memory[sector].items():
        total = data["total"]
        if total < 3:
            continue
        win_rate = round(data["wins"] / total * 100, 0)
        if win_rate >= 70:
            lines.append(f"✅ '{keyword}' funcionó {int(win_rate)}% en {sector} ({total} casos)")
        elif win_rate <= 35:
            lines.append(f"⚠️ '{keyword}' falló {int(100-win_rate)}% en {sector} ({total} casos)")

    return "\n".join(lines[:3]) if lines else ""


# ═══════════════════════════════════════════════════════════════════════
# CAPA 1 — QUICK SCAN + CORRELACIONES + MANIPULACIÓN
# ═══════════════════════════════════════════════════════════════════════

def detect_manipulation(sym, change, vol_ratio, price):
    if price < 2 and abs(change) > 20:
        return True, f"penny stock pump (${price}, {change:+.1f}%)"
    if change > 20 and vol_ratio < 1.5:
        return True, f"subida extrema ({change:+.1f}%) sin volumen institucional"
    if abs(change) > 35 and vol_ratio < 2:
        return True, f"movimiento extremo ({change:+.1f}%) sin volumen"
    return False, None


def quick_scan():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://finance.yahoo.com",
    }
    seen, candidates, corr_triggers = set(), [], []

    for url in [
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=50",
    ]:
        try:
            _scr_wait = 5
            for _scr_attempt in range(3):
                r = requests.get(url, headers=headers, timeout=15)
                if r.status_code == 429:
                    print(f"[WARN] screener 429, reintento en {_scr_wait}s")
                    time.sleep(_scr_wait)
                    _scr_wait *= 2
                    continue
                break
            else:
                continue
            if r.status_code != 200:
                continue

            quotes = r.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
            for q in quotes:
                sym       = q.get("symbol", "")
                price     = q.get("regularMarketPrice", 0)
                vol       = q.get("regularMarketVolume", 0)
                avg_vol   = max(q.get("averageDailyVolume3Month", 1), 1)
                change    = q.get("regularMarketChangePercent", 0)
                vol_ratio = vol / avg_vol

                if not sym or "." in sym or len(sym) > 5: continue
                if price < 1 or vol < 200_000:            continue
                if sym in seen:                           continue
                if already_alerted_today(sym):            continue

                is_manip, manip_reason = detect_manipulation(sym, change, vol_ratio, price)
                if is_manip:
                    print(f"  Filtrado manipulación: {sym} — {manip_reason}")
                    continue

                score = 0
                if abs(change) > 8:       score += 3
                elif abs(change) > 5:     score += 2
                elif abs(change) > 3:     score += 1
                if vol_ratio > 3:         score += 3
                elif vol_ratio > 2:       score += 2
                elif vol_ratio > 1.5:     score += 1

                if score >= 2:
                    seen.add(sym)
                    candidates.append({
                        "ticker": sym, "name": q.get("longName", sym),
                        "sector": q.get("sector", "Unknown"),
                        "price": price, "change": change,
                        "vol_ratio": round(vol_ratio, 2), "score": score,
                        "source": "screener",
                    })
                    if sym in CORRELATIONS:
                        for ct in CORRELATIONS[sym]:
                            if ct not in seen and not already_alerted_today(ct):
                                corr_triggers.append({
                                    "ticker": ct, "name": ct, "sector": "Unknown",
                                    "price": 0, "change": 0, "vol_ratio": 0,
                                    "score": 2, "source": f"correlación con {sym}",
                                })
                                seen.add(ct)
            time.sleep(0.5)
        except Exception as e:
            print(f"  Quick scan error: {e}")

    developing = [
        {"ticker": t, "name": t, "sector": "Unknown", "price": 0,
         "change": 0, "vol_ratio": 0, "score": 1, "source": "developing"}
        for t, s in watch_signals.items()
        if s.get("developing") and not already_alerted_today(t) and t not in seen
    ]

    corr_unique = []
    existing = {x["ticker"] for x in candidates + developing}
    for c in corr_triggers:
        if c["ticker"] not in existing and c["ticker"] not in {x["ticker"] for x in corr_unique}:
            corr_unique.append(c)

    all_candidates = (
        sorted(candidates, key=lambda x: x["score"], reverse=True)
        + corr_unique[:4] + developing
    )

    print(f"  Quick scan: {len(candidates)} urgentes + {len(corr_unique)} correlaciones + {len(developing)} en desarrollo")
    return all_candidates[:15]

# ═══════════════════════════════════════════════════════════════════════
# CAPA 2 — DATOS DE MERCADO con divergencias y premarket
# ═══════════════════════════════════════════════════════════════════════

def _fetch_twelve_data_candles(ticker, _retry=True):
    """Descarga hasta 500 días de OHLCV diario desde Twelve Data (funciona en IPs cloud)."""
    import pandas as pd
    global _td_credits_reset_at
    if not TWELVE_DATA_KEY:
        return None
    # Créditos agotados: no llamar hasta medianoche UTC
    now = time.time()
    if _td_credits_reset_at and now < _td_credits_reset_at:
        remaining = int(_td_credits_reset_at - now)
        print(f"    [{ticker}] Twelve Data sin créditos — reset en {remaining//3600}h {(remaining%3600)//60}m. Saltando.")
        return None
    _td_rate_limit()
    try:
        url = "https://api.twelvedata.com/time_series"
        params = {
            "symbol": ticker,
            "interval": "1day",
            "outputsize": 500,
            "apikey": TWELVE_DATA_KEY,
            "format": "JSON",
        }
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            print(f"    [{ticker}] Twelve Data HTTP {r.status_code}")
            return None
        data = r.json()
        if data.get("status") == "error":
            msg = data.get("message", "")
            # Créditos diarios agotados: bloquear hasta medianoche UTC y no reintentar
            if "run out of API credits" in msg:
                import datetime as _dt
                tomorrow = _dt.datetime.now(_dt.timezone.utc).replace(
                    hour=0, minute=2, second=0, microsecond=0
                ) + _dt.timedelta(days=1)
                _td_credits_reset_at = tomorrow.timestamp()
                print(f"    [{ticker}] Twelve Data: créditos agotados. Bloqueado hasta {tomorrow.strftime('%Y-%m-%d %H:%M UTC')}.")
            # Símbolo inválido: blacklist en sesión para no reintentar
            if "symbol" in msg.lower() and ("missing" in msg.lower() or "invalid" in msg.lower()):
                _td_invalid_symbols.add(ticker)
                _hist_cache[ticker] = None  # evita segunda llamada en get_market_data
            print(f"    [{ticker}] Twelve Data error: {msg}")
            return None
        values = data.get("values", [])
        if len(values) < 50:
            return None
        df = pd.DataFrame(values)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()
        df.index = df.index.tz_localize("UTC")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                                  "close": "Close", "volume": "Volume"})
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
    except Exception as e:
        print(f"    [{ticker}] Twelve Data excepción: {e}")
        return None


def _fetch_yahoo_candles(ticker):
    """Fallback: descarga ~500 días de OHLCV diario desde Yahoo Finance (API pública)."""
    import pandas as pd
    _wait = 5
    for _attempt in range(3):
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "2y"},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if r.status_code == 429:
                time.sleep(_wait)
                _wait *= 2
                continue
            if r.status_code != 200:
                return None
            chart = r.json().get("chart", {})
            result = (chart.get("result") or [None])[0]
            if not result:
                return None
            timestamps = result.get("timestamp", [])
            q = result.get("indicators", {}).get("quote", [{}])[0]
            adj = result.get("indicators", {}).get("adjclose", [{}])
            closes = (adj[0].get("adjclose") if adj else None) or q.get("close")
            if not timestamps or not closes or len(timestamps) < 50:
                return None
            df = pd.DataFrame({
                "Open":   q.get("open"),
                "High":   q.get("high"),
                "Low":    q.get("low"),
                "Close":  closes,
                "Volume": q.get("volume"),
            }, index=pd.to_datetime(timestamps, unit="s", utc=True))
            df = df.apply(pd.to_numeric, errors="coerce").dropna(how="all").sort_index()
            return df if len(df) >= 50 else None
        except Exception as e:
            print(f"    [{ticker}] Yahoo candles excepción: {e}")
            return None
    return None


def prefetch_tickers(tickers):
    """Descarga historial de todos los tickers vía Twelve Data con fallback a Yahoo Finance."""
    global _hist_cache
    _hist_cache = {}
    if not tickers:
        return
    # Filtrar símbolos ya conocidos como inválidos
    valid = [t for t in tickers if t not in _td_invalid_symbols]
    skipped = len(tickers) - len(valid)
    td_available = bool(TWELVE_DATA_KEY) and not (_td_credits_reset_at and time.time() < _td_credits_reset_at)
    source_label = "Twelve Data" if td_available else "Yahoo Finance (TD sin créditos)"
    if skipped:
        print(f"  Descargando historial de {len(valid)} tickers vía {source_label} ({skipped} inválidos omitidos)...")
    else:
        print(f"  Descargando historial de {len(valid)} tickers vía {source_label}...")
    ok_td = ok_yf = 0
    for t in valid:
        try:
            hist = None
            if td_available:
                hist = _fetch_twelve_data_candles(t)
                if hist is not None and len(hist) >= 50:
                    ok_td += 1
            if hist is None or len(hist) < 50:
                hist = _fetch_yahoo_candles(t)
                if hist is not None and len(hist) >= 50:
                    ok_yf += 1
                    print(f"    [{t}] Yahoo fallback OK")
            if hist is not None and len(hist) >= 50:
                _hist_cache[t] = hist
        except Exception as e:
            print(f"    {t}: error prefetch — {e}")
    print(f"  Datos OK: {ok_td} vía Twelve Data + {ok_yf} vía Yahoo = {ok_td+ok_yf}/{len(valid)} total")


def get_market_data(ticker):
    try:
        if ticker in _hist_cache:
            hist = _hist_cache[ticker]
        else:
            hist = _fetch_twelve_data_candles(ticker)
            if hist is None or len(hist) < 50:
                hist = _fetch_yahoo_candles(ticker)
        if hist is None or hist.empty or len(hist) < 50:
            print(f"    {ticker}: sin datos de mercado")
            return None

        closes  = hist["Close"].dropna().tolist()
        volumes = hist["Volume"].dropna().tolist()
        highs   = hist["High"].dropna().tolist()
        lows    = hist["Low"].dropna().tolist()

        if len(closes) < 50:
            return None

        # Semanal y mensual sin requests adicionales (resample en pandas)
        closes_w = hist["Close"].resample("W").last().dropna().tolist()
        closes_m = hist["Close"].resample("ME").last().dropna().tolist()

        price      = closes[-1]
        change_pct = ((price - closes[-2]) / closes[-2]) * 100 if closes[-2] else 0.0
        sma20      = sum(closes[-20:]) / 20
        sma50      = sum(closes[-50:]) / 50
        sma200     = sum(closes[-200:]) / 200 if len(closes) >= 200 else None

        # MACD: EMA12 y EMA26 inicializadas con SMA, señal = EMA9 del MACD
        k12, k26, k9 = 2/13, 2/27, 2/10
        if len(closes) >= 26:
            ema12 = sum(closes[:12]) / 12
            for c in closes[12:26]:
                ema12 = c * k12 + ema12 * (1 - k12)
            ema26 = sum(closes[:26]) / 26
            macd_hist = []
            for c in closes[26:]:
                ema12 = c * k12 + ema12 * (1 - k12)
                ema26 = c * k26 + ema26 * (1 - k26)
                macd_hist.append(ema12 - ema26)
            macd = macd_hist[-1] if macd_hist else 0.0
            if len(macd_hist) >= 9:
                macd_signal = sum(macd_hist[:9]) / 9
                for mv in macd_hist[9:]:
                    macd_signal = mv * k9 + macd_signal * (1 - k9)
            else:
                macd_signal = 0.0
        else:
            macd = 0.0
            macd_signal = 0.0
        macd_bullish = macd > macd_signal

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

        low14   = min(lows[-14:])  if len(lows)  >= 14 else min(lows)
        high14  = max(highs[-14:]) if len(highs) >= 14 else max(highs)
        stoch_k = ((price - low14) / (high14 - low14) * 100) if high14 != low14 else 50

        avg_vol20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 1
        vol_ratio = volumes[-1] / avg_vol20  if avg_vol20 > 0    else 1
        obv = sum(
            volumes[-i] if closes[-i] > closes[-i-1] else -volumes[-i]
            for i in range(1, min(20, len(closes)))
        )
        obv_trend = "ACUMULACION" if obv > 0 else "DISTRIBUCION"
        vwap      = sum(closes[-5:]) / 5

        atr_vals = [
            max(highs[-i] - lows[-i], abs(highs[-i] - closes[-i-1]), abs(lows[-i] - closes[-i-1]))
            for i in range(1, min(15, len(closes)))
        ]
        atr = sum(atr_vals) / len(atr_vals) if atr_vals else price * 0.02

        h52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        l52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
        rng = h52 - l52
        fib236 = round(h52 - rng * 0.236, 2)
        fib382 = round(h52 - rng * 0.382, 2)
        fib500 = round(h52 - rng * 0.500, 2)
        fib618 = round(h52 - rng * 0.618, 2)

        rh = max(highs[-20:]) if len(highs) >= 20 else price
        rl = min(lows[-20:])  if len(lows)  >= 20 else price
        support_touches = sum(1 for l in lows[-60:] if abs(l - rl) / rl < 0.02) if len(lows) >= 60 else 0

        mom1m = ((price - closes[-22]) / closes[-22] * 100) if len(closes) >= 22 else 0
        mom3m = ((price - closes[-66]) / closes[-66] * 100) if len(closes) >= 66 else 0

        rh10 = highs[-10:] if len(highs) >= 10 else highs
        rl10 = lows[-10:]  if len(lows)  >= 10 else lows
        hh = all(rh10[i] >= rh10[i-1] for i in range(1, len(rh10)))
        hl = all(rl10[i] >= rl10[i-1] for i in range(1, len(rl10)))
        lh = all(rh10[i] <= rh10[i-1] for i in range(1, len(rh10)))
        ll = all(rl10[i] <= rl10[i-1] for i in range(1, len(rl10)))
        if hh and hl:   structure = "TENDENCIA ALCISTA CLARA"
        elif lh and ll: structure = "TENDENCIA BAJISTA CLARA"
        else:           structure = "LATERAL / CONSOLIDACION"

        tech_score = 0
        if rsi_zone == "oversold_extreme":     tech_score += 4
        elif rsi_zone == "oversold":           tech_score += 2
        elif rsi_zone == "overbought_extreme": tech_score += 4
        elif rsi_zone == "overbought":         tech_score += 2
        if vol_ratio > 3:      tech_score += 3
        elif vol_ratio > 2:    tech_score += 2
        elif vol_ratio > 1.5:  tech_score += 1
        if abs(change_pct) > 8:   tech_score += 3
        elif abs(change_pct) > 5: tech_score += 2
        elif abs(change_pct) > 3: tech_score += 1
        if macd_bullish and change_pct > 0:               tech_score += 2
        if stoch_k < 20 or stoch_k > 80:                  tech_score += 1
        if obv_trend == "ACUMULACION" and change_pct > 0:  tech_score += 2
        if abs(mom1m) > 15:  tech_score += 2
        elif abs(mom1m) > 8: tech_score += 1
        if support_touches >= 3: tech_score += 2

        # ── Semanal y mensual (ya calculados arriba via resample) ────────
        weekly_trend  = "N/D"
        monthly_trend = "N/D"
        if len(closes_w) >= 10:
            weekly_trend = "ALCISTA" if closes_w[-1] > sum(closes_w[-10:]) / 10 else "BAJISTA"
        if len(closes_m) >= 6:
            monthly_trend = "ALCISTA" if closes_m[-1] > sum(closes_m[-6:]) / 6 else "BAJISTA"

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

        # ── Divergencias (solo si 2 timeframes) ──────────────────────
        # Calcular RSI semanal para la detección de divergencias
        rsi_w = None
        if len(closes_w) >= 15:
            gains_w, losses_w_rsi = [], []
            for i in range(1, 15):
                d_w = closes_w[-i] - closes_w[-i-1]
                (gains_w if d_w >= 0 else losses_w_rsi).append(abs(d_w))
            avg_gain_w = sum(gains_w) / 14
            avg_loss_w = sum(losses_w_rsi) / 14 if losses_w_rsi else 0.001
            rsi_w = 100 - (100 / (1 + avg_gain_w / avg_loss_w))
        div_type, div_desc = detect_divergence(closes, rsi, closes_w, rsi_w)

        # ── Confluencia dinámica ponderada ────────────────────────────
        tf_tech = {"daily_trend": daily_trend, "weekly_trend": weekly_trend,
                   "monthly_trend": monthly_trend}
        weighted_score, weighted_desc = calc_weighted_tf_confluence(tf_tech)

        return {
            "ticker": ticker, "name": ticker,
            "sector": "Unknown",
            "price": round(price, 2), "change_pct": round(change_pct, 2),
            "sma20": round(sma20, 2), "sma50": round(sma50, 2),
            "sma200": round(sma200, 2) if sma200 else None,
            "vwap": round(vwap, 2),
            "rsi": round(rsi, 1), "rsi_zone": rsi_zone,
            "macd": round(macd, 3), "macd_signal": round(macd_signal, 3),
            "macd_bullish": macd_bullish,
            "stoch_k": round(stoch_k, 1),
            "vol_ratio": round(vol_ratio, 2), "obv_trend": obv_trend,
            "atr": round(atr, 2),
            "h52": round(h52, 2), "l52": round(l52, 2),
            "dist_h": round(((price - h52) / h52) * 100, 1),
            "dist_l": round(((price - l52) / l52) * 100, 1),
            "fib236": fib236, "fib382": fib382, "fib500": fib500, "fib618": fib618,
            "rh": round(rh, 2), "rl": round(rl, 2),
            "support_touches": support_touches,
            "mom1m": round(mom1m, 1), "mom3m": round(mom3m, 1),
            "structure": structure,
            "daily_trend": daily_trend, "weekly_trend": weekly_trend,
            "monthly_trend": monthly_trend, "tf_confluence": tf_conf,
            "tech_score": max(tech_score, 0),
            # Nuevos en v5.2
            "divergence_type": div_type,
            "divergence_desc": div_desc,
            "weighted_tf_score": weighted_score,
            "weighted_tf_desc":  weighted_desc,
        }

    except Exception as e:
        print(f"    {ticker}: error en datos de mercado — {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════
# CAPA 3 — FUNDAMENTALES
# ═══════════════════════════════════════════════════════════════════════

def get_fundamentals(ticker):
    result = {
        "pe_ratio": None, "short_interest": None,
        "revenue_growth": None, "profit_margins": None,
        "rec_key": "hold", "analyst_target": None, "analyst_upside": None,
        "earnings_days": None, "earnings_beats": 0,
        "insider_buys": 0, "insider_sells": 0,
        "sector": None,
    }
    modules = "defaultKeyStatistics,financialData,calendarEvents,earningsHistory,insiderTransactions,assetProfile"
    try:
        _fund_wait = 5
        for _fund_attempt in range(3):
            r = requests.get(
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules={modules}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=12,
            )
            if r.status_code == 429:
                print(f"[WARN] get_fundamentals 429 {ticker}, reintento en {_fund_wait}s")
                time.sleep(_fund_wait)
                _fund_wait *= 2
                continue
            break
        else:
            return result
        if r.status_code != 200:
            return result

        data    = r.json().get("quoteSummary", {}).get("result", [{}])[0]
        stats   = data.get("defaultKeyStatistics", {})
        fin     = data.get("financialData", {})
        profile = data.get("assetProfile", {})
        if profile.get("sector"):
            result["sector"] = profile["sector"]

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
            days = (datetime.fromtimestamp(dates[0]["raw"], tz=SPAIN_TZ) - datetime.now(SPAIN_TZ)).days
            if 0 <= days <= 21:
                result["earnings_days"] = days

        history = data.get("earningsHistory", {}).get("history", [])
        result["earnings_beats"] = sum(
            1 for h in history[-4:] if (_raw(h, "surprisePercent", 0) or 0) > 0
        )

        for t in data.get("insiderTransactions", {}).get("transactions", [])[:10]:
            days_ago = (datetime.now(SPAIN_TZ) - datetime.fromtimestamp(_raw(t, "startDate", 0) or 0, tz=SPAIN_TZ)).days
            if days_ago <= 30:
                txt = t.get("transactionText", "")
                if "Purchase" in txt: result["insider_buys"]  += 1
                elif "Sale"  in txt:  result["insider_sells"] += 1

    except Exception as e:
        print(f"    {ticker}: fundamentales error — {e}")

    return result

# ═══════════════════════════════════════════════════════════════════════
# CAPA 4 — SENTIMIENTO enriquecido: NewsAPI + Stocktwits + Investing.com
# ═══════════════════════════════════════════════════════════════════════

def _score_headline(title, positive_words, negative_words):
    """Puntúa un titular teniendo en cuenta negaciones de contexto."""
    tl = title.lower()
    negation_ctx = ["not", "no ", "never", "downgrade", "cut", "miss", "disappoints",
                    "lower", "concern", "risk", "warn", "below", "slump", "despite"]
    score = 0
    for w in positive_words:
        if w in tl:
            # Si hay palabras de negación en los 40 caracteres previos al keyword, no contar como positivo
            idx = tl.find(w)
            context_window = tl[max(0, idx - 40):idx]
            if any(n in context_window for n in negation_ctx):
                score -= 1  # falso positivo → penalizar levemente
            else:
                score += 1
    for w in negative_words:
        if w in tl:
            score -= 1
    return score


def get_sentiment(ticker, sector):
    news_items      = []
    sentiment_score = 0
    positive_words  = ["beat","surge","jump","upgrade","buy","strong","growth","record","partnership","contract","raised","guidance"]
    negative_words  = ["miss","fall","drop","downgrade","sell","weak","loss","cut","investigation","lawsuit","recall","fraud"]

    # NewsAPI
    if NEWS_API_KEY:
        try:
            r = requests.get(
                f"https://newsapi.org/v2/everything?q={ticker}&language=en&sortBy=publishedAt&pageSize=6&apiKey={NEWS_API_KEY}",
                timeout=10,
            )
            if r.status_code == 200:
                for a in r.json().get("articles", [])[:6]:
                    title = a.get("title", "")
                    news_items.append(f"[NEWS] {title}")
                    sentiment_score += _score_headline(title, positive_words, negative_words)
        except Exception as e:
            print(f"[ERROR] ticker_news NewsAPI {ticker}: {e}")

    # RSS Yahoo Finance
    try:
        feed = feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}&region=US&lang=en-US")
        for e in feed.entries[:3]:
            news_items.append(f"[YAHOO] {e.title}")
    except Exception as e:
        print(f"[ERROR] ticker_news Yahoo RSS {ticker}: {e}")

    # Investing.com
    investing_news = get_investing_news(ticker)
    for n in investing_news:
        news_items.append(f"[INVESTING] {n}")
        sentiment_score += _score_headline(n, positive_words, negative_words) * 2  # peso doble por ser fuente seria

    # Stocktwits
    st_data = get_stocktwits_sentiment(ticker)

    # ETF sectorial
    sector_perf = None
    etf = SECTOR_ETFS.get(sector)
    if etf:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if r.status_code == 200:
                _etf_result = r.json().get("chart", {}).get("result") or []
                if _etf_result:
                    closes = [c for c in _etf_result[0]["indicators"]["quote"][0].get("close", []) if c]
                    if len(closes) >= 2:
                        sector_perf = round(((closes[-1] - closes[-2]) / closes[-2]) * 100, 2)
        except Exception as e:
            print(f"[ERROR] sector_perf {etf}: {e}")

    # Bias sectorial por geopolítica
    sector_geo_bias = market_context.get("sector_bias", {}).get(etf, "neutral") if etf else "neutral"

    return {
        "news":            news_items[:10],
        "sentiment_score": sentiment_score,
        "sentiment_label": "POSITIVO" if sentiment_score > 2 else "NEGATIVO" if sentiment_score < -2 else "NEUTRAL",
        "sector_perf":     sector_perf,
        "stocktwits":      st_data,
        "sector_geo_bias": sector_geo_bias,
    }

# ═══════════════════════════════════════════════════════════════════════
# CAPA 5 — SEÑAL INSTITUCIONAL
# ═══════════════════════════════════════════════════════════════════════

def get_inst_signal(tech):
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

    if obv_trend == "ACUMULACION"   and change_pct > 0: boost += 2
    elif obv_trend == "DISTRIBUCION" and change_pct < 0: boost += 2

    return signal, boost

# ═══════════════════════════════════════════════════════════════════════
# MOTOR DE APRENDIZAJE — igual que v5, sin cambios
# ═══════════════════════════════════════════════════════════════════════

def _resolved_predictions():
    return [p for p in predictions if p.get("result") in ("win", "loss")]


def _update_learning_level1():
    resolved = _resolved_predictions()
    if len(resolved) < LEARN_MIN_PREDS:
        return
    rules = []
    conditions = [
        ("rsi_oversold",          lambda p: (p.get("rsi") or 50) < 32),
        ("rsi_oversold_extreme",  lambda p: (p.get("rsi") or 50) < 25),
        ("rsi_overbought",        lambda p: (p.get("rsi") or 50) > 68),
        ("vol_high",              lambda p: (p.get("vol_ratio") or 1) > 2),
        ("vol_very_high",         lambda p: (p.get("vol_ratio") or 1) > 3),
        ("macd_bullish",          lambda p: p.get("macd_bullish") is True),
        ("obv_acum",              lambda p: p.get("obv_trend") == "ACUMULACION"),
        ("tf_full_bull",          lambda p: p.get("tf_confluence") == "CONFLUENCIA ALCISTA TOTAL"),
        ("tf_full_bear",          lambda p: p.get("tf_confluence") == "CONFLUENCIA BAJISTA TOTAL"),
        ("structure_bull",        lambda p: p.get("structure") == "TENDENCIA ALCISTA CLARA"),
        ("high_score",            lambda p: (p.get("tech_score") or 0) >= 10),
        ("support_strong",        lambda p: (p.get("support_touches") or 0) >= 3),
        ("mom1m_strong",          lambda p: abs(p.get("mom1m") or 0) > 10),
        ("high_impact_day",       lambda p: p.get("is_high_impact_day") is True),
        ("pre_earnings",          lambda p: p.get("signal_type") == "PRE_EARNINGS"),
        ("short_squeeze",         lambda p: p.get("signal_type") == "SHORT_SQUEEZE"),
    ]
    for name, cond_fn in conditions:
        matching = [p for p in resolved if cond_fn(p)]
        if len(matching) < 5:
            continue
        wins     = sum(1 for p in matching if p["result"] == "win")
        win_rate = round(wins / len(matching) * 100, 1)
        rules.append({
            "condition": name, "win_rate": win_rate,
            "sample_size": len(matching),
            "description": f"{name}: {win_rate}% acierto en {len(matching)} casos",
        })
    learnings["rules"] = sorted(rules, key=lambda x: abs(x["win_rate"] - 50), reverse=True)


def _update_learning_level2():
    resolved = _resolved_predictions()
    if len(resolved) < LEARN_MIN_PREDS:
        return
    regime_memory = {}
    for regime in ["BULL", "BEAR", "LATERAL"]:
        subset = [p for p in resolved if p.get("regime") == regime]
        if len(subset) < 3:
            continue
        wins     = sum(1 for p in subset if p["result"] == "win")
        win_rate = round(wins / len(subset) * 100, 1)
        regime_memory[regime] = {"win_rate": win_rate, "total": len(subset)}
    learnings["regime_memory"] = regime_memory


def _update_learning_level3():
    resolved = _resolved_predictions()
    if len(resolved) < LEARN_MIN_L3:
        return
    sector_memory = {}
    for sector in list(set(p.get("sector", "Unknown") for p in resolved)):
        subset = [p for p in resolved if p.get("sector") == sector]
        if len(subset) < 4:
            continue
        wins     = sum(1 for p in subset if p["result"] == "win")
        win_rate = round(wins / len(subset) * 100, 1)
        avg_conf = round(sum(p.get("confidence", 85) for p in subset) / len(subset), 1)
        sector_memory[sector] = {"win_rate": win_rate, "total": len(subset), "avg_conf": avg_conf}
    learnings["sector_memory"] = sector_memory


def _update_learning_level4():
    resolved = _resolved_predictions()
    if len(resolved) < LEARN_MIN_L4:
        return
    hour_memory = {}
    for hour in range(9, 23):
        subset = [p for p in resolved if p.get("hour") == hour]
        if len(subset) < 3:
            continue
        wins     = sum(1 for p in subset if p["result"] == "win")
        win_rate = round(wins / len(subset) * 100, 1)
        hour_memory[str(hour)] = {"win_rate": win_rate, "total": len(subset)}
    learnings["hour_memory"] = hour_memory


def _update_learning_level5():
    resolved = _resolved_predictions()
    if len(resolved) < LEARN_MIN_L5:
        return
    recent_losses = [p for p in resolved if p["result"] == "loss"][-10:]
    if len(recent_losses) < 5:
        return
    losses_txt = "\n".join([
        f"- {p['ticker']} ({p.get('signal','?')}) | RSI:{p.get('rsi','?')} "
        f"Vol:{p.get('vol_ratio','?')}x | Conf:{p.get('confidence','?')}% "
        f"| Régimen:{p.get('regime','?')} | Sector:{p.get('sector','?')} "
        f"| Tipo:{p.get('signal_type','NORMAL')}"
        for p in recent_losses
    ])
    prompt = f"""Analiza los últimos {len(recent_losses)} fallos de un bot de trading.
{losses_txt}
Responde SOLO con JSON (sin markdown):
[{{"indicator": "nombre", "description": "problema en 10 palabras", "count": N}}]
Máximo 5 entradas. Solo patrones en 3+ fallos."""
    try:
        result = call_ai(prompt, max_tokens=300)
        if result:
            clean  = result.strip().replace("```json", "").replace("```", "").strip()
            errors = json.loads(clean)
            if isinstance(errors, list):
                learnings["error_memory"] = errors[:5]
    except Exception as e:
        print(f"  Nivel 5 error: {e}")


def run_learning_engine():
    resolved = _resolved_predictions()
    print(f"  Motor de aprendizaje: {len(resolved)} predicciones resueltas")
    if len(resolved) < LEARN_MIN_PREDS:
        print(f"  Inactivo — necesita {LEARN_MIN_PREDS} predicciones")
        return
    _update_learning_level1()
    _update_learning_level2()
    if len(resolved) >= LEARN_MIN_L3: _update_learning_level3()
    if len(resolved) >= LEARN_MIN_L4: _update_learning_level4()
    if len(resolved) >= LEARN_MIN_L5: _update_learning_level5()
    learnings["last_updated"] = datetime.now(SPAIN_TZ).isoformat()
    save_state()
    rules_cnt  = len(learnings.get("rules", []))
    sector_cnt = len(learnings.get("sector_memory", {}))
    error_cnt  = len(learnings.get("error_memory", []))
    print(f"  Aprendizaje: {rules_cnt} reglas | {sector_cnt} sectores | {error_cnt} errores")
    send_log(f"🧠 Aprendizaje: {rules_cnt} reglas | {sector_cnt} sectores | {error_cnt} patrones error")


def update_prediction_results():
    pending = [p for p in predictions if p.get("result") == "pending"]
    if not pending:
        return
    for p in pending:
        ticker = p["ticker"]
        entry  = p.get("entry", 0)
        target = p.get("target", entry * 1.15)
        stop   = p.get("stop",   entry * 0.93)
        signal = p.get("signal", "COMPRAR")
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8,
            )
            if r.status_code == 200:
                _pred_result = r.json().get("chart", {}).get("result") or []
                if not _pred_result:
                    continue
                closes = [c for c in _pred_result[0]["indicators"]["quote"][0].get("close", []) if c]
                if not closes:
                    continue
                current    = closes[-1]
                days_since = (datetime.now(SPAIN_TZ) - _to_aware(p["date"])).days
                hit_target = (signal == "COMPRAR" and current >= target) or (signal == "VENDER" and current <= target)
                hit_stop   = (signal == "COMPRAR" and current <= stop)   or (signal == "VENDER" and current >= stop)
                expired    = days_since > 30
                if hit_target:
                    p["result"] = "win";  p["exit_price"] = round(current, 2); p["days_to_result"] = days_since; p["exit_reason"] = "TARGET"
                    chg = round(((current - p["entry"]) / p["entry"]) * 100, 1) if p.get("signal") == "COMPRAR" else round(((p["entry"] - current) / p["entry"]) * 100, 1)
                    _cb_register_result("win")
                    send_acierto(
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✅ **{ticker}** — TARGET ALCANZADO\n"
                        f"📈 Entrada: ${p['entry']} → Salida: ${round(current,2)} ({chg:+.1f}%)\n"
                        f"📅 {days_since} día(s) | {p.get('signal_type','NORMAL')}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                elif hit_stop or expired:
                    motivo = "STOP LOSS" if hit_stop else "EXPIRADA (30d)"
                    p["result"] = "loss"; p["exit_price"] = round(current, 2); p["days_to_result"] = days_since; p["exit_reason"] = "STOP" if hit_stop else "EXPIRADA"
                    chg = round(((current - p["entry"]) / p["entry"]) * 100, 1) if p.get("signal") == "COMPRAR" else round(((p["entry"] - current) / p["entry"]) * 100, 1)
                    _cb_register_result("loss")
                    send_acierto(
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"❌ **{ticker}** — {motivo}\n"
                        f"📉 Entrada: ${p['entry']} → Salida: ${round(current,2)} ({chg:+.1f}%)\n"
                        f"📅 {days_since} día(s) | {p.get('signal_type','NORMAL')}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
        except Exception as e:
            print(f"[ERROR] check_predictions {ticker}: {e}")
        time.sleep(0.3)
    save_state()

# ═══════════════════════════════════════════════════════════════════════
# CAPA 6 — IA con prompts adaptativos v5.2
# ═══════════════════════════════════════════════════════════════════════

def call_ai(prompt, max_tokens=700):
    global _ai_client
    try:
        if _ai_client is None:
            _ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg    = _ai_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        track_ai_cost()
        return msg.content[0].text.strip()
    except Exception as e:
        print(f"    IA error: {e}")
        return None


def _build_learning_context():
    resolved_count = len(_resolved_predictions())
    if resolved_count < LEARN_MIN_PREDS:
        return ""
    lines = ["\nCONOCIMIENTO HISTÓRICO APRENDIDO:"]

    top_rules = [r for r in learnings.get("rules", []) if r["sample_size"] >= 5][:5]
    if top_rules:
        lines.append("Patrones históricos:")
        for r in top_rules:
            perf = "✅ FIABLE" if r["win_rate"] >= 65 else "⚠️ POCO FIABLE" if r["win_rate"] <= 40 else "~neutro"
            lines.append(f"  {perf}: {r['description']}")

    regime     = market_regime.get("regime", "UNKNOWN")
    reg_memory = learnings.get("regime_memory", {}).get(regime, {})
    if reg_memory:
        lines.append(f"Régimen {regime}: win_rate histórico {reg_memory['win_rate']}% ({reg_memory['total']} casos)")

    if resolved_count >= LEARN_MIN_L3:
        worst = [s for s, d in learnings.get("sector_memory", {}).items() if d["win_rate"] < 45]
        if worst:
            lines.append(f"Sectores con bajo rendimiento histórico: {', '.join(worst)}")

    if resolved_count >= LEARN_MIN_L4:
        current_hour = str(datetime.now(SPAIN_TZ).hour)
        hour_data    = learnings.get("hour_memory", {}).get(current_hour, {})
        if hour_data:
            lines.append(f"A las {current_hour}h: win_rate {hour_data['win_rate']}% ({hour_data['total']} casos)")

    if resolved_count >= LEARN_MIN_L5:
        for e in learnings.get("error_memory", [])[:3]:
            lines.append(f"  ⚠️ Error conocido — {e['indicator']}: {e['description']}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_auto_prompt(tech, fund, sent, inst_signal, conf_boost, special_signals=None):
    """
    Prompt automático v5.2 con todas las nuevas capas:
    - Premarket / gap
    - Stocktwits
    - Investing.com
    - Put/call ratio
    - Contexto geopolítico
    - Divergencias
    - Confluencia ponderada
    - Señales especiales (squeeze, insiders, pre-earnings)
    - Calendario económico
    - Catalizadores históricos por sector
    """
    if special_signals is None:
        special_signals = {}

    fg      = market_context["fear_greed"]
    sp500   = market_context["sp500_change"]
    vix     = market_context["vix"]
    fg_str  = _fg_label(fg)
    regime  = market_regime.get("regime", "UNKNOWN")
    regime_desc = market_regime.get("description", "")

    # Contexto macro
    macro_txt = "\n".join(f"- {h}" for h in market_context.get("macro_news", [])[:4]) or "- Sin noticias"
    econ_txt  = "\n".join(f"- {h}" for h in market_context.get("economic_events", [])[:3]) or "- Sin eventos"

    # Calendario económico
    eco_warn = ""
    if econ_calendar.get("is_high_impact"):
        events   = ", ".join(econ_calendar["high_impact_today"])
        eco_warn = f"\n⚠️ ALTO IMPACTO HOY: {events} — ser MUY conservador"

    # Geopolítica
    geo_ctx = market_context.get("geopolitical_context", [])
    geo_bias = market_context.get("sector_bias", {})
    geo_txt = ""
    if geo_ctx:
        bias_up   = [k for k, v in geo_bias.items() if v == "up"]
        bias_down = [k for k, v in geo_bias.items() if v == "down"]
        geo_txt = f"\nContexto geopolítico: {', '.join(geo_ctx)}"
        if bias_up:   geo_txt += f" | Sectores favorecidos: {', '.join(bias_up)}"
        if bias_down: geo_txt += f" | Sectores penalizados: {', '.join(bias_down)}"

    # Noticias
    news_txt = "\n".join(f"- {h}" for h in sent["news"][:6]) or "- Sin noticias"

    # Stocktwits
    st = sent.get("stocktwits")
    st_txt = ""
    if st:
        st_txt = f"\nStocktwits ({st['message_count']} msgs): {st['label']} — {st['bullish_pct']}% alcista / {st['bearish_pct']}% bajista"
        if st.get("trending"):
            st_txt += " — TRENDING"

    # Fundamentales
    rec_map  = {"strongBuy":"COMPRA FUERTE","buy":"COMPRAR","hold":"MANTENER","sell":"VENDER","strongSell":"VENTA FUERTE"}
    rec_txt  = rec_map.get(fund.get("rec_key", "hold"), "MANTENER")
    tgt_txt  = (f"Objetivo analistas: ${fund['analyst_target']} ({fund['analyst_upside']:+.1f}%)"
                if fund.get("analyst_target") else "Sin precio objetivo")
    earn_txt = (f"EARNINGS EN {fund['earnings_days']} DÍAS — {fund.get('earnings_beats',0)}/4 beats"
                if fund.get("earnings_days") is not None else "Sin earnings próximos")

    # Premarket
    pm = special_signals.get("premarket")
    pm_txt = ""
    if pm and pm.get("significant"):
        direction = "GAP UP" if pm["gap_up"] else "GAP DOWN"
        pm_txt = f"\n🌅 PREMARKET: {direction} {pm['gap_pct']:+.1f}% | Precio pre: ${pm['pre_price']} | Vol premarket: {pm['pre_vol_ratio']}x"

    # Put/call ratio
    pc_ratio, pc_interp = special_signals.get("put_call", (None, None))
    pc_txt = f"\nPut/Call ratio: {pc_ratio} — {pc_interp}" if pc_ratio else ""

    # Divergencias
    div_txt = ""
    if tech.get("divergence_type"):
        div_txt = f"\n⚡ DIVERGENCIA {tech['divergence_type']}: {tech['divergence_desc']}"

    # Confluencia ponderada
    weighted_desc = tech.get("weighted_tf_desc", tech.get("tf_confluence", ""))

    # Señales especiales
    special_txt = ""
    if special_signals.get("squeeze"):
        special_txt += f"\n🔀 {special_signals['squeeze']}"
    if special_signals.get("insider"):
        special_txt += f"\n👥 {special_signals['insider']}"
    if special_signals.get("pre_earnings"):
        special_txt += f"\n📅 {special_signals['pre_earnings']}"

    # Catalizadores históricos
    catalyst_ctx = get_catalyst_context(tech.get("sector", ""))
    catalyst_txt = f"\nCatalizadores históricos en {tech.get('sector','')}:\n{catalyst_ctx}" if catalyst_ctx else ""

    # Ajuste confianza mínima
    regime_adj  = get_regime_conf_adjustment()
    signal_dir  = "COMPRAR" if tech.get("change_pct", 0) >= 0 else "VENDER"
    conf_minimo = CONF_NORMAL + regime_adj.get(signal_dir, 0)
    if econ_calendar.get("is_high_impact"):
        conf_minimo = max(conf_minimo, CONF_FUERTE)

    # Contexto aprendido
    learning_ctx = _build_learning_context()

    return f"""Eres el mejor analista cuantitativo del mundo. Tu misión es encontrar oportunidades REALES.

REGLA CRÍTICA: Solo señal con CONVERGENCIA en al menos 4 de 5 capas:
  1. Técnico (RSI, MACD, volumen, estructura, divergencias)
  2. Timeframes ponderados según régimen (diario/semanal/mensual)
  3. Fundamental (valoración, analistas, insiders)
  4. Sentimiento (NewsAPI + Stocktwits + Investing.com)
  5. Institucional (volumen, OBV, put/call ratio)
Si no hay convergencia real → NO_SIGNAL.

RÉGIMEN: {regime} — {regime_desc}{eco_warn}{geo_txt}
MACRO: Fear&Greed {fg}/100 ({fg_str}) | S&P500 {sp500:+.2f}% | VIX {vix} {'⚠️ ALTA VOLATILIDAD' if vix > 25 else ''}
{macro_txt}
{econ_txt}

TÉCNICO — {tech['ticker']} ({tech['name']}) | Sector: {tech['sector']} | ETF: {sent.get('sector_perf','N/D')}% | Bias geo: {sent.get('sector_geo_bias','neutral')}
Precio: ${tech['price']} ({tech['change_pct']:+.2f}% hoy){pm_txt}
Confluencia TF ponderada: {weighted_desc}
Estructura: {tech['structure']}
SMA20: ${tech['sma20']} | SMA50: ${tech['sma50']} | SMA200: ${tech.get('sma200','N/D')} | VWAP: ${tech['vwap']} ({'SOBRE' if tech['price'] > tech['vwap'] else 'BAJO'})
RSI(14): {tech['rsi']} {('SOBREVENTA EXTREMA' if tech['rsi']<25 else 'sobreventa' if tech['rsi']<32 else 'SOBRECOMPRA EXTREMA' if tech['rsi']>75 else 'sobrecompra' if tech['rsi']>68 else '')}
MACD: {'ALCISTA' if tech['macd_bullish'] else 'BAJISTA'} | Estocástico K: {tech['stoch_k']}{div_txt}
Volumen: {tech['vol_ratio']}x | OBV: {tech['obv_trend']} | ATR: ${tech['atr']}
Momentum: 1m {tech['mom1m']:+.1f}% | 3m {tech['mom3m']:+.1f}%
Fibonacci: 23.6%=${tech['fib236']} | 38.2%=${tech['fib382']} | 50%=${tech['fib500']} | 61.8%=${tech['fib618']}
Soporte: ${tech['rl']} ({tech['support_touches']} toques) | Resistencia: ${tech['rh']}
52s: mín ${tech['l52']} ({tech['dist_l']:+.1f}%) / máx ${tech['h52']} ({tech['dist_h']:+.1f}%)

FUNDAMENTAL
P/E: {fund.get('pe_ratio','N/D')} | Margen: {fund.get('profit_margins','N/D')}% | Short: {fund.get('short_interest',0)}% {'⚠️ SHORT SQUEEZE POSIBLE' if (fund.get('short_interest') or 0) > 20 else ''}
{earn_txt} | Insiders: {fund.get('insider_buys',0)} compras / {fund.get('insider_sells',0)} ventas (30d)

SENTIMIENTO
{sent['sentiment_label']} (score {sent['sentiment_score']}) | Analistas: {rec_txt} | {tgt_txt}{st_txt}{pc_txt}
{news_txt}

INSTITUCIONAL: {inst_signal} | Boost: +{conf_boost}%{special_txt}{catalyst_txt}
{learning_ctx}

Confianza mínima: {conf_minimo}% (régimen {regime}{', alto impacto' if econ_calendar.get('is_high_impact') else ''})
Responde EXACTAMENTE:

SEÑAL: COMPRAR o VENDER
CONFIANZA: [X]%
🎯 ENTRADA ÓPTIMA: $[precio]
📈 OBJETIVO: [+/-X%] → $[precio] en [plazo] — [razón 5 palabras]
🛑 STOP LOSS: $[precio] — prob. stop: [X]%
⚖️ RATIO R/B: [X]:1
💬 POR QUÉ: [2-3 frases — qué converge, por qué ahora]
⚡ CATALIZADOR: [factor principal]
❌ INVALIDACIÓN: [precio o evento]

Sin convergencia en 4 capas → NO_SIGNAL"""


def _build_manual_prompt(tech, fund, sent):
    fg     = market_context["fear_greed"]
    fg_str = _fg_label(fg)
    regime = market_regime.get("regime", "UNKNOWN")
    st     = sent.get("stocktwits")
    st_txt = f"\nStocktwits: {st['label']} ({st['bullish_pct']}% bull)" if st else ""
    div_txt = f"\nDivergencia: {tech.get('divergence_desc','')}" if tech.get("divergence_type") else ""
    learning_ctx = _build_learning_context()

    return f"""Eres el mejor analista del mundo. Análisis solicitado de {tech['ticker']} ({tech['name']}).
Da SIEMPRE análisis completo. NEUTRAL si no hay señal clara.

Precio: ${tech['price']} ({tech['change_pct']:+.2f}% hoy)
RSI: {tech['rsi']} | MACD: {'ALCISTA' if tech['macd_bullish'] else 'BAJISTA'} | Vol: {tech['vol_ratio']}x
SMA20: ${tech['sma20']} | SMA50: ${tech['sma50']} | VWAP: ${tech['vwap']}
TF ponderada: {tech.get('weighted_tf_desc', tech.get('tf_confluence',''))}
Soporte: ${tech['rl']} ({tech['support_touches']} toques) | Resistencia: ${tech['rh']}
Fib 38.2%: ${tech['fib382']} | 61.8%: ${tech['fib618']}
Momentum: 1m {tech['mom1m']:+.1f}% | 3m {tech['mom3m']:+.1f}%{div_txt}
Fear&Greed: {fg}/100 ({fg_str}) | VIX: {market_context['vix']} | Régimen: {regime}
P/E: {fund.get('pe_ratio','N/D')} | Short: {fund.get('short_interest','N/D')}% | Analistas: {fund.get('rec_key','N/D')}
Sentimiento: {sent['sentiment_label']}{st_txt}
{learning_ctx}

Responde EXACTAMENTE:
SEÑAL: COMPRAR / VENDER / NEUTRAL
CONFIANZA: [X]%
📊 SITUACIÓN: [1 frase]
🎯 ENTRADA: $[precio]
📈 OBJETIVO: [+/-X%] → $[precio] en [plazo] — [razón]
🛑 STOP: $[precio] — prob. stop: [X]%
⚖️ RATIO R/B: [X]:1
💬 POR QUÉ: [2-3 frases]
⚡ CATALIZADOR: [factor]
❌ INVALIDACIÓN: [precio o evento]"""

# ═══════════════════════════════════════════════════════════════════════
# FORMATEO DE ALERTAS
# ═══════════════════════════════════════════════════════════════════════

def format_alert(tech, ai_response, conf_final, session_tag="", signal_type="NORMAL"):
    signal = "COMPRAR"
    for line in ai_response.splitlines():
        if line.startswith("SEÑAL:"):
            signal = "VENDER" if "VENDER" in line else "COMPRAR"
            break

    is_buy = signal == "COMPRAR"

    # Emoji según tipo de señal y confianza
    if signal_type == "PRE_EARNINGS":
        emoji = "📅"
    elif signal_type == "SHORT_SQUEEZE":
        emoji = "🔀"
    elif signal_type == "INSIDER_MASSIVE":
        emoji = "👥"
    elif conf_final >= CONF_EXCEPCIONAL:
        emoji = "⚡" if is_buy else "💀"
    elif conf_final >= CONF_FUERTE:
        emoji = "🔥" if is_buy else "🔴"
    else:
        emoji = "🟢" if is_buy else "🔴"

    body = "\n".join(
        line for line in ai_response.splitlines()
        if not line.startswith("SEÑAL:") and not line.startswith("CONFIANZA:")
    ).strip()

    sign    = "+" if tech["change_pct"] >= 0 else ""
    sess    = f"  [{session_tag}]" if session_tag and session_tag != "MERCADO" else ""
    regime  = market_regime.get("regime", "")
    reg_tag = f"  {regime}" if regime and regime != "UNKNOWN" else ""

    # Tags especiales
    type_tag = ""
    if signal_type == "PRE_EARNINGS":   type_tag = "  PRE-EARNINGS"
    elif signal_type == "SHORT_SQUEEZE": type_tag = "  SHORT SQUEEZE"
    elif signal_type == "INSIDER_MASSIVE": type_tag = "  INSIDERS"

    # Alerta de alto impacto
    eco_tag = "  ⚠️ALTO IMPACTO" if econ_calendar.get("is_high_impact") else ""

    now = datetime.now(SPAIN_TZ).strftime("%H:%M  %d/%m/%Y")

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji}  **{signal}  —  {tech['ticker']}**{sess}{reg_tag}{type_tag}{eco_tag}\n"
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
# ANÁLISIS COMPLETO
# ═══════════════════════════════════════════════════════════════════════


def pre_filter_convergence(tech, fund, sent, inst_signal, boost, special_signals):
    """
    Prefiltro de convergencia SIN llamar a la IA.
    Evalúa las 5 capas con los datos ya descargados.
    Solo devuelve True si hay convergencia en al menos 3 de 5 capas.
    Ahorro estimado: ~85% de llamadas IA → ~$4/mes en vez de $38.
    """
    capas_ok = 0
    motivos  = []

    # Capa 1: Técnico
    rsi       = tech.get("rsi", 50)
    vol_ratio = tech.get("vol_ratio", 1)
    macd_bull = tech.get("macd_bullish", False)
    structure = tech.get("structure", "")
    tf_conf   = tech.get("tf_confluence", "")
    stoch     = tech.get("stoch_k", 50)
    obv       = tech.get("obv_trend", "")
    div_type  = tech.get("divergence_type")

    rsi_ok = rsi < 35 or rsi > 65
    vol_ok = vol_ratio > 1.5
    dir_ok = (macd_bull or "ALCISTA" in structure or "ALCISTA" in tf_conf
              or stoch < 25 or stoch > 75 or obv == "ACUMULACION" or div_type)

    if rsi_ok and vol_ok and dir_ok:
        capas_ok += 1
        motivos.append(f"tecnico RSI{rsi:.0f} vol{vol_ratio}x")

    # Capa 2: Timeframes
    bull_tf = sum([
        tech.get("daily_trend")   == "ALCISTA",
        tech.get("weekly_trend")  == "ALCISTA",
        tech.get("monthly_trend") == "ALCISTA",
    ])
    bear_tf = sum([
        tech.get("daily_trend")   == "BAJISTA",
        tech.get("weekly_trend")  == "BAJISTA",
        tech.get("monthly_trend") == "BAJISTA",
    ])
    if bull_tf >= 2 or bear_tf >= 2:
        capas_ok += 1
        motivos.append(f"TF {bull_tf}bull/{bear_tf}bear")

    # Capa 3: Fundamental
    upside   = fund.get("analyst_upside", 0) or 0
    ins_buys = fund.get("insider_buys", 0)
    beats    = fund.get("earnings_beats", 0)
    short    = fund.get("short_interest", 0) or 0
    squeeze  = special_signals.get("squeeze")
    insider  = special_signals.get("insider")
    pre_earn = special_signals.get("pre_earnings")

    if abs(upside) > 5 or ins_buys >= 2 or beats >= 3 or short > 15 or squeeze or insider or pre_earn:
        capas_ok += 1
        motivos.append(f"fund upside{upside:+.0f}% ins{ins_buys}")

    # Capa 4: Sentimiento
    sent_score  = sent.get("sentiment_score", 0)
    sector_perf = sent.get("sector_perf", 0) or 0
    st          = sent.get("stocktwits") or {}
    st_extreme  = (st.get("bullish_pct", 50) > 70 or st.get("bearish_pct", 50) > 70)

    if sent_score > 1 or abs(sector_perf) > 0.5 or st_extreme:
        capas_ok += 1
        motivos.append(f"sent score{sent_score} sect{sector_perf:+.1f}%")

    # Capa 5: Institucional
    if inst_signal != "NEUTRAL" or boost >= 3:
        capas_ok += 1
        motivos.append(f"inst {inst_signal} +{boost}")

    # Señales especiales siempre pasan (PRE-EARNINGS, SQUEEZE, INSIDERS)
    if squeeze or insider or pre_earn:
        capas_ok = max(capas_ok, 3)
        motivos.append("bypass-especial")

    # En BEAR extremo (F&G < 25): umbral 2/5 pero exige score técnico ≥ 8 (calidad antes que cantidad)
    bear_extremo = (
        market_regime.get("regime") == "BEAR"
        and market_context.get("fear_greed", 50) < 25
    )
    umbral = 2 if bear_extremo else 3
    tech_score = tech.get("tech_score", 0)

    if bear_extremo and capas_ok >= 2 and tech_score < 8:
        motivos.append(f"BEAR-calidad: score {tech_score} insuficiente (mín 8)")
        pasa = False
    else:
        pasa = capas_ok >= umbral

    if bear_extremo and pasa:
        motivos.append("BEAR-extremo-2/5")

    resumen = f"{capas_ok}/5 — {' | '.join(motivos) if motivos else 'sin convergencia'}"
    return pasa, resumen

def analyze_ticker(ticker, name="", sector="Unknown", force=False, force_score=False, solo_excepcionales=False):
    # Cooldown de score bajo: saltar si falló N ciclos consecutivos
    if not force and not force_score and _score_cooldown.get(ticker, 0) > 0:
        _score_cooldown[ticker] -= 1
        print(f"  Saltando {ticker} (cooldown score: {_score_cooldown[ticker]} ciclos restantes)")
        return None

    print(f"  Analizando {ticker}...")

    tech = get_market_data(ticker)
    if not tech:
        print(f"    {ticker}: sin datos de mercado")
        return None

    score_minimo_efectivo = max(7, SCORE_MINIMO - 1) if (
        market_regime.get("regime") == "BEAR" and market_context.get("fear_greed", 50) < 25
    ) else SCORE_MINIMO

    if not force and not force_score and tech["tech_score"] < score_minimo_efectivo:
        print(f"    {ticker}: score {tech['tech_score']} insuficiente (mín {score_minimo_efectivo})")
        _score_fail_count[ticker] = _score_fail_count.get(ticker, 0) + 1
        if _score_fail_count[ticker] >= SCORE_FAIL_THRESHOLD:
            _score_cooldown[ticker] = SCORE_COOLDOWN_CICLOS
            _score_fail_count[ticker] = 0
            print(f"    {ticker}: activado cooldown {SCORE_COOLDOWN_CICLOS} ciclos por score bajo repetido")
        return None

    # Score OK — resetear contador de fallos
    _score_fail_count[ticker] = 0

    fund               = get_fundamentals(ticker)
    # Sector real de fundamentales tiene prioridad sobre el pasado como parámetro
    sector_real = fund.get("sector") or sector or tech.get("sector", "Unknown")
    tech["sector"] = sector_real  # persist sector real antes de guardar predicción
    sent               = get_sentiment(ticker, sector_real)
    inst_signal, boost = get_inst_signal(tech)

    # Detectar señales especiales
    signal_type    = "NORMAL"
    special_signals = {}

    # Premarket
    if is_premarket():
        pm_data = get_premarket_data(ticker)
        if pm_data:
            special_signals["premarket"] = pm_data
            if pm_data.get("significant"):
                print(f"    {ticker}: gap premarket {pm_data['gap_pct']:+.1f}%")

    # Put/call ratio
    pc_ratio, pc_interp = get_put_call_ratio(ticker)
    if pc_ratio:
        special_signals["put_call"] = (pc_ratio, pc_interp)

    # Short squeeze
    is_squeeze, squeeze_desc = detect_short_squeeze(fund, tech)
    if is_squeeze:
        special_signals["squeeze"] = squeeze_desc
        signal_type = "SHORT_SQUEEZE"
        boost = min(boost + 3, 15)
        print(f"    {ticker}: {squeeze_desc}")

    # Insiders masivos
    is_insider, insider_desc = detect_massive_insider(fund)
    if is_insider and signal_type == "NORMAL":
        special_signals["insider"] = insider_desc
        signal_type = "INSIDER_MASSIVE"
        boost = min(boost + 3, 15)
        print(f"    {ticker}: {insider_desc}")

    # Pre-earnings (solo en automático)
    if not force:
        is_pe, pe_days, pe_desc = check_pre_earnings_signal(fund, tech)
        if is_pe:
            special_signals["pre_earnings"] = pe_desc
            if signal_type == "NORMAL":
                signal_type = "PRE_EARNINGS"
            boost = min(boost + 4, 15)
            print(f"    {ticker}: {pe_desc}")

    # Prefiltro de convergencia — evita llamadas IA innecesarias (~85% ahorro)
    if not force and not force_score:
        pasa, pf_resumen = pre_filter_convergence(tech, fund, sent, inst_signal, boost, special_signals)
        if not pasa:
            print(f"    {ticker}: prefiltro {pf_resumen}")
            with _ws_lock:
                watch_signals[ticker] = {"last_analyzed": datetime.now(SPAIN_TZ).isoformat(), "developing": False}
            save_state()
            return None
        print(f"    {ticker}: prefiltro OK — {pf_resumen}")

    # Construir prompt
    if force:
        prompt = _build_manual_prompt(tech, fund, sent)
    else:
        prompt = _build_auto_prompt(tech, fund, sent, inst_signal, boost, special_signals)

    ai_response = call_ai(prompt, max_tokens=700 if force else 900)
    if not ai_response:
        return None

    if not force and "NO_SIGNAL" in ai_response:
        with _ws_lock:
            watch_signals[ticker] = {"last_analyzed": datetime.now(SPAIN_TZ).isoformat(), "developing": False}
        save_state()
        return None

    # Extraer confianza — regex robusto con validación de rango
    conf_ia  = 0
    stop_ia  = None  # stop loss que devuelve la IA
    for line in ai_response.splitlines():
        if "CONFIANZA:" in line:
            m_conf = re.search(r"CONFIANZA:\s*(\d{1,3})", line)
            if m_conf:
                parsed = int(m_conf.group(1))
                conf_ia = max(0, min(parsed, 99))  # forzar rango 0-99
        if "STOP" in line and "LOSS" in line:
            m_stop = re.search(r"\$?([\d]+\.?[\d]*)", line)
            if m_stop:
                try:
                    stop_ia = float(m_stop.group(1))
                except ValueError:
                    pass

    conf_final = min(conf_ia + boost, 99) if not force else conf_ia

    # Controles en automático
    if not force:
        if conf_final < CONF_NORMAL:
            print(f"    {ticker}: confianza {conf_final}% insuficiente")
            return None

        if solo_excepcionales and conf_final < CONF_EXCEPCIONAL:
            print(f"    {ticker}: solo excepcionales activo, descartado")
            return None

        # Ajuste por régimen
        regime_adj   = get_regime_conf_adjustment()
        signal_check = "COMPRAR"
        for line in ai_response.splitlines():
            if line.startswith("SEÑAL:"):
                signal_check = "VENDER" if "VENDER" in line else "COMPRAR"
                break

        extra_conf = regime_adj.get(signal_check, 0)
        if conf_final < CONF_NORMAL + extra_conf:
            print(f"    {ticker}: conf insuficiente para régimen {market_regime.get('regime')} ({conf_final}%)")
            return None

        puede, motivo = puede_enviar_alerta(signal_check, conf_final, signal_type)
        if not puede:
            print(f"    {ticker}: {motivo}")
            return None

    session_tag = _session_label(datetime.now(SPAIN_TZ))
    text, signal = format_alert(tech, ai_response, conf_final, session_tag, signal_type)

    with _ws_lock:
        watch_signals[ticker] = {
            "last_analyzed": datetime.now(SPAIN_TZ).isoformat(),
            "developing":    conf_final >= CONF_FUERTE,
        }
    save_state()

    nivel = "EXCEPCIONAL ⚡" if conf_final >= CONF_EXCEPCIONAL else "FUERTE 🔥" if conf_final >= CONF_FUERTE else "NORMAL 🟢"
    print(f"    {ticker}: {nivel} {signal} {conf_final}% [{signal_type}]")

    return text, signal, conf_final, tech, signal_type, stop_ia

# ═══════════════════════════════════════════════════════════════════════
# CICLO AUTOMÁTICO — cada 5 minutos
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# SEGURIDAD — rate limiting, whitelist, watchdog, monitorización
# ═══════════════════════════════════════════════════════════════════════

def check_rate_limit(user_id):
    """Máximo MAX_ANALIZAR_POR_HORA por usuario por hora."""
    now = time.time()
    if user_id not in cmd_rate_limit:
        cmd_rate_limit[user_id] = []
    # Limpiar timestamps de hace más de 1 hora
    cmd_rate_limit[user_id] = [t for t in cmd_rate_limit[user_id] if now - t < 3600]
    if len(cmd_rate_limit[user_id]) >= MAX_ANALIZAR_POR_HORA:
        return False
    cmd_rate_limit[user_id].append(now)
    return True


def is_owner(user_id):
    """Solo el dueño puede usar !analizar si OWNER_DISCORD_ID está configurado."""
    if not OWNER_DISCORD_ID:
        return True   # si no está configurado, cualquiera puede usar
    return str(user_id) == str(OWNER_DISCORD_ID)


def track_ai_cost():
    """Registra una llamada IA y alerta si se supera el gasto diario."""
    global coste_estimado_hoy, ai_calls_hoy
    coste_estimado_hoy += 0.00548
    ai_calls_hoy       += 1
    if coste_estimado_hoy >= COSTE_MAX_DIA:
        send_log(
            f"⚠️ ALERTA COSTE: gasto estimado hoy ${coste_estimado_hoy:.3f} "
            f"({ai_calls_hoy} llamadas IA) — superado límite ${COSTE_MAX_DIA}"
        )


def reset_daily_counters():
    """Resetea contadores diarios a medianoche."""
    global coste_estimado_hoy, ai_calls_hoy, _score_fail_count, _score_cooldown
    coste_estimado_hoy = 0.0
    ai_calls_hoy       = 0
    _score_fail_count  = {}
    _score_cooldown    = {}
    send_log("🔄 Nuevo día — límites y contadores reseteados")


def check_429_watchdog(errores_429, total_intentos):
    """
    Si el 80%+ de intentos del ciclo son 429, incrementa contador.
    Si llega a MAX_429_SEGUIDOS ciclos, pausa el bot PAUSA_429_MINUTOS minutos.
    """
    global ciclos_429_seguidos, pausa_429_hasta
    if total_intentos == 0:
        return False

    tasa_429 = errores_429 / total_intentos
    if tasa_429 >= 0.8:
        ciclos_429_seguidos += 1
        if ciclos_429_seguidos >= MAX_429_SEGUIDOS:
            pausa_429_hasta = datetime.now(SPAIN_TZ) + timedelta(minutes=PAUSA_429_MINUTOS)
            ciclos_429_seguidos = 0
            send_log(
                f"⚠️ Yahoo bloqueando fuerte ({int(tasa_429*100)}% de 429 en {MAX_429_SEGUIDOS} ciclos) "
                f"— pausando {PAUSA_429_MINUTOS} min hasta las {pausa_429_hasta.strftime('%H:%M')}"
            )
            return True
    else:
        ciclos_429_seguidos = 0
    return False


def circuit_breaker_active():
    """Devuelve True si el circuit breaker está activo (umbral subido por rachas de pérdidas)."""
    global _cb_active_until
    if _cb_active_until and datetime.now(SPAIN_TZ) < _cb_active_until:
        return True
    if _cb_active_until and datetime.now(SPAIN_TZ) >= _cb_active_until:
        _cb_active_until = None
        send_log("✅ Circuit breaker desactivado — umbrales normales restablecidos")
    return False


def _cb_register_result(result):
    """Registra win/loss en el circuit breaker y activa si hay racha de pérdidas."""
    global _cb_consecutive_losses, _cb_active_until
    if result == "win":
        _cb_consecutive_losses = 0
    elif result == "loss":
        _cb_consecutive_losses += 1
        if _cb_consecutive_losses >= CB_MAX_LOSSES and not circuit_breaker_active():
            _cb_active_until = datetime.now(SPAIN_TZ) + timedelta(hours=CB_DURATION_HOURS)
            send_log(
                f"⚠️ CIRCUIT BREAKER activado — {CB_MAX_LOSSES} pérdidas seguidas\n"
                f"Umbral mínimo +{CB_CONF_BOOST}% durante {CB_DURATION_HOURS}h hasta {_cb_active_until.strftime('%d/%m %H:%M')}"
            )


def is_paused_429():
    """Devuelve True si el bot está en pausa por 429 masivos."""
    global pausa_429_hasta
    if pausa_429_hasta and datetime.now(SPAIN_TZ) < pausa_429_hasta:
        return True
    if pausa_429_hasta and datetime.now(SPAIN_TZ) >= pausa_429_hasta:
        pausa_429_hasta = None
        send_log("✅ Pausa Yahoo terminada — reanudando análisis")
    return False


def daily_summary():
    """Resumen diario a las 22:00h en #log."""
    now      = datetime.now(SPAIN_TZ)
    resolved = len([p for p in predictions if p.get("result") != "pending"])
    pending  = len([p for p in predictions if p.get("result") == "pending"])
    wins     = len([p for p in predictions if p.get("result") == "win"])
    losses   = len([p for p in predictions if p.get("result") == "loss"])
    rate     = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0

    send_log(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 RESUMEN DIARIO — {now.strftime('%d/%m/%Y')}\n"
        f"🤖 Llamadas IA: {ai_calls_hoy} | Coste est: ${coste_estimado_hoy:.3f}\n"
        f"📈 Alertas hoy: {alertas_hoy()}/{MAX_ALERTAS_DIA}\n"
        f"🎯 Historial: {wins}✅ {losses}❌ {pending}⏳ | Acierto: {rate}%\n"
        f"🧠 Reglas aprendidas: {len(learnings.get('rules', []))}\n"
        f"📡 Régimen: {market_regime.get('regime','?')} | F&G: {market_context.get('fear_greed',50)}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )


def cmd_estado():
    """Responde a !estado con situación actual del bot."""
    now      = datetime.now(SPAIN_TZ)
    fg       = market_context.get("fear_greed", 50)
    regime   = market_regime.get("regime", "?")
    pending  = [p for p in predictions if p.get("result") == "pending"]
    wins     = len([p for p in predictions if p.get("result") == "win"])
    losses   = len([p for p in predictions if p.get("result") == "loss"])
    rate     = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else 0
    eco_warn = "\n⚠️ ALTO IMPACTO: " + ", ".join(econ_calendar.get("high_impact_today", [])) if econ_calendar.get("is_high_impact") else ""

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📡 **ESTADO — {now.strftime('%H:%M %d/%m/%Y')}**",
        f"Régimen: {regime} | F&G: {fg}/100 ({_fg_label(fg)}) | VIX: {market_context.get('vix',0)}{eco_warn}",
        f"Alertas hoy: {alertas_hoy()}/{MAX_ALERTAS_DIA} | Cola calidad: {'activa hasta 14:00' if now.hour < HORA_DESBLOQUEO else 'desbloqueada'}",
        f"Llamadas IA hoy: {ai_calls_hoy} | Coste est: ${coste_estimado_hoy:.3f}",
        f"Historial: {wins}✅ {losses}❌ | Acierto: {rate}%",
        f"Predicciones pendientes: {len(pending)}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    send_solicitud("\n".join(lines))


def cmd_pendientes():
    """Responde a !pendientes con lista de predicciones activas."""
    pending = [p for p in predictions if p.get("result") == "pending"]
    if not pending:
        send_solicitud("⏳ No hay predicciones pendientes ahora mismo.")
        return

    lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━", "⏳ **PREDICCIONES PENDIENTES**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for p in sorted(pending, key=lambda x: x["date"], reverse=True):
        # Intentar obtener precio actual
        current_price = None
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{p['ticker']}?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=6,
            )
            if r.status_code == 200:
                _status_result = r.json().get("chart", {}).get("result") or []
                if _status_result:
                    closes = _status_result[0]["indicators"]["quote"][0].get("close", [])
                    closes = [c for c in closes if c]
                    if closes:
                        current_price = closes[-1]
        except Exception as e:
            print(f"[ERROR] portfolio_status {p['ticker']}: {e}")

        entry  = p.get("entry", 0)
        target = p.get("target", 0)
        signal = p.get("signal", "?")
        days   = (datetime.now(SPAIN_TZ) - datetime.fromisoformat(p["date"]).astimezone(SPAIN_TZ)).days
        stype  = f" [{p.get('signal_type','NORMAL')}]" if p.get("signal_type","NORMAL") != "NORMAL" else ""

        if current_price:
            chg     = round(((current_price - entry) / entry) * 100, 1) if entry else 0
            to_tgt  = round(((target - current_price) / current_price) * 100, 1) if target and current_price else 0
            price_str = f"${current_price:.2f} ({chg:+.1f}%) | falta {to_tgt:+.1f}% para objetivo"
        else:
            price_str = f"entrada ${entry:.2f} → objetivo ${target:.2f}"

        lines.append(f"{'📈' if signal=='COMPRAR' else '📉'} **{p['ticker']}**{stype} — {price_str} | {days}d")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    send_solicitud("\n".join(lines))

def watch_cycle():
    now = datetime.now(SPAIN_TZ)
    # Fin de semana: mercado cerrado, no escanear
    if now.weekday() >= 5:  # 5=sábado, 6=domingo
        return
    if now.hour < 9 or now.hour >= 23:
        return

    # Watchdog 429 — si Yahoo está bloqueando fuerte, pausar
    if is_paused_429():
        print(f"  ⏸️ Pausa Yahoo activa hasta {pausa_429_hasta.strftime('%H:%M')}")
        return

    solo_excepcionales = alertas_hoy() >= MAX_ALERTAS_DIA

    # Info de cola de calidad
    queue_active = now.hour < HORA_DESBLOQUEO
    if queue_active and not solo_excepcionales:
        print(f"  Cola de calidad activa — solo Excepcionales hasta las {HORA_DESBLOQUEO}:00")

    candidates = quick_scan()
    if not candidates:
        return

    not_analyzed = [
        t for t in UNIVERSE
        if not already_alerted_today(t)
        and (t not in watch_signals
             or (datetime.now(SPAIN_TZ) - _to_aware(
                 watch_signals[t].get("last_analyzed", "2000-01-01T00:00:00+00:00")
             )).total_seconds() > 86400)
    ]
    rotation = random.sample(not_analyzed, min(4, len(not_analyzed)))
    seen_set  = {c["ticker"] for c in candidates}
    rotation_items = [
        {"ticker": t, "name": t, "sector": "Unknown", "score": 0, "source": "rotation"}
        for t in rotation if t not in seen_set
    ]

    # ── Earnings priority: tickers con earnings en 1-10 días del universo ──
    # Se añaden al frente de la cola para analizarse aunque no pasen score técnico.
    # El cooldown es de 6h (en vez del estándar 1h) para no saturar en semana de earnings.
    earnings_priority = []
    if earnings_watch:
        seen_ep = {c["ticker"] for c in candidates} | {r["ticker"] for r in rotation_items}
        for tk, info in sorted(earnings_watch.items(), key=lambda x: x[1].get("days_ahead", 99)):
            days_left = info.get("days_ahead", 99)
            if days_left < 1 or days_left > 10:
                continue
            if already_alerted_today(tk):
                continue
            last = watch_signals.get(tk, {}).get("last_analyzed", "2000-01-01T00:00:00+00:00")
            hours_since = (datetime.now(SPAIN_TZ) - _to_aware(last)).total_seconds() / 3600
            if hours_since < 6:
                continue
            if tk not in seen_ep:
                earnings_priority.append({
                    "ticker":        tk,
                    "name":          info.get("name", tk),
                    "sector":        "Unknown",
                    "score":         SCORE_MINIMO,   # forzar análisis aunque no pase quick_scan
                    "source":        f"earnings_{days_left}d",
                    "earnings_days": days_left,
                })
                seen_ep.add(tk)

    to_analyze = earnings_priority + candidates + rotation_items
    regime     = market_regime.get("regime", "?")
    eco_flag   = " ⚠️ALTO IMPACTO" if econ_calendar.get("is_high_impact") else ""
    pm_flag    = " 🌅PREMARKET" if is_premarket() else ""
    print(f"\n[{now.strftime('%H:%M')} ES] {len(to_analyze)} candidatos | alertas hoy: {alertas_hoy()}/{MAX_ALERTAS_DIA} | {regime}{eco_flag}{pm_flag}")

    alerts_this_cycle = 0
    errores_429_ciclo  = 0
    intentos_ciclo     = 0

    # Descarga batch de todos los tickers antes del loop (1 request en vez de N)
    prefetch_tickers([item["ticker"] for item in to_analyze])

    _regime_now = market_regime.get("regime", "LATERAL")
    _max_ai_ciclo = 1 if _regime_now == "BEAR" else MAX_AI_POR_CICLO

    for item in to_analyze:
        if alerts_this_cycle >= _max_ai_ciclo:
            break

        ticker = item["ticker"]
        if already_alerted_today(ticker):
            continue

        last = watch_signals.get(ticker, {}).get("last_analyzed")
        if last:
            elapsed = (datetime.now(SPAIN_TZ) - _to_aware(last)).total_seconds()
            if elapsed < 3600:
                continue

        intentos_ciclo += 1
        is_earnings_forced = item.get("source", "").startswith("earnings_")
        result = analyze_ticker(
            ticker,
            item.get("name", ticker),
            item.get("sector", "Unknown"),
            force_score=is_earnings_forced,    # bypass score/cooldown/prefiltro para earnings
            solo_excepcionales=solo_excepcionales,
        )
        if not result:
            # Detectar si fue 429 (el ticker queda sin datos)
            if ticker not in watch_signals or not watch_signals.get(ticker, {}).get("last_analyzed"):
                errores_429_ciclo += 1
                # Marcar cooldown para no reintentar hasta ~20 min
                with _ws_lock:
                    watch_signals[ticker] = {
                        "last_analyzed": (datetime.now(SPAIN_TZ) - timedelta(minutes=40)).isoformat(),
                        "developing": False,
                        "reason": "429_cooldown",
                    }
                save_state()
            time.sleep(2)
            continue

        text, signal, conf, tech, signal_type, stop_ia = result
        send_alert(text)
        save_prediction(ticker, signal, tech, conf, signal_type, stop_ia)
        alerts_this_cycle += 1

        nivel = "EXCEPCIONAL ⚡" if conf >= CONF_EXCEPCIONAL else "FUERTE 🔥" if conf >= CONF_FUERTE else "NORMAL 🟢"
        print(f"    → Alerta: {ticker} {nivel} ({signal}, {conf}%, {signal_type})")
        time.sleep(6)

    if alerts_this_cycle > 0:
        print(f"  {alerts_this_cycle} alerta(s) este ciclo")

    # Watchdog 429
    check_429_watchdog(errores_429_ciclo, intentos_ciclo)

# ═══════════════════════════════════════════════════════════════════════
# COMANDOS MANUALES
# ═══════════════════════════════════════════════════════════════════════

def listen_commands(init=False):
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
            return

        try:
            messages = r.json()
        except ValueError as e:
            print(f"[ERROR] poll_commands JSON: {e}")
            return
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

            text    = msg.get("content", "").strip()
            user_id = msg.get("author", {}).get("id", "")

            # Comandos de consulta — sin restricción
            if text.strip().lower() == "!estado":
                cmd_estado()
                continue
            if text.strip().lower() == "!pendientes":
                cmd_pendientes()
                continue

            tickers = []
            for line in text.splitlines():
                line = line.strip().lower()
                if not line.startswith("!analizar"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    t = parts[1].upper().strip()
                    if re.match(r'^[A-Z]{1,5}$', t) and t not in tickers:
                        tickers.append(t)

            if not tickers:
                continue

            # Seguridad: owner check
            if not is_owner(user_id):
                send_solicitud("⛔ No tienes permiso para usar !analizar.")
                continue

            # Seguridad: rate limit
            if not check_rate_limit(user_id):
                send_solicitud(f"⏳ Máximo {MAX_ANALIZAR_POR_HORA} análisis por hora. Intenta más tarde.")
                continue

            for ticker in tickers:
                print(f"  !analizar {ticker} (user: {user_id})")
                send_solicitud(f"🔍  Analizando **{ticker}**... dame unos segundos.")
                update_status(f"🔍  Analizando **{ticker}** bajo demanda...")

                result = analyze_ticker(ticker, ticker, "Unknown", force=True)
                now_es = datetime.now(SPAIN_TZ)

                if not result:
                    send_solicitud(
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"❓  **{ticker}** no encontrado\n"
                        f"Verifica el ticker (ej: NVDA, AAPL)\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                else:
                    text_alert, signal, conf, tech, signal_type, _stop = result
                    send_solicitud(text_alert)

                fg_loop = market_context["fear_greed"]
                update_status(
                    f"🟢  **Activo v5.2** — vigilando mercado\n"
                    f"📡 F&G: {fg_loop} ({_fg_label(fg_loop)}) | VIX: {market_context['vix']} | {market_regime.get('regime','?')}\n"
                    f"🕐  {now_es.strftime('%H:%M')} — si no cambia en 10 min el bot está caído"
                )
                time.sleep(2)

    except Exception as e:
        import traceback
        print(f"  listen_commands excepción: {e}")
        print(traceback.format_exc())

# ═══════════════════════════════════════════════════════════════════════
# RESUMEN DOMINICAL
# ═══════════════════════════════════════════════════════════════════════

def weekly_report():
    now = datetime.now(SPAIN_TZ)
    update_prediction_results()
    run_learning_engine()

    wins    = [p for p in predictions if p.get("result") == "win"]
    losses  = [p for p in predictions if p.get("result") == "loss"]
    pending = [p for p in predictions if p.get("result") == "pending"]

    week_ago  = now - timedelta(days=7)
    wins_w    = [p for p in wins    if _to_aware(p["date"]) >= week_ago]
    losses_w  = [p for p in losses  if _to_aware(p["date"]) >= week_ago]
    pending_w = [p for p in pending if _to_aware(p["date"]) >= week_ago]

    total    = len(wins_w) + len(losses_w)
    win_rate = round(len(wins_w) / total * 100) if total > 0 else 0
    avg_win  = round(sum(((p.get("exit_price",0) - p["entry"]) / p["entry"] * 100) for p in wins_w)   / len(wins_w),   1) if wins_w   else 0
    avg_loss = round(sum(((p.get("exit_price",0) - p["entry"]) / p["entry"] * 100) for p in losses_w) / len(losses_w), 1) if losses_w else 0

    # Desglose por tipo de señal
    type_stats = {}
    for p in wins_w + losses_w:
        st = p.get("signal_type", "NORMAL")
        if st not in type_stats:
            type_stats[st] = {"wins": 0, "total": 0}
        type_stats[st]["total"] += 1
        if p["result"] == "win":
            type_stats[st]["wins"] += 1

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊  **RESUMEN SEMANAL v5.2**",
        f"Semana {(now-timedelta(days=7)).strftime('%d/%m')} → {now.strftime('%d/%m/%Y')}",
        f"Régimen: {market_regime.get('regime','?')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for w in sorted(wins_w, key=lambda x: x.get("exit_price", 0) - x.get("entry", 0), reverse=True):
        chg  = round(((w.get("exit_price",0) - w["entry"]) / w["entry"]) * 100, 1) if w.get("exit_price") else 0
        tag  = f" [{w.get('signal_type','NORMAL')}]" if w.get("signal_type", "NORMAL") != "NORMAL" else ""
        lines.append(f"✅  **{w['ticker']}**{tag}  {chg:+.1f}%  en {w.get('days_to_result','?')}d")
    for l in sorted(losses_w, key=lambda x: x.get("exit_price", 0) - x.get("entry", 0)):
        chg  = round(((l.get("exit_price",0) - l["entry"]) / l["entry"]) * 100, 1) if l.get("exit_price") else 0
        tag  = f" [{l.get('signal_type','NORMAL')}]" if l.get("signal_type", "NORMAL") != "NORMAL" else ""
        lines.append(f"❌  **{l['ticker']}**{tag}  {chg:+.1f}%  stop {l.get('days_to_result','?')}d")
    for p in pending_w:
        lines.append(f"⏳  **{p['ticker']}** pendiente")

    if not wins_w and not losses_w and not pending_w:
        lines.append("Sin predicciones esta semana")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯  {len(wins_w)}/{total} — {win_rate}%",
    ]
    if wins_w:   lines.append(f"💰  Media: {avg_win:+.1f}%")
    if losses_w: lines.append(f"📉  Media stops: {avg_loss:+.1f}%")

    # Stats por tipo de señal
    if type_stats:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("📈  **POR TIPO DE SEÑAL**")
        for st, d in type_stats.items():
            wr = round(d["wins"] / d["total"] * 100) if d["total"] > 0 else 0
            lines.append(f"  {st}: {d['wins']}/{d['total']} ({wr}%)")

    # Reglas aprendidas
    top_rules = [r for r in learnings.get("rules", []) if r["sample_size"] >= 5][:3]
    if top_rules:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🧠  **PATRONES APRENDIDOS**")
        for r in top_rules:
            icon = "✅" if r["win_rate"] >= 65 else "⚠️"
            lines.append(f"{icon}  {r['description']}")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    send_acierto("\n".join(lines))
    send_log(f"📊 Dominical: {len(wins_w)}✅ {len(losses_w)}❌ {len(pending_w)}⏳ | Reglas: {len(learnings.get('rules',[]))}")


def weekly_summary():
    now    = datetime.now(SPAIN_TZ)
    total  = len([p for p in predictions if p.get("result") != "pending"])
    wins   = len([p for p in predictions if p.get("result") == "win"])
    losses = len([p for p in predictions if p.get("result") == "loss"])
    pend   = len([p for p in predictions if p.get("result") == "pending"])
    rate   = round(wins / total * 100, 1) if total > 0 else 0
    send_log(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 RESUMEN — {now.strftime('%d/%m/%Y')}\n"
        f"✅ {wins}  ❌ {losses}  ⏳ {pend} | Acierto: {rate}%\n"
        f"🧠 Reglas: {len(learnings.get('rules',[]))} | Régimen: {market_regime.get('regime','?')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def _update_macro_if_market_hours():
    """Actualiza macro solo durante horario de mercado (15:00-22:30 España)."""
    now = datetime.now(SPAIN_TZ)
    if now.weekday() >= 5:
        return
    if (now.hour == 15 and now.minute >= 0) or (15 < now.hour < 22) or (now.hour == 22 and now.minute <= 30):
        update_market_context()


def main():
    # Validar variables de entorno críticas antes de arrancar
    missing = [name for name, val in [
        ("DISCORD_TOKEN", DISCORD_TOKEN),
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ] if not val]
    if missing:
        raise SystemExit(f"ERROR: variables de entorno no configuradas: {', '.join(missing)}")

    now = datetime.now(SPAIN_TZ)
    print(f"StockBot Pro v5.2 — {now.strftime('%H:%M %d/%m/%Y')}")

    load_state()
    update_status(f"⚙️  **Arrancando v5.2...**\n🕐  {now.strftime('%H:%M  %d/%m/%Y')}")
    update_market_context()   # incluye régimen + geopolítica + calendario

    fg     = market_context["fear_greed"]
    fg_str = _fg_label(fg)
    sp500  = market_context["sp500_change"]
    vix    = market_context["vix"]
    regime = market_regime.get("regime", "?")
    rules  = len(learnings.get("rules", []))
    eco    = ", ".join(econ_calendar.get("high_impact_today", [])) or "ninguno"

    send_log(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 **StockBot v5.2** — {now.strftime('%H:%M %d/%m/%Y')}\n"
        f"📡 F&G: {fg}/100 ({fg_str}) | S&P500: {sp500:+.2f}% | VIX: {vix}\n"
        f"🔄 Régimen: {regime} | Universo: {len(UNIVERSE)} acciones\n"
        f"🧠 Reglas: {rules} | Eventos macro: {eco}\n"
        f"📊 Alertas hoy: {alertas_hoy()}/{MAX_ALERTAS_DIA}\n"
        f"🟢 Solo Excepcionales hasta las {HORA_DESBLOQUEO}:00h\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    post_instrucciones()
    print("  Instrucciones publicadas")

    update_status(
        f"🟢  **Activo v5.2** — vigilando mercado\n"
        f"📡 F&G: {fg} ({fg_str}) | VIX: {vix} | {regime}\n"
        f"🕐  {now.strftime('%H:%M  %d/%m/%Y')}"
    )

    update_earnings_watch()   # escanear earnings al arrancar (sin esperar al 09:05)
    watch_cycle()
    listen_commands(init=True)

    schedule.every(5).minutes.do(watch_cycle)
    schedule.every(30).minutes.do(_update_macro_if_market_hours)
    schedule.every().day.at("09:00").do(update_market_context)
    schedule.every().day.at("09:05").do(update_earnings_watch)   # justo después del contexto macro
    schedule.every().day.at("00:01").do(reset_daily_counters)
    schedule.every().day.at("22:00").do(daily_summary)
    schedule.every().sunday.at("10:00").do(weekly_report)
    schedule.every().monday.at("09:00").do(weekly_summary)
    schedule.every().thursday.at("09:00").do(weekly_summary)

    # Dashboard web — hilo daemon (no afecta al bot si falla)
    try:
        import threading, web as _web
        threading.Thread(target=_web.start_web, daemon=True).start()
        print("  Dashboard web iniciado en puerto", os.environ.get("PORT", 8080))
    except Exception as _we:
        print(f"  Dashboard web no disponible: {_we}")

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
            eco_warn = " ⚠️ALTO IMPACTO" if econ_calendar.get("is_high_impact") else ""
            if now_loop.weekday() == 5:
                day_txt = "🗓️ Hoy es **sábado** — mercado cerrado. El domingo resumen semanal a las 10:00"
            elif now_loop.weekday() == 6:
                day_txt = "🗓️ Hoy es **domingo** — mercado cerrado. El lunes abrimos a las 15:30"
            else:
                day_txt = f"🕐  {now_loop.strftime('%H:%M')} — si no cambia en 10 min el bot está caído"
            update_status(
                f"🟢  **Activo v5.2** — vigilando mercado\n"
                f"📡 F&G: {fg_loop} ({_fg_label(fg_loop)}) | VIX: {market_context['vix']} | {market_regime.get('regime','?')}{eco_warn}\n"
                f"{day_txt}"
            )
            last_status_check = ts

        time.sleep(5)


if __name__ == "__main__":
    main()
