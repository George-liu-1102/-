#!/usr/bin/env python3
# run_veg_monitor.py
# 自动化蔬菜批发价格波动与异常检测脚本
# 用法示例（命令行）：
# python run_veg_monitor.py --base-dir "C:\Users\Lenovo\projects\retail_dashboard" --generate-report

import argparse
import logging
from pathlib import Path
import sys
import traceback

# Set matplotlib backend for headless environments
import matplotlib
matplotlib.use('Agg')  # no GUI
import matplotlib.pyplot as plt
import matplotlib as mpl

# Set default font for Windows to avoid missing glyphs (optional)
mpl.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
mpl.rcParams['axes.unicode_minus'] = False

import pandas as pd
import numpy as np
import seaborn as sns
sns.set_style("whitegrid")

# ---- Configuration defaults ----
DEFAULT_BASE = Path.home() / "projects" / "retail_dashboard"
DEFAULT_DATA = DEFAULT_BASE / "data" / "veg_wholesale_sample.csv"
DEFAULT_OUT = DEFAULT_BASE / "outputs"

# thresholds (tunable)
PRICE_Z_THRESHOLD = 3.0
VOL_DROP_THRESHOLD = -0.20
VOL_RISE_THRESHOLD = 0.20

# ---- Utility: setup logging ----
def setup_logging(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "veg_monitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return log_file

# ---- Core data pipeline functions ----
def load_data(path: Path):
    logging.info(f"Loading data from {path}")
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {path}")
    df = pd.read_csv(path, parse_dates=['date'])
    logging.info(f"Loaded rows: {len(df)}")
    return df

def aggregate_daily(df: pd.DataFrame):
    # aggregate to daily city-vegetable
    daily = df.groupby(['date','city','vegetable'], as_index=False).agg(
        avg_price=('wholesale_price','mean'),
        total_volume=('volume_kg','sum'),
    )
    daily['total_revenue'] = (daily['avg_price'] * daily['total_volume']).round(2)
    daily = daily.sort_values(['city','vegetable','date']).reset_index(drop=True)
    daily['price_pct_ch'] = daily.groupby(['city','vegetable'])['avg_price'].pct_change()
    daily['vol_pct_ch'] = daily.groupby(['city','vegetable'])['total_volume'].pct_change()
    return daily

def compute_rolling(daily: pd.DataFrame):
    def compute_rolling_group(g):
        g = g.sort_values('date').copy()
        g['rolling_vol'] = g['price_pct_ch'].rolling(window=14, min_periods=7).std()
        g['rolling_mean'] = g['avg_price'].rolling(window=30, min_periods=15).mean()
        g['rolling_std'] = g['avg_price'].rolling(window=30, min_periods=15).std()
        g['rolling_z'] = (g['avg_price'] - g['rolling_mean']) / g['rolling_std'].replace({0: np.nan})
        return g
    # IMPORTANT: reset_index() (not drop=True) so 'city' and 'vegetable' become columns
    vol = daily.groupby(['city','vegetable']).apply(compute_rolling_group).reset_index()
    return vol

def classify_anomalies(vol: pd.DataFrame, price_z_threshold=PRICE_Z_THRESHOLD,
                       vol_drop_threshold=VOL_DROP_THRESHOLD, vol_rise_threshold=VOL_RISE_THRESHOLD):
    v = vol.copy()
    v['price_anom'] = v['rolling_z'].abs() > price_z_threshold
    v['vol_drop'] = v['vol_pct_ch'] < vol_drop_threshold
    v['vol_rise'] = v['vol_pct_ch'] > vol_rise_threshold
    v['supply_risk'] = (v['rolling_z'] > price_z_threshold) & v['vol_drop']
    v['demand_spike'] = (v['rolling_z'] > price_z_threshold) & v['vol_rise']
    v['price_shock_any'] = v['price_anom']
    return v

def save_outputs(vol: pd.DataFrame, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    # full volatility table
    full_path = out_dir / "veg_volatility_full.csv"
    vol.to_csv(full_path, index=False, encoding='utf-8-sig')
    # anomalies by rule
    (vol[vol['price_shock_any']].sort_values('rolling_z', key=lambda s: s.abs(), ascending=False)
       .to_csv(out_dir / "veg_anomalies_rolling_price.csv", index=False, encoding='utf-8-sig'))
    (vol[vol['supply_risk']].sort_values('rolling_z', ascending=False)
       .to_csv(out_dir / "veg_anomalies_supply_risk.csv", index=False, encoding='utf-8-sig'))
    (vol[vol['demand_spike']].sort_values('rolling_z', ascending=False)
       .to_csv(out_dir / "veg_anomalies_demand_spike.csv", index=False, encoding='utf-8-sig'))
    logging.info(f"Saved outputs in {out_dir}")
    return full_path

def generate_report(vol: pd.DataFrame, out_dir: Path, top_k=12):
    # Generate small set of PNGs and an index.html report
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
    vol_summary = vol.groupby(['city','vegetable'])['rolling_vol'].mean().reset_index().rename(columns={'rolling_vol':'avg_rolling_vol'})
    top = vol_summary.sort_values('avg_rolling_vol', ascending=False).head(top_k)
    generated = []
    for _, r in top.iterrows():
        city, veg = r['city'], r['vegetable']
        sel = vol[(vol['city']==city) & (vol['vegetable']==veg)].sort_values('date')
        if sel.empty:
            continue
        fname = f"{city}_{veg}.png".replace(" ", "_")
        figfile = fig_dir / fname
        plt.figure(figsize=(10,4))
        plt.plot(sel['date'], sel['avg_price'], label='avg_price', linewidth=1)
        if 'rolling_mean' in sel.columns:
            plt.plot(sel['date'], sel['rolling_mean'], label='rolling_mean(30d)', linestyle='--', linewidth=1)
        if 'price_anom' in sel.columns:
            anoms = sel[sel['price_anom'] == True]
            if not anoms.empty:
                plt.scatter(anoms['date'], anoms['avg_price'], color='red', s=20, label='price anomalies')
        plt.title(f"{city} - {veg}")
        plt.xlabel("date")
        plt.ylabel("avg_price")
        plt.legend(fontsize=8)
        plt.tight_layout()
        plt.savefig(figfile, dpi=150)
        plt.close()
        generated.append((city, veg, figfile.name))
    # write simple index.html
    html_path = out_dir / "veg_report_index.html"
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write("<html><head><meta charset='utf-8'><title>Veg Monitor Report</title></head><body>\n")
        f.write(f"<h3>Veg Monitor Report - {pd.Timestamp.now()}</h3>\n")
        f.write("<h4>Top volatility combos</h4>\n<table border=1><tr><th>city</th><th>vegetable</th><th>avg_rolling_vol</th></tr>\n")
        for _, r in top.iterrows():
            f.write(f"<tr><td>{r['city']}</td><td>{r['vegetable']}</td><td>{r['avg_rolling_vol']:.4f}</td></tr>\n")
        f.write("</table>\n<hr>\n")
        for city, veg, fname in generated:
            f.write(f"<h4>{city} - {veg}</h4>\n")
            f.write(f"<img src='figures/{fname}' style='max-width:800px;border:1px solid #ccc;'><br/>\n")
        f.write("</body></html>\n")
    logging.info(f"Report generated: {html_path}")
    return html_path

# ---- main ----
def main(args):
    try:
        base = Path(args.base_dir).expanduser()
        data_path = Path(args.data_path).expanduser() if args.data_path else (base / "data" / "veg_wholesale_sample.csv")
        out_dir = Path(args.out_dir).expanduser() if args.out_dir else (base / "outputs")
        log_file = setup_logging(out_dir)
        logging.info("Starting veg_monitor")
        df = load_data(data_path)
        daily = aggregate_daily(df)
        vol = compute_rolling(daily)
        vol = classify_anomalies(vol, price_z_threshold=args.price_z_threshold,
                                 vol_drop_threshold=args.vol_drop_threshold, vol_rise_threshold=args.vol_rise_threshold)
        save_outputs(vol, out_dir)
        if args.generate_report:
            generate_report(vol, out_dir, top_k=args.top_k)
        logging.info("Finished successfully")
    except Exception as e:
        logging.error("Exception during run: %s", e)
        logging.error(traceback.format_exc())
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Veg wholesale volatility & anomaly monitor")
    parser.add_argument("--base-dir", default=str(DEFAULT_BASE), help="Base project directory")
    parser.add_argument("--data-path", default="", help="CSV input path (overrides base-dir/data/...)")
    parser.add_argument("--out-dir", default="", help="Outputs directory (overrides base-dir/outputs)")
    parser.add_argument("--generate-report", action="store_true", help="Also generate HTML+PNG report")
    parser.add_argument("--top-k", type=int, default=12, help="Top K combos to plot")
    parser.add_argument("--price-z-threshold", type=float, default=PRICE_Z_THRESHOLD)
    parser.add_argument("--vol-drop-threshold", type=float, default=VOL_DROP_THRESHOLD)
    parser.add_argument("--vol-rise-threshold", type=float, default=VOL_RISE_THRESHOLD)
    args = parser.parse_args()
    main(args)