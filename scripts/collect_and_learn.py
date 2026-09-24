import asyncio, json, os, re, math, unicodedata
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from smart_model import load_params, save_params, trend_slope
from event_peak_model import default_peak_learning, default_peak_events, update_peak_learning, build_unified_curve, _curve_value
from recession_model import build_recession_projection

WS_URL='wss://monitoramento.defesacivil.sc.gov.br/graphql'
CLIENT='secretaria-de-defesa-civil'
RIVER_URL='https://defesacivil.blumenau.sc.gov.br/static/data/nivel_oficial.json'
DATA_DIR=os.path.join(os.path.dirname(__file__),'..','data')
PARAMS=os.path.join(DATA_DIR,'learning.json')
SNAP=os.path.join(DATA_DIR,'learning_snapshots.json')
HIST=os.path.join(DATA_DIR,'river_history.json')
DAM_HIST=os.path.join(DATA_DIR,'dam_history.json')
FLOOD_EVENTS=os.path.join(DATA_DIR,'flood_events.json')
RAIN_RESPONSE=os.path.join(DATA_DIR,'rain_response.json')
PEAK_LEARNING=os.path.join(DATA_DIR,'peak_learning.json')
PEAK_EVENTS=os.path.join(DATA_DIR,'peak_events.json')

ALTO=['Agrolandia','Agronomica','Atalanta','Aurora','Braco do Trombudo','Chapadao do Lageado','Dona Emma','Ibirama','Imbuia','Ituporanga','Jose Boiteux','Laurentino','Lontras','Mirim Doce','Petrolandia','Pouso Redondo','Presidente Getulio','Presidente Nereu','Rio do Campo','Rio do Oeste','Rio do Sul','Salete','Santa Terezinha','Taio','Trombudo Central','Vidal Ramos','Vitor Meireles','Witmarsum']
MEDIO=['Apiuna','Ascurra','Benedito Novo','Blumenau','Botuvera','Brusque','Doutor Pedrinho','Gaspar','Guabiruba','Indaial','Pomerode','Rio dos Cedros','Rodeio','Timbo']
CONTRIB=ALTO+['Apiuna','Ascurra','Benedito Novo','Doutor Pedrinho','Indaial','Pomerode','Rio dos Cedros','Rodeio','Timbo']
ALL_CITIES=ALTO+MEDIO
CITY_PATTERNS=[(c,re.compile(r'(?:^|[^a-z])'+re.escape(''.join(ch for ch in unicodedata.normalize('NFD',c).lower() if unicodedata.category(ch)!='Mn'))+r'(?:$|[^a-z])',re.I)) for c in ALL_CITIES]

QUERY='''query Tags_data {
  tags_data(clients: ["%s"]) {
    qualle_meteorologia {
      codigo
      name { prefix general local }
      show
      timestamp
      position { bacia latitude longitude regiao altitude }
      data {
        rio { rio_nome { value } rio_nivel { value show { value } format { value } unit { value } } rio_nivel_tendencia { value show { value } } }
        chuva { acumulado {
          min005 { value show { value } format { value } unit { value } }
          h003 { value show { value } unit { value } }
          h006 { value show { value } unit { value } }
          h012 { value show { value } unit { value } }
          h024 { value show { value } unit { value } }
          h048 { value show { value } unit { value } }
          h096 { value show { value } unit { value } }
        }}
        barramento {
          nivel { percentual { value show { value } } montante { value show { value } } jusante { value show { value } } vertido { value show { value } } }
          capacidade { atual { value show { value } } maxima { value show { value } } }
          comportas {
            comporta_1 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_2 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_3 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_4 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_5 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_6 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_7 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_8 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_9 { estado { value } habilitada { value } nome { value show { value } } }
            comporta_10 { estado { value } habilitada { value } nome { value show { value } } }
          }
        }
      }
      filter { relacao { tem_chuva_acumulada tem_nivel_do_rio tem_barragem } }
    }
  }
}''' % CLIENT


def norm(v):
    return ''.join(ch for ch in unicodedata.normalize('NFD',str(v or '')).lower() if unicodedata.category(ch)!='Mn')

def unwrap(v):
    """Extrai o valor real do GraphQL sem confundir show.value com o dado.

    Na rede atual da Defesa Civil, um acumulado chega tipicamente como:
    {"value": 0.4, "show": {"value": true, "unit": null}}
    Portanto, ``value`` é o dado e ``show.value`` é apenas a flag de
    exibição. Só usamos show.value como fallback quando ele não for booleano.
    """
    seen=set()
    cur=v
    for _ in range(8):
        if id(cur) in seen: return None
        seen.add(id(cur))
        if cur is None: return None
        if isinstance(cur,(int,float,bool)): return cur
        if isinstance(cur,str): return cur
        if isinstance(cur,dict):
            # Prioridade absoluta ao campo numérico/real ``value``.
            if 'value' in cur:
                cur=cur['value']; continue
            # Em algumas respostas só existe show.value. Nunca trate a
            # flag booleana true/false como milímetros.
            show=cur.get('show')
            if isinstance(show,dict) and 'value' in show:
                candidate=show['value']
                if isinstance(candidate,bool): return None
                cur=candidate; continue
            return None
        return None
    return None

def val(x):
    x=unwrap(x)
    if x is None or x is True or x is False: return None
    if isinstance(x,(int,float)):
        return float(x) if math.isfinite(float(x)) else None
    s=str(x).strip().replace('\xa0',' ')
    if not s:return None
    # O navegador usa parseFloat(), que aceita valores como "0,4 mm" ou
    # "5.7 mm". O Python float() é mais rígido e antes transformava esses
    # valores em None. Extraímos o primeiro número e aceitamos vírgula decimal.
    m=re.search(r'[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)',s.replace(' ',''))
    if not m:return None
    token=m.group(0)
    if ',' in token and '.' in token:
        if token.rfind(',')>token.rfind('.'):
            token=token.replace('.','').replace(',','.')
        else:
            token=token.replace(',','')
    else:
        token=token.replace(',','.')
    try:
        n=float(token)
        return n if math.isfinite(n) else None
    except Exception:
        return None

def text(v):
    x=unwrap(v)
    return '' if x is None else str(x)

def as_stations(value):
    # A API pode entregar qualle_meteorologia como lista ou como objeto
    # indexado pelo código da estação. Aceitamos os dois formatos e também
    # evitamos tratar metadados aninhados como estações.
    if isinstance(value,list):
        return [x for x in value if isinstance(x,dict) and x.get('codigo') is not None]
    if isinstance(value,dict):
        if value.get('codigo') is not None:
            return [value]
        rows=[]
        for x in value.values():
            if isinstance(x,dict) and x.get('codigo') is not None:
                rows.append(x)
        return rows
    return []

def _flatten_text(v, out=None, depth=0):
    if out is None: out=[]
    if depth>8 or v is None: return out
    if isinstance(v,(str,int,float,bool)):
        out.append(str(v))
        return out
    if isinstance(v,dict):
        for key,valv in v.items():
            # Do not discard nested GraphQL value/show structures.
            if key in ('value','local','general','prefix','regiao','bacia','rio_nome','nome','name','codigo'):
                _flatten_text(valv,out,depth+1)
            elif depth<4:
                _flatten_text(valv,out,depth+1)
    elif isinstance(v,list):
        for item in v:
            _flatten_text(item,out,depth+1)
    return out

def station_city(s):
    name=s.get('name') or {}; pos=s.get('position') or {}
    rio=((s.get('data') or {}).get('rio') or {})
    # Primeiro, os mesmos campos usados pelo painel.
    parts=[
        s.get('codigo'),
        name.get('local'), name.get('general'), name.get('prefix'),
        pos.get('regiao'), pos.get('bacia'),
        (rio.get('rio_nome') or {}).get('value')
    ]
    h=norm(' '.join(text(x) for x in parts))
    for city,pat in CITY_PATTERNS:
        if pat.search(h):
            return city

    # Fallback: algumas estações trazem o município em outro metadado
    # aninhado. Procuramos todos os textos da própria estação, sem usar
    # coordenadas nem inferir município por proximidade.
    broad=norm(' '.join(_flatten_text(s)))
    for city,pat in CITY_PATTERNS:
        if pat.search(broad):
            return city
    return None

def rain(s,k):
    data=(s.get('data') or {}).get('chuva') or {}
    acc=data.get('acumulado') or {}
    v=val(acc.get(k))
    if v is not None: return max(0.0,v)
    # Alguns períodos secos chegam com acumulado ausente, mas min005 explícito em 0.
    now5=val(acc.get('min005'))
    if now5 is not None and now5 <= 0.0: return 0.0
    return None

def rain_quality(stations):
    out={}
    for k in ('h003','h006','h012','h024','h048','h096'):
        by_city={}
        station_count=0

        for s in stations:
            city=station_city(s)
            if city not in CONTRIB:
                continue
            v=rain(s,k)
            if v is None:
                continue
            station_count += 1
            by_city.setdefault(norm(city),[]).append(v)

        city_means={
            city: sum(values)/len(values)
            for city,values in by_city.items()
            if values
        }

        value=(
            sum(city_means.values())/len(city_means)
            if city_means else None
        )

        out[k]={
            'stations':station_count,
            'cities':len(city_means),
            'value':value
        }

    return out


