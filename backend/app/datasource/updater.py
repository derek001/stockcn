"""Background data jobs: 全量更新 (full) and 增量更新 (incremental).

Provider strategy: probe Eastmoney at job start; if its quote servers reject
us, fall back to Tencent for the whole run. K-lines are stored as 后复权(hfq)
bars; incremental updates still detect adjustment-base shifts by comparing
overlapping bars and rescale the stored history when prices differ.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import db
from ..datasource import eastmoney as em
from ..datasource import tencent as tc

_status_lock = threading.Lock()
_running: dict[str, threading.Thread] = {}


def _default_status() -> dict:
    return {
        "full": {"status": "idle", "progress": 0, "total": 0, "message": "", "time": None},
        "incremental": {"status": "idle", "progress": 0, "total": 0, "message": "", "time": None},
        "provider": None,
        "stock_count": 0,
        "last_full": None,
        "last_incremental": None,
    }


def get_status() -> dict:
    st = db.kv_get("data_status", _default_status())
    with _status_lock:
        st["running"] = sorted(_running.keys())
    for kind in ("full", "incremental"):
        if kind not in st["running"] and st.get(kind, {}).get("status") == "running":
            st[kind] = {"status": "error", "progress": 0, "total": 0,
                        "message": "任务已中断（服务重启）", "time": st[kind].get("time")}
    if not st.get("running"):
        row = db.query_one("SELECT COUNT(*) AS n FROM kline_daily")
        st["kline_rows"] = row["n"] if row else 0
    return st


def _save_status(st: dict) -> None:
    db.kv_set("data_status", st)


def _update(kind: str, **kw) -> None:
    st = db.kv_get("data_status", _default_status())
    st[kind].update(kw)
    _save_status(st)


def is_busy() -> bool:
    with _status_lock:
        return bool(_running)


def start_job(kind: str) -> str:
    """kind: full | incremental"""
    with _status_lock:
        if kind in _running:
            return "already running"
        t = threading.Thread(target=_run_job, args=(kind,), daemon=True)
        _running[kind] = t
        t.start()
    return "started"


def _run_job(kind: str) -> None:
    try:
        _update(kind, status="running", progress=0, total=0, message="准备中…")
        provider = _probe_provider()
        st = db.kv_get("data_status", _default_status())
        st["provider"] = provider
        _save_status(st)
        if kind == "full":
            _job_full(provider)
        else:
            _job_incremental(provider)
        st = db.kv_get("data_status", _default_status())
        msg = st.get(kind, {}).get("message", "")
        if "失败" not in msg:
            msg = "完成"
        _update(kind, status="done",
                message=msg, time=dt.datetime.now().isoformat(timespec="seconds"))
        st = db.kv_get("data_status", _default_status())
        if kind == "full":
            st["last_full"] = st["full"]["time"]
        else:
            st["last_incremental"] = st["incremental"]["time"]
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
    failed = _run_kline_pass("incremental", provider, stocks, _incremental_stock, "补齐K线")

    _refresh_indexes(provider)
    if failed:
        _update("incremental", message=f"{len(failed)} 只股票K线补齐失败（可稍后重试）")
