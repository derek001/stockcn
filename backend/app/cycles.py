"""周期分析：三周期（短=年内 / 中=一轮牛熊 / 长=全历史多轮牛熊）+ 历史相位类比。

分析对象：大盘指数（index_kline，上证/深成指/创业板指）或个股（kline_daily 的
等比后复权价 close×adj_factor——个股必须用复权价，否则分红送转会被当成暴跌划出假熊市）。

牛熊划分：ZigZag 摆动阈值法（大盘默认 30%，个股默认 50%，页面可调 10~60）。
自上一转折点以来的运行极值被反向运动回撤 ≥ 阈值时，确认为一个大级别转折点
（大底/大顶），相邻转折点之间为一轮牛（底→顶）或熊（顶→底）。
「一轮牛熊的时间」以数据划分出的实际轮次为准，不套固定波长——康波（50~60 年）
超出 A 股历史、基钦（40 个月）与实际轮次偏差大、朱格拉（~10 年）在长周期图上
只做可开关的十年参考网格，三者都不进统计。

转折点取**收盘价**极值（不是盘中极值），与行情软件记忆中的 6124/1664 等盘中
点位略有出入，属口径差异不是 bug。末端运行极值是「未确认候选」：距确认还差
（阈值 − 当前回撤），页面用空心标记展示。

历史相位类比（「通过历史经验预测」的诚实实现）：定位当前相位（本轮起点至今
N 个月、涨幅 X%），在历史每一轮同类轮次中找到同样相位的时间点，统计其后
3/6/12 个月的涨跌分布（中位数 / 四分位 / 上涨概率）。早于该相位就结束的历史
轮次单独列出（审查删失，不悄悄丢弃）。统计参考，不是预测。
"""
from __future__ import annotations

import bisect
from datetime import date, timedelta

from . import db

# 周期分析开放的大盘指数（科创50/北证50 历史太短、划不出轮次，不开放）
INDEXES = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
}

SHORT_SWING = 10.0        # 短周期波段划分阈值（%）
SHORT_WINDOW = 250        # 短周期窗口（交易日 ≈ 12 个月）
MONTH_DAYS = 30.4375      # 月 → 天换算（365.25/12）
FWD_MONTHS = (3, 6, 12)   # 相位类比的前向统计窗口（月）
PHASE_TOL_DAYS = 60       # 相位对齐容差（±天）：历史轮次早于当前相位不足 2 个月即算对齐
ADJUST_STATE_PCT = 15.0   # 中周期「上行/调整（或下行/反弹）」的分界回撤（%）

_STATE_LABEL = {"up": "上升趋势", "down": "下降趋势", "range": "横盘震荡"}


# ---------- 基础件 ----------