def avg_rain(stations,k):
    by={}
    for s in stations:
        c=station_city(s)
        if c not in CONTRIB: continue
        v=rain(s,k)
        if v is None: continue
        by.setdefault(norm(c),[]).append(v)
    means=[sum(v)/len(v) for v in by.values()]
    return sum(means)/len(means) if means else None


def spatial_rain(stations):
    """Resumo espacial da chuva observada, mantendo a média espacial atual.

    Agrupa por município contributivo (média das estações do município) e
    também produz duas macro-regiões. Não atribui pesos hidrológicos fixos.
    """
    windows=('h003','h006','h012','h024')
    cities={}
    for city in CONTRIB:
        vals={k:[] for k in windows}
        for st in stations:
            if station_city(st)!=city: continue
            for k in windows:
                v=rain(st,k)
                if v is not None: vals[k].append(v)
        row={k: round(sum(v)/len(v),3) for k,v in vals.items() if v}
        if row: cities[city]=row
    regions={'alto':{},'medio':{}}
    for region, members in (('alto',ALTO),('medio',[c for c in CONTRIB if c not in ALTO])):
        for k in windows:
            xs=[cities[c][k] for c in members if c in cities and k in cities[c]]
            if xs: regions[region][k]=round(sum(xs)/len(xs),3)
    return {'cities':cities,'regions':regions,'station_count':sum(1 for st in stations if station_city(st) in CONTRIB)}

async def get_ws():
    import websockets
    async with websockets.connect(WS_URL,subprotocols=['graphql-transport-ws'],open_timeout=20,close_timeout=5) as ws:
        await ws.send(json.dumps({'type':'connection_init','payload':{}}))
        ack=False
        for _ in range(10):
            m=json.loads(await asyncio.wait_for(ws.recv(),20))
            if m.get('type')=='connection_ack': ack=True; break
            if m.get('type')=='error': raise RuntimeError(str(m))
        if not ack: raise RuntimeError('WebSocket não confirmou connection_ack')
        await ws.send(json.dumps({'id':'1','type':'subscribe','payload':{'query':QUERY,'variables':{}}}))
        while True:
            m=json.loads(await asyncio.wait_for(ws.recv(),30))
            if m.get('type')=='ping':
                await ws.send(json.dumps({'type':'pong'})); continue
            if m.get('type')=='next': return m.get('payload',{}).get('data',{})
            if m.get('type')=='error': raise RuntimeError(str(m))


def _parse_river_payload(d): return d.get('niveis') or d.get('levels') or [] if isinstance(d,dict) else []
def _parse_river_text(textv):
    try:
        d=json.loads(textv); arr=_parse_river_payload(d)
        if arr:return arr
    except Exception: pass
    m=re.search(r'\{\s*"niveis"\s*:\s*\[.*\]\s*\}',textv,re.S)
    if m:
        try:
            arr=_parse_river_payload(json.loads(m.group(0)))
            if arr:return arr
        except Exception: pass
    return []

def _request_river(url,verify=True):
    r=requests.get(url,timeout=20,verify=verify,headers={'User-Agent':'Mozilla/5.0'}); r.raise_for_status(); return _parse_river_text(r.text)

def get_river():
    sources=[('direto',RIVER_URL,True),('corsproxy','https://corsproxy.io/?'+requests.utils.quote(RIVER_URL,safe=''),True),('jina','https://r.jina.ai/'+RIVER_URL,True),('direto-sem-validacao',RIVER_URL,False)]
    for label,url,verify in sources:
        try:
            arr=_request_river(url,verify); vals=[]
            for x in arr:
                t=x.get('horaLeitura') or x.get('timestamp') or x.get('hora'); lv=val(x.get('nivel'))
                if not t or lv is None: continue
                try:
                    parsed=datetime.fromisoformat(str(t).replace('Z','+00:00'))
                    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=ZoneInfo('America/Sao_Paulo'))
                    ms=int(parsed.timestamp()*1000)
                except Exception: continue
                vals.append({'t':ms,'level':lv})
            vals.sort(key=lambda x:x['t'])
            if vals:
                print(f'Fonte do rio: {label} ({len(vals)} leituras)'); return vals
        except Exception as e: print(f'Falha rio {label}: {type(e).__name__}: {e}')
    return []


def dam_name(s):
    textv=norm(' '.join(text(x) for x in [s.get('codigo'),(s.get('name') or {}).get('local'),(s.get('name') or {}).get('general'),(s.get('position') or {}).get('regiao'),(s.get('position') or {}).get('bacia')]))
    if 'ituporanga' in textv or 'barragem sul' in textv:return 'sul'
    if 'taio' in textv or 'barragem oeste' in textv:return 'oeste'
    return None

def dam_open_fraction(s):
    c=((s.get('data') or {}).get('barramento') or {}).get('comportas') or {}; opened=0; total=0
    for i in range(1,11):
        g=c.get(f'comporta_{i}')
        if not g: continue
        total+=1; raw=unwrap(g.get('estado')); state=norm(raw)
        if raw is True or raw==1 or str(raw).strip()=='1' or re.search(r'abert|open|parcial',state): opened+=1
    return opened/max(1,total),opened,total

def get_dams(stations):
    out={}
    for s in stations:
        if not ((s.get('data') or {}).get('barramento')): continue
        k=dam_name(s)
        if k is None: continue
        frac,opened,total=dam_open_fraction(s); b=s.get('data',{}).get('barramento',{}); nivel=b.get('nivel') or {}
        spill=val(nivel.get('vertido')) or 0; pct=val(nivel.get('percentual'))
        out[k]={'name':k,'codigo':s.get('codigo'),'timestamp':s.get('timestamp'),'open_fraction':frac,'open':opened,'total_gates':total,'spill':spill,'spill_active':1 if spill>0 else 0,'reservoir_percent':pct}
    return out

def dam_signal_now(dams):
    if not dams:return {'open_fraction':0.0,'spill_active':0,'open':0,'total':0,'event_opening':0.0,'recent_event_signal':0.0}
    total_open=sum(int(d.get('open',0) or 0) for d in dams.values()); total_gates=sum(int(d.get('total_gates',0) or 0) for d in dams.values())
    return {'open_fraction':total_open/max(1,total_gates),'spill_active':1 if any(int(d.get('spill_active',0) or 0)>0 for d in dams.values()) else 0,'open':total_open,'total':total_gates,'event_opening':0.0,'recent_event_signal':0.0}

def loadj(path,default):
    try:
        with open(path,encoding='utf-8') as f:return json.load(f)
    except Exception:return default

def savej(path,obj):
    os.makedirs(os.path.dirname(path),exist_ok=True); tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f:json.dump(obj,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)

# Camadas novas de observação/aprendizado. Elas NÃO alteram a previsão atual.
# Primeiro acumulam eventos reais; só no futuro poderão servir como referência.
FLOOD_START_LEVEL=4.00
FLOOD_END_LEVEL=3.90  # histerese para não abrir/fechar o evento na mesma faixa
RAIN_ONSET_MM_3H=2.0
RIVER_RESPONSE_RISE_M=0.05
RAIN_RESPONSE_MAX_HOURS=24.0
# Um episódio meteorológico só é considerado encerrado após este período
# contínuo sem chuva significativa. Pausas menores continuam no mesmo evento.
RAIN_EVENT_DRY_GAP_HOURS=24.0
# Evita encerrar durante uma recuperação hidrológica real, mesmo abaixo do pico anterior.
RIVER_MEANINGFUL_RISE_M=0.002
RIVER_CLOSING_POSITIVE_SLOPE_M_H=0.0005

def default_rain_response():
    """Estado de aprendizado hidrológico, separado da previsão principal.

    A camada aprende padrões do tipo:
    quantidade/intensidade de chuva -> atraso -> subida -> velocidade.
    Por enquanto é somente observacional para evitar regressões.
    """
    return {
        'version': 5,
        'reference_delay_hours': 5.0,
        'reference_cm_per_mm': 4.5,
        'reference_rain_window_hours': 48,
        'reference_calibration': {
            'min_rain_mm': 30.0,
            'min_events_before_adjustment': 3,
            'max_step_cm_per_mm': 0.20,
            'min_cm_per_mm': 2.0,
            'max_cm_per_mm': 6.0,
            'observations': 0,
            'sum_rain_mm': 0.0,
            'sum_rise_cm': 0.0,
            'estimated_cm_per_mm': 4.5,
            'confidence': 'prior'
        },
        'learned_delay_hours': 5.0,
        'events': 0,
        'sum_delay_hours': 0.0,
        'mean_delay_hours': None,
        'sum_initial_rise_rate_cm_h': 0.0,
        'sum_max_rise_rate_cm_h': 0.0,
        'mean_initial_rise_rate_cm_h': None,
        'mean_max_rise_rate_cm_h': None,
        'patterns': {},
        'event_history': [],
        'active': None,
        'last_event': None,
        # Metadados explícitos do episódio de chuva atual. O evento continua
        # aguardando a resposta do rio mesmo depois de a chuva terminar.
        'last_rain_episode': None,
        'spatial_patterns': {},
        # Aprendizado por cidade: associação histórica, não causalidade garantida.
        'city_influence': {},
        'similarity_status': None,
        # Após concluir/expirar um episódio, aguarda a chuva recente cair
        # abaixo do limiar antes de permitir outro evento.
        'awaiting_rain_reset': False,
        'updated_at': None
    }

