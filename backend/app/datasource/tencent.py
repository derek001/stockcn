"""Tencent (腾讯财经) fallback data source.

Used when Eastmoney's quote servers throttle this client. Provides:
- industry boards + constituents + per-stock valuation snapshot
- daily qfq klines via backward windowed paging (640 bars/call)
- broad market index klines

The gtimg/qq endpoints sit behind a Tencent WAF that answers HTTP 501 with a
JS interstitial once we ask too fast, so every request goes through a global
pace limiter and blocks are reported as Blocked for the callers to back off.
"""
from __future__ import annotations

import json
import os
import threading
import time

import requests

from .eastmoney import _UA  # same UA

GTIMG = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
QT = "https://qt.gtimg.cn"
PROXY = "https://proxy.finance.qq.com/cgi/cgi-bin/rank"

HEADERS = {"User-Agent": _UA, "Referer": "https://gu.qq.com/"}

# requests/second ceiling shared by all worker threads. 1.0s is the rate that
# ran overnight without a single WAF rejection; 0.4s got the IP banned twice.
MIN_INTERVAL = float(os.getenv("STOCKCN_HTTP_INTERVAL", "1.0"))
# first pause after a WAF rejection, doubling up to MAX_BACKOFF
BLOCK_BACKOFF = float(os.getenv("STOCKCN_BLOCK_BACKOFF", "60"))
MAX_BACKOFF = 900.0
# 一次 DNS 抖动 / 连接重置不该打断整轮刷新（全量刷新要跑几十分钟）
GET_TRIES = int(os.getenv("STOCKCN_HTTP_TRIES", "4"))
TRANSIENT = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)

INDEXES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000688": "科创50",
    "bj899050": "北证50",
}


class Blocked(RuntimeError):
    """The CDN rejected us for rate limiting; retry later, not harder."""


_pace_lock = threading.Lock()
_next_request_at = 0.0
_cooldown_until = 0.0
_block_streak = 0


def _pace() -> None:
    """Serialize outbound requests to MIN_INTERVAL and honour any WAF cooldown."""
    global _next_request_at
    while True:
        with _pace_lock:
            wait = max(_next_request_at, _cooldown_until) - time.monotonic()
            if wait <= 0:
                _next_request_at = time.monotonic() + MIN_INTERVAL
                return
        time.sleep(min(wait, 2.0))


def _note_block(status: int) -> None:
    """Escalate once per block episode; rejections during a pause are ignored."""
    global _block_streak, _cooldown_until
    with _pace_lock:
        now = time.monotonic()
        if now < _cooldown_until:
            return
        _block_streak += 1
        delay = min(BLOCK_BACKOFF * 2 ** (_block_streak - 1), MAX_BACKOFF)
        _cooldown_until = now + delay
    print(f"[tencent] rate limited (HTTP {status}), pausing {delay:.0f}s", flush=True)


def pause_remaining() -> float:
    with _pace_lock:
        return max(0.0, _cooldown_until - time.monotonic())


def _get_text(url: str, params: dict, timeout: int = 20) -> str:
    global _block_streak
    host = url.split("/")[2]
    for attempt in range(GET_TRIES):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=HEADERS)
            break
        except TRANSIENT as e:
            if attempt == GET_TRIES - 1:
                raise
            wait = min(2.0 * (attempt + 1), 8.0)
            print(f"[tencent] {host} 瞬时网络错误（{type(e).__name__}），"
                  f"{wait:.0f}s 后重试 {attempt + 1}/{GET_TRIES - 1}", flush=True)
            time.sleep(wait)
    body = r.text or ""
    if r.status_code in (401, 403, 429, 501) or body.lstrip().startswith("<"):
        _note_block(r.status_code)
        raise Blocked(f"rate limited by {url.split('/')[2]} (HTTP {r.status_code})")
    r.raise_for_status()
    with _pace_lock:
        _block_streak = 0
    return body


def _get(url: str, params: dict, timeout: int = 20):
    return json.loads(_get_text(url, params, timeout))


def _num(v):
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def fetch_industry_boards() -> list[dict]:
    boards: list[dict] = []
    offset = 0
    while True:
        js = _get(f"{PROXY}/pt/getRank", {
            "board_type": "hy", "sort_type": "price", "direct": "down",
            "offset": offset, "count": 50,
        })
        items = (js.get("data") or {}).get("rank_list") or []
        if not items:
            break
        for it in items:
            boards.append({
                "board_code": str(it.get("code")),
                "board_name": str(it.get("name")),
                "board_type": "industry",
            })
        if len(items) < 50:
            break
        offset += 50
    return boards


def fetch_board_stocks(board_code: str) -> list[dict]:
    """Constituents with valuation snapshot: code,name,pe_ttm,pb,float_mv,total_mv,price."""
    out: list[dict] = []
    offset = 0
    while True:
        js = _get(f"{PROXY}/hs/getBoardRankList", {
            "board_code": board_code, "sort_type": "price", "direct": "down",
            "offset": offset, "count": 50,
        })
        items = (js.get("data") or {}).get("rank_list") or []
        if not items:
            break
        for it in items:
            code = str(it.get("code") or "")
            if len(code) < 7:
                continue
            out.append({
                "sec_code": code,                       # e.g. sh600519
                "code": code[2:],
                "name": str(it.get("name") or ""),
                "price": _num(it.get("zxj")),
                "pct_chg": _num(it.get("zdf")),
                "pe_ttm": _num(it.get("pe_ttm")),
                # gtimg names 市净率 pn and 换手率 hsl; 成交额 turnover is in 万元
                "pb": _num(it.get("pn")),
                "turnover_rate": _num(it.get("hsl")),
                "amount": (_num(it.get("turnover")) or 0) * 1e4 or None,
                "float_mv": (_num(it.get("ltsz")) or 0) * 1e8,
                "total_mv": (_num(it.get("zsz")) or 0) * 1e8,
            })
        if len(items) < 50:
            break
        offset += 50
    return out


