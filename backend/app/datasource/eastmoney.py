"""Eastmoney (东方财富) HTTP data source client.

AkShare wraps these same endpoints but its default user-agent is rejected by
the CDN from this network, so we call the JSON APIs directly.
"""
from __future__ import annotations

import random
import time
import requests

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

PUSH2 = "https://push2.eastmoney.com/api/qt/clist/get"
PUSH2HIS = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# whole-market A-share stock filter used by clist
FS_ALL_STOCKS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
FS_INDUSTRY_BOARDS = "m:90+t:2+f:!50"
# SECURITY_TYPE_CODE of A股 in the datacenter reports (drops 三板股/B股)
TYPE_CODE_ASHARE = "058001001"

STOCK_FIELDS = "f2,f3,f6,f8,f9,f12,f13,f14,f20,f21,f23,f24,f25,f26,f100,f115"
BOARD_FIELDS = "f12,f14,f2,f3,f20,f104,f105"
KLINE_FIELDS1 = "f1,f2,f3,f4,f5,f6"
KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

INDEXES = {
    "sh000001": ("1.000001", "上证指数"),
    "sz399001": ("0.399001", "深证成指"),
    "sz399006": ("0.399006", "创业板指"),
    "sh000688": ("1.000688", "科创50"),
    "bj899050": ("0.899050", "北证50"),
}


def _get_json(url: str, params: dict, retries: int = 3, timeout: int = 20):
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers={
                    "User-Agent": _UA,
                    "Referer": "https://quote.eastmoney.com/",
                    "Accept": "application/json, text/plain, */*",
                },
            )
            if r.status_code == 200:
                return r.json()
            last_err = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.5 * (attempt + 1) + random.random() * 0.3)
    raise RuntimeError(f"request failed after {retries} retries: {url} ({last_err})")


def _num(v):
    if v in (None, "-", "", "false"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def market_for(code: str) -> str:
    # 920xxx 是北交所新代码段，不能按 9 前缀一概判为沪市
    if code.startswith(("4", "8", "92")):
        return "BJ"
    if code.startswith(("6", "9", "5")):
        return "SH"
    return "SZ"


def secid_for(code: str, name: str = "") -> str:
    """Eastmoney secid for a 6-digit A-share code."""
    return ("1." if market_for(code) == "SH" else "0.") + code


def index_secid_for(code: str) -> str:
    """Secid of the broad-market index that matches a stock's market."""
    market = market_for(code)
    if market == "SH":
        return "1.000001"          # 上证指数
    if market == "BJ":
        return "0.899050"          # 北证50
    return "0.399006" if code.startswith("3") else "0.399001"


def fetch_all_stocks() -> list[dict]:
    """Stock list with fundamental snapshot (one clist query per page)."""
    out: list[dict] = []
    pn = 1
    pz = 200
    while True:
        js = _get_json(PUSH2, {
            "pn": pn, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fs": FS_ALL_STOCKS, "fields": STOCK_FIELDS,
        })
        data = (js or {}).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        for it in diff:
            code = str(it.get("f12") or "")
            ld = it.get("f26")
            list_date = None
            if isinstance(ld, (int, float)) and 19900101 <= ld <= 21000101:
                s = str(int(ld))
                list_date = f"{s[:4]}-{s[4:6]}-{s[6:8]}"
            out.append({
                "code": code,
                "name": str(it.get("f14") or ""),
                "market": market_for(code),
                "secid": secid_for(code),
                "industry": (str(it["f100"]) if it.get("f100") not in (None, "-") else None),
                "list_date": list_date,
                "price": _num(it.get("f2")),
                "pct_chg": _num(it.get("f3")),
                "amount": _num(it.get("f6")),
                "turnover_rate": _num(it.get("f8")),
                "pe_dynamic": _num(it.get("f9")),
                "pe_ttm": _num(it.get("f115")),
                "total_mv": _num(it.get("f20")),
                "float_mv": _num(it.get("f21")),
                "pb": _num(it.get("f23")),
            })
        total = int(data.get("total") or 0)
        if pn * pz >= total:
            break
        pn += 1
    return out


def fetch_kline(secid: str, beg: str = "0", end: str = "20500101") -> list[dict]:
    """Daily hfq (后复权) klines. Returns [{date,open,close,high,low,volume,amount,pct_chg,turnover}]."""
    js = _get_json(PUSH2HIS, {
        "secid": secid, "klt": "101", "fqt": "2",
        "beg": beg, "end": end, "lmt": "100000", "iscca": "1",
        "fields1": KLINE_FIELDS1, "fields2": KLINE_FIELDS2, "fltt": "2", "invt": "2",
    })
    data = (js or {}).get("data") or {}
    klines = data.get("klines") or []
    rows = []
    for line in klines:
        p = line.split(",")
        if len(p) < 8:
            continue
        rows.append({
            "date": p[0], "open": _num(p[1]), "close": _num(p[2]),
            "high": _num(p[3]), "low": _num(p[4]), "volume": _num(p[5]),
            "amount": _num(p[6]), "pct_chg": _num(p[8]) if len(p) > 8 else None,
            "turnover": _num(p[10]) if len(p) > 10 else None,
        })
    return rows


def fetch_industry_boards() -> list[dict]:
    out: list[dict] = []
    pn = 1
    while True:
        js = _get_json(PUSH2, {
            "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fs": FS_INDUSTRY_BOARDS, "fields": BOARD_FIELDS,
        })
        data = (js or {}).get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        for it in diff:
            out.append({
                "board_code": str(it.get("f12")),
                "board_name": str(it.get("f14")),
                "board_type": "industry",
            })
        total = int(data.get("total") or 0)
        if pn * 100 >= total:
            break
        pn += 1
    return out


def fetch_financial_report(report_date: str) -> list[dict]:
    """Quarterly performance report (业绩报表) for one REPORTDATE, all pages."""
    out: list[dict] = []
    pn = 1
    while True:
        js = _get_json(DATACENTER, {
            "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
            "filter": f"(REPORTDATE='{report_date}')",
            "pageNumber": pn, "pageSize": 500,
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
        })
        result = (js or {}).get("result") or {}
        items = result.get("data") or []
        if not items:
            break
        for it in items:
            out.append({
                "code": str(it.get("SECURITY_CODE")),
                "report_date": str(it.get("REPORTDATE") or report_date)[:10],
                "eps": _num(it.get("BASIC_EPS")),
                "revenue": _num(it.get("TOTAL_OPERATE_INCOME")),
                "revenue_yoy": _num(it.get("YSTZ")),
                "net_profit": _num(it.get("PARENT_NETPROFIT")),
                "net_profit_yoy": _num(it.get("SJLTZ")),
                "roe_weighted": _num(it.get("WEIGHTAVG_ROE")),
                "gross_margin": _num(it.get("XSMLL")),
            })
        pages = int(result.get("pages") or 1)
        if pn >= pages:
            break
        pn += 1
    return out


ROSTER_COLUMNS = ("SECURITY_CODE,SECURITY_NAME_ABBR,SECUCODE,TRADE_MARKET,BOARD_NAME")
_MARKET_BY_SUFFIX = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}


