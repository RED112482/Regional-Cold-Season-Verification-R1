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
APRS_BASE = "https://aprs.fi"
DIAG = Path("site/data/cwop_aprsfi_diagnostic.html")
USER_AGENT = "Regional-Cold-Season-Viewer/R14-ETN-VERIFY"
HISTORY_HOURS = 168
MAX_STATIONS = 100
SEED_STATIONS = {
    "GW1930": (31.4510, -88.1962),  # Chatom / Washington County, AL
}

WX_CALL_RE = re.compile(r"(?:call=|>)([A-Z0-9]{1,8}(?:-[0-9]{1,2})?)(?:[&<])", re.I)
TS_RE = re.compile(r"\b(20\d{12})\b")
TS_ALT_RE = re.compile(r"\b(20\d{2})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?\b")
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
    # aprs.fi public pages support wildcard browsing, but for automated history
    # fallback we begin with explicitly verified regional CWOP ids. More can be
    # added after the page-history parser is validated.
    return sorted(SEED_STATIONS)


def station_position(call):
    return SEED_STATIONS.get(call)


def parse_timestamp(line: str):
    m = TS_RE.search(line)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            pass
    m = TS_ALT_RE.search(line)
    if m:
        try:
            sec = m.group(6) or "00"
            return datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(sec), tzinfo=timezone.utc
            )
        except Exception:
            pass
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
    url = f"{APRS_BASE}/weather/{urllib.parse.quote(call)}/week"
    txt = get(url, timeout=60)

    # Preserve the exact page returned for GW1930 when parsing fails so the
    # parser can be corrected from real aprs.fi markup rather than assumptions.
    def save_diag():
        try:
            DIAG.parent.mkdir(parents=True, exist_ok=True)
            DIAG.write_text(txt[:200000], encoding="utf-8", errors="ignore")
        except Exception:
            pass

    # Normalize markup into line-oriented text while retaining script content.
    cleaned = html.unescape(re.sub(r"<br\s*/?>", "\n", txt, flags=re.I))
    cleaned = re.sub(r"</(?:tr|div|p|li|script)>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    out = []
    # Support ISO timestamps and Unix timestamps commonly embedded in chart JS.
    iso_re = re.compile(r"(20\d{2}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)")
    unix_re = re.compile(r"\b(1\d{9})\b")
    temp_c_re = re.compile(r"(?:temp(?:erature)?|t)\s*[:=,]\s*(-?\d+(?:\.\d+)?)", re.I)
    hum_re = re.compile(r"(?:humidity|rh|h)\s*[:=,]\s*(\d+(?:\.\d+)?)", re.I)
    wind_ms_re = re.compile(r"(?:wind(?:_speed)?|windspeed|ws)\s*[:=,]\s*(\d+(?:\.\d+)?)", re.I)

    lines = [x.strip() for x in cleaned.splitlines() if x.strip()]
    for line in lines:
        dt = None
        m = iso_re.search(line)
        if m:
            try:
                dt = datetime.fromisoformat(m.group(1).replace("/", "-")).replace(tzinfo=timezone.utc)
            except Exception:
                dt = None
        if dt is None:
            m = unix_re.search(line)
            if m:
                try:
                    dt = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
                except Exception:
                    dt = None
        if dt is None:
            continue

        tm = temp_c_re.search(line)
        if not tm:
            continue
        try:
            tc = float(tm.group(1))
        except Exception:
            continue
        # Reject impossible Celsius values so graph axes/IDs are not mistaken
        # for temperatures.
        if tc < -80 or tc > 60:
            continue

        hm = hum_re.search(line)
        wm = wind_ms_re.search(line)
        rh = float(hm.group(1)) if hm else None
        wind_ms = float(wm.group(1)) if wm else None
        temp_f = c_to_f(tc)
        dew_f = dewpoint_f(temp_f, rh) if rh is not None else None
        out.append({
            "id": call,
            "provider": "aprs.fi CWOP/APRS public weather history",
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "time": dt.isoformat().replace("+00:00", "Z"),
            "tempF": round(temp_f, 2),
            "dewF": round(dew_f, 2) if dew_f is not None else None,
            "windKt": round(wind_ms * 1.94384449, 2) if wind_ms is not None else None,
        })

    # Dedupe plausible rows. If none are found, save real page markup for the
    # next parser refinement.
    dedup = {(r["id"], r["time"]): r for r in out}
    out = sorted(dedup.values(), key=lambda r: r["time"])
    if not out:
        save_diag()
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
                if call == "GW1930" and DIAG.exists():
                    try: DIAG.unlink()
                    except OSError: pass
                obs.extend(rows)
                stations.append({"id": call, "provider": "aprs.fi CWOP/APRS", "lat": lat, "lon": lon})
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
        "source": "aprs.fi public CWOP/APRS weather-history fallback",
        "windowDays": HISTORY_HOURS / 24,
        "status": {
            "discoveredStations": len(calls),
            "stationsWithHistory": len(unique_st),
            "observations": len(obs),
            "errors": errors[-20:],
            "seedGW1930Requested": "GW1930" in calls,
        },
        "stations": sorted(unique_st.values(), key=lambda s: s["id"]),
        "observations": obs,
    }
    write_atomic(payload)
    print("stations", len(unique_st), "observations", len(obs), "GW1930", "GW1930" in unique_st)

if __name__ == "__main__":
    main()
