#!/usr/bin/env python3
import argparse
import csv
import glob
import json
import os
import re
import sys
from datetime import datetime
from typing import Optional, Set, Dict, Any, List

TRUE_SET = {"1", "true", "yes", "y", "t"}

# -----------------------------
# Step 2: analyze per-seed JSONs (same data as old analyze_per_seed.py)
# -----------------------------

def _sort_key(path: str) -> int:
    bn = os.path.basename(path)
    m = re.match(r"(\d+)-", bn)
    return int(m.group(1)) if m else 10**9

def analyze_per_seed_dir(indir: str) -> List[Dict[str, Any]]:
    # We want files named like NNN-seedname.branches.json in order by NNN
    paths = sorted(glob.glob(os.path.join(indir, "*/*.branches.json")))
    # Also handle possible flat layout
    paths += sorted(glob.glob(os.path.join(indir, "*.branches.json")))
    paths = sorted(set(paths), key=_sort_key)

    seen = set()  # union of covered branches so far
    rows: List[Dict[str, Any]] = []

    for p in paths:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)

        seed = obj.get("seed")
        mtime_unix = obj.get("mtime_unix")
        mtime_iso = obj.get("mtime_iso")
        exit_code = obj.get("exit_code", 0)
        crashed = obj.get("crashed", 0)
        branches = obj.get("covered_branches", [])

        # Normalize branch IDs to tuples
        bset = set()
        for trip in branches:
            if isinstance(trip, list) and len(trip) == 3:
                fpath, line, bi = trip
                try:
                    bset.add((fpath, int(line), int(bi)))
                except Exception:
                    pass

        new_hits = bset - seen
        seen |= bset

        # index from filename
        bn = os.path.basename(p)
        m = re.match(r"(\d+)-", bn)
        idx = int(m.group(1)) if m else (len(rows) + 1)

        rows.append({
            "index": idx,
            "seed": seed,
            "mtime_unix": mtime_unix,
            "mtime_iso": mtime_iso,
            "exit_code": exit_code,
            "crashed": crashed,
            "covered_branches_this_seed": len(bset),
            "new_branches_this_seed": len(new_hits),
            "cumulative_covered_branches": len(seen),
        })

    return rows

# -----------------------------
# Step 3: normalize coverage over time (logic from get_cov_over_time.py)
# -----------------------------

def parse_iso_to_epoch(s: str) -> Optional[int]:
    try:
        s = s.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None

def load_initial_seeds_list(path: Optional[str]) -> Set[str]:
    if not path:
        return set()
    out: Set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name:
                out.add(name)
    return out

def normalize_cov_over_time(
    rows_in: List[Dict[str, Any]],
    initial_seeds_list: Optional[str],
    stitch_gaps_over: int,
    stitch_to: int,
    duration: int,
    fuzz_start_epoch: Optional[int],
    fuzz_start_iso: Optional[str],
) -> (List[Dict[str, Any]], int):
    # Coerce minimal fields used by normalization; keep other columns too.
    rows: List[Dict[str, Any]] = []
    for r in rows_in:
        try:
            mtime = int(float(r.get("mtime_unix", "")))
        except Exception:
            continue

        seed = (r.get("seed") or "").strip()
        idx = int(r.get("index", 0) or 0)
        try:
            newb = int(r.get("new_branches_this_seed", 0) or 0)
        except Exception:
            newb = 0

        rows.append({
            "index": idx,
            "seed": seed,
            "mtime_unix": mtime,
            "mtime_iso": r.get("mtime_iso", ""),
            "exit_code": r.get("exit_code", ""),
            "crashed": r.get("crashed", ""),
            "covered_branches_this_seed": r.get("covered_branches_this_seed", ""),
            "cumulative_covered_branches": r.get("cumulative_covered_branches", ""),
            "new_branches_this_seed": max(0, newb),
        })

    rows.sort(key=lambda x: (x["mtime_unix"], x["index"]))
    if not rows:
        raise SystemExit("No usable rows found after analyzing the directory.")

    initial_names = load_initial_seeds_list(initial_seeds_list)
    for r in rows:
        r["is_initial"] = 1 if r["seed"] in initial_names else 0

    # Determine fuzz start (t=0)
    if fuzz_start_epoch is not None:
        fuzz_start = int(fuzz_start_epoch)
    elif fuzz_start_iso:
        epoch = parse_iso_to_epoch(fuzz_start_iso)
        if epoch is None:
            raise SystemExit(f"Could not parse --fuzz-start: {fuzz_start_iso}")
        fuzz_start = epoch
    else:
        non_initial_times = [r["mtime_unix"] for r in rows if r["is_initial"] == 0]
        if not non_initial_times:
            raise SystemExit("All rows are initial seeds; cannot determine fuzz start.")
        fuzz_start = min(non_initial_times)

    # Raw time since fuzz start (initial seeds forced to 0)
    for r in rows:
        r["t_seconds_raw"] = 0 if r["is_initial"] else max(0, r["mtime_unix"] - fuzz_start)

    # Stitch gaps
    stitched_clock = 0
    last_raw = None

    for r in rows:
        if r["is_initial"]:
            r["t_seconds"] = 0
            continue

        tr = int(r["t_seconds_raw"])
        if last_raw is None:
            # FIRST non-initial seed also gets t=0 (matches get_cov_over_time.py behavior)
            r["t_seconds"] = 0
            last_raw = tr
            continue

        gap = tr - last_raw
        stitched_clock += stitch_to if gap > stitch_gaps_over else max(0, gap)
        r["t_seconds"] = stitched_clock
        last_raw = tr

    # Clamp duration window
    rows = [r for r in rows if 0 <= r["t_seconds"] <= duration]

    for r in rows:
        r["t_hours"] = f"{r['t_seconds'] / 3600:.6f}"

    return rows, fuzz_start

