#!/usr/bin/env bash
set -euo pipefail

# Reproducible TPC-H Q6 timing harness for this DuckDB fork.
# Usage:
#   scripts/run_tpch_q6_experiment.sh [sf] [runs] [threads] [out_csv]
# Example:
#   scripts/run_tpch_q6_experiment.sh 10 10 1 results_q6_sf10.csv

SF="${1:-10}"
RUNS="${2:-10}"
THREADS="${3:-1}"
OUT_CSV="${4:-results_q6_sf${SF}.csv}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DUCKDB_BIN="${ROOT_DIR}/build/release/duckdb"

if [[ ! -x "${DUCKDB_BIN}" ]]; then
  echo "duckdb binary not found at ${DUCKDB_BIN}. Build first: make release" >&2
  exit 1
fi

WORK_DIR="$(mktemp -d)"
DB_FILE="${WORK_DIR}/tpch_sf${SF}.duckdb"

cleanup() {
  rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

echo "Preparing database (sf=${SF}, threads=${THREADS})..."
"${DUCKDB_BIN}" "${DB_FILE}" -unsigned <<SQL >/dev/null
LOAD tpch;
SET threads TO ${THREADS};
CALL dbgen(sf=${SF});
SQL

echo "run,time_seconds" > "${OUT_CSV}"

echo "Running Q6 ${RUNS} times..."
for i in $(seq 1 "${RUNS}"); do
  START_NS="$(date +%s%N)"
  "${DUCKDB_BIN}" "${DB_FILE}" -unsigned <<SQL >/dev/null
LOAD tpch;
SET threads TO ${THREADS};
PRAGMA tpch(6);
SQL
  END_NS="$(date +%s%N)"
  ELAPSED_SEC="$(awk -v s="${START_NS}" -v e="${END_NS}" 'BEGIN { printf "%.6f", (e-s)/1000000000.0 }')"
  echo "${i},${ELAPSED_SEC}" >> "${OUT_CSV}"
  echo "  run ${i}/${RUNS}: ${ELAPSED_SEC}s"
done

echo
awk -F, 'NR>1 {sum+=$2; n++; vals[n]=$2} END {
  if (n==0) exit;
  # insertion sort for median on small n
  for (i=2;i<=n;i++) {x=vals[i]; j=i-1; while (j>=1 && vals[j]>x) {vals[j+1]=vals[j]; j--} vals[j+1]=x}
  if (n%2==1) med=vals[(n+1)/2]; else med=(vals[n/2]+vals[n/2+1])/2;
  printf "Summary: runs=%d, mean=%.6fs, median=%.6fs\n", n, sum/n, med;
}' "${OUT_CSV}"

echo "CSV written to ${OUT_CSV}"
