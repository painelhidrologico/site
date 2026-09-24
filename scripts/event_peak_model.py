import copy, json, math, os
from datetime import datetime, timezone

PEAK_VERSION = 3
UPDATE_HOURS = 1.0
UPDATE_INTERVAL_MINUTES = 60
MIN_UPDATE_MINUTES = 55.0
MAX_HISTORY_HOURS = 72.0
POST_PEAK_HOURS = 24.0
POST_PEAK_NEW_RAIN_MM = 5.0  # gatilho: 5 mm em 3 horas
POST_PEAK_RISE_M = 0.02
POST_PEAK_NEW_PEAK_MARGIN_M = 0.01


def _num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _clamp(x, a, b):
    return max(a, min(b, x))


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).isoformat()


def default_peak_learning():
    return {
        "version": PEAK_VERSION,
        "evaluated_events": 0,
        "reference_cm_per_mm": 4.5,
        "sum_abs_peak_error_m": 0.0,
        "mae_peak_m": None,
        "recent_errors_m": [],
        "active": None,
        "last_completed": None,
        "updated_at": None,
    }


def default_peak_events():
    return []


def _recent_history(history, started_at, now):
    out = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        t = int(row.get("t", 0) or 0)
        level = _num(row.get("level"), float("nan"))
        if not math.isfinite(level):
            continue
        if started_at <= t <= now and now - t <= MAX_HISTORY_HOURS * 3600000:
            out.append({"t": t, "level": level})
    return sorted(out, key=lambda x: x["t"])


def _rates(samples):
    """Retorna velocidades em janelas curtas/médias/longas e aceleração."""
    def rate(hours):
        cutoff = samples[-1]["t"] - hours * 3600000
        pts = [x for x in samples if x["t"] >= cutoff]
        if len(pts) < 2:
            return None
        a, b = pts[0], pts[-1]
        dt = max(0.25, (b["t"] - a["t"]) / 3600000.0)
        return (b["level"] - a["level"]) / dt

    r1 = rate(1.5)
    r3 = rate(3.0)
    r6 = rate(6.0)
    r12 = rate(12.0)
    vals = [x for x in (r1, r3, r6, r12) if x is not None]
    if not vals:
        return {"r1": 0.0, "r3": 0.0, "r6": 0.0, "r12": 0.0, "accel": 0.0, "decelerating": False}

    r1 = r1 if r1 is not None else vals[-1]
    r3 = r3 if r3 is not None else r1
    r6 = r6 if r6 is not None else r3
    r12 = r12 if r12 is not None else r6
    accel = (r1 - r6) / max(1.0, 6.0 - 1.5)
    return {
        "r1": r1, "r3": r3, "r6": r6, "r12": r12,
        "accel": accel,
        "decelerating": r6 > 0 and r1 < r6,
    }


def _response_fraction(age_h, delay_h):
    # Curva conservadora: início lento, acelera no miolo e desacelera perto do fim.
    points = [(0, 0.0), (1, 0.03), (2, 0.06), (4, 0.10), (6, 0.15),
              (12, 0.28), (24, 0.62), (36, 0.84), (48, 1.0)]
    x = max(0.0, age_h - max(0.0, delay_h))
    if x >= points[-1][0]:
        return 1.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            f = (x - x0) / max(1e-9, x1 - x0)
            return y0 + f * (y1 - y0)
    return 1.0


