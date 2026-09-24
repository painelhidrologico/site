import json, math, os
from datetime import datetime, timezone

DEFAULT = {
  "version": 9,
  "trend_gain": {"2":1.0,"4":1.0,"6":1.0,"8":1.0,"12":1.0,"24":1.0},
  "rain_gain": {"2":1.0,"4":1.0,"6":1.0,"8":1.0,"12":1.0,"24":1.0},
  "hydro_gain": {"2":1.0,"4":1.0,"6":1.0,"8":1.0,"12":1.0,"24":1.0},
  "forecast_rain_gain": {"2":0.0,"4":0.0,"6":0.0,"8":0.0,"12":0.0,"24":0.0},
  "dam_gain": {"2":1.0,"4":1.0,"6":1.0,"8":1.0,"12":1.0,"24":1.0},
  "bias_correction": {"2":0.0,"4":0.0,"6":0.0,"8":0.0,"12":0.0,"24":0.0},
  "persistence_blend": {"2":0.0,"4":0.0,"6":0.0,"8":0.0,"12":0.0,"24":0.0},
  "damping": {"2":0.88,"4":0.78,"6":0.70,"8":0.62,"12":0.55,"24":0.40},
  "metrics": {"evaluated":0,"mae_m":{"2":None,"4":None,"6":None,"8":None,"12":None,"24":None},"by_horizon":{"2":{"evaluated":0,"sum_abs_error":0.0,"sum_error":0.0,"mae_m":None,"bias_m":None,"recent_abs_errors":[]},"4":{"evaluated":0,"sum_abs_error":0.0,"sum_error":0.0,"mae_m":None,"bias_m":None,"recent_abs_errors":[]},"6":{"evaluated":0,"sum_abs_error":0.0,"sum_error":0.0,"mae_m":None,"bias_m":None,"recent_abs_errors":[]},"8":{"evaluated":0,"sum_abs_error":0.0,"sum_error":0.0,"mae_m":None,"bias_m":None,"recent_abs_errors":[]},"12":{"evaluated":0,"sum_abs_error":0.0,"sum_error":0.0,"mae_m":None,"bias_m":None,"recent_abs_errors":[]},"24":{"evaluated":0,"sum_abs_error":0.0,"sum_error":0.0,"mae_m":None,"bias_m":None,"recent_abs_errors":[]}},"last_update":None},
  "updated_at": None
}

HORIZONS=('2','4','6','8','12')

def clamp(x,a,b): return max(a,min(b,x))
def finite(x): return isinstance(x,(int,float)) and math.isfinite(x)

def load_params(path):
    try:
        with open(path,encoding='utf-8') as f: d=json.load(f)
    except Exception: d={}
    out=json.loads(json.dumps(DEFAULT))
    for k in ('trend_gain','rain_gain','hydro_gain','forecast_rain_gain','dam_gain','bias_correction','persistence_blend','damping'):
        if isinstance(d.get(k),dict):
            for h in HORIZONS:
                if finite(d[k].get(h)): out[k][h]=d[k][h]
    if isinstance(d.get('metrics'),dict):
        # Mantém compatibilidade com métricas antigas e adiciona contadores
        # históricos por horizonte sem perder os dados existentes.
        for key in ('evaluated','mae_m','last_update','last_snapshot'):
            if key in d['metrics']:
                out['metrics'][key]=d['metrics'][key]
        old_by=d['metrics'].get('by_horizon')
        if isinstance(old_by,dict):
            for h in HORIZONS:
                if isinstance(old_by.get(h),dict):
                    out['metrics']['by_horizon'][h].update(old_by[h])
    return out

def save_params(path,p):
    p['updated_at']=datetime.now(timezone.utc).isoformat()
    p['metrics']['last_update']=p['updated_at']
    tmp=path+'.tmp'
    with open(tmp,'w',encoding='utf-8') as f: json.dump(p,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)

def weighted_observed_rain(r3,r6,r12,r24,r48=0,r96=0,horizon=24):
    vals=[max(0,float(x or 0)) for x in (r3,r6,r12,r24,r48,r96)]
    v3,v6,v12,v24,v48,v96=vals
    v6=max(v3,v6); v12=max(v6,v12); v24=max(v12,v24); v48=max(v24,v48); v96=max(v48,v96)
    # Chuva observada exclusivamente dos pluviômetros.
    # Cada horizonte usa somente a janela observada que já existia naquele horizonte;
    # chuva de 48/96h não pode contaminar uma previsão de 6/12/24h.
    h=int(horizon)
    effective=v3*1.00+(v6-v3)*0.90
    if h>=12:
        effective+=(v12-v6)*0.65
    if h>=24:
        effective+=(v24-v12)*0.45
    if h>=48:
        effective+=(v48-v24)*0.25
    if h>=96:
        effective+=(v96-v48)*0.10
    return max(0,effective*(5.90/130.0)*0.30)

def weighted_rain(*args):
    # Compatibilidade com chamadas antigas.
    if len(args)==4:
        r6,r12,r24,r48=args
        return weighted_observed_rain(0,r6,r12,r24,r48,r48,horizon=24)
    return weighted_observed_rain(*args,horizon=24)

