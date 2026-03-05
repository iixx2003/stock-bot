"""
StockBot Pro v5
───────────────────────────────────────────────────────────────────────
Cambios respecto a v4:
  - Universo ampliado a ~2.000 acciones (Russell 2000 incluido)
  - Base de datos enriquecida: 30+ indicadores por predicción
  - Motor de aprendizaje autónomo en 5 niveles (activo desde predicción 20)
  - Correlación entre activos relacionados
  - Detección de régimen de mercado (bull/bear/lateral)
  - Memoria de errores específicos por indicador y sector
  - Detección de manipulación y pump & dump
  - Prompts adaptativos con conocimiento acumulado
  - Anti-429 mejorado: delays, backoff, rotación de hosts

Arquitectura de 6 capas:
  1. quick_scan()        — screeners Yahoo + correlaciones, sin IA
  2. get_market_data()   — técnico completo: diario, semanal, mensual
  3. get_fundamentals()  — P/E, short, earnings, insiders (1 petición)
  4. get_sentiment()     — noticias NewsAPI + RSS + ETF sectorial
  5. get_inst_signal()   — institucional + detección manipulación
  6. call_ai()           — Claude con prompts adaptativos por aprendizaje

Flujo automático:  quick_scan → analyze_ticker(force=False) → send_alert
Flujo manual:      !analizar TICKER → analyze_ticker(force=True) → send_solicitud
"""

import os, time, json, random, schedule, requests, feedparser, anthropic
from datetime import datetime, timedelta
from collections import defaultdict
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
CONF_NORMAL      = 85   # mínimo para alerta automática
CONF_FUERTE      = 88   # nivel fuerte
CONF_EXCEPCIONAL = 94   # sin límite diario, siempre se envía

# Límites diarios
MAX_ALERTAS_DIA  = 3    # máximo total (Excepcional no cuenta)
MAX_VENTAS_DIA   = 1    # máximo ventas al día
MAX_AI_POR_CICLO = 4    # máximo llamadas IA por ciclo de 5 min

# Score técnico mínimo para análisis profundo
SCORE_MINIMO = 6

# Aprendizaje: mínimo de predicciones resueltas para activar cada nivel
LEARN_MIN_PREDS = 20   # nivel 1 y 2
LEARN_MIN_L3    = 40   # nivel 3 (sector)
LEARN_MIN_L4    = 60   # nivel 4 (hora/sesión)
LEARN_MIN_L5    = 80   # nivel 5 (autopuntuación completa)

# Archivos de persistencia
PREDICTIONS_FILE  = "/app/data/predictions.json"
WATCHSTATE_FILE   = "/app/data/watchstate.json"
LEARNINGS_FILE    = "/app/data/learnings.json"
REGIME_FILE       = "/app/data/regime.json"

# ═══════════════════════════════════════════════════════════════════════
# UNIVERSO DE ACCIONES (~2.000 tickers)
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

