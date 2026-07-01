#!/usr/bin/env python3
"""
RFS-FIM_Fetch: CSV -> flood JSON transform (multi-model).

Reads a flood-flag CSV (ONE ROW PER MODEL-PER-BASIN) and emits a flat JSON list:
one entry per model-per-basin, each carrying its own basin_id (HydroBASINS
HYBAS_ID). Entries stay grouped by basin.

Why one row per model: the interface highlights basins where ONE OR MORE models
predict a flood, so the schema keeps each model's forecast separate. A basin
with two models = two CSV rows = two entries in the list.

Frozen output schema (impacts/actions intentionally omitted for now):

[
  # Every model shares the SAME field set (values may be blank where a
  # model lacks the data, e.g. Flood Hub without a gauge -> no discharge).
  {"basinId": "...", "model": "flood_hub", "severity": "...", "riverId": "...",
   "issuedTime": "...", "startTime": "...", "peakTime": "...", "endTime": "...",
   "historicalComparison": "...",
   "returnPeriodYr": <n>, "peakDischargeCms": <f>},
  {"basinId": "...", "model": "geoglows", "severity": "...", "riverId": "...",
   "issuedTime": "...", "startTime": "...", "peakTime": "...", "endTime": "...",
   "historicalComparison": "...",
   "returnPeriodYr": <n>, "peakDischargeCms": <f>}
]

One CSV per model: pass several and they MERGE into one JSON (so a basin can
carry both a Flood Hub and a GEOGLOWS forecast). Pass a single CSV to get that
one model on its own. Just run it (no terminal args needed) — edit CONFIG below.
  python csv_to_json.py                                   # uses CONFIG below
  python csv_to_json.py floodhub.csv geoglows.csv -o out.json
  python csv_to_json.py geoglows.csv -o geoglows.json     # one model only
  python csv_to_json.py floodhub.csv geoglows.csv --split -o ./basins/
"""

import argparse
import csv
import json
import os
import sys

# ---------------------------------------------------------------------------
# CONFIG — edit these so you can just run the file (e.g. press Run in your IDE).
# Paths are relative to this script's location, so it works from anywhere.
# ---------------------------------------------------------------------------
INPUT_CSVS = ["sample_floodhub.csv", "sample_geoglows.csv"]  # <- one per model; merged
OUTPUT = "flood_state.json"            # <- output file, or a folder if SPLIT=True
SPLIT = False                          # False = one combined file; True = one file per basin
# ---------------------------------------------------------------------------

# Shared schema: EVERY model CSV must carry these columns (values may be blank).
# This enforces one consistent interface contract across all models.
REQUIRED_COLUMNS = [
    "basin_id", "model", "river_id",
    "severity", "return_period_yr", "peak_discharge_cms",
    "issued_time", "start_time", "peak_time", "end_time", "historical_comparison",
]

# Recognized severity values (low -> high); used only to flag bad input.
SEVERITY_RANK = {
    "none": 0, "minor": 1, "moderate": 2, "major": 3, "severe": 4, "extreme": 5,
}

# Per-forecast text fields (river_id is handled explicitly).
TEXT_FIELDS = [
    "issued_time", "start_time", "peak_time", "end_time", "historical_comparison",
]
NUM_FIELDS = ["return_period_yr", "peak_discharge_cms"]

# CSV column name (snake_case input) -> camelCase key emitted in the JSON.
OUTPUT_KEYS = {
    "basin_id": "basinId",
    "river_id": "riverId",
    "return_period_yr": "returnPeriodYr",
    "peak_discharge_cms": "peakDischargeCms",
    "issued_time": "issuedTime",
    "start_time": "startTime",
    "peak_time": "peakTime",
    "end_time": "endTime",
    "historical_comparison": "historicalComparison",
}


def _clean(v) -> str:
    return (v or "").strip()


def _num(value: str, field: str, row_num: int):
    value = _clean(value)
    if value == "":
        return None
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except ValueError:
        raise ValueError(f"Row {row_num}: {field} is not a number: {value!r}")


