"""run_veg_monitor 核心逻辑的单元测试。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_veg_monitor as rvm

ROOT = Path(__file__).resolve().parents[1]


def make_daily(n_products: int = 3, n_days: int = 90, seed: int = 7) -> pd.DataFrame:
    """构造带人为暴涨点的合成日度数据。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-01-01", periods=n_days, freq="D")
    rows = []
    for p in range(n_products):
        base = 2.0 + p
        for d in dates:
            price = base + rng.normal(0, 0.08)
            rows.append({
                "date": d,
                "product": f"测试蔬菜{p + 1}",
                "avg_price": round(price, 3),
                "origin": "冀",
            })
    df = pd.DataFrame(rows)
    # 第 40 天对第一个品种制造一次 +50% 的价格跳升
    mask = (df["product"] == "测试蔬菜1") & (df["date"] == dates[39])
    df.loc[mask, "avg_price"] = df.loc[mask, "avg_price"] * 1.5
    return df


def test_load_data_real_csv():
    path = ROOT / "data" / "veg_wholesale_xinfadi.csv"
    if not path.exists():
        pytest.skip("真实数据集不存在，请先运行 build_xinfadi_dataset.py")
    df = rvm.load_data(path)
    assert {"date", "product", "avg_price"}.issubset(df.columns)
    assert df["avg_price"].notna().all()
    assert (df["avg_price"] > 0).all()
    assert df["product"].nunique() > 50


def test_compute_features_creates_rolling_stats():
    df = make_daily()
    out = rvm.compute_features(df)
    for col in ["price_pct_ch", "rolling_mean", "rolling_std", "rolling_z", "rolling_vol"]:
        assert col in out.columns
    mature = out[out["date"] >= pd.Timestamp("2026-03-01")]
    assert mature["rolling_mean"].notna().all()
    assert mature["rolling_std"].notna().all()


def test_classify_detects_planted_surge():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    spike = v[(v["product"] == "测试蔬菜1") & (v["date"] == pd.Timestamp("2026-02-09"))]
    assert not spike.empty
    assert bool(spike["surge"].iloc[0])
    assert bool(spike["anomaly_any"].iloc[0])


def test_classify_no_anomaly_in_quiet_series():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    quiet = v[v["product"] == "测试蔬菜3"]
    assert int(quiet["surge"].sum()) == 0
    assert int(quiet["crash"].sum()) == 0


def test_build_summary_sorted_by_risk():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    summary = rvm.build_summary(v)
    assert "risk_score" in summary.columns
    scores = summary["risk_score"].tolist()
    assert scores == sorted(scores, reverse=True)


def test_build_alert_message_contains_product():
    df = make_daily()
    v = rvm.compute_features(df)
    v = rvm.classify_anomalies(v)
    msg = rvm.build_alert_message(v)
    assert isinstance(msg, str)
    assert "测试蔬菜" in msg


def test_send_alert_dry_run():
    assert rvm.send_alert("测试消息", dry_run=True) is True