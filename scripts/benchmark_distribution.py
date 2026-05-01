#!/usr/bin/env python3
"""Distribution sweep benchmark.

Generates synthetic tables with three value distributions at fixed NDV=1000
and fixed row count (~6M rows). Compares all 4 scan methods, each time
targeting ~25% actual selectivity by calibrating the threshold per distribution.

Distributions
─────────────
  uniform   – values drawn uniformly from [0, NDV)
               P(val < t) = t/NDV  →  threshold = NDV * 0.25 = 250

  skewed    – power-law: val = floor(POWER(U, 2) * NDV)  where U ~ Uniform[0,1]
               CDF: P(val < t) = sqrt(t/NDV)
               For 25 %:  sqrt(t/1000) = 0.25  →  t = 62.5  →  threshold = 62

  clustered – values sorted in physical row order (monotone increasing),
               mimics TPC-H l_shipdate layout; threshold = 250 (same 25 %)

The table is always named 'synth' so the local_storage.cpp index dispatch
activates column 0 for sketch / cubit / rabit.

Usage:
    python scripts/benchmark_distribution.py [n_rows] [runs] [threads] [out_csv]

Example:
    python scripts/benchmark_distribution.py 6000000 5 1 /tmp/results_distribution.csv

Environment:
    DUCKDB_BIN  override path to duckdb binary (default: build/release/duckdb)
"""

import csv
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT  = Path(__file__).resolve().parent.parent
DUCKDB_BIN = Path(os.environ.get("DUCKDB_BIN", REPO_ROOT / "build" / "release" / "duckdb"))

N_ROWS  = int(sys.argv[1])   if len(sys.argv) > 1 else 6_000_000
RUNS    = int(sys.argv[2])   if len(sys.argv) > 2 else 5
THREADS = int(sys.argv[3])   if len(sys.argv) > 3 else 1
OUT_CSV = sys.argv[4]        if len(sys.argv) > 4 else "/tmp/results_distribution.csv"

METHODS = ["plain", "sketch", "cubit", "rabit"]
NDV     = 1_000

# Each entry: (name, create_sql_template, threshold)
# create_sql_template uses {n_rows} and {ndv} placeholders.
DISTRIBUTIONS = [
    {
        "name":       "uniform",
        # U[0, NDV): each value equally likely
        "create_sql": (
            "CREATE TABLE synth AS "
            "SELECT (random() * {ndv})::INTEGER AS val "
            "FROM generate_series(1, {n_rows});"
        ),
        "threshold":  NDV // 4,                  # P(val < 250) = 0.25
    },
    {
        "name":       "skewed",
        # Power-law: val = floor(U^2 * NDV)
        # CDF F(t) = sqrt(t/NDV)  →  P(val < 62) ≈ 0.249
        "create_sql": (
            "CREATE TABLE synth AS "
            "SELECT (POWER(random(), 2.0) * {ndv})::INTEGER AS val "
            "FROM generate_series(1, {n_rows});"
        ),
        "threshold":  max(1, int(NDV * 0.0625)),  # ~25 % via inverse CDF
    },
    {
        "name":       "clustered",
        # Monotone sorted: row i gets value floor(i * NDV / n_rows)
        # Same uniform marginal as 'uniform', but fully sorted —
        # maximises zone-map pruning, mimics l_shipdate ordering.
        "create_sql": (
            "CREATE TABLE synth AS "
            "SELECT (row_number() OVER () * {ndv} / {n_rows})::INTEGER AS val "
            "FROM generate_series(1, {n_rows});"
        ),
        "threshold":  NDV // 4,                  # P(val < 250) = 0.25
    },
]


def build_sql(dist: dict) -> str:
    create = dist["create_sql"].format(n_rows=N_ROWS, ndv=NDV)
    threshold = dist["threshold"]
    stmts = [f"SET threads TO {THREADS};", create]
    run_id = 0
    for _ in range(RUNS):
        run_id += 1
        stmts += [
            f"SELECT 's' AS tag, {run_id} AS run_id, epoch_ms(now()) AS ts;",
            f"SELECT count(*) FROM synth WHERE val < {threshold};",
            f"SELECT 'e' AS tag, {run_id} AS run_id, epoch_ms(now()) AS ts;",
        ]
    return "\n".join(stmts)


def run_method_dist(method: str, dist: dict) -> list[float]:
    """Returns a list of elapsed times in ms (length up to RUNS)."""
    env = os.environ.copy()
    env["DUCKDB_SCAN_METHOD"] = method

    sql = build_sql(dist)
    proc = subprocess.run(
        [str(DUCKDB_BIN), "-csv", ":memory:"],
        input=sql, capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        print(f"  [WARN] exit {proc.returncode}: {proc.stderr[:300]}", file=sys.stderr)

    starts: dict[int, int] = {}
    ends:   dict[int, int] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^([se]),(\d+),(\d+)$", line.strip())
        if m:
            tag, rid, ts = m.group(1), int(m.group(2)), int(m.group(3))
            (starts if tag == "s" else ends)[rid] = ts

    times: list[float] = []
    for i in range(1, RUNS + 1):
        if i in starts and i in ends:
            times.append(float(ends[i] - starts[i]))   # ms
    return times


def main() -> None:
    if not DUCKDB_BIN.exists():
        print(f"ERROR: duckdb binary not found at {DUCKDB_BIN}", file=sys.stderr)
        sys.exit(1)

    print(f"DuckDB binary : {DUCKDB_BIN}")
    print(f"N_ROWS={N_ROWS}  NDV={NDV}  runs={RUNS}  threads={THREADS}  output={OUT_CSV}")
    print(f"Distributions: {[d['name'] for d in DISTRIBUTIONS]}")
    print(f"Methods      : {METHODS}\n")

    all_rows: list[dict] = []

    for dist in DISTRIBUTIONS:
        print(f"Distribution: {dist['name']:<12}  threshold={dist['threshold']}")
        for method in METHODS:
            print(f"  {method:<8} ...", end=" ", flush=True)
            times = run_method_dist(method, dist)
            med = statistics.median(times) if times else None
            print(f"median={med:.0f}ms" if med is not None else "NO DATA")
            for i, t in enumerate(times, 1):
                all_rows.append({
                    "distribution": dist["name"],
                    "method":       method,
                    "run":          i,
                    "time_ms":      f"{t:.3f}",
                })
            for i in range(len(times) + 1, RUNS + 1):
                all_rows.append({
                    "distribution": dist["name"],
                    "method": method, "run": i, "time_ms": "",
                })

    Path(OUT_CSV).parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["distribution", "method", "run", "time_ms"]
        )
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} rows to {OUT_CSV}")


if __name__ == "__main__":
    main()
