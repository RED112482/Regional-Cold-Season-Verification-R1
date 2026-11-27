#!/usr/bin/env python3
"""Build compact supplemental mesonet verification history from Synoptic Data.

Discovers active public air-temperature stations by CWA/state, including
APRSWXNET/CWOP and other MADIS/National Mesonet providers, then downloads
time series in small batches. Requires SYNOPTIC_TOKEN in the environment.
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
from zoneinfo import ZoneInfo

API = "https://api.synopticdata.com/v2"
OUT = Path("site/data/cwop_recent.json")
OUT_DIR = Path("site/data/cwop")
DAILY_OUT = Path("site/data/cwop_daily.json")
LOCAL_TZ = ZoneInfo("America/Chicago")
HAZARDS = Path("site/data/hazards/2026.kml")
COUNTIES = Path("site/data/counties.geojson")
REGIONAL_STATES = ["AL", "MS", "FL", "LA", "GA", "AR"]
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



def point_in_ring(x, y, ring):
    inside = False
    if not ring:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        denom = (yj - yi) if (yj - yi) else 1e-12
        hit = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / denom + xi)
        if hit:
            inside = not inside
        j = i
    return inside


def feature_contains(feature, lon, lat):
    geom = (feature or {}).get("geometry") or {}
    coords = geom.get("coordinates") or []
    if geom.get("type") == "Polygon":
        if not coords or not point_in_ring(lon, lat, coords[0]):
            return False
        return not any(point_in_ring(lon, lat, hole) for hole in coords[1:])
    if geom.get("type") == "MultiPolygon":
        return any(
            poly and point_in_ring(lon, lat, poly[0])
            and not any(point_in_ring(lon, lat, hole) for hole in poly[1:])
            for poly in coords
        )
    return False


def load_counties():
    try:
        payload = json.loads(COUNTIES.read_text(encoding="utf-8"))
        return payload if isinstance(payload.get("features"), list) else {"features": []}
    except Exception as exc:
        print("Warning: county polygons unavailable for CWOP discovery:", exc)
        return {"features": []}


def county_for_point(counties, lon, lat):
    for feature in counties.get("features", []):
        if feature_contains(feature, lon, lat):
            p = feature.get("properties") or {}
            cwa = str(p.get("cwa") or "").upper()
            if cwa in CWAS:
                return {
                    "cwa": cwa,
                    "fips": str(p.get("fips") or "").zfill(5),
                    "county": str(p.get("countyname") or ""),
                    "state": str(p.get("state") or "").upper(),
                }
    return None


def add_discovered_station(stations, srow, counties, fallback_cwa=""):
    stid = str(srow.get("STID") or "").strip().upper()
    lat = num(srow.get("LATITUDE"))
    lon = num(srow.get("LONGITUDE"))
    if not stid or lat is None or lon is None:
        return False
    county = county_for_point(counties, lon, lat)
    cwa = county["cwa"] if county else str(fallback_cwa or "").upper()
    if cwa not in CWAS:
        return False
    mnet = str(srow.get("MNET_ID") or "").strip()
    provider = "Synoptic APRSWXNET/CWOP" if mnet == NETWORK else ("Synoptic/MADIS network " + mnet if mnet else "Synoptic/MADIS")
    stations[stid] = {
        "id": stid,
        "name": srow.get("NAME") or stid,
        "provider": provider,
        "networkId": mnet,
        "lat": lat,
        "lon": lon,
        "cwa": cwa,
        "countyFips": county["fips"] if county else "",
        "countyName": county["county"] if county else "",
        "state": county["state"] if county else "",
    }
    return True


def discover():
    """Discover active public air-temperature stations and assign by county polygon."""
    stations = {}
    errors = []
    counties = load_counties()

    # Primary discovery: every active station with air temperature in each CWA.
    # This intentionally spans APRSWXNET/CWOP plus other public MADIS/National
    # Mesonet providers so rural counties are not limited to airport/COOP sites.
    for cwa in CWAS:
        try:
            d = get_json("stations/latest", {
                "cwa": cwa,
                "status": "active",
                "vars": "air_temp",
                "units": "english,speed|kts",
                "obtimezone": "UTC",
                "qc": "off",
            })
            summary = d.get("SUMMARY") or {}
            if str(summary.get("RESPONSE_CODE", "")) != "1":
                errors.append(f"{cwa} latest API: {summary}")
                continue
            rows = d.get("STATION", []) or []
            print(f"{cwa} supplemental air-temp stations returned:", len(rows))
            for row in rows:
                add_discovered_station(stations, row, counties, fallback_cwa=cwa)
        except Exception as exc:
            errors.append(f"{cwa} latest: {type(exc).__name__}: {exc}")

    # State fallback catches stations whose CWA metadata is incomplete/stale.
    # County polygons remain the final authority for inclusion.
    for state in REGIONAL_STATES:
        try:
            d = get_json("stations/latest", {
                "state": state,
                "status": "active",
                "vars": "air_temp",
                "units": "english,speed|kts",
                "obtimezone": "UTC",
                "qc": "off",
            })
            summary = d.get("SUMMARY") or {}
            if str(summary.get("RESPONSE_CODE", "")) != "1":
                errors.append(f"{state} state latest API: {summary}")
                continue
            rows = d.get("STATION", []) or []
            print(f"{state} supplemental state stations returned:", len(rows))
            for row in rows:
                add_discovered_station(stations, row, counties)
        except Exception as exc:
            errors.append(f"{state} state latest: {type(exc).__name__}: {exc}")

    # Keep explicit diagnostics for examples/known rural stations when present.
    for stid in ("G4508", "GW4508", "E3034", "AW815", "G1930"):
        if stid in stations:
            print("Supplemental station discovered:", stid, stations[stid].get("provider"), stations[stid].get("countyName"))

    return stations, errors

def chunked(items, n):
    for i in range(0, len(items), n):
        yield items[i:i+n]



def sane_air_temp_f(value):
    n = num(value)
    return n if n is not None and -30.0 <= n <= 130.0 else None


def sane_apparent_f(value):
    n = num(value)
    return n if n is not None and -60.0 <= n <= 140.0 else None


def wind_chill_f(temp_f, wind_kt):
    if temp_f is None:
        return None
    if wind_kt is None:
        return temp_f
    mph = wind_kt * 1.150779
    if temp_f > 50 or mph < 3:
        return temp_f
    return 35.74 + 0.6215 * temp_f - 35.75 * (mph ** 0.16) + 0.4275 * temp_f * (mph ** 0.16)


def current_season_start(now):
    local = now.astimezone(LOCAL_TZ)
    year = local.year if local.month >= 6 else local.year - 1
    return datetime(year, 9, 1, 0, 0, tzinfo=LOCAL_TZ).astimezone(timezone.utc)


def update_daily_archive(stations, now):
    """Persist compact per-station daily minima for Cold Weather Frequency."""
    if DAILY_OUT.exists():
        try:
            archive = json.loads(DAILY_OUT.read_text(encoding="utf-8"))
        except Exception:
            archive = {}
    else:
        archive = {}

    season_start = current_season_start(now)
    season_key = season_start.astimezone(LOCAL_TZ).strftime("%Y")
    complete = str(archive.get("backfillCompleteSeason") or "") == season_key
    query_start = (now - timedelta(days=2)) if complete else season_start
    query_end = now

    daily = archive.get("stations") if isinstance(archive.get("stations"), dict) else {}

    # Purge bad historical sentinel/sensor values already persisted by older
    # builds so they cannot continue to qualify as cold-weather hits.
    for stid, days in list(daily.items()):
        if not isinstance(days, dict):
            daily[stid] = {}
            continue
        for day, rec in list(days.items()):
            if not isinstance(rec, dict):
                del days[day]
                continue
            rec["minTemp"] = sane_air_temp_f(rec.get("minTemp"))
            rec["minFeel"] = sane_apparent_f(rec.get("minFeel"))
            if rec["minTemp"] is None and rec["minFeel"] is None:
                del days[day]

    metadata = {str(x.get("id")): x for x in (archive.get("metadata") or []) if x.get("id")}
    for stid, meta in stations.items():
        metadata[stid] = {
            "id": stid,
            "name": meta.get("name") or stid,
            "lat": round(meta["lat"], 5),
            "lon": round(meta["lon"], 5),
            "cwa": meta.get("cwa") or "",
            "provider": meta.get("provider") or "Synoptic/MADIS",
            "networkId": meta.get("networkId") or "",
        }

    step = timedelta(days=7)
    cursor = query_start
    request_count = 0
    while cursor < query_end:
        window_end = min(query_end, cursor + step)
        start_s = cursor.strftime("%Y%m%d%H%M")
        end_s = window_end.strftime("%Y%m%d%H%M")
        print("CWOP daily archive window:", cursor.isoformat(), "to", window_end.isoformat())
        for ids_batch in chunked(sorted(stations), BATCH):
            request_count += 1
            try:
                d = get_json("stations/timeseries", {
                    "stid": ",".join(ids_batch),
                    "start": start_s,
                    "end": end_s,
                    "vars": "air_temp,wind_speed",
                    "units": "english,speed|kts",
                    "obtimezone": "UTC",
                    "qc_checks": "synopticlabs,madis",
                    "showemptyvars": "1",
                })
                if str((d.get("SUMMARY") or {}).get("RESPONSE_CODE", "")) != "1":
                    continue
                for row in d.get("STATION", []) or []:
                    stid = str(row.get("STID") or "").strip()
                    if not stid:
                        continue
                    obs = row.get("OBSERVATIONS") or {}
                    times = obs.get("date_time") or []
                    temps = first_series(obs, "air_temp") or []
                    winds = first_series(obs, "wind_speed") or []
                    station_days = daily.setdefault(stid, {})
                    for i, ts in enumerate(times):
                        t = sane_air_temp_f(temps[i]) if i < len(temps) else None
                        if t is None:
                            continue
                        w = num(winds[i]) if i < len(winds) else None
                        try:
                            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                        except Exception:
                            continue
                        day = dt.astimezone(LOCAL_TZ).date().isoformat()
                        feel = sane_apparent_f(wind_chill_f(t, w))
                        rec = station_days.setdefault(day, {"minTemp": None, "minFeel": None})
                        if rec.get("minTemp") is None or t < rec["minTemp"]:
                            rec["minTemp"] = round(t, 1)
                        if feel is not None and (rec.get("minFeel") is None or feel < rec["minFeel"]):
                            rec["minFeel"] = round(feel, 1)
            except Exception as exc:
                print("CWOP daily archive batch failed:", type(exc).__name__, exc)
        cursor = window_end

    archive = {
        "generatedUtc": now.isoformat().replace("+00:00", "Z"),
        "source": "Synoptic Data active public air-temperature stations (MADIS/National Mesonet/CWOP)",
        "seasonStart": season_start.astimezone(LOCAL_TZ).date().isoformat(),
        "backfillCompleteSeason": season_key,
        "metadata": [metadata[k] for k in sorted(metadata)],
        "stations": daily,
        "status": {"requests": request_count, "stationCount": len(stations)},
    }
    DAILY_OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(archive, separators=(",", ":")), encoding="utf-8")
    json.loads(tmp.read_text(encoding="utf-8"))
    tmp.replace(DAILY_OUT)
    print("CWOP daily archive:", len(daily), "stations", DAILY_OUT.stat().st_size, "bytes")


def backfill_event_days(stations, now):
    """Cache timestamped observations by UTC day and CWA, independent of KML.
    One-time backfill starts September 1; completed days are not re-requested.
    Store only temperature, dewpoint, humidity and wind needed by verification.
    """
    import concurrent.futures
    root = OUT_DIR / "history"
    root.mkdir(parents=True, exist_ok=True)
    start = current_season_start(now).date()
    today = now.date()
    # Prioritize the reported November 15 case, then fill the season backwards.
    days = [start + timedelta(days=i) for i in range((today-start).days+1)]
    days.reverse()
    target = datetime(now.year, 11, 15).date()
    if target in days:
        days.remove(target)
        days.insert(0, target)
    # Bound each scheduled run to prevent an unbounded archive rebuild.
    pending = [day for day in days if day >= today-timedelta(days=1) or not (root / (day.isoformat()+".complete")).exists()][:5]
    ids = sorted(stations)
    for day in pending:
        begin = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
        end = min(begin+timedelta(days=1), now)
        if end <= begin:
            continue
        def fetch_batch(batch):
            d = get_json("stations/timeseries", {
                "stid": ",".join(batch), "start": begin.strftime("%Y%m%d%H%M"),
                "end": end.strftime("%Y%m%d%H%M"),
                "vars": "air_temp,dew_point_temperature,relative_humidity,wind_speed",
                "units": "english,speed|kts", "obtimezone": "UTC",
                "qc_checks": "synopticlabs,madis", "showemptyvars": "1"})
            if str((d.get("SUMMARY") or {}).get("RESPONSE_CODE", "")) != "1":
                raise RuntimeError("Synoptic history batch unavailable")
            rows = []
            for station in d.get("STATION", []):
                sid = str(station.get("STID") or "").upper()
                if sid not in stations:
                    continue
                o = station.get("OBSERVATIONS") or {}
                series = [first_series(o,k) or [] for k in ("air_temp","dew_point_temperature","relative_humidity","wind_speed")]
                for i, ts in enumerate(o.get("date_time") or []):
                    vals = [num(a[i]) if i<len(a) else None for a in series]
                    if sane_air_temp_f(vals[0]) is None:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z","+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    if not begin <= dt < end:
                        continue
                    rows.append((sid, ts, *vals))
            return rows
        rows = []
        failures = 0
        # Keep API concurrency low and bound requests to daily chunks.
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            for future in [pool.submit(fetch_batch,b) for b in chunked(ids,BATCH)]:
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    failures += 1
                    print("History batch failed", day, type(exc).__name__)
        for cwa in CWAS:
            out = root / cwa / (day.isoformat()+".json")
            out.parent.mkdir(parents=True,exist_ok=True)
            bykey = {}
            if out.exists():
                try:
                    for r in json.loads(out.read_text()).get("rows",[]):
                        bykey[(r[0],r[1])] = r
                except Exception:
                    pass
            for r in rows:
                if stations[r[0]]["cwa"] == cwa:
                    bykey[(r[0],r[1])] = r
            used = {r[0] for r in bykey.values()}
            oldmeta = {}
            if out.exists():
                try:
                    oldmeta = {x["id"]:x for x in json.loads(out.read_text()).get("stations",[])}
                except Exception:
                    pass
            oldmeta.update({sid:stations[sid] for sid in used if sid in stations})
            payload = {"stations":[oldmeta[sid] for sid in sorted(used) if sid in oldmeta],
                       "rows":list(bykey.values()),"date":day.isoformat(),
                       "complete": failures==0 and day<today-timedelta(days=1)}
            tmp = out.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload,separators=(",",":")))
            tmp.replace(out)
        print("Historical UTC day",day,"samples",len(rows),"failed batches",failures)
        for sid in ("G4508","E3034","AW815","G1930"):
            sample = [r[2] for r in rows if r[0]==sid]
            if sample:
                print("Historical station",day,sid,"samples",len(sample),"daily UTC min F",min(sample))
        if failures==0 and rows and day<today-timedelta(days=1):
            (root / (day.isoformat()+".complete")).touch()

def main():
    if not TOKEN:
        raise SystemExit("SYNOPTIC_TOKEN is not set")

    now = datetime.now(timezone.utc)
    windows = recent_etn_windows(now, WINDOW_DAYS)

    # Always retain a rolling recent window in addition to hazard-specific
    # ETN windows.  Hazard KML and the CWOP workflow can update at different
    # times, so an ETN issued after the last hazard-data refresh must not leave
    # the browser with zero CWOP observations for an otherwise valid event.
    rolling_start = now - timedelta(days=2)
    rolling_end = now
    windows.append((rolling_start, rolling_end))

    # Merge overlapping/adjacent windows after adding the rolling fallback.
    windows = sorted(windows)
    merged = []
    for a, b in windows:
        if not merged or a > merged[-1][1] + timedelta(minutes=30):
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    windows = [(a, b) for a, b in merged]

    start = min(a for a, _ in windows)
    end = max(b for _, b in windows)
    start_s = start.strftime("%Y%m%d%H%M")
    end_s = end.strftime("%Y%m%d%H%M")

    print("ETN windows retained:", len(windows))
    for a, b in windows:
        print(" -", a.isoformat(), "to", b.isoformat())

    stations, errors = discover()
    print("Synoptic CWOP stations discovered:", len(stations))
    update_daily_archive(stations, now)
    backfill_event_days(stations, now)
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
                    t = sane_air_temp_f(temps[i]) if i < len(temps) else None
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
                        "provider": meta.get("provider") or "Synoptic/MADIS",
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
        "rollingRecentHours": 48,
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