# Russell 2000 — small caps americanas
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
    "CHEF","CHGG","CHRS","CHUY","CIFR","CINF","CIVB","CIVITAS","CKPT","CLBK","CLBT","CLDT",
    "CLFD","CLMT","CLNC","CLNE","CLNN","CLOV","CLPR","CLPT","CLRB","CLRO","CLSK","CLVT",
    "CLWT","CMBM","CMCO","CMLS","CMMB","CMPO","CMRX","CMTL","CNDT","CNMD","CNNB","CNOB",
    "CNXC","CNXN","CODA","CODX","COEP","COFS","COGT","COHU","COKE","COLB","CONN","CONX",
    "CORR","CORS","CORT","COVA","COVS","CPRT","CPSI","CPTK","CRAI","CRDO","CRIS","CRMT",
    "CRNX","CROX","CRSP","CRTD","CRVL","CRVO","CRWD","CRWS","CSBR","CSGS","CSLM","CSPI",
    "CSSE","CSTE","CSTL","CSTR","CTBI","CTGO","CTKB","CTLP","CTOS","CTSH","CTVA","CTXS",
    "CUBI","CURL","CUTR","CVBF","CVCO","CVGW","CVLG","CVLT","CVLY","CWCO","CWEN","CWST",
    "DAKT","DALI","DBRG","DBTX","DCBO","DCFC","DCOM","DCPH","DDOG","DENN","DFIN","DGICA",
    "DHIL","DIOD","DJCO","DLHC","DLTH","DMLP","DNOW","DNUT","DOCU","DOGZ","DOOR","DORM",
    "DOSE","DOUG","DPSI","DRCT","DRIO","DRNA","DRTS","DRVN","DSGX","DSSI","DTIL","DTST",
    "DUOL","DXPE","DYAI","DYNE","EARN","EBIX","EBTC","ECBK","ECHO","ECPG","EDSA","EDTK",
    "EFSC","EFXT","EGAN","EGBN","EGHT","EGIO","EGLE","EGRX","EHTH","EKSO","ELAT","ELLO",
    "ELME","ELOX","ELST","EMBC","EMCF","EMKR","EMLD","EMNT","EMXC","ENOV","ENSG","ENTA",
    "ENVB","ENVX","EOLS","EPAC","EPAM","EPIQ","EPIX","EPRT","EQBK","EQNR","EQRX","ERAS",
    "ERII","ERNA","EROS","ESAB","ESBA","ESEA","ESGR","ESNT","ESOA","ESPR","ESSA","ESTA",
    "ESTE","ETAO","ETSY","EVER","EVEX","EVGO","EVGR","EVLO","EVLV","EVMT","EVOP","EVRI",
    "EVTL","EVTV","EWBC","EXAI","EXAS","EXFY","EXLS","EXPI","EXPO","EXTR","EZPW","FARO",
    "FBIZ","FBMS","FBNC","FBRT","FCBC","FCBP","FCCO","FCNCA","FCRX","FCSP","FDMT","FDUS",
    "FEAT","FERG","FGBI","FGEN","FGFPP","FIAC","FIBK","FIHL","FIOB","FISH","FITBI","FIVE",
    "FIVN","FIZZ","FLGC","FLIC","FLNC","FLNT","FLNX","FLUX","FMBH","FMBI","FMCB","FMNB",
    "FMST","FNKO","FNLC","FNWB","FOLD","FONR","FORR","FORTY","FOUN","FOUR","FPAY","FRAF",
    "FRBA","FRGE","FRHC","FRME","FRPH","FRST","FRSX","FRXB","FSBW","FSEA","FSFG","FSLR",
    "FSRX","FSTR","FTDR","FTEK","FTFT","FTLF","FTRE","FULT","FUNC","FUSB","FUTU","FWBI",
    "FXNC","GAIN","GATO","GBBK","GBCI","GBIO","GCMG","GCUS","GENC","GENI","GEOS","GERN",
    "GEVO","GFAI","GFED","GFGD","GFNCP","GGAL","GHIX","GHLD","GIII","GILD","GLDD","GLNG",
    "GLPG","GLRE","GLSI","GLYC","GNLN","GNPX","GNSS","GNTX","GOCO","GOLF","GOOD","GOOG",
    "GOSS","GPAK","GPMT","GPOR","GPRE","GPRK","GRAB","GRFS","GRND","GROM","GROV","GRPN",
    "GRTS","GRTX","GSBC","GSIT","GSKY","GSMG","GSUN","GTLS","GTPB","GUTS","HAFC","HALO",
    "HARP","HAYN","HBAN","HBCP","HBIO","HBNC","HBOS","HCAT","HCCI","HCKT","HCNWF","HCSG",
    "HDSN","HEAR","HEES","HEICO","HELE","HFBL","HFFG","HFWA","HGTY","HIBB","HIFS","HIHO",
    "HIIQ","HIMS","HIPO","HIVE","HKIT","HLMN","HLNE","HLTH","HMPT","HMST","HNNA","HNNAZ",
    "HNST","HOFT","HOLO","HOLX","HONE","HOTH","HOWL","HRMY","HROW","HRPK","HRTG","HRTX",
    "HSAQ","HSHP","HSII","HSKA","HSON","HTBI","HTBK","HTGM","HTLD","HTLF","HTRE","HTUS",
    "HURC","HURN","HUSN","HVBC","HWBK","HWKN","HYMC","HYRE","HYXF","HZNP","IART","IBCP",
    "IBEX","IBIO","IBRX","IBSS","ICAD","ICCC","ICCH","ICFI","ICHR","ICLK","ICMB","ICPT",
    "IDCC","IDEX","IDYA","IESC","IFIN","IFRX","IGMS","IGPK","IHRT","IIIN","IIIV","IKNA",
    "IMAQ","IMCR","IMGO","IMKTA","IMMP","IMMR","IMNN","IMRX","IMTX","IMUX","IMVT","IMXI",
    "INBK","INBKZ","INBS","INCY","INDB","INDP","INDT","INFN","INGN","INMB","INMD","INNV",
    "INPX","INSE","INSM","INSP","INST","INSU","INTJ","INTZ","INVA","INVE","INVH","IPIX",
    "IPSC","IPVF","IPWR","IQMD","IRBT","IRDM","IRET","IRMD","IROQ","IRWD","ISEE","ISPC",
    "ISRG","ISTR","ITGR","ITRM","ITRN","ITRI","IVAC","IVVD","IZEA","JACK","JAGX","JANX",
    "JBLU","JBSS","JBWK","JELD","JFIN","JJSF","JKHY","JNCE","JOBY","JOUT","JPNX","JRVR",
    "JSPR","JTAI","JUPW","JYNT","KALA","KALV","KALU","KAPI","KARO","KBSF","KBTX","KCAP",
    "KDLY","KDMN","KFFB","KFRC","KGEI","KIDS","KION","KIRK","KINS","KLIC","KLTR","KNBE",
    "KNDI","KNSL","KNWN","KOPN","KPTI","KRMD","KROS","KRTX","KRUS","KRYS","KSCP","KTOS",
    "KTTX","KVHI","KYMR","KZIA","LBAI","LBPH","LBRT","LCII","LCNB","LCUT","LDOS","LECO",
    "LEGH","LESL","LGND","LGVN","LHCG","LIQT","LITE","LIVN","LLNW","LMAT","LMFA","LMNL",
    "LMNR","LNTH","LOCO","LOOP","LOVE","LPCN","LPLA","LPSN","LQDA","LQDT","LRFC","LSCC",
    "LSEA","LSXMA","LTHM","LTRN","LTRX","LUNA","LUNG","LUXH","LVOX","LWLG","LYEL","LYRA",
    "LYTS","MACK","MAGS","MAQC","MARPS","MATW","MAXN","MBCN","MBII","MBIN","MBNKP","MBUU",
    "MBWM","MCBC","MCBS","MCFT","MCRI","MCRB","MDGL","MDJH","MDNA","MDVX","MDWD","MEIP",
    "MELI","MERC","MESA","METC","MFAC","MFIN","MFON","MGEE","MGNX","MGPI","MGRC","MGTA",
    "MGYR","MHLD","MIND","MINM","MIRM","MIST","MITK","MJCO","MKFG","MKSI","MKTW","MLAB",
    "MLCO","MLKN","MLNK","MMSI","MMTRS","MNKD","MNMD","MNPR","MNRO","MNST","MNTK","MNTX",
    "MODN","MOFG","MOMO","MOND","MONN","MORA","MORF","MPAA","MPAC","MPLN","MPLX","MPWR",
    "MRAM","MRBK","MRCY","MREO","MRIN","MRKR","MRNS","MRSN","MRTX","MRUS","MSEX","MSFG",
    "MSGE","MSON","MSTR","MTCH","MTCN","MTEX","MTRN","MTRX","MTTE","MTTR","MTUS","MVBF",
    "MVIS","MXCT","MYMD","MYMX","MYND","MYPS","MYRG","MYSZ","NARI","NATH","NATR","NAUT",
    "NAVB","NAVI","NBHC","NBIX","NBTB","NCNA","NCSM","NDLS","NDRA","NEOG","NEON","NEPH",
    "NERD","NESR","NEXT","NFBK","NFLX","NGVC","NHHS","NHTC","NICE","NICK","NINE","NKLA",
    "NKTR","NLSP","NLYS","NMIH","NMRA","NNBR","NODK","NOMD","NOTE","NRBO","NRDS","NRIM",
    "NRIX","NRXP","NSIT","NSSC","NSTG","NTBL","NTCT","NTGR","NTIC","NTLA","NTNX","NTST",
    "NUAN","NUVA","NVAX","NVEI","NVST","NWBI","NWFL","NWGL","NWLI","NWPX","NXGN","NXRT",
    "NXST","NXUS","NYAX","NYMX","OABI","OBNK","OBSV","OCFC","OCGN","OCSL","OCUL","OCUP",
    "ODFL","OFIX","OFLX","OGN","OGTX","OHLB","OKTA","OMAB","OMCL","OMER","OMGA","OMQS",
    "ONTF","ONVO","OPBK","OPCH","OPEN","OPGN","OPOF","OPRX","OPTN","ORAC","ORBC","ORGO",
    "ORGS","ORIC","ORLY","ORMP","ORRF","OSBC","OSCR","OSEA","OSIS","OSST","OSTK","OSTU",
    "OTLK","OTMO","OTRK","OVBC","OVID","OVLY","OVNIX","OWLT","OXLC","OXSQ","OYST","OZRK",
    "PACK","PACS","PAHC","PASG","PATK","PBAX","PBFS","PBHC","PBIP","PBPB","PCBC","PCCO",
    "PCFG","PCOM","PCOR","PCPC","PCSA","PCTI","PCVX","PDCO","PDFS","PDSB","PDYN","PECO",
    "PEGA","PENN","PFBC","PFIS","PFMT","PFNX","PFSI","PGNY","PHAT","PHGE","PHIO","PHVS",
    "PIXY","PKBK","PKOH","PLBC","PLBY","PLCE","PLIN","PLRX","PLSE","PLUR","PMCB","PMTS",
    "PNFP","PNTG","PNTM","POAI","POCI","PODD","POLY","POND","POOL","POWI","PPBI","PPBT",
    "PPRX","PPTA","PRAA","PRAX","PRCH","PRDO","PRFT","PRGS","PRLD","PRME","PRNB","PRPB",
    "PRPL","PRQR","PRST","PRTA","PRTK","PRTS","PRVA","PRZO","PSFE","PSHG","PSMT","PSNL",
    "PSTV","PSTX","PTCT","PTGX","PTHM","PTLO","PTPI","PTVE","PUBM","PVBC","PWOD","PXLW",
    "PYCR","PYXS","QCRH","QDEL","QFIN","QNST","QRTEA","QRTEB","QRVO","QTWO","QUAD","QUBT",
    "QUIK","QURE","RADI","RAPT","RARE","RAVN","RCKT","RCKY","RCON","RCUS","RDCM","RDNT",
    "RDUS","RDVT","RDWR","REAL","REAX","REFI","REGI","REKR","RELI","RELY","RENB","RENN",
    "REPX","REXR","REYN","RFAC","RFIL","RGCO","RGEN","RGLD","RGLS","RGNX","RGRX","RGTI",
    "RIGL","RIOT","RIVN","RLGT","RLAY","RLMD","RLYB","RMBI","RMBS","RMCF","RMNI","RNAC",
    "RNAZ","RNDB","RNET","RNLX","RNXT","ROCC","ROCR","ROCO","RONI","RONN","ROTH","RPAY",
    "RPTX","RRBI","RRGB","RRST","RRTS","RSSS","RTLR","RTPX","RUBY","RVSB","RVNC","RVPH",
    "RVSN","RXDX","RXRX","RYAM","RZLT","SAFE","SAGE","SAIA","SANA","SAND","SANG","SATS",
    "SBCF","SBET","SBFG","SBGI","SBIG","SBSI","SBTX","SCHL","SCKT","SCNX","SCPH","SCSC",
    "SCVL","SDCL","SDGR","SDOT","SELB","SENS","SERV","SFBC","SFNC","SFST","SGBX","SGDM",
    "SGMO","SGRY","SGTX","SHBI","SHLS","SHLT","SHOO","SHPW","SHYF","SIBN","SIEB","SIGA",
    "SIGI","SILK","SILV","SIMO","SINT","SIRE","SISI","SITM","SIXT","SKIN","SKWD","SLCA",
    "SLCR","SLDB","SLDP","SLGL","SLGN","SLNO","SLNX","SLQT","SMBC","SMBK","SMFL","SMID",
    "SMIT","SMPL","SMSI","SMTC","SNBR","SNCY","SNCR","SNEX","SNFCA","SNOA","SNPO","SNPS",
    "SNSE","SNSR","SNVX","SOFI","SOHO","SOLO","SOLY","SONX","SOPA","SOPH","SOTK","SOWG",
    "SPFI","SPGX","SPKE","SPLK","SPNE","SPNS","SPOK","SPPI","SPRO","SPRY","SPRX","SPSC",
    "SPTN","SPTY","SPWH","SPWR","SQFT","SQNS","SQSP","SRCE","SRCL","SRFM","SRGA","SRRK",
    "SRTS","SSBI","SSBK","SSFI","SSII","SSRM","SSSS","SSYS","STAA","STAG","STBA","STBZ",
    "STCN","STEP","STGW","STIM","STKS","STLA","STNE","STOK","STRA","STRS","STRT","STRW",
    "STSS","STVN","STXS","SUMO","SUPN","SURF","SURGN","SVRA","SWAG","SWAV","SWIM","SWKH",
    "SWKX","SWVL","SXTP","SYBT","SYBX","SYKE","SYRS","TACT","TALO","TALS","TANH","TASK",
    "TAST","TATT","TBCP","TBIO","TBNK","TBPH","TBRG","TCBK","TCBX","TCFC","TCMD","TCON",
    "TCPC","TDUP","TELL","TENB","TENX","TERN","TESS","TFFP","TFII","TFSL","TGLS","TGTX",
    "THCA","THCH","THFF","THRD","THRM","THTX","TILE","TLGA","TLRY","TMBR","TMDI","TMDX",
    "TMHC","TNXP","TORC","TPVG","TPVG","TRAK","TRAN","TRDA","TREE","TRGP","TRGT","TRIN",
    "TRMK","TRMT","TRNO","TRON","TROO","TROW","TRST","TRTN","TRTX","TRUP","TRVG","TRVN",
    "TSEM","TSHA","TSIO","TSLX","TTEC","TTEK","TTGT","TTMI","TTNP","TTSH","TTWO","TUSK",
    "TUYA","TVIA","TVTX","TWCT","TWKS","TWLO","TWIN","TWNI","TWNK","TWST","TXNM","TXRH",
    "TYGO","UAVS","UBCP","UBFO","UBOH","UBSI","UCBI","UCBR","UCTT","UFCS","UFPI","UFPT",
    "UGRO","UHAL","ULCC","ULTA","ULTI","UMBF","UMPQ","UNAM","UNFI","UNIT","UNTY","UONE",
    "UPLD","UPWK","URBN","URGN","USAC","USAK","USAP","USAT","USAU","USEI","USFD","USIG",
    "USIO","USLM","USNA","USNF","USPH","UTHR","UTMD","UTSI","UVSP","VBFC","VBIV","VBTX",
    "VCNX","VCTR","VCEL","VCNX","VCYT","VECO","VERA","VERB","VERX","VGFC","VGLT","VHAQ",
    "VIAV","VICR","VIEW","VIGL","VINC","VIOT","VIPS","VITL","VIVO","VKTX","VLCN","VLON",
    "VNDA","VNRX","VOXX","VRAY","VRCA","VRDN","VREX","VRNA","VRNS","VRNT","VRPX","VRSK",
    "VSCO","VSEC","VSET","VSTA","VTGN","VTOL","VTVT","VUZI","VVOS","VVPR","VXRT","VYNT",
    "WABC","WAFD","WASH","WATT","WAVE","WBHC","WBND","WCFB","WDFC","WERN","WETF","WEYS",
    "WFCF","WFRD","WHLM","WHLR","WINA","WING","WINT","WIRE","WKHS","WKME","WLDN","WLFC",
    "WLMS","WNEB","WOLF","WOOF","WORX","WPRT","WRBY","WSBC","WSFS","WTBA","WTFC","WTRG",
    "WULF","WVVI","XAIR","XBIO","XCUR","XELA","XELB","XENE","XFOR","XGEVA","XNCR","XOMA",
    "XPEL","XPER","XPOF","XRAY","XTLB","XTNT","XXII","XYLO","YCBD","YEXT","YMAB","YORW",
    "YOSH","YPFSX","YTEN","YUMC","ZETA","ZEUS","ZFOX","ZGNX","ZIXI","ZLAB","ZNTE","ZNTL",
    "ZROZ","ZSAN","ZTLK","ZVRA","ZYME","ZYXI",
]

