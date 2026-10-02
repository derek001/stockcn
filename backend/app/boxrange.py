"""长箱体（box-range）统计：从日线算 3 年箱体的顶/底、高度、年化斜率、穿越次数、带内占比、挖坑。

口径（2026-10-03 与用户确认，四条）：
- 计算一律用等比后复权价 `close * adj_factor`。下面产出的比值/百分比类字段与复权口径无关；
  箱顶/箱底为了和页面上的「最新价」对齐，换算回今日价坐标（整段 ÷ 该股最新复权因子）。
- 箱体上下沿 = **覆盖 dense_pct% 交易日收盘的比值最窄区间**（密集区），不是区间极值。
  「最窄」按 ln 距离（= 顶/底比值）量，与 B1 的高度定义 顶/底-1 同一个目标函数；按绝对价差量会选出
  明显偏低的另一条带（603969：比值口径高 105%，价差口径高 132%），不可混用。
  极值只用于两件事：「现价已脱离底部」和「近 60 日挖坑跌破箱底」。
  注意由此推出的一个后果：带内占比 `box_time_pct` 恒 ≥ dense_pct（分箱宽度只会让它略高），
  所以它不能当硬过滤条件用（实测 dense=80 时全市场 5570 只全部 ≥80%），只作为展示/排序项。
- 年化斜率 = 对 ln(复权收盘) 关于**全市场交易日序号**做最小二乘，再 (exp(slope*250)-1) 年化。
  用全市场日历序号而不是「该股自身第几根 bar」，是为了让停牌缺口体现在时间轴上；
  实测两者对无停牌个股年化斜率完全相同（中位差 0.0000%），只在停牌股上有差异。
- 「上市满 N 年」用首根日线日期判（`stocks.list_date` 全库 NULL，没有可用来源）。

实现上刻意不走 `screening.load_universe()` 的那条 pandas 路径：750 交易日是 400 万行，
搬进 Python 会把冷启动从 45s 拖到几分钟，所以统计全部在 SQLite 侧聚合，只回传每只一行。
两遍扫描：① 收盘按 ln 做 0.25% 分箱直方图 → Python 滑窗求最窄密集带；
② 密集带写进临时表，一遍带 LAG 的聚合出全部统计量。约 28s（依赖 db.py 的页缓存/临时表内存化）。
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from . import db

# 分箱宽度：ln(价) × 400，即 0.25% 的价格精度。
# 实测（200 只箱体池样本，对「连续价上的比值最窄带」复核）：格宽 1% 时箱底/箱顶中位偏差 1.08%、
# 半数样本 >1%；0.25% 时中位 0.19%、p90 0.97%，_bands 从 10.7s 增到 13.9s。再细只剩并列歧义。
BIN_SCALE = 400
TRADING_DAYS = 250
DEFAULT_LOOK_DAYS = 750
DEFAULT_DENSE_PCT = 80.0

# (基准日, 回看天数, 密集占比) -> {code: 指标}
_cache: dict[tuple, dict[str, dict]] = {}
_CACHE_MAX = 4

_BAND_TABLE = "box_band_tmp"
_DATE_TABLE = "box_date_tmp"

# 密集带：ln(复权收盘) 的 0.25% 分箱直方图，按 (code, 分箱) 聚好后交给 Python 滑窗
_HIST_SQL = (
    "SELECT code, CAST(FLOOR(LN(close * adj_factor) * ?) AS INT), COUNT(*) "
    "FROM kline_daily WHERE date >= ? AND close > 0 AND adj_factor > 0 GROUP BY 1, 2 ORDER BY 1, 2")

# 一遍聚合出箱体全部统计量。x = 全市场交易日序号（_DATE_TABLE）；
# crossings 靠 LAG 比较相邻两根在中线上/下；带内天数靠 _BAND_TABLE 的 [lo, hi]。
_STATS_SQL = f"""
SELECT code,
       COUNT(*) AS bars,
       MIN(c) AS min_close,
       MAX(c) AS max_close,
       SUM(x) AS sx, SUM(ln) AS sl, SUM(x * ln) AS sxl, SUM(x * x) AS sxx,
       SUM(CASE WHEN prev IS NOT NULL AND above <> prev THEN 1 ELSE 0 END) AS crossings,
       SUM(in_band) AS in_band,
       MIN(low_recent) AS low60,
       MAX(last_close) AS last_close
