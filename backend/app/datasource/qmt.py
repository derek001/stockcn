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
57 只早年算出 ≤0 的价格。等比口径实测全市场只有 1 只出现负价（920427），那是它的累计复权
因子整只带了负号——四价同号、比值完好，按绝对值逐分可复现，所以入库因子取正值
（见 `_day_interval`），而不是把它当成导出缺陷。
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
    """当日四价共同允许的因子区间；等比价只精确到分，所以是一段闭区间。

    等比那份可以整行带负号：QMT 的累计复权因子被一次「除权乘数算成负数」的事件（派息 ≥
    前收盘一类）乘翻了符号，一旦变号就一路带到尾，于是这只股票从头到尾全是负价。
    负号只是作用在四价上的同一个公共符号，比值不受影响，所以这里按绝对值解出正的 |因子|，
    入库的 `adj_factor` 恒为正，读侧 `价格 × adj_factor` 才拿得到正的复权价。
    行内正负号混杂说明四价不是同一个因子乘出来的（两份不同源），整行不参与解因子。
    """
    lo, hi = 0.0, math.inf
    neg = pos = 0
    for k in _PRICE_KEYS:
        r, g = raw.get(k), geo.get(k)
        if not r or r <= 0 or not g:
            continue
        a = -g if g < 0 else g
        if g < 0:
            neg += 1
        else:
            pos += 1
        lo = max(lo, (a - _CENT) / r)
        hi = min(hi, (a + _CENT) / r)
    if hi == math.inf or neg and pos or lo > hi:
        return None
    return lo, hi


def _geo_sign(geo: dict) -> tuple[int, int]:
    """这一行等比价的负价个数、正价个数（0 与缺列都不算）。"""
    neg = pos = 0
    for k in _PRICE_KEYS:
        g = geo.get(k)
        if not g:
            continue
        if g < 0:
            neg += 1
        else:
            pos += 1
    return neg, pos