UNIVERSE = list(set(SP500 + NASDAQ100 + EXTRAS + RUSSELL2000))

SECTOR_ETFS = {
    "Technology": "XLK", "Healthcare": "XLV", "Financials": "XLF",
    "Energy": "XLE", "Consumer Cyclical": "XLY", "Industrials": "XLI",
    "Communication Services": "XLC", "Consumer Defensive": "XLP",
    "Utilities": "XLU", "Real Estate": "XLRE", "Basic Materials": "XLB",
}

# Correlaciones conocidas entre activos
CORRELATIONS = {
    "NVDA": ["AMD","SMCI","AVGO","AMAT","LRCX","KLAC","MU"],
    "TSLA": ["RIVN","LCID","NIO","XPEV","LI"],
    "COIN": ["MSTR","MARA","RIOT","CLSK","IREN","BTBT","HUT"],
    "MSTR": ["COIN","MARA","RIOT","CLSK"],
    "AAPL": ["MSFT","GOOGL","META","AMZN"],
    "META": ["SNAP","PINS","GOOGL","RBLX"],
    "AMZN": ["SHOP","EBAY","ETSY"],
    "PLTR": ["BBAI","SOUN","AI"],
    "AMD":  ["NVDA","INTC","QCOM","AVGO"],
    "SMCI": ["NVDA","AMD","DELL","HPQ"],
}

# ═══════════════════════════════════════════════════════════════════════
# ESTADO GLOBAL
# ═══════════════════════════════════════════════════════════════════════

predictions    = []   # predicciones enriquecidas guardadas en disco
watch_signals  = {}   # {ticker: {"last_analyzed": ISO, "developing": bool}}
learnings      = {    # motor de aprendizaje
    "rules":          [],    # reglas aprendidas [{condition, win_rate, sample_size, description}]
    "sector_memory":  {},    # {sector: {win_rate, total, avg_conf}}
    "hour_memory":    {},    # {hour: {win_rate, total}}
    "error_memory":   [],    # [{indicator, description, count}]
    "regime_memory":  {},    # {regime: {win_rate, total}}
    "last_updated":   None,
}
market_context = {
    "fear_greed": 50, "sp500_change": 0.0, "vix": 15.0,
    "macro_news": [], "economic_events": [], "updated_at": None,
}
market_regime  = {    # detectado automáticamente
    "regime":       "UNKNOWN",   # BULL / BEAR / LATERAL
    "strength":     0,           # 0-100
    "description":  "",
    "updated_at":   None,
}
status_msg_id     = None
last_cmd_msg_id   = None
processed_cmd_ids = set()

# ═══════════════════════════════════════════════════════════════════════
# PERSISTENCIA EN DISCO
# ═══════════════════════════════════════════════════════════════════════

def load_state():
    """Carga todo el estado desde disco al arrancar."""
    global predictions, watch_signals, learnings, market_regime
    os.makedirs("/app/data", exist_ok=True)

    for var_name, filepath, default in [
        ("predictions",   PREDICTIONS_FILE, []),
        ("watch_signals", WATCHSTATE_FILE,  {}),
        ("learnings",     LEARNINGS_FILE,   learnings),
        ("market_regime", REGIME_FILE,      market_regime),
    ]:
        try:
            if os.path.exists(filepath):
                with open(filepath) as f:
                    data = json.load(f)
                if var_name == "predictions":   predictions   = data
                elif var_name == "watch_signals": watch_signals = data
                elif var_name == "learnings":   learnings     = data
                elif var_name == "market_regime": market_regime = data
            else:
                if var_name == "predictions":   predictions   = default
                elif var_name == "watch_signals": watch_signals = default
        except Exception as e:
            print(f"  ERROR cargando {filepath}: {e}")

    print(f"  Estado cargado: {len(predictions)} predicciones | universo: {len(UNIVERSE)} acciones")
    resolved = len([p for p in predictions if p.get("result") != "pending"])
    print(f"  Predicciones resueltas: {resolved} | reglas aprendidas: {len(learnings.get('rules', []))}")


def save_state():
    """Guarda todo el estado en disco."""
    for data, filepath in [
        (predictions,   PREDICTIONS_FILE),
        (watch_signals, WATCHSTATE_FILE),
        (learnings,     LEARNINGS_FILE),
        (market_regime, REGIME_FILE),
    ]:
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"  ERROR guardando {filepath}: {e}")


