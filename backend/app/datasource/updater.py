"""Background data jobs: 全量更新 (full)、增量更新 (incremental) and 本地导入 (qmt_full /
qmt_incremental) from a QMT export directory.

Provider strategy: probe Eastmoney at job start; if its quote servers reject
us, fall back to Tencent for the whole run. K-lines are stored as 后复权(hfq)
bars; incremental updates still detect adjustment-base shifts by comparing
overlapping bars and rescale the stored history when prices differ.

hfq bases differ between providers, so a database built by one source must not
be topped up by another: `data_status.kline_source` records which ('remote' or
'qmt') owns the current bars. On a QMT library the online 增量更新 keeps refreshing
lists/reports/indexes but skips the K-line pass, and 全量更新 is refused outright.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        "kline_source": None,
        "stock_count": 0,
        "last_full": None,
        "last_incremental": None,
        "last_local": None,
    }


def _norm(st: dict) -> dict:
    base = _default_status()
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


def kline_source() -> str | None:
    return _norm(db.kv_get("data_status", _default_status())).get("kline_source")


def _save_status(st: dict) -> None:
    db.kv_set("data_status", st)


def _update(kind: str, **kw) -> None:
    st = _norm(db.kv_get("data_status", _default_status()))
    st[_status_key(kind)].update(kw)
    _save_status(st)


def is_busy() -> bool:
    with _status_lock:
        return bool(_running)


def start_job(kind: str, path: str | None = None, force: bool = False) -> str:
    """kind: full | incremental | qmt_full | qmt_incremental"""
    with _status_lock:
        if kind in _running:
            return "already running"
        t = threading.Thread(target=_run_job, args=(kind, path, force), daemon=True)
        _running[kind] = t
        t.start()
    return "started"


def _run_job(kind: str, path: str | None = None, force: bool = False) -> None:
    try:
        _update(kind, status="running", progress=0, total=0, message="准备中…")
        if kind in LOCAL_KINDS:
            _job_local(kind, path or "", force)
            source = "qmt"
        else:
            provider = _probe_provider()
            st = _norm(db.kv_get("data_status", _default_status()))
            st["provider"] = provider
            _save_status(st)
            if kind == "full":
                _job_full(provider)
            else:
                _job_incremental(provider)
            source = "remote"
        st = _norm(db.kv_get("data_status", _default_status()))
        key = _status_key(kind)
        msg = st[key].get("message", "")
        if not st[key].get("final") and "失败" not in msg:
            msg = "完成"
        now = dt.datetime.now().isoformat(timespec="seconds")
        _update(kind, status="done", message=msg, time=now, final=False)
        st = _norm(db.kv_get("data_status", _default_status()))
        st["kline_source"] = source
        st[f"last_{key}"] = now
        _save_status(st)
    except Exception as e:
        traceback.print_exc()
        _update(kind, status="error", message=f"失败: {e}")
    finally:
        with _status_lock:
            _running.pop(kind, None)


def _probe_provider() -> str:
    try:
        rows = em.fetch_kline("1.600000", beg="20250101", end="20250201")
        if rows:
            return "em"
    except Exception:
        pass
    return "tc"


# ---------- stock list & fundamentals ----------

def _refresh_stock_list(kind: str) -> None:
    _update(kind, message="更新股票列表与基本面快照…")
    stocks = None
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
        db.execute(
            "INSERT INTO stocks(code,name,market,secid,industry,board_code,list_date,is_active) "
            "VALUES(:code,:name,:market,:secid,:industry,:board_code,:list_date,:is_active) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, "
            "secid=excluded.secid, industry=excluded.industry, "
            "list_date=COALESCE(excluded.list_date, stocks.list_date), is_active=1",
            many=rows,
        )
        db.execute(
            "INSERT INTO fundamentals(code,trade_date,price,pct_chg,total_mv,float_mv,pe_dynamic,pe_ttm,pb,turnover_rate,amount) "
            "VALUES(:code,:trade_date,:price,:pct_chg,:total_mv,:float_mv,:pe_dynamic,:pe_ttm,:pb,:turnover_rate,:amount) "
            "ON CONFLICT(code) DO UPDATE SET trade_date=excluded.trade_date, price=excluded.price, "
            "pct_chg=excluded.pct_chg, total_mv=excluded.total_mv, float_mv=excluded.float_mv, "
            "pe_dynamic=excluded.pe_dynamic, pe_ttm=excluded.pe_ttm, pb=excluded.pb, "
            "turnover_rate=excluded.turnover_rate, amount=excluded.amount",
            many=[{**{k: s[k] for k in ("code", "price", "pct_chg", "total_mv", "float_mv",
                                        "pe_dynamic", "pe_ttm", "pb")},
                   "trade_date": dt.date.today().isoformat(),
                   "turnover_rate": None, "amount": s["amount"]} for s in stocks],
        )
        return
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
            all_rows.append({**it, "board_code": b["board_code"], "board_name": b["board_name"]})
    seen = set()
    srows, frows = [], []
    today = dt.date.today().isoformat()
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
                      "turnover_rate": None, "amount": None})
    if srows:
        db.execute(
            "INSERT INTO stocks(code,name,market,secid,industry,board_code,list_date,is_active) "
            "VALUES(:code,:name,:market,:secid,:industry,:board_code,:list_date,:is_active) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market, "
            "secid=excluded.secid, "
            "industry=COALESCE(excluded.industry, stocks.industry), "
            "board_code=COALESCE(excluded.board_code, stocks.board_code), is_active=1",
            many=srows,
        )
        db.execute(
            "INSERT INTO fundamentals(code,trade_date,price,pct_chg,total_mv,float_mv,pe_dynamic,pe_ttm,pb,turnover_rate,amount) "
            "VALUES(:code,:trade_date,:price,:pct_chg,:total_mv,:float_mv,:pe_dynamic,:pe_ttm,:pb,:turnover_rate,:amount) "
            "ON CONFLICT(code) DO UPDATE SET trade_date=excluded.trade_date, price=excluded.price, "
            "pct_chg=excluded.pct_chg, total_mv=excluded.total_mv, float_mv=excluded.float_mv, "
            "pe_dynamic=excluded.pe_dynamic, pe_ttm=excluded.pe_ttm, pb=excluded.pb",
            many=frows,
        )
    db.execute(
        "INSERT INTO boards(board_code,board_name,board_type) VALUES(:board_code,:board_name,:board_type) "
        "ON CONFLICT(board_code) DO UPDATE SET board_name=excluded.board_name",
        many=[{"board_code": b["board_code"], "board_name": b["board_name"],
               "board_type": "industry"} for b in boards],
    )
    st = db.kv_get("data_status", _default_status())
    st["stock_count"] = len(srows)
    _save_status(st)


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


# ---------- klines ----------

def _upsert_klines(code: str, rows: list[dict]) -> None:
    # gtimg's hfq series turns negative for a handful of stocks' earliest years,
    # which makes every indicator on that segment meaningless; not worth storing.
    rows = [r for r in rows if r["close"] is not None and r["close"] > 0]
    if not rows:
        return
    db.execute(
        "INSERT OR REPLACE INTO kline_daily(code,date,open,high,low,close,volume,amount,pct_chg,turnover) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        many=[(code, r["date"], r["open"], r["high"], r["low"], r["close"],
               r["volume"], r["amount"], r["pct_chg"], r["turnover"]) for r in rows],
    )


def _fetch_kline_full(provider: str, stock: dict) -> list[dict]:
    if provider == "em":
        return em.fetch_kline(stock["secid"])
    tcode = f"{stock['market'].lower()}{stock['code']}"
    return tc.fetch_kline(tcode)


def _fetch_full_kline(provider: str, stock: dict) -> None:
    _upsert_klines(stock["code"], _fetch_kline_full(provider, stock))


def _covered_codes() -> set[str]:
    """Stocks that already hold a deep history reaching the newest date locally."""
    ref = db.query_one("SELECT MAX(date) AS d FROM kline_daily")
    if not ref or not ref["d"]:
        return set()
    cutoff = (dt.date.fromisoformat(ref["d"]) - dt.timedelta(days=7)).isoformat()
    rows = db.query("SELECT code, COUNT(*) AS n, MAX(date) AS d FROM kline_daily GROUP BY code")
    return {r["code"] for r in rows if r["d"] >= cutoff and r["n"] >= 200}


def _run_kline_pass(kind: str, provider: str, stocks: list[dict], fetch, label: str) -> list[dict]:
    """Run fetch() over stocks; return the ones that still failed after retries.

    tencent._pace() holds a global cooldown, so a stock that hits the WAF waits
    the pause out on its next attempt instead of hammering the CDN.
    """
    total = len(stocks)
    if not total:
        return []
    _update(kind, progress=0, total=total, message=f"{label} 0/{total}")
    done = [0]
    failed: list[dict] = []

    def worker(s: dict) -> bool:
        for attempt in range(3):
            try:
                fetch(provider, s)
                return True
            except Exception:
                time.sleep(1.0 + attempt)
        return False

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(worker, s): s for s in stocks}
        for f in as_completed(futs):
            done[0] += 1
            if not f.result():
                failed.append(futs[f])
            if done[0] % 25 == 0 or done[0] == total:
                _update(kind, progress=done[0], total=total,
                        message=f"{label} {done[0]}/{total}，失败 {len(failed)}")
    return failed


def _job_full(provider: str) -> None:
    _refresh_stock_list("full")
    if provider == "em":
        _refresh_em_boards("full")
    _refresh_financials("full", 12)
    # refresh the market indexes up front: a run interrupted hours later must not
    # leave 大盘线 trailing the individual stocks by several trading days
    _refresh_indexes(provider)

    stocks = db.query("SELECT code,name,market,secid FROM stocks WHERE is_active=1")
    covered = _covered_codes()
    missing = [s for s in stocks if s["code"] not in covered]
    topped = [s for s in stocks if s["code"] in covered]

    # stocks with usable history only need a windowed top-up, which makes a
    # re-run after a partial failure resume instead of re-downloading everything
    f_missing = _run_kline_pass("full", provider, missing, _fetch_full_kline, "下载K线")
    if f_missing:
        f_missing = _run_kline_pass("full", provider, f_missing, _fetch_full_kline, "重试K线")
    f_topped = _run_kline_pass("full", provider, topped, _incremental_stock, "补齐K线")
    if f_topped:
        f_topped = _run_kline_pass("full", provider, f_topped, _incremental_stock, "重试补齐K线")

    _refresh_indexes(provider)
    cnt = db.query_one("SELECT COUNT(*) AS n FROM kline_daily")
    st = db.kv_get("data_status", _default_status())
    st["stock_count"] = len(stocks)
    st["kline_rows"] = cnt["n"] if cnt else 0
    _save_status(st)

    n_fail = len(f_missing) + len(f_topped)
    msg = (f"K线：首次下载 {len(missing) - len(f_missing)} 只，"
           f"更新 {len(topped) - len(f_topped)} 只")
    if n_fail:
        msg += f"，失败 {n_fail} 只（再跑一次全量更新可续传）"
    _update("full", progress=len(stocks), total=len(stocks), message=msg)


def _incremental_stock(provider: str, stock: dict) -> None:
    code = stock["code"]
    last = db.query_one("SELECT MAX(date) AS d FROM kline_daily WHERE code=?", (code,))
    last_date = last["d"] if last else None
    if not last_date:
        # incremental only tops up locally-available stocks; full download
        # belongs to the 全量更新 job
        return
    if provider == "em":
        beg = (dt.date.fromisoformat(last_date) - dt.timedelta(days=45)).strftime("%Y%m%d")
        new_rows = em.fetch_kline(stock["secid"], beg=beg, end="20500101")
    else:
        start = (dt.date.fromisoformat(last_date) - dt.timedelta(days=45)).isoformat()
        tcode = f"{stock['market'].lower()}{code}"
        new_rows = tc.fetch_kline_range(tcode, start)
    if not new_rows:
        return
    # windowed fetches lack pct_chg; recompute it from adjacent closes so the
    # overwrite doesn't blank previously stored values
    prev_row = db.query_one(
        "SELECT close FROM kline_daily WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
        (code, new_rows[0]["date"]))
    pc = prev_row["close"] if prev_row else None
    for r in new_rows:
        if r["pct_chg"] is None and pc and r["close"] is not None:
            r["pct_chg"] = round((r["close"] - pc) / pc * 100, 4)
        pc = r["close"]
    # hfq prices of old bars never change; overlapping bars may shift after an
    # ex-rights event, so overwrite everything returned by the windowed fetch.
    _upsert_klines(code, new_rows)


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

    stocks = db.query(
        "SELECT s.code, s.name, s.market, s.secid FROM stocks s "
        "WHERE s.is_active=1 AND EXISTS(SELECT 1 FROM kline_daily k WHERE k.code = s.code)")
    if kline_source() == "qmt":
        # 在线源的复权基准与 QMT 不同，日线交给「增量导入」，这里只刷新列表/财报/指数
        _update("incremental", progress=1, total=1, final=True,
                message="日线库为 QMT 导入，已跳过K线补齐（请用「增量导入」更新日线）")
        return
    failed = _run_kline_pass("incremental", provider, stocks, _incremental_stock, "补齐K线")

    _refresh_indexes(provider)
    if failed:
        _update("incremental", message=f"{len(failed)} 只股票K线补齐失败（可稍后重试）")


# ---------- 本地导入（QMT 导出目录） ----------

_KLINE_INSERT = ("INSERT OR REPLACE INTO kline_daily"
                 "(code,date,open,high,low,close,volume,amount,pct_chg,turnover) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)")


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
            b["volume"], b["amount"], b["pct_chg"], turnover)


def preview_local(path: str) -> dict:
    return qmt_src.check_dir(path, _universe(), _snapshot_prices())


def _snapshot_prices() -> dict[str, float]:
    return {r["code"]: r["price"]
            for r in db.query("SELECT code,price FROM fundamentals") if r["price"]}


def _job_local(kind: str, root: str, force: bool = False) -> None:
    """导入前强制体检：路径选错、导出是不复权，都会污染整个日线库。"""
    universe = _universe()
    info = qmt_src.check_dir(root, universe, _snapshot_prices())
    if info["warning"] and not force:
        raise ValueError(info["warning"] + "（确认口径无误可勾选「强制导入」）")
    if not info["matched"] or info["matched"] < len(universe) * 0.8:
        raise ValueError(
            f"目录里只匹配到 {info['matched']}/{len(universe)} 只股票，"
            "疑似路径选错或 QMT 导出尚未完成（不足 80% 覆盖率不允许导入）")
    files = qmt_src.scan_files(root, universe)
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
    """先写暂存表再原子换名：任务中断时现有日线库不受影响。"""
    with db.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS kline_stage")
        conn.execute(
            "CREATE TABLE kline_stage("
            "code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL,"
            "volume REAL, amount REAL, pct_chg REAL, turnover REAL, PRIMARY KEY(code,date))")
    total = len(files)
    done = failed = rows = 0
    batch: list[tuple] = []
    sql = ("INSERT OR IGNORE INTO kline_stage"
           "(code,date,open,high,low,close,volume,amount,pct_chg,turnover) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)")
    for code, path in files.items():
        try:
            bars = qmt_src.read_bars(path)
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
    st = _norm(db.kv_get("data_status", _default_status()))
    st["stock_count"] = len(_universe())
    _save_status(st)
    msg = f"导入 {rows} 行 / {total} 只"
    if failed:
        msg += f"，失败 {failed} 只"
    _update(kind, message=msg, final=True)


def _local_incremental(kind: str, files: dict, shares: dict[str, float]) -> None:
    last = {r["code"]: r["d"] for r in db.query(
        "SELECT code, MAX(date) AS d FROM kline_daily GROUP BY code")}
    total = len(files)
    done = failed = added = 0
    for code, path in files.items():
        try:
            bars = qmt_src.read_bars(path)
        except Exception:
            failed += 1
            bars = []
        cutoff = last.get(code)
        if cutoff:
            bars = [b for b in bars if b["date"] > cutoff]
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
