"""Rebuild `backend/app/industry_l1.csv` (行业名 → 申万一级).

数据来源是申万 2021 行业分类的三级表（乐咕乐股镜像，走 akshare），
输出给 `app/industry.py` 用：选股和行业分析都按这张表把个股 `stocks.industry`
（东财 f100 细分名 或 腾讯 一级板块名）归并成 31 个一级行业。

    python backend/scripts/build_industry_map.py

脚本报错就不写文件（断言见 _verify）。改口径/新增板块时重跑一次即可，
运行期不联网。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

APP_DIR = Path(__file__).resolve().parents[1] / "app"
OUT = APP_DIR / "industry_l1.csv"

# 罗马数字后缀（申万/东财用它区分同名跨级行业：银行Ⅱ、特钢Ⅲ）
_SUFFIX = "ⅠⅡⅢⅣⅤ"

# 东财板块名里不属于现行申万分类的历史叫法，手工钉死
LEGACY: dict[str, str] = {
    "光伏主材": "电力设备",            # 现行叫法：光伏设备
    "端到端供应链服务": "交通运输",      # 现行叫法：原材料/中间产品及消费品供应链服务 → 物流
}


def _base(name: str) -> str:
    while name and name[-1] in _SUFFIX:
        name = name[:-1]
    return name


def fetch_levels() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    import akshare as ak

    return (ak.sw_index_first_info(), ak.sw_index_second_info(), ak.sw_index_third_info())


def build(
    t1: pd.DataFrame, t2: pd.DataFrame, t3: pd.DataFrame
) -> tuple[dict[str, str], list[str]]:
    first = set(t1["行业名称"])
    parent2 = dict(zip(t2["行业名称"], t2["上级行业"]))
    parent3 = dict(zip(t3["行业名称"], t3["上级行业"]))

    # 二级/三级名 → 一级（三级再经二级拿父级）
    exact: dict[str, str] = {}
    for n, p in parent2.items():
        exact[n] = p
    for n, p in parent3.items():
        exact[n] = parent2.get(p, p)

    # 去掉罗马后缀后的名字：只在父级唯一时才收录，避免跨一级撞名
    by_base: dict[str, set[str]] = {}
    for n, l1 in exact.items():
        by_base.setdefault(_base(n), set()).add(l1)
    ambiguous = sorted(k for k, v in by_base.items() if len(v) > 1)

    mapping: dict[str, str] = {n: n for n in first}
    mapping.update(exact)
    for b, v in by_base.items():
        if len(v) == 1 and b not in first:
            mapping.setdefault(b, next(iter(v)))
    for n, l1 in LEGACY.items():
        if l1 in first:
            mapping.setdefault(n, l1)
    return mapping, ambiguous


def _verify(mapping: dict[str, str], t1: pd.DataFrame, ambiguous: list[str]) -> None:
    first = set(t1["行业名称"])
    assert len(first) == 31, f"一级行业应为 31 个，实得 {len(first)}"
    unresolved_base = [k for k in ambiguous if k in first]
    assert not unresolved_base, f"去后缀名与一级名冲突：{unresolved_base}"

    db_path = APP_DIR.parent.parent / "data" / "stock.db"
    if db_path.exists():
        import sqlite3

        con = sqlite3.connect(db_path)
        names = [r[0] for r in con.execute("SELECT DISTINCT industry FROM stocks") if r[0]]
        missing = [n for n in names if n not in first and n not in mapping and _base(n) not in mapping]
        assert not missing, f"库内 industry 名无法归并：{missing}"
        boards = [r[0] for r in con.execute(
            "SELECT board_name FROM boards WHERE board_code LIKE 'BK%'") if r[0]]
        missing_b = sorted({n for n in boards if n not in first and n not in mapping
                            and _base(n) not in mapping})
        print(f"  库内 industry 名 {len(names)} 个全部可归并；"
              f"东财板块 {len(boards)} 个，不可归并 {len(missing_b)} 个 {missing_b}")
        con.close()


def main() -> int:
    print("拉取申万三级行业表（乐咕乐股）…")
    t1, t2, t3 = fetch_levels()
    mapping, ambiguous = build(t1, t2, t3)
    print(f"  一级 {len(t1)} / 二级 {len(t2)} / 三级 {len(t3)} → 映射条目 {len(mapping)}"
          f"（去后缀撞名而弃用的 base 名 {len(ambiguous)} 个：{ambiguous}）")
    _verify(mapping, t1, ambiguous)
    rows = sorted(mapping.items())
    with OUT.open("w", encoding="utf-8", newline="") as f:
        f.write("industry,level1\n")
        for n, l1 in rows:
            f.write(f"{n},{l1}\n")
    print(f"已写入 {OUT}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