def save_prediction(ticker, signal, tech, conf):
    """
    Registra una predicción enriquecida con todos los indicadores.
    Estos datos son los que el motor de aprendizaje usará para detectar patrones.
    """
    predictions.append({
        # Identificación
        "ticker":     ticker,
        "signal":     signal,
        "confidence": conf,
        "date":       datetime.now(SPAIN_TZ).isoformat(),
        "result":     "pending",
        "exit_price": None,
        "days_to_result": None,

        # Precio
        "entry":      round(tech.get("price", 0), 2),
        "target":     round(tech.get("price", 0) * (1.15 if signal == "COMPRAR" else 0.85), 2),
        "stop":       round(tech.get("rl", tech.get("price", 0) * 0.93), 2),

        # Contexto técnico completo (para aprendizaje)
        "rsi":           tech.get("rsi"),
        "rsi_zone":      tech.get("rsi_zone"),
        "macd_bullish":  tech.get("macd_bullish"),
        "stoch_k":       tech.get("stoch_k"),
        "vol_ratio":     tech.get("vol_ratio"),
        "obv_trend":     tech.get("obv_trend"),
        "mom1m":         tech.get("mom1m"),
        "mom3m":         tech.get("mom3m"),
        "tf_confluence": tech.get("tf_confluence"),
        "structure":     tech.get("structure"),
        "tech_score":    tech.get("tech_score"),
        "support_touches": tech.get("support_touches"),
        "dist_h52":      tech.get("dist_h"),
        "dist_l52":      tech.get("dist_l"),

        # Contexto macro (para aprendizaje)
        "fear_greed":    market_context.get("fear_greed"),
        "vix":           market_context.get("vix"),
        "sp500_change":  market_context.get("sp500_change"),
        "regime":        market_regime.get("regime"),

        # Contexto de sesión (para aprendizaje)
        "sector":        tech.get("sector"),
        "hour":          datetime.now(SPAIN_TZ).hour,
        "session":       _session_label(datetime.now(SPAIN_TZ)),
        "day_of_week":   datetime.now(SPAIN_TZ).weekday(),
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
    return sum(1 for p in _preds_today() if p["confidence"] < CONF_EXCEPCIONAL)


def ventas_hoy():
    return sum(1 for p in _preds_today() if p["signal"] == "VENDER")


def puede_enviar_alerta(signal, conf):
    if conf >= CONF_EXCEPCIONAL:
        return True, None
    if alertas_hoy() >= MAX_ALERTAS_DIA:
        return False, f"límite diario ({MAX_ALERTAS_DIA}) alcanzado"
    if signal == "VENDER" and ventas_hoy() >= MAX_VENTAS_DIA:
        return False, "límite de ventas diario alcanzado"
    return True, None


def _session_label(now):
    m = now.hour * 60 + now.minute
    if  540 <= m <  570: return "PREMARKET"
    if  570 <= m < 1320: return "MERCADO"
    if 1320 <= m < 1440: return "AFTERHOURS"
    return "FUERA DE MERCADO"

# ═══════════════════════════════════════════════════════════════════════
# DISCORD — ENVÍO Y GESTIÓN DE MENSAJES
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
    """Edita el mensaje único de #status. Nunca crea duplicados."""
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
    """Borra mensajes anteriores del bot en #instrucciones y publica los nuevos."""
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
        print(f"  Error borrando instrucciones: {e}")

    resolved  = len([p for p in predictions if p.get("result") != "pending"])
    rules_cnt = len(learnings.get("rules", []))
    regime    = market_regime.get("regime", "UNKNOWN")

    _discord_post(DISCORD_INSTRUCCIONES_ID, f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━
📖  **CÓMO FUNCIONA STOCKBOT PRO v5**
━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍  **Análisis automático**
Vigila ~{len(UNIVERSE)} acciones cada 5 min.
Solo envía cuando hay convergencia real entre capas técnica, fundamental y macro.
Máximo 3 alertas al día.

🧠  **Aprendizaje autónomo**
Predicciones resueltas: {resolved} | Reglas aprendidas: {rules_cnt}
Régimen de mercado detectado: {regime}
El bot mejora sus análisis con cada predicción resuelta.

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
    """Actualiza Fear&Greed, S&P500, VIX y noticias macro."""
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

    # Actualizar régimen después de macro
    detect_market_regime()

# ═══════════════════════════════════════════════════════════════════════
# DETECCIÓN DE RÉGIMEN DE MERCADO
# Bull / Bear / Lateral — ajusta umbrales automáticamente
# ═══════════════════════════════════════════════════════════════════════

def detect_market_regime():
    """
    Detecta el régimen de mercado analizando SPY, QQQ y VIX.
    Guarda el resultado en market_regime y en disco.
    El régimen afecta los umbrales de confianza y la selectividad del bot.
    """
    global market_regime
    print("  Detectando régimen de mercado...")

    try:
        spy_closes, qqq_closes = [], []

        for sym, store in [("SPY", "spy"), ("QQQ", "qqq")]:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=3mo",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
            )
            if r.status_code == 200:
                closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if store == "spy": spy_closes = closes
                else:              qqq_closes = closes
            time.sleep(0.5)

        if not spy_closes or len(spy_closes) < 20:
            return

        price      = spy_closes[-1]
        sma20      = sum(spy_closes[-20:]) / 20
        sma50      = sum(spy_closes[-50:]) / 50 if len(spy_closes) >= 50 else sma20
        mom1m      = ((price - spy_closes[-22]) / spy_closes[-22] * 100) if len(spy_closes) >= 22 else 0
        mom3m      = ((price - spy_closes[-66]) / spy_closes[-66] * 100) if len(spy_closes) >= 66 else 0
        vix        = market_context.get("vix", 15)
        fg         = market_context.get("fear_greed", 50)

        # Puntuación de régimen
        bull_score = 0
        bear_score = 0

        if price > sma20:  bull_score += 2
        else:              bear_score += 2
        if price > sma50:  bull_score += 2
        else:              bear_score += 2
        if mom1m > 2:      bull_score += 2
        elif mom1m < -2:   bear_score += 2
        if mom3m > 5:      bull_score += 3
        elif mom3m < -5:   bear_score += 3
        if vix < 18:       bull_score += 2
        elif vix > 25:     bear_score += 2
        if fg > 60:        bull_score += 1
        elif fg < 30:      bear_score += 1

        total = bull_score + bear_score
        if total == 0:
            regime, strength, desc = "LATERAL", 50, "Mercado sin dirección clara"
        elif bull_score > bear_score * 1.5:
            strength = min(int((bull_score / total) * 100), 99)
            regime   = "BULL"
            desc     = f"Tendencia alcista confirmada | SPY {mom3m:+.1f}% en 3m | VIX {vix}"
        elif bear_score > bull_score * 1.5:
            strength = min(int((bear_score / total) * 100), 99)
            regime   = "BEAR"
            desc     = f"Tendencia bajista confirmada | SPY {mom3m:+.1f}% en 3m | VIX {vix}"
        else:
            strength = 50
            regime   = "LATERAL"
            desc     = f"Mercado sin dirección clara | SPY {mom3m:+.1f}% en 3m | VIX {vix}"

        prev_regime = market_regime.get("regime", "UNKNOWN")
        market_regime = {
            "regime":      regime,
            "strength":    strength,
            "description": desc,
            "spy_mom1m":   round(mom1m, 1),
            "spy_mom3m":   round(mom3m, 1),
            "vix":         vix,
            "updated_at":  datetime.now(SPAIN_TZ).isoformat(),
        }
        save_state()

        print(f"  Régimen: {regime} (fuerza {strength}%) — {desc}")

        if prev_regime != regime and prev_regime != "UNKNOWN":
            send_log(f"🔄 Cambio de régimen: {prev_regime} → {regime} | {desc}")

    except Exception as e:
        print(f"  detect_market_regime error: {e}")


def get_regime_conf_adjustment():
    """
    Ajusta la confianza mínima según el régimen de mercado.
    En BEAR: más estricto con compras (+3% confianza mínima)
    En BULL: más estricto con ventas (+3% confianza mínima)
    En LATERAL: estricto con ambos (+2%)
    """
    regime = market_regime.get("regime", "UNKNOWN")
    return {
        "BULL":    {"COMPRAR": 0,  "VENDER": 3},
        "BEAR":    {"COMPRAR": 3,  "VENDER": 0},
        "LATERAL": {"COMPRAR": 2,  "VENDER": 2},
    }.get(regime, {"COMPRAR": 0, "VENDER": 0})

# ═══════════════════════════════════════════════════════════════════════
# CAPA 1 — QUICK SCAN + CORRELACIONES + DETECCIÓN DE MANIPULACIÓN
# ═══════════════════════════════════════════════════════════════════════

def detect_manipulation(sym, change, vol_ratio, price):
    """
    Detecta señales de pump & dump o manipulación de precio.
    Devuelve (True, motivo) si es sospechoso, (False, None) si es limpio.

    Señales de alerta:
    - Subida >20% con volumen bajo (<1.5x)
    - Precio < $2 con volumen explosivo (típico penny stock pump)
    - Ticker desconocido con subida >30% sin noticias
    - Acción con capitalización muy baja y movimiento extremo
    """
    # Penny stocks con movimiento extremo
    if price < 2 and abs(change) > 20:
        return True, f"penny stock pump sospechoso (${price}, {change:+.1f}%)"

    # Subida explosiva con volumen bajo — posible manipulación
    if change > 20 and vol_ratio < 1.5:
        return True, f"subida extrema ({change:+.1f}%) con volumen bajo ({vol_ratio}x) — sospechoso"

    # Subida >35% en un día es casi siempre noticia específica o pump
    if abs(change) > 35 and vol_ratio < 2:
        return True, f"movimiento extremo ({change:+.1f}%) sin volumen institucional"

    return False, None


def quick_scan():
    """
    Escanea screeners de Yahoo Finance buscando candidatos reales.
    Incluye detección de correlaciones y filtro anti-manipulación.
    Devuelve lista de candidatos ordenados por urgencia.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://finance.yahoo.com",
    }
    seen          = set()
    candidates    = []
    corr_triggers = []   # tickers activados por correlación

    for url in [
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=most_actives&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=50",
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=50",
    ]:
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 429:
                print(f"  Quick scan: Yahoo 429 — esperando 5s")
                time.sleep(5)
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

                # Filtro anti-manipulación
                is_manip, manip_reason = detect_manipulation(sym, change, vol_ratio, price)
                if is_manip:
                    print(f"  Filtrado por manipulación: {sym} — {manip_reason}")
                    continue

                score = 0
                if abs(change) > 8:        score += 3
                elif abs(change) > 5:      score += 2
                elif abs(change) > 3:      score += 1
                if vol_ratio > 3:          score += 3
                elif vol_ratio > 2:        score += 2
                elif vol_ratio > 1.5:      score += 1

                if score >= 2:
                    seen.add(sym)
                    candidates.append({
                        "ticker":    sym,
                        "name":      q.get("longName", sym),
                        "sector":    q.get("sector", "Unknown"),
                        "price":     price,
                        "change":    change,
                        "vol_ratio": round(vol_ratio, 2),
                        "score":     score,
                        "source":    "screener",
                    })

                    # Correlaciones: si este ticker tiene correlacionados, añadirlos
                    if sym in CORRELATIONS:
                        for corr_ticker in CORRELATIONS[sym]:
                            if corr_ticker not in seen and not already_alerted_today(corr_ticker):
                                corr_triggers.append({
                                    "ticker":  corr_ticker,
                                    "name":    corr_ticker,
                                    "sector":  "Unknown",
                                    "price":   0,
                                    "change":  0,
                                    "vol_ratio": 0,
                                    "score":   2,
                                    "source":  f"correlación con {sym}",
                                })
                                seen.add(corr_ticker)

            time.sleep(0.5)
        except Exception as e:
            print(f"  Quick scan error: {e}")

    # Añadir en desarrollo
    developing = [
        {
            "ticker": t, "name": t, "sector": "Unknown",
            "price": 0, "change": 0, "vol_ratio": 0, "score": 1,
            "source": "developing",
        }
        for t, s in watch_signals.items()
        if s.get("developing") and not already_alerted_today(t) and t not in seen
    ]

    # Deduplicar correlaciones
    corr_unique = []
    for c in corr_triggers:
        if c["ticker"] not in {x["ticker"] for x in candidates + developing + corr_unique}:
            corr_unique.append(c)

    all_candidates = (
        sorted(candidates, key=lambda x: x["score"], reverse=True)
        + corr_unique[:4]
        + developing
    )

    n_corr = len(corr_unique)
    n_dev  = len(developing)
    print(f"  Quick scan: {len(candidates)} urgentes + {n_corr} correlaciones + {n_dev} en desarrollo")

    return all_candidates[:15]

# ═══════════════════════════════════════════════════════════════════════
# CAPA 2 — DATOS DE MERCADO (Yahoo Finance, 3 timeframes)
# Anti-429 mejorado: delays, backoff, rotación de hosts
# ═══════════════════════════════════════════════════════════════════════

def _yahoo_get(session, host, ticker, interval, range_, hdrs):
    """
    Petición a Yahoo Finance con manejo de 429.
    Devuelve (data, status_code).
    """
    try:
        r = session.get(
            f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={range_}",
            headers=hdrs, timeout=15,
        )
        if r.status_code == 429:
            print(f"    {ticker}: Yahoo 429 ({interval}) — esperando 5s")
            time.sleep(5)
            return None, 429
        if r.status_code != 200:
            return None, r.status_code
        return r.json(), 200
    except Exception as e:
        print(f"    {ticker}: Yahoo excepción ({interval}) — {e}")
        return None, 0


def get_market_data(ticker):
    """
    Datos técnicos completos para un ticker.
    3 peticiones: diario (1y), semanal (1y), mensual (3y).
    Anti-429: User-Agent aleatorio, host aleatorio, delays.
    """
    try:
        ua = random.choice([
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        ])
        hdrs = {
            "User-Agent": ua,
            "Accept":     "application/json",
            "Referer":    f"https://finance.yahoo.com/quote/{ticker}/",
        }
        host = random.choice(["query1", "query2"])
        s    = requests.Session()

        # Cookie inicial
        try:
            s.get(f"https://finance.yahoo.com/quote/{ticker}/", headers=hdrs, timeout=8)
            time.sleep(1.0)
        except: pass

        # ── Diario ────────────────────────────────────────────────────
        data, status = _yahoo_get(s, host, ticker, "1d", "1y", hdrs)
        if not data:
            if status != 429:
                print(f"    {ticker}: Yahoo HTTP {status}")
            return None

        chart = data.get("chart", {}).get("result", [])
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
            ema12 = closes[-(i+1)] * (2/13)  + ema12 * (11/13)
            ema26 = closes[-(i+1)] * (2/27)  + ema26 * (25/27)
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

        # VWAP
        vwap = sum(closes[-5:]) / 5

        # ATR
        atr_vals = [
            max(highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i-1]),
                abs(lows[-i]  - closes[-i-1]))
            for i in range(1, min(15, len(closes)))
        ]
        atr = sum(atr_vals) / len(atr_vals) if atr_vals else price * 0.02

        # Fibonacci
        h52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
        l52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
        rng = h52 - l52
        fib236 = round(h52 - rng * 0.236, 2)
        fib382 = round(h52 - rng * 0.382, 2)
        fib500 = round(h52 - rng * 0.500, 2)
        fib618 = round(h52 - rng * 0.618, 2)

        # Soporte y resistencia
        rh = max(highs[-20:]) if len(highs) >= 20 else price
        rl = min(lows[-20:])  if len(lows)  >= 20 else price
        support_touches = sum(1 for l in lows[-60:] if abs(l - rl) / rl < 0.02) if len(lows) >= 60 else 0

        # Momentum
        mom1m = ((price - closes[-22]) / closes[-22] * 100) if len(closes) >= 22 else 0
        mom3m = ((price - closes[-66]) / closes[-66] * 100) if len(closes) >= 66 else 0

        # Estructura
        rh10 = highs[-10:] if len(highs) >= 10 else highs
        rl10 = lows[-10:]  if len(lows)  >= 10 else lows
        hh = all(rh10[i] >= rh10[i-1] for i in range(1, len(rh10)))
        hl = all(rl10[i] >= rl10[i-1] for i in range(1, len(rl10)))
        lh = all(rh10[i] <= rh10[i-1] for i in range(1, len(rh10)))
        ll = all(rl10[i] <= rl10[i-1] for i in range(1, len(rl10)))
        if hh and hl:   structure = "TENDENCIA ALCISTA CLARA"
        elif lh and ll: structure = "TENDENCIA BAJISTA CLARA"
        else:           structure = "LATERAL / CONSOLIDACION"

        # Score técnico
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

        # ── Semanal ───────────────────────────────────────────────────
        weekly_trend = "N/D"
        time.sleep(0.8)
        data_w, _ = _yahoo_get(s, host, ticker, "1wk", "1y", hdrs)
        if data_w:
            try:
                wc = [c for c in data_w["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if len(wc) >= 10:
                    weekly_trend = "ALCISTA" if wc[-1] > sum(wc[-10:]) / 10 else "BAJISTA"
            except: pass

        # ── Mensual ───────────────────────────────────────────────────
        monthly_trend = "N/D"
        time.sleep(0.8)
        data_m, _ = _yahoo_get(s, host, ticker, "1mo", "3y", hdrs)
        if data_m:
            try:
                mc = [c for c in data_m["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
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
# CAPA 3 — FUNDAMENTALES
# ═══════════════════════════════════════════════════════════════════════

def get_fundamentals(ticker):
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
        if r.status_code == 429:
            time.sleep(5)
            return result
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
# CAPA 4 — SENTIMIENTO
# ═══════════════════════════════════════════════════════════════════════

def get_sentiment(ticker, sector):
    news_items      = []
    sentiment_score = 0
    positive_words  = ["beat","surge","jump","upgrade","buy","strong","growth","record","partnership","contract","raised","guidance"]
    negative_words  = ["miss","fall","drop","downgrade","sell","weak","loss","cut","investigation","lawsuit","recall","fraud"]

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
# CAPA 5 — SEÑAL INSTITUCIONAL
# ═══════════════════════════════════════════════════════════════════════

def get_inst_signal(tech):
    """
    Infiere actividad institucional desde volumen, precio y OBV.
    Boost aplicado ANTES del formateo para confianza coherente.
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

    if obv_trend == "ACUMULACION"  and change_pct > 0: boost += 2
    elif obv_trend == "DISTRIBUCION" and change_pct < 0: boost += 2

    return signal, boost

# ═══════════════════════════════════════════════════════════════════════
# MOTOR DE APRENDIZAJE AUTÓNOMO — 5 NIVELES
# Activo desde predicción 20 resuelta
# ═══════════════════════════════════════════════════════════════════════

def _resolved_predictions():
    """Predicciones con resultado conocido (win o loss)."""
    return [p for p in predictions if p.get("result") in ("win", "loss")]


def _update_learning_level1():
    """
    Nivel 1 — Reglas simples por indicador.
    Ejemplo: RSI < 30 + volumen > 2x → win_rate X%
    Activo desde predicción 20.
    """
    resolved = _resolved_predictions()
    if len(resolved) < LEARN_MIN_PREDS:
        return

    rules = []

    # Combinaciones a analizar
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
    ]

    for name, cond_fn in conditions:
        matching = [p for p in resolved if cond_fn(p)]
        if len(matching) < 5:
            continue
        wins     = sum(1 for p in matching if p["result"] == "win")
        win_rate = round(wins / len(matching) * 100, 1)
        rules.append({
            "condition":   name,
            "win_rate":    win_rate,
            "sample_size": len(matching),
            "description": f"{name}: {win_rate}% acierto en {len(matching)} casos",
        })

    learnings["rules"] = sorted(rules, key=lambda x: abs(x["win_rate"] - 50), reverse=True)


def _update_learning_level2():
    """
    Nivel 2 — Ajuste por régimen de mercado.
    Activo desde predicción 20.
    """
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
        regime_memory[regime] = {
            "win_rate": win_rate,
            "total":    len(subset),
            "buys":     sum(1 for p in subset if p.get("signal") == "COMPRAR"),
            "sells":    sum(1 for p in subset if p.get("signal") == "VENDER"),
        }

    learnings["regime_memory"] = regime_memory


def _update_learning_level3():
    """
    Nivel 3 — Memoria por sector.
    Activo desde predicción 40.
    """
    resolved = _resolved_predictions()
    if len(resolved) < LEARN_MIN_L3:
        return

    sector_memory = {}
    sectors = list(set(p.get("sector", "Unknown") for p in resolved))
    for sector in sectors:
        subset = [p for p in resolved if p.get("sector") == sector]
        if len(subset) < 4:
            continue
        wins     = sum(1 for p in subset if p["result"] == "win")
        win_rate = round(wins / len(subset) * 100, 1)
        avg_conf = round(sum(p.get("confidence", 85) for p in subset) / len(subset), 1)
        sector_memory[sector] = {
            "win_rate": win_rate,
            "total":    len(subset),
            "avg_conf": avg_conf,
        }

    learnings["sector_memory"] = sector_memory


def _update_learning_level4():
    """
    Nivel 4 — Memoria por hora y sesión.
    Activo desde predicción 60.
    """
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
    """
    Nivel 5 — Autopuntuación: la IA analiza sus propios errores.
    Activo desde predicción 80.
    Llama a la IA con los últimos 10 fallos para extraer lecciones.
    """
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
        f"| Estructura:{p.get('structure','?')} | TF:{p.get('tf_confluence','?')}"
        for p in recent_losses
    ])

    prompt = f"""Eres un analista cuantitativo analizando los fallos de un bot de trading.
Aquí están los últimos {len(recent_losses)} fallos:

{losses_txt}

Analiza los patrones comunes de estos fallos. ¿Qué indicadores o combinaciones
están correlacionando con los errores? Sé muy específico y conciso.

Responde SOLO con este formato JSON (sin markdown, sin explicaciones):
[
  {{"indicator": "nombre_indicador", "description": "descripción del problema en 10 palabras", "count": N}},
  ...
]
Máximo 5 entradas. Solo patrones que aparezcan en 3+ fallos."""

    try:
        result = call_ai(prompt, max_tokens=300)
        if result:
            # Limpiar posible markdown
            clean = result.strip().replace("```json", "").replace("```", "").strip()
            errors = json.loads(clean)
            if isinstance(errors, list):
                learnings["error_memory"] = errors[:5]
                print(f"  Nivel 5: {len(errors)} patrones de error detectados")
    except Exception as e:
        print(f"  Nivel 5 error: {e}")


