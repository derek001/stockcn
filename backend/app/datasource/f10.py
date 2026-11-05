"""东财 F10 报表抓取（资产负债表 + 现金流量表），只读，供行业库存/资本周期指标用。

接口口径（2026-11-05 实测）：
- `dates` 一次最多返回 5 个报告期（塞 10 个也只回前 5），所以 20 期 = 每表 4 次请求；
- 报告期清单按季末直接构造，不用先问 date 接口——未披露的期自动少回行，新股回 0 行；
- `companyType=4`（工商）对非金融票通用；银行/券商/保险报表口径不同，不在本模块抓取范围。
"""
from __future__ import annotations

import time
from datetime import date

import requests

from .eastmoney import _UA, market_for

BASE = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/"
HDRS = {"User-Agent": _UA, "Referer": "https://emweb.securities.eastmoney.com/",
        "Accept": "application/json, text/plain, */*"}
BATCH = 5            # 接口上限：一次 5 个报告期
RETRIES = 3
SLEEP = 0.2          # 请求间歇，别把公开接口打爆

# 只取库存/资本周期用得到的列，其余 300 多个字段丢掉
BS_FIELDS = {
    "INVENTORY": "inventory",            # 存货
    "CIP": "cip",                        # 在建工程
    "FIXED_ASSET": "fixed_asset",        # 固定资产
    "TOTAL_ASSETS": "total_assets",
    "ACCOUNTS_RECE": "accounts_rece",    # 应收账款
    "CONTRACT_LIAB": "contract_liab",    # 合同负债（下游先付钱＝需求前瞻）
    "MONETARYFUNDS": "monetary_funds",
}
CF_FIELDS = {
    "CONSTRUCT_LONG_ASSET": "capex_cash",   # 购建固定资产等支付的现金
    "NETCASH_OPERATE": "operate_cash",      # 经营活动现金流净额
}
TABLES = {
    "balance": ("zcfzbAjaxNew", "zcfzbList", BS_FIELDS),
    "cash": ("xjllbAjaxNew", "xjllbList", CF_FIELDS),
}


def f10_code(code: str) -> str:
    """6 位代码 → F10 要的 `SH600519` / `BJ920306` 形式。"""
    return market_for(code) + code


def quarter_ends(as_of: date, periods: int = 20) -> list[str]:
    """截至 as_of 的最后 `periods` 个季末报告期，新→旧。"""
    all_q = [date(y, m, d) for y in range(as_of.year, 1999, -1)
             for m, d in ((12, 31), (9, 30), (6, 30), (3, 31))]
    return [q.isoformat() for q in [x for x in all_q if x <= as_of][:periods]]


def _f(v):
    if v is None or v in ("", "-", "false"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick(row: dict, fields: dict) -> dict:
    out = {"report_date": str(row["REPORT_DATE"])[:10]}
    for src, dst in fields.items():
        out[dst] = _f(row.get(src))
    return out


def _fetch_table(path: str, list_key: str, fields: dict,
                 code: str, dates: list[str]) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    for i in range(0, len(dates), BATCH):
        chunk = dates[i:i + BATCH]
        params = {"companyType": "4", "reportDateType": "0", "reportType": "1",
                  "dates": ",".join(chunk), "code": code}
        got = None
        for attempt in range(RETRIES):
            try:
                r = requests.get(BASE + path, params=params, headers=HDRS, timeout=20)
                if r.status_code == 200:
                    data = r.json().get("data")
                    got = (data.get(list_key) if isinstance(data, dict) else data) or []
                    break
            except Exception:
                pass
            time.sleep(0.5 * (attempt + 1))
        if got is None:
            errors.append(f"{path} {chunk[0]}~{chunk[-1]}")
        else:
            rows += [_pick(x, fields) for x in got]
        time.sleep(SLEEP)
    return rows, errors


def fetch_stock(code: str, dates: list[str], tables: tuple[str, ...] = ("balance", "cash")) -> dict:
    """抓一只票的若干张表。返回 {"tables": {名: [行]}, "errors": [批次说明]}。"""
    f10 = f10_code(code)
    out: dict[str, list[dict]] = {}
    errors: list[str] = []
    for name in tables:
        path, key, fields = TABLES[name]
        rows, errs = _fetch_table(path, key, fields, f10, dates)
        out[name] = rows
        errors += errs
    return {"tables": out, "errors": errors}
