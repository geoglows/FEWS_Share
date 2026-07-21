#!/usr/bin/env python3
"""
build_basins.py — flag HydroBASINS basins that are flooding, telescoping from
level 4 (coarse) down to level 8 (fine).

GEOGLOWS rivers are joined to level-8 basins through the crosswalk; Flood Hub
gauges are placed by point-in-polygon. Level-8 results are then rolled up to
levels 7..4 using the PFAF_ID hierarchy (a level-N basin's code is the first N
digits of its level-8 descendants').

Reads:  ../Files/global_matches.csv      Basin_ID -> Best_Match (GEOGLOWS comid)
        ../Files/Geoglows_*.csv          comid, ret_per, mean
        ../Files/Flood_Hub_Global.csv    gauge severity + lat/lon
        ../Files/Basins/HUC0{4..8}.parquet   basin polygons (HYBAS_ID, PFAF_ID)
Writes: ../data.geojson                  one FeatureCollection, features tagged
                                         with `res` (the basin level); upload this
                                         to CloudFront for the app to consume
"""

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))   # <repo>/scripts
ROOT = os.path.dirname(HERE)                        # <repo>
FILES = os.path.join(ROOT, "Files")

MATCHES_CSV = os.path.join(FILES, "global_matches.csv")
GEOGLOWS_CSV = os.path.join(FILES, "Geoglows_2026-07-13-00.csv")
FLOOD_HUB_CSV = os.path.join(FILES, "Flood_Hub_Global.csv")
BASINS_DIR = os.path.join(FILES, "Basins")
IMPACT_DIR = os.path.join(FILES, "Impact")
HUC12_PARQUET = os.path.join(BASINS_DIR, "HUC12.parquet")
OUTPUT = os.path.join(ROOT, "data_basins.geojson")

# Impact stats are measured per HUC12 basin; each file is HYBAS_ID + value
# column(s), with a TOTAL row we skip. (file, {csv column: impact field})
IMPACT_FILES = [
    ("population_statistics.csv", {"pop_value": "population"}),
    ("building_statistics.csv", {"building_count": "buildings"}),
    ("farmland_statistics.csv", {"area_m2": "farmland_m2"}),
    ("transportation_statistics.csv", {"highway_km": "highway_km",
                                       "railway_km": "railway_km"}),
]
IMPACT_FIELDS = ["population", "buildings", "farmland_m2", "highway_km", "railway_km"]

LEVELS = [8]
BASE_LEVEL = 8

GEOGLOWS_SEVERITY_THRESHOLDS = [(20, "extreme"), (5, "danger"), (2, "warning")]
FLOOD_HUB_SEVERITY = {
    "ABOVE_NORMAL": "warning",
    "SEVERE": "danger",
    "EXTREME": "extreme",
}
SEVERITY_RANK = {
    "none": 0, "warning": 1, "danger": 2, "extreme": 3,
}


def worst_severity(forecasts):
    best, best_rank = "", -1
    for fc in forecasts:
        r = SEVERITY_RANK.get(str(fc.get("severity", "")).lower(), -1)
        if r > best_rank:
            best_rank, best = r, str(fc.get("severity", "")).lower()
    return best


def geoglows_severity(ret_per):
    for thr, sev in GEOGLOWS_SEVERITY_THRESHOLDS:
        if ret_per >= thr:
            return sev
    return None


def load_matches(path):
    basin_to_comid, holes = {}, 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            bid = (row.get("Basin_ID") or "").strip()
            match = (row.get("Best_Match") or "").strip()
            if not bid:
                continue
            if not match:
                holes += 1
                continue
            try:
                basin_to_comid[int(bid)] = int(float(match))
            except ValueError:
                holes += 1
    return basin_to_comid, holes