def _estimate_raw_legacy_unused(current, active, state, samples, learned_24h=None, peak_reference_cm_per_mm=4.5):
    baseline = _num(active.get("baseline_level"), current)
    if "event_rain_mm_h048" in active or "event_rain_mm_h096" in active:
        mm = max(_num(active.get("event_rain_mm_h048")), _num(active.get("event_rain_mm_h096")))
    else:
        # Compatibilidade com eventos antigos já gravados.
        mm = max(_num(active.get("max_rain_h048")), _num(active.get("max_rain_h024")))
    cm_per_mm = _clamp(_num(peak_reference_cm_per_mm, 4.5), 2.0, 6.0)
    # 4,5 cm/mm é o prior inicial pedido para o novo módulo. Se a memória
    # histórica já calibrou a referência, ela passa a ser usada.
    reference_total = baseline + mm * cm_per_mm / 100.0
    realized = max(0.0, (current - baseline) * 100.0)
    remaining_reference = max(0.0, (reference_total - current))

    age_h = max(0.0, (samples[-1]["t"] - int(active.get("started_at", samples[-1]["t"]))) / 3600000.0) if samples else 0.0
    delay = _num(active.get("response_delay_hours"), _num(state.get("learned_delay_hours"), 5.0))
    frac_now = _response_fraction(age_h, delay)
    # Não repete chuva: a resposta total é calculada uma vez e desconta o que já subiu.
    response_total = mm * cm_per_mm / 100.0
    expected_now = response_total * frac_now
    remaining_curve = max(0.0, expected_now - realized)

    rates = _rates(samples) if len(samples) >= 2 else {"r1": 0.0, "r3": 0.0, "r6": 0.0, "r12": 0.0, "accel": 0.0, "decelerating": False}
    effective_rate = max(0.0, 0.45 * rates["r1"] + 0.35 * rates["r3"] + 0.20 * rates["r6"])

    # Estimativa temporal independente: quanto o ritmo atual ainda pode acrescentar.
    # O horizonte cai quando a aceleração já está negativa de forma consistente.
    if rates["decelerating"] and rates["accel"] < -0.005:
        time_to_zero = _clamp(effective_rate / max(0.005, -rates["accel"]), 0.5, 18.0)
    else:
        # Usa o tempo restante da curva de resposta, sem assumir que a chuva vira nível imediatamente.
        time_to_zero = _clamp((1.0 - frac_now) * max(6.0, delay + 24.0), 1.0, 36.0)
        if rates["accel"] > 0.01:
            time_to_zero = min(36.0, time_to_zero * 1.20)
    temporal_total = current + effective_rate * time_to_zero

    # O componente histórico da calculadora é uma referência, não é somado à regra 4,5.
    learned_candidate = None
    if isinstance(learned_24h, (int, float)) and math.isfinite(learned_24h):
        learned_candidate = float(learned_24h)

    # Peso muda com evidência: início = prior; mais dados = curva observada.
    sample_count = len(samples)
    response_strength = _clamp((sample_count - 3) / 12.0, 0.0, 1.0)
    confidence = _clamp(0.18 + 0.035 * min(sample_count, 18), 0.18, 0.82)
    if realized > 0:
        confidence = min(0.88, confidence + 0.08)
    if rates["decelerating"]:
        confidence = min(0.92, confidence + 0.07)

    candidates = [(reference_total, 0.48 * (1.0 - response_strength) + 0.22),
                  (current + max(remaining_curve, remaining_reference), 0.22 + 0.28 * response_strength),
                  (temporal_total, 0.18 + 0.24 * response_strength)]
    if learned_candidate is not None:
        candidates.append((learned_candidate, 0.12 + 0.18 * response_strength))
    weight_sum = sum(max(0.0, w) for _, w in candidates)
    raw_peak = sum(v * max(0.0, w) for v, w in candidates) / max(1e-9, weight_sum)
    raw_peak = max(current, raw_peak)

    # Perto de uma desaceleração consistente, não deixa o prior de chuva forçar uma subida excessiva.
    if rates["decelerating"] and rates["accel"] < -0.01:
        raw_peak = min(raw_peak, current + max(0.03, effective_rate * time_to_zero * 1.25))

    peak_in_hours = _clamp(time_to_zero, 0.5, 48.0)
    if effective_rate <= 0.005 and current <= baseline + 0.02:
        peak_in_hours = None
        raw_peak = max(current, min(reference_total, current + 0.05))

    return {
        "raw_peak_m": raw_peak,
        "reference_peak_m": reference_total,
        "temporal_peak_m": temporal_total,
        "remaining_reference_m": remaining_reference,
        "remaining_curve_m": remaining_curve,
        "effective_rate_m_h": effective_rate,
        "acceleration_m_h2": rates["accel"],
        "decelerating": rates["decelerating"],
        "peak_in_hours": peak_in_hours,
        "confidence": confidence,
        "sample_count": sample_count,
        "realized_rise_m": max(0.0, current - baseline),
        "rain_mm": mm,
        "cm_per_mm": cm_per_mm,
    }


def _smooth(previous, raw, confidence, decelerating):
    if previous is None:
        return raw
    delta = raw - previous
    # Limite adaptativo. Mais confiança/evidência permite uma correção maior.
    max_change = 0.10 + 0.16 * _clamp(confidence, 0.0, 1.0)
    if decelerating:
        max_change += 0.05
    max_change = _clamp(max_change, 0.10, 0.30)
    if abs(delta) <= max_change:
        return raw
    return previous + math.copysign(max_change, delta)


def _smooth_time(previous_h, raw_h, confidence, decelerating):
    if previous_h is None or raw_h is None:
        return raw_h
    max_change = 1.0 + 1.5 * _clamp(confidence, 0.0, 1.0)
    if decelerating:
        max_change += 0.5
    return previous_h + _clamp(raw_h - previous_h, -max_change, max_change)



def _curve_value(c, h):
    """Interpolate one hour from the unified hydrological curve."""
    pts=c.get("points") or []
    if not pts:
        return _num(c.get("current_level_m"), 0.0)
    x=max(0.0,float(h))
    if x<=pts[0][0]: return float(pts[0][1])
    for (x0,y0),(x1,y1) in zip(pts,pts[1:]):
        if x<=x1:
            f=(x-x0)/max(1e-9,x1-x0)
            return y0+f*(y1-y0)
    return float(pts[-1][1])

