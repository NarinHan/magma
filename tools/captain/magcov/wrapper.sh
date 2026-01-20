#!/usr/bin/env bash
set -euo pipefail

# Helper function to print timestamp
timestamp() {
  date "+%Y-%m-%d %H:%M:%S"
}

# Helper function to measure duration
run_step() {
  local step_name=$1
  shift
  echo "=== [$step_name] STARTED at $(timestamp) ==="
  local start=$(date +%s)

  "$@"   # run the actual command

  local end=$(date +%s)
  local duration=$((end - start))
  echo "=== [$step_name] FINISHED at $(timestamp), took ${duration}s ==="
  echo
}

# Step 1: Run the per-seed gcovr collection
run_step "run_gcovr_per_seed" /magma/magcov/run_gcovr_per_seed.sh

# Step 2: Run per-seed analysis
run_step "analyze_per_seed" python3 /magma/magcov/analyze_per_seed.py --in /magma/outdir_per_seed

# Step 3: Run to find coverage over time per seed 
run_step "get_cov_over_time" python3 /magma/magcov/get_cov_over_time.py \
	--csv /magma/outdir_per_seed/per_seed_coverage.csv \
	--out /magma/outdir_per_seed/cov_over_time_per_seed.csv \
        --initial-seeds-file /magma/magcov/initial_seeds.txt

# Step 4: Run to find first branch hit itme
run_step "find_first_branch_hit_time" python3 /magma/magcov/find_first_branch_hit_time.py \
	--json-root /magma/outdir_per_seed \
	--initial-seeds-list /magma/magcov/initial_seeds.txt \
	--sort-by-time \
	--out-csv /magma/outdir_per_seed/first_branch_hit_time.csv

echo "=== All tasks finished successfully at $(timestamp) ==="

