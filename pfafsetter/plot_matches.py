"""
Turn basin -> nexus match results (results_au_8.csv) into a GeoPackage of only
the successfully matched basins and the nexus outlet chosen for each one, for
manual inspection in QGIS.

Output: basin_match_review_au_8.gpkg - two layers, same CRS:
    - "matched_basins"       only basins with a Best_Match, polygon +
                              Best_Match/Match_Percentage.
    - "matched_nexus_points" the nexus point each matched basin resolved to.

Note on the join: nexus points don't carry their own LINKNO. Each nexus point
has a DSLINKNO and a comma-separated USLINKNOs list (the river segments that
flow INTO that junction). `Best_Match` in the results CSV is one of those
upstream LINKNOs, so to plot it we explode every nexus point's USLINKNOs list
into (LINKNO -> nexus geometry) rows and join on that.
"""

import geopandas as gpd
import pandas as pd

# ---- Adjust these to match your local paths (same convention as main.py) ----
HYDROBASINS_PATH = "/Users/maugh24/FEWS_Share/hybas_au_lev01-12_v1c/hybas_au_lev08_v1c.shp"
NEXUS_PATH = "/Users/maugh24/FEWS_Share/pfafsetter/global_nexus.gpkg"
RESULTS_CSV = "/Users/maugh24/FEWS_Share/pfafsetter/results_au_8_0.25.csv"
OUT_GPKG = "/Users/maugh24/FEWS_Share/pfafsetter/basin_match_review_au_8_0.25.gpkg"


def load_data(hydrobasins_path=HYDROBASINS_PATH, nexus_path=NEXUS_PATH, results_csv=RESULTS_CSV):
    results = pd.read_csv(results_csv)
    hydrobasins = gpd.read_file(hydrobasins_path)
    nexus_points = gpd.read_file(nexus_path).to_crs(hydrobasins.crs)
    return results, hydrobasins, nexus_points


def build_link_to_nexus_lookup(nexus_points):
    """Explode USLINKNOs so every upstream LINKNO maps to the nexus point
    geometry it flows into. A given LINKNO should only feed one nexus point;
    if it somehow appears more than once, just keep the first."""
    exploded = nexus_points.assign(
        LINKNO=nexus_points["USLINKNOs"].str.split(",")
    ).explode("LINKNO")
    exploded["LINKNO"] = exploded["LINKNO"].astype("int64")
    return (
        exploded.drop_duplicates("LINKNO", keep="first")[["LINKNO", "geometry"]]
        .rename(columns={"geometry": "nexus_geometry"})
    )


def join_results(results, hydrobasins, nexus_points):
    """Returns:
        matched_basins - only basins that got a match: polygon +
                          Best_Match/Match_Percentage
        matched_points - the nexus point each matched basin resolved to
    """
    matched_results = results.dropna(subset=["Best_Match"]).copy()
    matched_results["Best_Match"] = matched_results["Best_Match"].astype("int64")

    matched_basins = hydrobasins.merge(
        matched_results, left_on="HYBAS_ID", right_on="Basin_ID", how="inner"
    )
    matched_basins = matched_basins.drop(columns=["Basin_ID"])  # duplicate of HYBAS_ID

    link_lookup = build_link_to_nexus_lookup(nexus_points)
    matched_points = matched_results.merge(link_lookup, left_on="Best_Match", right_on="LINKNO", how="left")
    matched_points = matched_points.drop(columns=["LINKNO"])
    matched_points = gpd.GeoDataFrame(matched_points, geometry="nexus_geometry", crs=nexus_points.crs)

    return matched_basins, matched_points


def write_gpkg(matched_basins, matched_points, out_path=OUT_GPKG):
    # First layer creates/overwrites the file; the second appends so QGIS
    # sees both layers in the same GeoPackage.
    matched_basins.to_file(out_path, layer="matched_basins", driver="GPKG", mode="w")
    matched_points.to_file(out_path, layer="matched_nexus_points", driver="GPKG", mode="a")
    print(f"Saved {out_path} (layers: matched_basins, matched_nexus_points)")


def main():
    results, hydrobasins, nexus_points = load_data()
    matched_basins, matched_points = join_results(results, hydrobasins, nexus_points)
    write_gpkg(matched_basins, matched_points)


if __name__ == "__main__":
    main()