def build_unified_curve(current, active, response_state, history, now, cm_per_mm=4.5, max_hours=12):
    """Constrói UMA curva futura usada tanto pela calculadora quanto pelo pico.

    A regra de 4,5 cm/mm define o potencial total do episódio. O nível já
    realizado é descontado antes da projeção. O atraso aprendido desloca a
    resposta no tempo; a velocidade e a aceleração/desaceleração observadas
    definem a forma e o tempo restante da curva. Não há segunda soma de chuva.
    """
    max_hours = min(float(max_hours), 12.0)
    current=float(current)
    active=active if isinstance(active,dict) else {}
    response_state=response_state if isinstance(response_state,dict) else {}
    started=int(active.get("started_at",now) or now)
    samples=_recent_history(history,started,now)
    if not samples or samples[-1]["t"]<now:
        samples.append({"t":int(now),"level":current})
    rates=_rates(samples) if len(samples)>=2 else {"r1":0.0,"r3":0.0,"r6":0.0,"r12":0.0,"accel":0.0,"decelerating":False}
    effective_rate=max(0.0,0.45*rates["r1"]+0.35*rates["r3"]+0.20*rates["r6"])

    if "event_rain_mm_h048" in active or "event_rain_mm_h096" in active:
        rain_mm=max(_num(active.get("event_rain_mm_h048")),_num(active.get("event_rain_mm_h096")))
    else:
        rain_mm=max(_num(active.get("max_rain_h048")),_num(active.get("max_rain_h024")))
    ratio=_clamp(_num(cm_per_mm,4.5),2.0,6.0)
    baseline=_num(active.get("baseline_level"),current)
    total_level=baseline+rain_mm*ratio/100.0
    realized=max(0.0,current-baseline)
    remaining=max(0.0,total_level-current)

    # Fora de um evento de chuva, não há potencial chuva->rio a projetar.
    # Mantemos apenas a tendência observada, com amortecimento temporal.
    if rain_mm<=0.0 or remaining<=0.0001:
        trend=effective_rate
        if trend<=0.0:
            pts=[(0.0,current),(float(max_hours),current)]
            return {"points":pts,"peak_m":current,"peak_in_hours":0.0,"rain_mm":rain_mm,"cm_per_mm":ratio,"remaining_m":0.0,"effective_rate_m_h":trend,"acceleration_m_h2":rates["accel"],"decelerating":rates["decelerating"],"baseline_level_m":baseline,"response_delay_hours":_num(response_state.get("learned_delay_hours"),_num(response_state.get("reference_delay_hours"),5.0))}
        T=max(6.0,min(float(max_hours),max(12.0,remaining/max(trend,0.001))))
    else:
        # Atraso aprendido é usado para o tempo de resposta; o estado atual
        # já está dentro da resposta, então o restante é distribuído a partir
        # do nível atual e nunca é contado novamente.
        delay=_num(active.get("response_delay_hours"),_num(response_state.get("learned_delay_hours"),_num(response_state.get("reference_delay_hours"),5.0)))
        age_h=max(0.0,(now-started)/3600000.0)
        response_remaining=max(0.0,48.0-max(0.0,age_h-delay))
        if effective_rate>0.0005:
            # Com desaceleração confirmada, a curva é alongada para que a
            # perda de velocidade afete o horário do pico sem apagar o
            # potencial hidrológico ainda não realizado.
            factor=2.0 if rates["decelerating"] else (1.25 if rates["accel"]>0.005 else 1.6)
            T=max(8.0,remaining/effective_rate*factor)
            if response_remaining>0:
                T=max(T,min(72.0,response_remaining*1.15))
        else:
            T=max(12.0,min(72.0,response_remaining if response_remaining>0 else 36.0))
        T=min(float(max_hours),T)

    # Curva cúbica Hermite: começa com a velocidade observada e termina com
    # velocidade zero exatamente no pico. O tempo é aumentado até a curva
    # ficar monotônica, preservando desaceleração sem criar uma nova subida
    # artificial.
    v0=max(0.0,effective_rate)
    if remaining<=0.0001:
        pts=[(0.0,current),(0.0,current)]
        return {"points":pts,"peak_m":current,"peak_in_hours":0.0,"rain_mm":rain_mm,"cm_per_mm":ratio,"remaining_m":0.0,"effective_rate_m_h":v0,"acceleration_m_h2":rates["accel"],"decelerating":rates["decelerating"],"baseline_level_m":baseline,"response_delay_hours":_num(active.get("response_delay_hours"),_num(response_state.get("learned_delay_hours"),5.0))}

    def coeffs(T):
        # y=c+v0*t+a*t²+b*t³; y(T)=c+remaining; y'(T)=0
        a=(3.0*remaining-2.0*v0*T)/(T*T)
        b=(v0*T-2.0*remaining)/(T*T*T)
        return a,b

    def min_rate(T,a,b):
        vals=[]
        for i in range(0,97):
            t=T*i/96.0
            vals.append(v0+2*a*t+3*b*t*t)
        return min(vals)

    for _ in range(12):
        a,b=coeffs(T)
        if min_rate(T,a,b)>=-1e-5: break
        T=min(float(max_hours),T*1.25)
        if T>=max_hours: break
    a,b=coeffs(T)

    pts=[]
    for i in range(int(max_hours)+1):
        t=float(i)
        if t>=T:
            y=total_level
        else:
            y=current+v0*t+a*t*t+b*t*t*t
            y=max(current,min(total_level,y))
        pts.append((t,y))

    # O pico operacional é procurado somente no horizonte futuro de 2h a 12h.
    # As projeções 2/4/6/12 e o pico continuam saindo da mesma curva.
    peak_candidates=[(t,y) for t,y in pts if 2.0 <= t <= 12.0]
    if not peak_candidates:
        peak_candidates=[(12.0, pts[-1][1])]
    peak_m=max(y for _,y in peak_candidates)
    peak_h=next((t for t,y in peak_candidates if y>=peak_m-1e-9),12.0)
    return {
        "points":pts,
        "peak_m":peak_m,
        "peak_in_hours":peak_h,
        "rain_mm":rain_mm,
        "cm_per_mm":ratio,
        "remaining_m":remaining,
        "effective_rate_m_h":v0,
        "acceleration_m_h2":rates["accel"],
        "decelerating":rates["decelerating"],
        "baseline_level_m":baseline,
        "response_delay_hours":_num(active.get("response_delay_hours"),_num(response_state.get("learned_delay_hours"),5.0)),
        "total_response_m":rain_mm*ratio/100.0,
    }


