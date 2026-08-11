# 数据来源与清洗说明

## 一、数据来源

本项目使用**真实公开数据**：北京新发地农产品批发市场官网"价格行情"页面
（<https://www.xinfadi.com.cn/priceDetail.html>）的公开行情接口
（`POST https://www.xinfadi.com.cn/getPriceData.html`）。

- 接口以 `limit + current` 分页返回，日期区间为闭区间，需以
  `application/x-www-form-urlencoded` 表单格式提交（JSON 格式不生效，已在脚本中验证）；
- 单页上限约 1000~2000 条，脚本默认每页 1000 条；
- 本次抓取窗口：**2026-01-01 ~ 2026-08-11**，共 103 页、**102,717 条**全品类记录；
- 其中**蔬菜类（prodCat="蔬菜"）27,415 条**，作为本项目分析对象。

## 二、抓取与复现

```bash
# 1. 拉取原始数据（含全品类，约 100KB/页，保存为 JSONL）
python scripts/fetch_xinfadi_data.py --start 2026/01/01 --end 2026/08/11

# 2. 清洗并生成分析数据集
python scripts/build_xinfadi_dataset.py
```

- 原始全量数据：`data/raw/xinfadi_raw.jsonl`（体积较大，已加入 `.gitignore`）；
- 蔬菜原始子集（溯源用）：`data/raw/xinfadi_vegetables_raw.jsonl`；
- 清洗后分析数据：`data/veg_wholesale_xinfadi.csv`。

## 三、清洗规则

| 规则 | 说明 |
|---|---|
| 品类过滤 | 仅保留 `prodCat == "蔬菜"` |
| 单位过滤 | 仅保留 `unitInfo == "斤"`（约 63 条"袋"计蔬菜因单位不可比被剔除） |
| 价格字段 | `avgPrice` 转 `float`；缺失的 `low/high` 用均价兜底；均价 `<= 0` 的记录剔除 |
| 日度聚合 | 同一 `(date, product)` 的多个规格/产地记录合并：均价取均值、最低/最高价取极值 |
| 产地处理 | 多产地以 `/` 连接并去重（如 `冀/辽`）；约 20.8% 的记录无产地信息 |

## 四、最终数据集字段

文件：`data/veg_wholesale_xinfadi.csv`（22,362 行，145 个品种，216 天）

| 字段 | 类型 | 说明 |
|---|---|---|
| date | date | 行情日期（2026-01-01 ~ 2026-08-11） |
| product | str | 品种名（如 大白菜、黄瓜、鸡毛菜） |
| avg_price | float | 当日均价（元/斤） |
| low_price | float | 当日最低价（元/斤） |
| high_price | float | 当日最高价（元/斤） |
| origin | str | 产地（省级简称，可为多产地） |
| n_records | int | 聚合前当日记录条数 |

## 五、数据局限（已知）

1. **无成交量**：新发地公开接口不提供成交量/成交额，因此本项目不做量价联动分析，
   供给风险/需求激增类规则不适用（原合成数据版规则已弃用）；
2. **单市场**：仅覆盖北京新发地一个批发市场，结论外推需谨慎；
3. **产地为省级简称**：部分记录为多产地（如 `冀鲁`），粒度较粗；
4. **品种覆盖不均**：145 个品种中约 38 个覆盖天数不足 30 天（如季节性强、上架晚的品种），
   这些品种的滚动 z-score 不可用，异常判定主要依赖单日环比规则；
5. **接口限制**：日期过滤需表单编码、每页最多约 2000 条，抓取时需控制频率（脚本默认 0.4s 间隔）。

## 六、数据更新

数据按日更新，重新运行 `fetch_xinfadi_data.py`（断点续传，按记录 id 去重）即可增量拉取，
再运行 `build_xinfadi_dataset.py` 更新分析数据集。