def run_learning_engine():
    """
    Ejecuta todos los niveles del motor de aprendizaje.
    Se llama cada domingo junto al resumen semanal.
    """
    resolved = _resolved_predictions()
    print(f"  Motor de aprendizaje: {len(resolved)} predicciones resueltas")

    if len(resolved) < LEARN_MIN_PREDS:
        print(f"  Aprendizaje inactivo — necesita {LEARN_MIN_PREDS} predicciones (tiene {len(resolved)})")
        return

    _update_learning_level1()
    _update_learning_level2()
    if len(resolved) >= LEARN_MIN_L3:  _update_learning_level3()
    if len(resolved) >= LEARN_MIN_L4:  _update_learning_level4()
    if len(resolved) >= LEARN_MIN_L5:  _update_learning_level5()

    learnings["last_updated"] = datetime.now(SPAIN_TZ).isoformat()
    save_state()

    rules_cnt  = len(learnings.get("rules", []))
    sector_cnt = len(learnings.get("sector_memory", {}))
    error_cnt  = len(learnings.get("error_memory", []))
    print(f"  Aprendizaje actualizado: {rules_cnt} reglas | {sector_cnt} sectores | {error_cnt} patrones de error")
    send_log(
        f"🧠 Motor de aprendizaje actualizado\n"
        f"Predicciones resueltas: {len(resolved)} | Reglas: {rules_cnt} | Sectores: {sector_cnt} | Errores detectados: {error_cnt}"
    )


