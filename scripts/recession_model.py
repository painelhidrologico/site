"""Projeção operacional de recessão do nível do rio.

Este módulo é independente do motor de subida e do motor de pico.
Ele apenas lê o pico já confirmado e as leituras horárias observadas.
"""
from __future__ import annotations

import math

HORIZONS = (2, 4, 6, 8, 12)
MIN_DROP_M = 0.01  # 1 cm entre leituras
MAX_READINGS = 6
MIN_INTERVAL_H = 0.25


def _num(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def _readings(history, peak_at_ms=None, now=None):
    rows = []
    for row in history or []:
        if not isinstance(row, dict):
            continue
        try:
            t = int(row.get("t", 0) or 0)
            level = float(row.get("level"))
        except Exception:
            continue
        if t <= 0 or not math.isfinite(level):
            continue
        if now is not None and t > int(now):
            continue
        if peak_at_ms and t < int(peak_at_ms):
            continue
        rows.append((t, level))
    rows.sort(key=lambda x: x[0])
    # Uma leitura por horário/timestamp. Se houver duplicata, fica a última.
    dedup = {}
    for t, level in rows:
        dedup[t] = level
    return [(t, dedup[t]) for t in sorted(dedup)][-MAX_READINGS:]


def build_recession_projection(current, history, peak_status, rain=None, now=None):
    """Retorna projeção de queda ou inactive.

    O pico já precisa ter sido confirmado pelo sistema existente. Não usa
    chuva de 24/48/96h para decidir se o rio está em recessão. O único
    bloqueio de chuva nova é o gatilho de 5 mm em 3h.
    """
    peak_status = peak_status if isinstance(peak_status, dict) else {}
    if not peak_status.get("active"):
        return {"active": False, "reason": "no_active_peak"}

    mode = str(peak_status.get("mode") or peak_status.get("status") or "")
    if mode not in {"recessao", "rio_em_recessao", "pico_atingido"}:
        return {"active": False, "reason": "peak_not_confirmed"}

    rain = rain if isinstance(rain, dict) else {}
    rain3 = max(0.0, _num(rain.get("h003")))
    if rain3 >= 5.0 or bool(peak_status.get("new_rain_detected")):
        return {
            "active": False,
            "reason": "new_rain_trigger",
            "rain_3h_mm": round(rain3, 3),
        }

    peak_at = peak_status.get("peak_at_ms") or peak_status.get("actual_peak_at_ms")
    rows = _readings(history, peak_at, now)
    if len(rows) < 2:
        # Fallback: se o horário do pico não estiver na série atual, use as
        # duas últimas leituras reais para não atrasar a primeira projeção.
        rows = _readings(history, None, now)[-2:]
    if len(rows) < 2:
        return {"active": False, "reason": "not_enough_readings"}

    # A última leitura precisa confirmar queda de pelo menos 1 cm. Assim que
    # 6,60 -> 6,58 acontece, a recessão pode ser projetada sem esperar seis horas.
    latest_t, latest_level = rows[-1]
    previous_t, previous_level = rows[-2]
    latest_dt_h = max(MIN_INTERVAL_H, (latest_t - previous_t) / 3600000.0)
    latest_delta = latest_level - previous_level
    if latest_delta > -MIN_DROP_M:
        return {
            "active": False,
            "reason": "latest_reading_not_falling",
            "latest_delta_m": round(latest_delta, 5),
        }

    # Depois do pico, uma subida real >= 1 cm interrompe a projeção de queda.
    intervals = []
    positive_reversal = False
    for (t0, l0), (t1, l1) in zip(rows, rows[1:]):
        dt_h = max(MIN_INTERVAL_H, (t1 - t0) / 3600000.0)
        delta = l1 - l0
        if delta >= MIN_DROP_M:
            positive_reversal = True
        if delta <= -MIN_DROP_M:
            intervals.append(abs(delta) / dt_h)

    if positive_reversal:
        return {"active": False, "reason": "rise_detected_after_peak"}
    if not intervals:
        return {"active": False, "reason": "no_drop_intervals"}

    # Média das quedas horárias observadas, usando até as seis últimas leituras.
    fall_rate_m_h = sum(intervals) / len(intervals)
    fall_rate_m_h = max(0.0, fall_rate_m_h)
    if fall_rate_m_h <= 0:
        return {"active": False, "reason": "zero_fall_rate"}

    projections = {}
    for horizon in HORIZONS:
        projections[str(horizon)] = round(max(0.0, latest_level - fall_rate_m_h * horizon), 4)

    return {
        "active": True,
        "mode": "recessao",
        "status": "rio_em_recessao",
        "current_level_m": round(latest_level, 4),
        "peak_level_m": round(_num(peak_status.get("peak_level_m"), latest_level), 4),
        "peak_at_ms": peak_at,
        "latest_reading_at_ms": latest_t,
        "readings_used": len(rows),
        "drop_intervals_used": len(intervals),
        "fall_rate_m_h": round(fall_rate_m_h, 5),
        "fall_rate_cm_h": round(fall_rate_m_h * 100.0, 2),
        "rain_3h_mm": round(rain3, 3),
        "rain_24h_mm": round(max(0.0, _num(rain.get("h024"))), 3),
        "projections": projections,
        "message": "Recessão projetada a partir da média das últimas leituras horárias. Chuva de 24h já contabilizada não bloqueia a queda; 5 mm em 3h inicia novo ciclo.",
    }
