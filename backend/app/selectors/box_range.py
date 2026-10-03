"""长箱体震荡选股（box-range）：找 3 年横着震荡、现价还在箱体内下半部的票。

复刻自视频号《老股民：不要预判下周板块轮动，真正可复用的是交易方法论》里的选股那一环
（需求文档 E:\\Project\\QoderCNProject\\.tmp-sph\\video-strategy-box-screen.md）。只做「哪些票算对的区域」，
不含买卖点、仓位、条件单——那些属于交易执行层，视频里也不是选股条件。

口径要点（箱体上下沿 = 3 年复权收盘的 80% 分位密集带，不是极值；带内占比恒 ≥80%，
所以它只当展示列，不参与筛选）见 boxrange.py 文件头。
"""
from __future__ import annotations

from .. import boxrange, screening
from .base import Selector, tradable

# 固定门槛：视频里「3 年箱体 + 不能是下跌中继 + 至少两个完整来回」这几条不给参数
HEIGHT_MAX = 120.0      # 箱体高度上限 %：再高就是「箱体之上再箱体」，口播说比较少见
BARS_MIN = 600          # 窗口内至少 600 根日线，太少算不出箱体
YEARS_MIN = 4.0         # 首根日线距今满 4 年（容纳出清段 + 3 年箱体）
CROSS_MIN = 4           # 收盘穿越箱体中线至少 4 次 = 至少两个完整来回
SLOPE_FLOOR = -3.0      # 年化斜率不低于 -3%：排除「漫长的下跌中继」（视频里的 70% 错误区域）

TIERS = ("低买候选", "高卖提示", "破位观察", "突破提示")


def _tier(pos: float) -> str:
    if pos < 0:
        return "破位观察"
    if pos <= 50:
        return "低买候选"
    if pos <= 100:
        return "高卖提示"
    return "突破提示"


def _score(tier: str, pos: float) -> float:
    """分档打分：低买 60~100（越贴箱底越高）> 高卖 30~59（越靠箱顶越该收）>
    破位 15~29（跌出箱体只等收回）> 突破 0~14（追主升正是视频反对的做法），档内不重叠。"""
    if tier == "低买候选":
        return 60 + 40 * (1 - pos / 50)
    if tier == "高卖提示":
        return 30 + 29 * (pos - 50) / 50
    if tier == "破位观察":
        return 15 + 14 * max(0.0, 1 + pos / 50)
    return 14 * max(0.0, 1 - (pos - 100) / 100)


class BoxRangeSelector(Selector):
    id = "box_range"
    name = "长箱体震荡"
    description = (
        "三年横成一口箱子：回看 750 个交易日，80% 的收盘价挤在一个比值最窄的价格带里，"
        "箱体高度（带顶÷带底-1）够做一波差价、三年回归斜率接近水平、收盘反复穿越中线至少 4 次，"
        "并且现价已经脱离三年最低收盘一段距离（不是还在下跌中继里）。命中后按现价在箱体内的位置分四档："
        "低买候选（0~50%）／高卖提示（50~100%）／破位观察（<0%）／突破提示（>100%）。"
        "要跑满 3 年日线，所以第一次跑要半分钟到 1 分钟；剔除 ST 和当日没有新K线的票。")
    params_schema = [
        {"key": "height_min", "label": "箱体高度 下限(%)", "type": "number",
         "default": 60, "min": 20, "max": 200},
        {"key": "slope_abs_max", "label": "三年年化斜率 绝对值上限(%)", "type": "number",
         "default": 5, "min": 0.5, "max": 20},
        {"key": "rebound_min", "label": "现价÷3年最低收盘 下限(倍)", "type": "number",
         "default": 1.3, "min": 1.0, "max": 3.0},
        {"key": "dip_bonus", "label": "近60日挖坑后收回 加分（0=不计分）", "type": "number",
         "default": 10, "min": 0, "max": 20},
    ]

    def select(self, universe: dict[str, dict], params: dict) -> list[dict]:
        screening.ensure_box(universe)
        h_lo, h_hi = params["height_min"], HEIGHT_MAX
        if h_lo >= h_hi:
            raise ValueError("「箱体高度 下限」要小于 120%，否则没有票能落进区间")
        out = []
        for code, ctx in universe.items():
            if not tradable(ctx):
                continue
            b = ctx.get("box") or {}
            height, pos = b.get("box_height"), b.get("box_pos")
            slope, cross = b.get("box_slope_3y"), b.get("box_cross")
            rebound, years = b.get("box_rebound"), b.get("box_years")
            if None in (height, pos, slope, cross, rebound, years):
                continue
            if b.get("box_bars", 0) < BARS_MIN or years < YEARS_MIN:
                continue
            if not (h_lo <= height <= h_hi):
                continue
            if abs(slope) > params["slope_abs_max"] or slope < SLOPE_FLOOR:
                continue
            if cross < CROSS_MIN or rebound < params["rebound_min"]:
                continue
            tier = _tier(pos)
            dip = bool(b.get("box_dip_60d"))
            score = _score(tier, pos)
            if dip and tier in ("低买候选", "高卖提示"):
                score = min(score + params["dip_bonus"], 100.0)
            out.append({
                "code": code,
                "score": round(score, 1),
                "reason": (f"【{tier}】现价在箱体 {pos:.1f}%、箱体高 {height:.1f}%、"
                           f"三年穿越 {cross} 次、斜率 {slope:+.2f}%/年"
                           f"{'、近60日挖坑后收回' if dip else ''}"),
            })
        return out
