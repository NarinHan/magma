#!/usr/bin/env bash
set -euo pipefail

# ========== USER SETTINGS ==========
FUZZBIN="/magma_out/libpng_read_fuzzer"                     # your libFuzzer-built binary
SEED_DIR="/magma/targets/libpng/corpus/libpng_read_fuzzer"  # directory containing seed files
OUTROOT="/magma/outdir_per_seed"                            # root output directory
ROOT="/magma/targets/libpng"                                # project root for gcovr -r (your compilation root)
TIMEOUT_SEC=3                                               # per-seed wall timeout for the target
EXTRA_FUZZ_ARGS=()                                          # e.g. ("-detect_leaks=0" "-rss_limit_mb=0")
GCOVR_BIN="gcovr"                                           # ensure gcovr==5.0 is in PATH
# ===================================

mkdir -p "$OUTROOT"
LOG="$OUTROOT/run_gcovr_per_seed.log"
: > "$LOG"

# Helper: delete all .gcda under ROOT
reset_counters() {
  find "$ROOT" -type f -name '*.gcda' -delete 2>/dev/null || true
}

# Build a null-delimited list "mtime<TAB>path" for files in SEED_DIR
# Sort by mtime then path, then cut to get paths
TMP_SORTED="$(mktemp)"
trap 'rm -f "$TMP_SORTED"' EXIT

# Gather (mtime, path) null-delimited to handle spaces safely
# -printf '%T@ %p\0' gives epoch.msec and path
find "$SEED_DIR" -maxdepth 1 -type f -printf '%T@\t%p\0' \
  | sort -z -n -t $'\t' -k1,1 -k2,2 \
  | cut -z -f2- > "$TMP_SORTED"

