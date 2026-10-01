"""QMT (迅投) 本地导出的日线 CSV/TXT 读取。

QMT「导出数据」产出的目录形如 `<导出根目录>/SH/price_600000.txt`，表头为
`timetag,open,high,low,close,volumn,amount`（`volumn` 是 QMT 自身的拼写）。
其中 volume 单位为**手**、amount 单位为**元**，复权口径由导出时的选项决定，
本项目的日线库要求**后复权**。换手率导不出来，由流通股本估算。
"""
from __future__ import annotations

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
        # QMT 后复权是仿射变换（adj = A·raw + B），复权价环比会系统性压缩交易所口径的
        # 涨跌幅，所以这里不伪造 pct_chg；最新一根在导入后按在线快照回填。
        bars.append({"date": d, "open": o, "high": h, "low": l, "close": c,
                     "volume": v, "amount": a, "pct_chg": None})
    return bars


def _count_markets(codes: list[str], universe: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in codes:
        mk = universe.get(c) or "?"
        out[mk] = out.get(mk, 0) + 1
    return dict(sorted(out.items()))


def check_dir(root: str, universe: dict[str, str],
              snapshot: dict[str, dict] | None = None) -> dict:
    """导入前的目录体检：覆盖情况 + 复权口径 + 是否收盘结算后导出（只读，不写库）。

    universe: code -> 市场（SH/SZ/BJ），即待导入的股票白名单。
    snapshot: code -> {"price", "amount", "trade_date"}，取最新在线快照，用于两项抽检：
      末根收盘价≈快照价 => 疑似不复权；末根成交额明显低于快照成交额 => 疑似盘中/未结算导出。
    """
    all_files = scan_files(root, universe)
    missing = sorted(set(universe) - set(all_files))
    res = {
        "path": os.path.abspath(root),
        "files": len(_price_files(Path(root))),
        "matched": len(all_files),
        "missing": len(missing),
        # 完整清单：导入按 stocks 白名单过滤文件，缺谁只能回 QMT 补导出，必须能逐个抄出来
        "missing_codes": missing,
        "missing_by_market": _count_markets(missing, universe),
        "latest_date": "",
        "warning": "",
    }
    snapshot = snapshot or {}
    latest = ""
    price_seen = price_aligned = amt_seen = 0
    amt_ratios: list[float] = []
    for code in list(all_files)[:400]:
        try:
            bars = read_bars(all_files[code])
        except ValueError:
            bars = []
        if not bars:
            continue
        last = bars[-1]
        latest = max(latest, last["date"])
        snap = snapshot.get(code) or {}
        if snap.get("price"):
            price_seen += 1
            if abs(last["close"] / snap["price"] - 1.0) < 0.03:
                price_aligned += 1
        # 快照交易日必须与末根同日，成交额比值才可比
        if snap.get("amount") and snap.get("trade_date") == last["date"] and last["amount"]:
            amt_ratios.append(float(last["amount"]) / float(snap["amount"]))
            amt_seen += 1
        if price_seen >= 30 and amt_seen >= 30:
            break
    res["latest_date"] = latest
    if price_seen and price_aligned / price_seen > 0.7:
        res["warning"] = (
            f"抽检 {price_seen} 只：{price_aligned} 只末根收盘价与最新快照价基本一致，"
            "疑似导出的是「不复权」数据。请在 QMT 导出时选择「后复权」，否则历史涨跌与分红送配不符。")
    if amt_seen >= 10:
        amt_ratios.sort()
        median = amt_ratios[len(amt_ratios) // 2]
        if median < 0.9:
            low = sum(1 for r in amt_ratios if r < 0.9)
            msg = (
                f"抽检 {amt_seen} 只：末根成交额只有收盘快照的 {median:.0%}（{low} 只偏低），"
                "疑似盘中或当日数据尚未结算时导出。这样的末根只有半天量额，"
                "请改在收盘结算后（建议 18:00 之后）重新导出再导入。")
            res["warning"] = f"{res['warning']}；{msg}" if res["warning"] else msg
    return res
