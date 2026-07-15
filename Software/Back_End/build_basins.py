#!/usr/bin/env python3
"""
build_basins.py — join flood forecasts to HydroBASINS polygons for the map UI.

Reads:  ../flood_state.json   forecast list (one entry per model-per-basin)
        ../HUC12.parquet      HydroBASINS level-12 polygons (key: HYBAS_ID)
Writes: data.geojson          same format the grid builds write, so app.js renders
                              basins unchanged: one FeatureCollection, features
                              tagged with `res` (= BASIN_LEVEL), plus a
                              `resolutions` member. Run this instead of a
                              build_cells_*.py for the basin view; last run wins.

Non-numeric basin ids (Flood Hub gauge ids like BWDB_/CWC_/ANA_) have no polygon
and are skipped. app.js fetches data.geojson, so serve the page via Go Live.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))            # Software/Back_End
DATA_ROOT = os.path.dirname(os.path.dirname(HERE))           # FEWS_Share (data files)
FRONT_END = os.path.join(os.path.dirname(HERE), "Front_End")  # Software/Front_End
FLOOD_JSON = os.path.join(DATA_ROOT, "flood_state.json")
PARQUET = os.path.join(DATA_ROOT, "HUC12.parquet")
OUTPUT = os.path.join(FRONT_END, "data.geojson")
BASIN_LEVEL = 12

SEVERITY_RANK = {
    "none": 0, "minor": 1, "moderate": 2, "major": 3, "severe": 4, "extreme": 5,
}


def as_hybas_id(basin_id):
    try:
        return int(str(basin_id).strip())
    except (TypeError, ValueError):
        return None


def load_forecasts(path):
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    grouped = {}
    for e in entries:
        bid = str(e.get("basinId", "")).strip()
        if bid:
            grouped.setdefault(bid, []).append(e)
    return grouped


def worst_severity(forecasts):
    best = ""
    best_rank = -1
    for fc in forecasts:
        r = SEVERITY_RANK.get(str(fc.get("severity", "")).lower(), -1)
        if r > best_rank:
            best_rank, best = r, str(fc.get("severity", "")).lower()
    return best


def load_geometry(path, wanted_ids):
    try:
        import pyarrow.parquet as pq
        from shapely import wkb
        from shapely.geometry import mapping
    except ImportError as e:
        sys.exit(
            f"Missing dependency: {e.name}. Install with:\n"
            f"    pip install pyarrow shapely"
        )

    want = {hid for hid in (as_hybas_id(x) for x in wanted_ids) if hid is not None}
    out = {}
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=200_000, columns=["HYBAS_ID", "geometry"]):
        d = batch.to_pydict()
        for hid, geom in zip(d["HYBAS_ID"], d["geometry"]):
            if hid in want and hid not in out:
                out[hid] = mapping(wkb.loads(bytes(geom)))
        if len(out) == len(want):
            break
    return out


def main():
    if not os.path.exists(FLOOD_JSON):
        sys.exit(f"Not found: {FLOOD_JSON}")
    if not os.path.exists(PARQUET):
        sys.exit(f"Not found: {PARQUET}")

    forecasts = load_forecasts(FLOOD_JSON)
    print(f"Forecasts cover {len(forecasts)} basin(s).")

    geom = load_geometry(PARQUET, forecasts.keys())

    non_hybas = [b for b in forecasts if as_hybas_id(b) is None]
    missing = [b for b in forecasts
               if as_hybas_id(b) is not None and as_hybas_id(b) not in geom]
    if non_hybas:
        print(f"  note: {len(non_hybas)} gauge-station basin(s) have no polygon "
              f"(skipped): {', '.join(non_hybas)}", file=sys.stderr)
    if missing:
        print(f"  warning: no geometry in parquet for: {', '.join(missing)}",
              file=sys.stderr)

    features = []
    for bid, fcs in forecasts.items():
        g = geom.get(as_hybas_id(bid))
        if g is None:
            continue
        features.append({
            "type": "Feature",
            "geometry": g,
            "properties": {
                "res": BASIN_LEVEL,
                "cell_id": bid,
                "basin_id": bid,
                "severity": worst_severity(fcs),
                "model_count": len(fcs),
                "forecasts": fcs,
            },
        })

    fc = {
        "type": "FeatureCollection",
        "kind": "basins",
        "resolutions": [BASIN_LEVEL],
        "features": features,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)
    print(f"Wrote {len(features)} basin feature(s) -> {OUTPUT}")


if __name__ == "__main__":
    main()
