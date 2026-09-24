"""Projeção operacional de recessão do nível do rio.

Este módulo é independente do motor de subida e do motor de pico.
Ele apenas lê o pico já confirmado e as leituras horárias observadas.
"""
from __future__ import annotations

import math

HORIZONS = (2, 4, 6, 8, 12)
MIN_DROP_M = 0.01  # 1 cm entre leituras
MAX_READINGS = 8
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
    """Retorna projeção de queda.

    O motor de pico continua sendo a fonte oficial quando existe um pico ativo.
    Quando o motor de pico já encerrou/limpou o evento, este módulo pode reconhecer
    uma recessão pós-pico curta diretamente pelas leituras observadas. Isso evita
    que o encerramento do estado de pico congele a calculadora, sem reativar nem
    alterar o motor de pico.

    A detecção pós-pico é conservadora:
      * exige uma máxima local recente seguida por pelo menos duas quedas;
      * exige queda acumulada >= 2 cm;
      * uma subida isolada de 1 cm não cancela a recessão;
      * duas subidas consecutivas, ou subida acumulada >= 2 cm, encerram a recessão;
      * chuva nova >= 5 mm/3h cancela a projeção.
    """
    peak_status = peak_status if isinstance(peak_status, dict) else {}
    rain = rain if isinstance(rain, dict) else {}
    rain3 = max(0.0, _num(rain.get("h003")))

    if rain3 >= 5.0 or bool(peak_status.get("new_rain_detected")):
        return {
            "active": False,
            "reason": "new_rain_trigger",
            "rain_3h_mm": round(rain3, 3),
        }

    # Quando o motor de pico está ativo, preserva exatamente a sua referência.
    official_peak = bool(peak_status.get("active"))
    mode = str(peak_status.get("mode") or peak_status.get("status") or "")
    if official_peak and mode not in {"recessao", "rio_em_recessao", "pico_atingido"}:
        return {"active": False, "reason": "peak_not_confirmed"}

    peak_at = None
    peak_level = None
    if official_peak:
        peak_at = peak_status.get("peak_at_ms") or peak_status.get("actual_peak_at_ms")
        peak_level = _num(peak_status.get("peak_level_m"), _num(current, 0.0))

    # Para o estado pós-pico encerrado, não reabre o pico: apenas procura uma
    # máxima local recente na própria série observada.
    all_rows = _readings(history, None, now)
    if len(all_rows) < 3:
        return {"active": False, "reason": "not_enough_readings"}

    if peak_at:
        rows = _readings(history, peak_at, now)
        if len(rows) < 2:
            rows = all_rows[-MAX_READINGS:]
    else:
        rows = all_rows[-MAX_READINGS:]

        # A máxima local deve estar entre as primeiras leituras da janela e ter
        # pelo menos duas quedas posteriores. Isso impede transformar estabilidade
        # ou ruído em recessão.
        peak_idx = None
        for i in range(0, len(rows) - 2):
            level = rows[i][1]
            later = [x[1] for x in rows[i+1:]]
            if level >= max(later) and level > later[0]:
                peak_idx = i
                break

        if peak_idx is None:
            return {"active": False, "reason": "no_recent_post_peak"}

        rows = rows[peak_idx:]
        peak_at = rows[0][0]
        peak_level = rows[0][1]

    if len(rows) < 3:
        return {"active": False, "reason": "not_enough_post_peak_readings"}

    latest_t, latest_level = rows[-1]

    # Calcula intervalos de queda e identifica reversão de forma mais robusta.
    drop_intervals = []
    rises = 0
    rise_total = 0.0
    for (t0, l0), (t1, l1) in zip(rows, rows[1:]):
        dt_h = max(MIN_INTERVAL_H, (t1 - t0) / 3600000.0)
        delta = l1 - l0
        if delta <= -MIN_DROP_M + 1e-9:
            drop_intervals.append(abs(delta) / dt_h)
        elif delta >= MIN_DROP_M - 1e-9:
            rises += 1
            rise_total += delta

    total_drop = peak_level - latest_level

    # Uma subida isolada de 1 cm, após uma recessão comprovada, pode ser ruído/
    # estabilização. Só encerra quando há reversão consistente.
    if rises >= 2 or rise_total >= 0.02:
        return {
            "active": False,
            "reason": "rise_detected_after_peak",
            "rise_count": rises,
            "rise_total_m": round(rise_total, 4),
        }

    if len(drop_intervals) < 2 or total_drop < 0.02:
        return {
            "active": False,
            "reason": "insufficient_confirmed_recession",
            "drop_intervals": len(drop_intervals),
            "total_drop_m": round(total_drop, 4),
        }

    fall_rate_m_h = sum(drop_intervals) / len(drop_intervals)
    if fall_rate_m_h <= 0:
        return {"active": False, "reason": "zero_fall_rate"}

    projections = {}
    for horizon in HORIZONS:
        projections[str(horizon)] = round(
            max(0.0, latest_level - fall_rate_m_h * horizon), 4
        )

    return {
        "active": True,
        "mode": "recessao",
        "status": "rio_em_recessao",
        "current_level_m": round(latest_level, 4),
        "peak_level_m": round(peak_level, 4),
        "peak_at_ms": peak_at,
        "peak_source": "official_peak" if official_peak else "observed_post_peak",
        "latest_reading_at_ms": latest_t,
        "readings_used": len(rows),
        "drop_intervals_used": len(drop_intervals),
        "fall_rate_m_h": round(fall_rate_m_h, 5),
        "fall_rate_cm_h": round(fall_rate_m_h * 100.0, 2),
        "rain_3h_mm": round(rain3, 3),
        "rain_24h_mm": round(max(0.0, _num(rain.get("h024"))), 3),
        "projections": projections,
        "message": (
            "Recessão projetada pelas leituras observadas. "
            "O encerramento do estado de pico não reativa o pico; "
            "apenas permite a projeção pós-pico enquanto a queda permanece confirmada."
        ),
    }
