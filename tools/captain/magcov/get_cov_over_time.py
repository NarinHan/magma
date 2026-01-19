#!/usr/bin/env python3
import argparse
import csv
import sys
from datetime import datetime
from typing import Optional, Set

TRUE_SET = {"1", "true", "yes", "y", "t"}

# -----------------------------
# Utilities
# -----------------------------

def parse_iso_to_epoch(s: str) -> Optional[int]:
    try:
        s = s.replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None

def load_initial_seeds_file(path: Optional[str]) -> Set[str]:
    if not path:
        return set()
    out: Set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            name = line.strip()
            if name:
                out.add(name)
    return out

def sniff_kind(fieldnames):
    f = set(fn.strip() for fn in fieldnames or [])
    if {"mtime_unix", "seed", "new_branches_this_seed"}.issubset(f):
        return "per-seed"
    if {"mtime_unix", "seed", "delta_since_prev", "measured"}.issubset(f):
        return "accum"
    raise SystemExit(
        "Unrecognized CSV schema.\n"
        "Expected either:\n"
        "  per-seed: mtime_unix, seed, new_branches_this_seed\n"
        "  accum:    mtime_unix, seed, delta_since_prev, measured"
    )

def load_rows(csv_path: str):
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        kind = sniff_kind(r.fieldnames)

        for row in r:
            try:
                mtime = int(float(row.get("mtime_unix", "")))
            except Exception:
                continue

            seed = (row.get("seed") or "").strip()
            idx = int(row.get("index", "0") or 0)

            if kind == "per-seed":
                try:
                    newb = int(row.get("new_branches_this_seed", "0") or 0)
                except Exception:
                    newb = 0
            else:
                measured = str(row.get("measured", "0")).strip().lower()
                if measured not in TRUE_SET:
                    continue
                try:
                    newb = int(row.get("delta_since_prev", "0") or 0)
                except Exception:
                    newb = 0

            rows.append({
                "index": idx,
                "seed": seed,
                "mtime_unix": mtime,
                "mtime_iso": row.get("mtime_iso", ""),
                "exit_code": row.get("exit_code", ""),
                "crashed": row.get("crashed", ""),
                "covered_branches_this_seed": row.get("covered_branches_this_seed", ""),
                "cumulative_covered_branches": row.get("cumulative_covered_branches", ""),
                "new_branches_this_seed": max(0, newb),
            })

    rows.sort(key=lambda r: (r["mtime_unix"], r["index"]))
    if not rows:
        raise SystemExit("No usable rows found.")
    return rows

# -----------------------------
# Main
# -----------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="get_cov_over_time.py",
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "Normalize fuzzing coverage data into a logical 0–24 hour timeline.\n\n"
            "Core behavior:\n"
            "  - Initial seeds are assigned time = 0\n"
            "  - The FIRST non-initial (generated) seed is also assigned time = 0\n"
            "  - Large idle gaps between seeds are stitched (ignored)\n"
            "  - Time is normalized and clamped to a 24-hour window\n\n"
            "The output is a per-seed CSV suitable for auditing."
        ),
        epilog=(
            "Typical usage:\n"
            "  python get_cov_over_time.py \\\n"
            "    --csv per_seed_coverage.csv \\\n"
            "    --initial-seeds-file initial_seeds.txt \\\n"
            "    --out coverage.normalized.csv\n"
        )
    )

    ap.add_argument("--csv", required=True, help="Input coverage CSV file.")
    ap.add_argument("--out", required=True, help="Output normalized CSV file.")
    ap.add_argument("--initial-seeds-file", help="File listing initial seed names.")
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

    rows = load_rows(args.csv)
    initial_names = load_initial_seeds_file(args.initial_seeds_file)

    for r in rows:
        r["is_initial"] = 1 if r["seed"] in initial_names else 0

    if args.fuzz_start_epoch is not None:
        fuzz_start = int(args.fuzz_start_epoch)
    elif args.fuzz_start:
        epoch = parse_iso_to_epoch(args.fuzz_start)
        if epoch is None:
            raise SystemExit(f"Could not parse --fuzz-start: {args.fuzz_start}")
        fuzz_start = epoch
    else:
        non_initial_times = [r["mtime_unix"] for r in rows if r["is_initial"] == 0]
        if not non_initial_times:
            raise SystemExit("All rows are initial seeds; cannot determine fuzz start.")
        fuzz_start = min(non_initial_times)

    for r in rows:
        r["t_seconds_raw"] = 0 if r["is_initial"] else max(0, r["mtime_unix"] - fuzz_start)

    stitched_clock = 0
    last_raw = None

    for r in rows:
        if r["is_initial"]:
            r["t_seconds"] = 0
            continue

        tr = int(r["t_seconds_raw"])
        if last_raw is None:
            r["t_seconds"] = 0
            last_raw = tr
            continue

        gap = tr - last_raw
        stitched_clock += args.stitch_to if gap > args.stitch_gaps_over else max(0, gap)
        r["t_seconds"] = stitched_clock
        last_raw = tr

    rows = [r for r in rows if 0 <= r["t_seconds"] <= args.duration]

    for r in rows:
        r["t_hours"] = f"{r['t_seconds'] / 3600:.6f}"

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "index","seed","mtime_unix","mtime_iso",
            "is_initial","t_seconds_raw","t_seconds","t_hours",
            "new_branches_this_seed",
            "covered_branches_this_seed",
            "cumulative_covered_branches",
            "exit_code","crashed"
        ]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

    print("Wrote:", args.out)
    print("Fuzz start epoch (t=0):", fuzz_start)

if __name__ == "__main__":
    main()