FROM (
  SELECT k.code AS code,
         k.x AS x, k.c AS c, k.ln AS ln,
         CASE WHEN k.c >= (b.lo + b.hi) / 2 THEN 1 ELSE 0 END AS above,
         LAG(CASE WHEN k.c >= (b.lo + b.hi) / 2 THEN 1 ELSE 0 END)
           OVER (PARTITION BY k.code ORDER BY k.date) AS prev,
         CASE WHEN k.c BETWEEN b.lo AND b.hi THEN 1 ELSE 0 END AS in_band,
         CASE WHEN k.date >= ? THEN k.lown END AS low_recent,
         CASE WHEN k.date = ? THEN k.c END AS last_close
  FROM (
    SELECT kd.code AS code, kd.date AS date,
           kd.close * kd.adj_factor AS c,
           LN(kd.close * kd.adj_factor) AS ln,
           kd.low * kd.adj_factor AS lown,
           d.x AS x
    FROM kline_daily kd JOIN {_DATE_TABLE} d ON d.date = kd.date
    WHERE kd.date >= ? AND kd.close > 0 AND kd.adj_factor > 0
  ) k
  JOIN {_BAND_TABLE} b ON b.code = k.code
) GROUP BY code
"""

# 该股最新复权因子 = 全历史末根的 adj_factor。
# 不要用两参 MAX(fac, date)：SQLite 里那是**标量** max，类型序 TEXT > REAL，会返回日期字符串。
_FACTOR_SQL = (
    "SELECT a.*, (SELECT adj_factor FROM kline_daily k "
    "WHERE k.code = a.code ORDER BY date DESC LIMIT 1) AS factor_last FROM (%s) a" % _STATS_SQL)


def latest_bar_date() -> str:
    row = db.query_one("SELECT MAX(date) AS d FROM kline_daily")
    return (row or {}).get("d") or ""


def _nth_last_date(back_days: int) -> str:
    """库里倒数第 back_days 个有行情的日历日（回看窗口起点、近 60 日分界都用它）。"""
    row = db.query_one(
        "SELECT date FROM (SELECT DISTINCT date FROM kline_daily "
        "ORDER BY date DESC LIMIT 1 OFFSET ?)", (back_days - 1,))
    return (row or {}).get("date") or ""


def _dense_band(counts: list[tuple[int, int]], n: int, dense_pct: float) -> tuple[float, float] | None:
    """在 0.25% 宽的对数分箱上滑窗，找覆盖 dense_pct% 交易日的**比值最窄**区间。

    返回 (底, 顶)（后复权价）：底 = 最低那格的左边界，顶 = 最高那格的右边界。
    这样定的带内天数只会 ≥ ceil(n·dense_pct/100)，即 box_time_pct 恒 ≥ dense_pct。
    """
    need = math.ceil(n * dense_pct / 100.0)
    if need <= 0 or need > n or not counts:
        return None
    best: tuple[int, int, int] | None = None
    j, acc = 0, 0
    for i in range(len(counts)):
        while j < len(counts) and acc < need:
            acc += counts[j][1]
            j += 1
        if acc >= need:
            hi_bin = counts[j - 1][0]
            span = hi_bin - counts[i][0]
            if best is None or span < best[0]:
                best = (span, counts[i][0], hi_bin)
        acc -= counts[i][1]
    if best is None:
        return None
    return (math.exp(best[1] / BIN_SCALE), math.exp((best[2] + 1) / BIN_SCALE))


def _bands(start: str, dense_pct: float) -> dict[str, tuple[float, float]]:
    """直方图 → 每只股票的密集区上下沿（后复权价）。"""
    per: dict[str, list[tuple[int, int]]] = defaultdict(list)
    totals: dict[str, int] = defaultdict(int)
    for code, bin_, k in db.query_rows(_HIST_SQL, (BIN_SCALE, start)):
        per[code].append((bin_, k))
        totals[code] += k
    out = {}
    for code, counts in per.items():
        band = _dense_band(counts, totals[code], dense_pct)
        if band:
            out[code] = band
    return out


def _regression_pct_year(bars: int, sx: float, sl: float, sxl: float, sxx: float) -> float | None:
    """ln(复权收盘) 对交易日序号最小二乘，按 250 交易日年化成百分比。"""
    den = bars * sxx - sx * sx
    if bars < 2 or den <= 0:
        return None
    v = (bars * sxl - sx * sl) / den * TRADING_DAYS
    if v > 20:  # exp 溢出（只有十几根 bar 的新股会到这里）
        return None
    return (math.exp(v) - 1) * 100


def _first_dates() -> dict[str, str]:
    return {r["code"]: r["d"] for r in
            db.query("SELECT code, MIN(date) AS d FROM kline_daily GROUP BY code")}


def _years_between(d1: str, d2: str) -> float | None:
    try:
        a = dt.date.fromisoformat(d1)
        b = dt.date.fromisoformat(d2)
    except (TypeError, ValueError):
        return None
    return (b - a).days / 365.25


def compute(look_days: int = DEFAULT_LOOK_DAYS,
            dense_pct: float = DEFAULT_DENSE_PCT) -> dict[str, dict]:
    """全市场箱体指标，返回 {code: {...}}；按 (基准日, 回看天数, 密集占比) 缓存。"""
    base = latest_bar_date()
    if not base:
        return {}
    key = (base, look_days, dense_pct)
    if key in _cache:
        return _cache[key]

    start = _nth_last_date(look_days)
    recent_from = _nth_last_date(60)
    if not start:
        return {}

    bands = _bands(start, dense_pct)
    if not bands:
        return {}

    dates = [r["date"] for r in
             db.query("SELECT DISTINCT date FROM kline_daily WHERE date >= ? ORDER BY date",
                      (start,))]
    conn = db.get_conn()  # 临时表建在这条共享连接上，同一连接才看得见
    db.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_BAND_TABLE} "
               f"(code TEXT PRIMARY KEY, lo REAL, hi REAL)")
    db.execute(f"CREATE TEMP TABLE IF NOT EXISTS {_DATE_TABLE} "
               f"(date TEXT PRIMARY KEY, x REAL)")
    db.execute(f"DELETE FROM {_BAND_TABLE}")
    db.execute(f"DELETE FROM {_DATE_TABLE}")
    db.execute(f"INSERT INTO {_BAND_TABLE}(code, lo, hi) VALUES (?,?,?)",
               many=[(c, lo, hi) for c, (lo, hi) in bands.items()])
    db.execute(f"INSERT INTO {_DATE_TABLE}(date, x) VALUES (?,?)",
               many=[(d, i + 1) for i, d in enumerate(dates)])
    try:
        agg = db.query(_FACTOR_SQL, (recent_from, base, start))
    finally:
        db.execute(f"DROP TABLE IF EXISTS {_BAND_TABLE}")
        db.execute(f"DROP TABLE IF EXISTS {_DATE_TABLE}")
        conn.commit()

    first = _first_dates()
    out: dict[str, dict] = {}
    for r in agg:
        code = r["code"]
        band = bands.get(code)
        if not band:
            continue
        lo, hi = band
        bars = r["bars"] or 0
        last_close, min_close = r["last_close"], r["min_close"]
        factor = r["factor_last"] or 0
        height = (hi / lo - 1) * 100 if lo > 0 else None
        ext_height = ((r["max_close"] / r["min_close"] - 1) * 100
                      if r["min_close"] and r["max_close"] else None)
        time_pct = (r["in_band"] or 0) / bars * 100 if bars else None
        pos = ((last_close - lo) / (hi - lo) * 100
               if last_close is not None and hi > lo else None)
        rebound = last_close / min_close if last_close and min_close else None
        low60 = r["low60"]
        # 挖坑：近 60 个交易日的复权最低价跌破密集区下沿，且现价又收回箱体内
        dip = 1 if (low60 is not None and last_close is not None and lo
                    and low60 < lo and lo <= last_close <= hi) else 0
        out[code] = {
            # 展示坐标：整段除以最新复权因子换回「今日价口径」（元），比值类字段不受影响
            "box_top": round(hi / factor, 2) if factor else None,
            "box_bottom": round(lo / factor, 2) if factor else None,
            "box_height": round(height, 1) if height is not None else None,
            "box_ext_height": round(ext_height, 1) if ext_height is not None else None,
            "box_pos": round(pos, 1) if pos is not None else None,
            "box_slope_3y": (lambda v: round(v, 2) if v is not None else None)(
                _regression_pct_year(bars, r["sx"], r["sl"], r["sxl"], r["sxx"])),
            "box_cross": r["crossings"] or 0,
            "box_time_pct": round(time_pct, 1) if time_pct is not None else None,
            "box_rebound": round(rebound, 2) if rebound is not None else None,
            "box_dip_60d": dip,
            "box_bars": bars,
            "box_years": (lambda v: round(v, 1) if v is not None else None)(
                _years_between(first.get(code, ""), base)),
        }

    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = out
    return out


def invalidate() -> None:
    """导入新日线后调用（基准日变了本来就会换缓存键，这里留给手动清缓存的场合）。"""
    _cache.clear()
