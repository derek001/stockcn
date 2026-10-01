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
-- 复权价 = 价格 × adj_factor；pct_chg = 交易所口径涨跌幅（除权日按取整后的参考价计算）
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
