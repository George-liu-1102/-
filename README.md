# 蔬菜批发价格波动与异常检测（Veg Wholesale Anomaly Monitor）

项目简介
---------
本项目面向蔬菜批发价格的波动检测与异常告警，目标是把日度 city+vegetable 级别数据进行聚合，基于滚动统计（rolling mean/std）与比例变化规则，自动识别：
- 价格异常（rolling z-score）；
- 供给风险候选（价格显著上升且成交量下降）；
- 需求激增候选（价格显著上升且成交量上升）。

项目产出
---------
- 数据：data/veg_wholesale_sample.csv（示例）
- 自动化脚本：scripts/run_veg_monitor.py（可用于定时任务）
- 报表与图表：outputs/veg_report_index.html + outputs/figures/*.png
- 异常/审核表：outputs/*.csv

主方法概述
------------
1. 日度聚合：对原始交易行按 date/city/vegetable 聚合得到 avg_price、total_volume。
2. 滚动统计：对每个 city+vegetable 使用 30 天窗口计算 rolling_mean 和 rolling_std；使用 14 天窗口计算 price_pct_ch 的 rolling_std（作为 price volatility 辅助指标）。
3. 异常判定：
   - rolling_z = (avg_price - rolling_mean) / rolling_std；
   - 价格异常：|rolling_z| > price_z_threshold；
   - 供给风险：rolling_z > price_z_threshold 且 vol_pct_ch < vol_drop_threshold；
   - 需求激增：rolling_z > price_z_threshold 且 vol_pct_ch > vol_rise_threshold。

快速开始（本地复现）
-------------------
1. 克隆仓库并进入：
   git clone <repo-url> && cd <repo>

2. 建立虚拟环境并安装依赖：
   python -m venv .venv
   source .venv/bin/activate   # macOS / Linux
   .venv\Scripts\activate      # Windows
   pip install -r requirements.txt

3. 生成样本数据（ 仓库已提供： data/veg_wholesale_sample.csv ）：
   python scripts/generate_sample_data.py --base-dir "<path_to_project>"

4. 运行完整监控脚本并生成报告：
   python scripts/run_veg_monitor.py --base-dir "<path_to_project>" --generate-report

5. 打开报告：
   open outputs/veg_report_index.html   # 或直接在浏览器中打开

默认参数
----------
- price_z_threshold: 3.0
- vol_drop_threshold: -0.20
- vol_rise_threshold: 0.20