def _post_peak_state(last_event, history, now, current, rain=None, previous_post=None, rain_history=None):
    """Mantém o último pico visível por até 24h sem chuva nova.

    Chuva >= 5 mm em 3h abre novo ciclo. Sem esse gatilho, o pico permanece
    registrado até o encerramento do evento.
    """
    if not isinstance(last_event, dict) or current is None:
        return None

    peak_at = int(last_event.get("peak_at") or 0)
    peak = _num(last_event.get("peak_level"), current)
    if peak_at <= 0:
        # Eventos de chuva antigos não guardavam o horário exato do máximo.
        # Recupera-o da série observada pelo primeiro ponto mais próximo do pico.
        candidates = []
        started = int(last_event.get("rain_started_at") or 0)
        ended = int(last_event.get("completed_at") or now)
        for row in history or []:
            if not isinstance(row, dict):
                continue
            t = int(row.get("t", 0) or 0)
            lv = _num(row.get("level"), float("nan"))
            if math.isfinite(lv) and started <= t <= ended:
                candidates.append((abs(lv - peak), t))
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1]))
            peak_at = candidates[0][1]
        else:
            peak_at = ended or now
    age_h = max(0.0, (now - peak_at) / 3600000.0)
    if age_h > POST_PEAK_HOURS:
        return None

    rain = rain if isinstance(rain, dict) else {}
    r3 = max(0.0, _num(rain.get("h003")))
    r6 = max(0.0, _num(rain.get("h006")))
    r24 = max(0.0, _num(rain.get("h024")))

    # Gatilho de novo ciclo: somente chuva >= 5 mm em 3h. Não usamos
    # integração cumulativa, pois h003 já representa a janela móvel observada.
    rain_since_peak = r3
    new_rain = r3 >= POST_PEAK_NEW_RAIN_MM

    pts = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        t = int(row.get("t", 0) or 0)
        lv = _num(row.get("level"), float("nan"))
        if not math.isfinite(lv) or t > now or now - t > 6 * 3600000:
            continue
        pts.append((t, lv))
    pts.sort()
    rates = _rates([{"t":t,"level":lv} for t,lv in pts]) if len(pts) >= 2 else {"r1":0.0,"r3":0.0,"r6":0.0}
    recent_rise = False
    if len(pts) >= 2:
        recent_rise = (pts[-1][1] - pts[0][1]) >= POST_PEAK_RISE_M and rates["r3"] > 0
    # Uma nova elevação pode ser detectada mesmo sem chuva registrada.
    new_rise = recent_rise

    peak_iso = last_event.get("peak_at_iso") or _iso(peak_at)
    event_id = str(last_event.get("completed_at") or last_event.get("rain_started_at") or peak_at or now)
    fallen = max(0.0, peak - float(current))

    if new_rain:
        # Novo ciclo somente após >=5 mm de chuva em 3h.
        # A oscilação natural do rio, sozinha, não abre um novo ciclo.
        rate = max(0.0, 0.55 * rates.get("r1",0.0) + 0.30 * rates.get("r3",0.0) + 0.15 * rates.get("r6",0.0))
        trend_h = 6.0 if rate > 0 else None
        trend_peak = float(current) + rate * trend_h if trend_h is not None else float(current)
        new_peak = float(current) > peak + POST_PEAK_NEW_PEAK_MARGIN_M
        return {
            "active": True,
            "mode": "novo_pico" if new_peak else "nova_elevacao",
            "status": "novo_pico" if new_peak else "nova_elevacao",
            "current_level_m": round(float(current),4),
            "previous_peak_m": round(peak,4),
            "previous_peak_at": peak_iso,
            "peak_age_hours": round(age_h,2),
            "new_rain_detected": bool(new_rain),
            "new_rise_detected": bool(new_rise),
            "rain_24h_mm": round(r24,3),
            "rain_since_peak_mm": round(rain_since_peak,3),
        "rain_3h_mm": round(r3,3),
            "rain_since_peak_last_at": now,
            "rain_since_peak_last_h003": round(r3,3),
            "rain_since_peak_started_at": now,
            "effective_rate_m_h": round(rate,5),
            "projected_peak_m": round(max(float(current), trend_peak),4),
            "projected_peak_in_hours": trend_h,
            "published_peak_m": round(max(float(current), trend_peak), 4),
            "published_peak_in_hours": trend_h,
            "peak_window_hours": 1.0,
            "update_interval_minutes": UPDATE_INTERVAL_MINUTES,
            "confidence": 0.45 if rate > 0 else 0.2,
            "trigger": "rain_observed" if new_rain else "river_rise_without_local_rain",
            "message": ("Novo pico observado; novo ciclo iniciado." if new_peak else
                         "Nova elevação detectada sem depender de chuva local; novo pico em projeção.")
        }

    # Em recessão, NÃO substituímos a última previsão do pico pelo pico
    # observado. O card e o histórico precisam conservar a previsão emitida
    # antes da confirmação, inclusive o horário previsto.
    projected_peak_m = None
    projected_peak_in_hours = None
    projected_peak_at_ms = None
    projected_peak_at = None
    if isinstance(previous_post, dict):
        projected_peak_m = previous_post.get("published_peak_m")
        projected_peak_in_hours = previous_post.get("published_peak_in_hours")
        projected_peak_at_ms = previous_post.get("projected_peak_at_ms")
        projected_peak_at = previous_post.get("projected_peak_at")
        if projected_peak_at_ms is None:
            projected_peak_at_ms = previous_post.get("peak_projection_at_confirmation_at_ms")
        if projected_peak_at is None:
            projected_peak_at = previous_post.get("peak_projection_at_confirmation_at")
        if projected_peak_m is None:
            projected_peak_m = previous_post.get("peak_projection_at_confirmation_m")
        if projected_peak_in_hours is None:
            projected_peak_in_hours = previous_post.get("peak_projection_at_confirmation_in_hours")

    return {
        "active": True,
        "event_id": event_id,
        "mode": "recessao",
        "status": "rio_em_recessao",
        "current_level_m": round(float(current),4),
        "peak_level_m": round(peak,4),
        "peak_at": peak_iso,
        "published_peak_m": round(_num(projected_peak_m, peak), 4),
        "published_peak_in_hours": round(_num(projected_peak_in_hours, 0.0), 4),
        "projected_peak_at_ms": projected_peak_at_ms,
        "projected_peak_at": projected_peak_at,
        "peak_window_hours": 0.0,
        "confidence": 0.9,
        "update_interval_minutes": UPDATE_INTERVAL_MINUTES,
        "peak_at_ms": peak_at,
        "peak_age_hours": round(age_h,2),
        "fallen_m": round(fallen,4),
        "fallen_cm": round(fallen*100.0,1),
        "remaining_post_peak_hours": round(max(0.0, POST_PEAK_HOURS-age_h),2),
        "rain_24h_mm": round(r24,3),
        "rain_since_peak_mm": round(rain_since_peak,3),
        "rain_3h_mm": round(r3,3),
        "rain_since_peak_last_at": now,
        "rain_since_peak_last_h003": round(r3,3),
            "rain_since_peak_started_at": now,
        "new_rain_detected": False,
        "new_rise_detected": False,
        "message": "Pico confirmado. Mantido até o encerramento do evento sem gatilho de 5 mm em 3h."
    }

