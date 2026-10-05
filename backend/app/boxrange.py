"""长箱体（box-range）统计：先找「一波下跌出清」的底，再在底之后的段上算箱体。

口径（2026-10-05 与用户重定，四条规则）：
1. **价格一律用收盘价**（含「挖坑」，不再看盘中最低价 `low`），计算用等比后复权收盘 `close * adj_factor`。
   比值/百分比类字段与复权口径无关；箱顶/箱底为与页面上的「最新价」对齐，整段 ÷ 该股最新复权因子。
2. **先找底**：在最近 6 年（1500 个交易日）里取最低复权收盘，其日期 `box_bottom_date` 就是「出清完成」那一点
   （并列取最早一根，实测取最早/最晚对段长与高度分布几乎无差：段≥600 根的只数 2697 vs 2691）。
   **箱体段 = 该日之后（含当日）的日线**，段根数 `box_bars`，段长必须 ≥3 年才谈得上「3 年以上箱体」。
3. **箱体上下沿 = 段内覆盖 dense_pct% 交易日收盘的比值最窄区间**（密集带），不是段内极值。
   「最窄」按 ln 距离（= 顶/底比值）量，与高度定义 顶/底−1 同一个目标函数；按绝对价差量会选出明显偏低的
   另一条带（603696：比值口径高 105%，价差口径高 132%），不可混用。
   段内收盘极值另出一个字段 `box_ext_height`（段内最高收盘 ÷ 最低收盘 − 1）当**形状门槛**用 ——
   用户规则「箱顶比箱底最多高一倍」就是它 ≤100%。带高与振幅的大小关系只受分箱边界约束：
   带不含段内极值时 `box_height < box_ext_height`；带把两极值都包进去时（横得极齐的票）带边各外扩一格，
   `box_height` 反而比振幅大到多 0.25%×(1+振幅)。所以「带高 ≤ 振幅」不是恒成立的不变量。
4. **出清段 = 箱底之前的那 750 个交易日**（按全市场日历倒推，逐股各自的窗口），
   `box_decline_pre` = 1 − 箱底 ÷ 该段最高复权收盘，即「一波下跌」的深度。

其余：
- 年化斜率 = 对 ln(复权收盘) 关于**全市场交易日序号**做最小二乘，再 (exp(slope*250)-1) 年化。用全市场日历序号
  而不是「该股自身第几根 bar」，停牌缺口才会体现在时间轴上（实测无停牌个股两者完全相同，中位差 0.0000%）。
- 挖坑 = 近 60 个交易日有**收盘**跌破密集带下沿，且现价又收回带内。
- 「上市年限 `box_years`」按 2026-10-05 用户裁定：**优先 `stocks.list_date`（上市公告日，在线刷新入库）**，
  接口没给日期的（最新 7 只新股）退回首根日线。两者在 5570 只有日线的股票里对不上 39 只（33 只上市日更晚：
  29 个 920xxx 新三板平移代码 + 000001/000002/000505/600018；6 只首根更晚，最多差 33 天），这些按上市日算。
  「横盘够不够久」的硬门槛是 `box_bars`（箱体段根数），与本字段无关。
- `box_time_pct`（带内占比）按定义恒 ≥ dense_pct，只能展示/排序，不能当硬过滤。

实现刻意不走 `screening.load_universe()` 的 pandas 路径：2250 交易日约 570 万行，全市场载入 Python 会拖到几分钟，
所以统计尽量在 SQLite 侧聚合，只回传每只一行。逐遍：① 找底窗口内最低复权收盘 → ② 该最低值出现的日期（并列取最早）
→ ③ 段内收盘按 ln 做 0.25% 分箱直方图，Python 滑窗求最窄密集带 → ④ 底之前 750 根的最高收盘（出清深度）
→ ⑤ 密集带写进临时表，一遍带 LAG 的聚合出段内全部统计量。全市场实测耗时见 README（比旧的两遍扫描明显变长）。
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict

from . import db

# 分箱宽度：ln(价) × 400，即 0.25% 的价格精度。
# 实测（200 只箱体池样本，对「连续价上的比值最窄带」复核）：格宽 1% 时箱底/箱顶中位偏差 1.08%、
# 半数样本 >1%；0.25% 时中位 0.19%、p90 0.97%。再细只剩并列歧义。
BIN_SCALE = 400
TRADING_DAYS = 250
DEFAULT_FIND_DAYS = 1500    # 找底窗口：6 年
DEFAULT_PRE_DAYS = 750      # 出清段：箱底之前 3 年
DEFAULT_DENSE_PCT = 80.0
RECENT_DAYS = 60            # 「挖坑」的近端窗口

# (基准日, 找底天数, 密集占比) -> {code: 指标}
_cache: dict[tuple, dict[str, dict]] = {}
_CACHE_MAX = 4

_BAND_TABLE = "box_band_tmp"
_DATE_TABLE = "box_date_tmp"
_MIN_TABLE = "box_min_tmp"
_BTM_TABLE = "box_bottom_tmp"

# ① 找底窗口内的最低复权收盘
_MIN_SQL = (
    "SELECT code, MIN(close * adj_factor) FROM kline_daily "
    "WHERE date >= ? AND close > 0 AND adj_factor > 0 GROUP BY code")

# ② 该最低值出现的日期；并列取最早一根（MIN(date)）。浮点等值成立：SQLite 存的乘积与这里重算是同一串位。
_BTM_SQL = (
    "SELECT k.code, MIN(k.date) FROM kline_daily k JOIN %s m ON m.code = k.code "
    "AND k.close * k.adj_factor = m.mn WHERE k.date >= ? AND k.close > 0 AND k.adj_factor > 0 "
    "GROUP BY k.code" % _MIN_TABLE)

# ③ 段内收盘的 ln 分箱直方图（0.25% 格宽），滑窗求最窄密集带
_HIST_SQL = (
    "SELECT k.code, CAST(FLOOR(LN(k.close * k.adj_factor) * ?) AS INT), COUNT(*) "
    "FROM kline_daily k JOIN %s b ON b.code = k.code WHERE k.date >= b.bd "
    "AND k.close > 0 AND k.adj_factor > 0 GROUP BY 1, 2 ORDER BY 1, 2" % _BTM_TABLE)

# ④ 出清段：底之前那 750 个交易日的最高复权收盘（pd = 逐股倒推出来的窗口起点，存在 _BTM_TABLE）
_PREMAX_SQL = (
    "SELECT k.code, MAX(k.close * k.adj_factor) FROM kline_daily k JOIN %s b ON b.code = k.code "
    "WHERE k.date >= b.pd AND k.date < b.bd AND k.close > 0 AND k.adj_factor > 0 GROUP BY k.code" % _BTM_TABLE)

# ⑤ 一遍聚合出箱体段全部统计量。x = 全市场交易日序号（_DATE_TABLE）；
# crossings 靠 LAG 比较相邻两根在中线上/下；带内天数靠 _BAND_TABLE 的 [lo, hi]；
# recent_min 用**收盘价**（规则 1），不再是盘中最低价。
_STATS_SQL = f"""
SELECT code,
       COUNT(*) AS bars,
       MIN(c) AS min_close,
       MAX(c) AS max_close,
       SUM(x) AS sx, SUM(ln) AS sl, SUM(x * ln) AS sxl, SUM(x * x) AS sxx,
       SUM(CASE WHEN prev IS NOT NULL AND above <> prev THEN 1 ELSE 0 END) AS crossings,
       SUM(in_band) AS in_band,
       MIN(recent_min) AS recent_min,
       MAX(last_close) AS last_close
