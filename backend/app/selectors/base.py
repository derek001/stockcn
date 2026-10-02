"""选股策略插件框架。

往本目录放一个新 .py、继承 Selector 并给出 id，就会被自动发现，直接出现在
「策略选股」页上可运行——和 strategies/ 的买入卖出信号插件同构，只是产出的是股票名单。

选股器只回答「哪些股票入选」，不碰行情口径：读的是 screening.load_universe() 已经算好的
上下文（基本面快照 + 最近一期财报 + 260 根复权日线的指标），所以和手动选股/自动选股共用同一套字段。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

# 这些是最后一根K线的指标值，存在 ctx["tech"] 里；其余字段在 ctx 顶层
# （pct_chg 属于顶层的当日快照口径，与 screening.eval_condition 保持一致；
#   close 是复权收盘，只用于算强度，不当作行情口径暴露给页面）
TECH_KEYS = {
    "close", "ma5", "ma10", "ma20", "ma60", "dif", "dea", "macd", "rsi",
    "k", "d", "boll_up", "boll_mid", "boll_low",
    "vol_ratio", "chg_20d", "chg_60d",
}


class Selector(ABC):
    id: str = ""
    name: str = ""
    description: str = ""
    # [{key,label,type(number),default,min?,max?}]
    params_schema: list[dict] = []

    @abstractmethod
    def select(self, universe: dict[str, dict], params: dict) -> list[dict]:
        """universe 即 screening.load_universe() 的结果。

        返回 [{"code","score","reason"}]，score 只用于排序（0~100），不参与命中判断。
        """

    def meta(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "params_schema": self.params_schema,
        }


def resolve_params(schema: list[dict], overrides: dict | None) -> dict:
    """把页面传来的参数覆盖值与默认值合并，并做类型/范围校验（越界直接抛 ValueError）。"""
    out: dict = {}
    for p in schema:
        key = p["key"]
        label = p.get("label") or key
        raw = (overrides or {}).get(key, p.get("default"))
        try:
            val = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"参数「{label}」要填数字，当前是 {raw!r}")
        lo, hi = p.get("min"), p.get("max")
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            raise ValueError(f"参数「{label}」只能在 {lo} ~ {hi} 之间，当前是 {val:g}")
        out[key] = val
    return out


def metric(ctx: dict, field: str):
    """取字段值：指标走 tech 快照，其余取顶层（市值/成交额在 load_universe 里已换成亿）。"""
    if field in TECH_KEYS:
        return (ctx.get("tech") or {}).get(field)
    return ctx.get(field)


def num(ctx: dict, field: str) -> float | None:
    v = metric(ctx, field)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def signal(ctx: dict, key: str) -> bool:
    return bool(((ctx.get("tech") or {}).get("signals") or {}).get(key))


def tradable(ctx: dict) -> bool:
    """剔除 ST/*ST/退市风险股，以及当日没有新K线的（停牌或本地缺文件）。"""
    name = (ctx.get("name") or "").upper()
    if "ST" in name or "退" in name:
        return False
    snap, bar = ctx.get("trade_date"), ctx.get("kline_date")
    if not bar:
        return False
    if snap and bar != snap:
        return False
    return True


def ramp(value: float, floor: float) -> float:
    """命中强度：达到 floor 得 0.5、达到 2×floor 封顶 1.0，用于算 score。floor 非正数时给满分。"""
    if floor is None or floor <= 0:
        return 1.0
    return max(0.0, min(value / (2 * floor), 1.0))


def get_selector(selector_id: str) -> Selector | None:
    from . import discover
    return discover().get(selector_id)
