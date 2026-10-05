"""Stock screening: condition evaluation over fundamentals + technicals."""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd

from . import boxrange, db
from .indicators import compute_all, tech_snapshot

_OP_FN = {
    ">=": lambda a, b: a is not None and a >= b,
    "<=": lambda a, b: a is not None and a <= b,
    ">": lambda a, b: a is not None and a > b,
    "<": lambda a, b: a is not None and a < b,
    "==": lambda a, b: a is not None and a == b,
    "in": lambda a, b: a in (b or []),
}

# ---- field catalogue exposed to the UI ----
FUNDAMENTAL_FIELDS = [
    {"field": "industry", "label": "行业", "type": "multi_text", "ops": ["in", "=="]},
    {"field": "market", "label": "市场", "type": "select", "options": ["SH", "SZ", "BJ"], "ops": ["in", "=="]},
    {"field": "total_mv", "label": "总市值(亿)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "float_mv", "label": "流通市值(亿)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "pe_ttm", "label": "市盈率TTM", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "pb", "label": "市净率", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "price", "label": "最新价(元)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "pct_chg", "label": "涨跌幅(%)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "amount", "label": "成交额(亿)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "turnover_rate", "label": "换手率(%)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "eps", "label": "每股收益(元)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "revenue_yoy", "label": "营收同比(%)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "net_profit_yoy", "label": "净利润同比(%)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "roe_weighted", "label": "ROE加权(%)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "gross_margin", "label": "毛利率(%)", "type": "number", "ops": [">=", "<=", ">", "<"]},
]

# 长箱体（box-range）派生字段：先在 6 年里找出「一波下跌出清」的箱底，再在底之后的段上算密集带，
# 口径见 boxrange.py 文件头。
# 「带内占比 box_time_pct」不在这里——箱顶/箱底本身就是「覆盖 80% 交易日的最窄区间」，
# 所以它恒 ≥80%（2026-10-03 用户确认 B4 撤掉筛选、只当展示列），放进目录会误导。
# 「箱底日期 box_bottom_date」是文本，只给结果表展示，不进条件目录。
BOX_FIELDS = [
    {"field": "box_height", "label": "箱体高度%(密集带)", "type": "number", "ops": [">=", "<="]},
    {"field": "box_ext_height", "label": "箱内振幅%(段内最高÷最低收盘)", "type": "number", "ops": [">=", "<="]},
    {"field": "box_pos", "label": "现价在箱体位置%", "type": "number", "ops": [">=", "<="]},
    {"field": "box_slope_3y", "label": "箱体年化斜率%", "type": "number", "ops": [">=", "<="]},
    {"field": "box_cross", "label": "收盘穿越箱体中线次数", "type": "number", "ops": [">=", "<="]},
    {"field": "box_rebound", "label": "现价÷段内最低收盘(倍)", "type": "number", "ops": [">=", "<="]},
    {"field": "box_decline_pre", "label": "出清回撤%(箱底前3年最高÷箱底)", "type": "number", "ops": [">=", "<="]},
    {"field": "box_dip_60d", "label": "近60日挖坑后收回(1是0否)", "type": "number", "ops": [">=", "<="]},
    {"field": "box_years", "label": "上市年限(按首根日线)", "type": "number", "ops": [">=", "<="]},
    {"field": "box_bars", "label": "箱体段交易日数", "type": "number", "ops": [">=", "<="]},
]

