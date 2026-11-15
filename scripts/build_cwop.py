#!/usr/bin/env python3
"""Build a compact recent CWOP/APRS observation cache from NOAA MADIS.

MADIS public mesonet netCDF files contain many providers.  This extractor keeps
CWOP/APRS-like stations within the regional cold-season domain and writes a
small JSON cache for browser-side ETN verification.
"""
from __future__ import annotations

import gzip
import io
import json
import math
import os
import re
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import netCDF4
import numpy as np

BASE = "https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/mesonet/netCDF"
OUT = Path("site/data/cwop_recent.json")
USER_AGENT = "Regional-Cold-Season-Viewer/R14-ETN-VERIFY"
# Broad box encompassing MOB/LIX/JAN/BMX/TAE with a little margin.
LAT_MIN, LAT_MAX = 27.5, 35.8
LON_MIN, LON_MAX = -92.5, -82.0
CWOP_ID = re.compile(r"^(?:[CDFEG]W\d{3,5}|[A-Z]{1,2}\d[A-Z0-9]{1,5}|K[A-Z]{2,5}\d{1,3})$", re.I)


def pick(ds, *names):
    low = {k.lower(): k for k in ds.variables}
    for name in names:
        if name.lower() in low:
            return ds.variables[low[name.lower()]]
    return None


def decode_strings(var):
    if var is None:
        return None
    a = var[:]
    try:
        if getattr(a, "dtype", None) is not None and a.dtype.kind in ("S", "U") and a.ndim > 1:
            a = netCDF4.chartostring(a)
    except Exception:
        pass
    out = []
    for x in np.asarray(a).reshape(-1):
        if isinstance(x, bytes):
            x = x.decode("utf-8", "ignore")
        out.append(str(x).strip().strip("\x00"))
    return out


def numeric(var):
    if var is None:
        return None
    a = np.asarray(var[:], dtype=float).reshape(-1)
    fill = getattr(var, "_FillValue", None)
    if fill is not None:
        a[np.isclose(a, float(fill), equal_nan=False)] = np.nan
    return a


def to_f(values, var):
    if values is None:
        return None
    units = str(getattr(var, "units", "")).lower()
    a = values.astype(float)
    if "kelvin" in units or units.strip() in {"k", "degk"}:
        return (a - 273.15) * 9 / 5 + 32
    if "celsius" in units or "degc" in units or units.strip() in {"c", "degree_c"}:
        return a * 9 / 5 + 32
    return a


def wind_to_kt(values, var):
    if values is None:
        return None
    units = str(getattr(var, "units", "")).lower()
    a = values.astype(float)
    if "m/s" in units or "meter" in units:
        return a * 1.94384449
    if "mph" in units:
        return a * 0.868976
    return a


def epoch_to_iso(v):
    if not math.isfinite(float(v)):
        return None
    # MADIS observationTime is normally Unix seconds.
    try:
        return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def looks_cwop(station, provider):
    p = (provider or "").upper()
    s = (station or "").upper()
    if "APRS" in p or "CWOP" in p:
        return True
    return bool(CWOP_ID.match(s))


def file_url(dt):
    return f"{BASE}/{dt:%Y%m%d_%H00}.gz"


def fetch_hour(dt):
    req = urllib.request.Request(file_url(dt), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
    return gzip.decompress(raw)


def parse_hour(blob):
    fd, path = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    try:
        Path(path).write_bytes(blob)
        ds = netCDF4.Dataset(path)
        try:
            sidv = pick(ds, "stationId", "station_id", "stationName", "id")
            provv = pick(ds, "providerId", "provider_id", "provider", "networkId", "network_id")
            latv = pick(ds, "latitude", "lat")
            lonv = pick(ds, "longitude", "lon")
            tv = pick(ds, "temperature", "airTemperature", "temp")
            tdv = pick(ds, "dewpoint", "dewPoint", "dewpointTemperature")
            wv = pick(ds, "windSpeed", "wind_speed")
            otv = pick(ds, "observationTime", "observation_time", "timeObs", "time")
            if sidv is None or latv is None or lonv is None or tv is None or otv is None:
                return []
            sid = decode_strings(sidv)
            prov = decode_strings(provv) if provv is not None else [""] * len(sid)
            lat = numeric(latv); lon = numeric(lonv); temp = to_f(numeric(tv), tv)
            dew = to_f(numeric(tdv), tdv) if tdv is not None else None
            wind = wind_to_kt(numeric(wv), wv) if wv is not None else None
            obs = numeric(otv)
            n = min(len(sid), len(lat), len(lon), len(temp), len(obs))
            out = []
            for i in range(n):
                if not (math.isfinite(lat[i]) and math.isfinite(lon[i]) and math.isfinite(temp[i])):
                    continue
                if not (LAT_MIN <= lat[i] <= LAT_MAX and LON_MIN <= lon[i] <= LON_MAX):
                    continue
                provider = prov[i] if i < len(prov) else ""
                if not looks_cwop(sid[i], provider):
                    continue
                rec = {
                    "id": sid[i],
                    "provider": provider or "CWOP/APRS",
                    "lat": round(float(lat[i]), 5),
                    "lon": round(float(lon[i]), 5),
                    "time": epoch_to_iso(obs[i]),
                    "tempF": round(float(temp[i]), 2),
                    "dewF": round(float(dew[i]), 2) if dew is not None and i < len(dew) and math.isfinite(dew[i]) else None,
                    "windKt": round(float(wind[i]), 2) if wind is not None and i < len(wind) and math.isfinite(wind[i]) else None,
                }
                if rec["time"]:
                    out.append(rec)
            return out
        finally:
            ds.close()
    finally:
        try: os.unlink(path)
        except OSError: pass


def load_existing():
    if not OUT.exists():
        return []
    try:
        return json.loads(OUT.read_text()).get("observations", [])
    except Exception:
        return []


def main():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    existing = load_existing()
    # First run backfills 48 h; later runs refresh the last 4 h and retain 14 days.
    hours = 48 if not existing else 4
    records = list(existing)
    for h in range(hours, 0, -1):
        dt = now - timedelta(hours=h)
        try:
            records.extend(parse_hour(fetch_hour(dt)))
            print("decoded", dt.isoformat())
        except Exception as exc:
            print("warning", dt.isoformat(), exc)
    cutoff = now - timedelta(days=14)
    dedup = {}
    for r in records:
        try:
            t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff:
            continue
        key = (r.get("id"), r.get("time"))
        dedup[key] = r
    obs = sorted(dedup.values(), key=lambda x: (x["time"], x["id"]))
    stations = {}
    for r in obs:
        stations[r["id"]] = {"id": r["id"], "provider": r.get("provider"), "lat": r["lat"], "lon": r["lon"]}
    payload = {
        "generatedUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "NOAA MADIS public mesonet / CWOP-APRS",
        "windowDays": 14,
        "stations": sorted(stations.values(), key=lambda x: x["id"]),
        "observations": obs,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    print("stations", len(stations), "observations", len(obs))


if __name__ == "__main__":
    main()
