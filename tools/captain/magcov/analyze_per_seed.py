#!/usr/bin/env python3
import argparse, json, os, re, csv, glob
from datetime import datetime

def atoi(s): 
    try: return int(s)
    except: return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="indir", required=True, help="Directory produced by run_per_seed_coverage.sh")
    args = ap.parse_args()

    indir = args.indir
    # We want files named like NNN-seedname.branches.json in order by NNN
    paths = sorted(glob.glob(os.path.join(indir, "*/*.branches.json")))
    # Also handle possible flat layout (if user changed script)
    paths += sorted([p for p in glob.glob(os.path.join(indir, "*.branches.json"))])

    # Sort by the numeric index at the start (0001-...)
    def sort_key(p):
        bn = os.path.basename(p)
        m = re.match(r"(\d+)-", bn)
        return int(m.group(1)) if m else 10**9
    paths = sorted(set(paths), key=sort_key)

    seen = set()  # union of covered branches so far
    rows = []     # for per_seed_coverage.csv

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
                bset.add((fpath, int(line), int(bi)))

        # “new” = branches in this seed not seen before
        new_hits = bset - seen
        seen |= bset

        # index from filename
        bn = os.path.basename(p)
        m = re.match(r"(\d+)-", bn)
        idx = int(m.group(1)) if m else len(rows) + 1

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

    # Write CSVs
    per_seed_csv = os.path.join(indir, f"per_seed_coverage.csv")

    with open(per_seed_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "index","seed","mtime_unix","mtime_iso","exit_code","crashed",
            "covered_branches_this_seed","new_branches_this_seed","cumulative_covered_branches"
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Wrote:\n  {per_seed_csv}\n")

if __name__ == "__main__":
    main()

