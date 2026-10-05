"""Built-in strategies (each is a plugin: subclass Strategy with an `id`).

信号一律用布尔掩码向量化算，不要逐根 `.iloc` 循环：全市场跑批每只都要走完一遍K线，
循环版实测 0.22s/只（5545 只 ≈ 20 分钟），掩码版能把这段压到毫秒级。
逐根循环那份留在 `.tmp-sph/legacy_builtin.py` 当交叉核对的参考实现。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import add_boll, add_kdj, add_ma, add_macd
from .base import Strategy


def _signals(dates: pd.Series, buy: np.ndarray, sell: np.ndarray,
             buy_reason: str, sell_reason: str) -> list[dict]:
    act = np.zeros(len(buy), dtype=np.int8)
    act[sell] = -1
    act[buy] = 1  # 同日既像买又像卖时买优先，与原来 if 买 / elif 卖 的写法一致
    vals = dates.to_numpy()
    out = []
    for i in np.flatnonzero(act):
        is_buy = act[i] == 1
        out.append({"date": vals[i], "side": "buy" if is_buy else "sell",
                    "reason": buy_reason if is_buy else sell_reason})
    return out


class MacdMaStrategy(Strategy):
    id = "macd_ma"
    name = "MACD金叉 + 站上MA20"
    description = "DIF上穿DEA且收盘价高于MA20时买入；DIF下穿DEA时卖出。"
    params_schema = [
        {"key": "ma", "label": "均线周期", "type": "number", "default": 20},
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict) -> list[dict]:
        ma_w = int(params.get("ma", 20))
        d = add_macd(add_ma(df, windows=(ma_w,)))
        ma, dif, dea, close = d[f"ma{ma_w}"], d["dif"], d["dea"], d["close"]
        ok = ma.notna() & dif.shift().notna()
        buy = ((dif > dea) & (dif.shift() <= dea.shift()) & (close > ma) & ok).to_numpy()
        sell = ((dif < dea) & (dif.shift() >= dea.shift()) & ok).to_numpy()
        return _signals(d["date"], buy, sell,
                        "MACD金叉且站上MA%d" % ma_w, "MACD死叉")


class DualMaStrategy(Strategy):
    id = "dual_ma"
    name = "双均线金叉死叉"
    description = "短期均线上穿长期均线买入，下穿卖出。"
    params_schema = [
        {"key": "fast", "label": "快线", "type": "number", "default": 5},
        {"key": "slow", "label": "慢线", "type": "number", "default": 20},
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict) -> list[dict]:
        fast, slow = int(params.get("fast", 5)), int(params.get("slow", 20))
        d = add_ma(df, windows=(fast, slow))
        f, s = d[f"ma{fast}"], d[f"ma{slow}"]
        ok = f.shift().notna() & s.shift().notna()
        buy = ((f > s) & (f.shift() <= s.shift()) & ok).to_numpy()
        sell = ((f < s) & (f.shift() >= s.shift()) & ok).to_numpy()
        return _signals(d["date"], buy, sell,
                        f"MA{fast}上穿MA{slow}", f"MA{fast}下穿MA{slow}")


class TurtleStrategy(Strategy):
    id = "turtle"
    name = "海龟交易法"
    description = "突破N日最高价买入(唐奇安通道)，跌破M日最低价卖出。"
    params_schema = [
        {"key": "entry", "label": "入场突破天数", "type": "number", "default": 20},
        {"key": "exit", "label": "离场跌破天数", "type": "number", "default": 10},
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict) -> list[dict]:
        entry, ex = int(params.get("entry", 20)), int(params.get("exit", 10))
        hi = df["high"].rolling(entry).max().shift(1)
        lo = df["low"].rolling(ex).min().shift(1)
        ok = hi.notna() & lo.notna()
        buy = ((df["close"] > hi) & ok).to_numpy()
        sell = ((df["close"] < lo) & ok).to_numpy()
        return _signals(df["date"], buy, sell,
                        f"突破{entry}日新高", f"跌破{ex}日新低")


class BollBreakStrategy(Strategy):
    id = "boll_break"
    name = "布林带突破"
    description = "收盘价上穿上轨买入，跌破中轨卖出。"
    params_schema = [
        {"key": "window", "label": "布林周期", "type": "number", "default": 20},
    ]

    def generate_signals(self, df: pd.DataFrame, params: dict) -> list[dict]:
        w = int(params.get("window", 20))
        d = add_boll(df, window=w)
        up, mid, close = d["boll_up"], d["boll_mid"], d["close"]
        prev_up, prev_mid, prev_close = up.shift(), mid.shift(), close.shift()
        # 中轨的上一日值缺失时按 +inf 处理，于是「上穿」不成立，与原来一致
        prev_ge = (prev_close >= prev_mid.fillna(np.inf)).to_numpy()
        prev_above = (prev_up.notna() & (prev_close > prev_up)).to_numpy()
        valid = up.notna().to_numpy()
        buy = (close > up).to_numpy() & ~prev_above & valid
        sell = (close < mid).to_numpy() & prev_ge & valid
        return _signals(d["date"], buy, sell, "突破布林上轨", "跌破布林中轨")


class KdjStrategy(Strategy):
    id = "kdj_cross"
    name = "KDJ低位金叉"
    description = "K值20以下金叉买入，K值80以上死叉卖出。"
    params_schema = []

    def generate_signals(self, df: pd.DataFrame, params: dict) -> list[dict]:
        d = add_kdj(df)
        k, dd = d["k"], d["d"]
        ok = k.shift().notna() & dd.shift().notna()
        gu = (k > dd) & (k.shift() <= dd.shift())
        gd = (k < dd) & (k.shift() >= dd.shift())
        buy = (gu & (k < 30) & ok).to_numpy()
        sell = (gd & (k > 70) & ok).to_numpy()
        return _signals(d["date"], buy, sell, "KDJ低位金叉", "KDJ高位死叉")
