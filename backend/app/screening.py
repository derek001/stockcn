"""Stock screening: condition evaluation over fundamentals + technicals."""
from __future__ import annotations

import datetime as dt
import time

import pandas as pd

from . import db
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
]

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

    # technicals: last 260 trading bars per stock
    cutoff = (dt.date.today() - dt.timedelta(days=420)).isoformat()
    bars = db.query(
        "SELECT code, date, open, high, low, close, volume, amount, pct_chg "
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


def eval_condition(ctx: dict, cond: dict) -> bool:
    field = cond.get("field")
    op = cond.get("op", ">=")
    value = cond.get("value")
    if not field:
        return True
    if field.startswith("signal:"):
        sig = (ctx.get("tech") or {}).get("signals") or {}
        return bool(sig.get(field.split(":", 1)[1]))
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
    out = []
    for code, ctx in universe.items():
        if all(eval_condition(ctx, c) for c in conditions):
            out.append(_row(code, ctx))
    out.sort(key=lambda r: (r.get("total_mv") or 0), reverse=True)
    return out


def score_groups(groups: list[dict], top_n: int = 100) -> list[dict]:
    """Auto screening: weighted score across condition groups."""
    universe = load_universe()
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
    }
