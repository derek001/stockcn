"""箱体选股的回放校准（证据回路）：历史快照日重跑箱体口径 + 前向交易回放。

回答的问题是「如果过去某个交易日我跑了一次长箱体选股，然后按箱体规则交易，会赚到钱吗」——
门槛（ext_max / decline_min / cross_min_per_year …）到底是松是紧，用收益分布说话，而不是拍脑袋。

口径：
- **快照**：从库里最新一根往回每 step_bars（默认 60）个交易日取一个基准日，共 snapshots（默认 6）个。
  每个基准日调 `boxrange.compute(base=...)`——所有窗口按 date <= base 截断，**不看未来**；
  门槛与选股器共用 `selectors.box_range.gates_ok`（同一份七条），但不含流动性/ST/当日新K线
  （那些依赖当日 universe 快照，历史时点没有）；ST 用当前股票名近似。
- **交易**：命中后从基准日次日起扫 hold_bars（默认 250）根收盘——
  收盘进入密集带下沿 entry_zone（默认 10%）以内且未破止损线 → 买入；
  持有后收盘到带上沿 exit_zone（默认 10%）以内 → 卖出（到带顶）；
  收盘跌破箱底×(1−stop_pct)（默认 5%）→ 止损；持满 hold_bars → 超时平仓；
  窗口走完还没触发 → 按最后一根收盘截断平仓（exit_reason=截断）。
  买卖都按收盘价、收双边 fee_rate；买前先破位 → 本次不入场（exit_reason=破位未入场）。
- **对照**：同一只同窗口的买入持有（自身β）与所属宽基指数同窗口涨幅（大盘β），
  超额＝策略收益 − 对照。每次快照每只最多一笔（不回补），统计口径干净。
- **敏感性网格**：grid 里每组门槛都从同一份箱体指标过滤（贵的 SQL 只跑一遍），
  所以多组对比几乎不增加耗时。

耗时：每个快照一次全市场箱体计算（空载约 1 分钟、负载高时 3~5 分钟），默认 6 个快照约 10~25 分钟；
跑的时候那条共用 SQLite 连接会被占住，其它页面会排队。结果存 kv，只留最近一次。
"""
from __future__ import annotations

import bisect
import datetime as dt
import threading
import traceback

from . import boxrange, db

STATUS_KEY = "box_replay_status"
RESULT_KEY = "box_replay_result"
MAX_TRADE_ROWS = 300   # 明细最多留这么多行（按收益排序），别让 kv 长胖
MAX_COMBOS = 8         # 主门槛 + 敏感性组合的上限：每个组合都乘全市场门槛评估和 kv 体积

_lock = threading.Lock()
_thread: threading.Thread | None = None


def _idle() -> dict:
    return {"status": "idle", "progress": 0, "total": 0, "message": "",
            "job_id": "", "started_at": "", "elapsed_sec": None}


def _save(st: dict) -> None:
    db.kv_set(STATUS_KEY, st)


def _progress(**kw) -> None:
    with _lock:
        st = {**_idle(), **(db.kv_get(STATUS_KEY) or {}), **kw}
        _save(st)


def status() -> dict:
    st = {**_idle(), **(db.kv_get(STATUS_KEY) or {})}
    with _lock:
        alive = _thread is not None and _thread.is_alive()
    if st["status"] == "running" and not alive:
        st.update({"status": "error", "message": "任务已中断（服务重启）"})
        _save(st)
    return st


def result() -> dict:
    return db.kv_get(RESULT_KEY) or {}