def build_forecast(row: dict, row_num: int) -> dict:
    """One CSV row -> one per-model forecast record (empty fields dropped)."""
    sev = _clean(row.get("severity")).lower()
    if sev and sev not in SEVERITY_RANK:
        print(f"  warning: row {row_num}: unknown severity {sev!r}", file=sys.stderr)

    fc = {
        "basinId": _clean(row.get("basin_id")),
        "model": _clean(row.get("model")),
        "severity": sev,
    }

    river_id = _clean(row.get("river_id"))
    if river_id:
        fc["riverId"] = river_id

    for col in TEXT_FIELDS:
        val = _clean(row.get(col))
        if val:
            fc[OUTPUT_KEYS[col]] = val
    for col in NUM_FIELDS:
        val = _num(row.get(col), col, row_num)
        if val is not None:
            fc[OUTPUT_KEYS[col]] = val
    return fc


def build_basin(rows: list) -> list:
    """All rows for one basin -> a list of model forecasts."""
    return [build_forecast(row, n) for n, row in rows]


def read_csv(path: str) -> list[dict]:
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
        present = {c.strip() for c in reader.fieldnames}
        missing = [c for c in REQUIRED_COLUMNS if c not in present]
        if missing:
            raise ValueError(
                f"{os.path.basename(path)} is missing required column(s): "
                + ", ".join(missing)
                + f"\nFound columns: {', '.join(reader.fieldnames)}"
            )
        return list(reader)


def read_inputs(paths: list[str]) -> list[dict]:
    """Read and concatenate one or more per-model CSVs into one row list."""
    all_rows: list[dict] = []
    for path in paths:
        rows = read_csv(path)
        print(f"  {os.path.basename(path)}: {len(rows)} row(s)")
        all_rows.extend(rows)
    return all_rows


def transform(rows: list[dict]) -> list:
    # Group rows by basin (preserving first-seen order) so a basin's models
    # stay adjacent, then flatten to one list.
    grouped: dict[str, list] = {}
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        basin_id = _clean(row.get("basin_id"))
        if not basin_id:
            raise ValueError(f"Row {i}: basin_id is empty (required).")
        grouped.setdefault(basin_id, []).append((i, row))

    forecasts: list = []
    for r in grouped.values():
        forecasts.extend(build_basin(r))
    return forecasts


def write_combined(doc: list, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(doc)} forecast(s) -> {out_path}")


def write_split(doc: list, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    # One file per basin, each containing that basin's list of forecasts.
    by_basin: dict[str, list] = {}
    for fc in doc:
        by_basin.setdefault(fc["basinId"], []).append(fc)
    for basin_id, entries in by_basin.items():
        with open(os.path.join(out_dir, f"{basin_id}.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
    print(f"Wrote {len(by_basin)} file(s) -> {out_dir}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Flood CSV -> GUI JSON transform (multi-model).")
    p.add_argument("input_csv", nargs="*", default=None,
                   help="One or more per-model CSVs (merged). Omit to use INPUT_CSVS from CONFIG.")
    p.add_argument("-o", "--output", default=None,
                   help="Output file (combined) or directory (with --split). "
                        "Omit to use OUTPUT from CONFIG.")
    p.add_argument("--split", action="store_true",
                   help="Write one JSON file per basin. Defaults to SPLIT from CONFIG.")
    args = p.parse_args(argv)

    here = os.path.dirname(os.path.abspath(__file__))
    if args.input_csv:
        input_csvs = args.input_csv
    else:
        input_csvs = [os.path.join(here, c) for c in INPUT_CSVS]
    output = args.output or os.path.join(here, OUTPUT)
    split = args.split or SPLIT

    print(f"Reading {len(input_csvs)} file(s):")
    try:
        rows = read_inputs(input_csvs)
        doc = transform(rows)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if split:
        write_split(doc, output)
    else:
        write_combined(doc, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