def write_cov_over_time_csv(rows: List[Dict[str, Any]], out_csv: str) -> None:
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    fieldnames = [
        "index","seed","mtime_unix","mtime_iso",
        "is_initial","t_seconds_raw","t_seconds","t_hours",
        "new_branches_this_seed",
        "covered_branches_this_seed",
        "cumulative_covered_branches",
        "exit_code","crashed"
    ]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

# -----------------------------
# Main
# -----------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="analyze_per_seed.py",
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "Analyze per-seed *.branches.json files AND emit a normalized coverage-over-time CSV.\n\n"
            "Output includes the original per-seed coverage columns plus:\n"
            "  is_initial, t_seconds_raw, t_seconds, t_hours\n"
        ),
    )
    ap.add_argument("--in", dest="indir", required=True,
                    help="Directory produced by run_per_seed_coverage.sh")

    ap.add_argument("--out", default=None,
                    help="Output CSV (default: <in>/cov_over_time_per_seed.csv)")
    ap.add_argument("--initial-seeds-list",
                    help="File listing initial seed names.")

    ap.add_argument("--stitch-gaps-over", type=int, default=600,
                    help="Collapse gaps larger than this many seconds (default: 600).")
    ap.add_argument("--stitch-to", type=int, default=0,
                    help="Seconds to replace a stitched gap with (default: 0).")
    ap.add_argument("--duration", type=int, default=86400,
                    help="Maximum normalized time in seconds (default: 86400).")
    ap.add_argument("--fuzz-start-epoch", type=int,
                    help="Explicit epoch start (overrides auto-detection).")
    ap.add_argument("--fuzz-start",
                    help="Explicit ISO start time (overrides auto-detection).")

    if len(sys.argv) == 1:
        ap.print_help()
        sys.exit(1)

    args = ap.parse_args()

    out_csv = args.out or os.path.join(args.indir, "cov_over_time_per_seed.csv")

    rows_step2 = analyze_per_seed_dir(args.indir)
    rows_norm, fuzz_start = normalize_cov_over_time(
        rows_in=rows_step2,
        initial_seeds_list=args.initial_seeds_list,
        stitch_gaps_over=args.stitch_gaps_over,
        stitch_to=args.stitch_to,
        duration=args.duration,
        fuzz_start_epoch=args.fuzz_start_epoch,
        fuzz_start_iso=args.fuzz_start,
    )
    write_cov_over_time_csv(rows_norm, out_csv)

    print("Wrote:", out_csv)
    print("Fuzz start epoch (t=0):", fuzz_start)

if __name__ == "__main__":
    main()
