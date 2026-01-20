#!/usr/bin/env python3
"""
find_first_branch_hit_time.py

For each branch (file, line, branch_idx), find:
  - the first covered normalized time (seconds within 24h)
  - ONE responsible seed (tie-broken by smallest parsed seed index)
  - the responsible seed index (parsed from the per-seed directory name)

Expected layout for tie-breaking / index parsing:
  .../<seed_dir>/<something>.branches.json
  where <seed_dir> looks like: "0538-c52e..." or "0538_c52e..."
  We parse the leading number as the seed index.

Normalization rules:
  1) Seeds in --initial-seeds-list are forced to normalized_time = 0.
  2) Build stitched timeline by sorting all seeds by mtime_unix and accumulating deltas.
  3) If gap > --stitch-threshold-seconds, stitch it (treat gap as 0).
  4) Cap normalized time to --cap-seconds (default 86400).

Output CSV columns:
  file,line,branch_idx,first_time_sec,seed_index,seed

Sorting modes:
  - default: by branch key (file,line,branch_idx)
  - --sort-by-time: primarily by first_time_sec, then by branch key
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from dataclasses import dataclass
from glob import glob
from typing import Dict, Iterable, List, Optional, Set, Tuple


BranchKey = Tuple[str, int, int]  # (file, line, branch_idx)


@dataclass(frozen=True)
class SeedRecord:
    json_path: str
    seed: str
    mtime_unix: int
    seed_dir_base: str  # basename of the per-seed directory, e.g. "0538-c52e..."


_INDEX_RE = re.compile(r"^(\d+)[-_].*$")  # "0538-c52e..." or "0538_c52e..." etc.


def parse_seed_index(seed_dir_base: str) -> Optional[int]:
    m = _INDEX_RE.match(seed_dir_base)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def read_initial_seeds_list(path: str) -> Set[str]:
    seeds: Set[str] = set()
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            seeds.add(line)
    return seeds


def discover_branch_json_files(root: str) -> List[str]:
    patterns = [
        os.path.join(root, "**", "*.branches.json"),
        os.path.join(root, "**", "*-branches.json"),
        os.path.join(root, "**", "*branches*.json"),
    ]
    files: Set[str] = set()
    for p in patterns:
        for fp in glob(p, recursive=True):
            if os.path.isfile(fp):
                files.add(fp)
    return sorted(files)


def seed_dir_basename(json_path: str) -> str:
    return os.path.basename(os.path.dirname(json_path))


def load_seed_record(json_path: str) -> Optional[SeedRecord]:
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        seed = obj.get("seed")
        mtime = obj.get("mtime_unix")
        if not isinstance(seed, str) or not isinstance(mtime, int):
            return None
        return SeedRecord(
            json_path=json_path,
            seed=seed,
            mtime_unix=mtime,
            seed_dir_base=seed_dir_basename(json_path),
        )
    except Exception:
        return None


def compute_normalized_times(
    records: List[SeedRecord],
    initial_seeds: Set[str],
    stitch_threshold_seconds: int,
    cap_seconds: int,
) -> Dict[str, int]:
    if not records:
        return {}

    records_sorted = sorted(records, key=lambda r: r.mtime_unix)

    prev_mtime = records_sorted[0].mtime_unix
    elapsed = 0

    seed_to_time: Dict[str, int] = {}
    seed_to_time[records_sorted[0].seed] = 0

    for rec in records_sorted[1:]:
        gap = rec.mtime_unix - prev_mtime
        if gap < 0:
            gap = 0
        if gap > stitch_threshold_seconds:
            gap = 0

        elapsed += gap
        if elapsed > cap_seconds:
            elapsed = cap_seconds

        if rec.seed not in seed_to_time or elapsed < seed_to_time[rec.seed]:
            seed_to_time[rec.seed] = elapsed

        prev_mtime = rec.mtime_unix

    for s in initial_seeds:
        seed_to_time[s] = 0

    return seed_to_time


def iter_covered_branches(json_path: str) -> Iterable[BranchKey]:
    with open(json_path, "r", encoding="utf-8") as f:
        obj = json.load(f)

    covered = obj.get("covered_branches", [])
    if not isinstance(covered, list):
        return

    for item in covered:
        if (
            isinstance(item, list)
            and len(item) == 3
            and isinstance(item[0], str)
            and isinstance(item[1], int)
            and isinstance(item[2], int)
        ):
            yield (item[0], item[1], item[2])


def better_seed_choice(current_best: Optional[SeedRecord], challenger: SeedRecord) -> SeedRecord:
    """
    Choose ONE representative seed for ties at the same first_time.

    Prefer:
      1) smaller parsed seed index (from directory name)
      2) if one has index and the other doesn't, prefer the one with index
      3) lexicographically smaller seed_dir_base
      4) lexicographically smaller seed id
    """
    if current_best is None:
        return challenger

    a_idx = parse_seed_index(current_best.seed_dir_base)
    b_idx = parse_seed_index(challenger.seed_dir_base)

    if a_idx is not None and b_idx is not None and a_idx != b_idx:
        return challenger if b_idx < a_idx else current_best

    if a_idx is None and b_idx is not None:
        return challenger
    if a_idx is not None and b_idx is None:
        return current_best

    if challenger.seed_dir_base != current_best.seed_dir_base:
        return challenger if challenger.seed_dir_base < current_best.seed_dir_base else current_best

    return challenger if challenger.seed < current_best.seed else current_best


def find_first_covered_time_and_one_seed(
    records: List[SeedRecord],
    seed_to_time: Dict[str, int],
) -> Tuple[Dict[BranchKey, int], Dict[BranchKey, SeedRecord]]:
    first_time: Dict[BranchKey, int] = {}
    first_seed_rec: Dict[BranchKey, SeedRecord] = {}

    for rec in records:
        t = seed_to_time.get(rec.seed)
        if t is None:
            continue

        try:
            for b in iter_covered_branches(rec.json_path):
                if b not in first_time:
                    first_time[b] = t
                    first_seed_rec[b] = rec
                else:
                    if t < first_time[b]:
                        first_time[b] = t
                        first_seed_rec[b] = rec
                    elif t == first_time[b]:
                        first_seed_rec[b] = better_seed_choice(first_seed_rec.get(b), rec)
        except Exception:
            continue

    return first_time, first_seed_rec


def write_csv(
    out_path: str,
    first_time: Dict[BranchKey, int],
    first_seed_rec: Dict[BranchKey, SeedRecord],
    sort_by_time: bool,
) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    items = list(first_time.items())
    if sort_by_time:
        items.sort(key=lambda kv: (kv[1], kv[0][0], kv[0][1], kv[0][2]))
    else:
        items.sort(key=lambda kv: (kv[0][0], kv[0][1], kv[0][2]))

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["file", "line", "branch_idx", "first_time_sec", "seed_index", "seed"])

        for (file, line, idx), t in items:
            rec = first_seed_rec[(file, line, idx)]
            sidx = parse_seed_index(rec.seed_dir_base)
            w.writerow([file, line, idx, t, "" if sidx is None else sidx, rec.seed])


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Find first covered time (normalized seconds within 24h) for each branch, including responsible seed + seed index."
    )
    ap.add_argument(
        "--json-root",
        required=True,
        help="Root directory containing per-seed branch coverage JSON files (searched recursively).",
    )
    ap.add_argument(
        "--initial-seeds-list",
        required=True,
        help="Text file listing initial seed ids/names (one per line). These are forced to time 0.",
    )
    ap.add_argument(
        "--stitch-threshold-seconds",
        type=int,
        default=86400,
        help="If gap between consecutive seeds (by mtime) is larger than this, gap is stitched (treated as 0). Default: 86400.",
    )
    ap.add_argument(
        "--cap-seconds",
        type=int,
        default=86400,
        help="Cap normalized time to this many seconds. Default: 86400.",
    )
    ap.add_argument(
        "--sort-by-time",
        action="store_true",
        help="Sort output primarily by first_time_sec (then by branch key). Default is by branch key.",
    )
    ap.add_argument(
        "--out-csv",
        required=True,
        help="Output CSV path.",
    )

    args = ap.parse_args()

    initial_seeds = read_initial_seeds_list(args.initial_seeds_list)

    json_files = discover_branch_json_files(args.json_root)
    if not json_files:
        raise SystemExit(f"No branch JSON files found under: {args.json_root}")

    records: List[SeedRecord] = []
    skipped = 0
    for fp in json_files:
        rec = load_seed_record(fp)
        if rec is None:
            skipped += 1
            continue
        records.append(rec)

    if not records:
        raise SystemExit("Found JSON files, but none had valid {seed, mtime_unix}.")

    seed_to_time = compute_normalized_times(
        records=records,
        initial_seeds=initial_seeds,
        stitch_threshold_seconds=args.stitch_threshold_seconds,
        cap_seconds=args.cap_seconds,
    )

    first_time, first_seed_rec = find_first_covered_time_and_one_seed(records, seed_to_time)

    write_csv(
        out_path=args.out_csv,
        first_time=first_time,
        first_seed_rec=first_seed_rec,
        sort_by_time=args.sort_by_time,
    )

    print(f"Parsed JSON files: {len(records)} (skipped invalid: {skipped})")
    print(f"Initial seeds forced to 0s: {len(initial_seeds)}")
    print(f"Branches with first-covered time: {len(first_time)}")
    print(f"Sort mode: {'time' if args.sort_by_time else 'branch'}")
    print(f"Wrote: {args.out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

