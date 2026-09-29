"""Technical indicators computed with pandas on per-stock daily qfq bars."""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_ma(df: pd.DataFrame, windows=(5, 10, 20, 60, 120, 250)) -> pd.DataFrame:
    for w in windows:
        df[f"ma{w}"] = df["close"].rolling(w).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    return df


def add_macd(df: pd.DataFrame, fast=12, slow=26, signal=9) -> pd.DataFrame:
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    df["dif"] = ema_fast - ema_slow
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()
    df["macd"] = (df["dif"] - df["dea"]) * 2
    return df


def add_boll(df: pd.DataFrame, window=20, num_std=2) -> pd.DataFrame:
    mid = df["close"].rolling(window).mean()
    std = df["close"].rolling(window).std()
    df["boll_mid"] = mid
    df["boll_up"] = mid + num_std * std
    df["boll_low"] = mid - num_std * std
    return df


def add_rsi(df: pd.DataFrame, window=14) -> pd.DataFrame:
    delta = df["close"].diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - 100 / (1 + rs)
    return df


def add_kdj(df: pd.DataFrame, n=9) -> pd.DataFrame:
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    df["k"] = rsv.ewm(com=2, adjust=False).mean()
    df["d"] = df["k"].ewm(com=2, adjust=False).mean()
    df["j"] = 3 * df["k"] - 2 * df["d"]
    return df


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index(drop=True)
    df = add_ma(df)
    df = add_macd(df)
    df = add_boll(df)
    df = add_rsi(df)
    df = add_kdj(df)
    df["high_250"] = df["high"].rolling(250, min_periods=60).max()
    df["low_250"] = df["low"].rolling(250, min_periods=60).min()
    return df


def _cross_up(a: pd.Series, b: pd.Series) -> bool:
    if len(a) < 2 or pd.isna(a.iloc[-1]) or pd.isna(b.iloc[-1]) \
       or pd.isna(a.iloc[-2]) or pd.isna(b.iloc[-2]):
        return False
    return a.iloc[-1] > b.iloc[-1] and a.iloc[-2] <= b.iloc[-2]


def _cross_down(a: pd.Series, b: pd.Series) -> bool:
    if len(a) < 2 or pd.isna(a.iloc[-1]) or pd.isna(b.iloc[-1]) \
       or pd.isna(a.iloc[-2]) or pd.isna(b.iloc[-2]):
        return False
    return a.iloc[-1] < b.iloc[-1] and a.iloc[-2] >= b.iloc[-2]


def tech_snapshot(df: pd.DataFrame) -> dict:
    """Latest-bar indicator values + boolean signals for rule evaluation."""
    if df is None or len(df) < 2:
        return {}
    last = df.iloc[-1]
    prev = df.iloc[-2]
    out = {
        "close": last["close"],
        "ma5": last["ma5"], "ma10": last["ma10"], "ma20": last["ma20"],
        "ma60": last["ma60"],
        "dif": last["dif"], "dea": last["dea"], "macd": last["macd"],
        "rsi": last["rsi"], "k": last["k"], "d": last["d"],
        "boll_up": last["boll_up"], "boll_mid": last["boll_mid"], "boll_low": last["boll_low"],
        "pct_chg": last["pct_chg"],
    }
    vol_ma_prev = prev["vol_ma5"] if not pd.isna(prev["vol_ma5"]) else None
    out["vol_ratio"] = (last["volume"] / vol_ma_prev) if vol_ma_prev else None
    out["chg_20d"] = (last["close"] / df["close"].iloc[-21] - 1) * 100 if len(df) > 21 else None
    out["chg_60d"] = (last["close"] / df["close"].iloc[-61] - 1) * 100 if len(df) > 61 else None

    sig = {}
    sig["macd_golden"] = _cross_up(df["dif"], df["dea"])
    sig["macd_dead"] = _cross_down(df["dif"], df["dea"])
    sig["macd_above_zero"] = (not pd.isna(df["dif"].iloc[-1])) and df["dif"].iloc[-1] > 0
    sig["boll_break_up"] = (not pd.isna(df["boll_up"].iloc[-1])) and df["close"].iloc[-1] > df["boll_up"].iloc[-1]
    sig["boll_break_down"] = (not pd.isna(df["boll_low"].iloc[-1])) and df["close"].iloc[-1] < df["boll_low"].iloc[-1]
    sig["ma_bullish"] = all([
        not pd.isna(df[f"ma{w}"].iloc[-1]) for w in (5, 10, 20, 60)
    ]) and df["ma5"].iloc[-1] > df["ma10"].iloc[-1] > df["ma20"].iloc[-1] > df["ma60"].iloc[-1]
    sig["ma20_up_cross"] = _cross_up(df["close"], df["ma20"])
    sig["kdj_golden"] = _cross_up(df["k"], df["d"])
    sig["new_high_250"] = (not pd.isna(df["high_250"].iloc[-2])) and df["close"].iloc[-1] >= df["high_250"].iloc[-2]
    sig["new_low_250"] = (not pd.isna(df["low_250"].iloc[-2])) and df["close"].iloc[-1] <= df["low_250"].iloc[-2]
    sig["vol_surge"] = out["vol_ratio"] is not None and out["vol_ratio"] >= 2.0
    out["signals"] = {k: bool(v) for k, v in sig.items()}
    # NaN -> None for JSON
    for k, v in list(out.items()):
        if isinstance(v, float) and pd.isna(v):
            out[k] = None
    return out
