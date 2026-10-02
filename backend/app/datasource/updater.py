"""Background data jobs: 其它数据刷新 (full / incremental) from the online
providers, and 日线导入 (qmt_full / qmt_incremental) from two QMT export
directories: 不复权 + 等比后复权.

The online jobs only maintain the stock list, the fundamentals snapshot, the
quarterly reports, the industry boards and the market indexes. Individual
stocks' daily bars are never downloaded online any more: `kline_daily` has
exactly one writer, the QMT import, which stores exchange-quoted (unadjusted)
prices plus one derived adjustment factor per bar — the adjusted price is
always `price * adj_factor`, never a second copy of the data.

Provider strategy: probe Eastmoney at job start; if its quote servers reject
us, fall back to Tencent for the whole run.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import traceback

from .. import db
from ..datasource import eastmoney as em
from ..datasource import qmt as qmt_src
from ..datasource import tencent as tc

_status_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}

LOCAL_KINDS = ("qmt_full", "qmt_incremental")


def _job_state() -> dict:
    return {"status": "idle", "progress": 0, "total": 0, "message": "", "time": None}


def _default_status() -> dict:
    return {
        "full": _job_state(),
        "incremental": _job_state(),
        "local": _job_state(),
        "provider": None,
        "qmt_path_raw": None,
        "qmt_path_geo": None,
        "stock_count": 0,
        # 任务运行中也要能报出上次的行数：不在 base 里的键会被 _norm 丢掉，
        # 数据中心徽章会在导入那一两分钟里显示成「未导入」
        "kline_rows": 0,
        "last_full": None,
        "last_incremental": None,
        "last_local": None,
    }


def _norm(st: dict) -> dict:
    base = _default_status()
    st = {k: v for k, v in st.items() if k in base}   # 丢掉已废弃的历史键
    for k, v in base.items():
        if isinstance(v, dict):
            merged = dict(v)
            merged.update(st.get(k) or {})
            st[k] = merged
        else:
            st.setdefault(k, v)
    return st


def _status_key(kind: str) -> str:
    """本地导入的 qmt_full / qmt_incremental 共用状态面板 'local'。"""
    return "local" if kind in LOCAL_KINDS else kind


def get_status() -> dict:
    st = _norm(db.kv_get("data_status", _default_status()))
    with _status_lock:
        st["running"] = sorted(_running.keys())
        active = {_status_key(k) for k in _running}
    for kind in ("full", "incremental", "local"):
        if kind not in active and st[kind].get("status") == "running":
            st[kind] = {**_job_state(), "status": "error",
                        "message": "任务已中断（服务重启）", "time": st[kind].get("time")}
    if not st.get("running"):
        row = db.query_one("SELECT COUNT(*) AS n FROM kline_daily")
        st["kline_rows"] = row["n"] if row else 0
    return st


def _save_status(st: dict) -> None:
    db.kv_set("data_status", st)


def _update(kind: str, **kw) -> None:
    st = _norm(db.kv_get("data_status", _default_status()))
    st[_status_key(kind)].update(kw)
    _save_status(st)


def is_busy() -> bool:
    with _status_lock:
        return bool(_running)


def start_job(kind: str, paths: tuple[str, str] | None = None, force: bool = False) -> str:
    """kind: full | incremental | qmt_full | qmt_incremental

    本地导入任务的 paths 是 (不复权目录, 等比后复权目录)。
    """
    with _status_lock:
        if kind in _running:
            return "already running"
        t = threading.Thread(target=_run_job, args=(kind, paths, force), daemon=True)
        _running[kind] = t
        t.start()
    return "started"


def _run_job(kind: str, paths: tuple[str, str] | None = None,
             force: bool = False) -> None:
    try:
        _update(kind, status="running", progress=0, total=0, message="准备中…")
        if kind in LOCAL_KINDS:
            raw, geo = paths or ("", "")
            _job_local(kind, raw, geo, force)
        else:
            provider = _probe_provider()
            st = _norm(db.kv_get("data_status", _default_status()))
            st["provider"] = provider
            _save_status(st)
            if kind == "full":
                _job_full(provider)
            else:
                _job_incremental(provider)
        st = _norm(db.kv_get("data_status", _default_status()))
        key = _status_key(kind)
        msg = st[key].get("message", "")
        if not st[key].get("final") and "失败" not in msg:
            msg = "完成"
        now = dt.datetime.now().isoformat(timespec="seconds")
        _update(kind, status="done", message=msg, time=now, final=False)
        st = _norm(db.kv_get("data_status", _default_status()))
        st[f"last_{key}"] = now
        _save_status(st)
    except Exception as e:
        traceback.print_exc()
        # 失败也要盖时间戳：否则徽章上的时间是上一轮的，看不出这次是什么时候挂的
        _update(kind, status="error", message=f"失败: {e}",
                time=dt.datetime.now().isoformat(timespec="seconds"))
    finally:
        with _status_lock:
            _running.pop(kind, None)


def _probe_provider() -> str:
    """东财 push2his 可达就用它（板块/指数历史更全），否则整轮退到腾讯。

    这里只探活，不写任何个股日线：`em.fetch_kline` 同时服务指数K线。
    """
    try:
        rows = em.fetch_kline("1.600000", beg="20250101", end="20250201")
        if rows:
            return "em"
    except Exception:
        pass
    return "tc"


# ---------- stock list & fundamentals ----------

# 两条主列表来源对空值的处理不同，统一成 COALESCE：新值缺失时保留旧值
_STOCK_UPSERT = (
    "INSERT INTO stocks(code,name,market,secid,industry,board_code,list_date,is_active) "
    "VALUES(:code,:name,:market,:secid,:industry,:board_code,:list_date,:is_active) "
    "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, "
    "secid=excluded.secid, "
    "industry=COALESCE(excluded.industry, stocks.industry), "
    "board_code=COALESCE(excluded.board_code, stocks.board_code), "
    "list_date=COALESCE(excluded.list_date, stocks.list_date), is_active=1")

_FUND_UPSERT = (
    "INSERT INTO fundamentals(code,trade_date,price,pct_chg,total_mv,float_mv,"
    "pe_dynamic,pe_ttm,pb,turnover_rate,amount) "
    "VALUES(:code,:trade_date,:price,:pct_chg,:total_mv,:float_mv,:pe_dynamic,:pe_ttm,"
    ":pb,:turnover_rate,:amount) "
    "ON CONFLICT(code) DO UPDATE SET trade_date=excluded.trade_date, price=excluded.price, "
    "pct_chg=excluded.pct_chg, total_mv=excluded.total_mv, float_mv=excluded.float_mv, "
    "pe_dynamic=excluded.pe_dynamic, pe_ttm=excluded.pe_ttm, "
    "pb=COALESCE(excluded.pb, fundamentals.pb), "
    "turnover_rate=COALESCE(excluded.turnover_rate, fundamentals.turnover_rate), "
    "amount=COALESCE(excluded.amount, fundamentals.amount)")


def _refresh_stock_list(kind: str) -> None:
    _update(kind, message="更新股票列表与基本面快照…")
    # 休市日（周末、长假）不能拿今天当快照日期，否则导入后「快照与末根同日」的涨跌幅回填整批跳过
    today = tc.market_trade_date() or dt.date.today().isoformat()
    try:
        stocks = em.fetch_all_stocks()
    except Exception:
        stocks = None
    if stocks and len(stocks) > 3000:
        rows = [{k: s[k] for k in ("code", "name", "market", "secid", "industry", "list_date")}
                for s in stocks]
        for r in rows:
            r["board_code"] = None
            r["is_active"] = 1
        db.execute(_STOCK_UPSERT, many=rows)
        db.execute(
            _FUND_UPSERT,
            many=[{**{k: s[k] for k in ("code", "price", "pct_chg", "total_mv", "float_mv",
                                        "pe_dynamic", "pe_ttm", "pb")},
                   "trade_date": today,
                   "turnover_rate": s.get("turnover_rate"), "amount": s.get("amount")}
                  for s in stocks],
        )
    else:
        # Tencent fallback: industry boards + constituents
        boards = tc.fetch_industry_boards()
        all_rows: list[dict] = []
        total = len(boards)
        for i, b in enumerate(boards):
            _update(kind, progress=i, total=total, message=f"股票列表 {i + 1}/{total}")
            try:
                items = tc.fetch_board_stocks(b["board_code"])
            except Exception:
                items = []
            for it in items:
                all_rows.append({**it, "board_code": b["board_code"],
                                 "board_name": b["board_name"]})
        seen = set()
        srows, frows = [], []
        for it in all_rows:
            if it["code"] in seen:
                continue
            seen.add(it["code"])
            code = it["code"]
            srows.append({"code": code, "name": it["name"], "market": em.market_for(code),
                          "secid": em.secid_for(code), "industry": it["board_name"],
                          "board_code": it["board_code"], "list_date": None, "is_active": 1})
            frows.append({"code": code, "trade_date": today, "price": it["price"],
                          "pct_chg": it["pct_chg"], "total_mv": it["total_mv"],
                          "float_mv": it["float_mv"], "pe_dynamic": None,
                          "pe_ttm": it["pe_ttm"], "pb": it["pb"],
                          "turnover_rate": it["turnover_rate"], "amount": it["amount"]})
        if srows:
            db.execute(_STOCK_UPSERT, many=srows)
            db.execute(_FUND_UPSERT, many=frows)
        db.execute(
            "INSERT INTO boards(board_code,board_name,board_type) "
            "VALUES(:board_code,:board_name,:board_type) "
            "ON CONFLICT(board_code) DO UPDATE SET board_name=excluded.board_name",
            many=[{"board_code": b["board_code"], "board_name": b["board_name"],
                   "board_type": "industry"} for b in boards],
        )
    _supplement_missing_stocks(kind)
    # 补录后才是真实在册数：主列表分支早写会漏掉补进来的科创板/北交所
    st = db.kv_get("data_status", _default_status())
    st["stock_count"] = len(_universe())
    _save_status(st)


def _supplement_missing_stocks(kind: str) -> int:
    """把两条主列表来源都漏掉的股票（科创板、北交所）从东财财报接口补进 stocks。

    本机连不上东财 push2，列表只能来自腾讯板块成分股，而腾讯的排行接口只覆盖 4606 只
    沪深A股；QMT 导入又按 stocks 白名单过滤文件，688/920/8xx 的导出会被静默丢掉。
    """
    _update(kind, message="补录缺失板块股票列表…")
    try:
        roster = em.fetch_stock_roster(_recent_quarters(2))
    except Exception:
        return 0
    have = {r["code"] for r in db.query("SELECT code FROM stocks")}
    missing = [r for r in roster if r["code"] not in have]
    if not missing:
        return 0
    quotes: dict[str, dict] = {}
    try:
        quotes = tc.fetch_quotes([(r["code"], r["market"]) for r in missing])
    except Exception:
        quotes = {}
    db.execute(_STOCK_UPSERT, many=[
        {"code": r["code"], "name": r["name"] or quotes.get(r["code"], {}).get("name") or "",
         "market": r["market"], "secid": em.secid_for(r["code"]), "industry": r["industry"],
         "board_code": None, "list_date": None, "is_active": 1} for r in missing])
    today = dt.date.today().isoformat()
    frows = []
    for r in missing:
        q = quotes.get(r["code"])
        if not q or not q["price"]:
            continue
        frows.append({"code": r["code"], "trade_date": q["trade_date"] or today,
                      "price": q["price"], "pct_chg": q["pct_chg"],
                      "total_mv": q["total_mv"], "float_mv": q["float_mv"],
                      "pe_dynamic": None, "pe_ttm": q["pe_ttm"], "pb": q["pb"],
                      "turnover_rate": q["turnover_rate"], "amount": q["amount"]})
    if frows:
        db.execute(_FUND_UPSERT, many=frows)
    _update(kind, message=f"补录缺失板块股票 {len(missing)} 只（快照 {len(frows)} 只）")
    return len(missing)


# ---------- boards (EM path) ----------

def _refresh_em_boards(kind: str) -> None:
    """Store EM industry boards and map stocks to BK codes by industry name."""
    try:
        boards = em.fetch_industry_boards()
    except Exception:
        return
    db.execute(
        "INSERT INTO boards(board_code,board_name,board_type) VALUES(:board_code,:board_name,:board_type) "
        "ON CONFLICT(board_code) DO UPDATE SET board_name=excluded.board_name",
        many=boards,
    )
    name2code = {b["board_name"]: b["board_code"] for b in boards}
    rows = db.query("SELECT code, industry FROM stocks WHERE board_code IS NULL AND industry IS NOT NULL")
    upd = [{"bc": name2code.get(r["industry"]), "c": r["code"]} for r in rows
           if name2code.get(r["industry"])]
    if upd:
        db.execute("UPDATE stocks SET board_code=:bc WHERE code=:c", many=upd)


# ---------- financial reports ----------

def _recent_quarters(n: int = 12) -> list[str]:
    today = dt.date.today()
    qs: list[str] = []
    y = today.year
    md = [(3, 31), (6, 30), (9, 30), (12, 31)]
    cur = None
    for m, d in md:
        if dt.date(y, m, d) < today:
            cur = f"{y}-{m:02d}-{d:02d}"
    if cur is None:
        cur = f"{y - 1}-12-31"
    yy, mm, dd = map(int, cur.split("-"))
    for _ in range(n):
        qs.append(f"{yy}-{mm:02d}-{dd:02d}")
        i = md.index((mm, dd))
        if i == 0:
            yy, mm, dd = yy - 1, 12, 31
        else:
            mm, dd = md[i - 1]
    return qs


def _refresh_financials(kind: str, n_quarters: int) -> None:
    quarters = _recent_quarters(n_quarters)
    total = len(quarters)
    for i, q in enumerate(quarters):
        _update(kind, progress=i, total=total, message=f"财报 {q} ({i + 1}/{total})")
        try:
            items = em.fetch_financial_report(q)
        except Exception:
            continue
        if items:
            db.execute(
                "INSERT INTO fin_report(code,report_date,eps,revenue,revenue_yoy,net_profit,net_profit_yoy,roe_weighted,gross_margin) "
                "VALUES(:code,:report_date,:eps,:revenue,:revenue_yoy,:net_profit,:net_profit_yoy,:roe_weighted,:gross_margin) "
                "ON CONFLICT(code,report_date) DO UPDATE SET eps=excluded.eps, revenue=excluded.revenue, "
                "revenue_yoy=excluded.revenue_yoy, net_profit=excluded.net_profit, "
                "net_profit_yoy=excluded.net_profit_yoy, roe_weighted=excluded.roe_weighted, "
                "gross_margin=excluded.gross_margin",
                many=items,
            )


# ---------- jobs ----------

def _job_full(provider: str) -> None:
    _refresh_stock_list("full")
    if provider == "em":
        _refresh_em_boards("full")
    _refresh_financials("full", 12)
    # refresh the market indexes up front: a run interrupted hours later must not
    # leave 大盘线 trailing the individual stocks by several trading days
    _refresh_indexes(provider)

    stocks = db.query("SELECT code FROM stocks WHERE is_active=1")
    st = _norm(db.kv_get("data_status", _default_status()))
    st["stock_count"] = len(stocks)
    _save_status(st)
    _update("full", progress=1, total=1, final=True,
            message=f"列表 {len(stocks)} 只 / 财报 12 期 / 指数已刷新；"
                    "个股日线请改到「K线数据（QMT）」做全量导入")


def _refresh_indexes(provider: str) -> None:
    today = dt.date.today().isoformat()
    for idx_code, meta in em.INDEXES.items():
        secid, name = meta
        try:
            if provider == "em":
                rows = em.fetch_kline(secid, beg="20100101")
            else:
                rows = tc.fetch_kline(idx_code, days_limit=4000)
        except Exception:
            continue
        if rows:
            db.execute(
                "INSERT OR REPLACE INTO index_kline(index_code,name,date,open,high,low,close,volume,amount,pct_chg) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                many=[(idx_code, name, r["date"], r["open"], r["high"], r["low"],
                       r["close"], r["volume"], r["amount"], r["pct_chg"]) for r in rows],
            )


def _job_incremental(provider: str) -> None:
    _refresh_stock_list("incremental")
    if provider == "em":
        _refresh_em_boards("incremental")
    _refresh_financials("incremental", 2)
    _refresh_indexes(provider)

    cnt = db.query_one("SELECT COUNT(*) AS n FROM kline_daily")
    rows = cnt["n"] if cnt else 0
    _update("incremental", progress=1, total=1, final=True,
            message=f"列表/板块/财报/指数已刷新；日线 {rows} 行未改动，"
                    "个股日线请在「K线数据（QMT）」做增量导入")


# ---------- 本地导入（QMT 导出目录） ----------

_KLINE_INSERT = ("INSERT OR REPLACE INTO kline_daily"
                 "(code,date,open,high,low,close,adj_factor,volume,amount,pct_chg,turnover) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)")


def _universe() -> dict[str, str]:
    return {r["code"]: r["market"]
            for r in db.query("SELECT code,market FROM stocks WHERE is_active=1")}


def _float_shares() -> dict[str, float]:
    """code -> 流通股本（股），由最新快照的流通市值/现价反推。"""
    out = {}
    for r in db.query("SELECT code,float_mv,price FROM fundamentals"):
        if r["float_mv"] and r["price"]:
            out[r["code"]] = r["float_mv"] / r["price"]
    return out


def _bar_row(code: str, b: dict, fs: float | None) -> tuple:
    turnover = round(b["volume"] / fs * 100, 4) if fs else None
    return (code, b["date"], b["open"], b["high"], b["low"], b["close"],
            b["adj_factor"], b["volume"], b["amount"], b["pct_chg"], turnover)


def _remember_qmt_paths(raw: str, geo: str) -> None:
    """记下最后一次体检通过 / 导入成功的两个目录，下次打开数据中心直接回填（路径不由代码写死）。"""
    st = _norm(db.kv_get("data_status", _default_status()))
    if (st.get("qmt_path_raw"), st.get("qmt_path_geo")) != (raw, geo):
        st["qmt_path_raw"], st["qmt_path_geo"] = raw, geo
        _save_status(st)


def preview_local(raw_path: str, geo_path: str) -> dict:
    info = qmt_src.check_pair(raw_path, geo_path, _universe(), _snapshots())
    if not info["warning"]:        # 被拦下来的配对不值得记住
        _remember_qmt_paths(os.path.abspath(raw_path), os.path.abspath(geo_path))
    return info


def _snapshots() -> dict[str, dict]:
    """code -> 最新在线快照的 现价/成交额/交易日，供导入体检抽检。"""
    return {r["code"]: r for r in db.query(
        "SELECT code, price, amount, trade_date FROM fundamentals") if r["price"]}


def _job_local(kind: str, raw_root: str, geo_root: str, force: bool = False) -> None:
    """导入前强制双目录体检：路径选错、两份不是同一批次，都会污染整个日线库。"""
    universe = _universe()
    info = qmt_src.check_pair(raw_root, geo_root, universe, _snapshots())
    if info["warning"] and not force:
        raise ValueError(info["warning"] + "（确认无误可勾选「强制导入」）")
    if not info["matched"] or info["matched"] < len(universe) * 0.8:
        raise ValueError(
            f"两个目录里只配对了 {info['matched']}/{len(universe)} 只股票，"
            "疑似路径选错或 QMT 导出尚未完成（不足 80% 覆盖率不允许导入）")
    files = qmt_src.pair_files(raw_root, geo_root, universe)
    shares = _float_shares()
    if kind == "qmt_full" and info["latest_date"] and not force:
        local_last = db.query_one("SELECT MAX(date) AS d FROM kline_daily")
        if local_last and local_last["d"] and info["latest_date"] < local_last["d"]:
            raise ValueError(
                f"导出数据只到 {info['latest_date']}，本地库已到 {local_last['d']}；"
                "全量导入会让历史回退，请在 QMT 里重新导出到最新交易日")
    _update(kind, progress=0, total=len(files),
            message=f"导入 QMT 导出 0/{len(files)}")
    if kind == "qmt_full":
        _local_full(kind, files, shares)
    else:
        _local_incremental(kind, files, shares)
    _sync_snapshot_from_klines()
    _backfill_pct_chg()
    # 徽章统计口径统一在任务末尾刷新：跑任务期间 get_status 不会现算 kline_rows，
    # 不写这里就会一直显示上一次全量更新留下的旧行数
    st = _norm(db.kv_get("data_status", _default_status()))
    st["stock_count"] = len(_universe())
    st["kline_rows"] = db.query_one("SELECT COUNT(*) AS n FROM kline_daily")["n"]
    st["qmt_path_raw"] = os.path.abspath(raw_root)
    st["qmt_path_geo"] = os.path.abspath(geo_root)
    _save_status(st)


def _backfill_pct_chg() -> None:
    """最新一根日线的涨跌幅改用在线快照的交易所口径。

    历史每根的涨跌幅在导入时已经按「不复权收盘价 ÷ 前一日的除权参考价」算好（除权参考价
    按 0.01 元取整，与交易所算法一致）；末根再用当日收盘后的权威快照覆盖一次，
    顺带修正停牌股与 QMT 因子推导的边际误差。要求快照交易日与这根同日，否则留空。
    """
    db.execute(
        "UPDATE kline_daily SET pct_chg = "
        "  (SELECT f.pct_chg FROM fundamentals f WHERE f.code = kline_daily.code) "
        "WHERE date = (SELECT MAX(date) FROM kline_daily) "
        "  AND EXISTS (SELECT 1 FROM fundamentals f "
        "              WHERE f.code = kline_daily.code AND f.trade_date = kline_daily.date "
        "                AND f.pct_chg IS NOT NULL)")


def _sync_snapshot_from_klines() -> None:
    """腾讯/降级源不返回成交额与换手率，选股条件因此永远不命中；用最新一根日线补空。

    只填空值，不覆盖在线快照；补的是该股票最后一根日线当天的数，可能比快照日期滞后。
    """
    db.execute(
        "UPDATE fundamentals SET "
        "  amount = COALESCE(amount, (SELECT k.amount FROM kline_daily k "
        "                             WHERE k.code = fundamentals.code "
        "                             ORDER BY k.date DESC LIMIT 1)), "
        "  turnover_rate = COALESCE(turnover_rate, (SELECT k.turnover FROM kline_daily k "
        "                             WHERE k.code = fundamentals.code "
        "                             ORDER BY k.date DESC LIMIT 1)) "
        "WHERE amount IS NULL OR turnover_rate IS NULL")


def _local_full(kind: str, files: dict, shares: dict[str, float]) -> None:
    """先写暂存表再原子换名：任务中断时现有日线库不受影响。

    两个目录（不复权 + 等比后复权）必须在同一个任务里合并成同一行写入，
    否则第二次导入走同一条换名路径会把第一次写的列整体抹掉。
    """
    with db.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS kline_stage")
        conn.execute(
            "CREATE TABLE kline_stage("
            "code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,"
            "adj_factor REAL, volume REAL, amount REAL, pct_chg REAL, turnover REAL,"
            "PRIMARY KEY(code,date))")
    total = len(files)
    done = failed = rows = 0
    batch: list[tuple] = []
    sql = ("INSERT OR IGNORE INTO kline_stage"
           "(code,date,open,high,low,close,adj_factor,volume,amount,pct_chg,turnover) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?)")
    for code, (raw_p, geo_p) in files.items():
        try:
            bars, _stats = qmt_src.read_pair(raw_p, geo_p)
        except Exception:
            failed += 1
            bars = []
        fs = shares.get(code)
        batch.extend(_bar_row(code, b, fs) for b in bars)
        rows += len(bars)
        done += 1
        if len(batch) >= 50000 or done == total:
            with db.transaction() as conn:
                conn.executemany(sql, batch)
            batch = []
        if done % 100 == 0 or done == total:
            _update(kind, progress=done, total=total,
                    message=f"导入 QMT 导出 {done}/{total}")
    with db.transaction() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_kline_date")
        conn.execute("DROP TABLE IF EXISTS kline_daily_old")
        conn.execute("ALTER TABLE kline_daily RENAME TO kline_daily_old")
        conn.execute("ALTER TABLE kline_stage RENAME TO kline_daily")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_date ON kline_daily(date)")
    db.execute("DROP TABLE IF EXISTS kline_daily_old")
    msg = f"导入 {rows} 行 / {total} 只"
    if failed:
        msg += f"，失败 {failed} 只"
    _update(kind, message=msg, final=True)


def _local_incremental(kind: str, files: dict, shares: dict[str, float]) -> None:
    last = {r["code"]: r["d"] for r in db.query(
        "SELECT code, MAX(date) AS d FROM kline_daily GROUP BY code")}
    total = len(files)
    done = failed = added = 0
    for code, (raw_p, geo_p) in files.items():
        try:
            bars, _stats = qmt_src.read_pair(raw_p, geo_p)
        except Exception:
            failed += 1
            bars = []
        cutoff = last.get(code)
        if cutoff:
            # 含 cutoff 当天：末根若是盘中导出的快照，重新导出后可以在这里覆盖修正
            bars = [b for b in bars if b["date"] >= cutoff]
        if bars:
            fs = shares.get(code)
            db.execute(_KLINE_INSERT,
                       many=[_bar_row(code, b, fs) for b in bars])
            added += len(bars)
        done += 1
        if done % 100 == 0 or done == total:
            _update(kind, progress=done, total=total,
                    message=f"导入 QMT 导出 {done}/{total}")
    msg = f"补齐 {added} 行"
    if failed:
        msg += f"，失败 {failed} 只"
    _update(kind, message=msg, final=True)
