#!/usr/bin/env python3
"""Build rolling CWA MinT verification for the cold-season viewer.

Method:
- Five most recent completed 12Z MinT periods.
- Forecast snapshot/cycle: 12Z on the preceding day.
- NDFD: historical forecast.json snapshots already committed by this repository.
- NBM: operational CONUS Core + QMD GRIB2 from NOAA Open Data (12Z, F024).
- Verification: hourly RTMA 2-m temperature from 00Z through 12Z; the minimum
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
import re
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
RTMA_BUCKET = "noaa-rtma-pds"
NDFD_BUCKET = "noaa-ndfd-pds"
PERCENTILES = [10, 25, 50, 75, 90]

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
                for name in ("shortName", "name", "stepType", "typeOfStatisticalProcessing", "percentileValue", "validityDate", "validityTime", "dataDate", "dataTime", "forecastTime"):
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


def _parse_iso_utc(value):
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ndfd_issuance_times(valid_day):
    """Return NOAA-published NDFD issuance/reference times for one MinT valid time."""
    valid_text = datetime(valid_day.year, valid_day.month, valid_day.day, 12, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M")
    query = urlencode({
        "grid": "ndfd",
        "elmt": "mint",
        "region": "conus",
        "vt": valid_text,
    })
    url = "https://digital.weather.gov/scripts/get_issuancetimes.php?" + query
    req = Request(url, headers={"User-Agent": "Regional-Cold-Season-Verification/1.0"})
    with urlopen(req, timeout=45) as resp:
        raw = resp.read().decode("utf-8", errors="replace")

    values = []
    try:
        payload = json.loads(raw)
        def walk(obj):
            if isinstance(obj, str):
                values.append(obj)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item)
            elif isinstance(obj, dict):
                for item in obj.values():
                    walk(item)
        walk(payload)
    except Exception:
        # Be tolerant of endpoint formatting changes; recover ISO timestamps
        # directly from the response body.
        values.extend(re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?", raw))

    times = []
    seen = set()
    for value in values:
        # Some JSON values contain surrounding descriptive text.
        matches = re.findall(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?", value)
        if not matches and isinstance(value, str):
            matches = [value]
        for token in matches:
            dt = _parse_iso_utc(token)
            if dt is not None and dt not in seen:
                seen.add(dt)
                times.append(dt)

    times.sort()
    if not times:
        raise RuntimeError(f"No NDFD issuance times returned for MinT valid {valid_text}")
    return times


def nearest_ndfd_issue(valid_day, target_dt, issuance_times=None):
    """Pick the NDFD reference time closest to a requested forecast lead.

    Prefer an issuance at or before the target so the verification never uses
    information that was not yet available at that lead. If none exist, use
    the earliest available issuance.
    """
    times = issuance_times if issuance_times is not None else ndfd_issuance_times(valid_day)
    prior = [dt for dt in times if dt <= target_dt]
    if prior:
        return min(prior, key=lambda dt: abs((dt - target_dt).total_seconds()))
    return min(times, key=lambda dt: abs((dt - target_dt).total_seconds()))


def ndfd_archive_keys(issue_dt):
    """List archived NDFD MinT WMO files nearest the requested issuance time."""
    root = discover_ndfd_mint_prefix()
    issue_day = issue_dt.date()
    prefix = f"{root}{issue_day:%Y/%m/%d}/"
    ranked = []
    token = None
    target_minutes = issue_dt.hour * 60 + issue_dt.minute
    while True:
        kw = {"Bucket": NDFD_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            rank = 99999
            tail = key.rsplit("_", 1)[-1]
            try:
                ts = datetime.strptime(tail[:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                rank = abs((ts.hour * 60 + ts.minute) - target_minutes)
            except Exception:
                pass
            ranked.append((rank, key))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    if not ranked:
        raise RuntimeError(f"No archived NDFD MinT WMO files found for {issue_day}")
    return [k for _, k in sorted(ranked)]


def decode_ndfd_object(key, issue_dt, valid_day):
    """Decode archived NDFD MinT messages matching one requested issuance cycle."""
    body = s3.get_object(Bucket=NDFD_BUCKET, Key=key)["Body"].read()
    messages = []
    pos = 0
    target_date = issue_dt.strftime("%Y%m%d")
    target_hour = issue_dt.hour
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
            if dd is not None and str(dd) != target_date:
                continue
            if dtm is not None:
                hh = int(dtm) // 100 if int(dtm) >= 100 else int(dtm)
                # NDFD grids can be disseminated shortly around the nominal
                # issuance time. Accept +/- 1 hour but do not substitute a
                # different forecast cycle.
                delta = abs(hh - target_hour)
                delta = min(delta, 24 - delta)
                if delta > 1:
                    continue
            if "K" in units or units.lower().startswith("k"):
                vals = (vals - 273.15) * 9.0 / 5.0 + 32.0
            vals = np.where(np.isfinite(vals) & (vals > -120.0) & (vals < 160.0), vals, np.nan)
            lons = np.where(lons > 180, lons - 360, lons)
            if np.isfinite(vals).any():
                messages.append((vals, lats, lons, {"source": key, "issueUtc": issue_dt.isoformat()}))
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
        if len(grids) >= 8:
            break
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
    base = f"blend.{ds}/{hh}/grib2"
    return (
        f"{base}/blend.t{hh}z.core.f{fh}.co.grib2",
        f"{base}/blend.t{hh}z.qmd.f{fh}.co.grib2",
    )


def read_nbm(init_dt, lead_hours):
    core_pref, qmd_pref = nbm_keys(init_dt, lead_hours)
    ds = init_dt.strftime("%Y%m%d")
    hh = init_dt.strftime("%H")
    fh = f"{int(lead_hours):03d}"
    core = find_key(NBM_BUCKET, core_pref, f"blend.{ds}/{hh}/", f"blend.t{hh}z.core.f{fh}.co.grib2")
    qmd = find_key(NBM_BUCKET, qmd_pref, f"blend.{ds}/{hh}/", f"blend.t{hh}z.qmd.f{fh}.co.grib2")

    core_lines = idx_lines(NBM_BUCKET, core)
    cand = select_idx_candidates(core_lines, ["tmp", "min"])
    if not cand:
        cand = select_idx_candidates(core_lines, ["tmp", "2 m above ground"])
    if not cand:
        raise RuntimeError(f"Could not locate NBM deterministic MinT message F{fh}")
    det = None
    for ci in reversed(cand):
        trial = fetch_message(NBM_BUCKET, core, ci, core_lines)
        vals = trial[0]
        finite = vals[np.isfinite(vals)]
        if finite.size and -80.0 < float(np.nanmedian(finite)) < 130.0:
            det = trial
            break
    if det is None:
        raise RuntimeError(f"NBM deterministic MinT F{fh} failed physical-range checks")

    qlines = idx_lines(NBM_BUCKET, qmd)
    min_candidates = select_idx_candidates(qlines, ["tmp", "min"])
    if not min_candidates:
        min_candidates = select_idx_candidates(qlines, ["tmp", "2 m above ground"])
    pct = {}
    for i in min_candidates:
        vals, lats, lons, meta = fetch_message(NBM_BUCKET, qmd, i, qlines)
        p = safe_float(meta.get("percentileValue"))
        if p is not None and int(round(p)) in PERCENTILES:
            pct[int(round(p))] = (vals, lats, lons, meta)
    if len(pct) < 3:
        for p in PERCENTILES:
            if p in pct:
                continue
            tokens = [f"{p}% level", f"{p} percentile", f"percentile={p}"]
            for i in min_candidates:
                if any(t in qlines[i].lower() for t in tokens):
                    pct[p] = fetch_message(NBM_BUCKET, qmd, i, qlines)
                    break
    return det, pct

def rtma_key(day, hour):
    ds = day.strftime("%Y%m%d")
    name = f"rtma2p5.t{hour:02d}z.2dvaranl_ndfd.grb2_wexp"
    preferred = f"rtma2p5.{ds}/{name}"
    return find_key(RTMA_BUCKET, preferred, f"rtma2p5.{ds}/", name)


def read_rtma_hour(day, hour):
    key = rtma_key(day, hour)
    lines = idx_lines(RTMA_BUCKET, key)
    cand = select_idx_candidates(lines, ["tmp", "2 m above ground"])
    if not cand:
        cand = select_idx_candidates(lines, ["tmp", "2 m"])
    if not cand:
        raise RuntimeError(f"Could not locate RTMA 2m temperature {day} {hour:02d}Z")
    return fetch_message(RTMA_BUCKET, key, cand[0], lines)


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
    geoms = cwa_geometries()
    now = datetime.now(timezone.utc)
    latest = now.date()
    if now.hour < 14:
        latest -= timedelta(days=1)
    verify_days = [latest - timedelta(days=i) for i in range(4, -1, -1)]
    lead_hours = [12, 24, 36, 48, 60]

    # daily[wfo][valid-date] contains a byLead dictionary. Archived NDFD and
    # NBM deterministic/percentiles are retained at every requested lead.
    daily = {w: {} for w in WFO_IDS}
    errors = []
    nbm_sources = ["NBM Deterministic"] + [f"P{p}" for p in PERCENTILES]
    default_sources = ["NDFD"] + nbm_sources

    for valid_day in verify_days:
        valid_dt = datetime(valid_day.year, valid_day.month, valid_day.day, 12, tzinfo=timezone.utc)
        issue_day = valid_day - timedelta(days=1)

        snap, sha, snap_time = historical_forecast_snapshot(issue_day)

        try:
            rtma_hours = [read_rtma_hour(valid_day, h) for h in range(0, 13)]
        except Exception as exc:
            errors.append(f"{valid_day}: RTMA unavailable ({exc})")
            continue

        nbm_by_lead = {}
        ndfd_by_lead = {}
        try:
            ndfd_issue_list = ndfd_issuance_times(valid_day)
        except Exception as exc:
            ndfd_issue_list = []
            errors.append(f"{valid_day} NDFD issuance-time lookup failed: {exc}")

        for lead in lead_hours:
            init_dt = valid_dt - timedelta(hours=lead)
            try:
                nbm_by_lead[lead] = read_nbm(init_dt, lead)
            except Exception as exc:
                errors.append(f"{valid_day} NBM F{lead:03d}: {exc}")
            try:
                issue_dt = nearest_ndfd_issue(valid_day, init_dt, ndfd_issue_list) if ndfd_issue_list else init_dt
                grids = read_ndfd_archive(issue_dt, valid_day)
                ndfd_by_lead[lead] = (grids, issue_dt)
            except Exception as exc:
                errors.append(f"{valid_day} NDFD F{lead:03d}: {exc}")

        geometry_snap = snap
        if geometry_snap is None:
            try:
                geometry_snap = json.loads((DATA / "forecast.json").read_text())
            except Exception:
                geometry_snap = None

        for wfo in WFO_IDS:
            field = ((geometry_snap or {}).get("fields") or {}).get(wfo)
            if not field:
                continue
            points = sample_nodes(field, geoms[wfo])
            if len(points) < 3:
                continue

            try:
                hourly = [sample_grib(g, points) for g in rtma_hours]
                obs = np.nanmin(np.vstack(hourly), axis=0)
            except Exception as exc:
                errors.append(f"{valid_day} {wfo}: RTMA sample failed ({exc})")
                continue

            row = {
                "date": valid_day.isoformat(),
                "snapshot": sha,
                "snapshotUtc": snap_time.isoformat() if snap_time else None,
                "byLead": {},
            }

            for lead in lead_hours:
                lead_row = {}

                ndfd_item = ndfd_by_lead.get(lead)
                if ndfd_item is not None:
                    try:
                        ndfd_grids, ndfd_issue_dt = ndfd_item
                        ndfd = sample_grib_collection(ndfd_grids, points)
                        lead_row["NDFD"] = metrics(ndfd, obs)
                        lead_row["NDFDIssueUtc"] = ndfd_issue_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    except Exception as exc:
                        lead_row["NDFD"] = None
                        errors.append(f"{valid_day} {wfo} NDFD F{lead:03d}: sampling failed ({exc})")
                elif lead == 24 and snap is not None:
                    # Preserve the repository snapshot only as a last-resort
                    # fallback for the traditional 24-hour NDFD reference.
                    try:
                        snap_field = (snap.get("fields") or {}).get(wfo) or {}
                        snap_points = sample_nodes(snap_field, geoms[wfo], require_values=True)
                        ndfd = np.full(len(points), np.nan, dtype=float)
                        if snap_points:
                            sla = np.array([p[0] for p in snap_points])
                            slo = np.array([p[1] for p in snap_points])
                            sv = np.array([p[2] for p in snap_points], dtype=float)
                            for n, (lat, lon, _) in enumerate(points):
                                d = (sla - lat) ** 2 + ((slo - lon) * math.cos(math.radians(lat))) ** 2
                                ndfd[n] = sv[int(np.argmin(d))]
                        lead_row["NDFD"] = metrics(ndfd, obs)
                    except Exception as exc:
                        lead_row["NDFD"] = None
                        errors.append(f"{valid_day} {wfo} NDFD F024 snapshot fallback failed ({exc})")
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
                        errors.append(f"{valid_day} {wfo} NBM F{lead:03d}: sampling failed ({exc})")
                row["byLead"][str(lead)] = lead_row

            # Backward-compatible 24-hour fields for the existing viewer.
            lead24 = row["byLead"].get("24") or {}
            row["NDFD"] = lead24.get("NDFD")
            for src in nbm_sources:
                row[src] = lead24.get(src)

            daily[wfo][valid_day.isoformat()] = row

    cwas = {}
    dates = [d.isoformat() for d in verify_days]
    for wfo in WFO_IDS:
        base_rows = [daily[wfo].get(d) for d in dates]
        base_summary = summarize_rows(base_rows, default_sources)
        by_lead = {}

        for lead in lead_hours:
            rows = []
            for d in dates:
                daily_row = daily[wfo].get(d) or {}
                lr = dict((daily_row.get("byLead") or {}).get(str(lead)) or {})
                rows.append(lr)
            summary = summarize_rows(rows, default_sources)
            summary["leadHours"] = lead
            summary["leadLabel"] = f"{lead} h before verifying 12Z"
            by_lead[str(lead)] = summary

        cwas[wfo] = {
            "dates": dates,
            "dailyMae": base_summary["dailyMae"],
            "dailyBias": base_summary["dailyBias"],
            "ranking": base_summary["ranking"],
            "byLead": by_lead,
            "leadHours": lead_hours,
            "defaultLeadHours": 24,
            "leadLabel": "24 h before verifying 12Z MinT",
            "sampleMode": "land",
            "method": "CWA land-only sample nodes; NDFD uses the NOAA-published issuance/reference time at or immediately before each 12/24/36/48/60-hour target lead, while NBM uses its corresponding model cycle. Both are compared with minimum hourly RTMA 2-m temperature from 00Z–12Z.",
        }

    payload = {
        "schemaVersion": 3,
        "generatedUtc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nbmVersion": "NBM v5.1",
        "element": "MinT",
        "verification": "RTMA",
        "windowDays": 5,
        "leadHours": lead_hours,
        "defaultLeadHours": 24,
        "spatialModes": ["land"],
        "method": "Rolling CWA land MAE and mean bias versus RTMA. NDFD history is keyed to NOAA reference/issuance times nearest but not later than each requested lead; NBM deterministic and percentiles use the corresponding archived model cycles.",
        "cwas": cwas,
        "errors": errors[-60:],
    }
    # Do not publish a dataset that silently collapses all leads back to the
    # legacy 24-hour series.  The viewer's lead slider and One Night mode
    # require a populated byLead block for every CWA.
    missing_leads = []
    for wfo, block in cwas.items():
        present = set((block.get("byLead") or {}).keys())
        required = {str(h) for h in lead_hours}
        if not required.issubset(present):
            missing_leads.append(f"{wfo}: missing {sorted(required - present)}")
    if missing_leads:
        raise RuntimeError("Lead-time verification schema incomplete: " + "; ".join(missing_leads))

    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Wrote {OUTPUT} (schemaVersion=3; archived NDFD issuance history + NBM lead cycles preserved)")
    for w in WFO_IDS:
        print(w, len(cwas[w]["dates"]), cwas[w]["byLead"].get("24", {}).get("ranking", [])[:3])
    if errors:
        print("Warnings:")
        for e in errors:
            print(" -", e)


if __name__ == "__main__":
    main()
