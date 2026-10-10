PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stocks (
  code TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  market TEXT NOT NULL,            -- SH / SZ / BJ
  secid TEXT NOT NULL,             -- eastmoney secid e.g. 1.600000
  industry TEXT,                   -- eastmoney industry name (f100)
  board_code TEXT,                 -- eastmoney industry board code e.g. BK0735
  list_date TEXT,                  -- YYYY-MM-DD
  is_active INTEGER DEFAULT 1
);

-- 日线：价格存**不复权**（交易所口径，与行情软件一致），复权靠 adj_factor 现算
-- adj_factor = QMT「等比后复权 ÷ 不复权」推导的累计复权因子（上市首日 = 1）
-- 等比那份坏掉（价非正等）导致当天解不出因子时，adj_factor 留 NULL：不复权价与量额照常如实入库，
-- 等 QMT 重导后再做一次增量导入即自动补齐（NULL 只表示「复权口径无从推算」，不是缺数据）
-- 复权价 = 价格 × adj_factor；pct_chg = 交易所口径涨跌幅（除权日按取整后的参考价计算，跨 NULL 段时留 NULL）
CREATE TABLE IF NOT EXISTS kline_daily (
  code TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,   -- 不复权
  adj_factor REAL,                              -- 等比累计复权因子
  volume REAL,                      -- shares
  amount REAL,                      -- yuan
  pct_chg REAL,                     -- %
  turnover REAL,                    -- %
  PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_kline_date ON kline_daily(date);

-- latest fundamental snapshot from market list API
CREATE TABLE IF NOT EXISTS fundamentals (
  code TEXT PRIMARY KEY,
  trade_date TEXT,
  price REAL,
  pct_chg REAL,
  total_mv REAL,                    -- yuan
  float_mv REAL,                    -- yuan
  pe_dynamic REAL,
  pe_ttm REAL,
  pb REAL,
  turnover_rate REAL,
  amount REAL                       -- yuan, day
);

-- quarterly financial reports
CREATE TABLE IF NOT EXISTS fin_report (
  code TEXT NOT NULL,
  report_date TEXT NOT NULL,        -- YYYY-MM-DD quarter end
  eps REAL,
  revenue REAL,                     -- yuan
  revenue_yoy REAL,                 -- %
  net_profit REAL,                  -- yuan
  net_profit_yoy REAL,              -- %
  roe_weighted REAL,                -- %
  gross_margin REAL,                -- %
  PRIMARY KEY (code, report_date)
);

CREATE TABLE IF NOT EXISTS boards (
  board_code TEXT PRIMARY KEY,
  board_name TEXT NOT NULL,
  board_type TEXT NOT NULL          -- industry
);

CREATE TABLE IF NOT EXISTS board_kline (
  board_code TEXT NOT NULL,
  date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL, pct_chg REAL,
  PRIMARY KEY (board_code, date)
);

CREATE TABLE IF NOT EXISTS index_kline (
  index_code TEXT NOT NULL,         -- e.g. sh000001
  name TEXT,
  date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL,
  volume REAL, amount REAL, pct_chg REAL,
  PRIMARY KEY (index_code, date)
);

CREATE TABLE IF NOT EXISTS pools (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS pool_items (
  pool_id INTEGER NOT NULL,
  code TEXT NOT NULL,
  added_at TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (pool_id, code)
);

CREATE TABLE IF NOT EXISTS kv_store (
  key TEXT PRIMARY KEY,
  value TEXT
);

-- 策略选股：每次运行的命中留痕。同一天重跑同一个选股器会覆盖，params 存本次生效参数便于复现
CREATE TABLE IF NOT EXISTS select_result (
  trade_date TEXT NOT NULL,         -- 数据基准日（这批K线的末根日期）
  selector_id TEXT NOT NULL,
  code TEXT NOT NULL,
  name TEXT,
  score REAL,                       -- 0..100，只用于排序，不代表概率
  reason TEXT,
  attrs TEXT,                       -- JSON：命中当时的展示字段（价格/市值/PE/ROE…），回看历史不会被今日数据覆盖
  params TEXT,                      -- JSON
  run_at TEXT NOT NULL,
  PRIMARY KEY (trade_date, selector_id, code)
);
CREATE INDEX IF NOT EXISTS idx_select_selector ON select_result(selector_id, trade_date);

-- 全市场回测：一次跑批一个批次头，summary 放分布指标、按年分解和全市场等权净值曲线
CREATE TABLE IF NOT EXISTS bt_batch (
  id TEXT PRIMARY KEY,              -- 批次号：策略_起止_时间戳
  run_at TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  strategy_name TEXT,
  params TEXT,                      -- JSON：本次生效参数
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  capital REAL NOT NULL,
  fee_rate REAL NOT NULL,
  stock_count INTEGER NOT NULL,     -- 实际参与只数（区间内K线>=30 根）
  skipped INTEGER NOT NULL,         -- 因K线不足被跳过的只数
  elapsed_sec REAL,
  summary TEXT                      -- JSON
);
CREATE INDEX IF NOT EXISTS idx_bt_batch_run ON bt_batch(run_at);

-- 逐只成绩单：同一批次重跑整批覆盖；excess = 策略收益 - 同期买入持有收益
CREATE TABLE IF NOT EXISTS bt_batch_item (
  batch_id TEXT NOT NULL,
  code TEXT NOT NULL,
  name TEXT,
  industry TEXT,
  bars INTEGER,
  buy_count INTEGER,
  order_count INTEGER,
  missed INTEGER,                   -- 信号来了但买不起一手的次数
  pnl_pct REAL,
  bh_pct REAL,
  excess_pct REAL,
  max_dd_pct REAL,
  fees REAL,
  years TEXT,                       -- JSON：{"2021": 收益%, ...} 该只每个自然年的策略收益
  PRIMARY KEY (batch_id, code)
);
CREATE INDEX IF NOT EXISTS idx_bt_item_pnl ON bt_batch_item(batch_id, pnl_pct);
