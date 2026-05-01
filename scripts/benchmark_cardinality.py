#!/usr/bin/env python3
"""Cardinality (NDV) sweep benchmark.

Generates a synthetic table with uniform-random INTEGER values, varying NDV
from 10 to 100 000 at a fixed row count. Compares all 4 scan methods at a
fixed ~25% selectivity (threshold = NDV // 4).

The table is named 'synth' so that local_storage.cpp's index dispatch fires
on column 0.

Usage:
    python scripts/benchmark_cardinality.py [n_rows] [runs] [threads] [out_csv]

Example:
    python scripts/benchmark_cardinality.py 6000000 5 1 /tmp/results_cardinality.csv

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
OUT_CSV = sys.argv[4]        if len(sys.argv) > 4 else "/tmp/results_cardinality.csv"

METHODS    = ["plain", "sketch", "cubit", "rabit"]
NDV_LEVELS = [10, 100, 1_000, 10_000, 100_000]


def build_sql(ndv: int) -> str:
    threshold = max(1, ndv // 4)   # ~25 % selectivity for uniform distribution
    stmts = [
        f"SET threads TO {THREADS};",
        # generate_series produces integers 1..N_ROWS; we map each to [0, ndv)
        f"CREATE TABLE synth AS "
        f"SELECT (random() * {ndv})::INTEGER AS val "
        f"FROM generate_series(1, {N_ROWS});",
    ]
    run_id = 0
    for _ in range(RUNS):
        run_id += 1
        stmts += [
            f"SELECT 's' AS tag, {run_id} AS run_id, epoch_ms(now()) AS ts;",
            f"SELECT count(*) FROM synth WHERE val < {threshold};",
            f"SELECT 'e' AS tag, {run_id} AS run_id, epoch_ms(now()) AS ts;",
        ]
    return "\n".join(stmts)


def run_method_ndv(method: str, ndv: int) -> list[float]:
    """Returns a list of elapsed times in ms (length up to RUNS)."""
    env = os.environ.copy()
    env["DUCKDB_SCAN_METHOD"] = method

    sql = build_sql(ndv)
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
    print(f"N_ROWS={N_ROWS}  runs={RUNS}  threads={THREADS}  output={OUT_CSV}")
    print(f"NDV levels : {NDV_LEVELS}")
    print(f"Methods    : {METHODS}")
    print(f"Selectivity: ~25% (threshold = NDV // 4)\n")

    all_rows: list[dict] = []

    for ndv in NDV_LEVELS:
        threshold = max(1, ndv // 4)
        print(f"NDV={ndv:>7}  threshold={threshold}")
        for method in METHODS:
            print(f"  {method:<8} ...", end=" ", flush=True)
            times = run_method_ndv(method, ndv)
            med = statistics.median(times) if times else None
            print(f"median={med:.0f}ms" if med is not None else "NO DATA")
            for i, t in enumerate(times, 1):
                all_rows.append({
                    "ndv":     ndv,
                    "method":  method,
                    "run":     i,
                    "time_ms": f"{t:.3f}",
                })
            for i in range(len(times) + 1, RUNS + 1):
                all_rows.append({
                    "ndv": ndv, "method": method, "run": i, "time_ms": "",
                })

    Path(OUT_CSV).parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ndv", "method", "run", "time_ms"])
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} rows to {OUT_CSV}")


if __name__ == "__main__":
    main()