# qt.gtimg.cn 批量行情字段下标
_Q_NAME, _Q_PRICE, _Q_DATE, _Q_PCT, _Q_AMT, _Q_TURN, _Q_PE, _Q_FLOAT, _Q_TOTAL, _Q_PB = (
    1, 3, 30, 32, 37, 38, 39, 44, 45, 46)
QUOTE_BATCH = 60


def fetch_quotes(items: list[tuple[str, str]]) -> dict[str, dict]:
    """[(code, market)] -> {code: 基本面快照}，走 qt.gtimg.cn 的批量行情。

    板块排行接口只有沪深A股，这个接口连科创板和北交所都答，补录的那批股票靠它取快照。
    """
    out: dict[str, dict] = {}
    syms = [f"{m.lower()}{c}" for c, m in items]
    for i in range(0, len(syms), QUOTE_BATCH):
        chunk = syms[i:i + QUOTE_BATCH]
        body = _get_text(f"{QT}/q={','.join(chunk)}", {})
        for line in body.split(";"):
            fields = line.partition("=")[2].strip().strip('"').split("~")
            if len(fields) <= _Q_PB or not fields[2]:
                continue
            d = fields[_Q_DATE]
            out[fields[2]] = {
                "name": fields[_Q_NAME],
                "trade_date": f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else None,
                "price": _num(fields[_Q_PRICE]),
                "pct_chg": _num(fields[_Q_PCT]),
                # 成交额以万元计、市值以亿元计，库内统一为元
                "amount": (_num(fields[_Q_AMT]) or 0) * 1e4 or None,
                "turnover_rate": _num(fields[_Q_TURN]),
                "pe_ttm": _num(fields[_Q_PE]),
                "float_mv": (_num(fields[_Q_FLOAT]) or 0) * 1e8 or None,
                "total_mv": (_num(fields[_Q_TOTAL]) or 0) * 1e8 or None,
                "pb": _num(fields[_Q_PB]),
            }
    return out


def market_trade_date() -> str | None:
    """最近一个交易日：读上证指数行情里的时间戳字段。

    休市日（周末、长假）不能拿 `date.today()` 当快照日期——导入后按「快照与末根同日」
    回填涨跌幅会因此整批跳过。
    """
    try:
        body = _get_text(f"{QT}/q=sh000001", {})
    except Exception:
        return None
    fields = body.partition("=")[2].strip().strip('"').split("~")
    d = fields[_Q_DATE] if len(fields) > _Q_DATE else ""
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else None


def fetch_kline_range(tcode: str, start: str, end: str = "2050-01-01") -> list[dict]:
    """Daily hfq bars within [start,end]."""
    js = _get(GTIMG, {"param": f"{tcode},day,{start},{end},640,hfq"})
    data = ((js.get("data") or {}).get(tcode)) or {}
    kl = data.get("hfqday") or data.get("day") or []
    rows = []
    for p in kl:
        if len(p) < 6:
            continue
        rows.append({
            "date": str(p[0]),
            "open": _num(p[1]), "close": _num(p[2]),
            "high": _num(p[3]), "low": _num(p[4]),
            "volume": (_num(p[5]) or 0) * 100.0,
            "amount": None, "pct_chg": None, "turnover": None,
        })
    return rows


def fetch_kline(tcode: str, days_limit: int = 100000) -> list[dict]:
    """Full daily hfq history, paging backwards (server returns last<=640 bars of range)."""
    by_date: dict[str, dict] = {}
    end = "2050-01-01"
    while True:
        js = _get(GTIMG, {"param": f"{tcode},day,1990-01-01,{end},640,hfq"})
        data = ((js.get("data") or {}).get(tcode)) or {}
        kl = data.get("hfqday") or data.get("day") or []
        bar_rows = []
        for p in kl:
            if len(p) < 6:
                continue
            bar_rows.append({
                "date": str(p[0]),
                "open": _num(p[1]), "close": _num(p[2]),
                "high": _num(p[3]), "low": _num(p[4]),
                "volume": (_num(p[5]) or 0) * 100.0,   # 手 -> 股
                "amount": None, "pct_chg": None, "turnover": None,
            })
        new_count = 0
        for b in bar_rows:
            if b["date"] not in by_date:
                by_date[b["date"]] = b
                new_count += 1
        if not bar_rows or len(kl) < 640 or new_count == 0:
            break
        end = bar_rows[0]["date"]
    rows = [by_date[d] for d in sorted(by_date)]
    # pct_chg from closes
    for i in range(1, len(rows)):
        pc = rows[i - 1]["close"]
        if pc:
            rows[i]["pct_chg"] = round((rows[i]["close"] - pc) / pc * 100, 4)
    return rows