def load_geoglows(path, wanted_comids):
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.reader(f)
        header = next(r, None)
        if not header:
            sys.exit(f"{os.path.basename(path)} is empty.")
        cols = {c.strip(): i for i, c in enumerate(header)}
        for key in ("comid", "ret_per", "mean"):
            if key not in cols:
                sys.exit(f"{os.path.basename(path)} has no '{key}' column.")
        i_id, i_rp, i_mean = cols["comid"], cols["ret_per"], cols["mean"]
        for row in r:
            try:
                cid = int(row[i_id])
            except (ValueError, IndexError):
                continue
            if cid not in wanted_comids:
                continue
            try:
                rp = int(float(row[i_rp]))
            except (ValueError, IndexError):
                continue
            sev = geoglows_severity(rp)
            if sev is None:
                continue
            try:
                mean = float(row[i_mean])
            except (ValueError, IndexError):
                mean = 0.0
            out[cid] = (sev, rp, mean)
    return out


def load_flood_hub(path):
    """Flood Hub alerts with a coordinate -> [(lon, lat, forecast), ...]."""
    if not os.path.exists(path):
        print(f"  note: {os.path.basename(path)} not found; basins will be "
              f"GEOGLOWS-only.", file=sys.stderr)
        return []
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            raw = (r.get("severity") or "").strip().upper()
            if raw not in FLOOD_HUB_SEVERITY:
                continue
            try:
                lat = float(r.get("gaugeLocation.latitude"))
                lon = float(r.get("gaugeLocation.longitude"))
            except (TypeError, ValueError):
                continue
            out.append((lon, lat, {
                "model": "flood_hub",
                "severity": FLOOD_HUB_SEVERITY[raw],
                "riverId": (r.get("gaugeId") or "").strip(),
                "country": (r.get("queriedCountryName") or "").strip(),
                "issuedTime": (r.get("issuedTime") or "").strip(),
                "startTime": (r.get("forecastTimeRange.start") or "").strip(),
                "peakTime": "",
                "endTime": (r.get("forecastTimeRange.end") or "").strip(),
                "returnPeriodYr": "",
                "peakDischargeCms": "",
                "historicalComparison": "",
            }))
    return out


def scan_base_level(geoglows_basins, gauges):
    """One pass over HUC08: collect geometry+PFAF for GEOGLOWS-flooded basins and
    assign each Flood Hub gauge to the basin that contains it."""
    import pyarrow.parquet as pq
    from shapely import from_wkb, STRtree, points as shapely_points

    path = os.path.join(BASINS_DIR, "HUC08.parquet")
    geom_by_basin, pfaf_by_basin = {}, {}
    gauge_basin = {}

    pts = shapely_points([[g[0], g[1]] for g in gauges]) if gauges else None

    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=20_000,
                                 columns=["HYBAS_ID", "PFAF_ID", "geometry"]):
        d = batch.to_pydict()
        geoms = from_wkb([bytes(g) for g in d["geometry"]])
        hybs, pfafs = d["HYBAS_ID"], d["PFAF_ID"]

        for i, h in enumerate(hybs):
            if h in geoglows_basins and h not in geom_by_basin:
                geom_by_basin[h] = geoms[i]
                pfaf_by_basin[h] = str(pfafs[i])

        if pts is not None and len(gauge_basin) < len(gauges):
            tree = STRtree(geoms)
            pi, gi = tree.query(pts, predicate="intersects")
            for p_idx, g_idx in zip(pi, gi):
                if p_idx in gauge_basin:
                    continue
                if geoms[g_idx].contains(pts[p_idx]):
                    h = hybs[g_idx]
                    gauge_basin[p_idx] = h
                    geom_by_basin.setdefault(h, geoms[g_idx])
                    pfaf_by_basin.setdefault(h, str(pfafs[g_idx]))
    return geom_by_basin, pfaf_by_basin, gauge_basin