TECHNICAL_FIELDS = [
    {"field": "rsi", "label": "RSI(14)", "type": "number", "ops": [">=", "<=", ">", "<"]},
    {"field": "macd", "label": "MACD柱", "type": "number", "ops": [">=", "<="]},
    {"field": "dif", "label": "MACD DIF", "type": "number", "ops": [">=", "<="]},
    {"field": "vol_ratio", "label": "量比(当日/5日均量)", "type": "number", "ops": [">=", "<="]},
    {"field": "chg_20d", "label": "20日涨幅(%)", "type": "number", "ops": [">=", "<="]},
    {"field": "chg_60d", "label": "60日涨幅(%)", "type": "number", "ops": [">=", "<="]},
    {"field": "signal:macd_golden", "label": "MACD金叉", "type": "bool"},
    {"field": "signal:macd_dead", "label": "MACD死叉", "type": "bool"},
    {"field": "signal:macd_above_zero", "label": "MACD零轴上方", "type": "bool"},
    {"field": "signal:boll_break_up", "label": "布林带上轨突破", "type": "bool"},
    {"field": "signal:boll_break_down", "label": "布林带下轨跌破", "type": "bool"},
    {"field": "signal:ma_bullish", "label": "均线多头排列", "type": "bool"},
    {"field": "signal:ma20_up_cross", "label": "股价上穿MA20", "type": "bool"},
    {"field": "signal:kdj_golden", "label": "KDJ金叉", "type": "bool"},
    {"field": "signal:new_high_250", "label": "创250日新高", "type": "bool"},
    {"field": "signal:new_low_250", "label": "创250日新低", "type": "bool"},
    {"field": "signal:vol_surge", "label": "放量(量比≥2)", "type": "bool"},
] + BOX_FIELDS

_cache: dict = {"ts": 0.0, "data": None}
CACHE_TTL = 300  # seconds


def load_universe(force: bool = False) -> dict[str, dict]:
    """Return {code: merged context of fundamentals + latest fin + tech snapshot}."""
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"]

    rows = db.query(
        "SELECT s.code, s.name, s.market, s.industry, s.list_date, "
        "f.trade_date, f.price, f.pct_chg, f.total_mv, f.float_mv, f.pe_ttm, f.pb, "
        "f.turnover_rate, f.amount "
        "FROM stocks s LEFT JOIN fundamentals f ON f.code = s.code WHERE s.is_active = 1")
    universe = {r["code"]: dict(r) for r in rows}

    fr = db.query(
        "SELECT code, report_date, eps, revenue, revenue_yoy, net_profit, net_profit_yoy, "
        "roe_weighted, gross_margin, "
        "ROW_NUMBER() OVER (PARTITION BY code ORDER BY report_date DESC) rn "
        "FROM fin_report")
    for r in fr:
        if r["rn"] == 1 and r["code"] in universe:
            universe[r["code"]].update({k: r[k] for k in r
                                        if k not in ("code", "rn")})

    # technicals: last 260 trading bars per stock，指标一律用复权价（价格 × 因子）
    cutoff = (dt.date.today() - dt.timedelta(days=420)).isoformat()
    bars = db.query(
        "SELECT code, date, "
        "open*adj_factor AS open, high*adj_factor AS high, "
        "low*adj_factor AS low, close*adj_factor AS close, "
        "volume, amount, pct_chg "
        "FROM kline_daily WHERE date >= ? ORDER BY code, date", (cutoff,))
    if bars:
        df_all = pd.DataFrame(bars)
        for code, g in df_all.groupby("code", sort=False):
            if code not in universe:
                continue
            # 各股K线末根日期：与快照交易日不一致说明该股停牌或导入缺文件
            universe[code]["kline_date"] = str(g["date"].iloc[-1])
            if len(g) < 2:
                continue
            try:
                snap = tech_snapshot(compute_all(g))
            except Exception:
                snap = {}
            if snap:
                universe[code]["tech"] = snap

    # display units: yuan -> 亿 for market cap and turnover amount
    for c, u in universe.items():
        if u.get("total_mv"):
            u["total_mv"] = u["total_mv"] / 1e8
        if u.get("float_mv"):
            u["float_mv"] = u["float_mv"] / 1e8
        if u.get("amount"):
            u["amount"] = u["amount"] / 1e8

    _cache["ts"], _cache["data"] = now, universe
    return universe


def ensure_box(universe: dict[str, dict], find_days: int = boxrange.DEFAULT_FIND_DAYS,
               dense_pct: float = boxrange.DEFAULT_DENSE_PCT) -> None:
    """把箱体指标挂到 ctx["box"] 上——只在条件真的用到箱体时才跑（全市场算一次耗时见 README，之后命中内存缓存秒回）。"""
    if any(ctx.get("box") for ctx in universe.values()):
        return
    bmap = boxrange.compute(find_days, dense_pct)
    for code, ctx in universe.items():
        if code in bmap:
            ctx["box"] = bmap[code]


def uses_box(conditions: list[dict]) -> bool:
    return any(str(c.get("field") or "").startswith("box_") for c in conditions)


