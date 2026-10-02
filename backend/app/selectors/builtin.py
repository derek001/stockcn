"""内置选股器（每个都是一个插件：带 id 的 Selector 子类）。

三个都只做「同时满足」的硬筛，score 仅用来排序；一律剔除 ST/退市风险股和当日没有新K线的股票。
"""
from __future__ import annotations

from .base import Selector, num, ramp, signal, tradable


class ValueQualitySelector(Selector):
    id = "value_quality"
    name = "低估值真成长"
    description = (
        "又便宜又能赚：市盈率TTM 和 市净率 低于上限，同时 ROE 和净利润同比不低于下限，"
        "并要求流通市值够大（太小的票成交清淡）。四项缺一不进，命中后按「越便宜、越能赚」排前。")
    params_schema = [
        {"key": "pe_max", "label": "市盈率TTM 上限", "type": "number", "default": 20, "min": 1, "max": 300},
        {"key": "pb_max", "label": "市净率 上限", "type": "number", "default": 3, "min": 0.1, "max": 30},
        {"key": "roe_min", "label": "ROE 下限(%)", "type": "number", "default": 10, "min": 0, "max": 60},
        {"key": "growth_min", "label": "净利润同比 下限(%)", "type": "number", "default": 10, "min": -50, "max": 200},
        {"key": "float_mv_min", "label": "流通市值 下限(亿)", "type": "number", "default": 30, "min": 0, "max": 5000},
    ]

    def select(self, universe: dict[str, dict], params: dict) -> list[dict]:
        out = []
        for code, ctx in universe.items():
            if not tradable(ctx):
                continue
            pe = num(ctx, "pe_ttm")
            pb = num(ctx, "pb")
            roe = num(ctx, "roe_weighted")
            growth = num(ctx, "net_profit_yoy")
            mv = num(ctx, "float_mv")
            if None in (pe, pb, roe, growth, mv):
                continue
            if not (0 < pe <= params["pe_max"]):
                continue
            if not (0 < pb <= params["pb_max"]):
                continue
            if roe < params["roe_min"] or growth < params["growth_min"] or mv < params["float_mv_min"]:
                continue
            score = (25 * (1 - pe / params["pe_max"]) + 20 * (1 - pb / params["pb_max"])
                     + 30 * ramp(roe, params["roe_min"]) + 25 * ramp(growth, params["growth_min"]))
            out.append({
                "code": code,
                "score": round(score, 1),
                "reason": (f"PE {pe:.1f}、PB {pb:.2f}、ROE {roe:.1f}%、"
                           f"净利同比 {growth:.1f}%、流通市值 {mv:.0f}亿"),
            })
        return out


class TrendVolumeSelector(Selector):
    id = "trend_volume"
    name = "趋势放量"
    description = (
        "走上升趋势且当天有钱进来：均线多头排列（5>10>20>60）、MACD 在零轴上方、"
        "当日成交量明显大于近 5 日均量、20 日涨幅落在区间里（涨幅太大说明已经炒到高位，会被排除）。")
    params_schema = [
        {"key": "vol_ratio_min", "label": "量比 下限", "type": "number", "default": 1.5, "min": 0.1, "max": 20},
        {"key": "chg20_min", "label": "20日涨幅 下限(%)", "type": "number", "default": 5, "min": -50, "max": 200},
        {"key": "chg20_max", "label": "20日涨幅 上限(%)", "type": "number", "default": 40, "min": 0, "max": 500},
        {"key": "float_mv_min", "label": "流通市值 下限(亿)", "type": "number", "default": 20, "min": 0, "max": 5000},
    ]

    def select(self, universe: dict[str, dict], params: dict) -> list[dict]:
        out = []
        lo, hi = params["chg20_min"], params["chg20_max"]
        if lo >= hi:
            raise ValueError("「20日涨幅 上限」要大于「下限」，否则没有股票能落进区间")
        for code, ctx in universe.items():
            if not tradable(ctx):
                continue
            if not signal(ctx, "ma_bullish") or not signal(ctx, "macd_above_zero"):
                continue
            vol_ratio = num(ctx, "vol_ratio")
            chg20 = num(ctx, "chg_20d")
            mv = num(ctx, "float_mv")
            close = num(ctx, "close")
            ma20 = num(ctx, "ma20")
            if None in (vol_ratio, chg20, mv, close, ma20) or ma20 <= 0:
                continue
            if vol_ratio < params["vol_ratio_min"] or mv < params["float_mv_min"]:
                continue
            if not (lo <= chg20 <= hi):
                continue
            # 三项都要「明显超过门槛」才满分，避免一批股票同时顶到 100 分排不出先后
            strength = min(max(close / ma20 - 1, 0) / 0.10, 1.0)
            score = (40 * ramp(vol_ratio, 2 * params["vol_ratio_min"])
                     + 30 * ramp(max(chg20, 0), 2 * max(lo, 0.01))
                     + 30 * strength)
            out.append({
                "code": code,
                "score": round(score, 1),
                "reason": (f"量比 {vol_ratio:.2f}、20日涨幅 {chg20:.1f}%、"
                           f"收盘高于MA20 {(close / ma20 - 1) * 100:.1f}%、流通市值 {mv:.0f}亿"),
            })
        return out


class OversoldReboundSelector(Selector):
    id = "oversold_rebound"
    name = "超跌企稳"
    description = (
        "跌得多但已经站回 20 日线：60 日跌幅超过设定的深度、RSI 仍在低位（说明不是已经反弹起来的票），"
        "同时当日收盘价回到 MA20 上方。适合找第一波反弹的候选，不保证后续一定继续涨。")
    params_schema = [
        {"key": "drop_min", "label": "60日跌幅至少(%)", "type": "number", "default": 15, "min": 1, "max": 90},
        {"key": "rsi_max", "label": "RSI 上限", "type": "number", "default": 55, "min": 5, "max": 90},
        {"key": "float_mv_min", "label": "流通市值 下限(亿)", "type": "number", "default": 20, "min": 0, "max": 5000},
    ]

    def select(self, universe: dict[str, dict], params: dict) -> list[dict]:
        out = []
        for code, ctx in universe.items():
            if not tradable(ctx):
                continue
            chg60 = num(ctx, "chg_60d")
            rsi = num(ctx, "rsi")
            close = num(ctx, "close")
            ma20 = num(ctx, "ma20")
            mv = num(ctx, "float_mv")
            if None in (chg60, rsi, close, ma20, mv):
                continue
            if chg60 > -params["drop_min"] or rsi > params["rsi_max"]:
                continue
            if close <= ma20 or mv < params["float_mv_min"]:
                continue
            depth = min(-chg60 / (2 * params["drop_min"]), 1.0)
            low_rsi = max(0.0, 1 - rsi / params["rsi_max"])
            back = min(max(close / ma20 - 1, 0) / 0.05, 1.0)
            score = 40 * depth + 30 * low_rsi + 30 * back
            out.append({
                "code": code,
                "score": round(score, 1),
                "reason": (f"60日跌 {-chg60:.1f}%、RSI {rsi:.1f}、"
                           f"收盘高于MA20 {(close / ma20 - 1) * 100:.1f}%、流通市值 {mv:.0f}亿"),
            })
        return out
