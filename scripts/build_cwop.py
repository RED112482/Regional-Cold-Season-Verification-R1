#!/usr/bin/env python3
"""Build a compact CWOP/APRS history cache using station-targeted findU queries.

This is the lightweight fallback for ETN verification.  It discovers nearby
weather stations around the counties in the regional CWA domain, gets each
station position, then requests recent timestamped APRS packets only for those
stations.  Output schema matches the viewer's CWOP cache.
"""
from __future__ import annotations

import html
import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

COUNTIES = Path("site/data/counties.geojson")
OUT = Path("site/data/cwop_recent.json")
BASE = "https://www.findu.com/cgi-bin"
USER_AGENT = "Regional-Cold-Season-Viewer/R14-ETN-VERIFY"
HISTORY_HOURS = 168
MAX_STATIONS = 100
SEED_STATIONS = {
    "GW1930": (31.4510, -88.1962),  # Chatom / Washington County, AL
}

WX_CALL_RE = re.compile(r"(?:call=|>)([A-Z0-9]{1,8}(?:-[0-9]{1,2})?)(?:[&<])", re.I)
TS_RE = re.compile(r"\b(20\d{12})\b")
TEMP_RE = re.compile(r"t(-?\d{3})(?!\d)", re.I)
HUM_RE = re.compile(r"h(\d{2})(?!\d)", re.I)
WIND_RE = re.compile(r"s(\d{3})(?!\d)", re.I)
POS_DEC_RE = re.compile(r"(-?\d{1,3}\.\d+)")

def get(url: str, timeout: int = 45) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0

def dewpoint_f(temp_f, rh):
    if temp_f is None or rh is None or rh <= 0:
        return None
    # Magnus formula.
    tc = (temp_f - 32.0) * 5.0 / 9.0
    rh = min(100.0, max(1.0, rh))
    a, b = 17.625, 243.04
    g = math.log(rh / 100.0) + a * tc / (b + tc)
    dc = b * g / (a - g)
    return c_to_f(dc)

def county_centers():
    d = json.loads(COUNTIES.read_text())
    out = []
    for f in d.get("features", []):
        p = f.get("properties") or {}
        try:
            lat = float(p.get("lat"))
            lon = float(p.get("lon"))
        except Exception:
            continue
        out.append((lat, lon))
    return out

def discover_calls():
    calls = set(SEED_STATIONS)
    # Use every county center, but stop once the regional cap is reached.
    for lat, lon in county_centers():
        if len(calls) >= MAX_STATIONS:
            break
        q = urllib.parse.urlencode({"lat": f"{lat:.4f}", "lon": f"{lon:.4f}", "limit": 20, "noold": 0})
        try:
            txt = get(f"{BASE}/wxnear.cgi?{q}")
        except Exception as exc:
            print("discover warning", lat, lon, exc)
            continue
        for call in WX_CALL_RE.findall(txt):
            call = html.unescape(call).upper()
            if 2 <= len(call) <= 10:
                calls.add(call)
            if len(calls) >= MAX_STATIONS:
                break
        time.sleep(0.12)
    return sorted(calls)

def station_position(call):
    if call in SEED_STATIONS:
        return SEED_STATIONS[call]
    q = urllib.parse.urlencode({"call": call, "start": 24, "length": 24, "comma": 1})
    try:
        txt = get(f"{BASE}/posit.cgi?{q}")
    except Exception:
        return None
    # findU comma output contains decimal lat/lon.  Choose the first plausible
    # pair in the regional domain.
    nums = [float(x) for x in POS_DEC_RE.findall(txt)]
    for i in range(len(nums) - 1):
        lat, lon = nums[i], nums[i + 1]
        if 27.0 <= lat <= 36.5 and -93.5 <= lon <= -81.0:
            return lat, lon
    return None

def parse_packet_weather(packet):
    tm = TEMP_RE.search(packet)
    if not tm:
        return None
    try:
        temp = float(int(tm.group(1)))
    except Exception:
        return None
    hm = HUM_RE.search(packet)
    rh = None
    if hm:
        h = int(hm.group(1))
        rh = 100.0 if h == 0 else float(h)
    wm = WIND_RE.search(packet)
    wind_mph = float(int(wm.group(1))) if wm else None
    return temp, rh, wind_mph

def station_history(call, lat, lon):
    # raw.cgi guarantees timestamp prefix when time=1.  We then keep only
    # packets that contain APRS weather temperature fields.
    q = urllib.parse.urlencode({
        "call": call,
        "start": HISTORY_HOURS,
        "length": HISTORY_HOURS,
        "time": 1,
    })
    txt = get(f"{BASE}/raw.cgi?{q}", timeout=60)
    out = []
    for raw_line in re.split(r"[\r\n]+|<br\s*/?>", txt, flags=re.I):
        line = html.unescape(re.sub(r"<[^>]+>", " ", raw_line))
        ts = TS_RE.search(line)
        if not ts:
            continue
        wx = parse_packet_weather(line)
        if not wx:
            continue
        temp_f, rh, wind_mph = wx
        try:
            dt = datetime.strptime(ts.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        dew_f = dewpoint_f(temp_f, rh)
        out.append({
            "id": call,
            "provider": "findU CWOP/APRS",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "time": dt.isoformat().replace("+00:00", "Z"),
            "tempF": round(temp_f, 2),
            "dewF": round(dew_f, 2) if dew_f is not None else None,
            "windKt": round(wind_mph * 0.868976, 2) if wind_mph is not None else None,
        })
    return out

def write_atomic(payload):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    tmp.replace(OUT)

def main():
    calls = discover_calls()
    obs = []
    stations = []
    errors = []
    for n, call in enumerate(calls, 1):
        pos = station_position(call)
        if not pos:
            errors.append(f"{call}: no regional position")
            continue
        lat, lon = pos
        try:
            rows = station_history(call, lat, lon)
            if rows:
                obs.extend(rows)
                stations.append({"id": call, "provider": "findU CWOP/APRS", "lat": lat, "lon": lon})
                print(call, "obs", len(rows))
        except Exception as exc:
            errors.append(f"{call}: {type(exc).__name__}: {exc}")
        time.sleep(0.18)

    # Dedupe by station/time.
    dedup = {(r["id"], r["time"]): r for r in obs}
    obs = sorted(dedup.values(), key=lambda r: (r["time"], r["id"]))
    unique_st = {s["id"]: s for s in stations}
    payload = {
        "generatedUtc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "findU station-targeted CWOP/APRS history fallback",
        "windowDays": HISTORY_HOURS / 24,
        "status": {
            "discoveredStations": len(calls),
            "stationsWithHistory": len(unique_st),
            "observations": len(obs),
            "errors": errors[-20:],
        },
        "stations": sorted(unique_st.values(), key=lambda s: s["id"]),
        "observations": obs,
    }
    write_atomic(payload)
    print("stations", len(unique_st), "observations", len(obs), "GW1930", "GW1930" in unique_st)

if __name__ == "__main__":
    main()