def read_pair(raw_path: Path, geo_path: Path) -> tuple[list[dict], dict]:
    """同一只股票的「不复权 + 等比后复权」两份导出 -> 入库用的日线行。

    因子取分段常数：只要现有因子还落在当日四价的可行区间内就沿用（于是非除权日的
    复权价环比恰好等于交易所涨跌幅），容不下了才认定发生了除权除息、换成新区间的中点。
    因子非空的行都落在当日区间内，所以 `round(价格 × 因子, 2)` 能逐分复现等比导出
    （等比那份整行带负号时复现的是它的绝对值，入库因子恒为正，见 `_day_interval`）。

    等比那份坏掉（行内正负号混杂、与不复权明显不同源）导致解不出因子时**不丢行**：
    不复权的 OHLC/量/额照记，`adj_factor` 与 `pct_chg` 留 NULL，表示「这根的复权口径无从推算」，
    等 QMT 重导后再做一次增量导入即可自动补上。
    """
    R, G = _by_date(raw_path), _by_date(geo_path)
    stats = {"rows": 0, "no_geo": 0, "no_raw": 0, "jumps": 0, "events": 0,
             "unresolved": 0, "no_factor": 0, "neg_geo": 0, "mixed_geo": 0}
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
            neg, pos = _geo_sign(geo)
            if neg:
                if pos:
                    stats["mixed_geo"] += 1
                else:
                    stats["neg_geo"] += 1
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
        stats["rows"] += 1
        if factor is None:
            stats["no_factor"] += 1
        pct = None
        # 前后两根都要有因子才算得出涨跌幅：跨越「解不出因子」的那一段时分不清是除权还是真跌
        if factor is not None and prev_factor is not None and prev_close and raw["close"]:
            if abs(factor / prev_factor - 1.0) > 1e-12:
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
            能逐分复现等比价。
    解不出因子的行（等比那份行内正负号混杂）不参与因子相关的判断，只按行数统计
    （`no_factor_rows`）；整行负价是合法口径，按 |因子| 解（`neg_geo_rows`）。
    这两类连同 `mixed_geo_rows`、`factor_backtrack_stocks` 都是**信息项，不进 warning**：
    如实入库原则下它们只影响那几行的复权口径，重导一次再点增量就自愈，不该拦整次导入。
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
    f_no_factor = f_no_factor_stocks = 0
    f_neg_geo = f_mixed_geo = 0
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
        f_no_factor += stats["no_factor"]
        f_neg_geo += stats["neg_geo"]
        f_mixed_geo += stats["mixed_geo"]
        if stats["no_factor"]:
            f_no_factor_stocks += 1
        # 首根因子 >1 合法：老股（如 920000 新三板转北交所）的因子基准早于导出窗口，实测 1.09。
        # 远小于 1 才是「前复权」特征——前复权把基准锚在最后一根，早期因子必然 <1。
        # 首根本就解不出因子（等比那份坏掉）时无从判断口径，跳过这只。
        if bars[0]["adj_factor"] is not None and bars[0]["adj_factor"] < 0.9:
            f_first_bad += 1
        prev = None
        for b in bars:
            fac = b["adj_factor"]
            if fac is None:
                continue
            # 累计因子在「除权参考价高于前收盘」的除权会合法下调（配股、重组复牌），
            # 0.5% 容差是实测舍入带宽的上限（普通噪声最大 0.21%），只用来数几只股票整段往下走
            if prev is not None and fac < prev * 0.995:
                f_nonmono += 1
                break
            prev = fac
        gmap = _by_date(geo_files[code])
        if gmap and max(gmap) != bars[-1]["date"]:
            date_mismatch += 1
        if any(abs(gmap[b["date"]]["amount"] - b["amount"]) > 0.51 or
               abs(gmap[b["date"]]["volume"] - b["volume"]) > 0.51
               for b in bars if b["date"] in gmap):
            vol_mismatch += 1

    # 下面这些提示会原样显示在数据中心页面上，写给操作员看：说清「哪份导错了 + 回 QMT 怎么选」，
    # 复权因子一类的实现细节留在 README，不要出现在这里。
    if price_seen and price_aligned / price_seen < 0.7:
        warns.append(
            f"抽了 {price_seen} 只股票，只有 {price_aligned} 只的不复权最新收盘价和实时行情一致，"
            "「不复权」目录疑似导成了复权价。请在 QMT 重导，复权方式选「不复权」。")
    if geo_seen and geo_same / geo_seen > 0.7:
        warns.append(
            f"抽了 {geo_seen} 只股票，有 {geo_same} 只的两份最新收盘价完全一样，"
            "「等比后复权」目录其实导的也是不复权。请在 QMT 另导一份，复权方式选「等比后复权」。")
    if res["raw_latest"] and res["geo_latest"] and res["raw_latest"] != res["geo_latest"]:
        warns.append(
            f"两份导出不是同一天：不复权到 {res['raw_latest']}，等比后复权到 {res['geo_latest']}。"
            "请在 QMT 把这两份按同一个交易日重导。")
    if vol_mismatch:
        warns.append(
            f"{vol_mismatch} 只股票的成交量/成交额在两份里对不上，说明不是同一次导出的。"
            "请把这两份一起重导。")
    if amt_seen >= 10:
        amt_ratios.sort()
        median = amt_ratios[len(amt_ratios) // 2]
        if median < 0.9:
            warns.append(
                f"最新一天的成交额只有收盘后的 {median:.0%}，疑似盘中或当日未结算时导的（当天那根只有半天量额）。"
                "请等收盘结算完成后（建议 18:00 之后）重导这两份。")
    if f_first_bad:
        warns.append(
            f"{f_first_bad} 只股票的复权系数远小于 1，这是「前复权 / 等比前复权」的特征。"
            "请在 QMT 改选「等比后复权」。")
    # 因子回退本身是合法口径（除权参考价高于前收盘：配股价高于市价、重组/股改后复牌重定价），
    # 全市场 1696 万行实测 7 只。只有**成规模**回退才说明两份的复权口径不匹配，所以按占比判，
    # 单只回退只报数不拦导入——否则抽到这 7 只里的任一只，用户就得被迫勾「强制导入」。
    if f_rows and f_nonmono >= max(8, f_rows // 5):
        warns.append(
            f"抽了 {f_rows} 只股票，有 {f_nonmono} 只的复权系数大面积回退，两份的复权口径不匹配。"
            "请把这两份一起重导，并确认选的是「不复权」+「等比后复权」。")
    # QMT 自己的四价偶发无法被单一因子整除到分（实测约占行数 0.05%），只有大面积出现才是口径问题
    if f_bars and f_unres / f_bars > 0.01:
        warns.append(
            f"{f_unres}/{f_bars} 行的等比价按不复权价算不出来，两份可能来自不同行情源或不同的除权规则版本。"
            "请在 QMT 一次性重导这两份。")
    if date_mismatch:
        warns.append(
            f"{date_mismatch} 只股票的两份最新一天日期不同（{f_no_geo} 行只在不复权目录里有），"
            "两份不完整对应，请一起重导。")
    res["unmatched_rows"] = f_no_geo
    res["sample_stocks"] = f_rows
    res["factor_events"] = f_events
    res["factor_unreproduced"] = f_unres
    # 信息项，不进 warning：某只股票的等比那份坏掉时导入仍会照写不复权数据，
    # 只是这些行的复权口径留空，回 QMT 重导那只股票再增量一次即可自动补上。
    res["no_factor_rows"] = f_no_factor
    res["no_factor_stocks"] = f_no_factor_stocks
    # 整行负价是合法口径（QMT 的累计复权因子带了负号），按 |因子| 解，这里只报行数供核对；
    # 行内正负号混杂（mixed_geo_rows）才是真解不出，那些行复权口径留 NULL，导入不拦。
    res["neg_geo_rows"] = f_neg_geo
    res["mixed_geo_rows"] = f_mixed_geo
    res["factor_backtrack_stocks"] = f_nonmono
    res["warning"] = "；".join(warns)
    return res
