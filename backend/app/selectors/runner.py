"""选股器的运行、配置与留痕。

留痕写在 select_result：主键 (trade_date, selector_id, code)，同一天重跑同一个选股器会整批覆盖；
每行把命中当时的展示字段一起存进 attrs，隔天回看历史名单不会被今日行情改掉。
"""
from __future__ import annotations

import datetime as dt
import json

from .. import db, screening
from . import discover
from .base import resolve_params

CONFIG_KEY = "selector_config"

# 命中当时抄一份留档，页面表格直接用这些列
ATTR_FIELDS = [
    "code", "name", "market", "industry", "price", "pct_chg", "total_mv", "float_mv",
    "pe_ttm", "pb", "roe_weighted", "net_profit_yoy", "turnover_rate", "amount",
    "trade_date", "kline_date",
]


def config() -> dict:
    raw = db.kv_get(CONFIG_KEY) or {}
    return {"enabled": list(raw.get("enabled") or []), "params": dict(raw.get("params") or {})}


def save_config(enabled: list[str], params: dict[str, dict]) -> None:
    valid = set(discover())
    db.kv_set(CONFIG_KEY, {
        "enabled": [i for i in enabled if i in valid],
        "params": {i: p for i, p in params.items() if i in valid and isinstance(p, dict)},
    })


def list_meta() -> list[dict]:
    cfg = config()
    enabled = cfg["enabled"]
    out = []
    for sid, sel in discover().items():
        m = sel.meta()
        m["enabled"] = sid in enabled if enabled else True
        m["params"] = resolve_params(sel.params_schema, cfg["params"].get(sid))
        m["last"] = _last_run(sid)
        out.append(m)
    return out


def run_one(sel, overrides: dict | None = None, persist: bool = True) -> dict:
    params = resolve_params(sel.params_schema, overrides)
    universe = screening.load_universe()
    trade_date = _base_date(universe)
    hits = sel.select(universe, params)
    rows = []
    for h in hits:
        ctx = universe.get(h.get("code"))
        if not ctx:
            continue
        attrs = {k: ctx.get(k) for k in ATTR_FIELDS}
        attrs["chg_20d"] = (ctx.get("tech") or {}).get("chg_20d")
        attrs["vol_ratio"] = (ctx.get("tech") or {}).get("vol_ratio")
        rows.append({"code": ctx.get("code") or h["code"],
                     "score": round(float(h.get("score") or 0), 1),
                     "reason": h.get("reason") or "",
                     "attrs": attrs})
    rows.sort(key=lambda r: (-r["score"], r["code"]))
    if persist:
        _replace(trade_date, sel.id, params, rows)
    return {"selector_id": sel.id, "name": sel.name, "trade_date": trade_date,
            "params": params, "count": len(rows), "rows": rows}


def run_all(only_enabled: bool = True, overrides_by_id: dict[str, dict] | None = None) -> list[dict]:
    cfg = config()
    out = []
    for sid, sel in discover().items():
        if only_enabled:
            enabled = cfg["enabled"]
            if enabled and sid not in enabled:
                continue
        out.append(run_one(sel, (overrides_by_id or {}).get(sid) or cfg["params"].get(sid)))
    return out


def dates(selector_id: str) -> list[str]:
    rows = db.query(
        "SELECT DISTINCT trade_date FROM select_result WHERE selector_id=? "
        "ORDER BY trade_date DESC LIMIT 60", (selector_id,))
    return [r["trade_date"] for r in rows]


def load(selector_id: str, trade_date: str = "") -> dict:
    """取留痕：不给日期就取最近一次。"""
    if trade_date:
        day = trade_date
    else:
        row = db.query_one(
            "SELECT MAX(trade_date) AS d FROM select_result WHERE selector_id=?", (selector_id,))
        day = (row or {}).get("d") or ""
    if not day:
        return {"selector_id": selector_id, "trade_date": "", "params": {},
                "count": 0, "rows": [], "run_at": ""}
    recs = db.query(
        "SELECT code, score, reason, attrs, params, run_at FROM select_result "
        "WHERE selector_id=? AND trade_date=? ORDER BY score DESC, code", (selector_id, day))
    rows = [{"code": r["code"], "score": r["score"], "reason": r["reason"],
             "attrs": json.loads(r["attrs"] or "{}")} for r in recs]
    params = json.loads(recs[0]["params"]) if recs and recs[0]["params"] else {}
    return {"selector_id": selector_id, "trade_date": day, "params": params,
            "count": len(rows), "rows": rows,
            "run_at": recs[0]["run_at"] if recs else ""}


def _last_run(selector_id: str) -> dict:
    row = db.query_one(
        "SELECT trade_date, COUNT(*) AS n, MAX(run_at) AS run_at FROM select_result "
        "WHERE selector_id=? AND trade_date=(SELECT MAX(trade_date) FROM select_result WHERE selector_id=?)",
        (selector_id, selector_id))
    if not row or not row["trade_date"]:
        return {}
    return {"trade_date": row["trade_date"], "count": row["n"], "run_at": row["run_at"]}


def _base_date(universe: dict[str, dict]) -> str:
    """这批名单的数据基准日：所有股票里最新的末根K线日期。"""
    days = [ctx.get("kline_date") for ctx in universe.values() if ctx.get("kline_date")]
    return max(days) if days else dt.date.today().isoformat()


def _replace(trade_date: str, selector_id: str, params: dict, rows: list[dict]) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute("DELETE FROM select_result WHERE trade_date=? AND selector_id=?",
                     (trade_date, selector_id))
        if rows:
            conn.executemany(
                "INSERT INTO select_result (trade_date, selector_id, code, name, score, reason,"
                " attrs, params, run_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [(trade_date, selector_id, r["code"], r["attrs"].get("name") or "",
                  r["score"], r["reason"], json.dumps(r["attrs"], ensure_ascii=False),
                  json.dumps(params, ensure_ascii=False), now) for r in rows])
