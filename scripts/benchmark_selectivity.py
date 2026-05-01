#!/usr/bin/env python3
"""Selectivity sweep benchmark matching CS561 midway report design.

For each of the 5 lineitem columns (l_discount, l_extendedprice, l_quantity,
l_shipdate, l_tax), runs single-predicate queries at 5 selectivity thresholds
comparing plain / sketch / cubit scan paths.

Query form (mirrors midway paper):
    SELECT count(*) FROM lineitem WHERE <col> < <threshold>

Each method runs dbgen + all trials in a single DuckDB process so in-memory
indices built during ingestion survive all scan trials.

Usage:
    python scripts/benchmark_selectivity.py [sf] [runs] [threads] [out_csv]

Example:
    python scripts/benchmark_selectivity.py 3 5 1 /tmp/results_selectivity.csv

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
OUT_CSV = sys.argv[4]        if len(sys.argv) > 4 else f"/tmp/results_selectivity_sf{SF:.0f}.csv"

METHODS = ["plain", "sketch", "cubit", "rabit"]

# Thresholds chosen to reproduce the selectivity levels from the midway report figures.
COLUMNS = [
    {
        "col": "l_discount",
        "pred_tmpl": "CAST(l_discount AS DOUBLE) < {val}",
        "thresholds": [
            {"label": "9.09%",  "val": "0.01"},
            {"label": "27.3%",  "val": "0.03"},
            {"label": "45.5%",  "val": "0.05"},
            {"label": "63.6%",  "val": "0.07"},
            {"label": "81.8%",  "val": "0.09"},
        ],
    },
    {
        "col": "l_extendedprice",
        "pred_tmpl": "CAST(l_extendedprice AS DOUBLE) < {val}",
        "thresholds": [
            {"label": "12.9%",  "val": "10000"},
            {"label": "33.7%",  "val": "25000"},
            {"label": "68.4%",  "val": "55000"},
            {"label": "92.6%",  "val": "90000"},
            {"label": "99.9%",  "val": "150000"},
        ],
    },
    {
        "col": "l_quantity",
        "pred_tmpl": "CAST(l_quantity AS INTEGER) < {val}",
        "thresholds": [
            {"label": "8.00%",  "val": "5"},
            {"label": "18.0%",  "val": "10"},
            {"label": "38.0%",  "val": "20"},
            {"label": "58.0%",  "val": "30"},
            {"label": "78.0%",  "val": "40"},
        ],
    },
    {
        "col": "l_shipdate",
        "pred_tmpl": "l_shipdate < {val}",
        "thresholds": [
            {"label": "3.78%",  "val": "date '1993-01-01'"},
            {"label": "12.7%",  "val": "date '1993-08-01'"},
            {"label": "27.8%",  "val": "date '1994-04-01'"},
            {"label": "43.0%",  "val": "date '1995-01-01'"},
            {"label": "73.4%",  "val": "date '1996-06-01'"},
        ],
    },
    {
        "col": "l_tax",
        "pred_tmpl": "CAST(l_tax AS DOUBLE) < {val}",
        "thresholds": [
            {"label": "11.1%",  "val": "0.01"},
            {"label": "22.2%",  "val": "0.02"},
            {"label": "44.4%",  "val": "0.04"},
            {"label": "66.7%",  "val": "0.06"},
            {"label": "88.9%",  "val": "0.08"},
        ],
    },
]


def build_sql(col_spec: dict) -> str:
    """Single DuckDB session: ingest once, run all threshold x run_id trials."""
    stmts = [
        "LOAD tpch;",
        f"SET threads TO {THREADS};",
        f"CALL dbgen(sf={SF});",
    ]
    run_id = 0
    for th in col_spec["thresholds"]:
        pred = col_spec["pred_tmpl"].format(val=th["val"])
        query = f"SELECT count(*) FROM lineitem WHERE {pred};"
        for _ in range(1, RUNS + 1):
            run_id += 1
            stmts += [
                f"SELECT 's' AS tag, {run_id} AS run_id, epoch_ms(now()) AS ts;",
                query,
                f"SELECT 'e' AS tag, {run_id} AS run_id, epoch_ms(now()) AS ts;",
            ]
    return "\n".join(stmts)


def run_method_column(method: str, col_spec: dict) -> dict:
    """Returns {label: [elapsed_s, ...]} for all thresholds."""
    env = os.environ.copy()
    env["DUCKDB_SCAN_METHOD"] = method

    sql = build_sql(col_spec)
    proc = subprocess.run(
        [str(DUCKDB_BIN), "-csv", ":memory:"],
        input=sql, capture_output=True, text=True, env=env,
    )
    if proc.returncode != 0:
        print(f"  [WARN] exit {proc.returncode}", file=sys.stderr)
        if proc.stderr:
            print(f"  {proc.stderr[:300]}", file=sys.stderr)

    starts: dict[int, int] = {}
    ends:   dict[int, int] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^([se]),(\d+),(\d+)$", line.strip())
        if m:
            tag, rid, ts = m.group(1), int(m.group(2)), int(m.group(3))
            (starts if tag == "s" else ends)[rid] = ts

    results: dict[str, list[float]] = {}
    run_id = 0
    for th in col_spec["thresholds"]:
        label = th["label"]
        results[label] = []
        for _ in range(1, RUNS + 1):
            run_id += 1
            if run_id in starts and run_id in ends:
                results[label].append((ends[run_id] - starts[run_id]) / 1000.0)
    return results


def main() -> None:
    if not DUCKDB_BIN.exists():
        print(f"ERROR: duckdb binary not found at {DUCKDB_BIN}", file=sys.stderr)
        sys.exit(1)

    print(f"DuckDB binary : {DUCKDB_BIN}")
    print(f"SF={SF}  runs={RUNS}  threads={THREADS}  output={OUT_CSV}")
    print(f"Columns  : {[c['col'] for c in COLUMNS]}")
    print(f"Methods  : {METHODS}")

    all_rows: list[dict] = []

    for col_spec in COLUMNS:
        col = col_spec["col"]
        print(f"\n{'='*60}")
        print(f"Column: {col}")
        print(f"{'='*60}")
        for method in METHODS:
            print(f"  method={method} ...", end=" ", flush=True)
            result = run_method_column(method, col_spec)
            summaries = []
            for th in col_spec["thresholds"]:
                label = th["label"]
                ts_list = result.get(label, [])
                med = statistics.median(ts_list) if ts_list else None
                if med is not None:
                    summaries.append(f"{label}={med*1000:.0f}ms")
                for i, t in enumerate(ts_list, 1):
                    all_rows.append({
                        "column":      col,
                        "selectivity": label,
                        "threshold":   th["val"],
                        "method":      method,
                        "run":         i,
                        "time_ms":     f"{t*1000:.3f}",
                    })
                for i in range(len(ts_list) + 1, RUNS + 1):
                    all_rows.append({
                        "column": col, "selectivity": label,
                        "threshold": th["val"], "method": method,
                        "run": i, "time_ms": "",
                    })
            print("  ".join(summaries) if summaries else "NO DATA")

    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["column", "selectivity", "threshold", "method", "run", "time_ms"]
        )
        writer.writeheader()
        writer.writerows(all_rows)

    # Summary table
    print(f"\n{'='*72}")
    print(f"CSV written to {OUT_CSV}")
    print(f"{'='*72}")
    print(f"{'Column':<20} {'Selectivity':<10} {'Method':<8} {'N':>3}  {'Median (ms)':>12}")
    print("-" * 60)
    for col_spec in COLUMNS:
        col = col_spec["col"]
        for th in col_spec["thresholds"]:
            for method in METHODS:
                ts = [
                    float(r["time_ms"])
                    for r in all_rows
                    if r["column"] == col and r["selectivity"] == th["label"]
                       and r["method"] == method and r["time_ms"]
                ]
                if ts:
                    print(f"{col:<20} {th['label']:<10} {method:<8} {len(ts):>3}  {statistics.median(ts):>12.1f}")
                else:
                    print(f"{col:<20} {th['label']:<10} {method:<8}  -- no data --")
        print()


if __name__ == "__main__":
    main()
