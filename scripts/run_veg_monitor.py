#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""蔬菜批发价格波动检测与异常告警（真实数据版）。

数据源：北京新发地批发市场公开行情（来源与清洗见 docs/DATA_SOURCES.md）
流程：读取日度数据 -> 计算环比/滚动统计 -> 异常判定 -> 输出 CSV / HTML 报告 /
      业务摘要 -> 可选推送告警（钉钉 / 企业微信机器人 Webhook）。

用法示例：
  python scripts/run_veg_monitor.py --generate-report
  python scripts/run_veg_monitor.py --generate-report --notify-dry-run
  python scripts/run_veg_monitor.py --since 2026-07-01 --notify-url "<webhook>"
"""

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无界面环境
import matplotlib.pyplot as plt
import matplotlib as mpl
import numpy as np
import pandas as pd

mpl.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC"]
mpl.rcParams["axes.unicode_minus"] = False

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data" / "veg_wholesale_xinfadi.csv"
DEFAULT_OUT = ROOT / "outputs"

# 检测参数（可调）
WINDOW = 30                # 滚动均值/标准差窗口（天）
MIN_PERIODS = 15           # 滚动统计最少天数
VOL_WINDOW = 14            # 波动率滚动窗口（天）
VOL_MIN_PERIODS = 7
Z_THRESHOLD = 3.0          # 价格异常 z-score 阈值
SUSTAINED_Z = 2.0          # 持续高位 z-score 阈值
SUSTAINED_DAYS = 3         # 持续天数
SURGE_PCT = 0.15           # 单日急涨阈值（+15%）
CRASH_PCT = -0.12          # 单日急跌阈值（-12%）
ORIGIN_PCT = 0.05          # 产地切换伴随价格异动的价格变化阈值


def setup_logging(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_file = out_dir / "veg_monitor.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def load_data(path: Path) -> pd.DataFrame:
    """读取日度数据集并做基础校验。"""
    df = pd.read_csv(path, parse_dates=["date"])
    required = {"date", "product", "avg_price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"数据缺少必要列：{missing}")
    if df["avg_price"].isna().any() or (df["avg_price"] <= 0).any():
        raise ValueError("数据中存在缺失或非正的 avg_price，请先运行清洗脚本")
    df = df.sort_values(["product", "date"]).reset_index(drop=True)
    logging.info("加载数据：%d 行，%d 个品种，%s ~ %s",
                 len(df), df["product"].nunique(), df["date"].min().date(), df["date"].max().date())
    return df


def compute_features(daily: pd.DataFrame) -> pd.DataFrame:
    """按品种计算环比、滚动统计与产地切换标记。"""
    out = daily.sort_values(["product", "date"]).copy()
    grp = out.groupby("product", sort=False)
    out["price_pct_ch"] = grp["avg_price"].pct_change()
    out["rolling_mean"] = grp["avg_price"].transform(
        lambda s: s.rolling(WINDOW, min_periods=MIN_PERIODS).mean())
    out["rolling_std"] = grp["avg_price"].transform(
        lambda s: s.rolling(WINDOW, min_periods=MIN_PERIODS).std())
    out["rolling_z"] = (out["avg_price"] - out["rolling_mean"]) / out["rolling_std"].replace({0: np.nan})
    out["rolling_vol"] = grp["price_pct_ch"].transform(
        lambda s: s.rolling(VOL_WINDOW, min_periods=VOL_MIN_PERIODS).std())
    out["origin_prev"] = grp["origin"].shift(1)
    out["origin_changed"] = (
        (out["origin"] != out["origin_prev"])
        & out["origin"].ne("")
        & out["origin_prev"].ne("")
    )
    return out.reset_index(drop=True)


def _sustained_flags(cond: pd.Series, min_days: int) -> pd.Series:
    """把连续 True 达到 min_days 的位置标记为 True。"""
    run = 0
    flags = []
    for x in cond.fillna(False):
        run = run + 1 if x else 0
        flags.append(run >= min_days)
    return pd.Series(flags, index=cond.index)


def classify_anomalies(v: pd.DataFrame,
                       z_threshold: float = Z_THRESHOLD,
                       surge_pct: float = SURGE_PCT,
                       crash_pct: float = CRASH_PCT,
                       origin_pct: float = ORIGIN_PCT) -> pd.DataFrame:
    """异常判定：价格异常 / 急涨 / 急跌 / 产地切换异动 / 持续高位。"""
    v = v.copy()
    v["price_anom"] = v["rolling_z"].abs() > z_threshold
    v["surge"] = v["price_pct_ch"] >= surge_pct
    v["crash"] = v["price_pct_ch"] <= crash_pct
    v["origin_shift_alert"] = v["origin_changed"] & (v["price_pct_ch"].abs() >= origin_pct)
    v["sustained_high"] = (
        v.groupby("product")["rolling_z"]
         .apply(lambda s: _sustained_flags(s.gt(SUSTAINED_Z), SUSTAINED_DAYS))
         .reset_index(level=0, drop=True)
    )
    v["anomaly_any"] = v[["price_anom", "surge", "crash", "origin_shift_alert", "sustained_high"]].any(axis=1)
    return v


def build_summary(v: pd.DataFrame) -> pd.DataFrame:
    """按品种汇总波动与异常情况。"""
    latest = (v.sort_values("date").groupby("product").tail(1)
                [["product", "avg_price", "rolling_z", "origin"]]
                .rename(columns={"avg_price": "latest_price",
                                 "rolling_z": "latest_z",
                                 "origin": "latest_origin"}))
    s = v.groupby("product").agg(
        n_days=("date", "count"),
        mean_price=("avg_price", "mean"),
        std_price=("avg_price", "std"),
        avg_rolling_vol=("rolling_vol", "mean"),
        max_abs_z=("rolling_z", lambda s: s.abs().max()),
        n_price_anom=("price_anom", "sum"),
        n_surge=("surge", "sum"),
        n_crash=("crash", "sum"),
        n_origin_shift=("origin_shift_alert", "sum"),
        n_sustained=("sustained_high", "sum"),
        n_anomalies=("anomaly_any", "sum"),
    ).reset_index()

    s["anomaly_rate"] = (s["n_anomalies"] / s["n_days"]).round(4)
    s["risk_score"] = (
        s["avg_rolling_vol"] * 10 + s["n_price_anom"] * 3 + s["n_surge"] + s["n_crash"]
        + s["n_origin_shift"] * 2 + s["n_sustained"] * 2
    ).round(2)
    return s.sort_values("risk_score", ascending=False).reset_index(drop=True)


def save_outputs(v: pd.DataFrame, summary: pd.DataFrame, out_dir: Path, since=None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    v.to_csv(out_dir / "veg_volatility_full.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(out_dir / "veg_summary.csv", index=False, encoding="utf-8-sig")

    if since is not None:
        v = v[v["date"] >= pd.Timestamp(since)]

    def dump(flag, name, sort_col=None):
        sub = v[v[flag]]
        if sort_col:
            sub = sub.sort_values(sort_col, key=lambda s: s.abs(), ascending=False)
        sub.to_csv(out_dir / name, index=False, encoding="utf-8-sig")
        logging.info("输出 %s：%d 条", name, len(sub))

    dump("price_anom", "veg_anomalies_price.csv")
    dump("surge", "veg_anomalies_surge.csv", "price_pct_ch")
    dump("crash", "veg_anomalies_crash.csv", "price_pct_ch")
    dump("origin_shift_alert", "veg_anomalies_origin_shift.csv", "price_pct_ch")
    dump("sustained_high", "veg_anomalies_sustained.csv", "rolling_z")
    dump("anomaly_any", "veg_anomalies_all.csv", "rolling_z")


def write_business_summary(v: pd.DataFrame, summary: pd.DataFrame, out_dir: Path, since=None) -> Path:
    """自动生成业务摘要（Markdown），便于直接阅读与汇报。"""
    if since is not None:
        v_recent = v[v["date"] >= pd.Timestamp(since)]
    else:
        v_recent = v

    top_vol = summary.head(10)
    top_anom = summary.sort_values("n_anomalies", ascending=False).head(10)
    monthly = (v_recent.groupby(v_recent["date"].dt.to_period("M"))
                .agg(n_anomalies=("anomaly_any", "sum"),
                     n_surge=("surge", "sum"),
                     n_crash=("crash", "sum"))
                .reset_index())
    monthly["month"] = monthly["date"].astype(str)

    recent = v_recent[v_recent["anomaly_any"]].sort_values("date", ascending=False).head(20)

    lines = [
        "# 蔬菜批发价格波动监测——业务摘要",
        "",
        f"生成时间：{pd.Timestamp.now():%Y-%m-%d %H:%M}",
        "",
        "## 一、监测概况",
        f"- 监测范围：北京新发地批发市场，{v['date'].min().date()} ~ {v['date'].max().date()}",
        f"- 覆盖品种：{v['product'].nunique()} 个；日度记录：{v['date'].nunique()} 天",
        f"- 异常事件总数：{int(v_recent['anomaly_any'].sum())} 条（"
        f"价格异常 {int(v_recent['price_anom'].sum())}、急涨 {int(v_recent['surge'].sum())}、"
        f"急跌 {int(v_recent['crash'].sum())}、产地切换异动 {int(v_recent['origin_shift_alert'].sum())}、"
        f"持续高位 {int(v_recent['sustained_high'].sum())}）",
        "",
        "## 二、高波动品种 Top 10（风险评分）",
        "",
        "| 品种 | 平均价格(元/斤) | 平均日波动率 | 异常次数 | 风险评分 |",
        "|---|---|---|---|---|",
    ]
    for _, r in top_vol.iterrows():
        lines.append(f"| {r['product']} | {r['mean_price']:.2f} | {r['avg_rolling_vol']:.3f} | {int(r['n_anomalies'])} | {r['risk_score']} |")

    lines += ["", "## 三、月度异常分布", "", "| 月份 | 异常总数 | 急涨 | 急跌 |", "|---|---|---|---|"]
    for _, r in monthly.iterrows():
        lines.append(f"| {r['month']} | {int(r['n_anomalies'])} | {int(r['n_surge'])} | {int(r['n_crash'])} |")

    lines += ["", "## 四、最近异常事件（按日期倒序）", "", "| 日期 | 品种 | 均价(元/斤) | 日环比 | 异常类型 |", "|---|---|---|---|---|"]
    for _, r in recent.iterrows():
        types = [t for t, flag in [("价格异常", r["price_anom"]), ("急涨", r["surge"]),
                                   ("急跌", r["crash"]), ("产地切换", r["origin_shift_alert"]),
                                   ("持续高位", r["sustained_high"])] if flag]
        lines.append(f"| {r['date'].date()} | {r['product']} | {r['avg_price']:.2f} | "
                     f"{r['price_pct_ch'] * 100:.1f}% | {'、'.join(types)} |")

    lines += [
        "",
        "## 五、业务建议（供采购/销售参考）",
        "",
        "1. **高波动品种**：优先锁定产地、签订长约，价格异动时按 1-2 天滞后确认后再调价；",
        "2. **急涨品种**：检查库存与替代品种，避免追高囤货；若为春节、汛期等季节性上涨，按历史同期节奏备货；",
        "3. **急跌品种**：警惕供给过剩，控制进货量，优先去库存；",
        "4. **产地切换异动**：产地切换往往伴随品质与运费变化，需人工复核报价合理性；",
        "5. **持续高位品种**：持续 z>2 超过 3 天说明价格脱离历史中枢，建议启动专项采购评审。",
        "",
        "> 说明：以上建议基于统计规则自动生成，供人工复核参考，不构成交易指令。",
    ]
    path = out_dir / "business_summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logging.info("业务摘要已生成：%s", path)
    return path


def generate_report(v: pd.DataFrame, summary: pd.DataFrame, out_dir: Path, top_k: int = 12) -> Path:
    """生成 HTML 报告与关键图表。"""
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    top = summary.head(top_k)
    generated = []
    for _, r in top.iterrows():
        product = r["product"]
        sel = v[v["product"] == product].sort_values("date")
        if len(sel) < MIN_PERIODS:
            continue
        fname = f"{product}.png"
        plt.figure(figsize=(10, 4))
        plt.plot(sel["date"], sel["avg_price"], label="均价", linewidth=1.2)
        plt.plot(sel["date"], sel["rolling_mean"], label=f"{WINDOW}日滚动均值", linestyle="--", linewidth=1)
        anom = sel[sel["price_anom"]]
        if not anom.empty:
            plt.scatter(anom["date"], anom["avg_price"], color="red", s=22, label="价格异常", zorder=5)
        surge = sel[sel["surge"]]
        if not surge.empty:
            plt.scatter(surge["date"], surge["avg_price"], color="orange", s=14, marker="^", label="急涨", zorder=4)
        crash = sel[sel["crash"]]
        if not crash.empty:
            plt.scatter(crash["date"], crash["avg_price"], color="green", s=14, marker="v", label="急跌", zorder=4)
        plt.title(f"{product}（风险评分 {r['risk_score']}）")
        plt.xlabel("日期")
        plt.ylabel("均价（元/斤）")
        plt.legend(fontsize=8, ncol=4)
        plt.tight_layout()
        plt.savefig(fig_dir / fname, dpi=150)
        plt.close()
        generated.append((product, fname))

    # 月度异常条形图
    monthly = (v.groupby(v["date"].dt.to_period("M"))
                .agg(n_anomalies=("anomaly_any", "sum"),
                     n_surge=("surge", "sum"),
                     n_crash=("crash", "sum"))
                .reset_index())
    monthly["month"] = monthly["date"].astype(str)
    plt.figure(figsize=(10, 4))
    x = np.arange(len(monthly))
    plt.bar(x - 0.2, monthly["n_surge"], width=0.4, label="急涨", color="orange")
    plt.bar(x + 0.2, monthly["n_crash"], width=0.4, label="急跌", color="green")
    plt.xticks(x, monthly["month"], rotation=45)
    plt.title("月度急涨/急跌事件数")
    plt.legend()
    plt.tight_layout()
    plt.savefig(fig_dir / "monthly_anomalies.png", dpi=150)
    plt.close()

    html_path = out_dir / "veg_report_index.html"
    with html_path.open("w", encoding="utf-8") as f:
        f.write("<html><head><meta charset='utf-8'><title>蔬菜价格波动监测报告</title></head><body>\n")
        f.write(f"<h2>蔬菜批发价格波动检测与异常告警</h2>\n")
        f.write(f"<p>生成时间：{pd.Timestamp.now():%Y-%m-%d %H:%M} ｜ "
                f"数据范围：{v['date'].min().date()} ~ {v['date'].max().date()}</p>\n")
        f.write("<h3>高波动品种（风险评分 Top 12）</h3>\n<table border='1' cellpadding='4'>"
                "<tr><th>品种</th><th>均价(元/斤)</th><th>平均日波动率</th><th>异常次数</th><th>风险评分</th></tr>\n")
        for _, r in top.iterrows():
            f.write(f"<tr><td>{r['product']}</td><td>{r['mean_price']:.2f}</td>"
                    f"<td>{r['avg_rolling_vol']:.3f}</td><td>{int(r['n_anomalies'])}</td>"
                    f"<td>{r['risk_score']}</td></tr>\n")
        f.write("</table>\n")
        f.write("<h3>月度异常分布</h3>\n<img src='figures/monthly_anomalies.png' style='max-width:820px;border:1px solid #ccc;'><br/>\n")
        f.write("<h3>重点品种走势</h3>\n")
        for product, fname in generated:
            f.write(f"<h4>{product}</h4>\n<img src='figures/{fname}' style='max-width:820px;border:1px solid #ccc;'><br/>\n")
        f.write("</body></html>\n")
    logging.info("HTML 报告已生成：%s", html_path)
    return html_path


def build_alert_message(v: pd.DataFrame, top_n: int = 8) -> str:
    """构造告警消息（文本，兼容钉钉/企业微信机器人）。"""
    recent = v[v["anomaly_any"]].sort_values("date", ascending=False).head(top_n)
    lines = [
        f"【蔬菜价格波动告警】{pd.Timestamp.now():%m-%d %H:%M}",
        f"监测范围：{v['date'].min().date()} ~ {v['date'].max().date()}",
        f"最近 {top_n} 条异常（按日期倒序）：",
    ]
    for _, r in recent.iterrows():
        types = [t for t, flag in [("价格异常", r["price_anom"]), ("急涨", r["surge"]),
                                   ("急跌", r["crash"]), ("产地切换", r["origin_shift_alert"]),
                                   ("持续高位", r["sustained_high"])] if flag]
        lines.append(
            f"- {r['date'].date()} {r['product']}：{r['avg_price']:.2f} 元/斤，"
            f"环比 {r['price_pct_ch'] * 100:+.1f}%（{'、'.join(types)}）"
        )
    lines.append("详见 outputs/business_summary.md")
    return "\n".join(lines)


def send_alert(message: str, webhook_url: str = None, dry_run: bool = False) -> bool:
    """推送告警到 Webhook（钉钉/企业微信文本消息通用格式）。"""
    import os
    url = webhook_url or os.environ.get("DINGTALK_WEBHOOK") or os.environ.get("WECHAT_WORK_WEBHOOK")
    if dry_run:
        logging.info("[告警预览] 未实际发送。\n%s", message)
        return True
    if not url:
        logging.warning("未配置 Webhook（--notify-url 或环境变量 DINGTALK_WEBHOOK / WECHAT_WORK_WEBHOOK），跳过推送")
        return False
    payload = json.dumps({"msgtype": "text", "text": {"content": message}}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
        logging.info("告警推送完成：%s", body[:200])
        return True
    except Exception as exc:  # noqa: BLE001
        logging.error("告警推送失败：%s", exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="蔬菜批发价格波动检测与异常告警")
    parser.add_argument("--data-path", type=Path, default=DEFAULT_DATA, help="输入数据 CSV")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="输出目录")
    parser.add_argument("--generate-report", action="store_true", help="生成 HTML 报告与图表")
    parser.add_argument("--top-k", type=int, default=12, help="报告重点品种数量")
    parser.add_argument("--z-threshold", type=float, default=Z_THRESHOLD, help="价格异常 z 阈值")
    parser.add_argument("--surge-pct", type=float, default=SURGE_PCT, help="急涨阈值")
    parser.add_argument("--crash-pct", type=float, default=CRASH_PCT, help="急跌阈值")
    parser.add_argument("--since", default=None, help="只输出该日期（YYYY-MM-DD）之后的异常（用于每日增量告警）")
    parser.add_argument("--notify-url", default=None, help="钉钉/企业微信 Webhook URL")
    parser.add_argument("--notify-dry-run", action="store_true", help="仅预览告警消息，不实际推送")
    args = parser.parse_args()

    log_file = setup_logging(args.out_dir)
    try:
        logging.info("启动监测脚本")
        df = load_data(args.data_path)
        v = compute_features(df)
        v = classify_anomalies(v, z_threshold=args.z_threshold,
                               surge_pct=args.surge_pct, crash_pct=args.crash_pct)
        summary = build_summary(v)
        save_outputs(v, summary, args.out_dir, since=args.since)
        write_business_summary(v, summary, args.out_dir, since=args.since)

        if args.generate_report:
            generate_report(v, summary, args.out_dir, top_k=args.top_k)

        if v["anomaly_any"].sum() > 0:
            message = build_alert_message(v)
            if args.notify_dry_run:
                send_alert(message, dry_run=True)
            else:
                send_alert(message, webhook_url=args.notify_url)
        else:
            logging.info("本期无异常事件")

        logging.info("全部完成，日志：%s", log_file)
        return 0
    except Exception as exc:  # noqa: BLE001
        logging.error("运行失败：%s", exc)
        raise


if __name__ == "__main__":
    sys.exit(main())