FROM (
  SELECT k.code AS code,
         k.x AS x, k.c AS c, k.ln AS ln,
         CASE WHEN k.c >= (b.lo + b.hi) / 2 THEN 1 ELSE 0 END AS above,
         LAG(CASE WHEN k.c >= (b.lo + b.hi) / 2 THEN 1 ELSE 0 END)
           OVER (PARTITION BY k.code ORDER BY k.date) AS prev,
         CASE WHEN k.c BETWEEN b.lo AND b.hi THEN 1 ELSE 0 END AS in_band,
         CASE WHEN k.date >= ? THEN k.c END AS recent_min,
         CASE WHEN k.date = ? THEN k.c END AS last_close
  FROM (
    SELECT kd.code AS code, kd.date AS date,
           kd.close * kd.adj_factor AS c,
           LN(kd.close * kd.adj_factor) AS ln,
           d.x AS x
    FROM kline_daily kd JOIN {_DATE_TABLE} d ON d.date = kd.date
    WHERE kd.date >= ? AND kd.close > 0 AND kd.adj_factor > 0
  ) k
  JOIN {_BAND_TABLE} b ON b.code = k.code AND k.date >= b.bd
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
    """库里倒数第 back_days 个有行情的日历日（找底窗口起点、出清段起点、近 60 日分界都用它）。"""
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


def _regression_pct_year(bars: int, sx: float, sl: float, sxl: float, sxx: float) -> float | None:
    """ln(复权收盘) 对交易日序号最小二乘，按 250 交易日年化成百分比。"""
    den = bars * sxx - sx * sx
    if bars < 2 or den <= 0:
        return None
    v = (bars * sxl - sx * sl) / den * TRADING_DAYS
    if v > 20:  # exp 溢出
        return None
    return (math.exp(v) - 1) * 100


def _first_dates() -> dict[str, str]:
    return {r["code"]: r["d"] for r in
            db.query("SELECT code, MIN(date) AS d FROM kline_daily GROUP BY code")}


def _start_dates() -> dict[str, str]:
    """上市年限的起算日：优先 `stocks.list_date`，接口没给日期的（最新几只新股）退回首根日线。"""
    out = _first_dates()
    for r in db.query("SELECT code, list_date FROM stocks WHERE list_date IS NOT NULL"):
        out[r["code"]] = r["list_date"]
    return out


def _years_between(d1: str, d2: str) -> float | None:
    try:
        a = dt.date.fromisoformat(d1)
        b = dt.date.fromisoformat(d2)
    except (TypeError, ValueError):
        return None
    return (b - a).days / 365.25


def compute(find_days: int = DEFAULT_FIND_DAYS,
            dense_pct: float = DEFAULT_DENSE_PCT) -> dict[str, dict]:
    """全市场箱体指标，返回 {code: {...}}；按 (基准日, 找底天数, 密集占比) 缓存。"""
    base = latest_bar_date()
    if not base:
        return {}
    key = (base, find_days, dense_pct)
    if key in _cache:
        return _cache[key]

    find_start = _nth_last_date(find_days)
    load_start = _nth_last_date(find_days + DEFAULT_PRE_DAYS)
    recent_from = _nth_last_date(RECENT_DAYS)
    if not find_start:
        return {}
    if not load_start:  # 库里不足 9 年（首次导入、只回补了几年）：从最早一天起算出清段
        load_start = (db.query_one("SELECT MIN(date) AS d FROM kline_daily") or {}).get("d") or find_start

    # 全市场交易日历：x 给斜率用（停牌缺口要落在时间轴上），倒推 750 格给每只票定出清段起点
    dates = [r["date"] for r in
             db.query("SELECT DISTINCT date FROM kline_daily WHERE date >= ? ORDER BY date",
                      (load_start,))]
    if not dates:
        return {}
    x_of = {d: i + 1 for i, d in enumerate(dates)}

    conn = db.get_conn()  # 临时表建在这条共享连接上，同一连接才看得见
    for tbl, cols in ((_MIN_TABLE, "code TEXT PRIMARY KEY, mn REAL"),
                      (_BTM_TABLE, "code TEXT PRIMARY KEY, bd TEXT, pd TEXT"),
                      (_BAND_TABLE, "code TEXT PRIMARY KEY, lo REAL, hi REAL, bd TEXT"),
                      (_DATE_TABLE, "date TEXT PRIMARY KEY, x REAL")):
        db.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tbl} ({cols})")
        db.execute(f"DELETE FROM {tbl}")
    try:
        db.execute(f"INSERT INTO {_DATE_TABLE}(date, x) VALUES (?,?)",
                   many=[(d, i + 1) for i, d in enumerate(dates)])
        db.execute(f"INSERT INTO {_MIN_TABLE}(code, mn) VALUES (?,?)",
                   many=[(r[0], r[1]) for r in db.query_rows(_MIN_SQL, (find_start,))])
        # 出清段起点 pd = 箱底在日历上往前数 DEFAULT_PRE_DAYS 格；不足则从日历最早一天起（回撤照算，
        # 只是窗口短于 3 年 —— 新股会走到这里，段根数门槛本来就会把它们挡掉）
        bottoms = []
        for code, bd in db.query_rows(_BTM_SQL, (find_start,)):
            idx = x_of.get(bd)
            if idx is None:
                continue
            bottoms.append((code, bd, dates[max(0, idx - 1 - DEFAULT_PRE_DAYS)]))
        if not bottoms:
            return {}
        db.execute(f"INSERT INTO {_BTM_TABLE}(code, bd, pd) VALUES (?,?,?)", many=bottoms)

        per_hist: dict[str, list[tuple[int, int]]] = defaultdict(list)
        n_of: dict[str, int] = defaultdict(int)
        for code, bin_, k in db.query_rows(_HIST_SQL, (BIN_SCALE,)):
            per_hist[code].append((bin_, k))
            n_of[code] += k
        bands = {}
        for code, counts in per_hist.items():
            band = _dense_band(counts, n_of[code], dense_pct)
            if band:
                bands[code] = band
        if not bands:
            return {}
        bd_of = {c: bd for c, bd, _ in bottoms}
        db.execute(f"INSERT INTO {_BAND_TABLE}(code, lo, hi, bd) VALUES (?,?,?,?)",
                   many=[(c, lo, hi, bd_of.get(c, "")) for c, (lo, hi) in bands.items()])

        premax = {r[0]: r[1] for r in db.query_rows(_PREMAX_SQL)}
        agg = db.query(_FACTOR_SQL, (recent_from, base, find_start))
    finally:
        for tbl in (_MIN_TABLE, _BTM_TABLE, _BAND_TABLE, _DATE_TABLE):
            db.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.commit()

    bottom_date = {c: bd for c, bd, _ in bottoms}
    starts = _start_dates()
    out: dict[str, dict] = {}
    for r in agg:
        code = r["code"]
        band = bands.get(code)
        if not band:
            continue
        lo, hi = band
        seg_bars = r["bars"] or 0
        last_close, min_close = r["last_close"], r["min_close"]
        factor = r["factor_last"] or 0
        height = (hi / lo - 1) * 100 if lo > 0 else None
        ext_height = ((r["max_close"] / min_close - 1) * 100
                      if min_close and r["max_close"] else None)
        time_pct = (r["in_band"] or 0) / seg_bars * 100 if seg_bars else None
        pos = ((last_close - lo) / (hi - lo) * 100
               if last_close is not None and hi > lo else None)
        rebound = last_close / min_close if last_close and min_close else None
        pm = premax.get(code)
        decline = (1 - min_close / pm) * 100 if pm and min_close and pm > min_close else None
        recent_min = r["recent_min"]
        # 挖坑：近 60 个交易日的**收盘价**跌破密集区下沿，且现价又收回带内（规则 1）
        dip = 1 if (recent_min is not None and last_close is not None and lo
                    and recent_min < lo and lo <= last_close <= hi) else 0
        out[code] = {
            # 展示坐标：整段除以最新复权因子换回「今日价口径」（元），比值类字段不受影响
            "box_top": round(hi / factor, 2) if factor else None,
            "box_bottom": round(lo / factor, 2) if factor else None,
            "box_height": round(height, 1) if height is not None else None,
            "box_ext_height": round(ext_height, 1) if ext_height is not None else None,
            "box_pos": round(pos, 1) if pos is not None else None,
            "box_slope_3y": (lambda v: round(v, 2) if v is not None else None)(
                _regression_pct_year(seg_bars, r["sx"], r["sl"], r["sxl"], r["sxx"])),
            "box_cross": r["crossings"] or 0,
            "box_time_pct": round(time_pct, 1) if time_pct is not None else None,
            "box_rebound": round(rebound, 2) if rebound is not None else None,
            "box_dip_60d": dip,
            "box_bars": seg_bars,
            "box_bottom_date": bottom_date.get(code),
            "box_decline_pre": round(decline, 1) if decline is not None else None,
            "box_years": (lambda v: round(v, 1) if v is not None else None)(
                _years_between(starts.get(code, ""), base)),
        }

    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = out
    return out


def invalidate() -> None:
    """导入新日线后调用（基准日变了本来就会换缓存键，这里留给手动清缓存的场合）。"""
    _cache.clear()
