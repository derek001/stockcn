"""QMT (迅投) 本地导出的日线 CSV/TXT 读取。

QMT「导出数据」产出的目录形如 `<导出根目录>/SH/price_600000.txt`，表头为
`timetag,open,high,low,close,volumn,amount`（`volumn` 是 QMT 自身的拼写）。
volume 单位为**手**、amount 单位为**元**，复权口径由导出时的选项决定。

本项目要求用户导出**两份**：「不复权」与「等比后复权」。`read_pair` 把同一只股票
的两份文件按日期对齐后产出入库用的行：价格是交易所口径（不复权），另存一列
`adj_factor`（等比累计复权因子，上市首日 = 1），复权价 = 价格 × 因子。

为什么是这两份而不是「不复权 + 后复权」：等比后复权是 `hfq = raw × 因子` 的纯乘法，
因子分段常数、只在除权除息日跳变，能把两种口径互相还原；而 QMT 的「后复权」是仿射
`A·raw + B`（B≠0），既反推不出不复权价、环比也会压缩交易所口径的涨跌幅，全市场还有
57 只早年算出 ≤0 的价格。等比口径实测只有 1 只出现非正价。
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

MARKETS = ("SH", "SZ", "BJ")
_FILE_RE = re.compile(r"^price_(\d{6})\.(txt|csv)$", re.I)
# 表头名 -> 内部字段
_ALIASES = {
    "timetag": "timetag", "date": "timetag", "time": "timetag",
    "open": "open", "high": "high", "low": "low", "close": "close",
    "volumn": "volume", "volume": "volume",
    "amount": "amount", "amt": "amount",
}
_PRICE_KEYS = ("open", "high", "low", "close")
_CENT = 0.005 + 1e-9      # 等比价按 0.01 元取整，单日单价的容许误差


def _market_dirs(root: Path) -> list[Path]:
    dirs = []
    for mk in MARKETS:
        for name in (mk, mk.lower()):
            cand = root / name
            if cand.is_dir():
                dirs.append(cand)
                break
    return dirs


def _price_files(root: Path) -> list[tuple[str, Path]]:
    """(市场目录名, 文件路径) —— 目录内没有 SH/SZ/BJ 子目录时按当前目录算。"""
    out: list[tuple[str, Path]] = []
    for d in _market_dirs(root) or [root]:
        mk = d.name.upper()
        for p in sorted(d.glob("price_*")):
            m = _FILE_RE.match(p.name)
            if m:
                out.append((mk if mk in MARKETS else "", p))
    return out


def scan_files(root: str, market_of: dict[str, str] | None = None) -> dict[str, Path]:
    """code -> 导出文件路径。

    market_of 给出 code 期望的市场（SH/SZ/BJ）时按目录过滤：000001 在 SH 目录是
    上证指数、在 SZ 目录才是平安银行，不能只按 6 位代码合并。
    """
    if not root or not os.path.isdir(root):
        raise ValueError(f"目录不存在：{root}")
    entries = _price_files(Path(root))
    if not entries:
        subs = sorted(p.name for p in Path(root).iterdir() if p.is_dir())
        hint = f"；该目录下的子目录是 {('、'.join(subs))}，请把路径填到含 SH/SZ/BJ 的那一层" if subs else ""
        raise ValueError(f"在 {root} 下未找到 price_XXXXXX.txt 导出文件{hint}")
    out: dict[str, Path] = {}
    for mk, p in entries:
        code = _FILE_RE.match(p.name).group(1)
        if market_of is not None:
            want = market_of.get(code)
            if want is None or (mk and want != mk):
                continue
        out.setdefault(code, p)
    return out


def _header_map(lines: list[str]) -> dict[str, int] | None:
    cols = [c.strip().lstrip("'\ufeff").lower() for c in lines[0].split(",")]
    idx: dict[str, int] = {}
    for i, c in enumerate(cols):
        key = _ALIASES.get(c)
        if key and key not in idx:
            idx[key] = i
    need = ("timetag", "open", "high", "low", "close", "volume", "amount")
    if any(k not in idx for k in need):
        return None
    return idx


def read_bars(path: Path) -> list[dict]:
    """解析单个导出文件，返回按日期升序的原始行（未含 pct_chg / turnover）。"""
    with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    idx = _header_map(lines)
    if idx is None:
        raise ValueError(f"{path.name} 表头无法识别（需 timetag,open,high,low,close,volumn,amount）")
    bars: list[dict] = []
    for ln in lines[1:]:
        parts = ln.split(",")
        try:
            tag = parts[idx["timetag"]].strip()
            d = f"{tag[0:4]}-{tag[4:6]}-{tag[6:8]}"
            o = float(parts[idx["open"]])
            h = float(parts[idx["high"]])
            l = float(parts[idx["low"]])
            c = float(parts[idx["close"]])
            v = float(parts[idx["volume"]]) * 100.0      # 手 -> 股
            a = float(parts[idx["amount"]])
        except (ValueError, IndexError):
            continue
        if len(tag) < 8 or not c:
            continue
        bars.append({"date": d, "open": o, "high": h, "low": l, "close": c,
                     "volume": v, "amount": a, "pct_chg": None})
    return bars


def _by_date(path: Path) -> dict[str, dict]:
    return {b["date"]: b for b in read_bars(path)}


def _day_interval(raw: dict, geo: dict) -> tuple[float, float] | None:
    """当日四价共同允许的因子区间；等比价只精确到分，所以是一段闭区间。"""
    lo, hi = 0.0, math.inf
    for k in _PRICE_KEYS:
        r, g = raw.get(k), geo.get(k)
        if not r or r <= 0 or g is None or g <= 0:
            continue
        lo = max(lo, (g - _CENT) / r)
        hi = min(hi, (g + _CENT) / r)
    if hi == math.inf or lo > hi:
        return None
    return lo, hi


def read_pair(raw_path: Path, geo_path: Path) -> tuple[list[dict], dict]:
    """同一只股票的「不复权 + 等比后复权」两份导出 -> 入库用的日线行。

    因子取分段常数：只要现有因子还落在当日四价的可行区间内就沿用（于是非除权日的
    复权价环比恰好等于交易所涨跌幅），容不下了才认定发生了除权除息、换成新区间的中点。
    每行的因子都落在当日区间内，所以 `round(价格 × 因子, 2)` 能逐分复现等比导出。
    """
    R, G = _by_date(raw_path), _by_date(geo_path)
    stats = {"rows": 0, "no_geo": 0, "no_raw": 0, "jumps": 0, "events": 0, "unresolved": 0}
    bars: list[dict] = []
    factor: float | None = None
    prev_close = prev_factor = None
    for d in sorted(set(R) | set(G)):
        raw, geo = R.get(d), G.get(d)
        if raw is None:
            stats["no_raw"] += 1
            continue
        if geo is None:
            stats["no_geo"] += 1
        else:
            iv = _day_interval(raw, geo)
            if iv is None:
                stats["unresolved"] += 1
            elif factor is None or not (iv[0] <= factor <= iv[1]):
                # 当日区间容不下现有因子 => 换段。幅度 >0.05% 才算一次真除权，
                # 其余是导出只精确到分带来的舍入带宽（实测 P50=0.005%）。
                new_f = (iv[0] + iv[1]) / 2
                if factor is not None:
                    stats["jumps"] += 1
                    if abs(new_f / factor - 1.0) > 5e-4:
                        stats["events"] += 1
                factor = new_f
        if factor is None:
            continue
        stats["rows"] += 1
        pct = None
        if prev_close and raw["close"]:
            if prev_factor and factor and abs(factor / prev_factor - 1.0) > 1e-12:
                # 除权除息日：先按 0.01 元取整出参考价，再算涨跌幅（与交易所算法一致）
                ref = round(prev_close * prev_factor / factor, 2)
                if ref > 0:
                    pct = (raw["close"] / ref - 1) * 100
            else:
                pct = (raw["close"] / prev_close - 1) * 100
        prev_close, prev_factor = raw["close"], factor
        bars.append({"date": d, "open": raw["open"], "high": raw["high"],
                     "low": raw["low"], "close": raw["close"],
                     "volume": raw["volume"], "amount": raw["amount"],
                     "adj_factor": factor,
                     "pct_chg": round(pct, 2) if pct is not None else None})
    return bars, stats


def _count_markets(codes: list[str], universe: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in codes:
        mk = universe.get(c) or "?"
        out[mk] = out.get(mk, 0) + 1
    return dict(sorted(out.items()))


def pair_files(raw_root: str, geo_root: str,
               universe: dict[str, str]) -> dict[str, tuple[Path, Path]]:
    """code -> (不复权文件, 等比后复权文件)，只保留两份都齐的股票。"""
    rf = scan_files(raw_root, universe)
    gf = scan_files(geo_root, universe)
    return {c: (rf[c], gf[c]) for c in sorted(set(rf) & set(gf))}


def _latest_date(files: dict[str, Path], sample: int = 300) -> str:
    latest = ""
    for code in list(files)[:sample]:
        try:
            bars = read_bars(files[code])
        except ValueError:
            continue
        if bars:
            latest = max(latest, bars[-1]["date"])
    return latest


def check_pair(raw_root: str, geo_root: str, universe: dict[str, str],
               snapshot: dict[str, dict] | None = None) -> dict:
    """导入前的双目录体检（只读，不写库）。

    两份导出各自核对口径，再合起来核对批次一致性与复权因子的合理性：
      不复权：末根收盘价 ≈ 在线快照现价；末根成交额 ≈ 快照成交额（否则是盘中导出）。
      等比后复权：末根必须与不复权明显不同（否则两个路径选成了同一份）。
      合并：截止日相同、量额逐行相同（不同即不是同一批次）、因子首日不小于 1、
            单调不减、能逐分复现等比价。
    """
    raw_files = scan_files(raw_root, universe)
    geo_files = scan_files(geo_root, universe)
    paired = sorted(set(raw_files) & set(geo_files))
    missing = sorted(set(universe) - set(paired))
    snapshot = snapshot or {}
    res = {
        "raw_path": os.path.abspath(raw_root),
        "geo_path": os.path.abspath(geo_root),
        "raw_files": len(raw_files), "geo_files": len(geo_files),
        "matched": len(paired),
        "missing": len(missing),
        # 导入按 stocks 白名单过滤文件，缺谁只能回 QMT 补导出，必须能逐个抄出来
        "missing_codes": missing,
        "missing_by_market": _count_markets(missing, universe),
        "raw_latest": "", "geo_latest": "", "latest_date": "",
        "warning": "",
    }
    res["raw_latest"] = _latest_date(raw_files)
    res["geo_latest"] = _latest_date(geo_files)
    res["latest_date"] = min(x for x in (res["raw_latest"], res["geo_latest"]) if x) or ""

    warns: list[str] = []
    price_seen = price_aligned = amt_seen = geo_seen = geo_same = 0
    amt_ratios: list[float] = []
    for code in paired[:400]:
        snap = snapshot.get(code) or {}
        if not snap.get("price"):
            continue
        try:
            rb = read_bars(raw_files[code])
            gb = read_bars(geo_files[code])
        except ValueError:
            continue
        if not rb or not gb:
            continue
        last_r, last_g = rb[-1], gb[-1]
        price_seen += 1
        # 快照交易日必须与末根同日，成交额比值才可比
        if abs(last_r["close"] / snap["price"] - 1.0) < 0.03:
            price_aligned += 1
        geo_seen += 1
        if abs(last_g["close"] / last_r["close"] - 1.0) < 0.005:
            geo_same += 1
        if snap.get("amount") and snap.get("trade_date") == last_r["date"] and last_r["amount"]:
            amt_seen += 1
            amt_ratios.append(last_r["amount"] / float(snap["amount"]))
        if price_seen >= 30 and amt_seen >= 30:
            break

    f_rows = f_no_geo = f_events = 0
    f_unres = f_bars = 0
    f_first_bad = f_nonmono = vol_mismatch = date_mismatch = 0
    for code in paired[:40]:
        bars, stats = read_pair(raw_files[code], geo_files[code])
        if not bars:
            continue
        f_rows += 1
        f_bars += len(bars)
        f_events += stats["events"]
        f_unres += stats["unresolved"]
        f_no_geo += stats["no_geo"]
        # 首根因子 >1 合法：老股（如 920000 新三板转北交所）的因子基准早于导出窗口，实测 1.09。
        # 远小于 1 才是「前复权」特征——前复权把基准锚在最后一根，早期因子必然 <1。
        if bars[0]["adj_factor"] < 0.9:
            f_first_bad += 1
        prev = None
        for b in bars:
            # 因子只应往上走（分红送转累积）；0.5% 容差是实测舍入带宽的上限（最大回退 0.21%）
            if prev is not None and b["adj_factor"] < prev * 0.995:
                f_nonmono += 1
                break
            prev = b["adj_factor"]
        gmap = _by_date(geo_files[code])
        if gmap and max(gmap) != bars[-1]["date"]:
            date_mismatch += 1
        if any(abs(gmap[b["date"]]["amount"] - b["amount"]) > 0.51 or
               abs(gmap[b["date"]]["volume"] - b["volume"]) > 0.51
               for b in bars if b["date"] in gmap):
            vol_mismatch += 1

    if price_seen and price_aligned / price_seen < 0.7:
        warns.append(
            f"抽检 {price_seen} 只：只有 {price_aligned} 只「不复权」末根收盘价与最新快照价吻合，"
            "该目录疑似不是不复权导出。请在 QMT 导出时选「不复权」。")
    if geo_seen and geo_same / geo_seen > 0.7:
        warns.append(
            f"抽检 {geo_seen} 只：{geo_same} 只两份导出的末根收盘价一致，"
            "「等比后复权」目录看起来导的也是不复权。请在 QMT 里另导一份选「等比后复权」。")
    if res["raw_latest"] and res["geo_latest"] and res["raw_latest"] != res["geo_latest"]:
        warns.append(
            f"两份导出截止日不一致：不复权 {res['raw_latest']} / 等比后复权 {res['geo_latest']}，"
            "请按同一交易日重新导出。")
    if vol_mismatch:
        warns.append(
            f"抽检 {f_rows} 只：{vol_mismatch} 只的成交量/成交额在两份导出里对不上，"
            "说明不是同一批次导出，请一起重新导出。")
    if amt_seen >= 10:
        amt_ratios.sort()
        median = amt_ratios[len(amt_ratios) // 2]
        if median < 0.9:
            low = sum(1 for r in amt_ratios if r < 0.9)
            warns.append(
                f"抽检 {amt_seen} 只：末根成交额只有收盘快照的 {median:.0%}（{low} 只偏低），"
                "疑似盘中或当日数据尚未结算时导出。这样的末根只有半天量额，"
                "请改在收盘结算后（建议 18:00 之后）重新导出再导入。")
    if f_first_bad:
        warns.append(
            f"抽检 {f_rows} 只：{f_first_bad} 只首日复权因子远小于 1，"
            "等比后复权以上市日为基准、因子只可能 ≥1，疑似选成了「前复权 / 等比前复权」。")
    if f_nonmono:
        warns.append(
            f"抽检 {f_rows} 只：{f_nonmono} 只的复权因子明显回退，"
            "两份导出的复权口径对不上（等比后复权的因子只会随分红送转累积上升）。")
    # QMT 自己的四价偶发无法被单一因子整除到分（实测约占行数 0.05%），只有大面积出现才是口径问题
    if f_bars and f_unres / f_bars > 0.01:
        warns.append(
            f"抽检 {f_rows} 只：{f_unres}/{f_bars} 行无法用一个分段常数因子把等比价格复现到分，"
            "两份导出可能来自不同的行情源或不同的除权规则版本。")
    if date_mismatch:
        warns.append(
            f"抽检 {f_rows} 只：{date_mismatch} 只的两份导出末根日期不一致"
            f"（共 {f_no_geo} 行只在不复权目录里有），两份导出不完整对应，请一起重新导出。")
    res["unmatched_rows"] = f_no_geo
    res["sample_stocks"] = f_rows
    res["factor_events"] = f_events
    res["factor_unreproduced"] = f_unres
    res["warning"] = "；".join(warns)
    return res
