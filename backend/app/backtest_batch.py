"""全市场批量回测：一个策略跑遍所有股票，产出逐只成绩单、收益分布、按年分解和等权曲线。

和单只回测共用 `backtest.walk_ledger`，所以同一只股票两边的收益率一定相等。
跑批按 400 只一块取数（每块一次 IN 查询），计算放在查询之外，这样不会长时间占着
那条共用连接——但全市场 1~2 分钟里其它页面仍会排队，页面上要写清楚。
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

import numpy as np
import pandas as pd

from . import backtest, db
from .strategies.base import get_strategy

MIN_BARS = 30
CHUNK = 400
STATUS_KEY = "bt_batch_status"
TOP_N_ALLOWED = (20, 50, 100)
# 一次批次 = 5545 行逐只成绩单 ≈ 2MB，留太多只是在长胖：每次跑批后只留最近这些个
KEEP_BATCHES = 20

_lock = threading.Lock()
_thread: threading.Thread | None = None


def _idle() -> dict:
    return {"status": "idle", "progress": 0, "total": 0, "message": "",
            "batch_id": "", "started_at": "", "elapsed_sec": None}


def status() -> dict:
    st = db.kv_get(STATUS_KEY) or _idle()
    out = {**_idle(), **st}
    with _lock:
        alive = _thread is not None and _thread.is_alive()
    if out["status"] == "running" and not alive:
        # 服务重启会带走线程，但 kv 里还留着 running 徽章
        out.update({"status": "error", "message": "任务已中断（服务重启）"})
        _save(out)
    return out


def _save(st: dict) -> None:
    db.kv_set(STATUS_KEY, st)


def _progress(**kw) -> None:
    st = {**_idle(), **(db.kv_get(STATUS_KEY) or {}), **kw}
    _save(st)


def is_busy() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def start(strategy_id: str, params: dict, start: str, end: str,
          capital: float, fee_rate: float) -> tuple[str, dict]:
    """启动跑批；已经在跑或正在做数据更新就挡住，别把两个全市场任务叠在一起。

    返回 (提示, 刚写入的 running 状态)，提示不是 "started" 时状态为空，
    给页面的提示都是可执行动作原文。批次号和状态在起线程前就落好，
    并把这份状态直接回给调用方——否则接口要再读一次 kv，而读 kv 得排在跑批线程的
    全市场查询后面，按钮点下去要转十几秒才出进度条。
    """
    from .datasource import updater
    if get_strategy(strategy_id) is None:
        return f"没有这个策略：{strategy_id}", {}
    if not (start and end and end > start):
        return f"区间不成立：{start} ~ {end}，结束日期要晚于开始日期", {}
    if capital < 100:
        return "每只初始资金太小（不到 100 元），连最便宜股票的一手都买不起，改成 100 万以上再跑", {}
    if updater.is_busy():
        return "数据更新正在跑，等它结束再跑批（两者都要扫全市场，会互相拖慢）", {}
    batch_id = f"{strategy_id}_{start}_{end}_{dt.datetime.now():%Y%m%d%H%M%S}"
    with _lock:
        global _thread
        if _thread is not None and _thread.is_alive():
            return "全市场回测正在跑，等这一次跑完再启动（约 1~2 分钟）", {}
        st = {**_idle(), "status": "running", "batch_id": batch_id,
              "started_at": dt.datetime.now().isoformat(timespec="seconds"),
              "message": "正在统计区间内的股票"}
        _save(st)
        _thread = threading.Thread(
            target=_run, args=(batch_id, strategy_id, params or {}, start, end,
                               capital, fee_rate),
            daemon=True)
        _thread.start()
    return "started", st


def _codes_in_range(start: str, end: str) -> tuple[list[str], int]:
    rows = db.query_rows(
        "SELECT code, COUNT(*) AS n FROM kline_daily WHERE date>=? AND date<=? GROUP BY code",
        (start, end))
    ok = sorted(r[0] for r in rows if r[1] >= MIN_BARS)
    return ok, len(rows) - len(ok)


def _year_bounds(dates: list[str]) -> dict[str, tuple[int, int]]:
    """每个自然年在交易日网格上的首末下标，用来切年度收益。"""
    out: dict[str, tuple[int, int]] = {}
    for i, d in enumerate(dates):
        y = d[:4]
        if y in out:
            out[y] = (out[y][0], i)
        else:
            out[y] = (i, i)
    return out


def _year_returns(idxs: np.ndarray, eq: np.ndarray, capital: float,
                  bounds: dict[str, tuple[int, int]]) -> dict[str, float]:
    """该只每年的收益：以「上一年最后一根净值」为基准。

    上一整年一根K线都没有（长停牌/那年才上市）就不算这一年——否则会把两年的涨幅
    记到一年头上。区间第一年没有上一根，基准取初始资金。
    """
    out = {}
    prev_start: int | None = None
    for y in sorted(bounds):
        a, b = bounds[y]
        lo = int(np.searchsorted(idxs, a, side="left"))
        hi = int(np.searchsorted(idxs, b, side="right"))
        if hi - lo >= 2:
            if lo == 0:
                base = float(capital)
            elif prev_start is not None and idxs[lo - 1] >= prev_start:
                base = float(eq[lo - 1])
            else:
                base = 0.0
            if base > 0:
                out[y] = round((float(eq[hi - 1]) / base - 1) * 100, 2)
        prev_start = a
    return out


def _chunk_bars(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    q = ",".join("?" * len(codes))
    rows = db.query_rows(
        f"SELECT code, date, open*adj_factor, high*adj_factor, low*adj_factor, "
        f"close*adj_factor, volume, amount, pct_chg, close, adj_factor "
        f"FROM kline_daily WHERE code IN ({q}) AND date>=? AND date<=? "
        f"ORDER BY code, date", (*codes, start, end))
    df = pd.DataFrame(rows, columns=[
        "code", "date", "open", "high", "low", "close", "volume", "amount",
        "pct_chg", "raw_close", "adj_factor"])
    return {c: g.reset_index(drop=True) for c, g in df.groupby("code", sort=False)}


def _run(batch_id: str, strategy_id: str, params: dict, start: str, end: str,
         capital: float, fee_rate: float) -> None:
    t0 = time.time()
    try:
        codes, skipped = _codes_in_range(start, end)
        if not codes:
            raise ValueError(f"{start} ~ {end} 区间内没有足够K线的股票，先去做数据更新")
        all_dates = [r[0] for r in db.query_rows(
            "SELECT DISTINCT date FROM kline_daily WHERE date>=? AND date<=? ORDER BY date",
            (start, end))]
        dates_arr = np.asarray(all_dates, dtype=object)
        bounds = _year_bounds(all_dates)
        names = {r["code"]: (r["name"] or "", r["industry"] or "") for r in db.query(
            "SELECT code, name, industry FROM stocks")}
        strategy = get_strategy(strategy_id)

        _progress(total=len(codes))
        eq_sum = np.zeros(len(all_dates))
        eq_cnt = np.zeros(len(all_dates))
        items: list[tuple] = []
        per_year: dict[str, list[float]] = {}
        t_load = t_walk = 0.0
        done = 0
        no_factor = 0
        for i in range(0, len(codes), CHUNK):
            part = codes[i:i + CHUNK]
            a = time.time()
            bars = _chunk_bars(part, start, end)
            b = time.time()
            for code, df in bars.items():
                if df["adj_factor"].isna().any():
                    # 区间里有缺复权因子的根（QMT 等比那份坏掉），复权价算不出来，
                    # 与其静默丢掉这几根把持股段接错，不如不纳入本次统计
                    no_factor += 1
                    continue
                sig = strategy.generate_signals(df, params)
                led = backtest.walk_ledger(code, df, sig, capital, fee_rate)
                if led["bars"] < MIN_BARS:
                    skipped += 1
                    continue
                name, industry = names.get(code, (code, ""))
                idxs = np.searchsorted(dates_arr, np.asarray(led["dates"], dtype=object))
                norm = led["equity"] / capital
                eq_sum[idxs] += norm
                eq_cnt[idxs] += 1
                excess = (led["pnl_pct"] or 0) - (led["bh_pct"] or 0)
                yrs = _year_returns(idxs, led["equity"], capital, bounds)
                for y, v in yrs.items():
                    per_year.setdefault(y, []).append(v)
                items.append((batch_id, code, name, industry, led["bars"],
                              led["trade_count"], led["order_count"], led["missed"],
                              round(led["pnl_pct"], 2), round(led["bh_pct"], 2),
                              round(excess, 2),
                              round(led["max_dd_pct"], 2) if led["max_dd_pct"] is not None else None,
                              round(led["fees_total"], 2),
                              json.dumps(yrs, ensure_ascii=False)))
            done += len(part)
            t_load += b - a
            t_walk += time.time() - b
            _progress(progress=done, message=f"已完成 {done}/{len(codes)}（{strategy.name}）")
        _persist(batch_id, strategy_id, strategy.name, params, start, end,
                 capital, fee_rate, items, eq_sum, eq_cnt, all_dates, per_year,
                 skipped, no_factor, t0, t_load, t_walk)
    except Exception as e:  # 线程里抛出去没人接，必须落到状态里
        _progress(status="error", message=f"跑批失败：{e}")
        raise


def _persist(batch_id: str, strategy_id: str, strategy_name: str, params: dict,
             start: str, end: str, capital: float, fee_rate: float,
             items: list[tuple], eq_sum: np.ndarray, eq_cnt: np.ndarray,
             all_dates: list[str], per_year: dict[str, list[float]],
             skipped: int, no_factor: int, t0: float, t_load: float,
             t_walk: float) -> None:
    pnl = np.array([r[8] for r in items]) if items else np.array([])
    exc = np.array([r[10] for r in items]) if items else np.array([])
    dd = np.array([r[11] for r in items if r[11] is not None])
    buys = np.array([r[5] for r in items]) if items else np.array([])
    curve = np.where(eq_cnt > 0, eq_sum / np.where(eq_cnt > 0, eq_cnt, 1), np.nan)
    # 前一段有值、后一段还没股票进来时不要断图，按前值续上
    if curve.size:
        idx = np.flatnonzero(~np.isnan(curve))
        if idx.size:
            first = idx[0]
            curve[:first] = curve[first]
    years = [{"year": y, "count": len(v), "median": round(float(np.median(v)), 2),
              "mean": round(float(np.mean(v)), 2),
              "win_rate": round(float(np.mean(np.asarray(v) > 0)) * 100, 1)}
             for y, v in sorted(per_year.items())]
    # 分布：分位数说「有多分散」，直方图说「形状」（双峰/长尾只看均值和中位数是看不出来的）
    def _dist(vals: np.ndarray) -> dict:
        """分位数 + 直方图。P10 = 从最差往好排第 10% 名的数值。"""
        if not vals.size:
            return {"count": 0}
        edges = [-100, -50, -30, -20, -10, 0, 10, 20, 30, 50, 100, 200, 300, 500]
        cnt, _ = np.histogram(vals, bins=edges)
        bins = [{"lo": edges[i], "hi": edges[i + 1], "count": int(cnt[i])}
                for i in range(len(edges) - 1)]
        # np.histogram 会丢掉两端越界的值，直方图就得能对上总数，越界单独成档
        under = int((vals < edges[0]).sum())
        if under:
            bins.insert(0, {"lo": None, "hi": edges[0], "count": under})
        over = int((vals > edges[-1]).sum())
        if over:
            bins.append({"lo": edges[-1], "hi": None, "count": over})
        return {
            "count": int(vals.size),
            "min": round(float(vals.min()), 2), "max": round(float(vals.max()), 2),
            "mean": round(float(vals.mean()), 2),
            "quantiles": {f"p{int(p):02d}": round(float(np.percentile(vals, p)), 2)
                          for p in (5, 10, 25, 50, 75, 90, 95)},
            "bins": bins,
        }

    dist = {"pnl": _dist(pnl), "excess": _dist(exc)}
    summary = {
        "win_count": int((pnl > 0).sum()) if pnl.size else 0,
        "win_rate": round(float((pnl > 0).mean()) * 100, 1) if pnl.size else None,
        "beat_bh_count": int((exc > 0).sum()) if exc.size else 0,
        "pnl_median": round(float(np.median(pnl)), 2) if pnl.size else None,
        "pnl_mean": round(float(np.mean(pnl)), 2) if pnl.size else None,
        "pnl_p25": round(float(np.percentile(pnl, 25)), 2) if pnl.size else None,
        "pnl_p75": round(float(np.percentile(pnl, 75)), 2) if pnl.size else None,
        "pnl_best": round(float(pnl.max()), 2) if pnl.size else None,
        "pnl_worst": round(float(pnl.min()), 2) if pnl.size else None,
        "excess_median": round(float(np.median(exc)), 2) if exc.size else None,
        "excess_p25": round(float(np.percentile(exc, 25)), 2) if exc.size else None,
        "excess_p75": round(float(np.percentile(exc, 75)), 2) if exc.size else None,
        "buy_count_mean": round(float(np.mean(buys)), 1) if buys.size else None,
        "max_dd_median": round(float(np.median(dd)), 2) if dd.size else None,
        "fees_total": round(float(sum(r[12] for r in items)), 2),
        "missed_total": int(sum(r[7] for r in items)),
        "missed_stocks": int(sum(1 for r in items if r[7] > 0)),
        "no_factor_stocks": no_factor,
        "years": years,
        "dist": dist,
        "curve": {"dates": all_dates,
                  "eq": [None if np.isnan(v) else round(float(v), 4) for v in curve]},
        "elapsed_load": round(t_load, 1), "elapsed_walk": round(t_walk, 1),
    }
    now = dt.datetime.now().isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute("DELETE FROM bt_batch_item WHERE batch_id=?", (batch_id,))
        conn.execute("DELETE FROM bt_batch WHERE id=?", (batch_id,))
        conn.execute(
            "INSERT INTO bt_batch (id, run_at, strategy_id, strategy_name, params, start, end,"
            " capital, fee_rate, stock_count, skipped, elapsed_sec, summary)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, now, strategy_id, strategy_name,
             json.dumps(params, ensure_ascii=False), start, end, capital, fee_rate,
             len(items), skipped, round(time.time() - t0, 1),
             json.dumps(summary, ensure_ascii=False)))
        if items:
            conn.executemany(
                "INSERT INTO bt_batch_item (batch_id, code, name, industry, bars, buy_count,"
                " order_count, missed, pnl_pct, bh_pct, excess_pct, max_dd_pct, fees, years)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", items)
        stale = prune(conn)
    _progress(status="done", progress=len(items), total=len(items), batch_id=batch_id,
              message=f"跑完 {len(items)} 只，耗时 {time.time()-t0:.0f}s"
                      + (f"，K线不足未纳入 {skipped} 只" if skipped else "")
                      + (f"，缺复权因子未纳入 {no_factor} 只（在 QMT 重导这两份再增量导入即可补上）"
                         if no_factor else "")
                      + (f"，清掉 {len(stale)} 个超期批次（只留最近 {KEEP_BATCHES} 个）" if stale else ""))


def list_batches(limit: int = KEEP_BATCHES) -> list[dict]:
    rows = db.query(
        "SELECT id, run_at, strategy_id, strategy_name, params, start, end, capital,"
        " fee_rate, stock_count, skipped, elapsed_sec FROM bt_batch "
        "ORDER BY run_at DESC LIMIT ?", (limit,))
    for r in rows:
        r["params"] = json.loads(r["params"] or "{}")
    return rows


def get_batch(batch_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM bt_batch WHERE id=?", (batch_id,))
    if not row:
        return None
    row["params"] = json.loads(row["params"] or "{}")
    row["summary"] = json.loads(row["summary"] or "{}")
    return row


def prune(conn, keep: int = KEEP_BATCHES) -> list[str]:
    """只留最近 keep 个批次（按 run_at 倒序），返回被清掉的批次号。

    跟本次写入同一个事务，所以不会出现「批次头在、成绩单被清掉」的半截状态。
    """
    stale = [r[0] for r in conn.execute(
        "SELECT id FROM bt_batch ORDER BY run_at DESC, id DESC LIMIT -1 OFFSET ?", (keep,))]
    for sid in stale:
        conn.execute("DELETE FROM bt_batch_item WHERE batch_id=?", (sid,))
        conn.execute("DELETE FROM bt_batch WHERE id=?", (sid,))
    return stale


def delete(batch_id: str) -> dict:
    """删掉一批留痕（批次头 + 逐只成绩单），只动这两张表，日线库不碰。

    跑批进行中不删：那一批的成绩单要在跑完时才整批写进来，中途删了等于白跑。
    """
    st = status()
    if st["status"] == "running":
        return {"error": "全市场回测正在跑，等这一次跑完再删批次"}
    head = db.query_one(
        "SELECT id, strategy_name, start, end, capital, stock_count FROM bt_batch WHERE id=?",
        (batch_id,))
    if not head:
        return {"error": f"批次 {batch_id} 已经不在库里了，重新选一个批次"}
    with db.transaction() as conn:
        n = conn.execute("SELECT COUNT(*) FROM bt_batch_item WHERE batch_id=?",
                         (batch_id,)).fetchone()[0]
        conn.execute("DELETE FROM bt_batch_item WHERE batch_id=?", (batch_id,))
        conn.execute("DELETE FROM bt_batch WHERE id=?", (batch_id,))
    return {"deleted": batch_id, "items": n, "batch": head}


def items(batch_id: str, sort: str = "excess_pct", desc: bool = True,
          limit: int = 100, offset: int = 0, q: str = "") -> dict:
    cols = {"code": "code", "name": "name", "industry": "industry", "bars": "bars",
            "buy_count": "buy_count", "order_count": "order_count", "missed": "missed",
            "pnl_pct": "pnl_pct", "bh_pct": "bh_pct", "excess_pct": "excess_pct",
            "max_dd_pct": "max_dd_pct", "fees": "fees"}
    key = cols.get(sort, "excess_pct")
    where, args = ["batch_id=?"], [batch_id]
    if q:
        where.append("(code LIKE ? OR name LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    w = " AND ".join(where)
    total = db.query_one(f"SELECT COUNT(*) AS n FROM bt_batch_item WHERE {w}",
                         tuple(args))["n"]
    rows = db.query(
        f"SELECT code, name, industry, bars, buy_count, order_count, missed, pnl_pct,"
        f" bh_pct, excess_pct, max_dd_pct, fees, years FROM bt_batch_item WHERE {w} "
        f"ORDER BY {key} {'DESC' if desc else 'ASC'}, code ASC LIMIT ? OFFSET ?",
        (*args, min(max(limit, 1), 500), max(offset, 0)))
    for r in rows:
        r["years"] = json.loads(r["years"] or "{}")
    return {"total": total, "sort": key, "desc": desc, "rows": rows}


def curve(batch_id: str, top_n: int = 0) -> dict:
    """top_n=0 用跑批时累加的全体等权净值；否则按超额收益取前 N 只现重放一遍。

    前 N 是拿同一批样本「事后按收益挑的」，必然比全体好看，页面上要写清楚。
    """
    head = get_batch(batch_id)
    if not head:
        return {"error": "批次不存在"}
    if not top_n:
        return {"batch_id": batch_id, "top_n": 0, "label": "全部参与股票等权",
                "biased": False, **head["summary"].get("curve", {})}
    top_n = top_n if top_n in TOP_N_ALLOWED else TOP_N_ALLOWED[0]
    picked = db.query(
        "SELECT code FROM bt_batch_item WHERE batch_id=? "
        "ORDER BY excess_pct DESC, code LIMIT ?", (batch_id, top_n))
    codes = [r["code"] for r in picked]
    strategy = get_strategy(head["strategy_id"])
    if strategy is None:
        return {"error": f"策略 {head['strategy_id']} 已不存在，无法重放曲线"}
    dates = head["summary"].get("curve", {}).get("dates") or [
        r[0] for r in db.query_rows(
            "SELECT DISTINCT date FROM kline_daily WHERE date>=? AND date<=? ORDER BY date",
            (head["start"], head["end"]))]
    arr = np.asarray(dates, dtype=object)
    capital = head["capital"]
    s = np.zeros(len(dates))
    c = np.zeros(len(dates))
    for code, df in _chunk_bars(codes, head["start"], head["end"]).items():
        if df["adj_factor"].isna().any():
            continue    # 与跑批同一口径：缺复权因子的股票不重放，否则净值曲线会接错
        led = backtest.walk_ledger(code, df,
                                   strategy.generate_signals(df, head["params"]),
                                   capital, head["fee_rate"])
        if not led["bars"]:
            continue
        idxs = np.searchsorted(arr, np.asarray(led["dates"], dtype=object))
        s[idxs] += led["equity"] / capital
        c[idxs] += 1
    curve_v = np.where(c > 0, s / np.where(c > 0, c, 1), np.nan)
    idx = np.flatnonzero(~np.isnan(curve_v))
    if idx.size:
        curve_v[:idx[0]] = curve_v[idx[0]]
    return {"batch_id": batch_id, "top_n": top_n,
            "label": f"超额收益前 {top_n} 只等权", "biased": True,
            "dates": dates,
            "eq": [None if np.isnan(v) else round(float(v), 4) for v in curve_v]}
