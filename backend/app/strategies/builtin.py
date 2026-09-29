"""Built-in strategies (each is a plugin: subclass Strategy with an `id`)."""
from __future__ import annotations

import pandas as pd

from ..indicators import add_boll, add_kdj, add_ma, add_macd
from .base import Strategy


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
        out = []
        for i in range(1, len(d)):
            if pd.isna(d["ma" + str(ma_w)].iloc[i]) or pd.isna(d["dif"].iloc[i - 1]):
                continue
            cross_up = d["dif"].iloc[i] > d["dea"].iloc[i] and \
                d["dif"].iloc[i - 1] <= d["dea"].iloc[i - 1]
            cross_dn = d["dif"].iloc[i] < d["dea"].iloc[i] and \
                d["dif"].iloc[i - 1] >= d["dea"].iloc[i - 1]
            if cross_up and d["close"].iloc[i] > d["ma" + str(ma_w)].iloc[i]:
                out.append({"date": d["date"].iloc[i], "side": "buy",
                            "reason": "MACD金叉且站上MA%d" % ma_w})
            elif cross_dn:
                out.append({"date": d["date"].iloc[i], "side": "sell",
                            "reason": "MACD死叉"})
        return out


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
        f, s = f"ma{fast}", f"ma{slow}"
        out = []
        for i in range(1, len(d)):
            if pd.isna(d[f].iloc[i - 1]) or pd.isna(d[s].iloc[i - 1]):
                continue
            if d[f].iloc[i] > d[s].iloc[i] and d[f].iloc[i - 1] <= d[s].iloc[i - 1]:
                out.append({"date": d["date"].iloc[i], "side": "buy",
                            "reason": f"MA{fast}上穿MA{slow}"})
            elif d[f].iloc[i] < d[s].iloc[i] and d[f].iloc[i - 1] >= d[s].iloc[i - 1]:
                out.append({"date": d["date"].iloc[i], "side": "sell",
                            "reason": f"MA{fast}下穿MA{slow}"})
        return out


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
        out = []
        for i in range(1, len(df)):
            if pd.isna(hi.iloc[i]) or pd.isna(lo.iloc[i]):
                continue
            if df["close"].iloc[i] > hi.iloc[i]:
                out.append({"date": df["date"].iloc[i], "side": "buy",
                            "reason": f"突破{entry}日新高"})
            elif df["close"].iloc[i] < lo.iloc[i]:
                out.append({"date": df["date"].iloc[i], "side": "sell",
                            "reason": f"跌破{ex}日新低"})
        return out


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
        out = []
        for i in range(1, len(d)):
            if pd.isna(d["boll_up"].iloc[i]):
                continue
            prev_above = not pd.isna(d["boll_up"].iloc[i - 1]) and \
                d["close"].iloc[i - 1] > d["boll_up"].iloc[i - 1]
            if d["close"].iloc[i] > d["boll_up"].iloc[i] and not prev_above:
                out.append({"date": d["date"].iloc[i], "side": "buy", "reason": "突破布林上轨"})
            elif d["close"].iloc[i] < d["boll_mid"].iloc[i] and \
                 d["close"].iloc[i - 1] >= (d["boll_mid"].iloc[i - 1] if not pd.isna(d["boll_mid"].iloc[i - 1]) else float("inf")):
                out.append({"date": d["date"].iloc[i], "side": "sell", "reason": "跌破布林中轨"})
        return out


class KdjStrategy(Strategy):
    id = "kdj_cross"
    name = "KDJ低位金叉"
    description = "K值20以下金叉买入，K值80以上死叉卖出。"
    params_schema = []

    def generate_signals(self, df: pd.DataFrame, params: dict) -> list[dict]:
        d = add_kdj(df)
        out = []
        for i in range(1, len(d)):
            if pd.isna(d["k"].iloc[i - 1]) or pd.isna(d["d"].iloc[i - 1]):
                continue
            gu = d["k"].iloc[i] > d["d"].iloc[i] and d["k"].iloc[i - 1] <= d["d"].iloc[i - 1]
            gd = d["k"].iloc[i] < d["d"].iloc[i] and d["k"].iloc[i - 1] >= d["d"].iloc[i - 1]
            if gu and d["k"].iloc[i] < 30:
                out.append({"date": d["date"].iloc[i], "side": "buy", "reason": "KDJ低位金叉"})
            elif gd and d["k"].iloc[i] > 70:
                out.append({"date": d["date"].iloc[i], "side": "sell", "reason": "KDJ高位死叉"})
        return out
