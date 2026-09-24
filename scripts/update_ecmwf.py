#!/usr/bin/env python3
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

POINTS = [
    (-26.82, -49.39), (-26.92, -49.64), (-27.02, -49.29), (-27.08, -49.55),
    (-27.18, -49.78), (-27.22, -49.48), (-27.30, -49.70), (-27.34, -49.38),
    (-27.42, -49.64), (-27.48, -49.50), (-27.55, -49.72), (-27.58, -49.32),
    (-26.62, -49.38), (-26.65, -49.17), (-26.70, -49.03), (-26.75, -49.29),
    (-26.78, -49.12), (-26.82, -49.42), (-26.86, -49.27), (-26.89, -49.10),
    (-26.93, -49.34), (-26.96, -49.19), (-27.00, -49.06), (-27.03, -48.92),
]
DATA = Path("data/ecmwf.json")
API = "https://single-runs-api.open-meteo.com/v1/forecast"


def utc_now():
    return datetime.now(timezone.utc)


def latest_candidate_run(now):
    """Return the newest official ECMWF main run (00Z or 12Z) available.

    The workflow may run frequently, but only these two main cycles are ever
    published. A 6-hour safety buffer gives Open-Meteo time to expose the run.
    The snapshot is always stored in the single fixed file data/ecmwf.json, so
    changing the model day never requires deleting a previous JSON manually.
    """
    now = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    day = now.date()
    # ECMWF main cycles used by this project: 00Z and 12Z only.
    # Open-Meteo documents that global-model runs are typically available
    # 4–6 hours after initialization, so use a conservative 6-hour buffer.
    # Before the official run is available, retain the previous snapshot.
    # valid snapshot rather than fabricating a future run.
    for hour in (12, 0):
        candidate = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
        if now >= candidate + timedelta(hours=6):
            return candidate
    # Between 00Z and its publication buffer, the newest available main run
    # is yesterday's 12Z. It is safe to retain the previous snapshot.
    return None


def get_json(run):
    lat = ",".join(f"{x[0]:.4f}" for x in POINTS)
    lon = ",".join(f"{x[1]:.4f}" for x in POINTS)
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "precipitation",
        "models": "ecmwf_ifs025",
        "forecast_days": "15",
        "timezone": "America/Sao_Paulo",
        "run": run.strftime("%Y-%m-%dT%H:%M"),
    }

    last_error = None
    for attempt in range(1, 4):
        try:
            r = requests.get(API, params=params, timeout=90)
            if r.status_code != 200:
                message = r.text[:300].strip()
                # Retry temporary provider errors; fail immediately on a bad
                # request because retrying will not make an invalid run valid.
                if r.status_code in (408, 425, 429) or r.status_code >= 500:
                    raise RuntimeError(f"Open-Meteo HTTP {r.status_code}: {message}")
                raise RuntimeError(f"Open-Meteo HTTP {r.status_code}: {message}")
            if not r.content.strip():
                raise RuntimeError("Open-Meteo retornou uma resposta vazia")
            try:
                return r.json()
            except ValueError as exc:
                raise RuntimeError("Open-Meteo retornou conteúdo que não é JSON") from exc
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt < 3:
                import time
                time.sleep(5 * attempt)

    raise RuntimeError(f"Falha ao obter a rodada {run:%Y-%m-%dT%H:%M}: {last_error}")


