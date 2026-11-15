#!/usr/bin/env python3
"""Build compact CWOP/APRS verification history from Synoptic Data.

Uses network 65 (APRSWXNET/CWOP), discovers stations by CWA, then downloads
7-day time series in small batches.  Requires SYNOPTIC_TOKEN in the environment.
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://api.synopticdata.com/v2"
OUT = Path("site/data/cwop_recent.json")
OUT_DIR = Path("site/data/cwop")
HAZARDS = Path("site/data/hazards/2026.kml")
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


def recent_etn_windows(now: datetime, lookback_days: int = 7):
    """Return merged recent cold-product ETN windows from the season KML."""
    if not HAZARDS.exists():
        return []
    text = HAZARDS.read_text(encoding="utf-8", errors="ignore")
    pat = re.compile(
        r"<Placemark>.*?<name>([^<]+)</name>.*?"
        r"<TimeSpan>\s*<begin>([^<]+)</begin>\s*<end>([^<]+)</end>\s*</TimeSpan>",
        re.I | re.S,
    )
    cutoff = now - timedelta(days=lookback_days)
    raw = []
    for name, begin_s, end_s in pat.findall(text):
        u = name.upper()
        if not any(x in u for x in (
            "FROST", "FREEZE", "COLD", "WIND CHILL", "HARD FREEZE",
            ".FR.", ".FZ.", ".CW.", ".EC.", ".WC.", ".HZ.",
        )):
            continue
        try:
            begin = datetime.fromisoformat(begin_s.replace("Z", "+00:00"))
            end = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
        except Exception:
            continue
        if end <= begin:
            continue
        if end < cutoff or begin > now:
            continue
        # Small margin captures observations just outside nominal event
        # boundaries without retaining full-day histories.
        a = max(begin, cutoff) - timedelta(minutes=30)
        b = min(end, now) + timedelta(minutes=30)
        if b <= a:
            continue
        raw.append((a, b))
    if not raw:
        return []
    raw = sorted(set(raw))
    merged = [list(raw[0])]
    for begin, end in raw[1:]:
        if begin <= merged[-1][1] + timedelta(minutes=30):
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return [(a, b) for a, b in merged]


def in_windows(dt: datetime, windows):
    return any(a <= dt <= b for a, b in windows)


def discover():
    """Discover active CWOP/APRS stations using the verified Synoptic latest endpoint.

    The latest endpoint is intentionally used instead of stations/metadata because it
    works with the public Synoptic token used by GitHub Actions and returns the same
    station metadata plus a current observation payload.  MNET_ID is returned as a
    string (for example "65"), so normalize before validating the network.
    """
    stations = {}
    errors = []
    for cwa in CWAS:
        try:
            d = get_json("stations/latest", {
                "cwa": cwa,
                "network": NETWORK,
                "status": "active",
                "units": "english,speed|kts",
                "obtimezone": "UTC",
                "qc": "off",
            })
            summary = d.get("SUMMARY") or {}
            code = str(summary.get("RESPONSE_CODE", ""))
            if code != "1":
                errors.append(f"{cwa} latest API: {summary}")
                continue

            rows = d.get("STATION", []) or []
            print(f"{cwa} latest stations returned:", len(rows))

            for s in rows:
                # Synoptic returns MNET_ID as a string in /stations/latest.
                mnet = str(s.get("MNET_ID") or "").strip()
                if mnet and mnet != NETWORK:
                    continue

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

                if stid == "G1930":
                    obs = s.get("OBSERVATIONS") or {}
                    temp = None
                    temp_time = None
                    for key, value in obs.items():
                        if key.startswith("air_temp_value_") and isinstance(value, dict):
                            temp = num(value.get("value"))
                            temp_time = value.get("date_time")
                            break
                    print(
                        "FOUND G1930 in discovery:",
                        "MNET_ID=", mnet,
                        "tempF=", temp,
                        "time=", temp_time,
                    )
        except Exception as exc:
            errors.append(f"{cwa} latest: {type(exc).__name__}: {exc}")
    return stations, errors


def chunked(items, n):
    for i in range(0, len(items), n):
        yield items[i:i+n]


def main():
    if not TOKEN:
        raise SystemExit("SYNOPTIC_TOKEN is not set")

    now = datetime.now(timezone.utc)
    windows = recent_etn_windows(now, WINDOW_DAYS)
    if windows:
        start = min(a for a, _ in windows)
        end = max(b for _, b in windows)
    else:
        start = now - timedelta(days=2)
        end = now
    start_s = start.strftime("%Y%m%d%H%M")
    end_s = end.strftime("%Y%m%d%H%M")

    print("ETN windows retained:", len(windows))
    for a, b in windows:
        print(" -", a.isoformat(), "to", b.isoformat())

    stations, errors = discover()
    print("Synoptic CWOP stations discovered:", len(stations))
    if errors:
        print("Synoptic discovery/errors:")
        for e in errors:
            print(" -", e)
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
            if str(summary.get("RESPONSE_CODE", "")) != "1":
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
                    try:
                        dt = datetime.fromisoformat(str(times[i]).replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if windows and not in_windows(dt, windows):
                        continue
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
        "eventWindows": [{"start": a.isoformat().replace("+00:00","Z"), "end": b.isoformat().replace("+00:00","Z")} for a,b in windows],
        "status": {
            "cwAs": CWAS,
            "network": NETWORK,
            "discoveredStations": len(stations),
            "stationsWithHistory": len(active_ids),
            "observations": len(observations),
            "etnWindows": len(windows),
            "queryStart": start_s,
            "queryEnd": end_s,
            "batches": batches,
            "batchesSuccessful": batches_ok,
            "errors": errors[-20:],
        },
        "stations": station_rows,
        "observations": observations,
    }
    write_atomic(payload)

    # Publish small per-CWA caches so the browser never has to download the
    # full regional history file.  The regional file remains for diagnostics.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cwa in CWAS:
        cwa_obs = [r for r in observations if r.get("cwa") == cwa]
        cwa_ids = {r["id"] for r in cwa_obs}
        cwa_stations = [stations[x] for x in sorted(cwa_ids) if x in stations]
        cwa_payload = {
            "generatedUtc": payload["generatedUtc"],
            "source": payload["source"],
            "cwa": cwa,
            "windowDays": WINDOW_DAYS,
            "eventWindows": payload["eventWindows"],
            "status": {
                "network": NETWORK,
                "stationsWithHistory": len(cwa_ids),
                "observations": len(cwa_obs),
            },
            "stations": cwa_stations,
            "observations": cwa_obs,
        }
        out = OUT_DIR / f"{cwa}.json"
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cwa_payload, separators=(",", ":")), encoding="utf-8")
        json.loads(tmp.read_text(encoding="utf-8"))
        tmp.replace(out)
        print(cwa, "cache", len(cwa_ids), "stations", len(cwa_obs), "observations", out.stat().st_size, "bytes")

    print(json.dumps(payload["status"], indent=2))
    # Helpful targeted check for the Chatom station discussed during development.
    for candidate in ("G1930", "GW1930"):
        if candidate in stations:
            count = sum(1 for r in observations if r["id"] == candidate)
            print(candidate, "found", count, "historical observations")
            if count:
                latest = max((r for r in observations if r["id"] == candidate), key=lambda r: r["time"])
                print(candidate, "latest retained observation:", latest["time"], latest["tempF"], "F")


if __name__ == "__main__":
    main()