# Count seeds and compute zero-padding width
NUM_SEEDS=$(tr -cd '\0' < "$TMP_SORTED" | wc -c | awk '{print $1}')
PAD=${#NUM_SEEDS}

echo "Found $NUM_SEEDS seeds in $SEED_DIR" | tee -a "$LOG"

# Read sorted seeds into array
mapfile -d '' SEEDS < "$TMP_SORTED"

# A small function to safely run the target once with a seed
run_one_seed() {
  local seed="$1"
  # Prefer libFuzzer's single-input run by passing the seed file and limiting work
  # -runs=1 works when input is a directory; with a single file it usually executes it once.
  # To be robust, we just pass the file path (libFuzzer calls LLVMFuzzerTestOneInput on it).
  timeout --preserve-status "${TIMEOUT_SEC}s" \
    "$FUZZBIN" "${EXTRA_FUZZ_ARGS[@]}" "$seed"
}

i=0
for seed in "${SEEDS[@]}"; do
  i=$((i+1))
  base="$(basename "$seed")"
  idx=$(printf "%0${PAD}d" "$i")
  outdir="$OUTROOT/$idx-$base"
  mkdir -p "$outdir"

  # mtime (seconds) and ISO time (for logs)
  MTIME_UNIX=$(stat -c %Y "$seed")
  MTIME_ISO=$(date -d "@$MTIME_UNIX" --iso-8601=seconds || date -r "$MTIME_UNIX" +"%Y-%m-%dT%H:%M:%S%z")

  echo "[$idx/$NUM_SEEDS] $base (mtime=$MTIME_UNIX $MTIME_ISO)" | tee -a "$LOG"

  # 1) reset coverage counters
  reset_counters

  # 2) run target once on this seed
  crashed=0
  if run_one_seed "$seed"; then
    exitcode=0
  else
    exitcode=$?
    crashed=1
    echo "  -> target exited non-zero (code=$exitcode) for $base" | tee -a "$LOG"
  fi

  # 3) Collect coverage with gcovr (branch detail)
  #    If there are no .gcda, gcovr may produce empty or error—detect and fallback.
  #    We try to run gcovr first; if it fails, we record an empty stub.
  gcovr_json="$outdir/$idx-$base.gcovr.json"
  gcovr_rc=0
  if compgen -G "$ROOT/**/*.gcda" >/dev/null 2>&1 || find "$ROOT" -name '*.gcda' | read -r _; then
    if ! "$GCOVR_BIN" -r "$ROOT" --json --json-pretty --branches -o "$gcovr_json"; then
      gcovr_rc=$?
    fi
  else
    gcovr_rc=99
  fi

  if [[ $gcovr_rc -ne 0 || ! -s "$gcovr_json" ]]; then
    # Write a minimal stub our parser understands: empty set
    cat > "$gcovr_json" <<'JSON'
{
  "gcovr_version": "missing-or-empty",
  "files": [],
  "metrics": { "line": { "covered": 0, "total": 0 },
               "branch": { "covered": 0, "total": 0 } }
}
JSON
  fi

  # 3.5) Add seed + mtime metadata into the gcovr JSON itself
  python3 - "$gcovr_json" "$seed" "$MTIME_UNIX" "$MTIME_ISO" "$exitcode" "$crashed" <<'PY'
import json, sys, os

gcovr_path, seed_path, mtime_unix, mtime_iso, exitcode, crashed = sys.argv[1:]
mtime_unix = int(mtime_unix)
exitcode = int(exitcode)
crashed = int(crashed)

# Load whatever is in gcovr_path (real gcovr output or stub),
# then inject metadata fields at top-level.
try:
    with open(gcovr_path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    # If it's corrupted for any reason, preserve something usable
    data = {
        "gcovr_version": "unreadable",
        "files": [],
        "metrics": {
            "line": {"covered": 0, "total": 0},
            "branch": {"covered": 0, "total": 0},
        },
    }

data["seed"] = os.path.basename(seed_path)
data["mtime_unix"] = mtime_unix
data["mtime_iso"] = mtime_iso

# Optional but often handy for debugging / filtering:
data["exit_code"] = exitcode
data["crashed"] = crashed

with open(gcovr_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
PY

  # 4) Extract covered branches into a compact JSON
  #    We produce: { "seed": "...", "mtime_unix": ..., "mtime_iso": "...",
  #                  "exit_code": N, "crashed": 0/1,
  #                  "covered_branches": [ ["file", line, branch_index], ... ] }
  python3 - "$gcovr_json" "$outdir/$idx-$base.branches.json" "$seed" "$MTIME_UNIX" "$MTIME_ISO" "$exitcode" "$crashed" <<'PY'
import json, sys, os
gcovr_path, out_path, seed_path, mtime_unix, mtime_iso, exitcode, crashed = sys.argv[1:]
mtime_unix = int(mtime_unix); exitcode = int(exitcode); crashed = int(crashed)

# Robustly extract covered branches from gcovr 5.0 JSON.
# Schema hint: top-level { "files": [ { "file": "path", "lines": [ { "line_number": N,
#   "count": M, "branches": [ { "count": K, ... }, ... ] }, ... ] } ] }
# Not all fields are guaranteed; we defensively check.
covered = []
try:
    with open(gcovr_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    files = data.get("files", [])
    for fobj in files:
        fpath = fobj.get("file")
        for line in fobj.get("lines", []):
            ln = line.get("line_number")
            branches = line.get("branches", [])
            for bi, b in enumerate(branches):
                # A branch is "covered" if count > 0
                if isinstance(b, dict) and b.get("count", 0) > 0:
                    if fpath is not None and ln is not None:
                        covered.append([fpath, int(ln), int(bi)])
except Exception as e:
    # If parsing fails for any reason, fall back to empty set
    covered = []

out = {
    "seed": os.path.basename(seed_path),
    "mtime_unix": mtime_unix,
    "mtime_iso": mtime_iso,
    "exit_code": exitcode,
    "crashed": crashed,
    "covered_branches": covered
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
PY
done

echo "Done. Per-seed outputs in: $OUTROOT"
