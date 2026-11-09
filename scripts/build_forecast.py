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
    """Parse NDFD unsummarized apparent-temperature DWML."""
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
            "values": [],
            "times": [],
        }

    for params in root.iter():
        if strip_ns(params.tag) != "parameters":
            continue
        loc_key = params.attrib.get("applicable-location", "")
        if loc_key not in locations:
            continue
        for temp in list(params):
            if strip_ns(temp.tag) != "temperature":
                continue
            ttype = str(temp.attrib.get("type", "")).lower()
            name = child_text(temp, "name").lower()
            if ttype != "apparent" and "apparent" not in name:
                continue
            layout_key = temp.attrib.get("time-layout") or child_text(temp, "time-layout")
            vals = [
                fnum(ch.text)
                for ch in list(temp)
                if strip_ns(ch.tag) == "value"
            ]
            locations[loc_key]["values"] = vals
            locations[loc_key]["times"] = layouts.get(layout_key, [])[: len(vals)]
            break

    return list(locations.values())


def overnight_min(values, times, start_day: date):
    """Return min apparent temperature from local 6 PM through 6 AM."""
    candidates = []
    next_day = start_day + timedelta(days=1)
    for value, ts in zip(values or [], times or []):
        if value is None or not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        local_day = dt.date()
        hour = dt.hour
        if (local_day == start_day and hour >= 18) or (
            local_day == next_day and hour <= 6
        ):
            candidates.append(value)
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
                    f"day{i}Apt": None for i in range(1, 7)
                }
                continue
            out[point["fips"]] = {
                f"day{i}Apt": overnight_min(
                    result.get("values"),
                    result.get("times"),
                    today + timedelta(days=i - 1),
                )
                for i in range(1, 7)
            }
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

    payload = {
        "generatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generatedLocalDate": datetime.now(LOCAL_TZ).date().isoformat(),
        "periods": forecast_periods(),
        "source": "NOAA/NWS NDFD minimum temperature + hourly apparent temperature",
        "wms": "https://mapservices.weather.noaa.gov/raster/services/NDFD/NDFD_temp/MapServer/WMSServer",
        "forecastNights": 5,
        "bufferNights": 1,
        "method": "Official NDFD MinT raster display. Derived MinApT is the minimum of official hourly NDFD Apparent Temperature from 6 PM through 6 AM local time. County values and light contour fields are sampled directly from NOAA/NWS NDFD.",
        "counties": county_rows,
        "fields": fields,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(county_rows)} counties")


if __name__ == "__main__":
    main()
