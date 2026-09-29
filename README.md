# A股分析终端

本地 Web 版股票分析软件：全量/增量数据更新、自选池、手动/自动选股、插件化交易策略、回测报告与 Excel 导出。

技术栈：Python 3.10+ / FastAPI / SQLite / pandas + React / TypeScript / Vite / ECharts。
数据源：东方财富行情接口（优先），腾讯财经接口（自动降级备用）；日线 K 线为**后复权**口径。

## 启动 / 停止

```bash
# 1. 后端依赖
pip install -r backend/requirements.txt

# 2. 前端（已构建过可跳过；改动源码后重新执行）
cd frontend && npm install && npm run build && cd ..

# 3. 启动（或直接双击 start.bat）
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8610
```

浏览器打开 http://127.0.0.1:8610

停止服务：双击 stop.bat（按端口 8610 结束服务进程及其子进程）。

## 六大功能

| 模块 | 页面 | 说明 |
|---|---|---|
| 基础数据 | 数据中心 | 「全量更新」下载全部 A 股上市以来日线 + 基本面 + 12 期财报（耗时数小时，可后台挂机）；「增量更新」刷新股票列表与财报，并把本地已有股票的日线补齐到最新交易日（新股票需先跑全量） |
| 股票自选池 | 股票自选池 | 默认 3 个池，可增删改名；池内股票可增删；点击股票进入个股详情页（K线/指标/财务） |
| 手动选股 | 手动选股 | 基本面（行业/市值/PE/PB/ROE/增速…）与技术面（MACD金叉、布林突破、多头排列、放量…）条件自由组合，须同时满足；结果支持单选/多选/全选加入自选池 |
| 自动选股 | 自动选股 | 5 个条件框，权重之和必须为 100；框内条件按满足比例计分，加权总分排序取前 100 |
| 交易策略 | 交易策略 | 内置 MACD+MA、双均线、海龟交易法、布林突破、KDJ 五个插件策略；插件放在 `backend/app/strategies/`，新增文件即自动发现；可勾选启用项 |
| 回测 | 回测 | 股票代码 + 时间范围（默认10年）+ 资金（默认100万）+ 手续费（默认0.0003）+ 已启用策略下拉；生成个股K线（标注买卖点）、大盘指数、行业指数三条曲线，汇总耗时/笔数/扣费盈亏/大盘与行业涨跌，交易明细可导出 Excel |

## 数据说明

- K线为后复权价格：历史K线不会因除权除息重算、长分红股早年也不会出现负价，适合指标计算与回测。
- 行业分类与行业K线来自所选数据源的行业板块；本地行业指数由板块成分股日线等权合成。
- 样本演示数据（13 只知名股票）可由 `python backend/scripts/seed_sample.py` 重建；正式使用请在「数据中心」执行全量更新。
- 全量更新可续传：已持有深度历史的股票只走一次窗口补齐，任务中断或个别股票失败后重跑不会重复下载。
- 数据源限速：腾讯行情接口每请求最多返回 640 根K线，请求过快会被其 WAF 拒绝（HTTP 501）。全量更新按全局最小间隔串行放行，并在被拒时指数退避（60s 起、最长 900s）。可用环境变量 `STOCKCN_HTTP_INTERVAL` 调整间隔秒数（默认 0.4，夜间长时间挂机建议 1.0，例如 `set STOCKCN_HTTP_INTERVAL=1.0` 后再运行 start.bat）。

## 目录结构

```
backend/app/            FastAPI 应用
  datasource/           eastmoney.py / tencent.py / updater.py(全量+增量任务)
  indicators.py         MA/MACD/BOLL/RSI/KDJ 指标库
  screening.py          手动/自动选股引擎（结果缓存5分钟）
  strategies/           策略插件目录（base.py 定义插件接口, builtin.py 内置策略）
  backtest.py           回测引擎 + Excel 导出
frontend/               React+Vite 源码；dist/ 构建产物由后端直接托管
data/                   SQLite 数据库（首次运行自动创建）
.tools/node             便携版 Node（仅用于构建前端，可删除）
```

## 编写自己的策略插件

在 `backend/app/strategies/` 新建 `my_strategy.py`：

```python
from .base import Strategy

class MyStrategy(Strategy):
    id = "my_strategy"
    name = "我的策略"
    description = "连续3日收盘价上涨则买入，跌破MA20卖出"
    params_schema = [{"key": "n", "label": "连涨天数", "type": "number", "default": 3}]

    def generate_signals(self, df, params):
        # df: date/open/high/low/close/volume 升序
        signals = []
        ...
        return signals  # [{"date": "...", "side": "buy"|"sell", "reason": "..."}]
```

重启服务后自动出现在策略列表与回测下拉框中。
