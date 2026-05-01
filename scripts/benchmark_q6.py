#!/usr/bin/env python3
"""TPC-H Q6 selectivity-sweep benchmark: compare plain / sketch / cubit scan paths.

Varies the l_quantity threshold across 6 levels to produce a selectivity sweep.
All other predicates are fixed at standard Q6 values.

Each method runs dbgen + all query trials within a single DuckDB process so that
in-memory indices built during ingestion survive all scan trials.

Usage:
    python scripts/benchmark_q6.py [sf] [runs] [threads] [out_csv]

Example:
    python scripts/benchmark_q6.py 3 5 1 results_q6_sf3.csv

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

REPO_ROOT = Path(__file__).resolve().parent.parent
DUCKDB_BIN = Path(os.environ.get("DUCKDB_BIN", REPO_ROOT / "build" / "release" / "duckdb"))

SF      = float(sys.argv[1]) if len(sys.argv) > 1 else 3
RUNS    = int(sys.argv[2])   if len(sys.argv) > 2 else 5
THREADS = int(sys.argv[3])   if len(sys.argv) > 3 else 1
OUT_CSV = sys.argv[4]        if len(sys.argv) > 4 else f"results_q6_sf{SF:.0f}.csv"

METHODS = ["plain", "sketch", "cubit"]

# l_quantity < X  (1-50 uniform distribution).
# Approximate fraction of rows that pass the quantity predicate alone: X/50.
# Combined with shipdate (~14%) and discount (~27%) filters, overall selectivity
# of full Q6 ≈ 0.14 * 0.27 * (X/50).  Labels indicate this approximate level.
SELECTIVITY_LEVELS = [
    {"label": "qty<5",  "qty": 5},   # ~1.5%  overall
    {"label": "qty<10", "qty": 10},  # ~3%
    {"label": "qty<15", "qty": 15},  # ~4.5%
    {"label": "qty<24", "qty": 24},  # ~7.3%  (standard Q6)
    {"label": "qty<35", "qty": 35},  # ~10.6%
    {"label": "qty<50", "qty": 50},  # ~15.1%
]

# Standard Q6 shipdate and discount predicates (fixed across all levels).
_SHIPDATE_GE = "date '1994-01-01'"
_SHIPDATE_LT = "date '1995-01-01'"
_DISC_LO     = "CAST(0.06 - 0.01 AS DOUBLE)"
_DISC_HI     = "CAST(0.06 + 0.01 AS DOUBLE)"


def q6_sql(qty_threshold: int) -> str:
    return (
        f"SELECT sum(l_extendedprice * l_discount) AS revenue "
        f"FROM lineitem "
        f"WHERE l_shipdate >= {_SHIPDATE_GE} "
        f"  AND l_shipdate < {_SHIPDATE_LT} "
        f"  AND l_discount BETWEEN {_DISC_LO} AND {_DISC_HI} "
        f"  AND l_quantity < {qty_threshold};"
    )


def build_sql(runs: int, qty_threshold: int) -> str:
    """SQL script: ingest data then run N timed trials of the parameterised Q6."""
    stmts = [
        "LOAD tpch;",
        f"SET threads TO {THREADS};",
        f"CALL dbgen(sf={SF});",
    ]
    for i in range(1, runs + 1):
        stmts += [
            f"SELECT 's' AS tag, {i} AS run_id, epoch_ms(now()) AS ts;",
            q6_sql(qty_threshold),
            f"SELECT 'e' AS tag, {i} AS run_id, epoch_ms(now()) AS ts;",
        ]
    return "\n".join(stmts)


def run_method(method: str, qty_threshold: int) -> dict[int, float]:
    """Run all trials for one method+selectivity. Returns {run_id: elapsed_seconds}."""
    env = os.environ.copy()
    env["DUCKDB_SCAN_METHOD"] = method

    sql = build_sql(RUNS, qty_threshold)
    proc = subprocess.run(
        [str(DUCKDB_BIN), "-csv", ":memory:"],
        input=sql,
        capture_output=True,
        text=True,
        env=env,
    )

    if proc.returncode != 0:
        print(f"  [WARN] duckdb exited with code {proc.returncode}", file=sys.stderr)
        if proc.stderr:
            print(f"  stderr: {proc.stderr[:400]}", file=sys.stderr)

    starts: dict[int, int] = {}
    ends:   dict[int, int] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r'^([se]),(\d+),(\d+)$', line.strip())
        if m:
            tag, run_id, ts = m.group(1), int(m.group(2)), int(m.group(3))
            (starts if tag == 's' else ends)[run_id] = ts

    times: dict[int, float] = {}
    for i in range(1, RUNS + 1):
        if i in starts and i in ends:
            times[i] = (ends[i] - starts[i]) / 1000.0  # ms -> s
    return times


def main() -> None:
    if not DUCKDB_BIN.exists():
        print(f"ERROR: duckdb binary not found at {DUCKDB_BIN}", file=sys.stderr)
        print("Build first: make release", file=sys.stderr)
        sys.exit(1)

    print(f"DuckDB binary : {DUCKDB_BIN}")
    print(f"SF={SF}  runs={RUNS}  threads={THREADS}  output={OUT_CSV}")
    print(f"Selectivity levels  : {[s['label'] for s in SELECTIVITY_LEVELS]}")
    print(f"Methods             : {METHODS}")

    all_rows: list[dict] = []

    for level in SELECTIVITY_LEVELS:
        label = level["label"]
        qty   = level["qty"]
        print(f"\n{'='*52}")
        print(f"Selectivity level: {label}  (l_quantity < {qty})")
        print(f"{'='*52}")
        for method in METHODS:
            print(f"  method={method} ...", end=" ", flush=True)
            times = run_method(method, qty)
            ts_list = [times[i] for i in range(1, RUNS + 1) if i in times]
            if ts_list:
                med = statistics.median(ts_list)
                print(f"median={med:.4f}s  ({len(ts_list)}/{RUNS} runs)")
            else:
                print("NO DATA")
            for i in range(1, RUNS + 1):
                t = times.get(i)
                all_rows.append({
                    "selectivity": label,
                    "qty_threshold": qty,
                    "method":  method,
                    "run":     i,
                    "time_s":  f"{t:.6f}" if t is not None else "",
                })

    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["selectivity", "qty_threshold", "method", "run", "time_s"]
        )
        writer.writeheader()
        writer.writerows(all_rows)

    # Summary table
    print(f"\n{'='*70}")
    print(f"CSV written to {OUT_CSV}")
    print(f"{'='*70}")
    print(f"{'Selectivity':<12} {'Method':<10} {'N':>4}  {'Median (s)':>12}  {'Mean (s)':>12}")
    print("-" * 56)
    for level in SELECTIVITY_LEVELS:
        label = level["label"]
        for method in METHODS:
            ts = [
                float(r["time_s"])
                for r in all_rows
                if r["selectivity"] == label and r["method"] == method and r["time_s"]
            ]
            if ts:
                med  = statistics.median(ts)
                mean = statistics.mean(ts)
                print(f"{label:<12} {method:<10} {len(ts):>4}  {med:>12.4f}  {mean:>12.4f}")
            else:
                print(f"{label:<12} {method:<10}   -- no data --")
        print()


if __name__ == "__main__":
    main()
