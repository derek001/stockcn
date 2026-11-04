"""板块轮动分析：市场结构 → 行业强弱 → 板块族轴 → 主线生命周期。

这页回答的是「现在处于哪一段」，不是「下个月买哪个板块」。本地 5 年实测月频轮动
没有可交易的信号（上月强弱 → 下月收益的截面 IC 均值≈0、上月前 5 名下月仍在前 5 的
比例≈随机、买前 10 卖后 10 的多空收益 −0.18%/月、t=−0.35），所以预测下一段的实现
一律不做；能站住的是**结构类**量：板块是不是几个「族」在一起动（族轴分段载荷相关
中位 +0.961，因果口径每月只有 0.5 个行业换组）、截面离散度处在什么水位、主线抱团度
处在什么分位。数字复现见 README「板块轮动」一节。

口径与已知偏差（页面顶部同样会写）：
- 行业 = 申万一级 31 类，成员由 `industry.py` 归并表从**今天的** `stocks.industry`
  快照往回套：今天归「电子」的股票，五年前可能还没上市、也可能当时归在别的行业。
  ⇒ 有幸存者与回溯偏差，横比强弱可用，不能当成历史行业指数。
- 行业净值 = 成员等权日收益累乘（`close×adj_factor`，缺价最多补 5 根，至少 1/4
  交易日有价才计入），与回测页的行业基准同一份实现（`backtest.synthesize_board_frame`）。
- 抱团度的分母 = 已归并进 31 类的那些股票的成交额合计，未分类股票不在内。
- 族轴分组走**因果**口径：每个月只用当月之前已收盘的数据重算分组，避免「用未来
  数据分组、再回测同一段」的前视偏差；同时给全样本（前视）版做稳定性对照并标明差异。
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import backtest, db, industry

# 板块轮动页开放的指数（科创50 历史短，只用于箱体参照，不参与行业统计）
INDEX_NAMES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
}
HORIZONS = (("1M", 21), ("3M", 63), ("6M", 126), ("12M", 252))
BOX_BARS = 250                 # 指数箱体窗口（交易日 ≈ 12 个月）
LOADING_CUT = 0.15             # 族载荷 ≥ 该值才算进 A/B 组
MIN_GROUP = 4                  # A、B 两组各至少几个行业才认为族轴可用
WARMUP_BARS = 380              # 约 18 个月历史后才开始因果分组
ROLL_Z_MONTHS = 36             # 价差滚动 z 值窗口（月）
ROLL_Z_MIN = 12                # 滚动 z 值最少几个月历史
MAINLINE_DD_PCT = 12.0         # 主线净值距段内高点回撤超此值 → 判瓦解
MAINLINE_COOL_PP = 1.5         # 抱团度 20 日降幅超此值（百分点）→ 判瓦解
CATCHUP_GAP_PCT = 2.0          # 次强梯队 1M 涨幅领先主线超此值 → 判补涨扩散
NO_MAIN_CONC_PCTILE = 40.0     # 抱团度分位低于此值 → 判无明确主线
CONC_HOT_PCTILE = 70.0         # 抱团度分位高于此值视为资金抱团
SERIES_STEP = 5                # 曲线抽稀：每 5 根交易日给一个点

_STATE_LABEL = {
    "none": "无明确主线",
    "forming": "主线酝酿",
    "crowded": "主线抱团推升",
    "spread": "主线扩散（补涨）",
    "broken": "主线退潮（瓦解）",
}

# 行业净值与全市场宽度都要扫几百万行，同一天重复请求直接复用
_BOARD_CACHE: "dict[tuple[str, str], tuple[pd.DataFrame, pd.DataFrame, list[dict]]]" = {}
_BREADTH_CACHE: "dict[tuple[str, str, str], dict]" = {}


# ---------- 基础件 ----------

def _calendar(win_start: str, as_of: str) -> list[str]:
    """交易日历：用上证指数日线取（比在 1690 万行日线上做 DISTINCT 便宜），
    再截到**个股末根**那天——指数刷得比个股快时，基准日必须统一到个股那天，
    否则「当日收盘/前日收盘」这些宽度指标全部取空。
    """
    rows = db.query(
        "SELECT date FROM index_kline WHERE index_code='sh000001' AND date>=? AND date<=? "
        "ORDER BY date", (win_start, as_of))
    out = [r["date"] for r in rows]
    if not out or out[-1] != as_of:      # 指数缺当天（例如停牌日差异）：补上个股末根
        out.append(as_of)
    return out


def _pct_rank(vals: list[float], cur: float) -> float | None:
    """当前值在历史样本里的分位（%）。样本为空返回 None。"""
    n = len(vals)
    if not n:
        return None
    return round(100.0 * sum(1 for v in vals if v <= cur) / n, 1)


def _median(xs: list[float]) -> float | None:
    return round(float(np.median(xs)), 2) if xs else None


def _downsample(dates: list[str], series: dict[str, list]) -> dict:
    out = {"dates": dates[::SERIES_STEP]}
    for k, v in series.items():
        out[k] = [None if x is None or (isinstance(x, float) and pd.isna(x))
                  else round(float(x), 2) for x in v[::SERIES_STEP]]
    return out


def _boards(start: str, end: str):
    """31 个一级行业的等权净值与成交额（按日），带进程内缓存。"""
    key = (start, end)
    hit = _BOARD_CACHE.get(key)
    if hit:
        return hit
    nav: dict[str, pd.Series] = {}
    amt: dict[str, pd.Series] = {}
    info: list[dict] = []
    for l1 in industry.level1_names():
        f = backtest.synthesize_board_frame(l1, start, end)
        if not f or len(f["dates"]) < 2:
            continue
        nav[l1] = pd.Series(f["level"], index=f["dates"], dtype="float64")
        amt[l1] = pd.Series(f["amount"], index=f["dates"], dtype="float64")
        info.append({"level1": l1, "n_members": f["n_members"],
                     "n_member_codes": f["n_member_codes"]})
    if not nav:
        raise ValueError("31 个一级行业都没能合成净值：日线缺复权因子或区间内没有数据，"
                         "请先到「数据中心」确认区间内有日线")
    nav_df = pd.DataFrame(nav).sort_index()
    amt_df = pd.DataFrame(amt).sort_index().reindex(nav_df.index)
    if len(_BOARD_CACHE) > 3:
        _BOARD_CACHE.clear()
    _BOARD_CACHE[key] = (nav_df, amt_df, info)
    return nav_df, amt_df, info


# ---------- 面板一：市场结构（指数箱体 + 宽度） ----------

def _index_panel(index_code: str, win_start: str, as_of: str) -> dict:
    rows = db.query(
        "SELECT date, close FROM index_kline WHERE index_code=? AND date>=? AND date<=? "
        "ORDER BY date", (index_code, win_start, as_of))
    if len(rows) < BOX_BARS // 2:
        raise ValueError(f"{INDEX_NAMES[index_code]} 的指数K线不足 {BOX_BARS // 2} 根，"
                         "请先到「数据中心 → 在线数据」跑「日常刷新」")
    newer = db.query_one(
        "SELECT MAX(date) AS d FROM index_kline WHERE index_code=? AND date>?",
        (index_code, as_of))["d"]
    closes = [float(r["close"]) for r in rows]
    dates = [r["date"] for r in rows]
    last = closes[-1]
    box = closes[-BOX_BARS:]
    hi, lo = max(box), min(box)
    year = dates[-1][:4]
    y_i = next((i for i, d in enumerate(dates) if d >= f"{year}-01-01"), len(dates) - 1)
    ytd = closes[y_i]
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    return {
        "code": index_code, "name": INDEX_NAMES[index_code], "date": dates[-1],
        "newer_bar_date": newer,
        "stale_note": (f"指数已刷到 {newer}，个股日线到 {dates[-1]}；本页统一按 {dates[-1]} 计算"
                       if newer else None),
        "close": round(last, 2),
        "chg_1d": round((last / closes[-2] - 1) * 100, 2),
        "ytd_pct": round((last / ytd - 1) * 100, 1),
        "ytd_date": dates[y_i],
        "box_lo": round(lo, 2), "box_hi": round(hi, 2),
        "box_pos_pct": round((last - lo) / (hi - lo) * 100, 1) if hi > lo else None,
        "box_width_pct": round((hi / lo - 1) * 100, 1) if lo else None,
        "chg_12m": round((last / closes[-1 - BOX_BARS] - 1) * 100, 1)
        if len(closes) > BOX_BARS else None,
        "above_ma20": last > ma20,
        "ma60": round(ma60, 2) if ma60 else None,
        "above_ma60": (last > ma60) if ma60 else None,
    }


def _breadth_panel(win_start: str, ytd_start: str, d20: str, d60: str,
                   last_date: str, prev_date: str) -> dict:
    """全市场宽度：一次 GROUP BY 扫窗口内所有日线。

    窗口新高/新低用**分析窗口**（默认 5 年）而不是全历史：全历史要扫 1690 万行，
    页面等待不划算，且「几年内新高」本身就是技术口径。复权收盘口径，含分红送转。
    """
    key = (win_start, ytd_start, last_date)
    hit = _BREADTH_CACHE.get(key)
    if hit:
        return hit
    rows = db.query_rows(
        "SELECT code, "
        "MAX(close*adj_factor), MIN(close*adj_factor), "
        "MAX(CASE WHEN date>=? THEN close*adj_factor END), "
        "MIN(CASE WHEN date>=? THEN close*adj_factor END), "
        "SUM(CASE WHEN date>=? THEN close*adj_factor ELSE 0 END), "
        "SUM(CASE WHEN date>=? THEN close*adj_factor ELSE 0 END), "
        "SUM(CASE WHEN date>=? THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN date>=? THEN 1 ELSE 0 END), "
        "MAX(CASE WHEN date=? THEN close*adj_factor END), "
        "MAX(CASE WHEN date=? THEN close*adj_factor END), "
        "MAX(CASE WHEN date=? THEN close*adj_factor END) "
        "FROM kline_daily WHERE date>=? AND adj_factor IS NOT NULL GROUP BY code",
        (ytd_start, ytd_start, d60, d20, d60, d20,
         last_date, prev_date, ytd_start, win_start))
    n_total = db.query_one("SELECT COUNT(*) AS n FROM stocks")["n"]
    ytd: list[float] = []
    adv = dec = flat = 0
    hi_last = hi_ytd = lo_ytd = 0
    above20 = above60 = 0
    n_active = 0
    for (_code, hi, lo, hi_y, lo_y, s60, s20, c60, c20,
         last_c, prev_c, ytd_c) in rows:
        if last_c is None:
            continue        # 末根缺价或缺因子：不计入任何宽度指标
        n_active += 1
        if hi is not None:
            if hi_y is not None and hi_y >= hi * 0.999:
                hi_ytd += 1
            if last_c >= hi * 0.999:
                hi_last += 1
        if lo is not None and lo_y is not None and lo_y <= lo * 1.001:
            lo_ytd += 1
        if c20 and last_c > s20 / c20:
            above20 += 1
        if c60 and last_c > s60 / c60:
            above60 += 1
        if prev_c:
            if last_c > prev_c * 1.0001:
                adv += 1
            elif last_c < prev_c * 0.9999:
                dec += 1
            else:
                flat += 1
        if ytd_c and last_c:
            ytd.append((last_c / ytd_c - 1) * 100)
    n_ytd = len(ytd)
    out = {
        "stocks_in_pool": n_total, "stocks_with_last_bar": n_active,
        "ytd_base_date": ytd_start, "ytd_n": n_ytd,
        "ytd_median_pct": _median(ytd),
        "ytd_mean_pct": round(float(np.mean(ytd)), 1) if ytd else None,
        "ytd_down_share_pct": round(100 * sum(1 for v in ytd if v < 0) / n_ytd, 1)
        if n_ytd else None,
        "ytd_down_over_20_n": sum(1 for v in ytd if v < -20) if n_ytd else None,
        "window_hi_reach_n": hi_ytd,
        "window_hi_reach_pct": round(100 * hi_ytd / n_active, 1) if n_active else None,
        "at_window_high_n": hi_last,
        "window_lo_reach_n": lo_ytd,
        "window_lo_reach_pct": round(100 * lo_ytd / n_active, 1) if n_active else None,
        "adv_n": adv, "dec_n": dec, "flat_n": flat,
        "above_ma20_pct": round(100 * above20 / n_active, 1) if n_active else None,
        "above_ma60_pct": round(100 * above60 / n_active, 1) if n_active else None,
        "window_years_note": "新高/新低（基准＝上方分析窗口内的最高/最低复权收盘，非上市以来全历史）",
    }
    if len(_BREADTH_CACHE) > 3:
        _BREADTH_CACHE.clear()
    _BREADTH_CACHE[key] = out
    return out


# ---------- 面板二：行业强弱矩阵 + 截面离散度 ----------

def _strength_panel(nav: pd.DataFrame, amt: pd.DataFrame) -> dict:
    """31 行业各窗口涨幅 + 排名 + 抱团度 + MA 位置，附等权大盘基准。

    大盘基准 = 31 个行业净值的等权平均（行业再等权），个股宽度看 `_breadth_panel`。
    """
    nav_f = nav.ffill()
    rn = nav_f.pct_change(fill_method=None).fillna(0.0)
    bench = 100.0 * (1 + rn.mean(axis=1)).cumprod()
    n = len(nav_f)
    amt_total = amt.sum(axis=1, min_count=1)
    share_ma20 = amt.div(amt_total, axis=0).rolling(20, min_periods=5).mean()
    look = min(n, 504)
    rows = []
    for c in nav_f.columns:
        s = nav_f[c]
        if s.isna().all():
            continue
        chg: dict[str, float | None] = {}
        for lbl, d in HORIZONS:
            base = s.iloc[-1 - d] if n > d and not pd.isna(s.iloc[-1 - d]) else None
            chg[lbl.lower()] = round((s.iloc[-1] / base - 1) * 100, 1) \
                if base and not pd.isna(base) and base > 0 else None
        y = s.loc[s.index[-1][:4] + "-01-01":]
        w12 = s.iloc[-min(n, BOX_BARS):]
        cur_share = float(share_ma20[c].iloc[-1]) if not pd.isna(share_ma20[c].iloc[-1]) else None
        hist = [float(v) for v in share_ma20[c].iloc[-look:] if not pd.isna(v)]
        rows.append({
            "level1": c,
            "chg": chg,
            "ytd_pct": round((s.iloc[-1] / y.iloc[0] - 1) * 100, 1) if len(y) > 1 else None,
            "excess_3m": None,  # 下面按大盘同期补
            "pos_1y_pct": round((s.iloc[-1] - w12.min()) / (w12.max() - w12.min()) * 100, 1)
            if w12.max() > w12.min() else None,
            "dd_1y_pct": round((s.iloc[-1] / w12.max() - 1) * 100, 1),
            "above_ma20": bool(s.iloc[-1] > s.iloc[-20:].mean()),
            "above_ma60": bool(s.iloc[-1] > s.iloc[-60:].mean()) if n >= 60 else None,
            "amt_share_pct": round(cur_share * 100, 2) if cur_share is not None else None,
            "amt_share_pctile": _pct_rank(hist, cur_share) if cur_share is not None else None,
        })
    bench_3m = round((bench.iloc[-1] / bench.iloc[-1 - 63] - 1) * 100, 1) \
        if n > 63 else None
    for r in rows:
        if r["chg"]["3m"] is not None and bench_3m is not None:
            r["excess_3m"] = round(r["chg"]["3m"] - bench_3m, 1)
    for lbl in ("1m", "3m", "6m", "12m"):
        order = sorted(rows, key=lambda x: (x["chg"][lbl] is None, -(x["chg"][lbl] or 0)))
        for i, r in enumerate(order):
            if r["chg"][lbl] is not None:
                r[f"rank_{lbl}"] = i + 1
    rows.sort(key=lambda x: -(x["chg"]["3m"] if x["chg"]["3m"] is not None else -1e9))
    disp = np.log(nav_f).diff().dropna(how="all").std(axis=1, ddof=0) * 100
    month = pd.PeriodIndex(pd.to_datetime(disp.index), freq="M")
    d_m = disp.groupby(month).last().dropna()
    d_hist = [float(v) for v in disp]
    disp_panel = {
        "unit": "行业日收益截面标准差（%）",
        "current_20d": round(float(disp.iloc[-20:].mean()), 3),
        "pctile": _pct_rank(d_hist, float(disp.iloc[-20:].mean())),
        "p10": round(float(np.quantile(d_hist, 0.10)), 3),
        "p50": round(float(np.median(d_hist)), 3),
        "p90": round(float(np.quantile(d_hist, 0.90)), 3),
        "monthly": [{"date": str(p.to_timestamp().date()), "val": round(float(v), 3)}
                    for p, v in list(d_m.items())[-36:]],
        "note": "截面离散度高＝板块之间有差异可轮；低＝同涨同跌，轮动无意义",
    }
    bench_chg = {lbl.lower(): round((bench.iloc[-1] / bench.iloc[-1 - d] - 1) * 100, 1)
                 for lbl, d in HORIZONS if n > d}
    return {
        "table": rows,
        "dispersion": disp_panel,
        "benchmark": {"name": "31 行业等权基准", "base": 100.0,
                      "chg": bench_chg, "bench_3m_pct": bench_3m},
        "benchmark_series": _downsample(list(nav_f.index), {"bench": list(bench)}),
    }


# ---------- 面板三：板块族轴（剔掉市场分量后的第一主因子） ----------

def _pc1(corr: pd.DataFrame) -> tuple[pd.Series, float]:
    u, s, _ = np.linalg.svd(corr.values, full_matrices=False)
    ev = (s ** 2 / (s ** 2).sum())[0]
    return pd.Series(u[:, 0], index=corr.index), float(ev)


def _groups(vec: pd.Series) -> tuple[list[str], list[str]]:
    a = [k for k, v in vec.items() if v >= LOADING_CUT]
    b = [k for k, v in vec.items() if v <= -LOADING_CUT]
    return sorted(a), sorted(b)


def _family_panel(nav: pd.DataFrame) -> dict:
    """因果族轴：每月只用当月之前的数据重算残差 PC1 分组，再看当月之后的族价差。

    残差 = 行业日收益 − 31 行业当日均值（剔掉「一起涨跌」的市场共同分量）。
    相关阵第一主成分的两个符号端＝两个板块族；价差 = A 组均值 − B 组均值。
    """
    nav_f = nav.ffill()
    r = np.log(nav_f).diff().dropna(how="all")
    resid = r.sub(r.mean(axis=1), axis=0)
    month = pd.PeriodIndex(pd.to_datetime(resid.index), freq="M")
    counts = resid.groupby(month).size()
    ends = np.cumsum(counts.to_numpy())
    m_sum = resid.groupby(month).sum()

    vecs: dict[str, pd.Series] = {}
    sets: list[tuple[str, list[str], list[str], float]] = []
    flips: list[int] = []
    prev_v: pd.Series | None = None
    prev_groups: tuple[frozenset[str], frozenset[str]] | None = None
    for i, m in enumerate(m_sum.index):
        cut = int(ends[i])
        if cut < WARMUP_BARS:
            continue
        v, ev = _pc1(resid.iloc[:cut].corr())
        if prev_v is not None and float(np.dot(v.values, prev_v.values)) < 0:
            v = -v                      # 符号锚定上月，累积分位才可比
        a, b = _groups(v)
        prev_v = v
        vecs[str(m)] = v
        if len(a) < MIN_GROUP or len(b) < MIN_GROUP:
            continue
        if prev_groups is not None:
            flips.append(len((frozenset(a) | frozenset(b)) ^ (prev_groups[0] | prev_groups[1])))
        prev_groups = (frozenset(a), frozenset(b))
        sets.append((str(m), a, b, ev))
    if not sets:
        return {"available": False,
                "reason": f"历史不足 {WARMUP_BARS} 个交易日（约 18 个月），无法做因果分组"}

    cur_month, group_a, group_b, cur_ev = sets[-1]
    rn = nav_f.pct_change(fill_method=None).fillna(0.0)
    a_level = 100.0 * (1 + rn[group_a].mean(axis=1)).cumprod()
    b_level = 100.0 * (1 + rn[group_b].mean(axis=1)).cumprod()

    sp = pd.Series([float(m_sum.loc[m, a].mean() - m_sum.loc[m, b].mean())
                    for m, a, b, _ in sets],
                   index=pd.Index([str(m) for m, _, _, _ in sets], name="month"))
    lvl = sp.cumsum()
    cur_pctile = round(100.0 * float((lvl <= lvl.iloc[-1]).mean()), 1)
    roll_mean = lvl.rolling(ROLL_Z_MONTHS, min_periods=ROLL_Z_MIN).mean()
    roll_std = lvl.rolling(ROLL_Z_MONTHS, min_periods=ROLL_Z_MIN).std(ddof=0)
    z = (lvl - roll_mean) / roll_std

    def _spearman(x: pd.Series, y: pd.Series) -> tuple[float | None, float | None, int]:
        ok = x.notna() & y.notna()
        nn = int(ok.sum())
        if nn < 8:
            return None, None, nn
        cc = float(np.corrcoef(x[ok].rank(), y[ok].rank())[0, 1])
        tt = cc * np.sqrt(max(nn - 2, 1) / max(1 - cc ** 2, 1e-9))
        return round(cc, 3), round(tt, 2), nn

    f1 = sp.shift(-1)
    f3 = sp.rolling(3).sum().shift(-3)
    rank_pct = lvl.rank(pct=True)
    ic1, t1, n1 = _spearman(rank_pct, f1)
    ic3, t3, n3 = _spearman(rank_pct, f3)
    hi = f1[rank_pct > 0.8]
    lo = f1[rank_pct < 0.2]

    # 稳定性：全样本（前视）轴 vs 分段轴；再和因果末月分组比重合度
    full_v, full_ev = _pc1(resid.corr())
    half = len(resid) // 2
    seg = []
    for lbl, sub in (("前半", resid.iloc[:half]), ("后半", resid.iloc[half:]),
                     ("最近1年", resid.iloc[-252:])):
        if len(sub) < 120:
            continue
        v, ev = _pc1(sub.corr())
        if float(np.dot(v.values, full_v.values)) < 0:
            v = -v
        seg.append({"label": lbl, "corr_with_full": round(float(np.corrcoef(full_v, v)[0, 1]), 3),
                    "explained_pct": round(ev * 100, 0)})
    fa, fb = _groups(full_v)
    loadings = [{"level1": k, "loading": round(float(v), 3),
                 "group": "A" if v >= LOADING_CUT else "B" if v <= -LOADING_CUT else "-"}
                for k, v in vecs[cur_month].sort_values(ascending=False).items()]
    # 逐月对照：末月因果版＝全样本（同一份数据），是恒等不是证据，所以拿前面每个月去比重合度
    agree = []
    for m, a, b, _ev in sets[:-1]:
        v_m = vecs[m]
        agree.append((str(m), len(set(a) & set(fa)), len(fa),
                      len(set(b) & set(fb)), len(fb),
                      float(np.corrcoef(v_m.sort_index(), full_v.sort_index())[0, 1])))
    corrs = [x[5] for x in agree]
    worst = sorted(agree, key=lambda x: x[5])[:3]

    def _fr(pair: tuple[list[int], list[int]]) -> str | None:
        return f"{int(np.median(pair[0]))}/{int(np.median(pair[1]))}" if agree else None

    overlap_with_full = {
        "months": len(agree),
        "a_median": _fr(([x[1] for x in agree], [x[2] for x in agree])),
        "b_median": _fr(([x[3] for x in agree], [x[4] for x in agree])),
        "a_min": f"{min(x[1] for x in agree)}/{max(x[2] for x in agree)}" if agree else None,
        "b_min": f"{min(x[3] for x in agree)}/{max(x[4] for x in agree)}" if agree else None,
        "corr_median": round(float(np.median(corrs)), 3) if corrs else None,
        "corr_min": round(float(np.min(corrs)), 3) if corrs else None,
        "worst_months": [f"{x[0]} 载荷相关 {x[5]:.2f}（B {x[3]}/{x[4]}）" for x in worst],
        "note": "本月的分组和「全样本分组」是同一批数据算出来的，重合是必然，不能当证据；"
                "这里拿其余每个月分别和全样本比，重合越高说明族的划分不依赖后视。",
    }
    series = _downsample(list(a_level.index), {
        "a_level": list(a_level), "b_level": list(b_level),
        "spread": list((a_level / b_level - 1) * 100)})
    adj = float(np.corrcoef(sp.iloc[:-1], sp.iloc[1:])[0, 1]) if len(sp) > 8 else None
    return {
        "available": True,
        "method": f"残差日收益相关阵第 1 主成分（载荷 ±{LOADING_CUT} 分组，"
                  f"每月末用截至当月末的数据重算，月内不看未来）",
        "months_tested": len(sets),
        "current_month": cur_month,
        "full_sample_explained_pct": round(full_ev * 100, 0),
        "explained_pct": round(cur_ev * 100, 0),
        "explained_pct_median": round(float(np.median([x[3] for x in sets])) * 100, 0),
        "group_a": group_a, "group_b": group_b,
        "loadings": loadings,
        "flip_avg": round(float(np.mean(flips)), 1) if flips else None,
        "flip_max": max(flips) if flips else None,
        "intra_group_corr": None,   # analyze 里补（需要 nav）
        "overlap_with_full": overlap_with_full,
        "stability": seg,
        "spread": {
            "note": "族价差 = A 组等权月收益 − B 组等权月收益；累计分位只说明「贵不贵」，"
                    "均值回归方向历史上同向但统计不显著（见 reversion）",
            "cur_month_pct": round(float(sp.iloc[-1]) * 100, 2),
            "last3_pct": round(float(sp.iloc[-3:].sum()) * 100, 2),
            "month_mean_abs_pct": round(float(sp.abs().mean()) * 100, 2),
            "month_std_pct": round(float(sp.std(ddof=1)) * 100, 2) if len(sp) > 2 else None,
            "adj_month_corr": round(adj, 3) if adj is not None else None,
            "cum": round(float(lvl.iloc[-1]) * 100, 1),
            "cum_pctile": cur_pctile,
            "roll_z": round(float(z.iloc[-1]), 2) if not pd.isna(z.iloc[-1]) else None,
            "series": series,
        },
        "reversion": {
            "ic_1m": ic1, "t_1m": t1, "n_1m": n1,
            "ic_3m": ic3, "t_3m": t3, "n_3m": n3,
            "hi_mean_pct": round(float(hi.mean()) * 100, 2) if len(hi) else None,
            "hi_n": int(len(hi)),
            "lo_mean_pct": round(float(lo.mean()) * 100, 2) if len(lo) else None,
            "lo_n": int(len(lo)),
            "note": "IC 为负＝价差冲高后下月倾向回落；|t|<2 时属方向参考，不构成信号",
        },
    }


def _intra_group_corr(nav: pd.DataFrame, group: list[str]) -> float | None:
    if len(group) < 2:
        return None
    c = np.log(nav.ffill()).diff()[group].corr()
    vals = c.where(np.triu(np.ones(c.shape, dtype=bool), k=1)).stack()
    return round(float(vals.mean()), 3)


# ---------- 面板四：主线生命周期 ----------

def _mainline_panel(nav: pd.DataFrame, amt: pd.DataFrame,
                    strength: list[dict], top_n: int) -> dict:
    """主线生命周期：无明确主线 / 酝酿 / 抱团推升 / 扩散补涨 / 退潮瓦解。

    主线按**钱在哪**定义（20 日均成交额占比前 top_n 的行业），不是按涨幅前 top_n——
    涨幅榜前五往往是补涨的二线，钱真正挤在哪个板块要按成交占比看。
    三个可观测量：① 抱团度（这组行业成交占比合计及其窗口分位）② 接力差
    （其余行业近 1M 均值是否反超主线）③ 主线净值自身的一年内高点回撤。
    指数箱体状态由 `_index_panel` 给，两者并排看就是「箱体不动、箱体内换内核」。
    """
    by_name = {r["level1"]: r for r in strength}
    share_sorted = sorted((r for r in strength if r["amt_share_pct"] is not None),
                          key=lambda r: -r["amt_share_pct"])
    leaders_d = [r["level1"] for r in share_sorted[:top_n]]
    others_d = [r["level1"] for r in share_sorted[top_n:]]
    mom_leaders = [r["level1"] for r in strength[:top_n]]
    nav_f = nav.ffill()
    rn = nav_f.pct_change(fill_method=None).fillna(0.0)
    lvl = 100.0 * (1 + rn[leaders_d].mean(axis=1)).cumprod()
    bench = 100.0 * (1 + rn.mean(axis=1)).cumprod()
    w12 = lvl.iloc[-BOX_BARS:]
    dd = float((lvl.iloc[-1] / w12.max() - 1) * 100)

    amt_total = amt.sum(axis=1, min_count=1)
    conc = (amt[leaders_d].sum(axis=1, min_count=1) / amt_total * 100)
    conc20 = conc.rolling(20, min_periods=5).mean()
    cur_conc = float(conc20.iloc[-1])
    hist = [float(v) for v in conc20 if not pd.isna(v)]
    pctile = _pct_rank(hist, cur_conc)
    chg_20d_pp = round(cur_conc - float(conc20.iloc[-21]), 2) if len(conc20) > 21 else None

    def _mean_1m(names: list[str]) -> float | None:
        v = [by_name[c]["chg"]["1m"] for c in names if by_name[c]["chg"]["1m"] is not None]
        return round(float(np.mean(v)), 1) if v else None

    lead_1m_mean = _mean_1m(leaders_d)
    others_1m_mean = _mean_1m(others_d)
    catchup_pp = round(others_1m_mean - lead_1m_mean, 1) \
        if lead_1m_mean is not None and others_1m_mean is not None else None
    excess_3m = [by_name[c]["excess_3m"] for c in leaders_d if by_name[c]["excess_3m"] is not None]
    lead_excess = round(float(np.mean(excess_3m)), 1) if excess_3m else None
    above60 = sum(1 for c in leaders_d if by_name[c]["above_ma60"])
    top1_share = share_sorted[0]["amt_share_pct"] if share_sorted else None

    if pctile is None or pctile < NO_MAIN_CONC_PCTILE:
        state = "none"
    elif dd <= -MAINLINE_DD_PCT or (chg_20d_pp is not None and chg_20d_pp <= -MAINLINE_COOL_PP):
        state = "broken"
    elif catchup_pp is not None and catchup_pp >= CATCHUP_GAP_PCT:
        state = "spread"
    elif pctile >= CONC_HOT_PCTILE and (lead_1m_mean is None or lead_1m_mean > 0):
        state = "crowded"
    else:
        state = "forming"

    def _s(v, fmt="{:+.1f}%"):
        return "无数据" if v is None else fmt.format(v)

    evidence = [
        f"主线（成交占比前 {top_n}）：{'、'.join(leaders_d)}；合计抱团度 {cur_conc:.1f}%"
        f"（窗口分位 {_s(pctile, '{:.0f}%')}，20 日变化 {_s(chg_20d_pp, '{:+.2f}pp')}），"
        f"第一大单板块 {share_sorted[0]['level1']} 独占 {top1_share:.1f}%",
        f"主线自身：近 1M 均值 {_s(lead_1m_mean)}、3M 超额（对 31 行业等权）均值 {_s(lead_excess)}、"
        f"距一年高点 {dd:+.1f}%、站上 MA60 的 {above60}/{top_n} 个",
        f"接力差：其余 {len(others_d)} 个行业近 1M 均值 {_s(others_1m_mean)} vs 主线 "
        f"{_s(lead_1m_mean)} → {_s(catchup_pp, '{:+.1f}pp')}"
        f"（正=补涨资金往二线走，是扩散/退潮的前兆）",
        f"另看动量榜（3M 超额前 {top_n}）：{'、'.join(mom_leaders)}"
        f"{'；两榜不重合＝钱还在原板块、涨幅已经切到二线（高低切换）' if set(mom_leaders) & set(leaders_d) != set(leaders_d) else '；两榜重合＝资金与涨幅同向'}",
    ]
    series = _downsample(list(nav_f.index), {
        "mainline": list(lvl), "bench": list(bench),
        "conc": [None if pd.isna(v) else float(v) for v in conc20]})
    return {
        "top_n": top_n,
        "leaders": [{"level1": c, "chg_1m": by_name[c]["chg"]["1m"],
                     "chg_3m": by_name[c]["chg"]["3m"], "chg_6m": by_name[c]["chg"]["6m"],
                     "excess_3m": by_name[c]["excess_3m"],
                     "amt_share_pct": by_name[c]["amt_share_pct"],
                     "pos_1y_pct": by_name[c]["pos_1y_pct"]} for c in leaders_d],
        "state": state, "state_label": _STATE_LABEL[state],
        "concentration_pct": round(cur_conc, 2),
        "concentration_pctile": pctile,
        "concentration_chg_20d_pp": chg_20d_pp,
        "mainline_dd_1y_pct": round(dd, 1),
        "leader_1m_mean_pct": lead_1m_mean,
        "others_1m_mean_pct": others_1m_mean,
        "leader_3m_excess_mean_pct": lead_excess,
        "catchup_gap_pp": catchup_pp,
        "above_ma60_n": above60,
        "momentum_leaders": mom_leaders,
        "evidence": evidence,
        "rules": {
            "主线定义": f"20 日均成交额占比前 {top_n} 的行业（钱在哪，不是涨幅榜）",
            "无明确主线": f"抱团度窗口分位 < {NO_MAIN_CONC_PCTILE:.0f}%",
            "主线退潮": f"主线净值距一年高点 ≤ −{MAINLINE_DD_PCT}% 或 抱团度 20 日降幅 ≥ {MAINLINE_COOL_PP}pp",
            "主线扩散": f"其余行业 1M 均值反超主线 ≥ {CATCHUP_GAP_PCT}pp",
            "主线抱团": f"抱团度分位 ≥ {CONC_HOT_PCTILE:.0f}% 且主线近 1M 仍为正",
            "其余情况": "主线酝酿（资金在聚集但还没到分位门槛）",
            "判定顺序": "无主线 → 退潮 → 扩散 → 抱团 → 酝酿",
        },
        "series": series,
    }


# ---------- 装配 ----------

def analyze(index_code: str = "sh000001", years: float = 5.0, top_n: int = 5) -> dict:
    years = min(10.0, max(3.0, float(years)))
    top_n = int(min(10, max(3, top_n)))
    if index_code not in INDEX_NAMES:
        raise ValueError(f"未知指数 {index_code}，可选："
                         + " / ".join(f"{k} {v}" for k, v in INDEX_NAMES.items()))
    t0 = time.time()
    today = db.query_one("SELECT MAX(date) AS d FROM kline_daily")["d"]
    if not today:
        raise ValueError("库里没有日线，请先到「数据中心 → 本地数据」导入")
    win_start = (date.fromisoformat(today) - timedelta(days=round(years * 365.25))).isoformat()
    nav, amt, board_info = _boards(win_start, today)
    cal = _calendar(win_start, today)
    if len(cal) < BOX_BARS:
        raise ValueError(f"交易日历只有 {len(cal)} 天，窗口太短或指数K线缺失，"
                         "请先把「日常刷新」跑一次")
    last_date, prev_date = cal[-1], cal[-2]
    d20, d60 = cal[-20], cal[-60]
    ytd_start = db.query_one(
        "SELECT MIN(date) AS d FROM index_kline "
        "WHERE index_code='sh000001' AND date>=? AND date<=?",
        (f"{last_date[:4]}-01-01", last_date))["d"]

    s2 = _strength_panel(nav, amt)
    strength = s2["table"]
    fam = _family_panel(nav)
    if fam.get("available"):
        fam["intra_group_corr"] = {
            "a": _intra_group_corr(nav, fam["group_a"]),
            "b": _intra_group_corr(nav, fam["group_b"])}
    out = {
        "meta": {
            "as_of": last_date, "window_start": nav.index[0], "window_end": nav.index[-1],
            "years": years, "bars": len(nav), "top_n": top_n,
            "industries": len(nav.columns),
            "industry_members": board_info,
            "unclassified_stocks": db.query_one(
                "SELECT COUNT(*) AS n FROM stocks WHERE industry IS NULL OR industry=''")["n"],
            "price_note": "行业净值＝成员股等权累计复权收益（close×adj_factor，起点 1000）",
            "caveats": [
                "行业归属用的是今天的 stocks.industry 快照往回套：早年还没上市或后来改归属的股票"
                "会被按今天的名字算进历史，所以这是「横比强弱」，不是真实的历史行业指数。",
                "月频「跟上月热点」在本库实测无信号（IC≈0、前 5 名次月留存≈随机），"
                "本页只给阶段识别与定位，不给下一个板块。",
                "新高/新低、强弱分位都是窗口内（当前所选年数）复权收盘口径，"
                "与行情软件的全历史/不复权口径不同。",
            ],
            "calc_sec": None,
        },
        "market": {"index": _index_panel(index_code, win_start, today),
                   "breadth": _breadth_panel(win_start, ytd_start, d20, d60,
                                             last_date, prev_date)},
        "strength": strength,
        "benchmark": s2["benchmark"],
        "benchmark_series": s2["benchmark_series"],
        "dispersion": s2["dispersion"],
        "families": fam,
        "mainline": _mainline_panel(nav, amt, strength, top_n),
    }
    out["meta"]["calc_sec"] = round(time.time() - t0, 1)
    return out