def fetch_stock_roster(report_dates: list[str]) -> list[dict]:
    """A-share roster (code,name,market,industry) read off the 业绩报表 rows.

    The datacenter host answers even when push2's quote servers reject us, and
    unlike Tencent's board rank (沪深A股 only) it carries 科创板 and 北交所 names.
    The market comes from SECUCODE's suffix rather than the code prefix, so
    920xxx lands in BJ. A stock appears once it has filed for one of
    `report_dates`, hence the caller passes more than one quarter.
    """
    out: dict[str, dict] = {}
    for rd in report_dates:
        pn = 1
        while True:
            js = _get_json(DATACENTER, {
                "reportName": "RPT_LICO_FN_CPD", "columns": ROSTER_COLUMNS,
                "filter": f"(REPORTDATE='{rd}')(SECURITY_TYPE_CODE=\"{TYPE_CODE_ASHARE}\")",
                "pageNumber": pn, "pageSize": 500,
                "sortColumns": "SECURITY_CODE", "sortTypes": "1",
            })
            result = (js or {}).get("result") or {}
            items = result.get("data") or []
            if not items:
                break
            for it in items:
                code = str(it.get("SECURITY_CODE") or "")
                if len(code) != 6 or not code.isdigit() or code in out:
                    continue
                suffix = str(it.get("SECUCODE") or "").rsplit(".", 1)[-1].upper()
                out[code] = {
                    "code": code,
                    "name": str(it.get("SECURITY_NAME_ABBR") or ""),
                    "market": _MARKET_BY_SUFFIX.get(suffix, market_for(code)),
                    "industry": (str(it["BOARD_NAME"]) if it.get("BOARD_NAME") else None),
                }
            if pn >= int(result.get("pages") or 1):
                break
            pn += 1
    return list(out.values())


LISTING_COLUMNS = "SECURITY_CODE,SECURITY_NAME_ABBR,LISTING_DATE"


def fetch_listing_dates() -> dict[str, str]:
    """code -> 上市日期（YYYY-MM-DD），来自 F10 公司概况 RPT_F10_ORG_BASICINFO。

    push2 的全市场快照也带上市日期（f26），但那台服务器在本机是连接级封锁；
    datacenter 这台可达，翻 17 页拿到全部 A 股（约 8300 条，含已退市与未上市申报股）。
    答不出日期的股票不进结果，调用方因此不会把已有值抹成空。
    """
    out: dict[str, str] = {}
    pn = 1
    while True:
        js = _get_json(DATACENTER, {
            "reportName": "RPT_F10_ORG_BASICINFO", "columns": LISTING_COLUMNS,
            "filter": f'(SECURITY_TYPE_CODE="{TYPE_CODE_ASHARE}")',
            "pageNumber": pn, "pageSize": 500,
            "sortColumns": "SECURITY_CODE", "sortTypes": "1",
        })
        result = (js or {}).get("result") or {}
        items = result.get("data") or []
        if not items:
            break
        for it in items:
            code = str(it.get("SECURITY_CODE") or "")
            date = str(it.get("LISTING_DATE") or "")[:10]
            if len(code) == 6 and code.isdigit() and len(date) == 10:
                out[code] = date
        if pn >= int(result.get("pages") or 1):
            break
        pn += 1
    return out
