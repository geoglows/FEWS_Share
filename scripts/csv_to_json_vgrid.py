#!/usr/bin/env python3
"""
csv_to_json_vgrid.py — flood CSVs -> per-point flood JSON (keeps lat/lon).

Reads:  Flood Hub + GEOGLOWS exports (auto-detected by columns; see CONFIG)
Writes: ../flood_points.json   flat list, one entry per model-per-point, each with
                               lat/lon + severity (consumed by build_cells_*.py)
"""

import argparse
import csv
import json
import os
import sys

# Paths are relative to this file (<repo>/scripts), so ../Files is <repo>/Files.
INPUT_CSVS = ["../Files/Flood_Hub_Global.csv", "../Files/Geoglows_2026-07-13-00.csv"]
OUTPUT = "../flood_points.json"

# Flood Hub interpretation: three tiers by return period.
FLOOD_HUB_SEVERITY = {
    "ABOVE_NORMAL": "warning",
    "SEVERE": "danger",
    "EXTREME": "extreme",
}

# GEOGLOWS has no severity, only ret_per (years). Same interpretation:
# >=20-yr = extreme, >=5-yr = danger, >=2-yr = warning; below 2-yr not shown.
GEOGLOWS_SEVERITY_THRESHOLDS = [(20, "extreme"), (5, "danger"), (2, "warning")]

# Minimum GEOGLOWS mean flow (m³/s) to keep; 0 keeps everything.
GEOGLOWS_MIN_MEAN_FLOW = 5


def geoglows_severity(ret_per):
    for thr, sev in GEOGLOWS_SEVERITY_THRESHOLDS:
        if ret_per >= thr:
            return sev
    return None

FLOOD_HUB_SIGNATURE = {"gaugeId", "queriedCountryName", "gaugeLocation.latitude"}
GEOGLOWS_SIGNATURE = {"comid", "ret_per", "lat", "lon"}
CANONICAL_SIGNATURE = {"lat", "lon", "severity", "model"}


def _clean(v) -> str:
    return (v or "").strip()


def _is_flood_hub(fieldnames) -> bool:
    return FLOOD_HUB_SIGNATURE.issubset({c.strip() for c in (fieldnames or [])})


def _is_geoglows(fieldnames) -> bool:
    return GEOGLOWS_SIGNATURE.issubset({c.strip() for c in (fieldnames or [])})


def _is_canonical(fieldnames) -> bool:
    return CANONICAL_SIGNATURE.issubset({c.strip() for c in (fieldnames or [])})


def _float_or_none(v):
    v = _clean(v)
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def adapt_flood_hub(raw_rows):
    out = []
    skipped_no_coord = 0
    for r in raw_rows:
        raw_sev = _clean(r.get("severity")).upper()
        if raw_sev in ("NO_FLOODING", "", "UNKNOWN"):
            continue
        lat = _float_or_none(r.get("gaugeLocation.latitude"))
        lon = _float_or_none(r.get("gaugeLocation.longitude"))
        if lat is None or lon is None:
            skipped_no_coord += 1
            continue
        gauge_id = _clean(r.get("gaugeId"))
        out.append({
            "model": "flood_hub",
            "severity": FLOOD_HUB_SEVERITY.get(raw_sev, raw_sev.lower()),
            "lat": lat,
            "lon": lon,
            "riverId": gauge_id,
            "country": _clean(r.get("queriedCountryName")),
            "issuedTime": _clean(r.get("issuedTime")),
            "startTime": _clean(r.get("forecastTimeRange.start")),
            "peakTime": "",
            "endTime": _clean(r.get("forecastTimeRange.end")),
            "returnPeriodYr": "",
            "peakDischargeCms": "",
            "historicalComparison": "",
        })
    if skipped_no_coord:
        print(f"  note: {skipped_no_coord} flooding row(s) had no coordinate "
              f"and were skipped (can't be gridded).", file=sys.stderr)
    return out


