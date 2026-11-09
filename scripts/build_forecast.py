#!/usr/bin/env python3
"""Build current NDFD overnight-minimum guidance for the GitHub Pages viewer.

Uses the official NWS NDFD REST/DWML multi-point service for county-center
minimum-temperature guidance.  The map itself uses the official NDFD WMS
MinTemp Day 1 / Day 2 raster layers in the browser.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
            layout_key = child_text(temp, "time-layout")
            vals = []
            for ch in list(temp):
                if strip_ns(ch.tag) == "value":
                    vals.append(fnum(ch.text))
            locations[loc_key]["values"] = vals
            locations[loc_key]["validTimes"] = time_layouts.get(layout_key, [])[:len(vals)]
            break
    return list(locations.values())


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
            "numDays": "3",
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
                out[p["fips"]] = {"day1": None, "day2": None, "validTimes": []}
                continue
            vals = result.get("values", [])
            out[p["fips"]] = {
                "day1": vals[0] if len(vals) > 0 else None,
                "day2": vals[1] if len(vals) > 1 else None,
                "validTimes": result.get("validTimes", [])[:2],
            }
    return out


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
    for fips, vals in ndfd.items():
        if fips in county_rows:
            county_rows[fips].update(vals)

    payload = {
        "generatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "NOAA/NWS NDFD minimum temperature",
        "wms": "https://mapservices.weather.noaa.gov/raster/services/NDFD/NDFD_temp/MapServer/WMSServer",
        "wmsLayers": {"day1": "140", "day2": "144"},
        "method": "Official NDFD raster display; county guidance values sampled at NOAA/NWS county representative points.",
        "counties": county_rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(county_rows)} counties")


if __name__ == "__main__":
    main()