def eval_condition(ctx: dict, cond: dict) -> bool:
    field = cond.get("field")
    op = cond.get("op", ">=")
    value = cond.get("value")
    if not field:
        return True
    if field.startswith("signal:"):
        sig = (ctx.get("tech") or {}).get("signals") or {}
        return bool(sig.get(field.split(":", 1)[1]))
    if field.startswith("box_"):
        val = (ctx.get("box") or {}).get(field)
    else:
        val = ctx.get(field)
        if field in ("close", "dif", "dea", "rsi", "macd", "vol_ratio", "chg_20d", "chg_60d"):
            val = (ctx.get("tech") or {}).get(field)
    fn = _OP_FN.get(op)
    if fn is None or val is None:
        return False
    try:
        if isinstance(value, list):
            return fn(val, value)
        if op in ("in", "==") and isinstance(val, str):
            return fn(val, value)
        return fn(float(val), float(value))
    except (TypeError, ValueError):
        return False


def screen_all(conditions: list[dict]) -> list[dict]:
    """Manual screening: stock must satisfy ALL conditions."""
    universe = load_universe()
    if uses_box(conditions):
        ensure_box(universe)
    out = []
    for code, ctx in universe.items():
        if all(eval_condition(ctx, c) for c in conditions):
            out.append(_row(code, ctx))
    out.sort(key=lambda r: (r.get("total_mv") or 0), reverse=True)
    return out


def score_groups(groups: list[dict], top_n: int = 100) -> list[dict]:
    """Auto screening: weighted score across condition groups."""
    universe = load_universe()
    if uses_box([c for g in groups for c in (g.get("conditions") or [])]):
        ensure_box(universe)
    results = []
    for code, ctx in universe.items():
        total = 0.0
        detail = []
        for g in groups:
            conds = g.get("conditions") or []
            if conds:
                hit = sum(1 for c in conds if eval_condition(ctx, c))
                score = hit / len(conds)
            else:
                score = 0.0
            total += score * float(g.get("weight") or 0)
            detail.append({"group": g.get("name"), "weight": g.get("weight"),
                           "score": round(score, 4)})
        if total <= 0:
            continue
        row = _row(code, ctx)
        row["score"] = round(total / 100.0, 4)
        row["group_detail"] = detail
        results.append(row)
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:top_n]


def _row(code: str, ctx: dict) -> dict:
    tech = ctx.get("tech") or {}
    box = ctx.get("box") or {}
    return {
        "code": code,
        "name": ctx.get("name"),
        "market": ctx.get("market"),
        "industry": ctx.get("industry"),
        "price": ctx.get("price"),
        "trade_date": ctx.get("trade_date"),
        "kline_date": ctx.get("kline_date"),
        "pct_chg": ctx.get("pct_chg"),
        "total_mv": ctx.get("total_mv"),
        "float_mv": ctx.get("float_mv"),
        "pe_ttm": ctx.get("pe_ttm"),
        "pb": ctx.get("pb"),
        "amount": ctx.get("amount"),
        "turnover_rate": ctx.get("turnover_rate"),
        "roe_weighted": ctx.get("roe_weighted"),
        "revenue_yoy": ctx.get("revenue_yoy"),
        "net_profit_yoy": ctx.get("net_profit_yoy"),
        "report_date": ctx.get("report_date"),
        "rsi": tech.get("rsi"),
        "signals": (tech.get("signals") or {}),
        # 箱体字段只在条件里用到 box_* 时才有值（ensure_box 懒加载），没算过时为 None
        "box_top": box.get("box_top"),
        "box_bottom": box.get("box_bottom"),
        "box_height": box.get("box_height"),
        "box_ext_height": box.get("box_ext_height"),
        "box_pos": box.get("box_pos"),
        "box_slope_3y": box.get("box_slope_3y"),
        "box_cross": box.get("box_cross"),
        "box_time_pct": box.get("box_time_pct"),
        "box_rebound": box.get("box_rebound"),
        "box_decline_pre": box.get("box_decline_pre"),
        "box_dip_60d": box.get("box_dip_60d"),
        "box_bars": box.get("box_bars"),
        "box_bottom_date": box.get("box_bottom_date"),
    }