def _detect_observed_peak(history, started_at, now, current):
    """Confirma um pico REAL durante o evento, sem esperar o encerramento.

    O pico só é confirmado depois que o máximo observado deixa de ser o nível
    atual e a queda fica consistente. Isso evita declarar pico por uma única
    oscilação de leitura.
    """
    samples = _recent_history(history, started_at, now)
    if len(samples) < 3 or current is None:
        return None
    peak_sample = max(samples, key=lambda x: (x["level"], -x["t"]))
    peak_level = float(peak_sample["level"])
    peak_at = int(peak_sample["t"])
    age_min = (int(now) - peak_at) / 60000.0
    rates = _rates(samples)
    # Confirmação exige pelo menos 30 min desde o máximo e queda de 1 cm,
    # além de tendência negativa na janela curta. A janela de 1,5 h torna a
    # decisão resistente a pequenas oscilações do sensor.
    if age_min < 30.0:
        return None
    if peak_level - float(current) < 0.01:
        return None
    if rates.get("r1", 0.0) > -0.0005:
        return None
    return {
        "peak_level_m": round(peak_level, 4),
        "peak_at_ms": peak_at,
        "peak_at": _iso(peak_at),
        "fallen_m": round(max(0.0, peak_level - float(current)), 4),
        "fallen_cm": round(max(0.0, peak_level - float(current)) * 100.0, 1),
        "confirmation_at": int(now),
    }