def _num(v, default=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def _rain_band(mm):
    mm=max(0.0,_num(mm))
    for lo,hi,label in ((0,10,'0_10'),(10,30,'10_30'),(30,60,'30_60'),(60,100,'60_100'),(100,150,'100_150')):
        if lo<=mm<hi:return label
    return '150_plus'

def _rain_pattern_key(active):
    # Intensidade + duração real do episódio. Assim, totais semelhantes
    # distribuídos em durações muito diferentes não são automaticamente
    # tratados como o mesmo padrão.
    r3=max(0.0,_num(active.get('max_rain_h003')))
    r6=max(r3,_num(active.get('max_rain_h006')))
    r12=max(r6,_num(active.get('max_rain_h012')))
    duration=max(0.0,_num(active.get('rain_duration_hours'))); dur='short' if duration<=3 else ('medium' if duration<=8 else 'long')
    if r3>=RAIN_ONSET_MM_3H:return _rain_band(r3)+'mm_3h_'+dur
    if r6>=RAIN_ONSET_MM_3H:return _rain_band(r6)+'mm_6h_'+dur
    if r12>=RAIN_ONSET_MM_3H:return _rain_band(r12)+'mm_12h_'+dur
    return _rain_band(max(r12,_num(active.get('max_rain_h024'))))+'mm_24h_'+dur

def _update_pattern(pattern, event):
    n=int(pattern.get('events',0) or 0)+1
    delay=_num(event.get('delay_hours'))
    rise_cm=_num(event.get('rise_cm'))
    initial_rate=_num(event.get('initial_rise_rate_cm_h'))
    max_rate=_num(event.get('max_rise_rate_cm_h'))
    for key,value in (('sum_delay_hours',delay),('sum_rise_cm',rise_cm),('sum_initial_rise_rate_cm_h',initial_rate),('sum_max_rise_rate_cm_h',max_rate)):
        pattern[key]=_num(pattern.get(key))+value
    pattern['events']=n
    pattern['mean_delay_hours']=round(pattern['sum_delay_hours']/n,3)
    pattern['mean_rise_cm']=round(pattern['sum_rise_cm']/n,3)
    pattern['mean_initial_rise_rate_cm_h']=round(pattern['sum_initial_rise_rate_cm_h']/n,3)
    pattern['mean_max_rise_rate_cm_h']=round(pattern['sum_max_rise_rate_cm_h']/n,3)
    # Curva temporal da resposta: subida acumulada após 1/2/4/6/12h.
    curve=event.get('response_rise_cm') or {}
    sums=pattern.setdefault('response_rise_sums_cm',{})
    counts=pattern.setdefault('response_rise_counts',{})
    means=pattern.setdefault('mean_response_rise_cm',{})
    for cp in ('1','2','4','6','12'):
        if isinstance(curve.get(cp),(int,float)):
            sums[cp]=_num(sums.get(cp))+_num(curve[cp])
            counts[cp]=int(counts.get(cp,0) or 0)+1
            means[cp]=round(sums[cp]/counts[cp],3)
    return pattern


def _update_spatial_pattern(pattern, spatial):
    n=int(pattern.get('events',0) or 0)+1
    pattern['events']=n
    sums=pattern.setdefault('sums',{})
    means=pattern.setdefault('means',{})
    for group in ('regions','cities'):
        for name,row in (spatial.get(group) or {}).items():
            if not isinstance(row,dict): continue
            for k,v in row.items():
                if not isinstance(v,(int,float)): continue
                key=group+'|'+str(name)+'|'+str(k)
                sums[key]=_num(sums.get(key))+float(v)
                means[key]=round(sums[key]/n,3)
    return pattern


def _city_window(row):
    if not isinstance(row,dict): return 0.0
    return max(_num(row.get('h003')), _num(row.get('h006')), _num(row.get('h012')))

def _solve_linear_system(a, b):
    """Resolve Ax=b por eliminação de Gauss; sem dependência extra."""
    n=len(b)
    aug=[list(a[i])+[b[i]] for i in range(n)]
    for col in range(n):
        pivot=max(range(col,n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col])<1e-10:
            return None
        aug[col],aug[pivot]=aug[pivot],aug[col]
        div=aug[col][col]
        aug[col]=[v/div for v in aug[col]]
        for r in range(n):
            if r==col: continue
            factor=aug[r][col]
            if factor:
                aug[r]=[aug[r][j]-factor*aug[col][j] for j in range(n+1)]
    return [aug[i][-1] for i in range(n)]

def _rebuild_city_influence(history):
    """Aprendizado multievento robusto por cidade.

    Usa ridge NÃO NEGATIVO com validação por reamostragem. Assim, uma cidade
    não recebe contribuição negativa artificial por multicolinearidade.
    Coeficientes instáveis são mantidos como observação, mas não ficam elegíveis
    para influenciar a projeção.
    """
    rows=[]; counts={}
    for ev in history or []:
        cities=((ev.get('spatial_rain') or {}).get('cities') or {})
        vals={c:_city_window(r) for c,r in cities.items() if _city_window(r)>0}
        y=max(0.0,_num(ev.get('rise_cm')))
        if not vals: continue
        rows.append((vals,y))
        for c in vals: counts[c]=counts.get(c,0)+1
    candidates=sorted([c for c,n in counts.items() if n>=6], key=lambda c:(-counts[c],c))
    if not candidates:
        return {'method':'robust_nonnegative_ridge','events':len(rows),'ready':False,
                'reason':'insufficient_repeated_city_samples','cities':{}}
    features=candidates[:max(1,min(8,(len(rows)//2)-1))]
    min_events=max(15,3*(len(features)+1))
    if len(rows)<min_events:
        return {'method':'robust_nonnegative_ridge','events':len(rows),'ready':False,
                'reason':'insufficient_events_for_multivariate_fit','required_events':min_events,
                'features':features,'cities':{}}
    X=[[vals.get(c,0.0) for c in features] for vals,_ in rows]
    Y=[y for _,y in rows]
    means=[sum(r[j] for r in X)/len(X) for j in range(len(features))]
    ym=sum(Y)/len(Y)
    # Coordinate descent para ridge com coeficientes >= 0.
    def fit(indices):
        x=[X[i] for i in indices]; y=[Y[i] for i in indices]
        mm=[sum(r[j] for r in x)/len(x) for j in range(len(features))]
        yy=sum(y)/len(y)
        scales=[]
        z=[]
        for j in range(len(features)):
            sd=math.sqrt(sum((r[j]-mm[j])**2 for r in x)/len(x))
            scales.append(max(1e-6,sd))
        z=[[(r[j]-mm[j])/scales[j] for j in range(len(features))] for r in x]
        yyv=[v-yy for v in y]; k=len(features); beta=[0.0]*k
        ridge=2.0
        for _ in range(400):
            change=0.0
            for j in range(k):
                numer=0.0; denom=ridge
                for i,row in enumerate(z):
                    pred=sum(row[q]*beta[q] for q in range(k) if q!=j)
                    numer+=row[j]*(yyv[i]-pred); denom+=row[j]*row[j]
                nb=max(0.0,numer/max(1e-9,denom))
                change=max(change,abs(nb-beta[j])); beta[j]=nb
            if change<1e-7: break
        return [beta[j]/scales[j] for j in range(k)],mm,yy
    idx=list(range(len(X)))
    coeff,_,_=fit(idx)
    preds=[ym+sum(coeff[j]*(r[j]-means[j]) for j in range(len(features))) for r in X]
    sse=sum((y-p)**2 for y,p in zip(Y,preds)); sst=sum((y-ym)**2 for y in Y)
    r2=max(0.0,1.0-sse/sst) if sst>1e-9 else 0.0
    # Leave-block-out stability: 5 folds determinísticos.
    samples=[]
    folds=min(5,max(3,len(X)//8))
    for f in range(folds):
        sub=[i for i in idx if i%folds!=f]
        if len(sub)>=len(features)+3: samples.append(fit(sub)[0])
    cities={}; stable_count=0
    for j,c in enumerate(features):
        vals=[q[j] for q in samples] or [coeff[j]]
        mean_b=sum(vals)/len(vals)
        spread=math.sqrt(sum((v-mean_b)**2 for v in vals)/len(vals))
        rel=spread/max(0.03,mean_b)
        stability=max(0.0,min(1.0,1.0-rel))
        support=min(1.0,counts[c]/15.0)
        confidence=support*stability*max(0.0,min(1.0,r2))
        eligible=(coeff[j]>0 and counts[c]>=8 and stability>=0.45 and r2>=0.20)
        if eligible: stable_count+=1
        cities[c]={'events':counts[c],'coefficient_cm_per_mm':round(coeff[j],6),
                   'mean_mm':round(means[j],4),'stability':round(stability,3),
                   'confidence':round(confidence,3),'eligible_for_projection':eligible,
                   'interpretation':'associacao_multievento_nao_negativa; controlando outras cidades; nao causalidade isolada'}
    ready=stable_count>0 and r2>=0.20
    return {'method':'robust_nonnegative_ridge','events':len(rows),'ready':ready,
            'features':features,'r2':round(r2,4),
            'confidence':round(max([v['confidence'] for v in cities.values()] or [0.0]),3),
            'stable_cities':stable_count,'cities':cities}

def city_component_from_memory(state, horizon):
    """Ajuste espacial aprendido por regressão, limitado e gradual."""
    if not isinstance(state,dict) or not isinstance(state.get('active'),dict): return 0.0
    model=state.get('city_influence') or {}
    if not model.get('ready'): return 0.0
    cities=((state['active'].get('spatial_peak') or {}).get('cities') or {})
    info=model.get('cities') or {}; conf=_num(model.get('confidence'))
    delta_cm=0.0
    for city,rec in info.items():
        if city not in cities or not rec.get('eligible_for_projection'): continue
        mm=_city_window(cities.get(city) or {})
        delta_cm+=_num(rec.get('coefficient_cm_per_mm'))*(mm-_num(rec.get('mean_mm')))
    # Não permite que a regressão sozinha imponha subida; é apenas correção
    # espacial em torno da memória geral e fica estritamente limitada.
    fraction={2:0.06,4:0.10,6:0.14,12:0.18,24:0.20}.get(int(horizon),0.10)
    weight=min(0.20,0.20*conf)
    return round(clamp((delta_cm/100.0)*fraction*weight,-0.05,0.08),6)

def spatial_component_from_memory(state, now, current, horizon):
    """Componente espacial conservador baseado em eventos semelhantes.

    Compara a distribuição por cidade do episódio ativo com eventos concluídos.
    Não inventa pesos fixos; usa apenas respostas realmente observadas.
    """
    if not isinstance(state,dict) or not isinstance(state.get('active'),dict):
        return 0.0
    active=state['active']
    cur=((active.get('spatial_peak') or {}).get('cities') or {})
    cur_total=sum(_city_window(v) for v in cur.values())
    if cur_total<=0: return 0.0
    history=state.get('event_history') or []
    matches=[]
    for ev in history:
        old=((ev.get('spatial_rain') or {}).get('cities') or {})
        old_total=sum(_city_window(v) for v in old.values())
        if old_total<=0: continue
        # Similaridade de distribuição (0..1) + proximidade de volume.
        cities=set(cur)|set(old)
        dist=sum(abs(_city_window(cur.get(c,{}))/cur_total-_city_window(old.get(c,{}))/old_total) for c in cities)
        volume=1.0/(1.0+abs(cur_total-old_total)/max(10.0,cur_total))
        score=max(0.0,1.0-dist/2.0)*volume
        if score<0.35: continue
        matches.append((score,ev))
    if not matches: return 0.0
    matches.sort(key=lambda x:x[0],reverse=True)
    top=matches[:8]
    expected=sum(score*max(0.0,_num(ev.get('rise_cm'))) for score,ev in top)/sum(score for score,_ in top)
    # Evento inteiro não deve ser adicionado de uma vez. Horizonte recebe
    # fração conservadora e confiança limitada.
    fraction={2:0.12,4:0.22,6:0.34,12:0.55,24:0.70}.get(int(horizon),0.2)
    n=len(top)
    confidence=min(0.30,0.03+0.035*n)
    return round(min(0.35,(expected/100.0)*fraction*confidence),6)

def event_similarity(current_event, history, limit=8):
    """Compara episódios por intensidade, duração e distribuição regional.
    Resultado é explicável e não altera a projeção sem dados suficientes.
    """
    if not isinstance(current_event,dict): return {'matches':[],'confidence':'insufficient'}
    candidates=[]
    cur_r=max(_num(current_event.get('max_rain_h003')), _num(current_event.get('max_rain_h006')))
    cur_d=_num(current_event.get('rain_duration_hours'))
    cur_regions=((current_event.get('spatial_peak') or {}).get('regions') or {})
    for ev in history or []:
        if not isinstance(ev,dict): continue
        er=max(_num(ev.get('rain_h003')), _num(ev.get('rain_h006')))
        ed=_num(ev.get('rain_duration_hours'))
        score=1.0/(1.0+abs(cur_r-er)/10.0+abs(cur_d-ed)/3.0)
        ereg=((ev.get('spatial_rain') or {}).get('regions') or {})
        for reg in set(cur_regions)|set(ereg):
            score-=min(0.25,abs(_num((cur_regions.get(reg) or {}).get('h006'))-_num((ereg.get(reg) or {}).get('h006')))/100.0)
        candidates.append((score,ev))
    candidates.sort(key=lambda x:x[0],reverse=True)
    out=[]
    for score,ev in candidates[:limit]:
        out.append({'score':round(max(0.0,score),3),'pattern':ev.get('pattern'),'peak_level':ev.get('peak_level'),'rise_cm':ev.get('rise_cm'),'delay_hours':ev.get('delay_hours')})
    return {'matches':out,'confidence':'low' if len(out)<3 else ('medium' if len(out)<8 else 'high')}

def update_rain_response(state, now, current, rain, slope, spatial=None):
    """Aprende chuva -> atraso -> subida -> velocidade, sem mexer na previsão.

    Um candidato é aberto por chuva significativa. A resposta só é aceita após
    subida real >=5 cm e tendência positiva. O evento é classificado por faixa
    de mm e janela, permitindo acumular padrões sem criar relações falsas do
    tipo '37.42 mm = exatamente X cm'.
    """
    if not isinstance(state,dict): state=default_rain_response()
    base=default_rain_response()
    for k,v in base.items(): state.setdefault(k,v)
    rain=rain or {}
    spatial=spatial if isinstance(spatial,dict) else {}
    active=state.get('active')
    r3=max(0.0,_num(rain.get('h003')))
    r6=max(r3,_num(rain.get('h006')))
    r12=max(r6,_num(rain.get('h012')))
    r24=max(r12,_num(rain.get('h024')))

    # Evita dividir a mesma chuva em vários eventos: depois que um episódio
    # termina, só arma novamente quando os acumulados recentes voltam abaixo
    # do limiar. Assim, um acumulado persistente não abre outro evento.
    if active is None and state.get('awaiting_rain_reset'):
        if r3 < RAIN_ONSET_MM_3H and r6 < RAIN_ONSET_MM_3H:
            state['awaiting_rain_reset']=False

    raining_now=(r3>=RAIN_ONSET_MM_3H or r6>=RAIN_ONSET_MM_3H)

    # Início explícito de um episódio. Usamos as janelas observadas curtas
    # disponíveis; isso evita abrir episódios por acumulados longos residuais.
    if (active is None and not state.get('awaiting_rain_reset')
            and current is not None and raining_now):
        active={
            'started_at':now,
            'rain_started_at':now,
            'rain_ended_at':None,
            'last_rain_active_at':now,
            'dry_gap_started_at':None,
            'baseline_level':float(current),
            'max_level':float(current),
            'last_observed_level':float(current),
            'max_rain_h003':r3,'max_rain_h006':r6,
            'max_rain_h012':r12,'max_rain_h024':r24,
            'max_rain_h048':max(r24,_num(rain.get('h048'))),'max_rain_h096':max(r24,_num(rain.get('h096'))),
            # Referência dos acumulados no instante em que o evento começou.
            # Permite separar a chuva nova da chuva que já estava acumulada.
            'rain_baseline_h048':_num(rain.get('h048')),
            'rain_baseline_h096':_num(rain.get('h096')),
            'event_rain_mm_h048':0.0,
            'event_rain_mm_h096':0.0,
            'response_detected_at':None,
            'response_level':None,
            'initial_rise_rate_cm_h':None,
            'max_rise_rate_cm_h':max(0.0,_num(slope)*100.0),
            'response_checkpoints_cm': {},
            'spatial_start': json.loads(json.dumps(spatial)),
            'spatial_peak': json.loads(json.dumps(spatial))
        }
        state['active']=active

    if isinstance(active,dict) and current is not None:
        # Pausas curtas não dividem o mesmo episódio. Enquanto a chuva
        # significativa voltar antes de 24h, o contador seco é cancelado e o
        # evento continua aberto. rain_ended_at só é confirmado após 24h secas.
        if raining_now:
            active['last_rain_active_at']=now
            active['dry_gap_started_at']=None
            active['rain_ended_at']=None
        else:
            if active.get('dry_gap_started_at') is None:
                active['dry_gap_started_at']=now
            dry_gap_h=max(0.0,(now-int(active.get('dry_gap_started_at')))/3600000.0)
            if dry_gap_h>=RAIN_EVENT_DRY_GAP_HOURS and active.get('rain_ended_at') is None:
                active['rain_ended_at']=int(active.get('last_rain_active_at') or active.get('dry_gap_started_at') or now)
                state['last_rain_episode']={
                    'started_at':active.get('rain_started_at',active.get('started_at')),
                    'ended_at':active.get('rain_ended_at'),
                    'dry_gap_confirmed_at':now,
                    'duration_hours':round(max(0.0,(int(active.get('rain_ended_at'))-int(active.get('rain_started_at',active.get('started_at',now))))/3600000.0),3),
                    'max_rain_h003':round(_num(active.get('max_rain_h003')),3),
                    'max_rain_h006':round(_num(active.get('max_rain_h006')),3),
                    'max_rain_h012':round(_num(active.get('max_rain_h012')),3),
                    'max_rain_h024':round(_num(active.get('max_rain_h024')),3)
                }
        if active.get('rain_started_at') is not None:
            # Duração do episódio meteorológico completo até a última chuva;
            # pausas curtas fazem parte do mesmo episódio.
            end_for_duration = int(active.get('last_rain_active_at') or now)
            active['rain_duration_hours']=round(max(0.0,(end_for_duration-int(active.get('rain_started_at')))/3600000.0),3)
        # Mantém o maior acumulado observado por cidade/região durante o episódio.
        peak=active.setdefault('spatial_peak',{})
        for group in ('cities','regions'):
            peak.setdefault(group,{})
            for name,row in (spatial.get(group) or {}).items():
                if not isinstance(row,dict): continue
                dst=peak[group].setdefault(name,{})
                for k,v in row.items():
                    if isinstance(v,(int,float)):
                        dst[k]=max(_num(dst.get(k)),float(v))
        # Registra subida significativa pela evolução recente do nível, não
        # apenas por uma nova máxima. Isso captura uma recuperação como
        # 2.10 -> 2.05 -> 2.09, que ainda é uma subida hidrológica real.
        previous_level=_num(active.get('last_observed_level'),float(current))
        level_delta=float(current)-previous_level
        previous_max=_num(active.get('max_level'),current)
        active['max_level']=max(previous_max,float(current))
        if level_delta>=RIVER_MEANINGFUL_RISE_M or float(current)>previous_max+RIVER_MEANINGFUL_RISE_M:
            active['last_meaningful_rise_at']=now
        active['last_observed_level']=float(current)
        # Similaridade histórica: informativa e explicável. Só ganha valor
        # conforme eventos reais forem acumulados; não força a projeção.
        state['similarity_status']=event_similarity(active,state.get('event_history') or [])
        active['max_rain_h003']=max(_num(active.get('max_rain_h003')),r3)
        active['max_rain_h006']=max(_num(active.get('max_rain_h006')),r6)
        active['max_rain_h012']=max(_num(active.get('max_rain_h012')),r12)
        active['max_rain_h024']=max(_num(active.get('max_rain_h024')),r24)
        active['max_rain_h048']=max(_num(active.get('max_rain_h048')),_num(rain.get('h048')))
        active['max_rain_h096']=max(_num(active.get('max_rain_h096')),_num(rain.get('h096')))
        # Chuva efetivamente nova desde o início do evento; não conta novamente
        # o acumulado que já existia antes da abertura.
        event_h48=max(0.0,_num(rain.get('h048'))-_num(active.get('rain_baseline_h048')))
        event_h96=max(0.0,_num(rain.get('h096'))-_num(active.get('rain_baseline_h096')))
        active['event_rain_mm_h048']=max(_num(active.get('event_rain_mm_h048')),event_h48)
        active['event_rain_mm_h096']=max(_num(active.get('event_rain_mm_h096')),event_h96)
        active['max_rise_rate_cm_h']=max(_num(active.get('max_rise_rate_cm_h')),max(0.0,_num(slope)*100.0))
        delay_h=max(0.0,(now-int(active.get('started_at',now)))/3600000.0)
        rise=float(current)-_num(active.get('baseline_level'),float(current))

        if rise>=RIVER_RESPONSE_RISE_M and _num(slope)>0 and active.get('response_detected_at') is None:
            # Detecta o atraso imediatamente e continua observando o episódio
            # completo; não há teto rígido de 12h.
            active['response_detected_at']=now
            active['response_level']=float(current)
            active['initial_rise_rate_cm_h']=max(0.0,_num(slope)*100.0)
            active['response_delay_hours']=delay_h
            active['response_checkpoints_cm']={}

        response_at=active.get('response_detected_at')
        if response_at is not None:
            response_age_h=max(0.0,(now-int(response_at))/3600000.0)
            response_level=_num(active.get('response_level'),current)
            response_max=max(response_level,_num(active.get('response_max_level'),response_level),float(current))
            active['response_max_level']=response_max
            checkpoints=active.setdefault('response_checkpoints_cm',{})
            response_rise_cm=max(0.0,(response_max-response_level)*100.0)
            for cp in (1,2,4,6,12):
                key=str(cp)
                if response_age_h>=cp and key not in checkpoints:
                    checkpoints[key]=round(response_rise_cm,3)
            # Não há teto rígido de 12h: frentes frias podem produzir chuva
            # intermitente por vários dias. O evento só pode fechar depois de
            # 24h contínuas sem chuva significativa e após a resposta principal
            # do rio estar caracterizada (curva mínima + estabilidade).
            last_rise=int(active.get('last_meaningful_rise_at') or response_at)
            stable_h=max(0.0,(now-last_rise)/3600000.0)
            enough_curve=(response_age_h>=6.0 and len(checkpoints)>=3)
            rain_finished=active.get('rain_ended_at') is not None
            # Não encerra se a tendência atual ainda for positivamente
            # significativa, mesmo que o nível esteja abaixo do pico anterior.
            river_still_rising=_num(slope)>RIVER_CLOSING_POSITIVE_SLOPE_M_H
            if rain_finished and enough_curve and stable_h>=3.0 and not river_still_rising:
                event={
                    'rain_started_at':active.get('rain_started_at',active.get('started_at')),
                    'rain_ended_at':active.get('rain_ended_at'),
                    'rain_duration_hours':round(max(0.0,((int(active.get('rain_ended_at') or now)-int(active.get('rain_started_at',active.get('started_at',now))))/3600000.0)),3) if active.get('rain_started_at') is not None else None,
                    'river_response_at':response_at,
                    'completed_at':now,
                    'delay_hours':round(_num(active.get('response_delay_hours')),3),
                    'delay_after_rain_end_hours': (round(max(0.0,(int(response_at)-int(active.get('rain_ended_at')))/3600000.0),3) if active.get('rain_ended_at') is not None else None),
                    'rain_h003':round(_num(active.get('max_rain_h003')),3),
                    'rain_h006':round(_num(active.get('max_rain_h006')),3),
                    'rain_h012':round(_num(active.get('max_rain_h012')),3),
                    'rain_h024':round(_num(active.get('max_rain_h024')),3),
                    'rain_h048':round(_num(active.get('max_rain_h048')),3),
                    'rain_h096':round(_num(active.get('max_rain_h096')),3),
                    'event_rain_mm_h048':round(_num(active.get('event_rain_mm_h048')),3),
                    'event_rain_mm_h096':round(_num(active.get('event_rain_mm_h096')),3),
                    'rise_cm':round((float(active.get('max_level',current))-_num(active.get('baseline_level'),current))*100.0,3),
                    'initial_rise_rate_cm_h':round(_num(active.get('initial_rise_rate_cm_h')),3),
                    'max_rise_rate_cm_h':round(_num(active.get('max_rise_rate_cm_h')),3),
                    'response_rise_cm':dict(active.get('response_checkpoints_cm') or {}),
                    'spatial_rain':dict(active.get('spatial_peak') or {}),
                    'peak_level':round(_num(active.get('max_level')),4),
                    'baseline_level':round(_num(active.get('baseline_level')),4),
                    'pattern':_rain_pattern_key(active)
                }
                state['events']=int(state.get('events',0) or 0)+1
                n=state['events']
                state['sum_delay_hours']=_num(state.get('sum_delay_hours'))+_num(event['delay_hours'])
                state['mean_delay_hours']=round(state['sum_delay_hours']/n,3)
                state['learned_delay_hours']=clamp(state['mean_delay_hours'],1.0,24.0)
                state['sum_initial_rise_rate_cm_h']=_num(state.get('sum_initial_rise_rate_cm_h'))+_num(event['initial_rise_rate_cm_h'])
                state['sum_max_rise_rate_cm_h']=_num(state.get('sum_max_rise_rate_cm_h'))+_num(event['max_rise_rate_cm_h'])
                state['mean_initial_rise_rate_cm_h']=round(state['sum_initial_rise_rate_cm_h']/n,3)
                state['mean_max_rise_rate_cm_h']=round(state['sum_max_rise_rate_cm_h']/n,3)
                patterns=state.setdefault('patterns',{})
                key=event['pattern']; patterns[key]=_update_pattern(dict(patterns.get(key) or {}),event)
                # Estatísticas espaciais transparentes: apenas registra médias por região/cidade.
                spats=state.setdefault('spatial_patterns',{})
                spats[key]=_update_spatial_pattern(dict(spats.get(key) or {}),event.get('spatial_rain') or {})
                state['event_history']=(list(state.get('event_history') or [])+[event])[-500:]
                _update_reference_calibration(state,event)
                # Recalibra influência por cidade usando vários eventos e
                # controlando simultaneamente as demais cidades. Não há
                # divisão proporcional artificial da subida.
                state['city_influence']=_rebuild_city_influence(state['event_history'])
                state['last_event']=event
                state['active']=None
                state['awaiting_rain_reset']=True
        elif delay_h>RAIN_RESPONSE_MAX_HOURS:
            # Sem resposta, não descarta uma chuva longa ainda ativa. Só desarma
            # após o episódio meteorológico realmente terminar (24h secas).
            if active.get('rain_ended_at') is not None:
                state['active']=None
                state['awaiting_rain_reset']=True
    state['updated_at']=datetime.now(timezone.utc).isoformat()
    return state

def _update_reference_calibration(state, event):
    """Calibra lentamente o prior 4,5 cm/mm a partir de eventos completos.

    Só usa eventos com chuva suficiente e resposta observada. O primeiro
    conjunto de eventos serve para confirmar o prior; depois o valor pode se
    mover gradualmente, limitado por passo e faixa de segurança. Isso evita
    que uma chuva pequena ou um evento anômalo reescreva o modelo.
    """
    cal=state.setdefault('reference_calibration',{})
    base=default_rain_response()['reference_calibration']
    for k,v in base.items(): cal.setdefault(k,v)
    rain=_num(event.get('rain_h048'))
    rise=max(0.0,_num(event.get('rise_cm')))
    if rain < _num(cal.get('min_rain_mm'),30.0) or rise < RIVER_RESPONSE_RISE_M*100.0:
        return
    observed=clamp(rise/rain,_num(cal.get('min_cm_per_mm'),2.0),_num(cal.get('max_cm_per_mm'),6.0))
    n=int(cal.get('observations',0) or 0)
    cal['observations']=n+1
    cal['sum_rain_mm']=_num(cal.get('sum_rain_mm'))+rain
    cal['sum_rise_cm']=_num(cal.get('sum_rise_cm'))+rise
    weighted=cal['sum_rise_cm']/max(1e-9,cal['sum_rain_mm'])
    old=_num(state.get('reference_cm_per_mm'),4.5)
    min_events=int(cal.get('min_events_before_adjustment',3) or 3)
    if cal['observations'] < min_events:
        cal['estimated_cm_per_mm']=round(weighted,4)
        cal['confidence']='confirming_prior'
        return
    # Pequeno passo por evento; média global reduz ruído de eventos isolados.
    target=clamp(0.65*weighted+0.35*observed,_num(cal.get('min_cm_per_mm'),2.0),_num(cal.get('max_cm_per_mm'),6.0))
    max_step=_num(cal.get('max_step_cm_per_mm'),0.20)
    new=old+clamp(target-old,-max_step,max_step)
    state['reference_cm_per_mm']=round(clamp(new,_num(cal.get('min_cm_per_mm'),2.0),_num(cal.get('max_cm_per_mm'),6.0)),4)
    cal['estimated_cm_per_mm']=state['reference_cm_per_mm']
    cal['confidence']='learning' if cal['observations']<8 else 'established'

def _reference_response_fraction(age_h, delay_h):
    """Curva inicial de resposta para a regra observacional de 4 cm/mm.

    A chuva não vira nível imediatamente. A curva usa o atraso aprendido e os
    primeiros pontos reais já observados no primeiro evento; depois de 12 h
    estende suavemente a resposta até 48 h. Ela representa uma FRAÇÃO da
    resposta total de um episódio, não um incremento repetido a cada ciclo.
    """
    x=float(age_h)-float(delay_h)
    if x<=0: return 0.0
    # Frações do primeiro evento observado: 16/520, 32/520, 54/520,
    # 72/520 e 136/520 após a detecção da resposta.
    points=[(0.0,0.0),(1.0,0.0308),(2.0,0.0615),(4.0,0.1038),
            (6.0,0.1385),(12.0,0.2615),(24.0,0.62),(36.0,0.84),(48.0,1.0)]
    if x>=48:return 1.0
    for (x0,y0),(x1,y1) in zip(points,points[1:]):
        if x<=x1:
            f=(x-x0)/max(1e-9,x1-x0)
            return y0+(y1-y0)*f
    return 1.0


def hydrological_reference_component(state, now, current, horizon):
    """Prior observacional: 1 mm de chuva -> 4,5 cm de rio.

    É aplicado como uma resposta temporal de episódio. O mesmo milímetro não
    é somado novamente em cada execução: calcula-se a resposta acumulada
    esperada naquele instante e subtrai-se o que o rio já realizou.
    """
    if not isinstance(state,dict) or not isinstance(state.get('active'),dict):
        return 0.0
    active=state['active']
    mm=max(0.0,_num(active.get('max_rain_h048')))
    ratio=_num(state.get('reference_cm_per_mm'),4.5)
    if mm<=0: return 0.0
    delay=_num(state.get('learned_delay_hours'),_num(state.get('reference_delay_hours'),5.0))
    age_h=max(0.0,(now-int(active.get('started_at',now)))/3600000.0)
    target_age=age_h+float(horizon)
    fraction=_reference_response_fraction(target_age,delay)
    expected_cm=mm*ratio*fraction
    baseline=_num(active.get('baseline_level'),current)
    observed_cm=max(0.0,(float(current)-baseline)*100.0)
    remaining_cm=max(0.0,expected_cm-observed_cm)
    # A referência 4,5 cm/mm não recebe mais um teto artificial por horizonte.
    # A própria curva temporal, o atraso aprendido e o desconto do nível já
    # realizado controlam a entrada da resposta sem bloquear eventos grandes.
    return round(max(0.0,remaining_cm/100.0),6)


def hydro_component_from_memory(state, now, rain, current, slope, horizon):
    """Memória chuva->rio, preservando o prior 4 cm/mm sem dupla contagem."""
    prior=hydrological_reference_component(state,now,current,horizon)
    if not isinstance(state,dict) or not isinstance(state.get('active'),dict):
        return prior
    active=state['active']
    key=_rain_pattern_key(active)
    pattern=(state.get('patterns') or {}).get(key)
    if not isinstance(pattern,dict):
        return prior
    n=int(pattern.get('events',0) or 0)
    means=pattern.get('mean_response_rise_cm') or {}
    if n<1 or not means:
        return prior
    started=int(active.get('started_at',now))
    age_h=max(0.0,(now-started)/3600000.0)
    delay=_num(pattern.get('mean_delay_hours'),_num(state.get('learned_delay_hours'),state.get('reference_delay_hours',5.0)))
    target_age=max(0.0,age_h+float(horizon)-delay)
    points=[(0.0,0.0)]
    for cp in (1,2,4,6,12):
        v=means.get(str(cp))
        if isinstance(v,(int,float)):
            points.append((float(cp),max(0.0,float(v))))
    points.sort()
    if len(points)<2:return prior
    def interp(x):
        if x<=0:return 0.0
        if x>=points[-1][0]:return points[-1][1]
        for (x0,y0),(x1,y1) in zip(points,points[1:]):
            if x<=x1:
                f=(x-x0)/max(1e-9,x1-x0); return y0+(y1-y0)*f
        return points[-1][1]
    expected_cm=interp(target_age)
    baseline=_num(active.get('baseline_level'),current)
    observed_cm=max(0.0,(float(current)-baseline)*100.0)
    learned_remaining=max(0.0,expected_cm-observed_cm)/100.0
    confidence=min(0.65,0.08+0.08*n)
    if _num(slope)<-0.01: confidence*=0.45
    learned_remaining*=confidence
    # O aprendizado histórico corrige o prior, mas não é somado integralmente.
    blended=(0.70*prior)+(0.30*min(0.70,learned_remaining))
    return round(min(1.20,max(0.0,blended)),6)

def update_flood_events(events, now, current, rain, dams, slope):
    """Registra a cheia completa acima de 4 m, sem interferir no modelo."""
    if not isinstance(events,list): events=[]
    active=None
    for e in reversed(events):
        if isinstance(e,dict) and e.get('status')=='active':
            active=e; break
    sample={
        't':now, 'level':round(float(current),4),
        'rain':dict(rain or {}), 'slope_m_per_h':round(float(slope or 0.0),6)
    }
    if active is None and current>=FLOOD_START_LEVEL:
        active={
            'id':datetime.fromtimestamp(now/1000,timezone.utc).strftime('flood-%Y%m%dT%H%M%SZ'),
            'status':'active','started_at':now,'threshold_crossed_at':now,
            'initial_level':round(float(current),4),'peak_level':round(float(current),4),'peak_at':now,
            'max_rise_rate_m_per_h':max(0.0,float(slope or 0.0)),
            'samples':[sample],'dam_events':[{'t':now,'dams':dict(dams or {})}]
        }
        events.append(active)
    elif active is not None:
        samples=active.setdefault('samples',[])
        if not samples or int(samples[-1].get('t',-1))!=now: samples.append(sample)
        if current>float(active.get('peak_level',current)):
            active['peak_level']=round(float(current),4); active['peak_at']=now
        active['max_rise_rate_m_per_h']=max(float(active.get('max_rise_rate_m_per_h',0.0)),max(0.0,float(slope or 0.0)))
        active.setdefault('dam_events',[]).append({'t':now,'dams':dict(dams or {})})
        # Encerra somente depois de cair abaixo de 3,90 m e estar sem tendência de alta.
        if current<FLOOD_END_LEVEL and slope<=0:
            active['status']='closed'; active['ended_at']=now
            active['duration_hours']=round((now-int(active['started_at']))/3600000.0,3)
            active['peak_rise_m']=round(float(active['peak_level'])-float(active['initial_level']),4)
    # Limite alto apenas para impedir crescimento ilimitado acidental.
    return events[-200:]

def nearest_actual(history,due):
    cand=[x for x in history if abs(x['t']-due)<=90*60*1000]; return min(cand,key=lambda x:abs(x['t']-due)) if cand else None

def _update_recent_bias(params, h, recent_errors):
    """Atualiza uma pequena correção de viés usando somente erros recentes.

    A correção é robusta, encolhida para zero quando há poucos casos e limitada
    por horizonte. Isso evita overfitting e é seguro para o workflow contínuo.
    """
    vals=[float(x) for x in recent_errors if isinstance(x,(int,float)) and math.isfinite(float(x))]
    if not vals:
        return
    n=len(vals)
    mean=sum(vals)/n
    # Shrinkage: com 1-10 casos o ajuste é pequeno; com 50 aproxima 35% do viés.
    strength=0.35*min(1.0,n/50.0)
    correction=-mean*strength
    limits={'2':0.10,'4':0.15,'6':0.20,'8':0.25,'12':0.30,'24':0.45}
    params.setdefault('bias_correction',{})[h]=round(clamp(correction,-limits[h],limits[h]),5)


def learn(params,snaps,history):
    total=0; abs_err={h:[] for h in ('2','4','6','8','12')}; now=int(datetime.now(timezone.utc).timestamp()*1000); remaining=[]
    for s in snaps:
        for h in ('2','4','6','8','12'):
            key='actual_'+h
            if s.get(key) is not None: continue
            due=s['t']+int(h)*3600000
            if due>now-15*60*1000: continue
            a=nearest_actual(history,due)
            if not a: continue
            pred=s.get('pred',{}); pred=pred.get(h) if isinstance(pred,dict) else None
            if not isinstance(pred,(int,float)) or not math.isfinite(pred): continue
            err=pred-a['level']; abs_err[h].append(abs(err)); total+=1; comps=s.get('components',{}).get(h,{})
            # Métricas históricas acumuladas por horizonte. O campo antigo
            # mae_m é mantido para compatibilidade, mas passa a refletir o
            # histórico acumulado quando novos resultados forem resolvidos.
            metrics=params.setdefault('metrics',{})
            by=metrics.setdefault('by_horizon',{})
            m=by.setdefault(h,{'evaluated':0,'sum_abs_error':0.0,'sum_error':0.0,'mae_m':None,'bias_m':None,'recent_abs_errors':[]})
            m['evaluated']=int(m.get('evaluated',0) or 0)+1
            m['sum_abs_error']=float(m.get('sum_abs_error',0.0) or 0.0)+abs(err)
            m['sum_error']=float(m.get('sum_error',0.0) or 0.0)+err
            m['mae_m']=round(m['sum_abs_error']/m['evaluated'],4)
            m['bias_m']=round(m['sum_error']/m['evaluated'],4)
            recent=list(m.get('recent_abs_errors',[]) or [])
            recent.append(round(abs(err),4)); m['recent_abs_errors']=recent[-50:]
            signed=list(m.get('recent_errors',[]) or [])
            signed.append(round(err,4)); m['recent_errors']=signed[-50:]
            persistence=list(m.get('recent_persistence_errors',[]) or [])
            persistence.append(round(abs(float(s.get('current',a['level']))-a['level']),4)); m['recent_persistence_errors']=persistence[-50:]
            _update_recent_bias(params,h,m['recent_errors'])
            # Aprende apenas quando a persistência realmente supera o modelo
            # recente. A diferença é convertida em um blend pequeno e limitado.
            model_mae=sum(m['recent_abs_errors'])/max(1,len(m['recent_abs_errors']))
            pers_mae=sum(m['recent_persistence_errors'])/max(1,len(m['recent_persistence_errors']))
            if len(m['recent_abs_errors'])>=20 and len(m['recent_persistence_errors'])>=20 and pers_mae+0.02 < model_mae:
                excess=(model_mae-pers_mae)/max(0.10,model_mae)
                target=clamp(0.55*excess,0.0,0.65)
            else:
                target=0.0
            old_blend=float(params.setdefault('persistence_blend',{}).get(h,0.0) or 0.0)
            params['persistence_blend'][h]=round(clamp(0.90*old_blend+0.10*target,0.0,0.65),5)
            ae=abs(err); huber_weight=1.0 if ae<=0.35 else 0.35/max(0.35,ae); lr=0.0125*huber_weight
            if ae<0.02: lr*=0.5
            def adj(name,component,lo=.50,hi=1.60):
                try:c=float(component or 0)
                except Exception:c=0.0
                if not math.isfinite(c) or abs(c)<0.005:return
                c=clamp(c,-1.0,1.0); delta=max(-0.035,min(0.035,lr*err*c)); params[name][h]=max(lo,min(hi,params[name][h]-delta))
            adj('trend_gain',comps.get('trend',0)); adj('rain_gain',comps.get('observed_rain',0)); adj('hydro_gain',comps.get('hydro',0),.35,1.50); adj('dam_gain',comps.get('dam',0),.20,2.00)
            s[key]=a['level']; s['error_'+h]=err; s['resolved_'+h]=datetime.now(timezone.utc).isoformat()
        if now-s['t']<60*24*3600000:remaining.append(s)
    params['metrics']['evaluated']=int(params['metrics'].get('evaluated',0))+total
    params['metrics'].setdefault('mae_m',{})
    for h in ('2','4','6','8','12'):
        m=(params['metrics'].get('by_horizon') or {}).get(h,{})
        if m.get('evaluated',0):
            params['metrics']['mae_m'][h]=m.get('mae_m')
        elif abs_err[h]:
            params['metrics']['mae_m'][h]=round(sum(abs_err[h])/len(abs_err[h]),4)
    return params,remaining

def clamp(x,a,b):return max(a,min(b,x))

async def main():
    os.makedirs(DATA_DIR,exist_ok=True); params=load_params(PARAMS)
    history=get_river()
    if history:
        existing=loadj(HIST,[])
        if not isinstance(existing,list):
            existing=[]

        merged={}
        for item in existing+history:
            if isinstance(item,dict) and item.get('t') is not None:
                merged[str(item['t'])]=item

        combined=sorted(
            merged.values(),
            key=lambda x: float(x.get('t',0))
        )
        savej(HIST,combined[-5000:])
    data=await get_ws(); stations=as_stations((data.get('tags_data') or {}).get('qualle_meteorologia'))
    if not stations: raise RuntimeError('WebSocket respondeu, mas nenhuma estação meteorológica foi encontrada.')
    rain_stations=[s for s in stations if isinstance(((s.get('data') or {}).get('chuva') or {}).get('acumulado'),dict)]
    print(f'Estacoes meteorologicas recebidas: {len(stations)}')
    print(f'Estacoes com bloco de chuva acumulada: {len(rain_stations)}')
    print(f'Estacoes com municipio identificado: {sum(1 for s in rain_stations if station_city(s))}')
    sample=[]
    for s in rain_stations:
        if station_city(s) in CONTRIB and len(sample)<5:
            acc=((s.get('data') or {}).get('chuva') or {}).get('acumulado') or {}
            sample.append({
                'codigo': s.get('codigo'),
                'municipio': station_city(s),
                'h003_raw': acc.get('h003'),
                'h024_raw': acc.get('h024')
            })
    if sample:
        print('Amostra bruta de chuva: '+json.dumps(sample,ensure_ascii=False,default=str))
    quality=rain_quality(rain_stations)
    spatial=spatial_rain(rain_stations)
    r={k:quality[k]['value'] for k in quality}
    # O painel público demonstra que a rede entrega acumulados. Não salvamos um
    # snapshot silenciosamente nulo: se não houver nenhum dado contribuinte,
    # a execução falha e o próximo ciclo tenta novamente.
    required=('h003','h006','h012','h024','h048','h096')
    if not all(quality[k]['cities']>0 and quality[k]['value'] is not None for k in required):
        raise RuntimeError('Dados de chuva insuficientes: '+json.dumps(quality,ensure_ascii=False))
    current=history[-1]['level'] if history else None; now=int(datetime.now(timezone.utc).timestamp()*1000)
    slope,_=trend_slope(history,now)
    # A memória hidrológica usa somente chuva observada e passa a influenciar
    # a projeção de forma limitada e progressiva conforme acumula eventos reais.
    response_state=loadj(RAIN_RESPONSE,default_rain_response())
    response_state=update_rain_response(response_state,now,current,r,slope,spatial)
    savej(RAIN_RESPONSE,response_state)
    dams=get_dams(stations); dam_now=dam_signal_now(dams)
    future={}
    damhist=loadj(DAM_HIST,[]); previous=damhist[-1].get('dams') if damhist else None; event=0.0
    if previous:
        for k in ('sul','oeste'):
            a=previous.get(k,{}).get('open_fraction',0); b=dams.get(k,{}).get('open_fraction',0)
            if b>a:event=max(event,float(b-a))
    recent_signal=0.0
    for item in reversed(damhist[-30:]):
        age_h=max(0,(now-int(item.get('t',now)))/3600000)
        if age_h>6:break
        sig=float((item.get('signal') or {}).get('event_opening',0) or 0); recent_signal=max(recent_signal,sig*math.exp(-age_h/3.0))
    dam_now['event_opening']=clamp(event,0,1); dam_now['recent_event_signal']=clamp(max(event,recent_signal),0,1)
    if current is not None:
        flood_events=loadj(FLOOD_EVENTS,[])
        flood_events=update_flood_events(flood_events,now,current,r,dam_now,slope)
        savej(FLOOD_EVENTS,flood_events)
    damhist=[x for x in damhist if int(x.get('t',-1))!=now]; damhist.append({'model_version':8,'t':now,'dams':dams,'signal':dam_now}); savej(DAM_HIST,damhist[-3000:])
    snaps=loadj(SNAP,[]); snaps=snaps if isinstance(snaps,list) else []
    dedup={int(x['t']):x for x in snaps if isinstance(x,dict) and x.get('t') is not None}; snaps=sorted(dedup.values(),key=lambda x:int(x.get('t',0)))
    params,snaps=learn(params,snaps,history)
    pred={}; components={}
    # UMA ÚNICA CURVA HIDROLÓGICA: a calculadora e o módulo de pico usam
    # exatamente os mesmos pontos horários. A regra-base é 4,5 cm/mm; o
    # nível já realizado é descontado, e o atraso/velocidade/aceleração do
    # episódio definem a distribuição temporal restante.
    unified_curve=None
    if current is not None:
        if int(response_state.get('reference_calibration',{}).get('observations',0) or 0)==0:
            response_state['reference_cm_per_mm']=4.5
        # A calibração hidrológica principal é independente do módulo de pico.
        # O pico jamais pode alterar a referência de 4,5 cm por mm.
        curve_ratio=_num(response_state.get('reference_cm_per_mm'),4.5)
        unified_curve=build_unified_curve(current,response_state.get('active'),response_state,history,now,curve_ratio,12)
        for h in ('2','4','6','8','12'):
            val=_curve_value(unified_curve,int(h))
            pred[h]=round(val,4)
            components[h]={
                'unified_curve':True,
                'curve_value':round(val,4),
                'rain_mm':round(_num(unified_curve.get('rain_mm')),3),
                'cm_per_mm':round(_num(unified_curve.get('cm_per_mm'),4.5),3),
                'total_response_m':round(_num(unified_curve.get('total_response_m')),4),
                'remaining_response_m':round(_num(unified_curve.get('remaining_m')),4),
                'response_delay_hours':round(_num(unified_curve.get('response_delay_hours')),3),
                'effective_rate_m_h':round(_num(unified_curve.get('effective_rate_m_h')),5),
                'acceleration_m_h2':round(_num(unified_curve.get('acceleration_m_h2')),5),
                'decelerating':bool(unified_curve.get('decelerating')),
            }

    # Previsão adaptativa do pico: NÃO é um segundo modelo.
    # O pico lê diretamente a mesma curva temporal unificada da calculadora.
    # ECMWF não entra.
    peak_learning=loadj(PEAK_LEARNING,default_peak_learning())
    peak_events=loadj(PEAK_EVENTS,default_peak_events())
    if not isinstance(peak_events,list): peak_events=[]
    # O módulo de pico não calcula uma previsão própria.
    # A curva unificada da calculadora é a única referência temporal ativa.
    peak_learning,peak_events,peak_status=update_peak_learning(peak_learning,peak_events,response_state,history,now,current,None,unified_curve,rain)

    # Guarda final contra estado de pico antigo/inconsistente.
    # A fonte do pico continua sendo o mesmo módulo existente.
    if (isinstance(peak_status,dict)
            and peak_status.get("active")
            and unified_curve is not None
            and current is not None
            and peak_status.get("status") not in ("pico_atingido", "rio_em_recessao")):
        curve_peak = max(float(current), float(unified_curve.get("peak_m", current)))
        peak_status["published_peak_m"] = round(curve_peak, 4)
        peak_status["raw_peak_m"] = round(curve_peak, 4)
        peak_status["temporal_peak_m"] = round(curve_peak, 4)
        if curve_peak <= float(current) + 1e-9:
            peak_status["published_peak_in_hours"] = 0.0
        else:
            peak_status["published_peak_in_hours"] = unified_curve.get("peak_in_hours")
        peak_h = float(peak_status.get("published_peak_in_hours") or 0.0)
        peak_status["projected_peak_at_ms"] = int(now + peak_h * 3600000)
        peak_status["projected_peak_at"] = datetime.fromtimestamp(
            peak_status["projected_peak_at_ms"] / 1000.0, timezone.utc
        ).isoformat()

    # Redução é um módulo separado: somente o pico já confirmado pode ativá-lo.
    # Ele não usa chuva acumulada de 24h para bloquear a recessão.
    recession = build_recession_projection(current, history, peak_status, r, now)
    if recession.get("active"):
        for h in ('2','4','6','8','12'):
            value = recession['projections'][h]
            pred[h] = value
            components[h] = {
                'recession': True,
                'curve_value': value,
                'fall_rate_m_h': recession['fall_rate_m_h'],
                'fall_rate_cm_h': recession['fall_rate_cm_h'],
                'readings_used': recession['readings_used'],
                'drop_intervals_used': recession['drop_intervals_used'],
                'rain_3h_mm': recession['rain_3h_mm'],
                'rain_24h_mm': recession['rain_24h_mm'],
            }

    savej(PEAK_LEARNING,peak_learning)
    savej(PEAK_EVENTS,peak_events[-200:])
    snaps=[x for x in snaps if int(x.get('t',-1))!=now]
    if current is not None and all(h in pred for h in ('2','4','6','8','12')):
        snaps.append({'t':now,'current':current,'rain':r,'rain_quality':quality,'spatial_rain':spatial,'future_rain':{},'dams':dam_now,'slope':slope,'pred':pred,'components':components})
    snaps=snaps[-1500:]
    params['metrics']['last_snapshot']=datetime.fromtimestamp(now/1000,timezone.utc).isoformat(); save_params(PARAMS,params)
    savej(SNAP,snaps)
    # Publica uma projeção já calculada pelo mesmo modelo do workflow. O site
    # pode consumi-la diretamente, evitando divergência entre Python e JS.
    river_timestamp=(history[-1].get('t') if history else now)
    projection_payload={
        'version':2,
        'generated_at':datetime.fromtimestamp(now/1000,timezone.utc).isoformat(),
        'river_timestamp':datetime.fromtimestamp(river_timestamp/1000,timezone.utc).isoformat(),
        'current':current,
        'pred':pred,
        'components':components,
        'peak_estimate':peak_status,
        'model':{
            'trend_gain':params.get('trend_gain',{}),
            'rain_gain':params.get('rain_gain',{}),
            'hydro_gain':params.get('hydro_gain',{}),
            'dam_gain':params.get('dam_gain',{}),
            'bias_correction':params.get('bias_correction',{}),
            'persistence_blend':params.get('persistence_blend',{}),
            'damping':params.get('damping',{}),
            'hydrological_reference_cm_per_mm':response_state.get('reference_cm_per_mm',4.5),
            'hydrological_reference_calibration':response_state.get('reference_calibration',{}),
            'hydrological_reference_window_hours':response_state.get('reference_rain_window_hours',48),
            'active_prediction_horizons_hours':[2,4,6,8,12],
            'recession':recession,
            'peak_uses_24h_prediction':False,
            'learned_delay_hours':response_state.get('learned_delay_hours'),
            'evaluated':params.get('metrics',{}).get('evaluated',0)
        }
    }
    savej(os.path.join(DATA_DIR,'projection.json'),projection_payload)
    metrics_by=params['metrics'].get('by_horizon',{})
    savej(os.path.join(DATA_DIR,'learning_status.json'),{'version':12,'updated_at':params['updated_at'],'evaluated':params['metrics']['evaluated'],'mae_m':params['metrics']['mae_m'],'metrics_by_horizon':metrics_by,'trend_gain':params['trend_gain'],'rain_gain':params['rain_gain'],'hydro_gain':params.get('hydro_gain',{}),'forecast_rain_gain':params['forecast_rain_gain'],'dam_gain':params['dam_gain'],'bias_correction':params.get('bias_correction',{}),'persistence_blend':params.get('persistence_blend',{}),'damping':params['damping'],'rain_quality':quality,'spatial_rain':spatial,'rain_response':response_state,'peak_estimate':peak_status,'recession':recession,'peak_learning':{'evaluated_events':peak_learning.get('evaluated_events',0),'mae_peak_m':peak_learning.get('mae_peak_m'),'reference_cm_per_mm':peak_learning.get('reference_cm_per_mm',4.5)}})
    print(json.dumps({'current':current,'rain':r,'rain_quality':quality,'future_rain':future,'dams':dam_now,'slope':slope,'pred':pred,'evaluated':params['metrics']['evaluated']},ensure_ascii=False))

if __name__=='__main__': asyncio.run(main())