def adapt_geoglows(rows):
    out = []
    skipped_no_coord = 0
    skipped_low_flow = 0
    for r in rows:
        rp_raw = _clean(r.get("ret_per"))
        try:
            rp = int(float(rp_raw))
        except (TypeError, ValueError):
            rp = 0
        sev = geoglows_severity(rp)
        if sev is None:
            continue
        mean_flow = _float_or_none(r.get("mean"))
        if GEOGLOWS_MIN_MEAN_FLOW and (mean_flow is None
                                       or mean_flow < GEOGLOWS_MIN_MEAN_FLOW):
            skipped_low_flow += 1
            continue
        lat = _float_or_none(r.get("lat"))
        lon = _float_or_none(r.get("lon"))
        if lat is None or lon is None:
            skipped_no_coord += 1
            continue
        out.append({
            "model": "geoglows",
            "severity": sev,
            "lat": lat,
            "lon": lon,
            "riverId": _clean(r.get("comid")),
            "country": "",
            "issuedTime": "",
            "startTime": "",
            "peakTime": "",
            "endTime": "",
            "returnPeriodYr": rp,
            "peakDischargeCms": _clean(r.get("mean")),
            "historicalComparison": "",
        })
    if skipped_low_flow:
        print(f"  note: {skipped_low_flow} GEOGLOWS reach(es) below the "
              f"{GEOGLOWS_MIN_MEAN_FLOW} m³/s mean-flow floor were skipped.",
              file=sys.stderr)
    if skipped_no_coord:
        print(f"  note: {skipped_no_coord} GEOGLOWS flood row(s) had no coordinate "
              f"and were skipped. Run comid_to_coordinate.py to fill lat/lon.",
              file=sys.stderr)
    return out


def adapt_canonical(rows):
    out = []
    for r in rows:
        lat = _float_or_none(r.get("lat"))
        lon = _float_or_none(r.get("lon"))
        if lat is None or lon is None:
            continue
        out.append({
            "model": _clean(r.get("model")) or "unknown",
            "severity": _clean(r.get("severity")).lower(),
            "lat": lat,
            "lon": lon,
            "riverId": _clean(r.get("riverId") or r.get("river_id")),
            "country": _clean(r.get("country")),
            "issuedTime": _clean(r.get("issuedTime") or r.get("issued_time")),
            "startTime": _clean(r.get("startTime") or r.get("start_time")),
            "peakTime": _clean(r.get("peakTime") or r.get("peak_time")),
            "endTime": _clean(r.get("endTime") or r.get("end_time")),
            "returnPeriodYr": _clean(r.get("returnPeriodYr") or r.get("return_period_yr")),
            "peakDischargeCms": _clean(r.get("peakDischargeCms") or r.get("peak_discharge_cms")),
            "historicalComparison": _clean(r.get("historicalComparison")
                                            or r.get("historical_comparison")),
        })
    return out


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        if reader.fieldnames is None:
            raise ValueError(f"{os.path.basename(path)} is empty (no header row).")
        if _is_flood_hub(reader.fieldnames):
            return adapt_flood_hub(list(reader))
        if _is_geoglows(reader.fieldnames):
            return adapt_geoglows(list(reader))
        if _is_canonical(reader.fieldnames):
            return adapt_canonical(list(reader))
        raise ValueError(
            f"{os.path.basename(path)}: unrecognized format. Need a Flood Hub export "
            f"(with {sorted(FLOOD_HUB_SIGNATURE)}), a GEOGLOWS export (with "
            f"{sorted(GEOGLOWS_SIGNATURE)}), or a canonical CSV (with "
            f"{sorted(CANONICAL_SIGNATURE)}).\nFound: {', '.join(reader.fieldnames)}"
        )


def main(argv=None):
    p = argparse.ArgumentParser(description="Flood CSV -> per-point JSON (keeps lat/lon).")
    p.add_argument("input_csv", nargs="*", help="One or more CSVs (merged). Omit to use CONFIG.")
    p.add_argument("-o", "--output", default=None, help="Output JSON. Omit to use CONFIG.")
    args = p.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    inputs = args.input_csv or [os.path.join(here, c) for c in INPUT_CSVS]
    output = args.output or os.path.join(here, OUTPUT)

    print(f"Reading {len(inputs)} file(s):")
    points = []
    for path in inputs:
        try:
            rows = read_csv(path)
        except (ValueError, FileNotFoundError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        print(f"  {os.path.basename(path)}: {len(rows)} point(s)")
        points.extend(rows)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(points, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(points)} point(s) -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