def update_peak_learning(learning, events, response_state, history, now, current,
                         learned_24h=None, unified_curve=None, rain=None, rain_history=None):
    """Motor do pico baseado exclusivamente na curva temporal da calculadora.

    IMPORTANTE:
    - não existe uma segunda fórmula de pico;
    - não existe uma segunda soma de chuva;
    - não existe previsão independente de 24h;
    - enquanto o evento está ativo, o pico é simplesmente o máximo da mesma
      `unified_curve` usada para produzir 2h/4h/6h/12h;
    - o aprendizado do módulo mede o erro do pico, mas não altera a curva.

    Assim, projeção e pico são matematicamente inseparáveis: se a curva mudar,
    os horizontes e o pico mudam juntos.
    """
    if not isinstance(learning, dict):
        learning = default_peak_learning()
    if not isinstance(events, list):
        events = []

    active = response_state.get("active") if isinstance(response_state, dict) else None

    # ---------------------------------------------------------------
    # EVENTO ENCERRADO / PÓS-PICO
    # ---------------------------------------------------------------
    if not isinstance(active, dict) or current is None:
        last_event = response_state.get("last_event") if isinstance(response_state, dict) else None
        active_learning = learning.get("active") or {}
        if isinstance(last_event, dict):
            event_id = str(last_event.get("completed_at") or last_event.get("rain_started_at") or "")
            already = learning.get("last_completed") or {}
            if event_id and str(already.get("event_id")) != event_id:
                completed_learning = learning.get("last_completed") or {}

                # Usa a previsão congelada da confirmação. Para estados antigos
                # que já estavam em recessão antes desta versão, aproveita o
                # último valor publicado desse mesmo evento como compatibilidade.
                if (str(completed_learning.get("event_id")) == event_id
                        and active_learning.get("status") == "rio_em_recessao"):
                    published = completed_learning.get(
                        "published_peak_m",
                        active_learning.get("peak_projection_at_confirmation_m",
                                             active_learning.get("published_peak_m"))
                    )
                    published_in_hours = completed_learning.get(
                        "published_peak_in_hours",
                        active_learning.get("peak_projection_at_confirmation_in_hours",
                                             active_learning.get("published_peak_in_hours"))
                    )
                    projected_at_ms = completed_learning.get("projected_peak_at_ms")
                    projected_at = completed_learning.get("projected_peak_at")
                else:
                    published = active_learning.get(
                        "peak_projection_at_confirmation_m",
                        active_learning.get("published_peak_m")
                    )
                    published_in_hours = active_learning.get(
                        "peak_projection_at_confirmation_in_hours",
                        active_learning.get("published_peak_in_hours")
                    )
                    projected_at_ms = active_learning.get("peak_projection_at_confirmation_at_ms")
                    projected_at = active_learning.get("peak_projection_at_confirmation_at")
                actual = _num(last_event.get("peak_level"), float("nan"))
                if not math.isfinite(actual):
                    actual = _num(active_learning.get("actual_peak_m"), float("nan"))
                actual_at_ms = (
                    last_event.get("peak_at")
                    or last_event.get("peak_at_ms")
                    or active_learning.get("actual_peak_at_ms")
                )
                actual_at = (
                    last_event.get("peak_at_iso")
                    or last_event.get("peak_at")
                    or active_learning.get("actual_peak_at")
                )
                if published is not None and math.isfinite(actual):
                    err = float(published) - actual
                    recent = list(learning.get("recent_errors_m") or []) + [round(err, 4)]
                    recent = recent[-50:]
                    n = int(learning.get("evaluated_events", 0) or 0) + 1
                    total = _num(learning.get("sum_abs_peak_error_m")) + abs(err)
                    learning["evaluated_events"] = n
                    learning["sum_abs_peak_error_m"] = round(total, 6)
                    learning["mae_peak_m"] = round(total / n, 6)
                    learning["recent_errors_m"] = recent
                learning["last_completed"] = {
                    "event_id": event_id,
                    "actual_peak_m": actual,
                    "actual_peak_at_ms": actual_at_ms,
                    "actual_peak_at": actual_at,
                    "published_peak_m": published,
                    "published_peak_in_hours": published_in_hours,
                    "projected_peak_at_ms": projected_at_ms,
                    "projected_peak_at": projected_at,
                }
                # O prior 4,5 cm/mm pertence ao modelo hidrológico principal.
                # O módulo de pico apenas avalia seu erro; não recalibra a curva.
                learning["reference_cm_per_mm"] = 4.5
                events.append({
                    "event_id": event_id,
                    "completed_at": int(last_event.get("completed_at") or now),
                    "baseline_level": last_event.get("baseline_level"),
                    "actual_peak_m": actual,
                    "actual_peak_at_ms": actual_at_ms,
                    "actual_peak_at": actual_at,
                    "published_peak_m": published,
                    "published_peak_in_hours": published_in_hours,
                    "projected_peak_at_ms": projected_at_ms,
                    "projected_peak_at": projected_at,
                    "peak_error_m": (
                        round(float(published) - actual, 4)
                        if published is not None and math.isfinite(actual) else None
                    ),
                })
                events = events[-200:]

        previous_post = (
            learning.get("active")
            if isinstance(learning.get("active"), dict)
            and learning.get("active", {}).get("mode") in ("recessao", "nova_elevacao", "novo_pico")
            else None
        )
        post = _post_peak_state(
            last_event, history, now, current, rain, previous_post, rain_history
        )
        if post is not None:
            completed = learning.get("last_completed") or {}
            for key in (
                "published_peak_m", "published_peak_in_hours",
                "projected_peak_at_ms", "projected_peak_at",
                "actual_peak_m", "actual_peak_at_ms", "actual_peak_at"
            ):
                if completed.get(key) is not None:
                    post[key] = completed.get(key)
                elif active_learning.get(key) is not None:
                    post[key] = active_learning.get(key)
            learning["active"] = post
            learning["updated_at"] = _iso(now)
            return learning, events, post

        learning["active"] = None
        learning["updated_at"] = _iso(now)
        return learning, events, {
            "active": False,
            "status": "no_event",
            "update_interval_minutes": UPDATE_INTERVAL_MINUTES,
        }

    # ---------------------------------------------------------------
    # EVENTO ATIVO: O PICO É O MÁXIMO DA CURVA DA CALCULADORA
    # ---------------------------------------------------------------
    started = int(active.get("started_at", now) or now)
    event_id = str(started)

    # ---------------------------------------------------------------
    # PICO REAL DURANTE O EVENTO: não espera 24h de seca.
    # ---------------------------------------------------------------
    rain_now = rain if isinstance(rain, dict) else {}
    post_peak_r3 = max(0.0, _num(rain_now.get("h003")))
    previous_active = learning.get("active") if isinstance(learning.get("active"), dict) else {}

    # Se já confirmamos o pico deste mesmo evento, mantemos os dados exatos
    # até o encerramento do evento. O único gatilho que abre nova projeção é
    # chuva >= 5 mm em 3h. Pequenas oscilações do rio não apagam o pico.
    new_rain_trigger = (
        previous_active.get("event_id") == event_id
        and previous_active.get("status") == "pico_atingido"
        and post_peak_r3 >= POST_PEAK_NEW_RAIN_MM
    )
    if (previous_active.get("event_id") == event_id
            and previous_active.get("status") == "pico_atingido"
            and not new_rain_trigger):
        actual = _num(previous_active.get("actual_peak_m"), current)
        actual_at = int(previous_active.get("actual_peak_at_ms") or now)
        status = dict(previous_active)
        status.update({
            "active": True,
            "current_level_m": round(float(current), 4),
            "actual_peak_m": round(actual, 4),
            "actual_peak_at_ms": actual_at,
            "actual_peak_at": previous_active.get("actual_peak_at") or _iso(actual_at),
            "fallen_m": round(max(0.0, actual - float(current)), 4),
            "fallen_cm": round(max(0.0, actual - float(current)) * 100.0, 1),
            "rain_3h_mm": round(post_peak_r3, 3),
            "new_rain_trigger": False,
            "message": "Pico atingido. Mantendo o pico real até o encerramento do evento."
        })
        learning["active"] = status
        learning["updated_at"] = _iso(now)
        return learning, events, status

    # Detecta o máximo observado e confirma quando o rio já entrou em queda.
    # Com o gatilho de 5 mm/3h, inicia-se uma nova análise; não reutilizamos
    # o máximo antigo do mesmo evento como se fosse um novo pico.
    detection_started = now if new_rain_trigger else started
    observed = _detect_observed_peak(history, detection_started, now, current)

    if not isinstance(unified_curve, dict) or not unified_curve.get("points"):
        # Sem a curva oficial não inventamos outro pico. Isso é deliberado:
        # o sistema deve aguardar o mesmo motor que alimenta as projeções.
        status = {
            "active": True,
            "event_id": event_id,
            "status": "aguardando_curva_calculadora",
            "current_level_m": round(float(current), 4),
            "published_peak_m": round(float(current), 4),
            "raw_peak_m": round(float(current), 4),
            "temporal_peak_m": round(float(current), 4),
            "published_peak_in_hours": 0.0,
            "raw_peak_in_hours": 0.0,
            "projected_peak_at_ms": int(now),
            "projected_peak_at": _iso(int(now)),
            "confidence": 0.0,
            "sample_count": 0,
            "rain_mm": 0.0,
            "cm_per_mm": 4.5,
            "effective_rate_m_h": 0.0,
            "acceleration_m_h2": 0.0,
            "decelerating": False,
            "last_update_at": int(now),
            "next_official_update_at": int(now + UPDATE_HOURS * 3600000),
            "update_interval_minutes": UPDATE_INTERVAL_MINUTES,
        }
        learning["active"] = status
        learning["updated_at"] = _iso(now)
        return learning, events, status

    points = []
    for item in unified_curve.get("points") or []:
        try:
            h = float(item[0])
            level = float(item[1])
            if math.isfinite(h) and math.isfinite(level):
                points.append((h, level))
        except Exception:
            continue
    points.sort(key=lambda x: x[0])

    current_f = float(current)
    curve_peak = max([current_f] + [level for _, level in points])
    # Primeiro instante em que a curva alcança o máximo.
    peak_h = next((h for h, level in points if level >= curve_peak - 1e-9), 0.0)

    if observed is not None:
        # Congela a ÚLTIMA projeção emitida antes da confirmação do pico.
        # Nunca reconstrói o pico projetado a partir do nível atual, pois o
        # nível atual já está em queda quando o pico real é confirmado.
        previous_projected = _num(previous_active.get("published_peak_m"), float("nan"))
        previous_projected_h = _num(previous_active.get("published_peak_in_hours"), float("nan"))
        previous_projected_at_ms = previous_active.get("projected_peak_at_ms")
        previous_projected_at = previous_active.get("projected_peak_at")

        # Compatibilidade com o estado antigo: se o workflow já estava em
        # recessão antes desta versão, aproveita o último pico publicado como
        # referência histórica, mas nunca inventa um horário projetado ausente.
        legacy_completed = learning.get("last_completed") or {}
        if (not math.isfinite(previous_projected)
                and str(legacy_completed.get("event_id")) == event_id):
            previous_projected = _num(legacy_completed.get("published_peak_m"), float("nan"))
            previous_projected_h = _num(legacy_completed.get("published_peak_in_hours"), float("nan"))
            previous_projected_at_ms = legacy_completed.get("projected_peak_at_ms")
            previous_projected_at = legacy_completed.get("projected_peak_at")

        if math.isfinite(previous_projected):
            projection_peak = previous_projected
        else:
            projection_peak = curve_peak

        if math.isfinite(previous_projected_h):
            projection_h = previous_projected_h
        else:
            projection_h = peak_h

        try:
            projection_at_ms = int(previous_projected_at_ms) if previous_projected_at_ms is not None else None
        except Exception:
            projection_at_ms = None

        if projection_at_ms is None and (previous_projected_at is not None):
            try:
                projection_at_ms = int(previous_projected_at_ms)
            except Exception:
                projection_at_ms = None
        if projection_at_ms is None and math.isfinite(previous_projected_h):
            projection_at_ms = int(now + float(previous_projected_h) * 3600000)
        projection_at = previous_projected_at if previous_projected_at else (
            _iso(projection_at_ms) if projection_at_ms is not None else None
        )

        sample_count = len(_recent_history(history, started, now)) or 1
        confidence = _clamp(0.70 + 0.025 * min(sample_count, 8), 0.70, 0.92)
        status = {
            "active": True,
            "event_id": event_id,
            "status": "pico_atingido",
            "mode": "pico_atingido",
            "current_level_m": round(float(current), 4),
            "published_peak_m": round(float(projection_peak), 4),
            "published_peak_in_hours": round(float(projection_h), 4),
            "peak_projection_at_confirmation_m": round(float(projection_peak), 4),
            "peak_projection_at_confirmation_in_hours": round(float(projection_h), 4),
            "peak_projection_at_confirmation_at_ms": int(projection_at_ms),
            "peak_projection_at_confirmation_at": projection_at,
            "projected_peak_at_ms": int(projection_at_ms),
            "projected_peak_at": projection_at,
            "actual_peak_m": observed["peak_level_m"],
            "actual_peak_at_ms": observed["peak_at_ms"],
            "actual_peak_at": observed["peak_at"],
            "fallen_m": observed["fallen_m"],
            "fallen_cm": observed["fallen_cm"],
            "peak_confirmed_at_ms": observed["confirmation_at"],
            "peak_confirmed": True,
            "peak_window_hours": 0.0,
            "confidence": round(confidence, 3),
            "sample_count": sample_count,
            "rain_mm": round(_num(unified_curve.get("rain_mm")), 3),
            "cm_per_mm": round(_num(unified_curve.get("cm_per_mm"), 4.5), 3),
            "effective_rate_m_h": round(_num(unified_curve.get("effective_rate_m_h")), 5),
            "acceleration_m_h2": round(_num(unified_curve.get("acceleration_m_h2")), 5),
            "decelerating": True,
            "response_delay_hours": round(_num(unified_curve.get("response_delay_hours")), 3),
            "remaining_m": round(max(0.0, _num(unified_curve.get("remaining_m"))), 4),
            "total_response_m": round(max(0.0, _num(unified_curve.get("total_response_m"))), 4),
            "curve_source": "calculadora_unificada",
            "curve_version": PEAK_VERSION,
            "last_update_at": int(now),
            "next_official_update_at": int(now + UPDATE_HOURS * 3600000),
            "update_interval_minutes": UPDATE_INTERVAL_MINUTES,
            "rain_3h_mm": round(post_peak_r3, 3),
            "new_rain_trigger": False,
            "message": "Pico atingido. O nível máximo observado já foi seguido de queda."
        }
        learning["active"] = status
        learning["updated_at"] = _iso(now)
        return learning, events, status

    # A curva já é horária. Não arredondamos nem suavizamos o pico depois:
    # qualquer suavização aqui criaria novamente uma segunda lógica.
    sample_count = len(_recent_history(history, started, now))
    if sample_count <= 0:
        sample_count = 1
    confidence = _clamp(0.55 + 0.035 * min(sample_count, 10), 0.55, 0.90)
    if float(unified_curve.get("effective_rate_m_h", 0.0)) > 0:
        confidence = min(0.94, confidence + 0.04)
    if unified_curve.get("decelerating"):
        confidence = min(0.96, confidence + 0.02)

    status_name = "pico_atingido" if peak_h <= 0.0 or curve_peak <= current_f + 1e-9 else "analisando_resposta"

    status = {
        "active": True,
        "event_id": event_id,
        "status": status_name,
        "mode": "pico_atingido" if status_name == "pico_atingido" else "analisando_resposta",
        "current_level_m": round(current_f, 4),
        "published_peak_m": round(curve_peak, 4),
        "raw_peak_m": round(curve_peak, 4),
        "temporal_peak_m": round(curve_peak, 4),
        "published_peak_in_hours": round(float(peak_h), 4),
        "raw_peak_in_hours": round(float(peak_h), 4),
        "peak_in_hours": round(float(peak_h), 4),
        "projected_peak_at_ms": int(now + float(peak_h) * 3600000),
        "projected_peak_at": _iso(int(now + float(peak_h) * 3600000)),
        "peak_window_hours": 1.0,
        "confidence": round(confidence, 3),
        "sample_count": sample_count,
        "rain_mm": round(_num(unified_curve.get("rain_mm")), 3),
        "cm_per_mm": round(_num(unified_curve.get("cm_per_mm"), 4.5), 3),
        "effective_rate_m_h": round(_num(unified_curve.get("effective_rate_m_h")), 5),
        "acceleration_m_h2": round(_num(unified_curve.get("acceleration_m_h2")), 5),
        "decelerating": bool(unified_curve.get("decelerating")),
        "response_delay_hours": round(_num(unified_curve.get("response_delay_hours")), 3),
        "remaining_m": round(max(0.0, _num(unified_curve.get("remaining_m"))), 4),
        "total_response_m": round(max(0.0, _num(unified_curve.get("total_response_m"))), 4),
        "curve_source": "calculadora_unificada",
        "curve_version": PEAK_VERSION,
        "last_update_at": int(now),
        "next_official_update_at": int(now + UPDATE_HOURS * 3600000),
        "update_interval_minutes": UPDATE_INTERVAL_MINUTES,
    }

    learning["active"] = status
    learning["updated_at"] = _iso(now)
    return learning, events, status

