# Running the CUBIT / RABIT Benchmark Experiments
This project contains CUBIT and RABIT implementations in DuckDB for a Database Architecture course. Below are instructions
to run scripts which perform experiments benchmarking the performance of RABIT, CUBIT, Sketches, and plain queries.

## Prerequisites

1. **Build the project** (required before any benchmark):
   ```sh
   make release
   ```
   The binary will be at `build/release/duckdb`.

2. All benchmark scripts are run from the repo root inside WSL  
   From PowerShell on Windows, prefix every command with:
   ```powershell
   wsl bash -c "cd /CS561-Access-Path-Selection && <command>"
   ```

---

## Experiment 1 — Selectivity Sweep (TPC-H lineitem)

Runs `SELECT count(*) FROM lineitem WHERE <col> < <threshold>` across 5 columns
and 5 selectivity levels, comparing all four scan methods.

```sh
python3 scripts/benchmark_selectivity.py [sf] [runs] [threads] [out_csv]
```

| Argument | Default | Description |
|---|---|---|
| `sf` | `3` | TPC-H scale factor |
| `runs` | `5` | Timed repetitions per (column, threshold, method) |
| `threads` | `1` | DuckDB thread count |
| `out_csv` | `/tmp/results_selectivity_sf3.csv` | Output path |

**Example:**
```sh
python3 scripts/benchmark_selectivity.py 3 5 1 data/results_selectivity.csv
```

Columns benchmarked: `l_discount`, `l_extendedprice`, `l_quantity`, `l_shipdate`, `l_tax`.

---

## Experiment 2 — Cardinality (NDV) Sweep

Sweeps NDV from 10 to 100,000 on a synthetic 6M-row uniform table at fixed ~25% selectivity.

```sh
python3 scripts/benchmark_cardinality.py [n_rows] [runs] [threads] [out_csv]
```

**Example:**
```sh
python3 scripts/benchmark_cardinality.py 6000000 5 1 data/results_cardinality.csv
```

NDV levels: 10, 100, 1,000, 10,000, 100,000.

---

## Experiment 3 — Data Distribution

Compares scan latency across three data distributions at fixed NDV=1,000 and ~25% selectivity.

```sh
python3 scripts/benchmark_distribution.py [n_rows] [runs] [threads] [out_csv]
```

**Example:**
```sh
python3 scripts/benchmark_distribution.py 6000000 5 1 data/results_distribution.csv
```

Distributions:
- **Uniform** — values drawn uniformly at random from [0, 1000)
- **Skewed** — power-law distribution (values concentrated near 0)
- **Sorted** — values in monotonically increasing physical row order (mimics `l_shipdate`)

---

## How Scan Methods Are Selected

The scan method is controlled by the `DUCKDB_SCAN_METHOD` environment variable.
The benchmark scripts set this automatically for each method.

| Value | Description |
|---|---|
| `plain` | Standard DuckDB zone-map scan (baseline) |
| `sketch` | Column Sketch-based scan |
| `cubit` | CUBIT bitmap index scan |
| `rabit` | RABIT range-encoded bitmap index scan |

Indexes are built automatically during `dbgen` / table ingestion for the columns
configured in `src/storage/local_storage.cpp`.

---

## Pre-run Results

Pre-generated CSVs from the last benchmark run are stored in `data/`:

- `data/results_selectivity.csv` — SF=3, 5 runs, 1 thread
- `data/results_cardinality.csv` — 6M rows, 5 runs, 1 thread
- `data/results_distribution.csv` — 6M rows, 5 runs, 1 thread
