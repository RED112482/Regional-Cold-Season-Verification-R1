#!/usr/bin/env python3
"""Build current NDFD overnight-minimum guidance for the GitHub Pages viewer.

Uses the official NWS NDFD REST/DWML multi-point service for county-center
minimum-temperature guidance.  The map itself uses the official NDFD WMS
MinTemp guidance through five overnight periods in the browser.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SITE = Path("site")
DATA = SITE / "data"
COUNTIES = DATA / "counties.geojson"
OUTPUT = DATA / "forecast.json"
WFO_IDS = ["MOB", "LIX", "JAN", "BMX", "TAE"]
LOCAL_TZ = ZoneInfo("America/Chicago")
USER_AGENT = "Regional-Cold-Season-Verification/forecast (+https://github.com/RED112482/Regional-Cold-Season-Verification-R1)"
NDFD_URL = "https://digital.weather.gov/xml/sample_products/browser_interface/ndfdBrowserClientByDay.php"
NDFD_TS_URL = "https://digital.weather.gov/xml/sample_products/browser_interface/ndfdXMLclient.php"


def get_text(url: str, timeout: int = 90) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fnum(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def first_child(parent, name):
    if parent is None:
        return None
    for child in list(parent):
        if strip_ns(child.tag) == name:
            return child
    return None


def child_text(parent, name):
    node = first_child(parent, name)
    return (node.text or "").strip() if node is not None and node.text else ""


def parse_dwml(xml_text: str):
    root = ET.fromstring(xml_text)
    locations = {}
    time_layouts = {}

    for node in root.iter():
        if strip_ns(node.tag) == "time-layout":
            key = child_text(node, "layout-key")
            starts = []
            for ch in list(node):
                if strip_ns(ch.tag) == "start-valid-time" and ch.text:
                    starts.append(ch.text.strip())
            if key:
                time_layouts[key] = starts

    for node in root.iter():
        if strip_ns(node.tag) != "location":
            continue
        key = child_text(node, "location-key")
        point = first_child(node, "point")
        if not key or point is None:
            continue
        lat = fnum(point.attrib.get("latitude"))
        lon = fnum(point.attrib.get("longitude"))
        locations[key] = {"lat": lat, "lon": lon, "values": [], "validTimes": []}

    for params in root.iter():
        if strip_ns(params.tag) != "parameters":
            continue
        loc_key = params.attrib.get("applicable-location", "")
        if loc_key not in locations:
            continue
        for temp in list(params):
            if strip_ns(temp.tag) != "temperature" or str(temp.attrib.get("type", "")).lower() != "minimum":
                continue
            layout_key = temp.attrib.get("time-layout") or child_text(temp, "time-layout")
            vals = []
            for ch in list(temp):
                if strip_ns(ch.tag) == "value":
                    vals.append(fnum(ch.text))
            locations[loc_key]["values"] = vals
            locations[loc_key]["validTimes"] = time_layouts.get(layout_key, [])[:len(vals)]
            break
    return list(locations.values())



def parse_apparent_dwml(xml_text: str):
    """Parse NDFD hourly apparent temperature plus frost-support fields."""
    root = ET.fromstring(xml_text)
    locations = {}
    layouts = {}

    for node in root.iter():
        if strip_ns(node.tag) == "time-layout":
            key = child_text(node, "layout-key")
            starts = []
            for ch in list(node):
                if strip_ns(ch.tag) == "start-valid-time" and ch.text:
                    starts.append(ch.text.strip())
            if key:
                layouts[key] = starts

    for node in root.iter():
        if strip_ns(node.tag) != "location":
            continue
        key = child_text(node, "location-key")
        point = first_child(node, "point")
        if not key or point is None:
            continue
        locations[key] = {
            "lat": fnum(point.attrib.get("latitude")),
            "lon": fnum(point.attrib.get("longitude")),
            "series": {},
        }

    for params in root.iter():
        if strip_ns(params.tag) != "parameters":
            continue
        loc_key = params.attrib.get("applicable-location", "")
        if loc_key not in locations:
            continue
        for node in list(params):
            tag = strip_ns(node.tag)
            key = None
            if tag == "temperature":
                ttype = str(node.attrib.get("type", "")).lower()
                name = child_text(node, "name").lower()
                if ttype == "apparent" or "apparent" in name:
                    key = "apparent"
                elif ttype == "hourly" or "hourly temperature" in name:
                    key = "temperature"
                elif "dew" in ttype or "dew point" in name:
                    key = "dewpoint"
            elif tag == "wind-speed":
                wtype = str(node.attrib.get("type", "")).lower()
                if wtype in ("sustained", ""):
                    key = "wind"
            elif tag == "cloud-amount":
                key = "sky"
            if not key:
                continue
            layout_key = node.attrib.get("time-layout") or child_text(node, "time-layout")
            vals = [
                fnum(ch.text)
                for ch in list(node)
                if strip_ns(ch.tag) == "value"
            ]
            locations[loc_key]["series"][key] = {
                "values": vals,
                "times": layouts.get(layout_key, [])[: len(vals)],
            }

    return list(locations.values())


def _in_overnight(ts: str, start_day: date) -> bool:
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    next_day = start_day + timedelta(days=1)
    return (dt.date() == start_day and dt.hour >= 18) or (
        dt.date() == next_day and dt.hour <= 6
    )


def _overnight_pairs(series, start_day: date):
    if not series:
        return []
    return [
        (v, ts)
        for v, ts in zip(series.get("values") or [], series.get("times") or [])
        if v is not None and _in_overnight(ts, start_day)
    ]


def _nearest_series_value(series, target_ts):
    if not series or not target_ts:
        return None
    pairs = [(v, ts) for v, ts in zip(series.get("values") or [], series.get("times") or []) if v is not None and ts]
    if not pairs:
        return None
    exact = next((v for v, ts in pairs if ts == target_ts), None)
    if exact is not None:
        return exact
    try:
        target = datetime.fromisoformat(target_ts.replace("Z", "+00:00"))
        return min(
            pairs,
            key=lambda p: abs((datetime.fromisoformat(p[1].replace("Z", "+00:00")) - target).total_seconds()),
        )[0]
    except Exception:
        return pairs[0][0]


def frost_score_for_night(result, start_day: date):
    """UK-style weighted frost-potential indicator (0-100).

    Uses the same NDFD ingredients emphasized by the University of Kentucky
    agricultural frost guidance: temperature, dewpoint depression, wind, and
    cloud cover. Categories are intended as screening guidance, not an NWS
    hazard probability.
    """
    series = (result or {}).get("series") or {}
    tpairs = _overnight_pairs(series.get("temperature"), start_day)
    if not tpairs:
        return None
    tmin, tmin_ts = min(tpairs, key=lambda p: p[0])
    dew = _nearest_series_value(series.get("dewpoint"), tmin_ts)
    wind = _nearest_series_value(series.get("wind"), tmin_ts)
    sky = _nearest_series_value(series.get("sky"), tmin_ts)

    # Temperature is the gate. Favorable moisture/wind/sky cannot create
    # a frost signal by themselves when the overnight air mass is too warm.
    if tmin > 40:
        return 0.0

    score = 0.0
    if tmin <= 32:
        score += 45
    elif tmin <= 34:
        score += 40
    elif tmin <= 37:
        score += 32
    else:
        score += 8

    dep = None if dew is None else max(0.0, tmin - dew)
    if dep is not None:
        score += 25 if dep <= 2 else 20 if dep <= 4 else 12 if dep <= 7 else 5 if dep <= 10 else 0

    if wind is not None:
        score += 20 if wind <= 3 else 15 if wind <= 5 else 8 if wind <= 8 else 3 if wind <= 12 else 0

    if sky is not None:
        score += 10 if sky <= 20 else 8 if sky <= 40 else 4 if sky <= 60 else 0

    # Above 37F, retain only a sub-threshold marginal signal. The map's
    # first actionable category begins at 20.
    if tmin > 37:
        score = min(score, 19.0)
    return round(max(0.0, min(100.0, score)), 1)
def overnight_min(values, times, start_day: date):
    """Return an overnight minimum from local 6 PM through 6 AM."""
    candidates = [v for v, ts in zip(values or [], times or []) if v is not None and _in_overnight(ts, start_day)]
    return min(candidates) if candidates else None


def fetch_apparent(points):
    """Fetch hourly NDFD apparent temperature and derive overnight minima."""
    out = {}
    today = datetime.now(LOCAL_TZ).date()
    begin = datetime.combine(today, dtime(12, 0), tzinfo=LOCAL_TZ).astimezone(timezone.utc)
    end = datetime.combine(today + timedelta(days=7), dtime(12, 0), tzinfo=LOCAL_TZ).astimezone(timezone.utc)

    for group in chunks(points, 80):
        list_lat_lon = " ".join(f'{p["lat"]:.5f},{p["lon"]:.5f}' for p in group)
        params = {
            "listLatLon": list_lat_lon,
            "product": "time-series",
            "begin": begin.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Unit": "e",
            "appt": "appt",
            "temp": "temp",
            "dew": "dew",
            "wspd": "wspd",
            "sky": "sky",
            "XMLformat": "DWML",
        }
        url = NDFD_TS_URL + "?" + urllib.parse.urlencode(params)
        parsed = parse_apparent_dwml(get_text(url, timeout=120))
        if not parsed:
            raise RuntimeError("NDFD returned no apparent-temperature time series")
        for point in group:
            result = nearest_result(point["lat"], point["lon"], parsed)
            if not result:
                out[point["fips"]] = {
                    **{f"day{i}Apt": None for i in range(1, 7)},
                    **{f"day{i}Frost": None for i in range(1, 7)},
                }
                continue
            apparent_series = (result.get("series") or {}).get("apparent") or {}
            item = {}
            for i in range(1, 7):
                night = today + timedelta(days=i - 1)
                item[f"day{i}Apt"] = overnight_min(
                    apparent_series.get("values"),
                    apparent_series.get("times"),
                    night,
                )
                item[f"day{i}Frost"] = frost_score_for_night(result, night)
            out[point["fips"]] = item
    return out


def nearest_result(lat, lon, parsed):
    usable = [p for p in parsed if p.get("lat") is not None and p.get("lon") is not None]
    if not usable:
        return None
    return min(usable, key=lambda p: (p["lat"] - lat) ** 2 + (p["lon"] - lon) ** 2)


def fetch_ndfd(points):
    out = {}
    today = datetime.now(LOCAL_TZ).date().isoformat()
    for group in chunks(points, 80):
        list_lat_lon = " ".join(f'{p["lat"]:.5f},{p["lon"]:.5f}' for p in group)
        params = {
            "listLatLon": list_lat_lon,
            "startDate": today,
            "numDays": "6",
            "Unit": "e",
            "format": "24 hourly",
            "XMLformat": "DWML",
        }
        url = NDFD_URL + "?" + urllib.parse.urlencode(params)
        text = get_text(url)
        parsed = parse_dwml(text)
        if not parsed:
            raise RuntimeError("NDFD returned no minimum-temperature point data")
        for p in group:
            result = nearest_result(p["lat"], p["lon"], parsed)
            if not result:
                out[p["fips"]] = {
                    **{f"day{i}": None for i in range(1, 7)},
                    "validTimes": [],
                }
                continue
            vals = result.get("values", [])
            item = {
                f"day{i}": vals[i - 1] if len(vals) >= i else None
                for i in range(1, 7)
            }
            item["validTimes"] = result.get("validTimes", [])[:5]
            out[p["fips"]] = item
    return out



def walk_coords(coords):
    if not isinstance(coords, list):
        return
    if len(coords) >= 2 and isinstance(coords[0], (int, float)) and isinstance(coords[1], (int, float)):
        yield float(coords[0]), float(coords[1])
        return
    for item in coords:
        yield from walk_coords(item)


def build_cwa_grids(counties, nx=17, ny=17):
    bounds = {}
    for feat in counties.get("features", []):
        p = feat.get("properties") or {}
        wfo = str(p.get("cwa") or "").upper()
        if wfo not in WFO_IDS:
            continue
        for lon, lat in walk_coords((feat.get("geometry") or {}).get("coordinates") or []):
            b = bounds.setdefault(wfo, [lon, lat, lon, lat])
            b[0] = min(b[0], lon)
            b[1] = min(b[1], lat)
            b[2] = max(b[2], lon)
            b[3] = max(b[3], lat)

    fields = {}
    for wfo, b in bounds.items():
        pad_lon = 0.10
        pad_lat = 0.08
        xmin, ymin, xmax, ymax = b[0] - pad_lon, b[1] - pad_lat, b[2] + pad_lon, b[3] + pad_lat
        lons = [xmin + i * (xmax - xmin) / (nx - 1) for i in range(nx)]
        lats = [ymax - j * (ymax - ymin) / (ny - 1) for j in range(ny)]
        points = []
        for j, lat in enumerate(lats):
            for i, lon in enumerate(lons):
                points.append({"fips": f"{wfo}_{j}_{i}", "lat": lat, "lon": lon})

        mint = fetch_ndfd(points)
        apt = fetch_apparent(points)
        field_values = {
            **{f"day{i}": [] for i in range(1, 7)},
            **{f"day{i}Apt": [] for i in range(1, 7)},
            **{f"day{i}Frost": [] for i in range(1, 7)},
        }
        for point in points:
            key = point["fips"]
            for i in range(1, 7):
                field_values[f"day{i}"].append(
                    (mint.get(key) or {}).get(f"day{i}")
                )
                field_values[f"day{i}Apt"].append(
                    (apt.get(key) or {}).get(f"day{i}Apt")
                )
                field_values[f"day{i}Frost"].append(
                    (apt.get(key) or {}).get(f"day{i}Frost")
                )

        fields[wfo] = {
            "nx": nx,
            "ny": ny,
            "xmin": xmin,
            "xmax": xmax,
            "ymin": ymin,
            "ymax": ymax,
            **field_values,
            "source": "NOAA/NWS NDFD point-sampled field",
        }
    return fields


def _ring_contains(ring, lon, lat):
    inside = False
    if not ring:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        hit = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if hit:
            inside = not inside
        j = i
    return inside


def _polygon_contains(poly, lon, lat):
    if not poly or not _ring_contains(poly[0], lon, lat):
        return False
    return not any(_ring_contains(hole, lon, lat) for hole in poly[1:])


def _geometry_contains(geom, lon, lat):
    if not geom:
        return False
    if geom.get("type") == "Polygon":
        return _polygon_contains(geom.get("coordinates") or [], lon, lat)
    if geom.get("type") == "MultiPolygon":
        return any(_polygon_contains(poly, lon, lat) for poly in geom.get("coordinates") or [])
    return False


def _median(values):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    return round(vals[n // 2], 1) if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2.0, 1)


def _pct(values, threshold):
    vals = [v for v in values if v is not None]
    if not vals or threshold is None:
        return None
    return round(100.0 * sum(1 for v in vals if v <= threshold) / len(vals), 1)


def _pct_ge(values, threshold):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(100.0 * sum(1 for v in vals if v >= threshold) / len(vals), 1)


def local_cold_thresholds(row):
    wfo = row.get("wfo")
    state = row.get("state")
    lat = row.get("lat")
    name = str(row.get("county") or "").upper()
    if wfo == "MOB":
        north_al = {"CHOCTAW", "WASHINGTON", "CLARKE", "WILCOX", "BUTLER", "CRENSHAW", "MONROE", "CONECUH", "COVINGTON", "ESCAMBIA"}
        north = state == "MS" or (state == "AL" and name in north_al)
        return (20, 10) if north else (25, 15)
    if wfo == "LIX":
        north = lat is not None and lat >= 30.45
        return (20, 10) if north else (25, 15)
    if wfo == "JAN":
        if state == "AR":
            zone = "central"
        elif state == "LA":
            zone = "central" if lat is not None and lat >= 31.6 else "south"
        elif lat is not None and lat >= 33.0:
            zone = "north"
        elif lat is not None and lat >= 31.8:
            zone = "central"
        else:
            zone = "south"
        return {"north": (10, 0), "central": (15, 5), "south": (20, 10)}[zone]
    if wfo == "BMX":
        return 0, -10
    if wfo == "TAE":
        return (24, 14) if state == "FL" else (19, 9)
    return None, None


def attach_county_areal_stats(counties, county_rows, fields):
    """Approximate county coverage from raw NDFD field samples (nearest-neighbor).

    A dense set of points is placed within each county polygon. Each point uses
    the nearest *raw* NDFD sample from the existing CWA field; no bilinear
    smoothing is used for product qualification.
    """
    feature_by_fips = {
        str((f.get("properties") or {}).get("fips") or "").zfill(5): f
        for f in counties.get("features", [])
    }
    for fips, row in county_rows.items():
        feat = feature_by_fips.get(fips)
        field = fields.get(row.get("wfo"))
        if not feat or not field:
            continue
        coords = list(walk_coords((feat.get("geometry") or {}).get("coordinates") or []))
        if not coords:
            continue
        xmin = min(x for x, _ in coords); xmax = max(x for x, _ in coords)
        ymin = min(y for _, y in coords); ymax = max(y for _, y in coords)
        sample_cells = []
        n = 14
        for jy in range(n):
            lat = ymin + (jy + 0.5) * (ymax - ymin) / n
            for ix in range(n):
                lon = xmin + (ix + 0.5) * (xmax - xmin) / n
                if _geometry_contains(feat.get("geometry") or {}, lon, lat):
                    gx = round((lon - field["xmin"]) / ((field["xmax"] - field["xmin"]) or 1) * (field["nx"] - 1))
                    gy = round((field["ymax"] - lat) / ((field["ymax"] - field["ymin"]) or 1) * (field["ny"] - 1))
                    gx = max(0, min(field["nx"] - 1, gx))
                    gy = max(0, min(field["ny"] - 1, gy))
                    sample_cells.append(gy * field["nx"] + gx)
        if not sample_cells:
            continue

        adv, ext = local_cold_thresholds(row)
        for i in range(1, 7):
            tvals = [field[f"day{i}"][idx] for idx in sample_cells if idx < len(field.get(f"day{i}") or [])]
            avals = [field[f"day{i}Apt"][idx] for idx in sample_cells if idx < len(field.get(f"day{i}Apt") or [])]
            fvals = [field[f"day{i}Frost"][idx] for idx in sample_cells if idx < len(field.get(f"day{i}Frost") or [])]
            coldvals = [
                min(t, a) if t is not None and a is not None else t if t is not None else a
                for t, a in zip(tvals, avals)
            ]
            row[f"day{i}Stats"] = {
                "sampleCount": len(sample_cells),
                "medianT": _median(tvals),
                "medianApt": _median(avals),
                "pctFrostTemp": _pct(tvals, 37),
                "pctFreeze": _pct(tvals, 32),
                "pctHardFreeze": _pct(tvals, 25),
                "pctColdAdvisory": _pct(coldvals, adv),
                "pctExtremeCold": _pct(coldvals, ext),
                "medianFrostScore": _median(fvals),
                "pctFrostPossible": _pct_ge(fvals, 20),
                "pctFrostAreas": _pct_ge(fvals, 40),
                "pctFrostWidespread": _pct_ge(fvals, 70),
            }


def forecast_periods():
    """Five overnight periods, rolling strictly at local midnight."""
    today = datetime.now(LOCAL_TZ).date()
    periods = {}
    for i in range(1, 7):
        start = today + timedelta(days=i - 1)
        end = start + timedelta(days=1)
        periods[f"day{i}"] = {
            "name": "Tonight" if i == 1 else (
                "Tomorrow Night" if i == 2 else f"Night {i}"
            ),
            "nightNumber": i,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
    return periods


def main():
    counties = json.loads(COUNTIES.read_text(encoding="utf-8"))
    points = []
    county_rows = {}
    for feat in counties.get("features", []):
        p = feat.get("properties") or {}
        wfo = str(p.get("cwa") or "").upper()
        if wfo not in WFO_IDS:
            continue
        fips = str(p.get("fips") or "").zfill(5)
        lat = fnum(p.get("lat"))
        lon = fnum(p.get("lon"))
        if not fips or lat is None or lon is None:
            continue
        points.append({"fips": fips, "lat": lat, "lon": lon})
        county_rows[fips] = {
            "fips": fips,
            "county": str(p.get("countyname") or ""),
            "state": str(p.get("state") or "").upper(),
            "wfo": wfo,
            "lat": lat,
            "lon": lon,
        }

    ndfd = fetch_ndfd(points)
    apparent = fetch_apparent(points)
    for fips, vals in ndfd.items():
        if fips in county_rows:
            county_rows[fips].update(vals)
            county_rows[fips].update(apparent.get(fips, {}))

    fields = build_cwa_grids(counties)
    attach_county_areal_stats(counties, county_rows, fields)

    payload = {
        "generatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatedLocalDate": datetime.now(LOCAL_TZ).date().isoformat(),
        "periods": forecast_periods(),
        "source": "NOAA/NWS NDFD minimum temperature + hourly apparent temperature",
        "wms": "https://mapservices.weather.noaa.gov/raster/services/NDFD/NDFD_temp/MapServer/WMSServer",
        "forecastNights": 5,
        "bufferNights": 1,
        "method": "Official NDFD MinT and hourly NDFD guidance. County product screening uses approximate county areal coverage from the raw CWA NDFD sample field with a 50% coverage threshold; display smoothing is not used for qualification. Frost potential is a UK-style weighted screening indicator using temperature, dewpoint depression, wind, and sky cover.",
        "counties": county_rows,
        "fields": fields,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(county_rows)} counties")


if __name__ == "__main__":
    main()