def load_level_geometry(level, wanted_pfafs):
    """PFAF_ID -> (HYBAS_ID, geometry) for one coarser level."""
    import pyarrow.parquet as pq
    from shapely import from_wkb

    path = os.path.join(BASINS_DIR, f"HUC{level:02d}.parquet")
    if not os.path.exists(path):
        sys.exit(f"Not found: {path}")
    out = {}
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=20_000,
                                 columns=["HYBAS_ID", "PFAF_ID", "geometry"]):
        d = batch.to_pydict()
        hybs, pfafs = d["HYBAS_ID"], d["PFAF_ID"]
        need = [i for i, p in enumerate(pfafs) if str(p) in wanted_pfafs]
        if not need:
            continue
        geoms = from_wkb([bytes(d["geometry"][i]) for i in need])
        for k, i in enumerate(need):
            out.setdefault(str(pfafs[i]), (hybs[i], geoms[k]))
        if len(out) == len(wanted_pfafs):
            break
    return out


def load_impacts(wanted_by_level):
    """Sum the HUC12 impact stats into each flagged basin.

    wanted_by_level: {level: set(PFAF prefixes of flagged basins at that level)}
    Returns {level: {pfaf_prefix: {population, buildings, farmland_m2, ...}}}.
    A HUC12 basin belongs to the level-N basin whose PFAF code is its first N
    digits, so every HUC12 inside a flagged basin is counted — the whole basin,
    not just the matched river.
    """
    import pyarrow.parquet as pq

    if not os.path.isdir(IMPACT_DIR):
        print(f"  note: {IMPACT_DIR} not found; skipping impact.", file=sys.stderr)
        return {}

    hyb12_pfaf = {}
    if os.path.exists(HUC12_PARQUET):
        pf = pq.ParquetFile(HUC12_PARQUET)
        for batch in pf.iter_batches(batch_size=100_000,
                                     columns=["HYBAS_ID", "PFAF_ID"]):
            d = batch.to_pydict()
            for h, p in zip(d["HYBAS_ID"], d["PFAF_ID"]):
                hyb12_pfaf[h] = str(p)
    else:
        print(f"  note: {HUC12_PARQUET} not found; skipping impact.", file=sys.stderr)
        return {}
    print(f"Impact: {len(hyb12_pfaf):,} HUC12 basins in the lookup.")

    totals = {lv: {} for lv in wanted_by_level}
    matched_rows = 0

    for fname, colmap in IMPACT_FILES:
        path = os.path.join(IMPACT_DIR, fname)
        if not os.path.exists(path):
            print(f"  note: missing {fname}; skipped.", file=sys.stderr)
            continue
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                raw = (row.get("HYBAS_ID") or "").strip()
                if not raw or raw.upper() == "TOTAL":
                    continue
                try:
                    hyb = int(raw)
                except ValueError:
                    continue
                pfaf = hyb12_pfaf.get(hyb)
                if not pfaf:
                    continue
                vals = {}
                for col, field in colmap.items():
                    try:
                        vals[field] = float(row.get(col) or 0)
                    except ValueError:
                        vals[field] = 0.0
                hit = False
                for lv, wanted in wanted_by_level.items():
                    key = pfaf[:lv]
                    if key not in wanted:
                        continue
                    hit = True
                    bucket = totals[lv].setdefault(
                        key, {k: 0.0 for k in IMPACT_FIELDS})
                    for field, v in vals.items():
                        bucket[field] += v
                if hit:
                    matched_rows += 1
    print(f"Impact: {matched_rows:,} HUC12 rows fell inside a flagged basin.")
    return totals