def update_prediction_results():
    """
    Comprueba si alguna predicción pendiente ha llegado a objetivo o stop.
    Se llama cada domingo antes del resumen.
    """
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
                closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0].get("close", []) if c]
                if not closes:
                    continue
                current    = closes[-1]
                date_pred  = datetime.fromisoformat(p["date"])
                days_since = (datetime.now() - date_pred).days

                hit_target = (signal == "COMPRAR" and current >= target) or (signal == "VENDER" and current <= target)
                hit_stop   = (signal == "COMPRAR" and current <= stop)   or (signal == "VENDER" and current >= stop)
                expired    = days_since > 30

                if hit_target:
                    p["result"]        = "win"
                    p["exit_price"]    = round(current, 2)
                    p["days_to_result"]= days_since
                elif hit_stop or expired:
                    p["result"]        = "loss"
                    p["exit_price"]    = round(current, 2)
                    p["days_to_result"]= days_since
        except: pass
        time.sleep(0.3)

    save_state()

# ═══════════════════════════════════════════════════════════════════════
# CAPA 6 — IA (Claude Sonnet) CON PROMPTS ADAPTATIVOS
# ═══════════════════════════════════════════════════════════════════════

def call_ai(prompt, max_tokens=700):
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


def _build_learning_context():
    """
    Construye el bloque de conocimiento aprendido para inyectar en el prompt.
    Solo incluye lo relevante según el número de predicciones resueltas.
    """
    resolved_count = len(_resolved_predictions())
    if resolved_count < LEARN_MIN_PREDS:
        return ""

    lines = ["\nCONOCIMIENTO HISTÓRICO DEL BOT (aprendido de predicciones reales):"]

    # Nivel 1 — reglas con alta significancia
    top_rules = [r for r in learnings.get("rules", []) if r["sample_size"] >= 5][:5]
    if top_rules:
        lines.append("Patrones con mejor/peor rendimiento histórico:")
        for r in top_rules:
            perf = "✅ FIABLE" if r["win_rate"] >= 65 else "⚠️ POCO FIABLE" if r["win_rate"] <= 40 else "~neutro"
            lines.append(f"  {perf}: {r['description']}")

    # Nivel 2 — régimen actual
    regime      = market_regime.get("regime", "UNKNOWN")
    reg_memory  = learnings.get("regime_memory", {}).get(regime, {})
    if reg_memory:
        lines.append(f"En régimen {regime}: win_rate histórico {reg_memory['win_rate']}% ({reg_memory['total']} casos)")

    # Nivel 3 — sector (activo desde 40 predicciones)
    if resolved_count >= LEARN_MIN_L3:
        sector_memory = learnings.get("sector_memory", {})
        if sector_memory:
            worst = [s for s, d in sector_memory.items() if d["win_rate"] < 45]
            if worst:
                lines.append(f"Sectores con bajo rendimiento histórico: {', '.join(worst)} — ser más estricto")

    # Nivel 4 — hora actual (activo desde 60 predicciones)
    if resolved_count >= LEARN_MIN_L4:
        current_hour = str(datetime.now(SPAIN_TZ).hour)
        hour_data    = learnings.get("hour_memory", {}).get(current_hour, {})
        if hour_data:
            lines.append(f"A las {current_hour}h: win_rate histórico {hour_data['win_rate']}% ({hour_data['total']} casos)")

    # Nivel 5 — patrones de error (activo desde 80 predicciones)
    if resolved_count >= LEARN_MIN_L5:
        errors = learnings.get("error_memory", [])
        if errors:
            lines.append("Indicadores que han engañado al bot anteriormente:")
            for e in errors[:3]:
                lines.append(f"  ⚠️ {e['indicator']}: {e['description']}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_auto_prompt(tech, fund, sent, inst_signal, conf_boost):
    fg     = market_context["fear_greed"]
    sp500  = market_context["sp500_change"]
    vix    = market_context["vix"]
    fg_str = _fg_label(fg)
    regime = market_regime.get("regime", "UNKNOWN")
    regime_desc = market_regime.get("description", "")

    news_txt  = "\n".join(f"- {h}" for h in sent["news"][:5]) or "- Sin noticias"
    macro_txt = "\n".join(f"- {h}" for h in market_context.get("macro_news", [])[:4]) or "- Sin noticias macro"
    econ_txt  = "\n".join(f"- {h}" for h in market_context.get("economic_events", [])[:3]) or "- Sin eventos"

    rec_map  = {"strongBuy":"COMPRA FUERTE","buy":"COMPRAR","hold":"MANTENER","sell":"VENDER","strongSell":"VENTA FUERTE"}
    rec_txt  = rec_map.get(fund.get("rec_key","hold"), "MANTENER")
    tgt_txt  = (f"Precio objetivo analistas: ${fund['analyst_target']} ({fund['analyst_upside']:+.1f}% upside)"
                if fund.get("analyst_target") else "Sin precio objetivo disponible")
    earn_txt = (f"EARNINGS EN {fund['earnings_days']} DÍAS — {fund.get('earnings_beats',0)}/4 últimos beats"
                if fund.get("earnings_days") is not None else "Sin earnings próximos")

    # Ajuste de confianza por régimen
    regime_adj  = get_regime_conf_adjustment()
    conf_minimo = CONF_NORMAL + regime_adj.get(
        "COMPRAR" if tech.get("change_pct", 0) >= 0 else "VENDER", 0
    )

    # Contexto aprendido
    learning_ctx = _build_learning_context()

    return f"""Eres el mejor analista cuantitativo del mundo. Tu misión es encontrar las pocas oportunidades REALES del mercado.

REGLA CRÍTICA: Solo emites señal cuando hay CONVERGENCIA entre al menos 4 de estas 5 capas:
  1. Técnico (RSI, MACD, volumen, estructura)
  2. Timeframes (diario + semanal + mensual alineados)
  3. Fundamental (valoración, analistas, insiders)
  4. Sentimiento (noticias, contexto macro)
  5. Institucional (volumen anómalo, OBV)
Si no hay convergencia real en 4 capas → NO_SIGNAL obligatorio.
Prefiere NO_SIGNAL a una señal mediocre. La calidad importa más que la cantidad.

RÉGIMEN DE MERCADO ACTUAL: {regime} — {regime_desc}
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
{learning_ctx}

INSTRUCCIONES
Confianza mínima aceptable: {conf_minimo}% (ajustado por régimen {regime}).
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
    fg     = market_context["fear_greed"]
    fg_str = _fg_label(fg)
    regime = market_regime.get("regime", "UNKNOWN")
    learning_ctx = _build_learning_context()

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
Fear&Greed: {fg}/100 ({fg_str}) | VIX: {market_context['vix']} | Régimen: {regime}
P/E: {fund.get('pe_ratio','N/D')} | Short: {fund.get('short_interest','N/D')}% | Analistas: {fund.get('rec_key','N/D')}
Sentimiento noticias: {sent['sentiment_label']}
{learning_ctx}

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
    signal = "COMPRAR"
    for line in ai_response.splitlines():
        if line.startswith("SEÑAL:"):
            signal = "VENDER" if "VENDER" in line else "COMPRAR"
            break

    is_buy = signal == "COMPRAR"
    if conf_final >= CONF_EXCEPCIONAL:
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
    now     = datetime.now(SPAIN_TZ).strftime("%H:%M  %d/%m/%Y")

    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji}  **{signal}  —  {tech['ticker']}**{sess}{reg_tag}\n"
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
# ANÁLISIS COMPLETO — automático y manual
# ═══════════════════════════════════════════════════════════════════════

def analyze_ticker(ticker, name="", sector="Unknown", force=False, solo_excepcionales=False):
    """
    Análisis completo con las 6 capas + aprendizaje.

    force=False (automático):
        - Filtro de score mínimo
        - Boost institucional antes del formateo
        - Límites diarios y de régimen
        - Rechaza NO_SIGNAL

    force=True (manual, !analizar):
        - Sin filtros
        - Siempre devuelve resultado aunque sea NEUTRAL
    """
    print(f"  Analizando {ticker}...")

    tech = get_market_data(ticker)
    if not tech:
        print(f"    {ticker}: sin datos de mercado")
        return None

    if not force and tech["tech_score"] < SCORE_MINIMO:
        print(f"    {ticker}: score {tech['tech_score']} insuficiente (mín {SCORE_MINIMO})")
        return None

    fund               = get_fundamentals(ticker)
    sent               = get_sentiment(ticker, sector or tech["sector"])
    inst_signal, boost = get_inst_signal(tech)

    prompt      = _build_manual_prompt(tech, fund, sent) if force else _build_auto_prompt(tech, fund, sent, inst_signal, boost)
    ai_response = call_ai(prompt, max_tokens=650 if force else 800)
    if not ai_response:
        return None

    if not force and "NO_SIGNAL" in ai_response:
        watch_signals[ticker] = {"last_analyzed": datetime.now().isoformat(), "developing": False}
        save_state()
        return None

    # Extraer confianza
    conf_ia = 0
    for line in ai_response.splitlines():
        if "CONFIANZA:" in line:
            digits = "".join(c for c in line if c.isdigit())
            if digits:
                conf_ia = int(digits[:3])
            break

    # Boost institucional ANTES del formateo
    conf_final = min(conf_ia + boost, 99) if not force else conf_ia

    # Controles solo en automático
    if not force:
        if conf_final < CONF_NORMAL:
            print(f"    {ticker}: confianza {conf_final}% insuficiente (mín {CONF_NORMAL}%)")
            return None

        if solo_excepcionales and conf_final < CONF_EXCEPCIONAL:
            print(f"    {ticker}: límite normal alcanzado, descartado (no es Excepcional)")
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
            print(f"    {ticker}: confianza insuficiente para régimen {market_regime.get('regime')} ({conf_final}% < {CONF_NORMAL + extra_conf}%)")
            return None

        puede, motivo = puede_enviar_alerta(signal_check, conf_final)
        if not puede:
            print(f"    {ticker}: {motivo}")
            return None

    session_tag = _session_label(datetime.now(SPAIN_TZ))
    text, signal = format_alert(tech, ai_response, conf_final, session_tag)

    watch_signals[ticker] = {
        "last_analyzed": datetime.now().isoformat(),
        "developing":    conf_final >= CONF_FUERTE,
    }
    save_state()

    nivel = "EXCEPCIONAL ⚡" if conf_final >= CONF_EXCEPCIONAL else "FUERTE 🔥" if conf_final >= CONF_FUERTE else "NORMAL 🟢"
    print(f"    {ticker}: {nivel} {signal} {conf_final}%")

    return text, signal, conf_final, tech

# ═══════════════════════════════════════════════════════════════════════
# CICLO AUTOMÁTICO — cada 5 minutos
# ═══════════════════════════════════════════════════════════════════════

def watch_cycle():
    now = datetime.now(SPAIN_TZ)
    if now.hour < 9 or now.hour >= 23:
        return

    solo_excepcionales = alertas_hoy() >= MAX_ALERTAS_DIA

    candidates = quick_scan()
    if not candidates:
        return

    not_analyzed = [
        t for t in UNIVERSE
        if not already_alerted_today(t)
        and (t not in watch_signals
             or (datetime.now() - datetime.fromisoformat(
                 watch_signals[t].get("last_analyzed", "2000-01-01")
             )).total_seconds() > 86400)
    ]
    rotation = random.sample(not_analyzed, min(4, len(not_analyzed)))
    seen_set  = {c["ticker"] for c in candidates}
    rotation_items = [
        {"ticker": t, "name": t, "sector": "Unknown", "score": 0, "source": "rotation"}
        for t in rotation if t not in seen_set
    ]

    to_analyze = candidates + rotation_items
    regime     = market_regime.get("regime", "?")
    print(f"\n[{now.strftime('%H:%M')} ES] {len(to_analyze)} candidatos | alertas hoy: {alertas_hoy()}/{MAX_ALERTAS_DIA} | régimen: {regime}")

    alerts_this_cycle = 0

    for item in to_analyze:
        if alerts_this_cycle >= MAX_AI_POR_CICLO:
            break

        ticker = item["ticker"]
        if already_alerted_today(ticker):
            continue

        last = watch_signals.get(ticker, {}).get("last_analyzed")
        if last:
            elapsed = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < 3600:
                continue

        source = item.get("source", "")
        if source.startswith("correlación"):
            print(f"  Analizando {ticker} ({source})")

        result = analyze_ticker(
            ticker,
            item.get("name", ticker),
            item.get("sector", "Unknown"),
            solo_excepcionales=solo_excepcionales,
        )
        if not result:
            time.sleep(2)
            continue

        text, signal, conf, tech = result
        send_alert(text)
        save_prediction(ticker, signal, tech, conf)
        alerts_this_cycle += 1

        nivel = "EXCEPCIONAL ⚡" if conf >= CONF_EXCEPCIONAL else "FUERTE 🔥" if conf >= CONF_FUERTE else "NORMAL 🟢"
        print(f"    → Alerta enviada: {ticker} {nivel} ({signal}, {conf}%)")
        time.sleep(4)

    if alerts_this_cycle > 0:
        print(f"  {alerts_this_cycle} alerta(s) enviada(s) este ciclo")

# ═══════════════════════════════════════════════════════════════════════
# COMANDOS MANUALES — !analizar cada 30 segundos
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

                fg_loop = market_context["fear_greed"]
                update_status(
                    f"🟢  **Activo** — vigilando mercado\n"
                    f"📡 Fear&Greed: {fg_loop} ({_fg_label(fg_loop)}) | VIX: {market_context['vix']} | Régimen: {market_regime.get('regime','?')}\n"
                    f"🕐  Última actualización: {now_es.strftime('%H:%M')} — si esto no cambia en 10 min el bot está caído"
                )
                time.sleep(2)

    except Exception as e:
        import traceback
        print(f"  listen_commands excepción: {e}")
        print(traceback.format_exc())

# ═══════════════════════════════════════════════════════════════════════
# RESUMEN DOMINICAL — actualiza resultados + aprende + publica
# ═══════════════════════════════════════════════════════════════════════

def weekly_report():
    now = datetime.now(SPAIN_TZ)

    # 1. Actualizar resultados pendientes
    update_prediction_results()

    # 2. Ejecutar motor de aprendizaje
    run_learning_engine()

    # 3. Publicar resumen
    wins    = [p for p in predictions if p.get("result") == "win"]
    losses  = [p for p in predictions if p.get("result") == "loss"]
    pending = [p for p in predictions if p.get("result") == "pending"]

    # Solo la semana pasada
    week_ago = now - timedelta(days=7)
    wins_w    = [p for p in wins    if datetime.fromisoformat(p["date"]) >= week_ago]
    losses_w  = [p for p in losses  if datetime.fromisoformat(p["date"]) >= week_ago]
    pending_w = [p for p in pending if datetime.fromisoformat(p["date"]) >= week_ago]

    total    = len(wins_w) + len(losses_w)
    win_rate = round(len(wins_w) / total * 100) if total > 0 else 0
    avg_win  = round(sum(((p["exit_price"] - p["entry"]) / p["entry"] * 100) for p in wins_w)   / len(wins_w),   1) if wins_w   else 0
    avg_loss = round(sum(((p["exit_price"] - p["entry"]) / p["entry"] * 100) for p in losses_w) / len(losses_w), 1) if losses_w else 0

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊  **RESUMEN SEMANAL**",
        f"Semana del {(now - timedelta(days=7)).strftime('%d/%m')} al {now.strftime('%d/%m/%Y')}",
        f"Régimen de mercado: {market_regime.get('regime','?')}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for w in sorted(wins_w, key=lambda x: x.get("exit_price", 0) - x.get("entry", 0), reverse=True):
        chg = round(((w["exit_price"] - w["entry"]) / w["entry"]) * 100, 1) if w.get("exit_price") else 0
        lines.append(f"✅  **{w['ticker']}**  {chg:+.1f}%  en {w.get('days_to_result','?')} días")
    for l in sorted(losses_w, key=lambda x: x.get("exit_price", 0) - x.get("entry", 0)):
        chg = round(((l["exit_price"] - l["entry"]) / l["entry"]) * 100, 1) if l.get("exit_price") else 0
        lines.append(f"❌  **{l['ticker']}**  {chg:+.1f}%  stop en {l.get('days_to_result','?')} días")
    for p in pending_w:
        lines.append(f"⏳  **{p['ticker']}**  pendiente")
    if not wins_w and not losses_w and not pending_w:
        lines.append("Sin predicciones esta semana")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯  Aciertos: {len(wins_w)}/{total}  —  {win_rate}%",
    ]
    if wins_w:   lines.append(f"💰  Ganancia media: {avg_win:+.1f}%")
    if losses_w: lines.append(f"📉  Pérdida media: {avg_loss:+.1f}%")

    # Añadir reglas aprendidas si las hay
    top_rules = [r for r in learnings.get("rules", []) if r["sample_size"] >= 5][:3]
    if top_rules:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("🧠  **PATRONES APRENDIDOS**")
        for r in top_rules:
            icon = "✅" if r["win_rate"] >= 65 else "⚠️"
            lines.append(f"{icon}  {r['description']}")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    send_acierto("\n".join(lines))
    send_log(f"📊 Resumen dominical: {len(wins_w)} aciertos / {len(losses_w)} stops / {len(pending_w)} pendientes | Reglas aprendidas: {len(learnings.get('rules',[]))}")


def weekly_summary():
    now     = datetime.now(SPAIN_TZ)
    total   = len([p for p in predictions if p.get("result") != "pending"])
    wins    = len([p for p in predictions if p.get("result") == "win"])
    losses  = len([p for p in predictions if p.get("result") == "loss"])
    pending = len([p for p in predictions if p.get("result") == "pending"])
    rate    = round(wins / total * 100, 1) if total > 0 else 0
    regime  = market_regime.get("regime", "?")
    rules   = len(learnings.get("rules", []))
    send_log(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 RESUMEN — {now.strftime('%d/%m/%Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Acertadas: {wins}  ❌ Falladas: {losses}  ⏳ Pendientes: {pending}\n"
        f"🎯 Tasa de acierto: {rate}%\n"
        f"🧠 Reglas aprendidas: {rules} | Régimen: {regime}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════

def main():
    now = datetime.now(SPAIN_TZ)
    print(f"StockBot Pro v5 — {now.strftime('%H:%M %d/%m/%Y')}")

    load_state()
    update_status(f"⚙️  **Arrancando v5...**\n🕐  {now.strftime('%H:%M  %d/%m/%Y')}")
    update_market_context()   # incluye detect_market_regime()

    fg      = market_context["fear_greed"]
    fg_str  = _fg_label(fg)
    sp500   = market_context["sp500_change"]
    vix     = market_context["vix"]
    regime  = market_regime.get("regime", "?")
    rules   = len(learnings.get("rules", []))
    total_h = alertas_hoy()

    send_log(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 **StockBot v5 arrancado** — {now.strftime('%H:%M %d/%m/%Y')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 Fear&Greed: {fg}/100 ({fg_str})\n"
        f"📈 S&P500: {sp500:+.2f}%  |  VIX: {vix}\n"
        f"🔄 Régimen: {regime} | Universo: {len(UNIVERSE)} acciones\n"
        f"🧠 Reglas aprendidas: {rules}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Alertas hoy: {total_h}/{MAX_ALERTAS_DIA}\n"
        f"🟢 Vigilancia activa cada 5 min"
    )

    post_instrucciones()
    print("  Instrucciones publicadas")

    update_status(
        f"🟢  **Activo v5** — vigilando mercado\n"
        f"📡 Fear&Greed: {fg} ({fg_str}) | VIX: {vix} | Régimen: {regime}\n"
        f"🕐  {now.strftime('%H:%M  %d/%m/%Y')}"
    )

    watch_cycle()
    listen_commands(init=True)

    schedule.every(5).minutes.do(watch_cycle)
    schedule.every().day.at("09:00").do(update_market_context)
    schedule.every().day.at("00:01").do(lambda: send_log("🔄 Nuevo día — límites reseteados"))
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
                f"🟢  **Activo v5** — vigilando mercado\n"
                f"📡 Fear&Greed: {fg_loop} ({_fg_label(fg_loop)}) | VIX: {market_context['vix']} | Régimen: {market_regime.get('regime','?')}\n"
                f"🕐  Última actualización: {now_loop.strftime('%H:%M')} — si esto no cambia en 10 min el bot está caído"
            )
            last_status_check = ts

        time.sleep(5)


if __name__ == "__main__":
    main()
