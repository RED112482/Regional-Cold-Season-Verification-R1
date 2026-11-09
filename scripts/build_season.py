#!/usr/bin/env python3
"""
Build static cold-season datasets for the GitHub Pages viewer.

No third-party packages are required. GitHub Actions runs this script in the
cloud, so the viewer can remain entirely on GitHub with no Apps Script or local
npm/Python installation.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

APP_TITLE = "Regional Cold Season Verification"
BUILD_ID = "REGIONAL-COLD-SEASON-GITHUB-R3-SIMPLIFIED"
DATA_MODE = "COLD_SEASON_COUNTY_VERIFICATION"
LOCAL_TZ = ZoneInfo("America/Chicago")
IEM_BASE = "https://mesonet.agron.iastate.edu"
WFO_IDS = ["MOB", "LIX", "JAN", "BMX", "TAE"]
MIN_SEASON_YEAR = 2005
USER_AGENT = f"Regional-Cold-Season-Viewer/{BUILD_ID} (+https://github.com/RED112482/Regional-Cold-Season-Verification-R1)"
RETRY_CODES = {429, 500, 502, 503, 504}


def request_text(url: str, accept: str = "*/*", tries: int = 5, timeout: int = 90) -> str:
    last = None
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            url,
            headers={"Accept": accept, "User-Agent": USER_AGENT},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in RETRY_CODES or attempt == tries:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                raise RuntimeError(f"HTTP {exc.code} for {url}: {body}") from exc
        except Exception as exc:
            last = exc
            if attempt == tries:
                raise
        time.sleep(min(30, 3 * attempt))
    raise RuntimeError(str(last))


def request_json(url: str):
    return json.loads(request_text(url, "application/geo+json,application/json"))


def default_season_start_year() -> int:
    now = datetime.now(LOCAL_TZ)
    return now.year if now.month >= 6 else now.year - 1


def season_info(start_year: int) -> dict:
    return {
        "startYear": start_year,
        "label": f"{start_year}–{start_year + 1}",
        "startDate": f"{start_year}-09-01",
        "endDate": f"{start_year + 1}-05-31",
    }


def iso_date(value: str) -> date:
    return date.fromisoformat(value)


def date_range(start: str, end: str) -> list[str]:
    out = []
    d = iso_date(start)
    e = iso_date(end)
    while d <= e:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def point_in_ring(x: float, y: float, ring) -> bool:
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


def polygon_contains(rings, lon: float, lat: float) -> bool:
    if not rings or not point_in_ring(lon, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_ring(lon, lat, hole):
            return False
    return True


def feature_contains(feature: dict, lon: float, lat: float) -> bool:
    geom = (feature or {}).get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "Polygon":
        return polygon_contains(coords, lon, lat)
    if gtype == "MultiPolygon":
        return any(polygon_contains(poly, lon, lat) for poly in coords)
    return False


def containing_county(lon: float, lat: float, geo: dict):
    for feature in geo.get("features", []):
        if feature_contains(feature, lon, lat):
            p = feature.get("properties") or {}
            return {
                "fips": str(p.get("fips") or "").zfill(5),
                "countyname": str(p.get("countyname") or ""),
                "state": str(p.get("state") or "").upper(),
                "cwa": str(p.get("cwa") or "").upper(),
            }
    return None


def haversine_miles(lat1, lon1, lat2, lon2):
    r = 3958.7613
    rad = math.pi / 180.0
    dlat = (lat2 - lat1) * rad
    dlon = (lon2 - lon1) * rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(dlon / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def fetch_counties() -> dict:
    base = "https://mapservices.weather.noaa.gov/static/rest/services/nws_reference_maps/nws_reference_map/MapServer/2/query"
    params = {
        "where": "cwa IN ('" + "','".join(WFO_IDS) + "')",
        "outFields": "state,cwa,countyname,fips,lat,lon,time_zone",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    payload = request_json(base + "?" + urllib.parse.urlencode(params))
    if not isinstance(payload.get("features"), list):
        raise RuntimeError("NOAA county service returned no features")
    for feature in payload["features"]:
        p = feature.setdefault("properties", {})
        p["cwa"] = str(p.get("cwa") or "").upper()
        p["state"] = str(p.get("state") or "").upper()
        p["countyname"] = str(p.get("countyname") or "").strip()
        p["fips"] = str(p.get("fips") or "").zfill(5)
    return payload


def station_from_feature(feature: dict, network: str = "", state: str = ""):
    geom = feature.get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates") or []
    if len(coords) < 2:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except Exception:
        return None
    p = feature.get("properties") or {}
    obs = str(p.get("sid") or p.get("station") or p.get("id") or feature.get("id") or "").strip().upper()
    if not obs:
        return None
    display_id = "K" + obs if len(obs) == 3 and obs.isalpha() else obs
    return {
        "obs": obs,
        "displayId": display_id,
        "name": str(p.get("sname") or p.get("name") or p.get("station_name") or p.get("site_name") or display_id).strip(),
        "lat": lat,
        "lon": lon,
        "network": str(p.get("network") or p.get("iem_network") or network).strip(),
        "state": str(p.get("state") or p.get("state_abbr") or state).strip().upper(),
        "type": "METAR",
        "wfo": "",
        "countyFips": "",
        "countyName": "",
    }


def fetch_network_geo(network: str) -> dict:
    url = IEM_BASE + "/geojson/network.py?" + urllib.parse.urlencode({"network": network})
    payload = request_json(url)
    return payload if isinstance(payload.get("features"), list) else {"type": "FeatureCollection", "features": []}


def fetch_station_catalog(counties: dict) -> dict:
    states = sorted({str((f.get("properties") or {}).get("state") or "").upper() for f in counties.get("features", []) if (f.get("properties") or {}).get("state")})
    asos_ids = set()
    try:
        for feature in fetch_network_geo("ASOS1MIN").get("features", []):
            site = station_from_feature(feature)
            if site:
                asos_ids.add(site["obs"])
    except Exception as exc:
        print(f"Warning: ASOS1MIN metadata unavailable: {exc}", file=sys.stderr)

    all_sites = []
    seen = set()
    for state in states:
        network = f"{state}_ASOS"
        print(f"Fetching station metadata: {network}")
        geo = fetch_network_geo(network)
        for feature in geo.get("features", []):
            site = station_from_feature(feature, network=network, state=state)
            if not site or site["obs"] in seen:
                continue
            county = containing_county(site["lon"], site["lat"], counties)
            if not county or county["cwa"] not in WFO_IDS:
                continue
            seen.add(site["obs"])
            site["wfo"] = county["cwa"]
            site["countyFips"] = county["fips"]
            site["countyName"] = county["countyname"]
            if not site["state"]:
                site["state"] = county["state"]
            upper_name = site["name"].upper()
            if site["obs"] in asos_ids:
                site["type"] = "ASOS"
            elif "AWOS" in upper_name:
                site["type"] = "AWOS"
            else:
                site["type"] = "METAR"
            all_sites.append(site)

    wfo_rank = {wfo: i for i, wfo in enumerate(WFO_IDS)}
    all_sites.sort(key=lambda s: (wfo_rank.get(s["wfo"], 99), s["displayId"]))
    return {
        "allSites": all_sites,
        "asosAwosSites": [s for s in all_sites if s["type"] in ("ASOS", "AWOS")],
    }


def parse_num(value):
    value = (value or "").strip()
    if not value or value in {"M", "null", "None"}:
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except Exception:
        return None


def fetch_daily_summaries(all_sites: list[dict], start_date: str, end_date: str) -> dict:
    output = {s["obs"]: {} for s in all_sites}
    groups = {}
    for site in all_sites:
        network = site.get("network") or (site.get("state", "") + "_ASOS")
        if network:
            groups.setdefault(network, []).append(site)

    first = True
    for network in sorted(groups):
        if not first:
            # IEM documents a per-IP throttle for daily.py. Respect it explicitly.
            time.sleep(2.25)
        first = False
        sites = groups[network]
        params = [
            ("sts", start_date),
            ("ets", end_date),
            ("network", network),
            ("stations", ",".join(s["obs"] for s in sites)),
            ("var", "min_temp_f"),
            ("var", "min_feel"),
            ("format", "csv"),
            ("na", "M"),
        ]
        url = IEM_BASE + "/cgi-bin/request/daily.py?" + urllib.parse.urlencode(params)
        print(f"Fetching daily summaries: {network} ({len(sites)} stations)")
        text = request_text(url, "text/csv,text/plain")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            station = str(row.get("station") or row.get("id") or "").strip().upper()
            d = str(row.get("day") or row.get("date") or "").strip()[:10]
            if station not in output or not d:
                continue
            output[station][d] = {
                "minTemp": parse_num(row.get("min_temp_f") or row.get("min_tmpf")),
                "minFeel": parse_num(row.get("min_feel")),
            }
    return output


def criteria_for_county(county: dict) -> dict:
    wfo = str(county.get("cwa") or "").upper()
    state = str(county.get("state") or "").upper()
    name = str(county.get("countyname") or "").upper()
    lat = county.get("lat")
    advisory = warning = None
    band = ""

    if wfo == "MOB":
        north_al = {"CHOCTAW", "WASHINGTON", "CLARKE", "WILCOX", "BUTLER", "CRENSHAW", "MONROE", "CONECUH", "COVINGTON", "ESCAMBIA"}
        north = state == "MS" or (state == "AL" and name in north_al)
        advisory, warning = (20, 10) if north else (25, 15)
        band = "MOB northern tier" if north else "MOB southern/coastal tier"
    elif wfo == "LIX":
        north = isinstance(lat, (int, float)) and lat >= 30.45
        advisory, warning = (20, 10) if north else (25, 15)
        band = "LIX north of I-10/12 proxy" if north else "LIX south/coastal proxy"
    elif wfo == "JAN":
        if state == "AR":
            zone = "central"
        elif state == "LA":
            zone = "central" if isinstance(lat, (int, float)) and lat >= 31.6 else "south"
        elif isinstance(lat, (int, float)) and lat >= 33.0:
            zone = "north"
        elif isinstance(lat, (int, float)) and lat >= 31.8:
            zone = "central"
        else:
            zone = "south"
        advisory, warning = {"north": (10, 0), "central": (15, 5), "south": (20, 10)}[zone]
        band = f"JAN {zone} tier"
    elif wfo == "BMX":
        advisory, warning = 0, -10
        band = "BMX Central Alabama"
    elif wfo == "TAE":
        florida = state == "FL"
        advisory, warning = (24, 14) if florida else (19, 9)
        band = "TAE Florida tier" if florida else "TAE Alabama/Georgia tier"

    return {
        "frostProxy": 36,
        "freeze": 32,
        "hardFreeze": 25,
        "coldAdvisory": advisory,
        "extremeCold": warning,
        "bandLabel": band,
    }


def build_county_stats(counties: dict, sites: list[dict], daily_by_station: dict, dates: list[str]) -> dict:
    by_county = {}
    for site in sites:
        if site.get("countyFips"):
            by_county.setdefault(site["countyFips"], []).append(site)

    result = {}
    for feature in counties.get("features", []):
        p = feature.get("properties") or {}
        fips = str(p.get("fips") or "").zfill(5)
        try:
            county_lat = float(p.get("lat"))
            county_lon = float(p.get("lon"))
        except Exception:
            county_lat = county_lon = None

        county_sites = list(by_county.get(fips, []))
        coverage = "inside"
        nearest_miles = None
        if not county_sites and sites and county_lat is not None and county_lon is not None:
            best = min(sites, key=lambda s: haversine_miles(county_lat, county_lon, s["lat"], s["lon"]))
            nearest_miles = round(haversine_miles(county_lat, county_lon, best["lat"], best["lon"]), 1)
            county_sites = [best]
            coverage = "nearest"

        daily_temp = []
        daily_feel = []
        for d in dates:
            temps = []
            feels = []
            for site in county_sites:
                row = (daily_by_station.get(site["obs"]) or {}).get(d)
                if not row:
                    continue
                if isinstance(row.get("minTemp"), (int, float)):
                    temps.append(row["minTemp"])
                if isinstance(row.get("minFeel"), (int, float)):
                    feels.append(row["minFeel"])
            daily_temp.append(round(min(temps), 1) if temps else None)
            daily_feel.append(round(min(feels), 1) if feels else None)

        criteria = criteria_for_county({
            "cwa": str(p.get("cwa") or "").upper(),
            "state": str(p.get("state") or "").upper(),
            "countyname": str(p.get("countyname") or ""),
            "lat": county_lat,
            "lon": county_lon,
        })
        result[fips] = {
            "fips": fips,
            "county": str(p.get("countyname") or ""),
            "state": str(p.get("state") or ""),
            "wfo": str(p.get("cwa") or "").upper(),
            "lat": county_lat,
            "lon": county_lon,
            "coverage": coverage,
            "nearestMiles": nearest_miles,
            "stations": [
                {
                    "id": s["displayId"],
                    "obs": s["obs"],
                    "name": s["name"],
                    "type": s["type"],
                    "lat": s["lat"],
                    "lon": s["lon"],
                }
                for s in county_sites
            ],
            "criteria": criteria,
            "dailyMinTemp": daily_temp,
            "dailyMinFeel": daily_feel,
        }
    return result


def fetch_hazards_kml(start_date: str, end_date: str) -> str:
    start = datetime.fromisoformat(start_date + "T00:00:00+00:00")
    lookback = start - timedelta(days=7)
    end_exclusive = datetime.fromisoformat(end_date + "T00:00:00+00:00") + timedelta(days=1)
    params = [
        ("accept", "kml"),
        ("sts", lookback.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("ets", end_exclusive.strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("wfo", ",".join(WFO_IDS)),
        ("limitps", "1"),
        ("phenomena", "FR,FZ,FZ,CW,EC,EC,WC,WC,WC,HZ,HZ"),
        ("significance", "Y,A,W,Y,A,W,Y,A,W,A,W"),
        ("simple", "yes"),
        ("addsvs", "yes"),
    ]
    url = IEM_BASE + "/cgi-bin/request/gis/watchwarn.py?" + urllib.parse.urlencode(params)
    return request_text(url, "application/vnd.google-earth.kml+xml,application/xml,text/xml")


def query_end_for_season(season: dict) -> str:
    # Include the current local day so overnight frost/freeze/cold products can
    # verify as soon as the morning observations are available.
    today = datetime.now(LOCAL_TZ).date().isoformat()
    return min(season["endDate"], today)


def build_one_season(start_year: int, mode: str, counties: dict, catalog: dict, all_daily: dict, out_dir: Path):
    season = season_info(start_year)
    query_end = query_end_for_season(season)
    if query_end < season["startDate"]:
        print(f"Skipping future season {season['label']}")
        return None

    sites = catalog["asosAwosSites"] if mode == "asosawos" else catalog["allSites"]
    dates = date_range(season["startDate"], query_end)
    stats = build_county_stats(counties, sites, all_daily, dates)

    payload = {
        "buildId": BUILD_ID,
        "dataMode": DATA_MODE,
        "season": season,
        "queryEnd": query_end,
        "stationMode": mode,
        "stationModeLabel": "ASOS / AWOS only" if mode == "asosawos" else "All IEM METAR sites",
        "dates": dates,
        "countiesUrl": "./data/counties.geojson",
        "hazardsUrl": f"./data/hazards/{start_year}.kml",
        "countyStats": stats,
        "stationCount": len(sites),
        "catalogCounts": {
            "all": len(catalog["allSites"]),
            "asosawos": len(catalog["asosAwosSites"]),
        },
        "warnings": [],
        "generatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{start_year}-{mode}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {path} ({path.stat().st_size / 1024:.1f} KiB)")
    return path


def write_config(output_root: Path):
    current = default_season_start_year()
    options = [{"value": y, "label": f"{y}–{y+1}"} for y in range(current, MIN_SEASON_YEAR - 1, -1)]
    config = {
        "appTitle": APP_TITLE,
        "buildId": BUILD_ID,
        "dataMode": DATA_MODE,
        "wfos": WFO_IDS,
        "center": [31.45, -87.7],
        "zoom": 6,
        "defaultSeasonStartYear": current,
        "seasonOptions": options,
        "thresholdMin": -20,
        "thresholdMax": 40,
        "thresholdDefault": 32,
        "defaultStationMode": "all",
        "sourceNote": "Static GitHub Pages viewer. GitHub Actions preprocesses IEM computed daily summaries + IEM VTEC archive + NOAA/NWS county reference boundaries. County fills are observation-based screening, not official gridded verification.",
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def build_seasons(years: list[int], modes: list[str], output_root: Path):
    print("Fetching regional county boundaries…")
    counties = fetch_counties()
    print(f"County features: {len(counties.get('features', []))}")
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "counties.geojson").write_text(
        json.dumps(counties, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print("Fetching regional station catalog…")
    catalog = fetch_station_catalog(counties)
    print(f"Stations: {len(catalog['allSites'])} all / {len(catalog['asosAwosSites'])} ASOS-AWOS")

    seasons_dir = output_root / "seasons"
    for year in years:
        season = season_info(year)
        query_end = query_end_for_season(season)
        if query_end < season["startDate"]:
            print(f"Skipping {season['label']}: season has not started")
            continue

        print(f"\n=== Building {season['label']} through {query_end} ===")
        all_daily = fetch_daily_summaries(catalog["allSites"], season["startDate"], query_end)
        try:
            hazards = fetch_hazards_kml(season["startDate"], query_end)
        except Exception as exc:
            print(f"Warning: hazard archive failed: {exc}", file=sys.stderr)
            hazards = ""
        hazards_dir = output_root / "hazards"
        hazards_dir.mkdir(parents=True, exist_ok=True)
        (hazards_dir / f"{year}.kml").write_text(hazards, encoding="utf-8")

        for mode in modes:
            build_one_season(year, mode, counties, catalog, all_daily, seasons_dir)

    write_config(output_root)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--season", default="current", help="current, all, or a start year such as 2026")
    p.add_argument("--mode", default="both", choices=["both", "all", "asosawos"])
    p.add_argument("--output", default="site/data")
    return p.parse_args()


def main():
    args = parse_args()
    current = default_season_start_year()
    if args.season == "current":
        years = [current]
    elif args.season == "all":
        years = list(range(MIN_SEASON_YEAR, current + 1))
    else:
        year = int(args.season)
        if year < MIN_SEASON_YEAR or year > current:
            raise SystemExit(f"Season year must be between {MIN_SEASON_YEAR} and {current}")
        years = [year]

    modes = ["all", "asosawos"] if args.mode == "both" else [args.mode]
    output_root = Path(args.output)
    build_seasons(years, modes, output_root)


if __name__ == "__main__":
    main()
