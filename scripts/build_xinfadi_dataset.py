#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清洗新发地原始行情数据，生成分析用数据集。

输入：data/raw/xinfadi_raw.jsonl（由 fetch_xinfadi_data.py 下载）
输出：
  - data/raw/xinfadi_vegetables_raw.jsonl（蔬菜子集，作为溯源保留）
  - data/veg_wholesale_xinfadi.csv（日度 品种 级别分析数据）

清洗规则：
  1. 只保留 prodCat == "蔬菜" 的记录；
  2. 只保留 unitInfo == "斤" 的记录（其他单位不具可比性）；
  3. 价格字段转 float，缺失的 low/high 用 avg 兜底；
  4. 同一 (date, product) 的多个规格/产地记录聚合为一行：均价取均值，
     最低/最高价取极值，产地合并去重（如 "冀/辽"）；
  5. 丢弃均价 <= 0 的异常记录。
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = ROOT / "data" / "raw" / "xinfadi_raw.jsonl"
DEFAULT_VEG_RAW = ROOT / "data" / "raw" / "xinfadi_vegetables_raw.jsonl"
DEFAULT_OUT = ROOT / "data" / "veg_wholesale_xinfadi.csv"


def read_raw(path: Path) -> list:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def to_float(value, fallback=None):
    if value is None or str(value).strip() == "":
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def clean(rows: list) -> pd.DataFrame:
    veg = [r for r in rows if r.get("prodCat") == "蔬菜"]
    if not veg:
        raise ValueError("原始数据中没有蔬菜类记录")

    records = []
    for r in veg:
        date_str = str(r.get("pubDate", ""))[:10]
        avg = to_float(r.get("avgPrice"))
        if not date_str or avg is None or avg <= 0:
            continue
        if r.get("unitInfo") != "斤":
            continue
        records.append({
            "date": date_str,
            "product": r.get("prodName"),
            "avg_price": avg,
            "low_price": to_float(r.get("lowPrice"), avg),
            "high_price": to_float(r.get("highPrice"), avg),
            "origin": str(r.get("place") or "").strip(),
            "unit": "斤",
        })

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])

    # 同一天同一品种的多个规格/产地记录聚合
    agg = df.groupby(["date", "product"], as_index=False).agg(
        avg_price=("avg_price", "mean"),
        low_price=("low_price", "min"),
        high_price=("high_price", "max"),
        n_records=("avg_price", "size"),
    )
    origins = (df.groupby(["date", "product"])["origin"]
                 .apply(lambda s: "/".join(sorted({o for o in s if o})))
                 .reset_index(name="origin"))
    out = agg.merge(origins, on=["date", "product"])
    out["avg_price"] = out["avg_price"].round(3)
    out = out.sort_values(["product", "date"]).reset_index(drop=True)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="清洗新发地原始数据")
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--veg-raw", type=Path, default=DEFAULT_VEG_RAW)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.raw.exists():
        print(f"原始文件不存在：{args.raw}，请先运行 fetch_xinfadi_data.py", file=sys.stderr)
        return 1

    rows = read_raw(args.raw)
    print(f"原始记录：{len(rows)} 条")

    veg_rows = [r for r in rows if r.get("prodCat") == "蔬菜"]
    args.veg_raw.parent.mkdir(parents=True, exist_ok=True)
    with args.veg_raw.open("w", encoding="utf-8") as f:
        for r in veg_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"蔬菜原始子集：{len(veg_rows)} 条 -> {args.veg_raw}")

    df = clean(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"清洗后数据集：{len(df)} 行 -> {args.out}")

    # 诊断信息
    print("\n日期范围：", df["date"].min().date(), "->", df["date"].max().date())
    print("品种数：", df["product"].nunique())
    print("日均记录数：", round(df.groupby("date").size().mean(), 1))
    print("价格区间（元/斤）：", df["avg_price"].min(), "-", df["avg_price"].max())
    print("产地缺失比例：", round(df["origin"].eq("").mean() * 100, 1), "%")

    cov = df.groupby("product")["date"].nunique().sort_values(ascending=False)
    print("\n覆盖天数 Top 15：")
    for name, n in cov.head(15).items():
        print(f"  {name}: {n} 天")
    print("\n覆盖天数不足 30 天的品种数：", int((cov < 30).sum()))
    return 0


if __name__ == "__main__":
    sys.exit(main())