def is_busy() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def start(gates: dict, grid: list[dict], snapshots: int, step_bars: int,
          hold_bars: int, entry_zone: float, exit_zone: float,
          stop_pct: float, fee_rate: float) -> tuple[str, dict]:
    """启动回放校准；参数先在这里校验/补全，起线程前把 running 状态落好（同 bt_batch 的理由）。"""
    from .selectors.base import resolve_params
    from .selectors.box_range import BoxRangeSelector

    if not (1 <= snapshots <= 12):
        return "快照数只能在 1~12 之间", {}
    if not (20 <= step_bars <= 500):
        return "快照间隔只能在 20~500 个交易日之间", {}
    if not (20 <= hold_bars <= 750):
        return "持有窗口只能在 20~750 个交易日之间", {}
    if not (0 < entry_zone < 0.5 and 0 < exit_zone < 0.5):
        return "入场/离场带内宽度只能在 0~50% 之间", {}
    if not (0 < stop_pct <= 0.3):
        return "止损幅度只能在 0~30% 之间", {}
    if not (0 <= fee_rate <= 0.05) or fee_rate != fee_rate:
        return "费率只能在 0~5% 之间", {}
    combos = [gates] + [g for g in (grid or [])]
    if len(combos) > MAX_COMBOS:
        return f"门槛组合太多（含主门槛最多 {MAX_COMBOS} 组），回放是要全市场重算的", {}
    try:
        combos = [resolve_params(BoxRangeSelector.params_schema, c) for c in combos]
    except ValueError as e:
        return f"门槛参数不合法：{e}", {}
    from .datasource import updater
    if updater.is_busy():
        return "数据更新正在跑，等它结束再回放（两者都要扫全市场）", {}
    job_id = f"box_replay_{dt.datetime.now():%Y%m%d%H%M%S}"
    with _lock:
        global _thread
        if _thread is not None and _thread.is_alive():
            return "回放校准正在跑，等这一次跑完再启动", {}
        st = {**_idle(), "status": "running", "job_id": job_id,
              "started_at": dt.datetime.now().isoformat(timespec="seconds"),
              "total": snapshots, "message": "定位历史快照日…"}
        _save(st)
        # 旧结果作废：新一轮运行中/失败时，页面绝不能把上一轮门槛的成绩单当成当前的
        db.kv_set(RESULT_KEY, {})
        _thread = threading.Thread(
            target=_run, args=(job_id, combos, snapshots, step_bars, hold_bars,
                               entry_zone, exit_zone, stop_pct, fee_rate),
            daemon=True)
        _thread.start()
    return "started", st


def _snapshot_bases(snapshots: int, step_bars: int) -> list[str]:
    # 快照锚点必须与 compute(base=…) 的窗口截断同一套日历语义，直接共用 boxrange 的助手
    latest = boxrange.latest_bar_date()
    out: list[str] = []
    for i in range(1, snapshots + 1):
        d = boxrange._nth_last_date(step_bars * i, latest)
        if d and (not out or d < out[-1]):
            out.append(d)
    return out


def _forward_calendar(base: str, hold_bars: int) -> list[str]:
    rows = db.query(
        "SELECT DISTINCT date FROM kline_daily WHERE date > ? ORDER BY date LIMIT ?",
        (base, hold_bars))
    return [r["date"] for r in rows]


