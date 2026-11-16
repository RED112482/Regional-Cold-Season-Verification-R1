#!/usr/bin/env python3
"""Backfill historical NDFD lead verification into the existing verification.json.

Uses this repository's committed forecast.json snapshots as the historical NDFD
record. This avoids rebuilding NBM guidance when only NDFD lead history changes.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import build_verification as bv

OUTPUT = Path("site/data/verification.json")
LEADS = [12, 24, 36, 48, 60]


def main():
    payload = json.loads(OUTPUT.read_text())
    cwas = payload.get("cwas") or {}
    geoms = bv.cwa_geometries()

    dates = None
    for block in cwas.values():
        if block.get("dates"):
            dates = list(block["dates"])
            break
    if not dates:
        raise RuntimeError("verification.json contains no verifying dates")

    daily = {w: {lead: [] for lead in LEADS} for w in bv.WFO_IDS}
    issue_meta = {w: {lead: [] for lead in LEADS} for w in bv.WFO_IDS}
    errors = []

    for date_text in dates:
        valid_day = datetime.strptime(date_text, "%Y-%m-%d").date()
        valid_dt = datetime(valid_day.year, valid_day.month, valid_day.day, 12, tzinfo=timezone.utc)
        issue_day = valid_day - timedelta(days=1)

        geometry_snap, _, _ = bv.historical_forecast_snapshot(issue_day)
        if geometry_snap is None:
            try:
                geometry_snap = json.loads(Path("site/data/forecast.json").read_text())
            except Exception:
                geometry_snap = None
        if geometry_snap is None:
            errors.append(f"{valid_day}: no geometry snapshot")
            for w in bv.WFO_IDS:
                for lead in LEADS:
                    daily[w][lead].append(None)
                    issue_meta[w][lead].append(None)
            continue

        try:
            rtma_hours = [bv.read_rtma_hour(valid_day, h) for h in range(0, 13)]
        except Exception as exc:
            errors.append(f"{valid_day}: RTMA unavailable ({exc})")
            for w in bv.WFO_IDS:
                for lead in LEADS:
                    daily[w][lead].append(None)
                    issue_meta[w][lead].append(None)
            continue

        snapshots = {}
        for lead in LEADS:
            target = valid_dt - timedelta(hours=lead)
            fpayload, period_key, sha, issue_dt = bv.historical_forecast_at(target, valid_day)
            snapshots[lead] = (fpayload, period_key, sha, issue_dt)
            if fpayload is None:
                errors.append(f"{valid_day} F{lead:03d}: no forecast snapshot")

        for wfo in bv.WFO_IDS:
            field = ((geometry_snap.get("fields") or {}).get(wfo) or {})
            if not field:
                for lead in LEADS:
                    daily[wfo][lead].append(None)
                    issue_meta[wfo][lead].append(None)
                continue

            points = bv.sample_nodes(field, geoms[wfo])
            if len(points) < 3:
                for lead in LEADS:
                    daily[wfo][lead].append(None)
                    issue_meta[wfo][lead].append(None)
                continue

            try:
                hourly = [bv.sample_grib(g, points) for g in rtma_hours]
                obs = np.nanmin(np.vstack(hourly), axis=0)
            except Exception as exc:
                errors.append(f"{valid_day} {wfo}: RTMA sampling failed ({exc})")
                for lead in LEADS:
                    daily[wfo][lead].append(None)
                    issue_meta[wfo][lead].append(None)
                continue

            for lead in LEADS:
                fpayload, period_key, sha, issue_dt = snapshots[lead]
                metric = None
                meta = None
                if fpayload is not None and period_key:
                    try:
                        hist_field = ((fpayload.get("fields") or {}).get(wfo) or {})
                        fcst = bv.sample_snapshot_field(hist_field, period_key, points)
                        metric = bv.metrics(fcst, obs)
                        meta = {
                            "issueUtc": issue_dt.strftime("%Y-%m-%dT%H:%M:%SZ") if issue_dt else None,
                            "snapshot": sha,
                            "period": period_key,
                        }
                    except Exception as exc:
                        errors.append(f"{valid_day} {wfo} F{lead:03d}: {exc}")
                daily[wfo][lead].append(metric)
                issue_meta[wfo][lead].append(meta)

    for wfo in bv.WFO_IDS:
        block = cwas.get(wfo)
        if not block:
            continue
        by_lead = block.setdefault("byLead", {})
        for lead in LEADS:
            lead_block = by_lead.setdefault(str(lead), {})
            rows = [{"NDFD": m} if m else {} for m in daily[wfo][lead]]
            summary = bv.summarize_rows(rows, ["NDFD"])
            lead_block.setdefault("dailyMae", {})["NDFD"] = summary["dailyMae"]["NDFD"]
            lead_block.setdefault("dailyBias", {})["NDFD"] = summary["dailyBias"]["NDFD"]
            ranking = [r for r in (lead_block.get("ranking") or []) if r.get("source") != "NDFD"]
            if summary["ranking"]:
                ranking.insert(0, summary["ranking"][0])
            lead_block["ranking"] = ranking
            lead_block["ndfdHistory"] = issue_meta[wfo][lead]

        # Preserve the legacy top-level 24h NDFD arrays too.
        lead24 = by_lead.get("24") or {}
        block.setdefault("dailyMae", {})["NDFD"] = (lead24.get("dailyMae") or {}).get("NDFD", [])
        block.setdefault("dailyBias", {})["NDFD"] = (lead24.get("dailyBias") or {}).get("NDFD", [])
        top_rank = [r for r in (block.get("ranking") or []) if r.get("source") != "NDFD"]
        ndfd24_rank = next((r for r in (lead24.get("ranking") or []) if r.get("source") == "NDFD"), None)
        if ndfd24_rank:
            top_rank.insert(0, ndfd24_rank)
        block["ranking"] = top_rank

    # Hard validation: do not publish another 24h-only NDFD dataset.
    missing = []
    for wfo in bv.WFO_IDS:
        block = cwas.get(wfo) or {}
        for lead in LEADS:
            vals = ((((block.get("byLead") or {}).get(str(lead)) or {}).get("dailyMae") or {}).get("NDFD") or [])
            if not any(v is not None for v in vals):
                missing.append(f"{wfo} F{lead:03d}")
    if missing:
        raise RuntimeError("NDFD history incomplete: " + ", ".join(missing))

    payload["schemaVersion"] = 3
    payload["ndfdHistorySource"] = "Repository forecast.json snapshots"
    payload["ndfdHistoryGeneratedUtc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["method"] = (
        "Rolling CWA land MAE and mean bias versus RTMA. NDFD uses the most recent "
        "forecast.json snapshot available at or before each 12/24/36/48/60-hour lead "
        "and the overnight period ending on the verifying date; NBM uses archived model cycles."
    )
    payload["errors"] = (payload.get("errors") or [])[-30:] + errors[-30:]
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    print("NDFD lead history backfilled successfully.")
    for wfo in bv.WFO_IDS:
        print(wfo, {
            lead: ((((cwas[wfo]["byLead"][str(lead)]["dailyMae"]).get("NDFD"))))
            for lead in LEADS
        })


if __name__ == "__main__":
    main()