def _sma(xs: list[float], w: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= w:
            s -= xs[i - w]
        if i >= w - 1:
            out[i] = s / w
    return out


def _months_between(d1: str, d2: str) -> float:
    a = date.fromisoformat(d1)
    b = date.fromisoformat(d2)
    return (b - a).days / MONTH_DAYS


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    v = sorted(xs)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def _idx_on_or_before(dates: list[str], iso: str) -> int | None:
    """目标日当天或之前最近一根的下标（dates 升序、ISO 字符串可二分）。"""
    i = bisect.bisect_right(dates, iso) - 1
    return i if i >= 0 else None


def zigzag(dates: list[str], closes: list[float], pct: float):
    """ZigZag 摆动划分 → (已确认转折点, 未确认候选)。

    确认规则：自上一转折点以来的运行极值，被反向运动回撤 ≥ pct% 时确认为新转折点，
    类型与上一个交替（底→顶→底…）。首个转折点由「先出现的一方被反向走出阈值」决定。
    运行极值在确认时刻必然是段内真实极值（更极端的值会先触发确认），无需回溯修正。
    """
    n = len(closes)
    if n < 3:
        return [], None
    thr = pct / 100.0
    pivots: list[dict] = []
    direction = 0  # 0 未定 / 1 找顶 / -1 找底
    hi_i = lo_i = 0

    def mk(i: int, typ: str) -> dict:
        return {"idx": i, "date": dates[i], "price": closes[i], "type": typ}

    for i in range(1, n):
        c = closes[i]
        if c >= closes[hi_i]:
            hi_i = i
        if c <= closes[lo_i]:
            lo_i = i
        if direction == 0:
            if c >= closes[lo_i] * (1 + thr):
                pivots.append(mk(lo_i, "bottom"))
                direction, hi_i = 1, i
            elif c <= closes[hi_i] * (1 - thr):
                pivots.append(mk(hi_i, "top"))
                direction, lo_i = -1, i
        elif direction == 1:
            if c <= closes[hi_i] * (1 - thr):
                pivots.append(mk(hi_i, "top"))
                direction, lo_i = -1, i
        else:
            if c >= closes[lo_i] * (1 + thr):
                pivots.append(mk(lo_i, "bottom"))
                direction, hi_i = 1, i

    provisional = None
    last_close = closes[-1]
    if pivots:
        if direction == 1 and hi_i > pivots[-1]["idx"]:
            provisional = {"date": dates[hi_i], "price": closes[hi_i], "type": "top",
                           "retrace_pct": round((1 - last_close / closes[hi_i]) * 100, 2)}
        elif direction == -1 and lo_i > pivots[-1]["idx"]:
            provisional = {"date": dates[lo_i], "price": closes[lo_i], "type": "bottom",
                           "retrace_pct": round((last_close / closes[lo_i] - 1) * 100, 2)}
    return pivots, provisional


# ---------- 各周期面板 ----------

def _rounds(pivots: list[dict]) -> list[dict]:
    """相邻转折点连成轮次：bottom→top 为牛、top→bottom 为熊。"""
    out = []
    for a, b in zip(pivots, pivots[1:]):
        kind = "bull" if a["type"] == "bottom" else "bear"
        chg = b["price"] / a["price"] - 1
        out.append({
            "kind": kind,
            "kind_label": "牛市" if kind == "bull" else "熊市",
            "label": f"{a['date'][:7]}→{b['date'][:7]}",
            "from_date": a["date"], "from_price": round(a["price"], 2),
            "from_idx": a["idx"],
            "to_date": b["date"], "to_price": round(b["price"], 2),
            "to_idx": b["idx"],
            "chg_pct": round(chg * 100, 1),
            "months": round(_months_between(a["date"], b["date"]), 1),
        })
    return out


def _current_segment(pivots, provisional, dates, closes, swing):
    """中周期＝本轮（最后一个确认转折点 → 现在，未走完）。"""
    if not pivots:
        return None
    p = pivots[-1]
    kind = "bull" if p["type"] == "bottom" else "bear"
    seg = closes[p["idx"]:]
    hi_k = max(range(len(seg)), key=lambda k: seg[k])
    lo_k = min(range(len(seg)), key=lambda k: seg[k])
    hi_i, lo_i = p["idx"] + hi_k, p["idx"] + lo_k
    last = closes[-1]
    out = {
        "kind": kind,
        "kind_label": "牛市" if kind == "bull" else "熊市",
        "from_date": p["date"], "from_price": round(p["price"], 2),
        "start_idx": p["idx"],
        "months": round(_months_between(p["date"], dates[-1]), 1),
        "chg_pct": round((last / p["price"] - 1) * 100, 1),
        "seg_hi": round(closes[hi_i], 2), "seg_hi_date": dates[hi_i],
        "seg_lo": round(closes[lo_i], 2), "seg_lo_date": dates[lo_i],
    }
    if kind == "bull":
        dd = (1 - last / closes[hi_i]) * 100
        out["drawdown_pct"] = round(dd, 1)
        out["state"] = "牛市上行" if dd < ADJUST_STATE_PCT else "牛市调整"
    else:
        rb = (last / closes[lo_i] - 1) * 100
        out["rebound_pct"] = round(rb, 1)
        out["state"] = "熊市下行" if rb < ADJUST_STATE_PCT else "熊市反弹"
    if provisional:
        out["confirm_gap_pct"] = round(swing - provisional["retrace_pct"], 1)
    return out


def _short_panel(dates: list[str], closes: list[float]) -> dict:
    """短周期＝近 12 个月：MA20/60 投票定状态，10% 摆动划年内波段。"""
    n = len(closes)
    w = min(SHORT_WINDOW, n)
    i0 = n - w
    seg = closes[i0:]
    last = closes[-1]
    hi_k = max(range(len(seg)), key=lambda k: seg[k])
    lo_k = min(range(len(seg)), key=lambda k: seg[k])
    hi, lo = seg[hi_k], seg[lo_k]
    pos = 50.0 if hi <= lo else (last - lo) / (hi - lo) * 100

    ma20_all = _sma(closes, 20)
    ma60_all = _sma(closes, 60)
    m20, m60 = ma20_all[-1], ma60_all[-1]

    piv, prov = zigzag(dates[i0:], seg, SHORT_SWING)
    pivots = [{"date": p["date"], "price": round(p["price"], 2), "type": p["type"]} for p in piv]

    evidence: list[str] = []
    if m20 is not None and m60 is not None:
        if last > m20 > m60:
            state = "up"
            evidence.append(f"收盘 {last:.2f} > MA20 {m20:.2f} > MA60 {m60:.2f}（多头排列）")
        elif last < m20 < m60:
            state = "down"
            evidence.append(f"收盘 {last:.2f} < MA20 {m20:.2f} < MA60 {m60:.2f}（空头排列）")
        else:
            state = "range"
            evidence.append(f"MA20 {m20:.2f} / MA60 {m60:.2f} 交叉缠绕，无多空排列")
    elif piv:
        state = "up" if piv[-1]["type"] == "bottom" else "down"
        evidence.append("历史不足 60 根，MA60 不可用，按年内波段方向判定")
    else:
        state = "range"
        evidence.append("历史不足 60 根且无波段划分，默认震荡")
    evidence.append(
        f"近一年区间 {lo:.2f}({dates[i0 + lo_k]}) ~ {hi:.2f}({dates[i0 + hi_k]})，"
        f"现价位于 {pos:.0f}% 分位")
    if piv:
        p = piv[-1]
        evidence.append(f"年内最后确认转折：{p['date']} "
                        f"{'低点' if p['type'] == 'bottom' else '高点'} {p['price']:.2f}（{SHORT_SWING:.0f}% 摆动）")
    return {
        "window": w, "partial": w < SHORT_WINDOW,
        "swing": SHORT_SWING,
        "state": state, "state_label": _STATE_LABEL[state],
        "evidence": evidence,
        "hi": round(hi, 2), "hi_date": dates[i0 + hi_k],
        "lo": round(lo, 2), "lo_date": dates[i0 + lo_k],
        "pos_pct": round(pos, 1),
        "ma20": round(m20, 2) if m20 is not None else None,
        "ma60": round(m60, 2) if m60 is not None else None,
        "pivots": pivots,
        "provisional": prov,
    }


def _seasonality(dates: list[str], closes: list[float]) -> list[dict]:
    """年内季节性：每个自然月的月度涨跌幅统计（收盘口径，月末对月末）。"""
    last_by_ym: dict[str, float] = {}
    for d, c in zip(dates, closes):
        last_by_ym[d[:7]] = c  # dates 升序，后写覆盖 → 每月最后一根
    yms = sorted(last_by_ym)
    by_month: dict[int, list[float]] = {}
    prev = None
    for ym in yms:
        c = last_by_ym[ym]
        if prev is not None:
            by_month.setdefault(int(ym[5:7]), []).append((c / prev - 1) * 100)
        prev = c
    out = []
    for m in range(1, 13):
        v = by_month.get(m, [])
        n = len(v)
        out.append({
            "month": m, "n": n,
            "mean_pct": round(sum(v) / n, 2) if v else None,
            "median_pct": round(_median(v), 2) if v else None,
            "win_rate": round(100 * sum(1 for x in v if x > 0) / n, 1) if v else None,
        })
    return out


def _analogy(rounds: list[dict], current: dict | None,
             dates: list[str], closes: list[float]) -> dict | None:
    """历史相位类比：在历史同类轮次的同一相位处统计后续 3/6/12 个月涨跌。"""
    if not current or not rounds:
        return None
    phase_days = round(current["months"] * MONTH_DAYS)
    same = [r for r in rounds if r["kind"] == current["kind"]]

    cases: list[dict] = []
    ended_before: list[dict] = []
    for r in same:
        r_days = (date.fromisoformat(r["to_date"]) - date.fromisoformat(r["from_date"])).days
        target = date.fromisoformat(r["from_date"]) + timedelta(days=phase_days)
        if r_days < phase_days - PHASE_TOL_DAYS:
            ended_before.append({"label": r["label"], "months": r["months"],
                                 "chg_pct": r["chg_pct"]})
            continue
        # 相位点限定在该轮内，取离目标日最近的交易日
        j = min(range(r["from_idx"], r["to_idx"] + 1),
                key=lambda k: abs((date.fromisoformat(dates[k]) - target).days))
        base = closes[j]
        case = {
            "label": r["label"],
            "at_date": dates[j],
            "at_price": round(base, 2),
            "phase_chg_pct": round((base / closes[r["from_idx"]] - 1) * 100, 1),
            "round_chg_pct": r["chg_pct"],
        }
        for k in FWD_MONTHS:
            tgt = (date.fromisoformat(dates[j]) + timedelta(days=k * MONTH_DAYS)).isoformat()
            ii = _idx_on_or_before(dates, tgt)
            case[f"fwd{k}"] = (round((closes[ii] / base - 1) * 100, 1)
                               if ii is not None and ii > j else None)
        cases.append(case)

    stats = {}
    for k in FWD_MONTHS:
        vals = [c[f"fwd{k}"] for c in cases if c[f"fwd{k}"] is not None]
        if len(vals) >= 3:
            v = sorted(vals)
            stats[f"fwd{k}"] = {
                "n": len(vals),
                "win_rate": round(100 * sum(1 for x in vals if x > 0) / len(vals), 1),
                "p25": round(v[int(0.25 * (len(v) - 1))], 1),
                "p50": round(_median(vals), 1),
                "p75": round(v[int(0.75 * (len(v) - 1))], 1),
            }
    if not stats:
        if cases:
            note = "同类轮次进入当前相位的不足 3 轮，样本无法统计，只列明细"
        else:
            longest = max((e["months"] for e in ended_before), default=0.0)
            note = (f"历史上没有任何同类轮次走到当前相位：{len(ended_before)} 轮全部更早结束"
                    f"（最长 {longest:.1f} 个月），本轮时长已是同类之最——这本身就是重要信息")
    else:
        note = None
    return {
        "phase": {
            "kind": current["kind"], "kind_label": current["kind_label"],
            "months": current["months"], "chg_pct": current["chg_pct"],
            "from_date": current["from_date"],
        },
        "cases": cases,
        "ended_before": ended_before,
        "stats": stats,
        "note": note,
    }


def _long_panel(dates: list[str], closes: list[float], pivots: list[dict],
                rounds: list[dict], current: dict | None) -> dict:
    """长周期＝全历史：现价位置感 + 轮次分布统计。"""
    last = closes[-1]
    ranked = sorted(closes)
    pctile = round(100 * bisect.bisect_left(ranked, last) / len(ranked), 1)

    last_top = next((p for p in reversed(pivots) if p["type"] == "top"), None)
    last_bottom = next((p for p in reversed(pivots) if p["type"] == "bottom"), None)
    dist_top = round((last / last_top["price"] - 1) * 100, 1) if last_top else None
    dist_bottom = round((last / last_bottom["price"] - 1) * 100, 1) if last_bottom else None

    bull = [r["chg_pct"] for r in rounds if r["kind"] == "bull"]
    bear = [r["chg_pct"] for r in rounds if r["kind"] == "bear"]
    bull_m = [r["months"] for r in rounds if r["kind"] == "bull"]
    bear_m = [r["months"] for r in rounds if r["kind"] == "bear"]

    y0, y1 = int(dates[0][:4]), int(dates[-1][:4])
    decades = [y for y in range(y0 // 10 * 10, y1 + 10, 10) if y0 <= y <= y1]
    return {
        "pctile": pctile,
        "last_top": {"date": last_top["date"], "price": round(last_top["price"], 2)} if last_top else None,
        "last_bottom": {"date": last_bottom["date"], "price": round(last_bottom["price"], 2)} if last_bottom else None,
        "dist_last_top_pct": dist_top,
        "dist_last_bottom_pct": dist_bottom,
        "rounds_count": len(rounds),
        "bull_count": len(bull), "bear_count": len(bear),
        "bull_med_chg_pct": round(_median(bull), 1) if bull else None,
        "bear_med_chg_pct": round(_median(bear), 1) if bear else None,
        "bull_med_months": round(_median(bull_m), 1) if bull_m else None,
        "bear_med_months": round(_median(bear_m), 1) if bear_m else None,
        "decades": decades,
        "current_kind": current["kind"] if current else None,
    }


# ---------- 装配 ----------

def _load_index(index_code: str):
    name = INDEXES.get(index_code)
    if not name:
        raise ValueError(f"未知指数 {index_code}，周期分析支持：{' / '.join(f'{k} {v}' for k, v in INDEXES.items())}")
    rows = db.query(
        "SELECT date, close FROM index_kline WHERE index_code=? ORDER BY date", (index_code,))
    if not rows:
        raise ValueError(f"{name} 没有指数K线，请先到「数据中心 → 在线数据」跑一次「日常刷新」")
    return name, [r["date"] for r in rows], [float(r["close"]) for r in rows], "指数点位", 0


def _load_stock(code: str):
    code = code.strip()
    if not code:
        raise ValueError("个股代码不能为空")
    st = db.query_one("SELECT name FROM stocks WHERE code=?", (code,))
    total = db.query_one("SELECT COUNT(*) AS n FROM kline_daily WHERE code=?", (code,))
    rows = db.query(
        "SELECT date, close*adj_factor AS close FROM kline_daily "
        "WHERE code=? AND adj_factor IS NOT NULL ORDER BY date", (code,))
    if not rows:
        if total and total["n"]:
            raise ValueError(f"{code} 的日线全部缺复权因子，请重导该股后再做一次增量导入")
        raise ValueError(f"{code} 没有日线数据（查无代码或未导入），请先在「数据中心 → 本地数据」导入")
    skipped = (total["n"] if total else 0) - len(rows)
    return (st["name"] if st else code), [r["date"] for r in rows], \
        [float(r["close"]) for r in rows], "后复权价（含分红送转）", skipped


def analyze(index_code: str = "sh000001", code: str = "", swing: float = 30.0) -> dict:
    swing = min(60.0, max(10.0, float(swing)))
    if code:
        name, dates, closes, price_note, skipped = _load_stock(code)
        subject, idx_code = "stock", None
    else:
        name, dates, closes, price_note, skipped = _load_index(index_code)
        subject, idx_code = "index", index_code

    pivots, provisional = zigzag(dates, closes, swing)
    rounds = _rounds(pivots)
    current = _current_segment(pivots, provisional, dates, closes, swing)

    return {
        "meta": {
            "subject": subject, "index_code": idx_code, "code": code or None,
            "name": name, "price_note": price_note,
            "swing": swing, "short_swing": SHORT_SWING,
            "bars": len(dates), "first_date": dates[0], "last_date": dates[-1],
            "skipped_factor_rows": skipped,
            "rounds_note": f"转折点为收盘价极值（{swing:.0f}% 摆动确认）",
        },
        "dates": dates,
        "closes": [round(c, 4) for c in closes],
        "pivots": [{"date": p["date"], "price": round(p["price"], 2), "type": p["type"]}
                   for p in pivots],
        "provisional": provisional,
        "rounds": rounds,
        "current": current,
        "short": _short_panel(dates, closes),
        "seasonality": _seasonality(dates, closes),
        "analogy": _analogy(rounds, current, dates, closes),
        "long": _long_panel(dates, closes, pivots, rounds, current),
    }
