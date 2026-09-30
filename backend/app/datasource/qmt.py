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
        raise ValueError(f"在 {root} 下未找到 price_XXXXXX.txt 导出文件（应包含 SH/SZ/BJ 子目录）")
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
    prev_close: float | None = None
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
        pct = round((c - prev_close) / prev_close * 100, 4) if prev_close else None
        prev_close = c
        bars.append({"date": d, "open": o, "high": h, "low": l, "close": c,
                     "volume": v, "amount": a, "pct_chg": pct})
    return bars


def check_dir(root: str, universe: dict[str, str], snapshot: dict[str, float] | None = None
              ) -> dict:
    """导入前的目录体检：覆盖情况 + 复权口径提示（只读，不写库）。

    universe: code -> 市场（SH/SZ/BJ），即待导入的股票白名单。
    """
    all_files = scan_files(root, universe)
    res = {
        "path": os.path.abspath(root),
        "files": len(_price_files(Path(root))),
        "matched": len(all_files),
        "missing": len(universe) - len(all_files),
        "sample_missing": sorted(set(universe) - set(all_files))[:10],
        "latest_date": "",
        "warning": "",
    }
    snapshot = snapshot or {}
    latest = ""
    if snapshot:
        picked = [c for c in list(all_files)[:400] if c in snapshot]
        seen = aligned = 0
        for code in picked:
            try:
                bars = read_bars(all_files[code])
            except ValueError:
                bars = []
            if not bars:
                continue
            latest = max(latest, bars[-1]["date"])
            ratio = bars[-1]["close"] / snapshot[code] if snapshot[code] else None
            if ratio is None:
                continue
            seen += 1
            if abs(ratio - 1.0) < 0.03:
                aligned += 1
            if seen >= 30:
                break
        res["latest_date"] = latest
        if seen and aligned / seen > 0.7:
            res["warning"] = (
                f"抽检 {seen} 只：{aligned} 只末根收盘价与最新快照价基本一致，"
                "疑似导出的是「不复权」数据。请在 QMT 导出时选择「后复权」，否则历史涨跌与分红送配不符。")
    return res