def dam_effect(dams, horizon):
    """Sinal conservador baseado apenas em evento operacional observado.

    A abertura estática das comportas não é tratada como vazão nem como subida
    automática em Blumenau. Só há componente quando existe vertimento ativo
    ou alteração recente de abertura. O ganho é ajustado pelo aprendizado.
    """
    if not isinstance(dams,dict): return 0.0
    h=str(horizon)
    try: spill_active=1.0 if float(dams.get('spill_active',0) or 0)>0 else 0.0
    except: spill_active=0.0
    try: event=clamp(float(dams.get('event_opening',0) or 0),0,1)
    except: event=0.0
    try: recent_event=clamp(float(dams.get('recent_event_signal',0) or 0),0,1)
    except: recent_event=0.0
    signal=0.20*spill_active + 0.35*max(event,recent_event)
    factor={'2':0.05,'4':0.08,'6':0.10,'12':0.20,'24':0.32}[h]
    return clamp(signal,0,1.0)*factor

def trend_slope(history, now_ms):
    """Calcula uma tendência conservadora das últimas 6 horas.

    Mantém a tendência geral, mas dá mais peso aos movimentos recentes. Isso
    permite reagir gradualmente à aceleração, desaceleração e início de uma
    reversão sem mudar a arquitetura do modelo ou os dados de aprendizado.
    """
    window=6*3600*1000
    recent=sorted(
        [x for x in history if 0 <= now_ms-x['t'] <= window],
        key=lambda x:x['t']
    )
    if len(recent)<2:return 0.0,0

    first,last=recent[0],recent[-1]
    hours=max(.25,(last['t']-first['t'])/3600000)
    overall=(last['level']-first['level'])/hours

    # Inclinações entre leituras consecutivas. Leituras mais recentes recebem
    # peso maior, mas nenhuma leitura isolada domina a tendência.
    segments=[]
    for a,b in zip(recent,recent[1:]):
        dt=(b['t']-a['t'])/3600000
        if dt<=0: continue
        s=(b['level']-a['level'])/dt
        segments.append(s)
    if not segments:return overall,len(recent)

    weights=list(range(1,len(segments)+1))
    recent_slope=sum(s*w for s,w in zip(segments,weights))/sum(weights)

    # Base híbrida: preserva a visão de 6h, mas reage mais ao comportamento
    # recente. Isso reduz o atraso quando a subida perde força ou se inverte.
    slope=0.55*overall+0.45*recent_slope

    # Se a tendência recente ainda tem o mesmo sentido, porém está perdendo
    # força, aplica uma redução suave. Ex.: +30,+25,+20,+10,+10.
    if overall*recent_slope>0 and abs(recent_slope)<abs(overall):
        ratio=abs(recent_slope)/max(abs(overall),1e-9)
        slowdown=0.70+0.30*ratio
        slope*=slowdown

    # Em reversão real, o sinal recente recebe mais peso, mas sem trocar para
    # um valor extremo por causa de uma única leitura.
    if overall*recent_slope<0:
        slope=0.30*overall+0.70*recent_slope

    return slope,len(recent)

def project(current, rainp, slope, horizon, params, future_rain=None, dams=None, hydro_component=0.0):
    h=str(horizon)
    raw_trend=slope*float(horizon)
    trend_component=clamp(
        raw_trend,
        -0.10 if h=='2' else (-0.20 if h=='4' else (-0.30 if h=='6' else (-0.45 if h=='12' else -0.60))),
        0.20 if h=='2' else (0.40 if h=='4' else (0.60 if h=='6' else (1.00 if h=='12' else 1.40)))
    )
    trend_component*=params['trend_gain'][h]*params['damping'][h]
    response_factor={'2':0.07,'4':0.12,'6':0.18,'12':0.38,'24':0.72}[h]
    # rainp pode ser um escalar legado; chamadas novas passam o valor específico do horizonte.
    rain_component=rainp*response_factor*params['rain_gain'][h]
    # IMPORTANTE: previsão ECMWF NÃO participa da calculadora. ECMWF é apenas informativo.
    forecast_component=0.0
    dam_signal=dam_effect(dams,h)
    dam_component=dam_signal*params['dam_gain'][h]
    # Componente hidrológico aprendido exclusivamente com chuva observada e
    # respostas reais do rio. É limitado antes de chegar aqui e ganha ajuste
    # gradual pelos próprios erros da projeção.
    try:
        hydro_component=float(hydro_component or 0.0)*params.get('hydro_gain',{}).get(h,1.0)
    except Exception:
        hydro_component=0.0
    # Permite a referência hidrológica de um evento excepcional atravessar
    # o horizonte sem um teto fixo de 1,20 m. A componente aprendida é
    # limitada antes de chegar aqui, preservando a proteção do aprendizado.
    hydro_component=clamp(hydro_component,-0.20,8.00)
    # Correção de viés aprendida a partir dos erros recentes. É deliberadamente
    # pequena e limitada por horizonte para não deixar uma sequência curta de
    # erros deslocar toda a curva de projeção. Erro positivo = previsão acima
    # do real; portanto a correção armazenada já vem com o sinal apropriado.
    bias_component=clamp(float(params.get('bias_correction',{}).get(h,0.0) or 0.0),
                         -0.45 if h=='24' else (-0.30 if h=='12' else (-0.20 if h=='6' else (-0.15 if h=='4' else -0.10))),
                         0.45 if h=='24' else (0.30 if h=='12' else (0.20 if h=='6' else (0.15 if h=='4' else 0.10))))
    total=current+trend_component+rain_component+hydro_component+forecast_component+dam_component+bias_component
    # Se o histórico recente mostra que a projeção está pior que a persistência,
    # o aprendizado pode aproximar a saída do nível atual. O peso é pequeno e
    # limitado para evitar que uma sequência curta domine o modelo.
    blend=clamp(float(params.get('persistence_blend',{}).get(h,0.0) or 0.0),0.0,0.65)
    total=current+(total-current)*(1.0-blend)
    return max(0,total),trend_component,rain_component,forecast_component,dam_component,hydro_component