def _forward_closes(codes: list[str], base: str, end: str) -> dict[str, list[tuple[str, float]]]:
    """命中股票在 (base, end] 的复权收盘，按 400 只一块取，避免长时间占着共用连接。"""
    out: dict[str, list[tuple[str, float]]] = {}
    for i in range(0, len(codes), 400):
        chunk = codes[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for code, date, c in db.query_rows(
                f"SELECT code, date, close * adj_factor FROM kline_daily "
                f"WHERE code IN ({ph}) AND date > ? AND date <= ? AND close > 0 AND adj_factor > 0 "
                f"ORDER BY code, date", (*chunk, base, end)):
            out.setdefault(code, []).append((date, c))
    return out


def _index_series(base: str, end: str) -> dict[str, tuple[list[str], list[float]]]:
    from .datasource.eastmoney import INDEXES

    out: dict[str, tuple[list[str], list[float]]] = {}
    for key in INDEXES:
        rows = db.query(
            "SELECT date, close FROM index_kline WHERE index_code=? AND date > ? AND date <= ? "
            "AND close IS NOT NULL ORDER BY date", (key, base, end))
        if rows:
            out[key] = ([r["date"] for r in rows], [r["close"] for r in rows])
    return out


def _index_of(code: str, secid2key: dict[str, str]) -> str:
    from .datasource.eastmoney import index_secid_for

    return secid2key.get(index_secid_for(code), "")


def _index_pct(series: dict[str, tuple[list[str], list[float]]], key: str,
               d1: str, d2: str) -> float | None:
    got = series.get(key)
    if not got:
        return None
    dates, closes = got
    i = bisect.bisect_right(dates, d1) - 1
    j = bisect.bisect_right(dates, d2) - 1
    if i < 0 or j < 0 or i == j or not closes[i]:
        return None
    return (closes[j] / closes[i] - 1) * 100


def _simulate(bars: list[tuple[str, float]], lo: float, hi: float,
              entry_zone: float, exit_zone: float, stop_pct: float,
              fee_rate: float, hold_bars: int) -> dict | None:
    """一笔箱体交易的前向回放。返回 None＝窗口内没等到入场（破位或没跌进低吸区，由调用方区分）。"""
    rng = hi - lo
    entry_level = lo + entry_zone * rng
    exit_level = hi - exit_zone * rng
    stop_level = lo * (1.0 - stop_pct)
    entry_i = None
    for i, (_, c) in enumerate(bars):
        if entry_i is None:
            if c < stop_level:
                return None          # 买前就破位：本次不入场
            if c <= entry_level:
                entry_i = i
            continue
        if c >= exit_level or c < stop_level or i - entry_i >= hold_bars - 1:
            reason = ("到带顶" if c >= exit_level else
                      "止损" if c < stop_level else "超时")
            return _trade(bars, entry_i, i, fee_rate, reason)
    if entry_i is not None:  # 窗口走完还持着：按最后一根收盘截断
        return _trade(bars, entry_i, len(bars) - 1, fee_rate, "截断")
    return None


def _trade(bars: list[tuple[str, float]], i: int, j: int, fee_rate: float,
           reason: str) -> dict:
    d1, p1 = bars[i]
    d2, p2 = bars[j]
    ret = (p2 * (1 - fee_rate)) / (p1 * (1 + fee_rate)) - 1
    return {"entry_date": d1, "exit_date": d2, "days": j - i + 1,
            "ret_pct": round(ret * 100, 2),
            "bh_pct": round((p2 / p1 - 1) * 100, 2), "exit_reason": reason}


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    i = min(int(q * (len(sorted_vals) - 1)), len(sorted_vals) - 1)
    return round(sorted_vals[i], 2)


def _agg(trades: list[dict]) -> dict:
    rets = sorted(t["ret_pct"] for t in trades)
    ex_own = sorted(t["ret_pct"] - t["bh_pct"] for t in trades)
    ex_idx = sorted(t["excess_idx_pct"] for t in trades if t["excess_idx_pct"] is not None)
    n = len(trades)
    return {
        "n": n,
        "win_rate": round(sum(1 for r in rets if r > 0) / n * 100, 1) if n else None,
        "ret_med": _quantile(rets, 0.5), "ret_mean": round(sum(rets) / n, 2) if n else None,
        "ret_p25": _quantile(rets, 0.25), "ret_p75": _quantile(rets, 0.75),
        "excess_own_med": _quantile(ex_own, 0.5),
        "excess_idx_med": _quantile(ex_idx, 0.5),
        "stop_rate": round(sum(1 for t in trades if t["exit_reason"] == "止损") / n * 100, 1) if n else None,
        "timeout_rate": round(sum(1 for t in trades if t["exit_reason"] in ("超时", "截断")) / n * 100, 1) if n else None,
    }


def _run(job_id: str, combos: list[dict], snapshots: int, step_bars: int,
         hold_bars: int, entry_zone: float, exit_zone: float,
         stop_pct: float, fee_rate: float) -> None:
    from .selectors.box_range import gates_ok

    t0 = dt.datetime.now()
    try:
        bases = _snapshot_bases(snapshots, step_bars)
        if not bases:
            raise ValueError("库里交易日不足，定位不到任何历史快照日")
        names = {r["code"]: (r["name"] or "") for r in db.query("SELECT code,name FROM stocks")}
        from .datasource.eastmoney import INDEXES
        from .selectors.base import name_excluded
        secid2key = {v[0]: k for k, v in INDEXES.items()}
        combo_out = [{ "gates": c, "snapshots": [], "trades": []} for c in combos]
        for n, base in enumerate(bases):
            _progress(progress=n, message=f"快照 {n + 1}/{len(bases)}（{base}）：全市场箱体计算…")
            bmap = boxrange.compute(base=base, use_cache=False)
            cal = _forward_calendar(base, hold_bars)
            if not cal:
                for c in combo_out:
                    c["snapshots"].append({"base": base, "hits": 0, "trades": 0, "no_entry": 0, "broken": 0})
                continue
            end = cal[-1]
            # 各门槛组合的命中并集一次取数（贵的查询只跑一遍）
            hits_by_combo = []
            union: set[str] = set()
            for c in combos:
                hits = sorted(code for code, b in bmap.items()
                              if gates_ok(b, c)[0]
                              and not name_excluded(names.get(code)))
                hits_by_combo.append(hits)
                union.update(hits)
            fwd = _forward_closes(sorted(union), base, end)
            idx_series = _index_series(base, end)
            for c, hits, c_out in zip(combos, hits_by_combo, combo_out):
                snap = {"base": base, "hits": len(hits), "trades": 0, "no_entry": 0, "broken": 0}
                for code in hits:
                    b = bmap[code]
                    bars = fwd.get(code) or []
                    if len(bars) < 2:
                        snap["no_entry"] += 1
                        continue
                    lo, hi = b.get("box_lo_adj"), b.get("box_hi_adj")
                    if not lo or not hi or hi <= lo:
                        continue
                    t = _simulate(bars, lo, hi, entry_zone, exit_zone, stop_pct,
                                  fee_rate, hold_bars)
                    if t is None:
                        # 区分「没等到低吸区」和「先破位」：买前是否已破止损线
                        stop_level = lo * (1.0 - stop_pct)
                        if any(c < stop_level for _, c in bars):
                            snap["broken"] += 1
                        else:
                            snap["no_entry"] += 1
                        continue
                    idx = _index_pct(idx_series, _index_of(code, secid2key),
                                     t["entry_date"], t["exit_date"])
                    t.update({"code": code, "name": names.get(code) or code,
                              "base": base,
                              "excess_idx_pct": round(t["ret_pct"] - idx, 2)
                              if idx is not None else None})
                    c_out["trades"].append(t)
                    snap["trades"] += 1
                c_out["snapshots"].append(snap)
            _progress(progress=n + 1, message=f"快照 {n + 1}/{len(bases)}（{base}）完成")
        out_combos = []
        for c_out in combo_out:
            trades = sorted(c_out["trades"], key=lambda t: -t["ret_pct"])
            out_combos.append({
                "gates": c_out["gates"],
                "agg": _agg(c_out["trades"]),
                "snapshots": c_out["snapshots"],
                "trades": trades[:MAX_TRADE_ROWS],
            })
        res = {
            "job_id": job_id,
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "elapsed_sec": round((dt.datetime.now() - t0).total_seconds(), 1),
            "params": {"snapshots": len(bases), "step_bars": step_bars,
                       "hold_bars": hold_bars, "entry_zone": entry_zone,
                       "exit_zone": exit_zone, "stop_pct": stop_pct,
                       "fee_rate": fee_rate},
            "bases": bases,
            "combos": out_combos,
        }
        db.kv_set(RESULT_KEY, res)
        _progress(status="done", progress=len(bases), total=len(bases),
                  message=f"完成：{len(bases)} 个快照 × {len(combos)} 组门槛",
                  elapsed_sec=res["elapsed_sec"])
    except Exception as e:
        traceback.print_exc()
        _progress(status="error", message=f"失败: {e}",
                  elapsed_sec=round((dt.datetime.now() - t0).total_seconds(), 1))
