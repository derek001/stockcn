"""东财 F10 资本开支口径：混合档样本名单 + 回填 + 落库。

样本口径（2026-11-05 实测定案）：
- 分组键＝申万一级（`industry.first_level`）；银行/非银金融共 121 只整体剔除（报表口径不同）；
- 资产负债表档＝每行业按年报营收累计盖到 70% 的最少只数（自适应，实测 688 只，
  最少的行业 2 只＝石油石化，最多的 82 只＝基础化工）；
- 现金流量表档＝每行业营收前 10（实测 290 只）；两档去重 722 只；
- 一次请求最多带 5 个报告期，所以 20 期＝每表 4 次请求，合计约 3,912 次、实测 0.81s/次 ≈ 55 分钟。

数值一律按接口原样记（元），接口没给的期留 NULL 而不是拦在库外：数据薄是当期事实，
下次重跑同一只即自愈。
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import db, industry
from .datasource import f10

COVER = 0.70                 # 资产负债表档要盖到的行业营收占比
CF_TOP = 10                  # 现金流量表档：每行业营收前 N
PERIODS = 20                 # 回填深度：5 年 × 4 期
FINANCE = {"银行", "非银金融"}
META_KEY = "f10_capital_meta"
CONFIRM_AT = 200             # 一轮要发这么多请求就得让操作者明确确认（首轮全量约 3900 次）


def requests_per_table(periods: int = PERIODS) -> int:
    return -(-periods // f10.BATCH)         # 接口一次最多 5 期


def requests_needed(plan: list[tuple[str, tuple[str, ...]]], periods: int = PERIODS) -> int:
    return sum(len(t) for _c, t in plan) * requests_per_table(periods)


def _revenue_base() -> tuple[dict[str, float], dict[str, str], tuple[str | None, str | None]]:
    """排序用营收：最近一个年报期优先，缺年报的用最近半年报 ×2 折算。"""
    ann = db.query_one("SELECT MAX(report_date) d FROM fin_report WHERE report_date LIKE '%-12-31'")
    half = db.query_one("SELECT MAX(report_date) d FROM fin_report WHERE report_date LIKE '%-06-30'")
    periods = (ann["d"], half["d"])
    rev, src = {}, {}
    if not ann["d"]:
        return rev, src, periods
    for row in db.query_rows(
            "SELECT code, revenue FROM fin_report WHERE report_date=? AND revenue IS NOT NULL",
            (ann["d"],)):
        if row["revenue"]:
            rev[row["code"]] = float(row["revenue"])
            src[row["code"]] = ann["d"]
    if half["d"]:
        for row in db.query_rows(
                "SELECT code, revenue FROM fin_report WHERE report_date=? AND revenue IS NOT NULL",
                (half["d"],)):
            if row["revenue"] and row["code"] not in rev:
                rev[row["code"]] = float(row["revenue"]) * 2
                src[row["code"]] = half["d"] + "(×2)"
    return rev, src, periods


def sample_rows(cover: float = COVER, cf_top: int = CF_TOP):
    """返回 (样本行, 逐行业统计, 排序基准期)。样本行 = {code, level1, revenue, rank, need_balance, need_cash}。"""
    rev, _src, periods = _revenue_base()
    by_level: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for code, ind in db.query_rows("SELECT code, industry FROM stocks WHERE is_active=1"):
        l1 = industry.first_level(ind)
        if l1 == industry.UNCLASSIFIED or code not in rev:
            continue
        by_level[l1].append((code, rev[code]))

    rows: list[dict] = []
    levels: list[dict] = []
    for l1 in sorted(by_level):
        lst = sorted(by_level[l1], key=lambda x: -x[1])
        tot = sum(v for _c, v in lst)
        fin = l1 in FINANCE
        n_bs = 0
        if not fin:
            cum = 0.0
            for _c, v in lst:
                cum += v
                n_bs += 1
                if cum / tot >= cover:
                    break
        n_cf = 0 if fin else min(cf_top, len(lst))
        bs_set = {c for c, _ in lst[:n_bs]}
        cf_set = {c for c, _ in lst[:n_cf]}
        for rank, (c, v) in enumerate(lst, start=1):
            if c in bs_set or c in cf_set:
                rows.append({"code": c, "level1": l1, "revenue": v, "rank": rank,
                             "need_balance": int(c in bs_set), "need_cash": int(c in cf_set)})
        levels.append({"level1": l1, "n_rev": len(lst), "revenue_yi": round(tot / 1e8, 1),
                       "finance": fin, "n_balance": n_bs, "n_cash": n_cf,
                       "cover_pct": None if fin else
                       round(100 * sum(v for _c, v in lst[:n_bs]) / tot, 1)})
    return rows, levels, periods


def rebuild_sample(cover: float = COVER, cf_top: int = CF_TOP) -> dict:
    """按当前财报重算样本名单并整表替换（名单本身几百行，重写代价可忽略）。"""
    rows, levels, periods = sample_rows(cover, cf_top)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute("DELETE FROM f10_sample")
        conn.executemany(
            "INSERT INTO f10_sample(code,level1,revenue,rank_in_level1,need_balance,need_cash,built_at)"
            " VALUES(?,?,?,?,?,?,?)",
            [(r["code"], r["level1"], r["revenue"], r["rank"], r["need_balance"], r["need_cash"], now)
             for r in rows])
    meta = {"built_at": now, "cover": cover, "cf_top": cf_top, "revenue_periods": list(periods),
            "n_codes": len(rows),
            "n_balance": sum(1 for r in rows if r["need_balance"]),
            "n_cash": sum(1 for r in rows if r["need_cash"])}
    db.kv_set(META_KEY, {**db.kv_get(META_KEY, {}), **meta})
    return {"sample": meta, "levels": levels}


def preview(cover: float = COVER, cf_top: int = CF_TOP) -> dict:
    """面板用：当前库里名单 vs 按最新财报重算的名单差多少，以及逐行业覆盖率。"""
    rows, levels, _periods = sample_rows(cover, cf_top)
    want = {r["code"] for r in rows}
    stored = {r["code"] for r in db.query_rows("SELECT code FROM f10_sample")}
    got = {r[0] for r in db.query_rows("SELECT DISTINCT code FROM fin_capital")}
    n_bs = sum(1 for r in rows if r["need_balance"])
    n_cf = sum(1 for r in rows if r["need_cash"])
    left = pending()
    return {"levels": levels,
            "totals": {"n_codes": len(rows), "n_balance": n_bs, "n_cash": n_cf,
                       "requests_est": (n_bs + n_cf) * requests_per_table()},
            "stored": {"built_at": db.kv_get(META_KEY, {}).get("built_at"),
                       "revenue_periods": db.kv_get(META_KEY, {}).get("revenue_periods"),
                       "n_codes": len(stored),
                       "added": len(want - stored), "removed": len(stored - want)},
            "backfilled": {"n_codes": len(got), "rows": db.query_one(
                "SELECT COUNT(*) n FROM fin_capital")["n"],
                # todo_units = 还欠几张表（一只票可能只欠现金流量表）
                "todo_units": sum(len(t) for _c, t in left),
                "todo_codes": len(left),
                "requests_left": requests_needed(left),
                "confirm_at": CONFIRM_AT}}


def pending(force: bool = False) -> list[tuple[str, tuple[str, ...]]]:
    """名单里还欠抓的 (代码, 要抓哪些表)。

    按「表」判断而不是按「只」：某只上次只欠现金流量表，这次就只请求现金流量表，
    所以名单档位变了也不用先清空；force=True 才是全部重抓。
    """
    done = {"balance": {r[0] for r in db.query_rows(
                "SELECT code FROM fin_capital WHERE has_balance=1")},
            "cash": {r[0] for r in db.query_rows(
                "SELECT code FROM fin_capital WHERE has_cash=1")}}
    out = []
    for r in db.query_rows("SELECT code,need_balance,need_cash FROM f10_sample ORDER BY code"):
        want = tuple(n for n, flag in (("balance", r["need_balance"]),
                                       ("cash", r["need_cash"])) if flag)
        todo = tuple(n for n in want if force or r["code"] not in done[n])
        if todo:
            out.append((r["code"], todo))
    return out


def _merge_rows(code: str, tables: dict, now: str) -> list[tuple]:
    vals: dict[str, dict] = defaultdict(dict)
    for name, key in (("balance", "need_balance"), ("cash", "need_cash")):
        for row in tables.get(name) or []:
            d = row.pop("report_date")
            vals[d].update(row)
            vals[d][key] = 1
    out = []
    for d, v in vals.items():
        out.append((code, d, v.get("inventory"), v.get("cip"), v.get("fixed_asset"),
                    v.get("total_assets"), v.get("accounts_rece"), v.get("contract_liab"),
                    v.get("monetary_funds"), v.get("capex_cash"), v.get("operate_cash"),
                    v.get("need_balance", 0), v.get("need_cash", 0), now))
    return out


UPSERT = (
    "INSERT INTO fin_capital(code,report_date,inventory,cip,fixed_asset,total_assets,"
    "accounts_rece,contract_liab,monetary_funds,capex_cash,operate_cash,"
    "has_balance,has_cash,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(code,report_date) DO UPDATE SET "
    "inventory=COALESCE(excluded.inventory,inventory),cip=COALESCE(excluded.cip,cip),"
    "fixed_asset=COALESCE(excluded.fixed_asset,fixed_asset),"
    "total_assets=COALESCE(excluded.total_assets,total_assets),"
    "accounts_rece=COALESCE(excluded.accounts_rece,accounts_rece),"
    "contract_liab=COALESCE(excluded.contract_liab,contract_liab),"
    "monetary_funds=COALESCE(excluded.monetary_funds,monetary_funds),"
    "capex_cash=COALESCE(excluded.capex_cash,capex_cash),"
    "operate_cash=COALESCE(excluded.operate_cash,operate_cash),"
    "has_balance=MAX(fin_capital.has_balance,excluded.has_balance),"
    "has_cash=MAX(fin_capital.has_cash,excluded.has_cash),"
    "fetched_at=excluded.fetched_at")


def backfill(progress=None, periods: int = PERIODS, force: bool = False) -> dict:
    """逐只抓 F10 并落库。progress(done, total, msg) 由调用方给（updater 用它更新徽章）。"""
    if not db.query_one("SELECT COUNT(*) n FROM f10_sample")["n"]:
        rebuild_sample()
    plan = pending(force)
    dates = f10.quarter_ends(dt.date.today(), periods)
    now = dt.datetime.now().isoformat(timespec="seconds")
    stat = {"n_codes": len(plan), "rows": 0, "empty": 0, "errors": []}
    for i, (code, tables) in enumerate(plan):
        res = f10.fetch_stock(code, dates, tables)
        rows = _merge_rows(code, res["tables"], now)
        if rows:
            with db.transaction() as conn:
                conn.executemany(UPSERT, rows)
            stat["rows"] += len(rows)
        else:
            stat["empty"] += 1
        if res["errors"]:
            stat["errors"].append(f"{code}: " + ";".join(res["errors"]))
        if progress:
            progress(i + 1, len(plan), f"F10 回填 {i + 1}/{len(plan)}（{code} {'+'.join(tables)}）")
    db.kv_set(META_KEY, {**db.kv_get(META_KEY, {}),
                         "backfill_at": now, "periods": periods,
                         "last_rows": stat["rows"], "last_empty": stat["empty"],
                         "last_errors": stat["errors"][:20]})
    return stat


def clear() -> int:
    """把 F10 数据整层清空（样本名单保留，便于重抓；名单本身用 rebuild_sample 重建）。"""
    n = db.query_one("SELECT COUNT(*) n FROM fin_capital")["n"]
    db.execute("DELETE FROM fin_capital")
    return int(n)
