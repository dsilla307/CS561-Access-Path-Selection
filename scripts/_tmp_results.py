import pandas as pd
df = pd.read_csv("/tmp/results_selectivity.csv")
df["time_ms"] = pd.to_numeric(df["time_ms"], errors="coerce")
med = (df.dropna(subset=["time_ms"])
       .groupby(["column","selectivity","threshold","method"])["time_ms"]
       .median().reset_index())
for col in ["l_shipdate","l_tax"]:
    print(f"\n=== {col} ===")
    sub = med[med["column"]==col].copy()
    # sort selectivity by threshold value
    order = sub.drop_duplicates("selectivity").set_index("selectivity")["threshold"].to_dict()
    sel_order = sorted(order.keys(), key=lambda s: str(order[s]))
    pt = sub.pivot(index="selectivity", columns="method", values="time_ms").reindex(sel_order)
    print(pt.to_string())
