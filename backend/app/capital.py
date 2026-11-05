"""东财 F10 资本开支口径：混合档样本名单 + 回填 + 落库。

样本口径（2026-11-05 实测定案，三个参数在「数据中心 → F10 报表数据」页可调）：
- 分组键＝申万一级（`industry.first_level`）；银行/非银金融共 121 只整体剔除（报表口径不同）；
- 资产负债表档＝每行业按年报营收累计盖到 `COVER`（默认 70%，可调 30%~95%）的最少只数
  （自适应，默认档实测 688 只，最少的行业 2 只＝石油石化，最多的 82 只＝基础化工）；
- 现金流量表档＝每行业营收前 `CF_TOP`（默认 10，可调 3~50；默认档实测 290 只）；两档去重 722 只；
- 回填深度＝最近 `PERIODS` 个**已过披露截止日**的季末报告期（默认 20 期＝5 年，可调 4~40）。

抵扣粒度是「票 × 表 × 期」：库里某只某表某期有一行（哪怕字段全 NULL）就算抓过，所以
- 新季报过了截止日后自动变成缺期，下次点「开始回填」只补那一期，不用先清空；
- 把期数调大（20→24）只补多出来的那几期，已入库的期不受影响；
- 一次请求最多带 5 个报告期 ⇒ 某表缺 6 期就是 2 次请求。缺多少期就发多少次，页面按这个数确认。

参数（门槛 + 期数）用哪一套**记在名单里**（`kv` 的 `f10_capital_meta`），所以「重建名单」不传参
就沿用上次的口径，预览/回填不传参就按库里那套算 —— 改参数只有一点：调大 ⇒ 新增的票/期变成待抓，
按请求数重新确认。

数值一律按接口原样记（元），接口没给的期留 NULL 而不是拦在库外：数据薄是当期事实，
下次重跑同一只即自愈。
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import db, industry
from .datasource import f10

COVER = 0.70                 # 资产负债表档要盖到的行业营收占比（页面可调）
CF_TOP = 10                  # 现金流量表档：每行业营收前 N（页面可调）
COVER_RANGE = (0.30, 0.95)   # 再低就盖不出行业结论，再高名单接近全市场、请求数失控
CF_TOP_RANGE = (3, 50)       # 下限跟着四象限页的 MIN_CODES=5 走，上限防手滑
PERIODS = 20                 # 回填深度：5 年 × 4 期（页面可调）
PERIODS_RANGE = (4, 40)      # 下限 4 期＝1 年（同比要有去年同期），上限防手滑把请求数放大十倍
# 各季末报告期的法定披露截止（报告期 + 自然日）：年报 4-30、半年报 8-31、一季报 4-30、三季报 10-31。
# 未按闰年精确到±1 天不要紧：那一期顶多晚一天进清单，下次回填即补上。
DISCLOSE_DAYS = {(3, 31): 30, (6, 30): 62, (9, 30): 31, (12, 31): 120}
FINANCE = {"银行", "非银金融"}
META_KEY = "f10_capital_meta"
CONFIRM_AT = 200             # 一轮要发这么多请求就得让操作者明确确认（首轮全量约 3900 次）


def today() -> dt.date:
    """留一个可替换的缝隙：断言脚本要按「三季报到期前/后」两个日子算清单。"""
    return dt.date.today()


def disclosed(report_date: str, as_of: dt.date) -> bool:
    """该报告期过了法定披露截止日没有（过了才可能有人披露，才值得去请求）。"""
    d = dt.date.fromisoformat(report_date)
    days = DISCLOSE_DAYS.get((d.month, d.day))
    return True if days is None else as_of >= d + dt.timedelta(days=days)


def target_dates(periods: int | None = None, as_of: dt.date | None = None) -> list[str]:
    """最近 `periods` 个**已到期**的季末报告期，新→旧。

    先过滤截止日再截个数，所以窗口永远是「有东西可抓的那几期」：三季报在 10-31 之前不会进来，
    4 月初同时未到期的年报 + 一季报会一起跳掉（窗口整体往前挪，不是少给一期）。
    """
    as_of = as_of or today()
    p = resolve_periods(periods)
    # 未到期最多连着 4 期，候选池按此放宽，保证截得出 p 个
    return [d for d in f10.quarter_ends(as_of, p + len(DISCLOSE_DAYS))
            if disclosed(d, as_of)][:p]


def periods_value() -> int:
    """回填深度：库里名单记的那套；没记过就用代码默认。"""
    meta = db.kv_get(META_KEY, {}) or {}
    return int(meta.get("periods", PERIODS))


def resolve_periods(periods: int | None = None) -> int:
    return periods_value() if periods is None else int(periods)


def check_periods(periods) -> list[str]:
    """期数是否可用；返回问题列表（空=可用）。页面按它拦按钮，后端按它返 400。"""
    lo, hi = PERIODS_RANGE
    try:
        p = float(periods)
    except (TypeError, ValueError):
        p = float("nan")
    return [] if p == int(p) and lo <= int(p) <= hi else [
        f"回填期数要是 {lo}~{hi} 之间的整数（现在 {periods}；4 期＝1 年）"]


def requests_per_table(periods: int = PERIODS) -> int:
    return -(-periods // f10.BATCH)         # 接口一次最多 5 期


def requests_needed(plan: list[tuple[str, dict[str, tuple[str, ...]]]]) -> int:
    """按「票×表」各自的缺期数算请求次数（一批最多 5 期，缺 6 期就发 2 次）。"""
    return sum(-(-len(v) // f10.BATCH) for _c, todo in plan for v in todo.values())


def params() -> dict:
    """名单当前用的门槛（建名单时记进 kv）；没建过名单就回代码默认值。"""
    meta = db.kv_get(META_KEY, {}) or {}
    return {"cover": float(meta.get("cover", COVER)), "cf_top": int(meta.get("cf_top", CF_TOP))}


def cover_text(cover: float) -> str:
    """覆盖率写成人话：0.7→70%、0.725→72.5%（用 .0% 会折成 73%，和页面输入框对不上）。"""
    return f"{round(float(cover) * 1000) / 10:g}%"


def check_params(cover, cf_top) -> list[str]:
    """门槛是否可用；返回问题列表（空=可用）。页面按它拦按钮，后端按它返 400。"""
    problems = []
    c_lo, c_hi = COVER_RANGE
    try:
        c = float(cover)
    except (TypeError, ValueError):
        c = float("nan")
    if not c_lo <= c <= c_hi:
        problems.append(f"负债表档覆盖率要用 {c_lo:.0%}~{c_hi:.0%} 之间的数（现在 {cover}）")
    t_lo, t_hi = CF_TOP_RANGE
    try:
        t = float(cf_top)
    except (TypeError, ValueError):
        t = float("nan")
    if t != int(t) or not t_lo <= int(t) <= t_hi:
        problems.append(f"现金流量表档「前 N」要是 {t_lo}~{t_hi} 之间的整数（现在 {cf_top}）")
    return problems


def resolve_params(cover=None, cf_top=None) -> tuple[float, int]:
    p = params()
    if cover is not None:
        p["cover"] = float(cover)
    if cf_top is not None:
        p["cf_top"] = int(cf_top)
    return p["cover"], p["cf_top"]


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


def sample_rows(cover: float | None = None, cf_top: int | None = None):
    """返回 (样本行, 逐行业统计, 排序基准期)。样本行 = {code, level1, revenue, rank, need_balance, need_cash}。"""
    cover, cf_top = resolve_params(cover, cf_top)
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


def rebuild_sample(cover: float | None = None, cf_top: int | None = None) -> dict:
    """按当前财报重算样本名单并整表替换（名单本身几百行，重写代价可忽略）。

    不传参就沿用名单上次用的门槛，所以「每日重建」不会因为代码默认值把口径悄悄改回去。
    """
    cover, cf_top = resolve_params(cover, cf_top)
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


def preview(cover: float | None = None, cf_top: int | None = None,
            periods: int | None = None) -> dict:
    """面板用：按这套参数重算的名单 vs 库里名单差多少、重建后还欠多少请求，以及逐行业覆盖率。

    不传参就按库里那套参数算（`params()` / `periods_value()`），所以页面没改输入时看到的数字
    就是当前名单的口径；改了输入就等于「重建之后会变成什么样」。请求数一律按**还缺的期**算，
    不是按「只」算 —— 补齐一个新季度只需要 1 期，跟首轮全量 20 期不是一笔账。
    """
    cover, cf_top = resolve_params(cover, cf_top)
    p = resolve_periods(periods)
    rows, levels, _rev_periods = sample_rows(cover, cf_top)
    want = {r["code"] for r in rows}
    stored_rows = db.query_rows("SELECT code,need_balance,need_cash FROM f10_sample ORDER BY code")
    stored = {r["code"] for r in stored_rows}
    got = {r[0] for r in db.query_rows("SELECT DISTINCT code FROM fin_capital")}
    dates = target_dates(p)
    done = _done_dates()
    r_t = [(x["code"], x["need_balance"], x["need_cash"]) for x in rows]
    s_t = [(x["code"], x["need_balance"], x["need_cash"]) for x in stored_rows]
    new_todo = _todo(r_t, done, dates)
    left = _todo(s_t, done, dates)
    n_bs = sum(1 for r in rows if r["need_balance"])
    n_cf = sum(1 for r in rows if r["need_cash"])
    meta = db.kv_get(META_KEY, {}) or {}
    return {"params": {"cover": cover, "cf_top": cf_top, "cover_range": list(COVER_RANGE),
                       "cf_top_range": list(CF_TOP_RANGE),
                       "default_cover": COVER, "default_cf_top": CF_TOP,
                       "periods": p, "periods_range": list(PERIODS_RANGE),
                       "default_periods": PERIODS},
            "levels": levels,
            "totals": {"n_codes": len(rows), "n_balance": n_bs, "n_cash": n_cf,
                       "requests_est": (n_bs + n_cf) * requests_per_table(p)},
            "stored": {"built_at": meta.get("built_at"), "cover": meta.get("cover"),
                       "cf_top": meta.get("cf_top"),
                       "revenue_periods": meta.get("revenue_periods"),
                       "n_codes": len(stored),
                       "added": len(want - stored), "removed": len(stored - want)},
            # 按这套门槛重建名单之后还要发多少请求（库里已有的「票×表×期」抵扣掉）
            "if_rebuilt": {"codes": len(new_todo), "units": sum(len(t) for _c, t in new_todo),
                           "dates": sum(len(v) for _c, t in new_todo for v in t.values()),
                           "requests": requests_needed(new_todo)},
            "backfilled": {"n_codes": len(got), "rows": db.query_one(
                "SELECT COUNT(*) n FROM fin_capital")["n"],
                # todo_units = 还欠几张表（一只票可能只欠现金流量表），todo_dates = 还欠几期
                "todo_units": sum(len(t) for _c, t in left),
                "todo_codes": len(left),
                "todo_dates": sum(len(v) for _c, t in left for v in t.values()),
                "requests_left": requests_needed(left),
                "window": {"first": dates[0] if dates else None,
                           "last": dates[-1] if dates else None, "n": len(dates)},
                "confirm_at": CONFIRM_AT}}


def _done_dates() -> dict[str, dict[str, set[str]]]:
    """每张表「哪些票的哪些期已经抓过」——有行就算，字段全 NULL 也算（如实记录，不重复请求）。"""
    out = {"balance": defaultdict(set), "cash": defaultdict(set)}
    for r in db.query_rows("SELECT code,report_date,has_balance,has_cash FROM fin_capital"):
        if r["has_balance"]:
            out["balance"][r["code"]].add(r["report_date"])
        if r["has_cash"]:
            out["cash"][r["code"]].add(r["report_date"])
    return out


def _todo(rows: list[tuple[str, int, int]], done: dict[str, dict[str, set[str]]],
          dates: list[str], force: bool = False) -> list[tuple[str, dict[str, tuple[str, ...]]]]:
    """(代码, {表: 缺哪几期})。缺期按 `dates` 的新→旧顺序，所以一批请求带的是最靠近的几期。"""
    out = []
    for code, need_bs, need_cf in rows:
        want = {}
        for name, flag in (("balance", need_bs), ("cash", need_cf)):
            if not flag:
                continue
            got = done[name].get(code, ())
            miss = tuple(d for d in dates if force or d not in got)
            if miss:
                want[name] = miss
        if want:
            out.append((code, want))
    return out


def sample_todo(cover: float | None = None, cf_top: int | None = None,
                periods: int | None = None, force: bool = False):
    """按这套门槛（不落库）算还欠哪些期，返回 (计划, 名单行, 目标期)。"""
    cover, cf_top = resolve_params(cover, cf_top)
    rows, _levels, _rev_periods = sample_rows(cover, cf_top)
    dates = target_dates(periods)
    return _todo([(r["code"], r["need_balance"], r["need_cash"]) for r in rows],
                 _done_dates(), dates, force), rows, dates


def pending(periods: int | None = None, force: bool = False) -> list[tuple[str, dict[str, tuple[str, ...]]]]:
    """库里名单还欠抓的 (代码, {表: 缺哪几期})。

    按「票×表×期」抵扣：某只上次只欠现金流量表就只请求现金流量表；新披露的季度是库里没有的那期，
    所以它会自己变成缺期，不用先清空整层；force=True 才是全窗口重抓。
    """
    return _todo([(r["code"], r["need_balance"], r["need_cash"])
                  for r in db.query_rows(
                      "SELECT code,need_balance,need_cash FROM f10_sample ORDER BY code")],
                 _done_dates(), target_dates(periods), force)


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


def backfill(progress=None, periods: int | None = None, force: bool = False) -> dict:
    """逐只抓 F10 并落库。progress(done, total, msg) 由调用方给（updater 用它更新徽章）。

    每只票只发「它缺的那几张表 × 各自缺的期」这些请求：新季度只补新季度，
    把期数调大只补多出来的期，接口没回的期下次还会再来一次。
    """
    if not db.query_one("SELECT COUNT(*) n FROM f10_sample")["n"]:
        rebuild_sample()
    p = resolve_periods(periods)
    plan = pending(p, force)
    now = dt.datetime.now().isoformat(timespec="seconds")
    stat = {"n_codes": len(plan), "rows": 0, "empty": 0, "errors": []}
    for i, (code, todo) in enumerate(plan):
        res = f10.fetch_stock(code, todo)
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
            progress(i + 1, len(plan),
                     f"F10 回填 {i + 1}/{len(plan)}（{code} {'+'.join(todo)}）")
    db.kv_set(META_KEY, {**db.kv_get(META_KEY, {}),
                         "backfill_at": now, "periods": p,
                         "last_rows": stat["rows"], "last_empty": stat["empty"],
                         "last_errors": stat["errors"][:20]})
    return stat


def clear() -> int:
    """把 F10 数据整层清空（样本名单保留，便于重抓；名单本身用 rebuild_sample 重建）。"""
    n = db.query_one("SELECT COUNT(*) n FROM fin_capital")["n"]
    db.execute("DELETE FROM fin_capital")
    return int(n)
