#!/usr/bin/env python3
"""Build rolling CWA MinT verification for the cold-season viewer.

Method:
- Five most recent completed 12Z MinT periods.
- Forecast snapshot/cycle: 12Z on the preceding day.
- NDFD: historical forecast.json snapshots already committed by this repository.
- NBM: operational CONUS Core + QMD GRIB2 from NOAA Open Data (12Z, F024).
- Verification: hourly URMA 2-m temperature from 00Z through 12Z; the minimum
  at each sample point is the verifying MinT.
- Spatial sampling: the existing NDFD raw sample nodes that fall inside each
  supported CWA's county polygons. This keeps all sources on the identical
  land-only sample.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.client import Config
from eccodes import (
    codes_get,
    codes_get_array,
    codes_grib_new_from_file,
    codes_release,
)

DATA = Path("site/data")
COUNTIES = DATA / "counties.geojson"
OUTPUT = DATA / "verification.json"
WFO_IDS = ["MOB", "LIX", "JAN", "BMX", "TAE"]
NBM_BUCKET = "noaa-nbm-grib2-pds"
URMA_BUCKET = "noaa-rtma-pds"
URMA_BUCKET = "noaa-urma-pds"
NDFD_BUCKET = "noaa-ndfd-pds"
PERCENTILES = [10, 25, 50, 75, 90]
MARINE_WFO_IDS = {"MOB", "LIX", "TAE"}

s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def safe_float(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def point_in_ring(x, y, ring):
    inside = False
    if not ring:
        return False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        hit = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if hit:
            inside = not inside
        j = i
    return inside


def point_in_geom(x, y, geom):
    if not geom:
        return False
    polys = [geom.get("coordinates") or []] if geom.get("type") == "Polygon" else (geom.get("coordinates") or [])
    for poly in polys:
        if not poly or not point_in_ring(x, y, poly[0]):
            continue
        if any(point_in_ring(x, y, hole) for hole in poly[1:]):
            continue
        return True
    return False


def cwa_geometries():
    geo = json.loads(COUNTIES.read_text())
    out = {w: [] for w in WFO_IDS}
    for f in geo.get("features", []):
        w = str((f.get("properties") or {}).get("cwa") or "").upper()
        if w in out:
            out[w].append(f.get("geometry") or {})
    return out


def inside_cwa(lon, lat, geoms):
    return any(point_in_geom(lon, lat, g) for g in geoms)


MARINE_ZONES_URL = "https://mapservices.weather.noaa.gov/static/rest/services/nws_reference_maps/nws_reference_map/FeatureServer/5/query"


def marine_geometries():
    """Fetch official NWS coastal marine-zone polygons for supported WFOs."""
    where = "wfo IN (" + ",".join(f"'{w}'" for w in sorted(MARINE_WFO_IDS)) + ")"
    params = {
        "where": where,
        "outFields": "wfo,name,id",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    req = Request(
        MARINE_ZONES_URL + "?" + urlencode(params),
        headers={"User-Agent": "Regional-Cold-Season-Verification/1.0"},
    )
    with urlopen(req, timeout=90) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    out = {w: [] for w in WFO_IDS}
    for feat in payload.get("features", []):
        props = feat.get("properties") or {}
        wfo = str(props.get("wfo") or props.get("WFO") or "").upper()
        if wfo in out:
            out[wfo].append(feat.get("geometry") or {})
    return out


def regular_points_in_geoms(geoms, spacing=0.12, max_points=220):
    """Build a compact, reproducible over-water sample from marine polygons."""
    pts = []
    seen = set()
    for geom in geoms or []:
        coords = geom.get("coordinates") or []
        flat = []
        def walk(x):
            if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], (int, float)) and isinstance(x[1], (int, float)):
                flat.append((float(x[0]), float(x[1])))
            elif isinstance(x, list):
                for item in x:
                    walk(item)
        walk(coords)
        if not flat:
            continue
        xmin=min(x for x,_ in flat); xmax=max(x for x,_ in flat)
        ymin=min(y for _,y in flat); ymax=max(y for _,y in flat)
        lat=ymin + spacing/2
        while lat <= ymax and len(pts) < max_points:
            lon=xmin + spacing/2
            while lon <= xmax and len(pts) < max_points:
                if point_in_geom(lon, lat, geom):
                    key=(round(lat,4),round(lon,4))
                    if key not in seen:
                        seen.add(key)
                        pts.append((lat,lon,None))
                lon += spacing
            lat += spacing
    return pts


def historical_forecast_snapshot(issue_day):
    target = datetime(issue_day.year, issue_day.month, issue_day.day, 12, tzinfo=timezone.utc)
    lines = git("log", "--format=%H|%cI", "--", "site/data/forecast.json").splitlines()
    choices = []
    for line in lines:
        try:
            sha, ts = line.split("|", 1)
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        if abs((dt - target).total_seconds()) <= 4 * 3600:
            choices.append((abs((dt - target).total_seconds()), sha, dt))
    for _, sha, dt in sorted(choices):
        try:
            payload = json.loads(git("show", f"{sha}:site/data/forecast.json"))
        except Exception:
            continue
        p = (payload.get("periods") or {}).get("day1") or {}
        if p.get("startDate") == issue_day.isoformat():
            return payload, sha, dt
    return None, None, None



_forecast_history_cache = None
_forecast_payload_cache = {}


def forecast_history():
    """Return forecast.json commits newest-first with UTC commit times."""
    global _forecast_history_cache
    if _forecast_history_cache is not None:
        return _forecast_history_cache
    rows = []
    for line in git("log", "--format=%H|%cI", "--", "site/data/forecast.json").splitlines():
        try:
            sha, ts = line.split("|", 1)
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        rows.append((dt, sha))
    _forecast_history_cache = rows
    return rows


def forecast_payload_at_sha(sha):
    if sha in _forecast_payload_cache:
        return _forecast_payload_cache[sha]
    try:
        payload = json.loads(git("show", f"{sha}:site/data/forecast.json"))
    except Exception:
        payload = None
    _forecast_payload_cache[sha] = payload
    return payload


def historical_forecast_at(target_dt, valid_day):
    """Return the NDFD snapshot that actually existed at target_dt.

    The verifying MinT ending at valid_day 12Z corresponds to the overnight
    forecast period whose endDate equals valid_day. Choose the latest committed
    forecast.json at or before target_dt that still contains that night.
    """
    valid_iso = valid_day.isoformat()
    candidates = [(dt, sha) for dt, sha in forecast_history() if dt <= target_dt]
    candidates.sort(reverse=True)
    # Search a bounded number of prior snapshots; the file is refreshed often,
    # so the correct one should be near the target.
    for dt, sha in candidates[:30]:
        payload = forecast_payload_at_sha(sha)
        if not payload:
            continue
        period_key = None
        for key, period in (payload.get("periods") or {}).items():
            if str((period or {}).get("endDate") or "") == valid_iso:
                period_key = key
                break
        if not period_key:
            continue
        # Prefer the payload's own generation time as the true snapshot time.
        gen = payload.get("generatedUtc")
        try:
            issue_dt = datetime.fromisoformat(str(gen).replace("Z", "+00:00")).astimezone(timezone.utc) if gen else dt
        except Exception:
            issue_dt = dt
        if issue_dt > target_dt + timedelta(minutes=10):
            continue
        return payload, period_key, sha, issue_dt
    return None, None, None, None


def sample_snapshot_field(field, period_key, points):
    """Sample one historical forecast.json field onto the verification nodes."""
    vals = field.get(period_key) or []
    nx, ny = int(field["nx"]), int(field["ny"])
    xmin, xmax = float(field["xmin"]), float(field["xmax"])
    ymin, ymax = float(field["ymin"]), float(field["ymax"])
    out = np.full(len(points), np.nan, dtype=float)
    for n, (lat, lon, _) in enumerate(points):
        gx = round((lon - xmin) / ((xmax - xmin) or 1) * (nx - 1))
        gy = round((ymax - lat) / ((ymax - ymin) or 1) * (ny - 1))
        gx = max(0, min(nx - 1, gx))
        gy = max(0, min(ny - 1, gy))
        k = gy * nx + gx
        if k < len(vals):
            v = safe_float(vals[k])
            if v is not None:
                out[n] = v
    return out


def sample_nodes(field, geoms, require_values=False):
    nx, ny = int(field["nx"]), int(field["ny"])
    xmin, xmax = float(field["xmin"]), float(field["xmax"])
    ymin, ymax = float(field["ymin"]), float(field["ymax"])
    vals = field.get("day1") or []
    out = []
    for j in range(ny):
        lat = ymax - j * (ymax - ymin) / max(1, ny - 1)
        for i in range(nx):
            lon = xmin + i * (xmax - xmin) / max(1, nx - 1)
            k = j * nx + i
            v = safe_float(vals[k]) if k < len(vals) else None
            if inside_cwa(lon, lat, geoms) and (not require_values or v is not None):
                out.append((lat, lon, v))
    return out


def key_exists(bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def find_key(bucket, preferred, prefix, suffix):
    if key_exists(bucket, preferred):
        return preferred
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            k = obj["Key"]
            if k.endswith(suffix):
                return k
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    raise FileNotFoundError(f"{bucket}: {preferred}")


def idx_lines(bucket, key):
    idx_key = key + ".idx"
    obj = s3.get_object(Bucket=bucket, Key=idx_key)
    return obj["Body"].read().decode("utf-8", "replace").splitlines()


def range_for_line(lines, i, object_size):
    parts = lines[i].split(":", 2)
    start = int(parts[1])
    if i + 1 < len(lines):
        end = int(lines[i + 1].split(":", 2)[1]) - 1
    else:
        end = object_size - 1
    return start, end


def fetch_message(bucket, key, line_index, lines):
    size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
    start, end = range_for_line(lines, line_index, size)
    body = s3.get_object(Bucket=bucket, Key=key, Range=f"bytes={start}-{end}")["Body"].read()
    fd, path = tempfile.mkstemp(suffix=".grib2")
    os.close(fd)
    try:
        Path(path).write_bytes(body)
        with open(path, "rb") as fh:
            gid = codes_grib_new_from_file(fh)
            if gid is None:
                raise RuntimeError("Unable to decode GRIB message")
            try:
                vals = np.asarray(codes_get_array(gid, "values"), dtype=float)
                lats = np.asarray(codes_get_array(gid, "latitudes"), dtype=float)
                lons = np.asarray(codes_get_array(gid, "longitudes"), dtype=float)
                units = str(codes_get(gid, "units") or "")
                meta = {}
                for name in ("shortName", "name", "stepType", "typeOfStatisticalProcessing", "percentileValue", "validityDate", "validityTime", "dataDate", "dataTime", "forecastTime", "startStep", "endStep", "stepRange"):
                    try:
                        meta[name] = codes_get(gid, name)
                    except Exception:
                        pass
            finally:
                codes_release(gid)
        if "K" in units or units.lower().startswith("k"):
            vals = (vals - 273.15) * 9.0 / 5.0 + 32.0
        # Treat impossible temperatures / GRIB missing sentinels as missing.
        vals = np.where(np.isfinite(vals) & (vals > -120.0) & (vals < 160.0), vals, np.nan)
        lons = np.where(lons > 180, lons - 360, lons)
        return vals, lats, lons, meta
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def select_idx_candidates(lines, required):
    out = []
    for i, line in enumerate(lines):
        low = line.lower()
        if all(x.lower() in low for x in required):
            out.append(i)
    return out



_ndfd_mint_prefix = None


def discover_ndfd_mint_prefix():
    """Find the archived WMO parameter prefix for NDFD minimum temperature."""
    global _ndfd_mint_prefix
    if _ndfd_mint_prefix:
        return _ndfd_mint_prefix
    # NDFD element name is normally "mint"; verify the prefix exists first.
    test = s3.list_objects_v2(Bucket=NDFD_BUCKET, Prefix="wmo/mint/", MaxKeys=1)
    if test.get("KeyCount", 0):
        _ndfd_mint_prefix = "wmo/mint/"
        return _ndfd_mint_prefix
    resp = s3.list_objects_v2(Bucket=NDFD_BUCKET, Prefix="wmo/", Delimiter="/")
    prefixes = [p.get("Prefix", "") for p in resp.get("CommonPrefixes", [])]
    choices = [p for p in prefixes if "mint" in p.lower() or ("min" in p.lower() and "temp" in p.lower())]
    if not choices:
        raise RuntimeError("Could not discover NDFD minimum-temperature WMO archive prefix")
    _ndfd_mint_prefix = sorted(choices, key=len)[0]
    return _ndfd_mint_prefix


def debug_ndfd_keys(issue_dt, limit=20):
    """Compact diagnostic for NDFD WMO key naming near a requested issuance."""
    root = discover_ndfd_mint_prefix()
    prefix = f"{root}{issue_dt:%Y/%m/%d}/"
    resp = s3.list_objects_v2(Bucket=NDFD_BUCKET, Prefix=prefix, MaxKeys=limit)
    out = []
    for obj in resp.get("Contents", []):
        out.append({
            "key": obj.get("Key"),
            "lastModified": obj.get("LastModified").isoformat() if obj.get("LastModified") else None,
            "size": obj.get("Size"),
        })
    return out


_ndfd_day_cache = {}


def ndfd_day_objects(day):
    """List relevant archived CONUS MinT objects for one UTC day once."""
    cache_key = day.isoformat()
    if cache_key in _ndfd_day_cache:
        return _ndfd_day_cache[cache_key]

    prefix = f"wmo/mint/{day:%Y/%m/%d}/"
    families = ("YHUZ97_KWBN_", "YHUZ98_KWBN_")
    rows = []
    token = None
    while True:
        kw = {"Bucket": NDFD_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            key = obj.get("Key", "")
            name = key.rsplit("/", 1)[-1]
            if not name.startswith(families):
                continue
            stamp = name.rsplit("_", 1)[-1][:12]
            try:
                ts = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            rows.append((ts, key))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")

    rows.sort(key=lambda row: row[0])
    _ndfd_day_cache[cache_key] = rows
    return rows


def ndfd_archive_keys(issue_dt):
    """Return the latest archived NDFD MinT object(s) available by issue_dt."""
    rows = []
    for day_offset in (0, -1):
        rows.extend(ndfd_day_objects((issue_dt + timedelta(days=day_offset)).date()))
    if not rows:
        raise RuntimeError(f"No archived NDFD MinT objects found near {issue_dt:%Y-%m-%d %H:%MZ}")

    # Select the most recent snapshot at or before the requested lead time.
    prior = [row for row in rows if row[0] <= issue_dt]
    if prior:
        selected_time = max(row[0] for row in prior)
    else:
        selected_time = min(rows, key=lambda row: abs((row[0] - issue_dt).total_seconds()))[0]

    # Keep all relevant MinT sector/product files carrying the exact same
    # dissemination timestamp so geographic coverage is preserved.
    keys = [key for ts, key in rows if ts == selected_time]
    if not keys:
        raise RuntimeError(f"No NDFD MinT object selected for {issue_dt:%Y-%m-%d %H:%MZ}")
    return keys

def decode_ndfd_object(key, issue_dt, valid_day):
    """Decode archived NDFD MinT messages matching one requested issuance cycle."""
    body = s3.get_object(Bucket=NDFD_BUCKET, Key=key)["Body"].read()
    messages = []
    pos = 0
    while True:
        a = body.find(b"GRIB", pos)
        if a < 0:
            break
        z = body.find(b"7777", a)
        if z < 0:
            break
        chunk = body[a:z+4]
        pos = z + 4
        fd, path = tempfile.mkstemp(suffix=".grib2")
        os.close(fd)
        try:
            Path(path).write_bytes(chunk)
            with open(path, "rb") as fh:
                gid = codes_grib_new_from_file(fh)
                if gid is None:
                    continue
                try:
                    vals = np.asarray(codes_get_array(gid, "values"), dtype=float)
                    lats = np.asarray(codes_get_array(gid, "latitudes"), dtype=float)
                    lons = np.asarray(codes_get_array(gid, "longitudes"), dtype=float)
                    units = str(codes_get(gid, "units") or "")
                    try:
                        vd = int(codes_get(gid, "validityDate"))
                        vt = int(codes_get(gid, "validityTime"))
                    except Exception:
                        vd = vt = None
                    try:
                        dd = int(codes_get(gid, "dataDate"))
                        dtm = int(codes_get(gid, "dataTime"))
                    except Exception:
                        dd = dtm = None
                finally:
                    codes_release(gid)
            if vd is not None and str(vd) != valid_day.strftime("%Y%m%d"):
                continue
            if vt is not None and int(vt) not in (1200, 120000):
                continue
            if "K" in units or units.lower().startswith("k"):
                vals = (vals - 273.15) * 9.0 / 5.0 + 32.0
            vals = np.where(np.isfinite(vals) & (vals > -120.0) & (vals < 160.0), vals, np.nan)
            lons = np.where(lons > 180, lons - 360, lons)
            if np.isfinite(vals).any():
                actual_stamp = key.rsplit("_", 1)[-1][:12]
                try:
                    actual_issue = datetime.strptime(actual_stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                except Exception:
                    actual_issue = issue_dt
                messages.append((vals, lats, lons, {"source": key, "issueUtc": actual_issue.isoformat()}))
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    return messages


def read_ndfd_archive(issue_dt, valid_day):
    """Return archived NDFD MinT grids from the requested historical issuance."""
    grids = []
    for key in ndfd_archive_keys(issue_dt):
        try:
            grids.extend(decode_ndfd_object(key, issue_dt, valid_day))
        except Exception:
            continue
    if not grids:
        raise RuntimeError(f"No usable archived NDFD MinT grids for {issue_dt:%Y-%m-%d %HZ} -> {valid_day}")
    return grids

def sample_grib_collection(grids, points):
    """Sample nearest value from a collection of NDFD sector grids."""
    out = np.full(len(points), np.nan, dtype=float)
    for n, (lat, lon, _) in enumerate(points):
        best_d = None
        best_v = None
        for vals, lats, lons, _ in grids:
            mask = np.isfinite(vals) & (lats >= lat - .5) & (lats <= lat + .5) & (lons >= lon - .5) & (lons <= lon + .5)
            if not mask.any():
                continue
            vv, yy, xx = vals[mask], lats[mask], lons[mask]
            d = (yy - lat) ** 2 + ((xx - lon) * math.cos(math.radians(lat))) ** 2
            j = int(np.argmin(d))
            if best_d is None or float(d[j]) < best_d:
                best_d = float(d[j])
                best_v = float(vv[j])
        if best_v is not None:
            out[n] = best_v
    return out


def nbm_keys(init_dt, lead_hours):
    ds = init_dt.strftime("%Y%m%d")
    hh = init_dt.strftime("%H")
    fh = f"{int(lead_hours):03d}"
    return (
        f"blend.{ds}/{hh}/core/blend.t{hh}z.core.f{fh}.co.grib2",
        f"blend.{ds}/{hh}/qmd/blend.t{hh}z.qmd.f{fh}.co.grib2",
    )


def grib_valid_datetime(meta):
    """Return GRIB validity timestamp in UTC when metadata provides it."""
    try:
        d = str(int(meta.get("validityDate"))).zfill(8)
        t = str(int(meta.get("validityTime", 0))).zfill(4)
        return datetime.strptime(d + t[-4:], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def grib_matches_valid(meta, expected_dt):
    got = grib_valid_datetime(meta)
    return got is not None and got == expected_dt.astimezone(timezone.utc)


def read_nbm(init_dt, lead_hours):
    core_pref, qmd_pref = nbm_keys(init_dt, lead_hours)
    ds = init_dt.strftime("%Y%m%d")
    hh = init_dt.strftime("%H")
    fh = f"{int(lead_hours):03d}"
    expected_valid = (init_dt + timedelta(hours=int(lead_hours))).astimezone(timezone.utc)
    core = find_key(NBM_BUCKET, core_pref, f"blend.{ds}/{hh}/core/", f"blend.t{hh}z.core.f{fh}.co.grib2")
    qmd = find_key(NBM_BUCKET, qmd_pref, f"blend.{ds}/{hh}/qmd/", f"blend.t{hh}z.qmd.f{fh}.co.grib2")

    core_lines = idx_lines(NBM_BUCKET, core)
    cand = [i for i,line in enumerate(core_lines) if ":TMIN:" in line.upper() and "MIN FCST" in line.upper() and "ENS STD DEV" not in line.upper()]
    if not cand:
        raise RuntimeError(f"Could not locate exact NBM deterministic TMIN message F{fh}")
    det = None
    rejected = []
    for ci in cand:
        trial = fetch_message(NBM_BUCKET, core, ci, core_lines)
        vals, _, _, meta = trial
        if not grib_matches_valid(meta, expected_valid):
            rejected.append(grib_valid_datetime(meta))
            continue
        if str(meta.get("stepType") or "").lower() not in ("min", "minimum"):
            continue
        finite = vals[np.isfinite(vals)]
        if finite.size and -80.0 < float(np.nanmedian(finite)) < 130.0:
            det = trial
            break
    if det is None:
        raise RuntimeError(
            f"NBM deterministic MinT F{fh} has no message valid exactly "
            f"{expected_valid:%Y-%m-%d %HZ}; candidates={rejected}"
        )

    qlines = idx_lines(NBM_BUCKET, qmd)
    min_candidates = select_idx_candidates(qlines, ["tmp", "min"])
    if not min_candidates:
        raise RuntimeError(f"Could not locate NBM percentile MinT messages F{fh}")
    pct = {}
    for i in min_candidates:
        vals, lats, lons, meta = fetch_message(NBM_BUCKET, qmd, i, qlines)
        if not grib_matches_valid(meta, expected_valid):
            continue
        if str(meta.get("stepType") or "").lower() not in ("min", "minimum"):
            continue
        p = safe_float(meta.get("percentileValue"))
        if p is not None and int(round(p)) in PERCENTILES:
            pct[int(round(p))] = (vals, lats, lons, meta)

    missing = [p for p in PERCENTILES if p not in pct]
    if missing:
        raise RuntimeError(
            f"NBM percentile MinT F{fh} missing {missing} at exact valid time "
            f"{expected_valid:%Y-%m-%d %HZ}"
        )
    return det, pct

def urma_tmin_key(day):
    ds = day.strftime("%Y%m%d")
    return f"urma2p5.{ds}/urma2p5.t20z.2dvaranl_ndfd.grb2_wexp"


def read_urma_tmin(day):
    """Read NOAA URMA full-grid minimum temperature analysis valid 12Z.

    Operational metadata: TMIN, 2 m above ground, stepType=min, 0-12 hour
    analysis period, validity at 12Z. URMA is the analysis of record used for
    verification/calibration.
    """
    key = urma_tmin_key(day)
    lines = idx_lines(URMA_BUCKET, key)
    cand = [i for i,line in enumerate(lines) if ":TMIN:" in line.upper()]
    if not cand:
        raise RuntimeError(f"Could not locate URMA TMIN for {day}")
    grib = fetch_message(URMA_BUCKET, key, cand[0], lines)
    meta = grib[3]
    expected = datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc)
    if not grib_matches_valid(meta, expected):
        raise RuntimeError(f"URMA TMIN validity mismatch for {day}: {grib_valid_datetime(meta)}")
    if str(meta.get("stepType") or "").lower() != "min":
        raise RuntimeError(f"URMA TMIN stepType is not min for {day}")
    if str(meta.get("stepRange") or "") not in ("0-12", "0-12h"):
        # Do not hard-fail solely on representation differences, but retain the
        # explicit validity/step-type checks above.
        pass
    return grib


def rtma_key(day, hour):
    ds = day.strftime("%Y%m%d")
    name = f"rtma2p5.t{hour:02d}z.2dvaranl_ndfd.grb2_wexp"
    preferred = f"rtma2p5.{ds}/{name}"
    return find_key(URMA_BUCKET, preferred, f"rtma2p5.{ds}/", name)


def read_rtma_hour(day, hour):
    key = rtma_key(day, hour)
    lines = idx_lines(URMA_BUCKET, key)
    cand = select_idx_candidates(lines, ["tmp", "2 m above ground"])
    if not cand:
        cand = select_idx_candidates(lines, ["tmp", "2 m"])
    if not cand:
        raise RuntimeError(f"Could not locate URMA 2m temperature {day} {hour:02d}Z")
    return fetch_message(URMA_BUCKET, key, cand[0], lines)


def sample_grib(grib, points):
    vals, lats, lons, _ = grib
    if not points:
        return np.array([], dtype=float)
    plat = np.array([p[0] for p in points])
    plon = np.array([p[1] for p in points])
    minlat, maxlat = plat.min() - .35, plat.max() + .35
    minlon, maxlon = plon.min() - .35, plon.max() + .35
    mask = (lats >= minlat) & (lats <= maxlat) & (lons >= minlon) & (lons <= maxlon) & np.isfinite(vals)
    sv, sla, slo = vals[mask], lats[mask], lons[mask]
    if not len(sv):
        raise RuntimeError("No GRIB cells in CWA sampling box")
    out = np.empty(len(points), dtype=float)
    for n, (lat, lon, _) in enumerate(points):
        d = (sla - lat) ** 2 + ((slo - lon) * math.cos(math.radians(lat))) ** 2
        out[n] = sv[int(np.argmin(d))]
    return out


def metrics(fcst, obs):
    f = np.asarray(fcst, dtype=float)
    o = np.asarray(obs, dtype=float)
    mask = np.isfinite(f) & np.isfinite(o)
    if not mask.any():
        return None
    e = f[mask] - o[mask]
    return {"mae": float(np.mean(np.abs(e))), "bias": float(np.mean(e)), "samples": int(mask.sum())}


def summarize_rows(rows, source_names):
    daily_mae = {src: [(((r or {}).get(src) or {}).get("mae")) for r in rows] for src in source_names}
    daily_bias = {src: [(((r or {}).get(src) or {}).get("bias")) for r in rows] for src in source_names}
    ranking = []
    for src in source_names:
        ms = [(r or {}).get(src) for r in rows if (r or {}).get(src)]
        if not ms:
            continue
        total_n = sum(m["samples"] for m in ms)
        if not total_n:
            continue
        mae = sum(m["mae"] * m["samples"] for m in ms) / total_n
        bias = sum(m["bias"] * m["samples"] for m in ms) / total_n
        ranking.append({"source": src, "mae": round(mae, 3), "bias": round(bias, 3), "samples": total_n})
    return {"dailyMae": daily_mae, "dailyBias": daily_bias, "ranking": ranking}


def main():
    if os.environ.get("DEBUG_NDFD_KEYS") == "1":
        target = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        print(json.dumps(debug_ndfd_keys(target, 50), indent=2))
        return

    geoms = cwa_geometries()
    try:
        marine_geoms = marine_geometries()
    except Exception as exc:
        marine_geoms = {w: [] for w in WFO_IDS}
        print(f"WARNING: marine-zone geometry unavailable: {exc}")
    now = datetime.now(timezone.utc)
    latest = now.date()
    if now.hour < 14:
        latest -= timedelta(days=1)
    verify_days = [latest - timedelta(days=i) for i in range(4, -1, -1)]
    lead_hours = [12, 24, 36, 48, 60]

    # daily[wfo][valid-date] contains a byLead dictionary. Archived NDFD and
    # NBM deterministic/percentiles are retained at every requested lead.
    spatial_modes = ["land", "marine", "both"]
    daily = {mode: {w: {} for w in WFO_IDS} for mode in spatial_modes}
    errors = []
    nbm_sources = ["NBM Deterministic"] + [f"P{p}" for p in PERCENTILES]
    default_sources = ["NDFD"] + nbm_sources

    for valid_day in verify_days:
        valid_dt = datetime(valid_day.year, valid_day.month, valid_day.day, 12, tzinfo=timezone.utc)
        issue_day = valid_day - timedelta(days=1)

        snap, sha, snap_time = historical_forecast_snapshot(issue_day)

        try:
            urma_tmin = read_urma_tmin(valid_day)
        except Exception as exc:
            errors.append(f"{valid_day}: URMA TMIN unavailable ({exc})")
            continue

        nbm_by_lead = {}
        ndfd_by_lead = {}

        for lead in lead_hours:
            init_dt = valid_dt - timedelta(hours=lead)
            try:
                nbm_by_lead[lead] = read_nbm(init_dt, lead)
            except Exception as exc:
                errors.append(f"{valid_day} NBM F{lead:03d}: {exc}")
            try:
                fpayload, period_key, fsha, issue_dt = historical_forecast_at(init_dt, valid_day)
                if fpayload is None:
                    raise RuntimeError("no forecast.json snapshot at or before target lead containing verifying night")
                ndfd_by_lead[lead] = (fpayload, period_key, fsha, issue_dt)
            except Exception as exc:
                errors.append(f"{valid_day} NDFD F{lead:03d}: {exc}")

        geometry_snap = snap
        if geometry_snap is None:
            try:
                geometry_snap = json.loads((DATA / "forecast.json").read_text())
            except Exception:
                geometry_snap = None

        # Target the raw archived NDFD MinT snapshot once per lead. Unlike
        # forecast.json, the WMO grid extends over coastal waters and lets the
        # same NDFD source be verified for land, marine, and combined samples.
        ndfd_wmo_by_lead = {}
        for lead in lead_hours:
            target_dt = valid_dt - timedelta(hours=lead)
            try:
                keys = ndfd_archive_keys(target_dt)
                stamp = keys[0].rsplit("_", 1)[-1][:12]
                actual_issue = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                ndfd_wmo_by_lead[lead] = (read_ndfd_archive(actual_issue, valid_day), actual_issue)
            except Exception as exc:
                errors.append(f"{valid_day} NDFD-WMO F{lead:03d}: {exc}")

        for wfo in WFO_IDS:
            field = ((geometry_snap or {}).get("fields") or {}).get(wfo)
            if not field:
                continue
            land_points = sample_nodes(field, geoms[wfo])
            marine_points = regular_points_in_geoms(marine_geoms.get(wfo) or [])
            scope_points = {"land": land_points}
            if wfo in MARINE_WFO_IDS and len(marine_points) >= 3:
                scope_points["marine"] = marine_points
                scope_points["both"] = land_points + marine_points

            for spatial_mode, points in scope_points.items():
                if len(points) < 3:
                    continue

                try:
                    obs = sample_grib(urma_tmin, points)
                except Exception as exc:
                    errors.append(f"{valid_day} {wfo} {spatial_mode}: URMA TMIN sample failed ({exc})")
                    continue

                row = {
                    "date": valid_day.isoformat(),
                    "snapshot": sha,
                    "snapshotUtc": snap_time.isoformat() if snap_time else None,
                    "sampleMode": spatial_mode,
                    "sampleCount": len(points),
                    "byLead": {},
                }

                for lead in lead_hours:
                    lead_row = {}

                    ndfd_item = ndfd_wmo_by_lead.get(lead)
                    if ndfd_item is not None:
                        try:
                            ndfd_grids, ndfd_issue_dt = ndfd_item
                            ndfd = sample_grib_collection(ndfd_grids, points)
                            lead_row["NDFD"] = metrics(ndfd, obs)
                            lead_row["NDFDIssueUtc"] = ndfd_issue_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                        except Exception as exc:
                            lead_row["NDFD"] = None
                            errors.append(f"{valid_day} {wfo} {spatial_mode} NDFD F{lead:03d}: sampling failed ({exc})")
                    else:
                        lead_row["NDFD"] = None

                    pair = nbm_by_lead.get(lead)
                    if pair:
                        try:
                            nbm_det, nbm_pct = pair
                            det = sample_grib(nbm_det, points)
                            lead_row["NBM Deterministic"] = metrics(det, obs)
                            for p in PERCENTILES:
                                lead_row[f"P{p}"] = metrics(sample_grib(nbm_pct[p], points), obs) if p in nbm_pct else None
                        except Exception as exc:
                            errors.append(f"{valid_day} {wfo} {spatial_mode} NBM F{lead:03d}: sampling failed ({exc})")
                    row["byLead"][str(lead)] = lead_row

                lead24 = row["byLead"].get("24") or {}
                row["NDFD"] = lead24.get("NDFD")
                for src in nbm_sources:
                    row[src] = lead24.get(src)

                daily[spatial_mode][wfo][valid_day.isoformat()] = row

    cwas = {}
    dates = [d.isoformat() for d in verify_days]
    for wfo in WFO_IDS:
        scopes = {}
        supported_modes = ["land"] + (["marine", "both"] if wfo in MARINE_WFO_IDS else [])
        for spatial_mode in supported_modes:
            base_rows = [daily[spatial_mode][wfo].get(d) for d in dates]
            base_summary = summarize_rows(base_rows, default_sources)
            by_lead = {}
            for lead in lead_hours:
                rows = []
                for d in dates:
                    daily_row = daily[spatial_mode][wfo].get(d) or {}
                    rows.append(dict((daily_row.get("byLead") or {}).get(str(lead)) or {}))
                summary = summarize_rows(rows, default_sources)
                summary["leadHours"] = lead
                summary["leadLabel"] = f"{lead} h before verifying 12Z"
                by_lead[str(lead)] = summary
            scopes[spatial_mode] = {
                "dates": dates,
                "dailyMae": base_summary["dailyMae"],
                "dailyBias": base_summary["dailyBias"],
                "ranking": base_summary["ranking"],
                "byLead": by_lead,
                "leadHours": lead_hours,
                "defaultLeadHours": 24,
                "leadLabel": "24 h before verifying 12Z MinT",
                "sampleMode": spatial_mode,
            }

        # Preserve the land block at the top level for backward compatibility.
        land = scopes.get("land") or {}
        cwas[wfo] = {**land, "scopes": scopes}

    payload = {
        "schemaVersion": 4,
        "generatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nbmVersion": "NBM v5.1",
        "element": "MinT",
        "verification": "URMA TMIN",
        "validTimeDefinition": "Forecast MinT valid at 12Z; observed verification uses the NOAA URMA TMIN full grid, a 0-12Z minimum-temperature analysis valid at 12Z.",
        "windowDays": 5,
        "leadHours": lead_hours,
        "defaultLeadHours": 24,
        "spatialModes": spatial_modes,
        "method": "Rolling CWA MAE and mean bias versus URMA for land, official coastal-marine-zone, or combined samples. Historical NDFD MinT comes from timestamped NOAA WMO archive snapshots; NBM deterministic and percentiles use corresponding archived model cycles.",
        "cwas": cwas,
        "errors": errors[-60:],
    }
    # Do not publish a dataset that silently collapses all leads back to the
    # legacy 24-hour series.  The viewer's lead slider and One Night mode
    # require a populated byLead block for every CWA.
    missing_leads = []
    for wfo, block in cwas.items():
        supported_modes = ["land"] + (["marine", "both"] if wfo in MARINE_WFO_IDS else [])
        for spatial_mode in supported_modes:
            scope = (block.get("scopes") or {}).get(spatial_mode) or {}
            present = set((scope.get("byLead") or {}).keys())
            required = {str(h) for h in lead_hours}
            if not required.issubset(present):
                missing_leads.append(f"{wfo}/{spatial_mode}: missing {sorted(required - present)}")
    if missing_leads:
        raise RuntimeError("Lead-time verification schema incomplete: " + "; ".join(missing_leads))

    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Wrote {OUTPUT} (schemaVersion=4; land/marine/both NDFD + NBM verification preserved)")
    for w in WFO_IDS:
        print(w, len(cwas[w]["dates"]), cwas[w]["byLead"].get("24", {}).get("ranking", [])[:3])
    if errors:
        print("Warnings:")
        for e in errors:
            print(" -", e)


if __name__ == "__main__":
    main()
