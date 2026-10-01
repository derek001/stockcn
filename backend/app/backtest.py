"""Backtest engine + report assembly + Excel export."""
from __future__ import annotations

import datetime as dt
import io
import math
import time

import pandas as pd

from . import db
from .strategies.base import get_strategy


def _df_records(df: pd.DataFrame) -> list[dict]:
    out = df.to_dict(orient="records")
    for r in out:
        for k, v in list(r.items()):
            if isinstance(v, float) and pd.isna(v):
                r[k] = None
    return out


def load_bars(code: str, start: str, end: str) -> pd.DataFrame:
    """回测用的日线：价格取复权口径（价格 × 因子），量额保持交易所原值。

    回测必须用复权价，否则跨除权日会凭空亏掉一笔分红送转的市值。
    """
    rows = db.query(
        "SELECT date, open*adj_factor AS open, high*adj_factor AS high, "
        "low*adj_factor AS low, close*adj_factor AS close, "
        "volume, amount, pct_chg "
        "FROM kline_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        (code, start, end))
    return pd.DataFrame(rows)


def load_index_bars(index_code: str, start: str, end: str) -> list[dict]:
    rows = db.query(
        "SELECT index_code AS code, date, open, high, low, close, volume, amount, pct_chg "
        "FROM index_kline WHERE index_code=? AND date>=? AND date<=? ORDER BY date",
        (index_code, start, end))
    return rows


def synthesize_board_bars(board_code: str | None, industry: str | None,
                          start: str, end: str) -> list[dict]:
    """Industry index synthesized as equal-weighted cumulative return of
    locally available member stocks."""
    conds, args = [], []
    if board_code:
        conds.append("board_code=?")
        args.append(board_code)
    if industry:
        conds.append("industry=?")
        args.append(industry)
    if not conds:
        return []
    members = db.query(
        f"SELECT code FROM stocks WHERE {' OR '.join(conds)}", tuple(args))
    codes = sorted({m["code"] for m in members})
    if not codes:
        return []
    ph = ",".join("?" * len(codes))
    rows = db.query(
        f"SELECT k.code, k.date, k.close * k.adj_factor AS close FROM kline_daily k "
        f"WHERE k.code IN ({ph}) AND k.date>=? AND k.date<=? ORDER BY k.date",
        (*codes, start, end))
    if not rows:
        return []
    df = pd.DataFrame(rows).pivot(index="date", columns="code", values="close")
    df = df[df > 0].ffill(limit=5)
    df = df.dropna(axis=1, thresh=max(2, len(df) // 4))
    if df.empty or df.shape[1] == 0:
        return []
    # equal-weighted daily-return index of available members
    ret = df.pct_change(fill_method=None)
    level = 1000.0 * (1 + ret.mean(axis=1, skipna=True).fillna(0)).cumprod()
    out = []
    prev = None
    for date, val in level.items():
        if pd.isna(val):
            continue
        out.append({"date": date, "open": None, "high": None, "low": None,
                    "close": round(float(val), 2), "volume": None, "amount": None,
                    "pct_chg": round((float(val) / prev - 1) * 100, 4) if prev else None})
        prev = float(val)
    return out


def _range_pct(bars: list[dict]) -> float | None:
    closes = [b["close"] for b in bars if b.get("close")]
    if len(closes) < 2 or not closes[0]:
        return None
    return round((closes[-1] / closes[0] - 1) * 100, 2)


def run_backtest(code: str, start: str, end: str, capital: float,
                 fee_rate: float, strategy_id: str,
                 params: dict | None) -> dict:
    t0 = time.time()
    strategy = get_strategy(strategy_id)
    if strategy is None:
        raise ValueError(f"未知策略: {strategy_id}")
    df = load_bars(code, start, end)
    if df.empty or len(df) < 30:
        raise ValueError("该股票在区间内没有足够的本地K线数据，请先执行数据更新")
    signals = strategy.generate_signals(df, params or {})
    sig_by_date: dict[str, str] = {}
    reason_by_date = {}
    for s in signals:
        sig_by_date.setdefault(s["date"], s["side"])
        reason_by_date[s["date"]] = s.get("reason", "")

    cash = float(capital)
    qty = 0
    cost_basis = 0.0
    buy_fee = 0.0
    trades: list[dict] = []
    markers: list[dict] = []
    cur_buy = None

    for _, row in df.iterrows():
        date, price = row["date"], row["close"]
        side = sig_by_date.get(date)
        if side is None or price is None or (isinstance(price, float) and math.isnan(price)):
            continue
        if side == "buy" and qty == 0:
            lot = int(cash / (price * 100 * (1 + fee_rate))) * 100
            if lot >= 100:
                amount = lot * price
                fee = amount * fee_rate
                cash -= amount + fee
                qty, cost_basis, buy_fee = lot, amount, fee
                cur_buy = date
                trades.append({
                    "date": date, "side": "buy", "code": code, "qty": lot,
                    "price": round(price, 3), "amount": round(amount, 2),
                    "fee": round(fee, 2), "pnl": None, "pnl_pct": None,
                    "reason": reason_by_date.get(date, ""),
                })
                markers.append({"date": date, "type": "buy",
                                "price": round(price, 3)})
        elif side == "sell" and qty > 0:
            amount = qty * price
            fee = amount * fee_rate
            cash += amount - fee
            pnl = amount - fee - cost_basis - buy_fee
            trades.append({
                "date": date, "side": "sell", "code": code, "qty": qty,
                "price": round(price, 3), "amount": round(amount, 2),
                "fee": round(fee, 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl / (cost_basis + buy_fee) * 100, 2)
                if cost_basis + buy_fee else None,
                "reason": reason_by_date.get(date, ""),
            })
            markers.append({"date": date, "type": "sell", "price": round(price, 3)})
            qty = 0

    # liquidate open position at last close (marked separately)
    last = df.iloc[-1]
    final_equity = cash
    if qty > 0:
        amount = qty * last["close"]
        fee = amount * fee_rate
        pnl = amount - fee - cost_basis - buy_fee
        trades.append({
            "date": last["date"], "side": "sell", "code": code, "qty": qty,
            "price": round(float(last["close"]), 3), "amount": round(amount, 2),
            "fee": round(fee, 2), "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (cost_basis + buy_fee) * 100, 2)
            if cost_basis + buy_fee else None,
            "reason": "回测结束强制平仓",
        })
        markers.append({"date": last["date"], "type": "sell",
                        "price": round(float(last["close"]), 3)})
        final_equity = cash + amount - fee
    fees_total = sum(t["fee"] for t in trades)
    pnl_total = final_equity - capital

    stock = db.query_one("SELECT * FROM stocks WHERE code=?", (code,))
    from .datasource.eastmoney import index_secid_for, INDEXES
    idx_secid = index_secid_for(code)
    idx_code = next((k for k, v in INDEXES.items() if v[0] == idx_secid), "")
    idx_name = next((v[1] for v in INDEXES.values() if v[0] == idx_secid), idx_secid)
    index_bars = load_index_bars(idx_code, start, end)
    board_bars = synthesize_board_bars(
        stock["board_code"] if stock else None,
        stock["industry"] if stock else None, start, end)

    report_id = f"{code}_{strategy_id}_{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
    report = {
        "id": report_id,
        "code": code,
        "stock_name": stock["name"] if stock else code,
        "industry": stock["industry"] if stock else None,
        "board_code": stock["board_code"] if stock else None,
        "strategy": {"id": strategy_id, "name": strategy.name, "params": params or {}},
        "range": {"start": start, "end": end},
        "capital": capital,
        "fee_rate": fee_rate,
        "summary": {
            "elapsed_sec": round(time.time() - t0, 3),
            "order_count": len(trades),
            "trade_count": sum(1 for t in trades if t["side"] == "buy"),
            "final_equity": round(final_equity, 2),
            "pnl": round(pnl_total, 2),
            "pnl_pct": round(pnl_total / capital * 100, 2) if capital else None,
            "fees_total": round(fees_total, 2),
            "index_name": idx_name,
            "index_pct": _range_pct(index_bars),
            "industry_pct": _range_pct(board_bars),
        },
        "stock_bars": _df_records(df),
        "markers": markers,
        "index_bars": index_bars,
        "board_bars": board_bars,
        "trades": trades,
    }
    _REPORTS[report_id] = report
    while len(_REPORTS) > 20:
        _REPORTS.pop(next(iter(_REPORTS)))
    return report


_REPORTS: dict[str, dict] = {}


def get_report(report_id: str) -> dict | None:
    return _REPORTS.get(report_id)


def export_excel(report: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "汇总"
    s = report["summary"]
    bold = Font(bold=True)
    rows = [
        ("股票代码", report["code"]), ("股票名称", report["stock_name"]),
        ("所属行业", report["industry"]),
        ("交易策略", report["strategy"]["name"]),
        ("回测区间", f"{report['range']['start']} ~ {report['range']['end']}"),
        ("初始资金", report["capital"]), ("手续费率", report["fee_rate"]),
        ("回测耗时(秒)", s["elapsed_sec"]), ("买入笔数", s["trade_count"]),
        ("总委托笔数", s["order_count"]),
        ("期末权益", s["final_equity"]),
        ("盈亏金额(含手续费)", s["pnl"]),
        ("盈亏比例(%)", s["pnl_pct"]), ("手续费合计", s["fees_total"]),
        (f"大盘涨跌幅({s['index_name']},%)", s["index_pct"]),
        ("行业涨跌幅(%)", s["industry_pct"]),
    ]
    for i, (k, v) in enumerate(rows, 1):
        ws.cell(row=i, column=1, value=k).font = bold
        ws.cell(row=i, column=2, value=v)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 28

    ws2 = wb.create_sheet("交易明细")
    headers = ["交易日期", "交易股票", "方向", "交易数量(股)", "成交价(元)",
               "交易金额", "手续费", "盈亏金额", "盈亏比例(%)", "触发原因"]
    ws2.append(headers)
    for c in ws2[1]:
        c.font = bold
    code_name = f"{report['code']} {report['stock_name']}"
    for t in report["trades"]:
        ws2.append([t["date"], code_name, "买入" if t["side"] == "buy" else "卖出",
                    t["qty"], t["price"], t["amount"], t["fee"],
                    t["pnl"], t["pnl_pct"], t["reason"]])
    widths = [12, 18, 8, 14, 12, 14, 10, 12, 12, 24]
    for i, w in enumerate(widths):
        ws2.column_dimensions[chr(ord("A") + i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