def build_snapshot(rows, run, captured):
    if not isinstance(rows, list):
        rows = [rows]
    if len(rows) != len(POINTS):
        raise RuntimeError(f"ECMWF retornou {len(rows)} pontos; esperado {len(POINTS)}")

    # The forecast is captured once per model run. Totals are calculated from
    # the moment the snapshot is created and then remain unchanged until the
    # next ECMWF run. This prevents the browser from moving the rolling window
    # every 30 minutes.
    now_ms = captured.timestamp() * 1000
    max_h = 360
    sums = [0.0] * max_h
    counts = [0] * max_h

    # Align all point series by their local-time ISO strings. Open-Meteo returns
    # the same hourly grid for all points, so this also avoids timezone drift.
    time_keys = rows[0].get("hourly", {}).get("time", [])
    point_values = []
    for row in rows:
        times = row.get("hourly", {}).get("time", [])
        rain = row.get("hourly", {}).get("precipitation", [])
        if len(times) != len(rain):
            raise RuntimeError("Série ECMWF incompleta em um dos pontos")
        point_values.append({t: float(v) for t, v in zip(times, rain) if isinstance(v, (int, float)) and math.isfinite(v)})

    hourly_basin = []
    # Preserve the exact run forecast from the current snapshot onward.
    # Convert local ISO timestamps to Unix milliseconds with the São Paulo DST
    # rule by parsing them as UTC-03:00 (Brazil has no DST in 2026).
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Sao_Paulo")
    for t in time_keys:
        try:
            local_dt = datetime.fromisoformat(t).replace(tzinfo=tz)
            ts_ms = int(local_dt.timestamp() * 1000)
        except Exception:
            continue
        if ts_ms < now_ms:
            continue
        vals = [m[t] for m in point_values if t in m]
        if not vals:
            continue
        hourly_basin.append({"t": ts_ms, "rain": round(sum(vals) / len(vals), 4)})

    def total(hours):
        end = now_ms + hours * 3600 * 1000
        vals = [p["rain"] for p in hourly_basin if now_ms <= p["t"] < end]
        return round(sum(vals), 2) if vals else None

    # 360h = 15 days. The card displays the same fixed snapshot all day.
    totals = {str(h): total(h) for h in (6, 12, 24, 48, 72, 192, 360)}
    valid = len(rows)
    if totals["12"] is None or totals["24"] is None:
        raise RuntimeError("Não há horas futuras suficientes no snapshot ECMWF")

    # ECMWF main runs used by this project: exactly two official cycles/day.
    # The snapshot remains fixed until the next 00Z/12Z cycle is published.
    return {
        "version": 3,
        "model": "ECMWF IFS 0.25°",
        "run_utc": run.strftime("%Y-%m-%dT%H:%M"),
        "run_label": f"{run.hour:02d}Z",
        "official_runs_per_day": 2,
        "official_run_hours_utc": [0, 12],
        "captured_at": captured.isoformat(),
        "valid_points": valid,
        "points_per_region": {"alto": 12, "medio": 12},
        "totals": totals,
        "hourly_basin": hourly_basin,
    }


def main():
    DATA.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    run = latest_candidate_run(now)

    if run is None:
        print("ECMWF: nenhuma rodada 00Z/12Z está disponível com a margem de 6 horas.")
        return 0

    run_key = run.strftime("%Y-%m-%dT%H:%M")

    existing = None
    if DATA.exists():
        try:
            existing = json.loads(DATA.read_text(encoding="utf-8"))
        except Exception:
            existing = None

    if existing and existing.get("run_utc") == run_key and existing.get("totals"):
        print(f"ECMWF: rodada {run_key} já está publicada; nenhuma alteração.")
        return 0

    # If the newest candidate is temporarily unavailable, retain the last good
    # snapshot instead of publishing an empty or partially updated forecast.
    try:
        payload = get_json(run)
    except Exception as exc:
        print(f"ECMWF: rodada {run_key} ainda indisponível: {exc}")
        if existing and existing.get("totals"):
            print("ECMWF: mantendo o último snapshot válido enquanto a nova rodada não está disponível.")
        else:
            print("ECMWF: ainda não há snapshot válido novo para publicar.")
        # Retorno não-zero: o workflow pode repetir a tentativa em vez de
        # registrar uma execução verde sem atualizar o JSON.
        return 2

    snapshot = build_snapshot(payload, run, now)
    DATA.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"ECMWF: publicada rodada {run_key} com {snapshot['valid_points']} pontos.")
    print("Totais:", snapshot["totals"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main() or 0)
