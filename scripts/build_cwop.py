#!/usr/bin/env python3
"""Build compact CWOP/APRS verification history from Synoptic Data.

Uses network 65 (APRSWXNET/CWOP), discovers stations by CWA, then downloads
7-day time series in small batches.  Requires SYNOPTIC_TOKEN in the environment.
"""
from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.synopticdata.com/v2"
OUT = Path("site/data/cwop_recent.json")
TOKEN = os.environ.get("SYNOPTIC_TOKEN", "").strip()
CWAS = ["MOB", "LIX", "JAN", "BMX", "TAE"]
NETWORK = "65"
WINDOW_DAYS = 7
BATCH = 40
USER_AGENT = "Regional-Cold-Season-Viewer/R14-ETN-VERIFY"


def get_json(path: str, params: dict) -> dict:
    q = dict(params)
    q["token"] = TOKEN
    url = f"{API}/{path}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def first_series(obs: dict, base: str):
    for k, v in obs.items():
        if k == base or k.startswith(base + "_set_"):
            return v
    return None


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def write_atomic(payload: dict):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    tmp.replace(OUT)


def discover():
    stations = {}
    errors = []
    for cwa in CWAS:
        try:
            d = get_json("stations/metadata", {
                "cwa": cwa,
                "network": NETWORK,
                "status": "active",
                "fields": "stid,name,latitude,longitude,mnet_id",
            })
            for s in d.get("STATION", []) or []:
                stid = str(s.get("STID") or "").strip()
                if not stid:
                    continue
                lat = num(s.get("LATITUDE"))
                lon = num(s.get("LONGITUDE"))
                if lat is None or lon is None:
                    continue
                stations[stid] = {
                    "id": stid,
                    "name": s.get("NAME") or stid,
                    "provider": "Synoptic APRSWXNET/CWOP",
                    "lat": lat,
                    "lon": lon,
                    "cwa": cwa,
                }
        except Exception as exc:
            errors.append(f"{cwa} metadata: {type(exc).__name__}: {exc}")
    return stations, errors


def chunked(items, n):
    for i in range(0, len(items), n):
        yield items[i:i+n]


def main():
    if not TOKEN:
        raise SystemExit("SYNOPTIC_TOKEN is not set")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=WINDOW_DAYS)
    start_s = start.strftime("%Y%m%d%H%M")
    end_s = now.strftime("%Y%m%d%H%M")

    stations, errors = discover()
    obs_out = []
    batches = 0
    batches_ok = 0

    ids = sorted(stations)
    for ids_batch in chunked(ids, BATCH):
        batches += 1
        try:
            d = get_json("stations/timeseries", {
                "stid": ",".join(ids_batch),
                "start": start_s,
                "end": end_s,
                "vars": "air_temp,dew_point_temperature,relative_humidity,wind_speed,wind_gust",
                "units": "english,speed|kts",
                "obtimezone": "UTC",
                "qc_checks": "synopticlabs,madis",
                "showemptyvars": "1",
            })
            summary = d.get("SUMMARY") or {}
            if str(summary.get("RESPONSE_CODE", "1")) not in ("1", "200"):
                errors.append(f"batch {batches}: {summary}")
                continue

            for s in d.get("STATION", []) or []:
                stid = str(s.get("STID") or "").strip()
                meta = stations.get(stid)
                if not meta:
                    continue
                o = s.get("OBSERVATIONS") or {}
                times = o.get("date_time") or []
                temps = first_series(o, "air_temp") or []
                dews = first_series(o, "dew_point_temperature") or []
                rhs = first_series(o, "relative_humidity") or []
                winds = first_series(o, "wind_speed") or []
                gusts = first_series(o, "wind_gust") or []

                n = len(times)
                for i in range(n):
                    t = num(temps[i]) if i < len(temps) else None
                    if t is None:
                        continue
                    td = num(dews[i]) if i < len(dews) else None
                    rh = num(rhs[i]) if i < len(rhs) else None
                    w = num(winds[i]) if i < len(winds) else None
                    g = num(gusts[i]) if i < len(gusts) else None
                    obs_out.append({
                        "id": stid,
                        "name": meta["name"],
                        "provider": "Synoptic APRSWXNET/CWOP",
                        "lat": round(meta["lat"], 5),
                        "lon": round(meta["lon"], 5),
                        "cwa": meta["cwa"],
                        "time": times[i],
                        "tempF": round(t, 2),
                        "dewF": round(td, 2) if td is not None else None,
                        "rh": round(rh, 1) if rh is not None else None,
                        "windKt": round(w, 2) if w is not None else None,
                        "gustKt": round(g, 2) if g is not None else None,
                    })
            batches_ok += 1
        except Exception as exc:
            errors.append(f"batch {batches}: {type(exc).__name__}: {exc}")

    dedup = {(r["id"], r["time"]): r for r in obs_out}
    observations = sorted(dedup.values(), key=lambda r: (r["time"], r["id"]))
    active_ids = {r["id"] for r in observations}
    station_rows = [stations[x] for x in sorted(active_ids) if x in stations]

    payload = {
        "generatedUtc": now.isoformat().replace("+00:00", "Z"),
        "source": "Synoptic Data APRSWXNET/CWOP network 65",
        "windowDays": WINDOW_DAYS,
        "status": {
            "cwAs": CWAS,
            "network": NETWORK,
            "discoveredStations": len(stations),
            "stationsWithHistory": len(active_ids),
            "observations": len(observations),
            "batches": batches,
            "batchesSuccessful": batches_ok,
            "errors": errors[-20:],
        },
        "stations": station_rows,
        "observations": observations,
    }
    write_atomic(payload)
    print(json.dumps(payload["status"], indent=2))
    # Helpful targeted check for the Chatom station discussed during development.
    for candidate in ("G1930", "GW1930"):
        if candidate in stations:
            print(candidate, "found", sum(1 for r in observations if r["id"] == candidate), "observations")


if __name__ == "__main__":
    main()
