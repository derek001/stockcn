"""行业分组键：把个股 `stocks.industry` 归并到 31 个申万一级行业。

`industry` 是混源的：主板票拿到的是一级板块名（腾讯列表），科创板/北交所票拿到的是
细分行业名（东财 f100，如「半导体」「军工电子Ⅱ」）。所以它不能直接当分组键，
`board_code` 更不能（一列里同时有 `pt018…` 和 `BK…` 两套口径）。

映射表 `industry_l1.csv` 由 `backend/scripts/build_industry_map.py` 从申万三级
行业表生成，含历史叫法补丁；运行期只读本地文件，不联网。表随分类调整而更新，
归并不出的一律落到 `UNCLASSIFIED`（页面可见，不会静默并错行业）。
"""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

_MAP_PATH = Path(__file__).with_name("industry_l1.csv")
UNCLASSIFIED = "未分类"
# 申万/东财用罗马后缀区分同名跨级行业（银行Ⅱ、特钢Ⅲ）
_SUFFIX = "ⅠⅡⅢⅣⅤ"


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    name2l1: dict[str, str] = {}
    with _MAP_PATH.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name2l1[row["industry"]] = row["level1"]
    l1names: dict[str, set[str]] = {}
    for n, l1 in name2l1.items():
        l1names.setdefault(l1, set()).add(n)
    return name2l1, {k: tuple(sorted(v)) for k, v in l1names.items()}


def _strip_suffix(name: str) -> str:
    while name and name[-1] in _SUFFIX:
        name = name[:-1]
    return name


def first_level(name: str | None) -> str:
    """个股 `industry` 名 → 一级行业名；认不出的一律返回「未分类」。"""
    if not name:
        return UNCLASSIFIED
    name2l1, _ = _index()
    return name2l1.get(name) or name2l1.get(_strip_suffix(name)) or UNCLASSIFIED


def level1_names() -> list[str]:
    _, l1names = _index()
    return sorted(l1names)


def member_names(level1: str) -> tuple[str, ...]:
    """归属该一级行业的所有 `industry` 取值（含一级名自身与下辖细分名）。"""
    if level1 == UNCLASSIFIED:
        return ()
    _, l1names = _index()
    return l1names.get(level1, (level1,))
