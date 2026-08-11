#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下载北京新发地批发市场公开行情数据（真实数据源）。

数据来源：新发地官网"价格行情"页面 https://www.xinfadi.com.cn/priceDetail.html
接口：POST https://www.xinfadi.com.cn/getPriceData.html（application/x-www-form-urlencoded）
说明：公开接口按 limit + current 分页返回，日期为闭区间；
     本脚本按日期窗口拉取原始 JSONL（断点续传、失败重试），供清洗脚本使用。
"""

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

API_URL = "http://www.xinfadi.com.cn/getPriceData.html"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def fetch_page(limit: int, current: int, start: str, end: str, timeout: int = 60) -> dict:
    """POST 拉取一页数据，失败自动重试 3 次。"""
    body = urllib.parse.urlencode({
        "limit": limit,
        "current": current,
        "pubDateStartTime": start,
        "pubDateEndTime": end,
        "prodPcatid": "",
        "prodCatid": "",
        "prodName": "",
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("User-Agent", UA)
    req.add_header("Referer", "https://www.xinfadi.com.cn/priceDetail.html")

    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 网络错误类型多样，统一重试
            last_err = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"请求失败（重试 3 次后放弃）：{last_err}")


def main() -> int:
    parser = argparse.ArgumentParser(description="下载新发地公开行情数据")
    parser.add_argument("--start", default="2026/01/01", help="开始日期 yyyy/MM/dd")
    parser.add_argument("--end", default=date.today().strftime("%Y/%m/%d"), help="结束日期 yyyy/MM/dd")
    parser.add_argument("--limit", type=int, default=1000, help="单页条数")
    parser.add_argument("--sleep", type=float, default=0.4, help="请求间隔（秒）")
    parser.add_argument("--out", type=Path, default=Path("data/raw/xinfadi_raw.jsonl"))
    args = parser.parse_args()

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)

    # 断点续传：按记录 id 去重
    existing_ids: set = set()
    if out.exists():
        with out.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    existing_ids.add(json.loads(line).get("id"))
                except json.JSONDecodeError:
                    continue
        print(f"发现已下载 {len(existing_ids)} 条，将跳过重复记录", flush=True)

    first = fetch_page(args.limit, 1, args.start, args.end)
    total = int(first.get("count") or 0)
    if total <= 0:
        print("接口未返回数据，请检查日期范围", file=sys.stderr)
        return 1
    pages = (total + args.limit - 1) // args.limit
    print(f"时间范围 {args.start} ~ {args.end}，共 {total} 条，分 {pages} 页下载", flush=True)

    saved = 0
    with out.open("a", encoding="utf-8") as f:
        for page in range(1, pages + 1):
            resp = fetch_page(args.limit, page, args.start, args.end)
            rows = resp.get("list") or []
            new_rows = [r for r in rows if r.get("id") not in existing_ids]
            for r in new_rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            existing_ids.update(r.get("id") for r in new_rows)
            saved += len(new_rows)
            print(f"第 {page}/{pages} 页：返回 {len(rows)}，新增 {len(new_rows)}，累计 {saved}", flush=True)
            time.sleep(args.sleep)

    print(f"完成：新增 {saved} 条，原始数据已保存至 {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())