def main():
    from shapely.geometry import mapping

    for p in (MATCHES_CSV, GEOGLOWS_CSV, BASINS_DIR):
        if not os.path.exists(p):
            sys.exit(f"Not found: {p}")

    basin_to_comid, holes = load_matches(MATCHES_CSV)
    print(f"Crosswalk: {len(basin_to_comid):,} basins matched ({holes:,} holes).")

    flooding = load_geoglows(GEOGLOWS_CSV, set(basin_to_comid.values()))
    print(f"Matched rivers flooding: {len(flooding):,}")

    # level-8 basin -> list of forecasts
    base = {}
    for bid, cid in basin_to_comid.items():
        if cid not in flooding:
            continue
        sev, rp, mean = flooding[cid]
        base.setdefault(bid, []).append({
            "model": "geoglows", "severity": sev, "riverId": str(cid),
            "country": "", "issuedTime": "", "startTime": "", "peakTime": "",
            "endTime": "", "returnPeriodYr": rp, "peakDischargeCms": mean,
            "historicalComparison": "",
        })
    print(f"Basins flooding (GEOGLOWS): {len(base):,}")

    gauges = load_flood_hub(FLOOD_HUB_CSV)
    print(f"Flood Hub alerts with coordinates: {len(gauges):,}")

    geom_by_basin, pfaf_by_basin, gauge_basin = scan_base_level(set(base), gauges)
    for p_idx, hyb in gauge_basin.items():
        base.setdefault(hyb, []).append(gauges[p_idx][2])
    print(f"  Flood Hub gauges placed in a basin: {len(gauge_basin):,}"
          f" ({len(gauges) - len(gauge_basin):,} fell outside)")
    print(f"Basins flooding (both models): {len(base):,}")

    # PFAF-keyed view of the flagged base basins, then the coarser roll-ups.
    base_by_pfaf = {}
    for bid, fcs in base.items():
        pfaf = pfaf_by_basin.get(bid)
        if pfaf:
            base_by_pfaf[pfaf] = (bid, fcs)

    coarser = sorted([l for l in LEVELS if l != BASE_LEVEL], reverse=True)
    by_level = {BASE_LEVEL: {p: fcs for p, (_, fcs) in base_by_pfaf.items()}}
    for level in coarser:
        grouped = {}
        for pfaf, (_, fcs) in base_by_pfaf.items():
            grouped.setdefault(pfaf[:level], []).extend(fcs)
        by_level[level] = grouped

    impacts = load_impacts({lv: set(g) for lv, g in by_level.items()})

    def tidy(d):
        return {
            "population": round(d["population"]),
            "buildings": round(d["buildings"]),
            "farmland_m2": round(d["farmland_m2"]),
            "highway_km": round(d["highway_km"], 1),
            "railway_km": round(d["railway_km"], 1),
        }

    features = []
    counts = {}

    # --- base level -----------------------------------------------------------
    n = 0
    for pfaf, (bid, fcs) in base_by_pfaf.items():
        g = geom_by_basin.get(bid)
        if g is None:
            continue
        props = {
            "res": BASE_LEVEL, "cell_id": str(bid), "basin_id": str(bid),
            "severity": worst_severity(fcs), "model_count": len(fcs),
            "forecasts": fcs,
        }
        imp = impacts.get(BASE_LEVEL, {}).get(pfaf)
        if imp:
            props["impact"] = tidy(imp)
        features.append({"type": "Feature", "geometry": mapping(g),
                         "properties": props})
        n += 1
    counts[BASE_LEVEL] = n

    # --- coarser levels (rolled up by PFAF prefix) ----------------------------
    for level in coarser:
        grouped = by_level[level]
        geoms = load_level_geometry(level, set(grouped))
        n = 0
        for pfaf, fcs in grouped.items():
            hit = geoms.get(pfaf)
            if hit is None:
                continue
            hyb, g = hit
            props = {
                "res": level, "cell_id": str(hyb), "basin_id": str(hyb),
                "severity": worst_severity(fcs), "model_count": len(fcs),
                "forecasts": fcs,
            }
            imp = impacts.get(level, {}).get(pfaf)
            if imp:
                props["impact"] = tidy(imp)
            features.append({"type": "Feature", "geometry": mapping(g),
                             "properties": props})
            n += 1
        counts[level] = n

    with_impact = sum(1 for f in features if "impact" in f["properties"])
    for lv in LEVELS:
        print(f"  level {lv}: {counts.get(lv, 0):,} basin(s)")
    print(f"Basins with impact totals: {with_impact:,}/{len(features):,}")

    fc = {
        "type": "FeatureCollection",
        "kind": "basins-telescoping",
        "resolutions": LEVELS,
        "features": features,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    print(f"Wrote {len(features):,} feature(s) across {len(LEVELS)} levels -> {OUTPUT}")


if __name__ == "__main__":
    main()
